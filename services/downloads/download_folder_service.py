"""Download folder monitoring service.

Tracks physical download folders on disk and their metadata:
- Resolves folders to album/artist information.
- Checks folder groupings for MusicBrainz matches.
- Provides download folder contents for queue status display.

Uses ``services.infrastructure.filesystem_service`` for all disk I/O.
"""

from __future__ import annotations

import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

import structlog
from sqlalchemy import text

from db.engine import db_session
from services.infrastructure.filesystem_service import (
    _get_files_in_folder,
    get_folder_group_details,
    is_path_under_directory,
    resolve_downloads_dir,
    resolve_original_archive_dir,
)
from services.metadata.release_service import get_active_releases_with_progress

logger = structlog.get_logger(__name__)

_MB_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)

SUPPORTED_AUDIO_FORMATS = {
    ".mp3", ".flac", ".m4a", ".ogg", ".wav", ".aac", ".wma",
}


def get_folder_groups_with_musicbrainz() -> dict[str, Any]:
    """Combined folder groups including MusicBrainz releases."""
    try:
        releases = get_active_releases_with_progress()
        folder_groups = []

        for release in releases:
            folder = release.get("monitoring_folder_path") or release.get("monitoring_folder")
            if not folder:
                continue

            folder_groups.append({
                "type": "musicbrainz",
                "name": folder,
                "display_name": (
                    f"{release.get('release_title') or release.get('title') or 'Unknown'} "
                    f"({release.get('artist') or 'Unknown'} - {release.get('release_year') or 'Unknown'})"
                ),
                "release_id": release.get("release_id"),
                "total_tracks": release.get("total_tracks", 0),
                "discovered_count": release.get("discovered_count", 0),
                "organized_count": release.get("organized_count", 0),
                "finalized_count": release.get("finalized_count", 0),
                "progress_percent": release.get("progress_percent", 0),
                "status": release.get("status", "active"),
                "files": _get_files_in_folder(folder),
                "metadata": {
                    "artist": release.get("artist"),
                    "album": release.get("release_title") or release.get("title"),
                    "year": release.get("release_year"),
                    "source": "musicbrainz",
                },
            })

        return {
            "success": True,
            "count": len(folder_groups),
            "folder_groups": folder_groups,
        }

    except Exception as e:
        logger.error("Error getting folder groups", error=str(e), exc_info=True)
        return {"success": False, "error": str(e), "folder_groups": []}


def get_folder_groups() -> dict[str, Any]:
    return get_folder_groups_with_musicbrainz()


def get_folder_details(folder_path: str) -> dict[str, Any]:
    return get_folder_group_details(folder_path)


def cancel_folder(folder_path: str) -> dict[str, Any]:
    return cancel_folder_downloads(folder_path)


def _tracked_monitoring_folders() -> set[str]:
    tracked: set[str] = set()
    try:
        for release in get_active_releases_with_progress():
            folder = release.get("monitoring_folder_path") or release.get("monitoring_folder")
            if folder:
                tracked.add(os.path.normpath(str(folder)))
    except Exception as exc:
        logger.debug("Tracked-folder lookup failed", error=str(exc))
    return tracked


def _imported_source_paths() -> set[str]:
    imported: set[str] = set()
    try:
        with db_session() as session:
            result = session.execute(text(
                "SELECT matched_file_path FROM download_queue "
                "WHERE status = 'imported' AND matched_file_path IS NOT NULL "
                "AND TRIM(matched_file_path) != ''"
            ))
            for row in result.fetchall() or []:
                path = row[0]
                if path:
                    imported.add(os.path.normpath(str(path)))
    except Exception as exc:
        logger.debug("Imported-path lookup failed", error=str(exc))
    return imported


def _read_audio_metadata(folder_path: str, file_name: str) -> dict[str, str]:
    try:
        from helpers.metadata_reader import read_mp3_metadata
        full = os.path.join(folder_path, file_name)
        meta = read_mp3_metadata(full) or {}
        return {
            "artist": str(meta.get("artist") or "").strip(),
            "album": str(meta.get("album") or "").strip(),
            "title": str(meta.get("title") or "").strip(),
        }
    except Exception as exc:
        logger.debug("Metadata read failed", folder=folder_path, file=file_name, error=str(exc))
        return {"artist": "", "album": "", "title": ""}


