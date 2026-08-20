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


def _read_audio_metadata(folder_path: str, file_name: str) -> dict:
    """Read artist/album/title from an audio file's embedded tags.

    Best-effort: returns ``{"artist": str, "album": str, "title": str}`` —
    empty strings when tags are missing or unreadable.  Files without
    metadata fall back to folder-path grouping in ``get_unmatched_folders``.
    """
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
        logger.debug("[FOLDER] Metadata read failed for %s/%s: %s", folder_path, file_name, exc)
        return {"artist": "", "album": "", "title": ""}


def _derive_folder_group(folder_name: str, files: list[dict]) -> dict:
    """Derive a grouping key for a Matched-Folders entry.

    Groups by the Artist / Album found in the audio files' embedded metadata
    when consistent across the folder; folders without usable metadata (or
    with mixed albums) group by their folder path instead.  Returns
    ``{"artist": str, "album": str, "group_key": str}``.
    """
    artists: set[str] = set()
    albums: set[str] = set()
    for f in files:
        if not f.get("is_audio"):
            continue
        if f.get("artist"):
            artists.add(str(f["artist"]))
        if f.get("album"):
            albums.add(str(f["album"]))

    # Consistent single artist + album → group by metadata.
    if len(artists) == 1 and len(albums) == 1:
        artist = next(iter(artists))
        album = next(iter(albums))
        if artist and album:
            return {
                "artist": artist,
                "album": album,
                "group_key": f"{artist} :: {album}",
            }

    # Fallback: group by the folder path.
    return {
        "artist": "",
        "album": "",
        "group_key": folder_name,
    }


def _is_torrents_root(name: str) -> bool:
    """True when *name* is the torrents root folder (any casing).

    qBittorrent / deluge / transmission all create a ``torrents`` (or
    ``Torrents`` / ``TORRENTS``) folder under the downloads root.  The old
    case-sensitive ``entry == "torrents"`` skip let ``Torrents`` through as
    ONE folder whose ``_get_files_in_folder`` recursion (depth 3) merged
    every album subfolder into a single Matched Folder — matching the whole
    ``/Torrents`` directory instead of each album.
    """
    return str(name or "").strip().lower() == "torrents"


def _iter_matched_folder_candidates(downloads_dir: str, archive_dir: str) -> list[tuple[str, str]]:
    """Yield ``(folder_abs, display_name)`` for the Matched Folders list.

    One entry per top-level folder under ``downloads_dir`` — EXCEPT the
    torrents root, which is flattened into its album subfolders so each
    album under ``/torrents/<Album>`` gets its own Match / Confirm actions
    instead of being merged into the whole torrents directory.
    """
    candidates: list[tuple[str, str]] = []
    try:
        entries = sorted(os.listdir(downloads_dir))
    except Exception as exc:
        logger.debug("[FOLDER_CANDIDATES] listdir failed for %s: %s", downloads_dir, exc)
        return candidates
    for entry in entries:
        full = os.path.join(downloads_dir, entry)
        if not os.path.isdir(full):
            continue
        # Never surface the FLAC conversion archive or hidden/system dirs.
        if entry.startswith(".") or entry.startswith("__"):
            continue
        if os.path.normpath(full) == os.path.normpath(archive_dir):
            continue
        if _is_torrents_root(entry):
            # Flatten: one Matched Folder per album subfolder under the
            # torrents root (skip the root itself, hidden dirs, the archive).
            try:
                sub_entries = sorted(os.listdir(full))
            except Exception as exc:
                logger.debug("[FOLDER_CANDIDATES] torrents listdir failed for %s: %s", full, exc)
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


