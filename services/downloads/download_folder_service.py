"""Download folder monitoring service.

Tracks physical download folders on disk and their metadata:
- Resolves folders to album/artist information.
- Checks folder groupings for MusicBrainz matches.
- Provides download folder contents for queue status display.

Uses ``services.infrastructure.filesystem_service`` for all disk I/O.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path
from datetime import datetime

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

logger = logging.getLogger(__name__)

_MB_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


# =============================================================================
# FILE HELPERS
# =============================================================================

SUPPORTED_AUDIO_FORMATS = {
    ".mp3", ".flac", ".m4a", ".ogg", ".wav", ".aac", ".wma"
}


def get_folder_groups_with_musicbrainz():
    """
    Combined folder groups including MusicBrainz releases.
    """

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
                }
            })

        return {
            "success": True,
            "count": len(folder_groups),
            "folder_groups": folder_groups
        }

    except Exception as e:
        logger.error("[FOLDER_GROUPS] Error: %s", e, exc_info=True)
        return {"success": False, "error": str(e), "folder_groups": []}


def get_folder_groups():
    return get_folder_groups_with_musicbrainz()


def get_folder_details(folder_path: str):
    return get_folder_group_details(folder_path)


def cancel_folder(folder_path: str):
    return cancel_folder_downloads(folder_path)


# -----------------------------------------------------------------------------
# UNMATCHED FOLDERS (folders on disk not tracked as MusicBrainz releases)
# -----------------------------------------------------------------------------

def _tracked_monitoring_folders() -> set[str]:
    """Normalized monitoring folder paths of active releases."""
    tracked: set[str] = set()
    try:
        for release in get_active_releases_with_progress():
            folder = release.get("monitoring_folder_path") or release.get("monitoring_folder")
            if folder:
                tracked.add(os.path.normpath(str(folder)))
    except Exception as exc:
        logger.debug("[FOLDER_GROUPS] Tracked-folder lookup failed: %s", exc)
    return tracked


def _imported_source_paths() -> set[str]:
    """Normalized download-side file paths of imported queue rows."""
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
        logger.debug("[FOLDER_GROUPS] Imported-path lookup failed: %s", exc)
    return imported


def get_unmatched_folders() -> dict:
    """List folders under the downloads directory that are NOT tracked as
    MusicBrainz releases (the monitor page's "Matched Folders in Downloads"
    section). A folder whose audio files have all been imported to the
    library is marked ``matched`` — it is safe to delete.
    """
    try:
        downloads_dir = resolve_downloads_dir()
        if not os.path.isdir(downloads_dir):
            return {"success": True, "count": 0, "folders": []}

        archive_dir = resolve_original_archive_dir()

        tracked = _tracked_monitoring_folders()
        imported = _imported_source_paths()

        folders = []
        for entry in sorted(os.listdir(downloads_dir)):
            full = os.path.join(downloads_dir, entry)
            if not os.path.isdir(full):
                continue
            # Never surface the torrent root, the FLAC conversion archive or
            # hidden/system dirs.
            if entry == "torrents" or entry.startswith(".") or entry.startswith("__"):
                continue
            if os.path.normpath(full) == archive_dir:
                continue
            if os.path.normpath(full) in tracked:
                continue

            files = _get_files_in_folder(full)
            audio = [f for f in files if f.get("is_audio")]
            # Matched = every audio file's source path was imported to the
            # library (the import moves files, so remaining audio means the
            # folder is still in progress).
            matched = all(
                os.path.normpath(os.path.join(full, f["name"])) in imported
                for f in audio
            ) if audio else False
            folders.append({
                "type": "unmatched",
                "name": full,
                "display_name": entry,
                "files": files,
                "audio_count": len(audio),
                "file_count": len(files),
                "total_size": sum(f.get("size", 0) for f in files),
                "status": "matched" if matched else "unmatched",
            })

        return {"success": True, "count": len(folders), "folders": folders}
    except Exception as exc:
        logger.error("[UNMATCHED_FOLDERS] Error: %s", exc, exc_info=True)
        return {"success": False, "error": str(exc), "folders": []}


def delete_download_folder(folder_path: str) -> dict:
    """Delete a folder under the downloads directory (safety-railed)."""
    try:
        downloads_dir = resolve_downloads_dir()
        folder_abs = os.path.abspath(folder_path or "")
        downloads_abs = os.path.abspath(downloads_dir or "")
        if not is_path_under_directory(folder_abs, downloads_abs) or folder_abs == downloads_abs:
            return {"success": False, "error": f"Unsafe folder path for deletion: {folder_path}"}
        if not os.path.isdir(folder_abs):
            return {"success": False, "error": f"Folder not found: {folder_path}"}
        shutil.rmtree(folder_abs)
        logger.info("[FOLDER_DELETE] Deleted %s", folder_abs)
        return {"success": True, "deleted": folder_abs}
    except Exception as exc:
        logger.error("[FOLDER_DELETE] Error: %s", exc, exc_info=True)
        return {"success": False, "error": str(exc)}


def auto_delete_imported_folders() -> int:
    """Delete unmatched folders whose audio files have all been imported.

    Runs in the worker's maintenance cycle: once every file of a download
    folder has been moved into the library (queue rows imported), the stale
    folder (leftover covers/nfo/empty dirs) is removed automatically.
    """
    deleted = 0
    try:
        downloads_dir = resolve_downloads_dir()
        if not os.path.isdir(downloads_dir):
            return 0
        archive_dir = resolve_original_archive_dir()
        tracked = _tracked_monitoring_folders()
        imported = _imported_source_paths()
        for entry in sorted(os.listdir(downloads_dir)):
            full = os.path.join(downloads_dir, entry)
            if not os.path.isdir(full):
                continue
            if entry == "torrents" or entry.startswith(".") or entry.startswith("__"):
                continue
            if os.path.normpath(full) == archive_dir:
                continue
            if os.path.normpath(full) in tracked:
                continue
            files = _get_files_in_folder(full)
            audio = [f for f in files if f.get("is_audio")]
            if not audio:
                # Folder fully moved out (or never held audio) — stale.
                continue
            if all(
                os.path.normpath(os.path.join(full, f["name"])) in imported
                for f in audio
            ):
                shutil.rmtree(full)
                deleted += 1
                logger.info("[FOLDER_DELETE] Auto-deleted fully-imported folder %s", full)
    except Exception as exc:
        logger.error("[AUTO_DELETE_FOLDERS] Error: %s", exc, exc_info=True)
    return deleted


def _extract_mb_id(value: str) -> str:
    """Extract a MusicBrainz UUID from an ID, URL or bare string."""
    match = _MB_ID_RE.search(value or "")
    return match.group(0) if match else ""


def match_folder_to_release(folder_path: str, mb_id: str) -> dict:
    """Copy an unmatched download folder into the library as a MusicBrainz
    release, using the configured naming convention, then delete the folder.

    ``mb_id`` accepts a MusicBrainz release or release-group URL/ID.
    """
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
        from services.downloads.download_organize_helpers import move_track_to_library
        from helpers.metadata_reader import read_mp3_metadata

        client = MusicBrainzHttpClient()

        # Resolve release metadata (release-group IDs resolve via a
        # representative release; release IDs are used directly).
        release_data = client.get_release(mb_id, inc="artist-credits+recordings+media", timeout=15.0)
        release_mbid = mb_id
        if not release_data:
            # Try as a release-group.
            rg = client.get_release_group(mb_id, timeout=15.0)
            if not rg:
                return {"success": False, "error": "MusicBrainz could not resolve that release/release-group"}
            release_search = client.get(
                "release",
                params={"release-group": mb_id, "limit": 1, "fmt": "json"},
                timeout=15.0,
            )
            releases = (release_search or {}).get("releases") or []
            if not releases:
                return {"success": False, "error": "No release found for that release-group"}
            release_mbid = releases[0]["id"]
            release_data = client.get_release(release_mbid, inc="artist-credits+recordings+media", timeout=15.0)

        artist_credit = release_data.get("artist-credit") or []
        album_artist = " ".join(
            str(part.get("name") or part if isinstance(part, dict) else part)
            for part in artist_credit
        ).strip() or "Unknown Artist"
        album = (release_data.get("title") or "").strip() or "Unknown Album"
        year = (release_data.get("date") or "")[:4]

        # Track list from the release (media → tracks) for numbering.
        mb_tracks: list[dict] = []
        for medium in release_data.get("media") or []:
            for trk in medium.get("tracks") or []:
                mb_tracks.append({
                    "title": str(trk.get("title") or "").strip(),
                    "number": trk.get("number"),
                })

        from helpers.normalization_service import normalize_title_for_lookup, edition_annotations_compatible

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
            track_meta: dict = {}
            try:
                track_meta = read_mp3_metadata(src) or {}
            except Exception:
                track_meta = {}

            title = str(track_meta.get("title") or "").strip()
            track_artist = str(track_meta.get("artist") or "").strip() or album_artist
            number = track_meta.get("track_number")

            # Match the file to the MB tracklist when possible so numbering
            # follows the release.
            if not title or number is None:
                match_title = title or Path(src).stem
                norm_title = normalize_title_for_lookup(match_title)
                for mb_trk in mb_tracks:
                    if not mb_trk["title"]:
                        continue
                    # An edition-annotated file ("Valhalla (Epic Edition)")
                    # must not be renumbered/retitled against the plain
                    # "Valhalla" MB track — normalize_title_for_lookup strips
                    # brackets on both sides, so the edition suffix is
                    # otherwise invisible.
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
            else:
                errors.append(f"{Path(src).name}: {result.get('error')}")

        # Auto-delete the folder once matched/copied (legacy parity).
        try:
            shutil.rmtree(folder_abs)
        except Exception as exc:
            errors.append(f"folder cleanup: {exc}")

        logger.info(
            "[FOLDER_MATCH] Matched %s → '%s - %s' (%s): %d file(s) moved",
            folder_abs, album_artist, album, year, moved,
        )
        return {
            "success": True,
            "moved": moved,
            "errors": errors,
            "album_artist": album_artist,
            "album": album,
            "year": year,
        }
    except Exception as exc:
        logger.error("[FOLDER_MATCH] Error: %s", exc, exc_info=True)
        return {"success": False, "error": str(exc)}


# -----------------------------------------------------------------------------

def retry_matching_for_release(release_id: str):
    """
    Returns unmatched tracks (future matching hook).
    """

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
                    "artist": r[2]
                }
                for r in unmatched
            ],
        }

    except Exception as e:
        logger.error("[RETRY_MATCH] Error: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


# -----------------------------------------------------------------------------

def cancel_folder_downloads(folder_path: str):
    """
    Cancel downloads associated with a folder.
    """

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
        logger.error("[CANCEL_FOLDER] Error: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


def check_folder_duplicates(folder_path: str, data: dict) -> dict:
    """Check a folder for duplicate queue items."""
    try:
        from db.repositories.queue import get_queue_items_by_folder
        items = get_queue_items_by_folder(folder_path)
        return {"success": True, "duplicates": items or [], "count": len(items or [])}
    except Exception as e:
        logger.error("[check_folder_duplicates] Error: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


def process_album_existing(data: dict) -> dict:
    """Process an existing album match from queue data."""
    try:
        queue_id = (data or {}).get("queue_id")
        if not queue_id:
            return {"success": False, "error": "queue_id required"}
        from services.downloads.match_orchestrator import apply_mbid_match_batch
        return apply_mbid_match_batch(queue_ids=[int(queue_id)], new_mbid="", expand_tracks=False)
    except Exception as e:
        logger.error("[process_album_existing] Error: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}