def _derive_folder_group(folder_name: str, files: list[dict[str, Any]]) -> dict[str, str]:
    artists: set[str] = set()
    albums: set[str] = set()
    for f in files:
        if not f.get("is_audio"):
            continue
        if f.get("artist"):
            artists.add(str(f["artist"]))
        if f.get("album"):
            albums.add(str(f["album"]))

    if len(artists) == 1 and len(albums) == 1:
        artist = next(iter(artists))
        album = next(iter(albums))
        if artist and album:
            return {
                "artist": artist,
                "album": album,
                "group_key": f"{artist} :: {album}",
            }

    return {
        "artist": "",
        "album": "",
        "group_key": folder_name,
    }


def _is_torrents_root(name: str) -> bool:
    return str(name or "").strip().lower() == "torrents"


def _iter_matched_folder_candidates(downloads_dir: str, archive_dir: str) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    try:
        entries = sorted(os.listdir(downloads_dir))
    except Exception as exc:
        logger.debug("listdir failed", downloads_dir=downloads_dir, error=str(exc))
        return candidates
        
    for entry in entries:
        full = os.path.join(downloads_dir, entry)
        if not os.path.isdir(full):
            continue
        if entry.startswith(".") or entry.startswith("__"):
            continue
        if os.path.normpath(full) == os.path.normpath(archive_dir):
            continue
        if _is_torrents_root(entry):
            try:
                sub_entries = sorted(os.listdir(full))
            except Exception as exc:
                logger.debug("torrents listdir failed", path=full, error=str(exc))
                sub_entries = []
            for sub in sub_entries:
                sub_full = os.path.join(full, sub)
                if not os.path.isdir(sub_full):
                    continue
                if sub.startswith(".") or sub.startswith("__"):
                    continue
                if os.path.normpath(sub_full) == os.path.normpath(archive_dir):
                    continue
                candidates.append((sub_full, sub))
            continue
        candidates.append((full, entry))
    return candidates


def _iter_torrent_album_candidates(downloads_dir: str, archive_dir: str) -> list[tuple[str, str]]:
    try:
        entries = sorted(os.listdir(downloads_dir))
    except Exception as exc:
        logger.debug("torrent-root listdir failed", downloads_dir=downloads_dir, error=str(exc))
        return []
        
    albums: list[tuple[str, str]] = []
    for entry in entries:
        if not _is_torrents_root(entry):
            continue
        root = os.path.join(downloads_dir, entry)
        try:
            sub_entries = sorted(os.listdir(root))
        except Exception as exc:
            logger.debug("torrents listdir failed", path=root, error=str(exc))
            sub_entries = []
        for sub in sub_entries:
            sub_full = os.path.join(root, sub)
            if not os.path.isdir(sub_full):
                continue
            if sub.startswith(".") or sub.startswith("__"):
                continue
            if os.path.normpath(sub_full) == os.path.normpath(archive_dir):
                continue
            albums.append((sub_full, sub))
    return albums


def _resolve_folder_match(
    folder_abs: str,
    *,
    match_rows: dict[str, dict] | None = None,
) -> dict | None:
    if match_rows is None:
        try:
            from db.repositories.folder_match_repository import get_all_folder_matches
            match_rows = {
                os.path.normpath(m.get("folder_path") or ""): m
                for m in get_all_folder_matches()
            }
        except Exception as exc:
            logger.debug("match lookup failed", error=str(exc))
            return None

    stored = match_rows.get(os.path.normpath(folder_abs))
    if stored:
        return stored

    try:
        parent = os.path.dirname(os.path.normpath(folder_abs))
        if _is_torrents_root(os.path.basename(parent)):
            stored = match_rows.get(os.path.normpath(parent))
            if stored:
                return stored
    except Exception:
        pass
    return None