def get_unmatched_folders() -> dict:
    """List folders under the downloads directory that are NOT tracked as
    MusicBrainz releases (the monitor page's "Matched Folders in Downloads"
    section). A folder whose audio files have all been imported to the
    library is marked ``matched`` — it is safe to delete.

    The torrents root (any casing) is flattened into its album subfolders —
    each album gets its own entry so matching one album never merges every
    torrent into a single folder (the old ``entry == "torrents"`` skip also
    let ``Torrents``/``TORRENTS`` through as ONE folder containing every
    album's files).
    """
    try:
        downloads_dir = resolve_downloads_dir()
        if not os.path.isdir(downloads_dir):
            return {"success": True, "count": 0, "folders": []}

        archive_dir = resolve_original_archive_dir()

        tracked = _tracked_monitoring_folders()
        imported = _imported_source_paths()

        folders = []
        for full, entry in _iter_matched_folder_candidates(downloads_dir, archive_dir):
            if os.path.normpath(full) in tracked:
                continue

            files = _get_files_in_folder(full)
            # Read embedded Artist/Album metadata for each audio file so the
            # Matched Folders list can group by metadata (falling back to the
            # folder path for files without tags).  Each audio file also gets
            # an ``imported`` flag so the UI can show per-track actions
            # (files already moved to the library are not actionable).
            for f in files:
                if f.get("is_audio"):
                    f.update(_read_audio_metadata(full, f.get("name") or ""))
                    f["imported"] = bool(
                        os.path.normpath(os.path.join(full, f.get("name") or "")) in imported
                    )
            audio = [f for f in files if f.get("is_audio")]
            # Matched = every audio file's source path was imported to the
            # library (the import moves files, so remaining audio means the
            # folder is still in progress).
            matched = all(
                os.path.normpath(os.path.join(full, f["name"])) in imported
                for f in audio
            ) if audio else False
            group = _derive_folder_group(entry, files)
            folders.append({
                "type": "unmatched",
                "name": full,
                "display_name": entry,
                "files": files,
                "audio_count": len(audio),
                "file_count": len(files),
                "total_size": sum(f.get("size", 0) for f in files),
                "status": "matched" if matched else "unmatched",
                "match": None,
                "release_mbid": None,
                "artist": group["artist"],
                "album": group["album"],
                "group_key": group["group_key"],
            })

        # Sort by metadata group: artist then album (metadata-derived groups
        # first, folders without metadata grouped by path after).
        folders.sort(
            key=lambda x: (
                not bool(x.get("artist") and x.get("album")),
                str(x.get("artist") or "").lower(),
                str(x.get("album") or "").lower(),
                str(x.get("display_name") or "").lower(),
            )
        )

        # Merge stored folder → release associations so the Matched Folders
        # UI can render the two-state flow: folders with an association show
        # ``[Change Match] [Confirm Match]`` instead of ``[Match]``.
        try:
            from db.repositories.folder_match_repository import get_all_folder_matches
            match_rows = {os.path.normpath(m.get("folder_path") or ""): m for m in get_all_folder_matches()}
            for folder in folders:
                stored = match_rows.get(os.path.normpath(str(folder.get("name") or "")))
                if stored:
                    folder["match"] = stored
                    folder["release_mbid"] = stored.get("release_mbid")
                    folder["status"] = "matched"
        except Exception as exc:
            logger.debug("[UNMATCHED_FOLDERS] folder-match merge skipped: %s", exc)

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


# =============================================================================
# PER-TRACK ACTIONS (Matched Folders)
# =============================================================================

def _resolve_folder_abs(folder_path: str) -> tuple[str | None, str | None]:
    """Resolve a Matched-Folders folder path under the downloads dir.

    Returns ``(folder_abs, downloads_abs)`` or ``(None, None)`` when the
    path is unsafe / not under the downloads directory.
    """
    try:
        downloads_dir = resolve_downloads_dir()
        folder_abs = os.path.abspath(folder_path or "")
        downloads_abs = os.path.abspath(downloads_dir or "")
        if not is_path_under_directory(folder_abs, downloads_abs) or folder_abs == downloads_abs:
            return None, None
        return folder_abs, downloads_abs
    except Exception:
        return None, None


def _folder_files_with_metadata(folder_abs: str) -> list[dict]:
    """Read every audio file in a folder with embedded metadata + size."""
    files = _get_files_in_folder(folder_abs)
    for f in files:
        if f.get("is_audio"):
            f.update(_read_audio_metadata(folder_abs, f.get("name") or ""))
            f["full_path"] = os.path.join(folder_abs, f.get("name") or "")
            try:
                f["size"] = os.path.getsize(f["full_path"])
            except Exception:
                f["size"] = 0
    return files