def get_unmatched_folders() -> dict[str, Any]:
    """List folders under the downloads directory that are NOT tracked."""
    try:
        downloads_dir = resolve_downloads_dir()
        if not os.path.isdir(downloads_dir):
            return {"success": True, "count": 0, "folders": []}

        archive_dir = resolve_original_archive_dir()
        tracked = _tracked_monitoring_folders()
        imported = _imported_source_paths()

        try:
            from db.repositories.folder_match_repository import get_all_folder_matches
            match_rows = {
                os.path.normpath(m.get("folder_path") or ""): m
                for m in get_all_folder_matches()
            }
        except Exception as exc:
            logger.debug("folder-match load skipped", error=str(exc))
            match_rows = {}

        folders = []
        for full, entry in _iter_matched_folder_candidates(downloads_dir, archive_dir):
            if os.path.normpath(full) in tracked:
                continue

            files = _get_files_in_folder(full)
            for f in files:
                if f.get("is_audio"):
                    f.update(_read_audio_metadata(full, f.get("name") or ""))
                    f["imported"] = bool(
                        os.path.normpath(os.path.join(full, f.get("name") or "")) in imported
                    )
            audio = [f for f in files if f.get("is_audio")]
            matched = all(
                os.path.normpath(os.path.join(full, f["name"])) in imported
                for f in audio
            ) if audio else False
            
            group = _derive_folder_group(entry, files)
            stored = _resolve_folder_match(full, match_rows=match_rows)
            
            folders.append({
                "type": "unmatched",
                "name": full,
                "display_name": entry,
                "files": files,
                "audio_count": len(audio),
                "file_count": len(files),
                "total_size": sum(f.get("size", 0) for f in files),
                "status": "matched" if (matched or stored) else "unmatched",
                "match": stored,
                "release_mbid": (stored or {}).get("release_mbid"),
                "artist": group["artist"],
                "album": group["album"],
                "group_key": group["group_key"],
            })

        folders.sort(
            key=lambda x: (
                not bool(x.get("artist") and x.get("album")),
                str(x.get("artist") or "").lower(),
                str(x.get("album") or "").lower(),
                str(x.get("display_name") or "").lower(),
            )
        )

        return {"success": True, "count": len(folders), "folders": folders}
    except Exception as exc:
        logger.error("Error getting unmatched folders", error=str(exc), exc_info=True)
        return {"success": False, "error": str(exc), "folders": []}


def refresh_folder_matches() -> dict[str, Any]:
    """Re-sync stored folder → release associations."""
    try:
        from db.repositories.folder_match_repository import (
            delete_folder_match,
            get_all_folder_matches,
            upsert_folder_match,
        )
        downloads_dir = resolve_downloads_dir()
        archive_dir = resolve_original_archive_dir()

        matches = get_all_folder_matches()
        details: list[dict[str, Any]] = []
        updated = 0
        
        for m in matches:
            folder_path = os.path.normpath(str(m.get("folder_path") or ""))
            if not os.path.basename(folder_path) or not _is_torrents_root(os.path.basename(folder_path)):
                continue
            if not os.path.isdir(folder_path):
                continue

            albums = _iter_torrent_album_candidates(downloads_dir, archive_dir)
            if not albums:
                continue

            release_mbid = m.get("release_mbid") or ""
            for album_abs, _album_name in albums:
                upsert_folder_match(
                    folder_path=album_abs,
                    release_mbid=release_mbid,
                    release_title=m.get("release_title"),
                    artist=m.get("artist"),
                    release_year=m.get("release_year"),
                    status="matched",
                )
                updated += 1
                details.append({
                    "from": folder_path,
                    "to": album_abs,
                    "release_mbid": release_mbid,
                })
            delete_folder_match(folder_path)

        logger.info("Refreshed folder matches", updated_count=updated)
        return {"success": True, "updated": updated, "details": details}
    except Exception as exc:
        logger.error("Folder match refresh error", error=str(exc), exc_info=True)
        return {"success": False, "error": str(exc), "updated": 0, "details": []}


def delete_download_folder(folder_path: str) -> dict[str, Any]:
    """Delete a folder under the downloads directory."""
    try:
        downloads_dir = resolve_downloads_dir()
        folder_abs = os.path.abspath(folder_path or "")
        downloads_abs = os.path.abspath(downloads_dir or "")
        
        if not is_path_under_directory(folder_abs, downloads_abs) or folder_abs == downloads_abs:
            return {"success": False, "error": f"Unsafe folder path for deletion: {folder_path}"}
        if not os.path.isdir(folder_abs):
            return {"success": False, "error": f"Folder not found: {folder_path}"}
            
        shutil.rmtree(folder_abs)
        logger.info("Deleted download folder", folder=folder_abs)
        return {"success": True, "deleted": folder_abs}
    except Exception as exc:
        logger.error("Error deleting folder", error=str(exc), exc_info=True)
        return {"success": False, "error": str(exc)}


def _resolve_folder_abs(folder_path: str) -> tuple[str | None, str | None]:
    try:
        downloads_dir = resolve_downloads_dir()
        folder_abs = os.path.abspath(folder_path or "")
        downloads_abs = os.path.abspath(downloads_dir or "")
        if not is_path_under_directory(folder_abs, downloads_abs) or folder_abs == downloads_abs:
            return None, None
        return folder_abs, downloads_abs
    except Exception:
        return None, None


def get_folder_tracks(folder_path: str) -> dict[str, Any]:
    """Return the audio tracks of a Matched-Folders folder."""
    folder_abs, _ = _resolve_folder_abs(folder_path)
    if not folder_abs or not os.path.isdir(folder_abs):
        return {"success": False, "error": f"Folder not found: {folder_path}"}

    imported = _imported_source_paths()
    tracks = []
    
    files = _get_files_in_folder(folder_abs)
    for f in files:
        if not f.get("is_audio"):
            continue
            
        f.update(_read_audio_metadata(folder_abs, f.get("name") or ""))
        full_path = os.path.join(folder_abs, f.get("name") or "")
        
        try:
            size = os.path.getsize(full_path)
        except Exception:
            size = 0

        tracks.append({
            "name": f.get("name") or "",
            "full_path": full_path,
            "artist": f.get("artist") or "",
            "album": f.get("album") or "",
            "title": f.get("title") or "",
            "size": int(size),
            "imported": bool(full_path and os.path.normpath(full_path) in imported),
        })

    return {"success": True, "folder_path": folder_path, "tracks": tracks}


def delete_folder_track(folder_path: str, file_name: str) -> dict[str, Any]:
    """Delete ONE audio file from a Matched-Folders folder."""
    folder_abs, downloads_abs = _resolve_folder_abs(folder_path)
    if not folder_abs or not os.path.isdir(folder_abs):
        return {"success": False, "error": f"Folder not found: {folder_path}"}

    base = os.path.basename(file_name or "")
    if not base:
        return {"success": False, "error": "No file name provided"}
        
    full = os.path.join(folder_abs, base)
    if not is_path_under_directory(full, downloads_abs):
        return {"success": False, "error": f"Unsafe file path for deletion: {base}"}
    if not os.path.isfile(full):
        return {"success": False, "error": f"File not found: {base}"}

    imported = _imported_source_paths()
    if os.path.normpath(full) in imported:
        return {"success": False, "error": f"'{base}' was already imported to the library — refusing to delete"}

    try:
        os.remove(full)
        logger.info("Deleted folder track", path=full)
        return {"success": True, "deleted": full}
    except Exception as exc:
        logger.error("Error deleting folder track", path=full, error=str(exc))
        return {"success": False, "error": str(exc)}