def get_folder_tracks(folder_path: str) -> dict:
    """Return the audio tracks of a Matched-Folders folder, one per file.

    Each track carries the file name, embedded artist/album/title (best
    effort), size, and whether the file's source path was already imported
    to the library (``imported``).  Enables per-track actions (match/delete)
    on folders that contain multiple copies of the same track.
    """
    folder_abs, _ = _resolve_folder_abs(folder_path)
    if not folder_abs or not os.path.isdir(folder_abs):
        return {"success": False, "error": f"Folder not found: {folder_path}"}

    imported = _imported_source_paths()
    tracks = []
    for f in _folder_files_with_metadata(folder_abs):
        if not f.get("is_audio"):
            continue
        full_path = f.get("full_path") or ""
        tracks.append({
            "name": f.get("name") or "",
            "full_path": full_path,
            "artist": f.get("artist") or "",
            "album": f.get("album") or "",
            "title": f.get("title") or "",
            "size": int(f.get("size") or 0),
            "imported": bool(full_path and os.path.normpath(full_path) in imported),
        })

    return {"success": True, "folder_path": folder_path, "tracks": tracks}


def delete_folder_track(folder_path: str, file_name: str) -> dict:
    """Delete ONE audio file from a Matched-Folders folder (safety-railed).

    Only deletes an audio file under the downloads directory.  Never deletes
    a file whose source path was already imported to the library — that would
    orphan the library copy's provenance and could delete the only copy if
    the import was a move (source already gone).
    """
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
        return {"success": False, "error": f"'{base}' was already imported to the library — refusing to delete it here"}

    try:
        os.remove(full)
        logger.info("[TRACK_DELETE] Deleted %s", full)
        return {"success": True, "deleted": full}
    except Exception as exc:
        logger.error("[TRACK_DELETE] Error deleting %s: %s", full, exc)
        return {"success": False, "error": str(exc)}