def move_folder_track_to_library(folder_path: str, file_name: str) -> dict[str, Any]:
    """Move ONE track out of a Matched-Folders folder into the library."""
    folder_abs, downloads_abs = _resolve_folder_abs(folder_path)
    if not folder_abs or not os.path.isdir(folder_abs):
        return {"success": False, "error": f"Folder not found: {folder_path}"}

    base = os.path.basename(file_name or "")
    if not base:
        return {"success": False, "error": "No file name provided"}
        
    full = os.path.join(folder_abs, base)
    if not is_path_under_directory(full, downloads_abs):
        return {"success": False, "error": f"Unsafe file path: {base}"}
    if not os.path.isfile(full):
        return {"success": False, "error": f"File not found: {base}"}

    try:
        meta = _read_audio_metadata(folder_abs, base)
    except Exception:
        meta = {}

    artist = (meta.get("artist") or "").strip() or os.path.basename(os.path.dirname(folder_abs)) or "Unknown Artist"
    album = (meta.get("album") or "").strip() or os.path.basename(folder_abs) or "Unknown Album"
    title = (meta.get("title") or "").strip() or os.path.splitext(base)[0]

    try:
        from services.downloads.download_organize_helpers import move_track_to_library
        _music_root = os.environ.get("MUSIC_ROOT", "/music")
        track = {"file_path": full, "artist": artist, "title": title}
        release_metadata = {"album_artist": artist, "album": album}
        
        move_result = move_track_to_library(track, release_metadata, _music_root)
        if not move_result.get("success"):
            return {"success": False, "error": move_result.get("error") or "move failed"}
            
        target_path = move_result.get("target_path") or ""

        try:
            from db.repositories.queue import insert_queue_item
            insert_queue_item(
                artist=artist,
                title=title,
                album=album,
                source="folder_track",
                status="imported",
                file_path=target_path,
                found_filename=base,
                import_group="manual",
            )
        except Exception as _qexc:
            logger.debug("Queue record skipped", error=str(_qexc))

        return {
            "success": True,
            "target_path": target_path,
            "moved": 1,
            "artist": artist,
            "album": album,
            "title": title,
        }
    except Exception as exc:
        logger.error("Error moving folder track", path=full, error=str(exc), exc_info=True)
        return {"success": False, "error": str(exc)}


def auto_delete_imported_folders() -> int:
    """Delete unmatched folders whose audio files have all been imported."""
    deleted = 0
    try:
        downloads_dir = resolve_downloads_dir()
        if not os.path.isdir(downloads_dir):
            return 0
            
        archive_dir = resolve_original_archive_dir()
        tracked = _tracked_monitoring_folders()
        imported = _imported_source_paths()
        
        for full, _entry in _iter_matched_folder_candidates(downloads_dir, archive_dir):
            if os.path.normpath(full) in tracked:
                continue
            files = _get_files_in_folder(full)
            audio = [f for f in files if f.get("is_audio")]
            if not audio:
                continue
            if all(
                os.path.normpath(os.path.join(full, f["name"])) in imported
                for f in audio
            ):
                shutil.rmtree(full)
                deleted += 1
                logger.info("Auto-deleted fully-imported folder", path=full)
    except Exception as exc:
        logger.error("Error auto-deleting imported folders", error=str(exc), exc_info=True)
    return deleted


def _extract_mb_id(value: str) -> str:
    match = _MB_ID_RE.search(value or "")
    return match.group(0) if match else ""


def _resolve_release(client: Any, mb_id: str) -> tuple[dict | None, str]:
    try:
        data = client.get_release(mb_id, inc="artist-credits+recordings+media", timeout=15.0)
        if data and data.get("id"):
            return data, str(data["id"])
    except Exception:
        pass

    try:
        rg = client.get_release_group(mb_id, timeout=15.0)
        if not rg:
            return None, ""
            
        release_search = client.get(
            "release",
            params={"release-group": mb_id, "limit": 1, "fmt": "json"},
            timeout=15.0,
        )
        releases = (release_search or {}).get("releases") or []
        if not releases:
            return None, ""
            
        release_mbid = str(releases[0].get("id") or "")
        if not release_mbid:
            return None, ""
            
        data = client.get_release(release_mbid, inc="artist-credits+recordings+media", timeout=15.0)
        return data, release_mbid
    except Exception as exc:
        logger.debug("release-group resolution failed", mb_id=mb_id, error=str(exc))
        return None, ""


def associate_folder_to_release(folder_path: str, mb_id: str) -> dict[str, Any]:
    """Phase 1: Record folder to release association without moving files."""
    try:
        mb_id = _extract_mb_id(mb_id)
        if not mb_id:
            return {"success": False, "error": "A MusicBrainz release/release-group URL or ID is required"}

        downloads_dir = resolve_downloads_dir()
        folder_abs = os.path.abspath(folder_path or "")
        downloads_abs = os.path.abspath(downloads_dir or "")
        
        if not is_path_under_directory(folder_abs, downloads_abs) or folder_abs == downloads_abs:
            return {"success": False, "error": f"Unsafe folder path: {folder_path}"}
        if not os.path.isdir(folder_abs):
            return {"success": False, "error": f"Folder not found: {folder_path}"}

        from api_clients.musicbrainz_http import MusicBrainzHttpClient
        from db.repositories.folder_match_repository import upsert_folder_match
        from services.enrichment.musicbrainz_service import primary_album_artist

        client = MusicBrainzHttpClient()
        release_data, release_mbid = _resolve_release(client, mb_id)
        
        if release_data is None or not release_mbid:
            return {"success": False, "error": "MusicBrainz could not resolve that release/release-group"}

        artist_credit = release_data.get("artist-credit") or []
        album_artist = primary_album_artist(artist_credit) or "Unknown Artist"
        album = (release_data.get("title") or "").strip() or "Unknown Album"
        year_raw = (release_data.get("date") or "")[:4]
        
        try:
            year = int(year_raw) if year_raw.isdigit() else None
        except Exception:
            year = None

        stored = upsert_folder_match(
            folder_path=folder_abs,
            release_mbid=release_mbid,
            release_title=album,
            artist=album_artist,
            release_year=year,
            status="matched",
        )
        if stored is None:
            return {"success": False, "error": "Could not store folder match"}

        logger.info("Associated folder to release", folder=folder_abs, artist=album_artist, album=album)
        return {
            "success": True,
            "match": stored,
            "release_mbid": release_mbid,
            "album_artist": album_artist,
            "album": album,
            "year": year,
        }
    except Exception as exc:
        logger.error("Associate folder error", error=str(exc), exc_info=True)
        return {"success": False, "error": str(exc)}


def match_folder_to_release(folder_path: str, mb_id: str) -> dict[str, Any]:
    """Phase 2: Confirm match and execute migration/organization pipeline."""
    try:
        mb_id = _extract_mb_id(mb_id)
        if not mb_id:
            return {"success": False, "error": "A MusicBrainz release/release-group URL or ID is required"}

        downloads_dir = resolve_downloads_dir()
        folder_abs = os.path.abspath(folder_path or "")
        downloads_abs = os.path.abspath(downloads_dir or "")
        
        if not is_path_under_directory(folder_abs, downloads_abs) or folder_abs == downloads_abs:
            return {"success": False, "error": f"Unsafe folder path: {folder_path}"}
        if not os.path.isdir(folder_abs):
            return {"success": False, "error": f"Folder not found: {folder_path}"}

        from api_clients.musicbrainz_http import MusicBrainzHttpClient
        from helpers.config_helpers import get_config
        from helpers.metadata_reader import read_mp3_metadata
        from helpers.normalization_service import edition_annotations_compatible, normalize_title_for_lookup
        from services.downloads.download_organize_helpers import move_track_to_library
        from services.enrichment.musicbrainz_service import build_artist_credit_string, primary_album_artist

        client = MusicBrainzHttpClient()
        release_data, release_mbid = _resolve_release(client, mb_id)
        
        if release_data is None or not release_mbid:
            return {"success": False, "error": "MusicBrainz could not resolve that release/release-group"}

        artist_credit = release_data.get("artist-credit") or []
        album_artist = primary_album_artist(artist_credit) or "Unknown Artist"
        release_credit = build_artist_credit_string(artist_credit) if artist_credit else album_artist
        album = (release_data.get("title") or "").strip() or "Unknown Album"
        year = (release_data.get("date") or "")[:4]

        mb_tracks: list[dict[str, Any]] = []
        for medium in release_data.get("media") or []:
            for trk in medium.get("tracks") or []:
                mb_tracks.append({
                    "title": str(trk.get("title") or "").strip(),
                    "number": trk.get("number"),
                })

        moved = 0
        errors: list[str] = []
        music_root = Path(
            (get_config().get("music", {}) or {}).get("root")
            or os.environ.get("MUSIC_ROOT", "/music")
        )

        for audio in _get_files_in_folder(folder_abs):
            if not audio.get("is_audio"):
                continue
            src = os.path.join(folder_abs, audio["name"])
            
            try:
                track_meta = read_mp3_metadata(src) or {}
            except Exception:
                track_meta = {}

            title = str(track_meta.get("title") or "").strip()
            track_artist = str(track_meta.get("artist") or "").strip() or release_credit
            number = track_meta.get("track_number")

            if not title or number is None:
                match_title = title or Path(src).stem
                norm_title = normalize_title_for_lookup(match_title)
                for mb_trk in mb_tracks:
                    if not mb_trk["title"]:
                        continue
                    if not edition_annotations_compatible(match_title, mb_trk["title"]):
                        continue
                    if normalize_title_for_lookup(mb_trk["title"]) == norm_title:
                        title = mb_trk["title"]
                        number = mb_trk.get("number")
                        break

            result = move_track_to_library(
                {
                    "file_path": src,
                    "artist": track_artist,
                    "title": title or Path(src).stem,
                    "track_number": number,
                },
                {
                    "album_artist": album_artist,
                    "year": year,
                    "album": album,
                },
                music_root,
            )
            if result.get("success"):
                moved += 1
                target_path = result.get("target_path")
                if target_path:
                    # Link the just-moved file back to its download_queue item
                    # (the old system's watcher did this via
                    # mark_queue_item_matched_from_torrent).  Without this the
                    # file lands in the library but the queue item stays
                    # 'downloading'/orphaned — the reported regression where a
                    # release appears in Matched Folders but never matches the
                    # queue entry.
                    _link_moved_file_to_queue_item(
                        queue_artist=album_artist,
                        queue_album=album,
                        file_title=title or Path(src).stem,
                        track_number=number,
                        target_path=target_path,
                        release_mbid=release_mbid,
                    )
            else:
                errors.append(f"{Path(src).name}: {result.get('error')}")

        try:
            shutil.rmtree(folder_abs)
        except Exception as exc:
            errors.append(f"folder cleanup: {exc}")

        try:
            from db.repositories.folder_match_repository import delete_folder_match
            delete_folder_match(folder_abs)
        except Exception as exc:
            logger.debug("association cleanup skipped", error=str(exc))

        logger.info("Confirmed folder match and organized tracks", folder=folder_abs, moved=moved)
        return {
            "success": True,
            "moved": moved,
            "errors": errors,
            "album_artist": album_artist,
            "album": album,
            "year": year,
        }
    except Exception as exc:
        logger.error("Match folder error", error=str(exc), exc_info=True)
        return {"success": False, "error": str(exc)}