def move_folder_track_to_library(folder_path: str, file_name: str) -> dict:
    """Move ONE track out of a Matched-Folders folder into the library.

    Uses the file's own embedded artist/album/title (falling back to the
    folder name) so the library copy is placed under the correct artist /
    album path.  This is the per-track equivalent of a folder "Confirm
    Match" — it moves the file via ``move_track_to_library`` and records an
    ``imported`` queue row so ``auto_delete_imported_folders`` can later
    prune the folder once every file has been imported.
    """
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

    try:
        meta = _read_audio_metadata(folder_abs, base)
    except Exception:
        meta = {}

    artist = (meta.get("artist") or "").strip() or os.path.basename(os.path.dirname(folder_abs)) or "Unknown Artist"
    album = (meta.get("album") or "").strip() or os.path.basename(folder_abs) or "Unknown Album"
    title = (meta.get("title") or "").strip() or os.path.splitext(base)[0]

    try:
        from services.downloads.download_organize_helpers import move_track_to_library
        from helpers.config_helpers import get_downloads_config
        _music_root = os.environ.get("MUSIC_ROOT", "/music")
        track = {"file_path": full, "artist": artist, "title": title}
        release_metadata = {"album_artist": artist, "album": album}
        move_result = move_track_to_library(track, release_metadata, _music_root)
        if not move_result.get("success"):
            return {"success": False, "error": move_result.get("error") or "move failed"}
        target_path = move_result.get("target_path") or ""

        # Record an imported queue row so the folder auto-prune can see this
        # file as imported (the source file was moved away).
        try:
            from db.repositories.queue import insert_queue_item
            row = insert_queue_item(
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
            logger.debug("[TRACK_MOVE] Queue record skipped: %s", _qexc)

        return {
            "success": True,
            "target_path": target_path,
            "moved": 1,
            "artist": artist,
            "album": album,
            "title": title,
        }
    except Exception as exc:
        logger.error("[TRACK_MOVE] Error moving %s: %s", full, exc, exc_info=True)
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
        for full, _entry in _iter_matched_folder_candidates(downloads_dir, archive_dir):
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


def _resolve_release(client, mb_id: str) -> tuple[dict | None, str]:
    """Resolve a MusicBrainz *release* OR *release-group* MBID to a concrete
    release payload plus its release MBID.

    The MB search modal hands over release-GROUP MBIDs (the search returns
    release-groups); ``/ws/2/release/{id}`` 404s for those.  ``get_release``
    raises ``httpx.HTTPStatusError`` on a 404 rather than returning empty, so
    the caller must catch it — this helper treats any lookup failure as "not
    a release" and falls back to browsing the release-group for a concrete
    release.

    Returns ``(release_data, release_mbid)`` or ``(None, "")`` when the ID
    cannot be resolved at all.
    """
    try:
        data = client.get_release(mb_id, inc="artist-credits+recordings+media", timeout=15.0)
        if data and data.get("id"):
            return data, str(data["id"])
    except Exception:
        # Not a release (404 etc.) — try as a release-group below.
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
        logger.debug("[FOLDER_MATCH] release-group resolution failed for %s: %s", mb_id, exc)
        return None, ""


def associate_folder_to_release(folder_path: str, mb_id: str) -> dict:
    """PHASE 1 of the two-phase folder-match flow: record the folder → release
    association WITHOUT moving any files.

    ``mb_id`` accepts a MusicBrainz release or release-group URL/ID.  The
    release is resolved so the association carries the canonical title/artist/
    year (used to pre-fill the "Confirm Match" card), but no audio file is
    tagged, formatted, or moved — the folder stays fully passive on disk.

    Returns:
        ``{"success": True, "match": {...}, "release_mbid": ...}`` on success,
        or ``{"success": False, "error": ...}``.
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
        from db.repositories.folder_match_repository import upsert_folder_match

        client = MusicBrainzHttpClient()

        # Resolve release metadata — accepts a release OR release-group MBID
        # (the MB search modal returns release-groups).  ``_resolve_release``
        # catches the 404 that ``get_release`` raises for a release-group ID.
        release_data, release_mbid = _resolve_release(client, mb_id)
        if release_data is None or not release_mbid:
            return {"success": False, "error": "MusicBrainz could not resolve that release/release-group"}

        artist_credit = release_data.get("artist-credit") or []
        album_artist = " ".join(
            str(part.get("name") or part if isinstance(part, dict) else part)
            for part in artist_credit
        ).strip() or "Unknown Artist"
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

        logger.info(
            "[FOLDER_MATCH] Associated %s → '%s - %s' (%s) — awaiting confirmation (no files moved)",
            folder_abs, album_artist, album, year,
        )
        return {
            "success": True,
            "match": stored,
            "release_mbid": release_mbid,
            "album_artist": album_artist,
            "album": album,
            "year": year,
        }
    except Exception as exc:
        logger.error("[FOLDER_MATCH] Associate error: %s", exc, exc_info=True)
        return {"success": False, "error": str(exc)}


def match_folder_to_release(folder_path: str, mb_id: str) -> dict:
    """PHASE 2 (confirm) of the two-phase folder-match flow.

    Executes the full migration pipeline for an already-associated folder:
    1. Writes MusicBrainz tags to the files (via ``move_track_to_library``).
    2. Formats the destination path using the configured naming convention.
    3. Moves the folder's audio files into the music library.
    4. Removes the folder from the Matched Folders list (deletes the
       ``folder_matches`` association and the staging folder).

    ``mb_id`` accepts a MusicBrainz release or release-group URL/ID.

    Backward compatible: callers that only ever did "match → move" (the old
    one-step flow) can keep calling this directly; the two-phase UI calls
    ``associate_folder_to_release`` first (phase 1, no move) and this
    function as ``POST /api/downloads/confirm-match`` (phase 2).
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

        # Resolve release metadata — accepts a release OR release-group MBID
        # (the MB search modal returns release-groups).  ``_resolve_release``
        # catches the 404 that ``get_release`` raises for a release-group ID.
        release_data, release_mbid = _resolve_release(client, mb_id)
        if release_data is None or not release_mbid:
            return {"success": False, "error": "MusicBrainz could not resolve that release/release-group"}

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

        # The confirm step is complete — drop the folder → release association
        # so the folder no longer appears in the Matched Folders list.
        try:
            from db.repositories.folder_match_repository import delete_folder_match
            delete_folder_match(folder_abs)
        except Exception as exc:
            logger.debug("[FOLDER_MATCH] association cleanup skipped: %s", exc)

        logger.info(
            "[FOLDER_MATCH] Confirmed %s → '%s - %s' (%s): %d file(s) moved",
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