def _link_moved_file_to_queue_item(
    *,
    queue_artist: str,
    queue_album: str,
    file_title: str,
    track_number: Any,
    target_path: str,
    release_mbid: str,
) -> bool:
    """Associate a library-moved file with its download_queue item.

    Mirrors the old system's ``mark_queue_item_matched_from_torrent``: after
    confirming a folder match and moving a file to the library, the matching
    queue item is marked ``imported`` with its file paths and release MBID so
    the queue reflects the completed download (instead of staying orphaned in
    'downloading').
    """
    try:
        from db.repositories.queue import get_album_queue_tracks, update_queue_item
        from helpers.normalization_service import (
            edition_annotations_compatible,
            extract_track_disc,
            normalize_match_text,
        )

        file_title = str(file_title or "")
        file_title_norm = normalize_match_text(file_title)
        file_num, _ = extract_track_disc(str(track_number or ""))

        # Prefer album-scoped queue items; fall back to a title-only match
        # across ALL active queue items (a single-track download often has a
        # different album label on the queue item than the resolved MB
        # release, e.g. queue album "BiiiG" vs release "BiiiG (FLAC)...").
        queue_items = get_album_queue_tracks(queue_artist, queue_album)
        if not queue_items:
            try:
                from db.repositories.queue import get_active_queue
                _all = get_active_queue(limit=500) or []
                _a_norm = normalize_match_text(queue_artist)
                queue_items = [
                    q for q in _all
                    if _a_norm and (
                        _a_norm.startswith(normalize_match_text(str(q.get("artist") or ""))[:12])
                        or normalize_match_text(str(q.get("artist") or "")).startswith(_a_norm[:12])
                    )
                ]
            except Exception:
                queue_items = []
        if not queue_items:
            return False

        used: set[int] = set()
        target = None

        # 1) Prefer an exact track-number match.
        if file_num is not None:
            for q in queue_items:
                if q.get("id") in used or str(q.get("status") or "").lower() in ("imported", "completed"):
                    continue
                q_num, _ = extract_track_disc(str(q.get("track_number") or ""))
                if q_num == file_num and edition_annotations_compatible(
                    file_title, str(q.get("title") or "")
                ):
                    target = q
                    used.add(int(q["id"]))
                    break

        # 2) Fall back to normalized-title equality.
        if target is None:
            for q in queue_items:
                if q.get("id") in used or str(q.get("status") or "").lower() in ("imported", "completed"):
                    continue
                q_title_norm = normalize_match_text(str(q.get("title") or ""))
                if q_title_norm and q_title_norm == file_title_norm:
                    target = q
                    used.add(int(q["id"]))
                    break

        if target is None:
            logger.debug(
                "No queue item matched for moved file",
                artist=queue_artist, album=queue_album, title=file_title,
            )
            return False

        queue_id = int(target["id"])
        update_queue_item(
            queue_id,
            status="imported",
            file_path=target_path,
            matched_file_path=target_path,
            music_file_path=target_path,
            found_filename=os.path.basename(target_path),
            release_mbid=release_mbid,
            release_id=release_mbid,
            release_source="musicbrainz",
            copied_individually=1,
        )
        logger.info(
            "Linked moved file to queue item",
            queue_id=queue_id, target=target_path, release_mbid=release_mbid,
        )
        return True
    except Exception as exc:
        logger.warning("Could not link moved file to queue item", error=str(exc))
        return False


def retry_matching_for_release(release_id: str) -> dict[str, Any]:
    try:
        with db_session() as session:
            result = session.execute(text("""
                SELECT id, monitoring_folder_path, total_tracks, discovered_count
                FROM musicbrainz_releases
                WHERE release_id = :release_id
            """), {"release_id": release_id})

            row = result.fetchone()
            if not row:
                return {"success": False, "error": "Release not found"}

            release_db_id = row[0]
            folder = row[1]
            total_tracks = row[2]
            discovered = row[3]

            result = session.execute(text("""
                SELECT track_number, track_title, track_artist
                FROM musicbrainz_release_tracks
                WHERE release_id = :release_id
                  AND status NOT IN ('discovered', 'finalized')
            """), {"release_id": release_id})

            unmatched = result.fetchall()

        return {
            "success": True,
            "release_id": release_id,
            "folder": folder,
            "total_tracks": total_tracks,
            "discovered_count": discovered,
            "unmatched_tracks": [
                {
                    "track_number": r[0],
                    "title": r[1],
                    "artist": r[2],
                }
                for r in unmatched
            ],
        }
    except Exception as e:
        logger.error("Retry match error", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


def cancel_folder_downloads(folder_path: str) -> dict[str, Any]:
    try:
        with db_session() as session:
            result = session.execute(text("""
                SELECT id, release_id
                FROM musicbrainz_releases
                WHERE monitoring_folder_path = :folder
            """), {"folder": folder_path})

            row = result.fetchone()
            if not row:
                return {"success": False, "error": "Folder not recognized"}

            release_db_id = row[0]
            release_id = row[1]

            session.execute(text("""
                DELETE FROM download_queue
                WHERE mb_release_download_id = :id
            """), {"id": release_db_id})

            session.execute(text("""
                UPDATE musicbrainz_releases
                SET status = 'cancelled',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :id
            """), {"id": release_db_id})

        return {
            "success": True,
            "release_id": release_id,
            "folder": folder_path,
            "message": "Cancelled release downloads",
        }
    except Exception as e:
        logger.error("Cancel folder downloads error", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


def check_folder_duplicates(folder_path: str, data: dict[str, Any]) -> dict[str, Any]:
    try:
        from db.repositories.queue import get_queue_items_by_folder
        items = get_queue_items_by_folder(folder_path)
        return {"success": True, "duplicates": items or [], "count": len(items or [])}
    except Exception as e:
        logger.error("Check folder duplicates error", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


def process_album_existing(data: dict[str, Any]) -> dict[str, Any]:
    try:
        queue_id = (data or {}).get("queue_id")
        if not queue_id:
            return {"success": False, "error": "queue_id required"}
        from services.downloads.match_orchestrator import apply_mbid_match_batch
        return apply_mbid_match_batch(queue_ids=[int(queue_id)], new_mbid="", expand_tracks=False)
    except Exception as e:
        logger.error("Process album existing error", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}
