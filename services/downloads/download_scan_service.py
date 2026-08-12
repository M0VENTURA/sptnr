"""Download filesystem scanning services.

Provides filesystem-level audio file discovery and path resolution
for the download pipeline. Responsible for locating downloaded audio
files on disk and making them available for queue ingestion.

Key Responsibilities:
    - Path resolution: Resolves download directories from env/config.
    - Filesystem discovery: Walks download directories for audio files.
    - File metadata: Provides DiscoveredFile dataclass with path info.

Architecture:
    Pure filesystem operations - no database access. Results are passed
    to queue services for ingestion and further processing.

    Callers:
        - services/downloads/download_queue_service.py (auto-discovery)
        - services/downloads/__init__.py (package-level re-exports)
        - routes/downloads.py (UI status endpoints)
"""

from __future__ import annotations
import logging
import os
from dataclasses import dataclass
from typing import List, Dict

from helpers.config_helpers import get_supported_audio_formats
from services.infrastructure.filesystem_service import (
    resolve_downloads_dir,
    resolve_original_archive_dir,
)

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = get_supported_audio_formats()

@dataclass(slots=True)
class DiscoveredFile:
    filename: str
    full_path: str
    rel_path: str
    extension: str
    folder: str

# resolve_downloads_dir is re-exported from services.infrastructure.filesystem_service
# (single source of truth: DOWNLOADS_DIR env → downloads.monitor_folder →
# downloads.folder (config.html) → /downloads/Music). Kept here so existing
# callers (watcher, completion service, queue repos) import it from one place.

def resolve_torrents_dir() -> str:
    root = os.environ.get("DOWNLOADS_DIR", "/downloads")
    torrents_dir = os.path.join(root, "torrents")
    return torrents_dir if os.path.isdir(torrents_dir) else resolve_downloads_dir()


def resolve_downloads_monitor_dir(_config: object | None = None) -> str:
    """Compatibility shim for older callers expecting a monitor-folder resolver."""
    return resolve_downloads_dir()


_last_discovered_count: int | None = None


def discover_audio_files() -> list[DiscoveredFile]:
    """Filesystem-only scan for audio files across the configured downloads root.

    Scans the configured root directly — NOT the ``Music``/``torrents``
    subfolder preferences — so albums landing anywhere under the downloads
    folder are discovered.  The recursive walk covers subfolders including
    ``Music`` anyway.
    """
    downloads_dir = resolve_downloads_dir(prefer_music_subfolder=False)
    if not os.path.isdir(downloads_dir):
        logger.warning("Downloads directory not found: %s", downloads_dir)
        return []

    # The FLAC conversion archive (downloads/<original_subfolder>) must never
    # be re-discovered: its files were already imported (converted), and
    # re-queueing them would download the album AGAIN as duplicates.
    archive_dir = resolve_original_archive_dir()

    discovered: list[DiscoveredFile] = []
    for root, dirs, files in os.walk(downloads_dir):
        dirs[:] = [
            d for d in dirs
            if os.path.normpath(os.path.join(root, d)) != archive_dir
        ]
        for filename in sorted(files):
            _, ext = os.path.splitext(filename)
            ext = ext.lower()
            if ext in SUPPORTED_EXTENSIONS:
                full_path = os.path.join(root, filename)
                discovered.append(DiscoveredFile(
                    filename=filename,
                    full_path=full_path,
                    rel_path=os.path.relpath(full_path, downloads_dir),
                    extension=ext,
                    folder=root
                ))
    # Only log when the count CHANGES — the queue worker's maintenance cycle
    # calls this every ~30s and the repeated identical lines flooded the
    # unified log / dashboard scanning panel ("[SCAN] Discovered N audio
    # files" every 30 seconds).
    global _last_discovered_count
    if _last_discovered_count != len(discovered):
        logger.info("[SCAN] Discovered %s audio files", len(discovered))
        _last_discovered_count = len(discovered)
    return discovered


def scan_downloads(_metadata_reader=None) -> dict[str, object]:
    """Compatibility wrapper for the downloads package API."""
    return {"success": True, "files": [file.full_path for file in discover_audio_files()]}


def get_scan_progress() -> dict[str, object]:
    return {"success": True, "progress": 0, "status": "idle"}


def verify_moved_files(_minutes_old: int = 30) -> dict[str, object]:
    return {"success": True, "verified": 0}


def check_completed_downloads() -> dict[str, object]:
    """Check for newly completed downloads and match them to queue items.

    Delegates to ``services.downloads.download_completion_service`` which
    reconciles items stuck in ``downloading`` against slskd completed
    transfers and filesystem matches, then moves matched files into the music
    library and promotes the queue rows to ``imported``.
    """
    from services.downloads.download_completion_service import check_completed_downloads as _check
    return _check()


def _extract_discovered_metadata(file_path: str, filename: str) -> dict[str, str | None]:
    """Best-effort metadata for a discovered file.

    Reads embedded ID3/FLAC tags first (mutagen via metadata_reader), then
    falls back to parsing the filename/folder ("Artist - Album - 01 - Title"
    or "01 - Title", folder-as-artist).  Never returns empty names — the
    old hardcoded "Unknown" bucket is replaced by "Unidentified …"
    placeholders.
    """
    import os
    import re

    meta: dict = {}
    try:
        from helpers.metadata_reader import read_mp3_metadata
        meta = read_mp3_metadata(file_path) or {}
    except Exception:
        pass

    artist = str(meta.get("artist") or "").strip()
    album = str(meta.get("album") or "").strip()
    title = str(meta.get("title") or "").strip()
    year_raw = str(meta.get("year") or meta.get("date") or "").strip()
    track_raw = str(meta.get("track_number") or "").strip()

    stem = os.path.splitext(filename or os.path.basename(file_path or ""))[0]
    # Strip slskd-style trailing hashes ("Holler_639220186280397812" → "Holler").
    stem = re.sub(r"[\s_\-]?\d{10,}\s*$", "", stem).strip()
    folder = os.path.basename(os.path.dirname(file_path or "") or "")

    # Filename fallback: split "Artist - Album - 01 - Title" style names.
    parts = [p.strip() for p in re.split(r"\s*[-–]\s*", stem) if p.strip()]
    if (not title or not artist) and parts:
        if not artist and len(parts) >= 2:
            artist = parts[0]
        # Album from "Artist - Album - 01 - Title" (4+ parts) or the
        # "Artist - Album - 12" pattern whose last part is a track number.
        if not album:
            if len(parts) >= 4:
                album = parts[1]
            elif len(parts) == 3 and re.match(r"^\d{1,3}$", parts[2]):
                album = parts[1]
        if not title:
            _rest = parts[1:] if len(parts) >= 2 else parts
            # Skip a leading track-number part ("01").
            if _rest and re.match(r"^\d{1,3}$", _rest[0]):
                _rest = _rest[1:]
            title = " - ".join(_rest) if _rest else parts[-1]
            # Drop a trailing track number ("Holler - 12" → "Holler").
            title = re.sub(r"\s*[-–]\s*\d{1,3}\s*$", "", title).strip()
        if not track_raw and parts and re.match(r"^\d{1,3}$", parts[0]):
            track_raw = parts[0]

    # Folder-as-artist fallback (skip generic root folders).
    if not artist:
        folder_lower = folder.lower()
        if folder and folder_lower not in ("downloads", "music", "completed", "inbox"):
            artist = folder

    return {
        "artist": artist or "Unidentified Artist",
        "album": album or "Unidentified Release",
        "title": title or stem or "Unidentified Track",
        "track_number": track_raw or None,
        "year": year_raw[:4] if year_raw else None,
    }


def _duplicate_cleanup_config(key: str, default: bool = True) -> bool:
    """Gate for the Downloads card's duplicate-cleanup toggles.

    Reads ``features.downloads_duplicate_cleanup.{delete_duplicate_files,
    prune_empty_folders}`` — the settings page previously saved these with
    no consumer, so both toggles now control real behaviour.
    """
    try:
        from helpers.config_helpers import get_config
        cfg = (get_config() or {}).get("features", {}).get("downloads_duplicate_cleanup") or {}
        return bool(cfg.get(key, default))
    except Exception:
        return default


def _quality_filter_config() -> dict:
    """Read ``downloads.quality_filter`` (enabled/priorities/tolerance/reject)."""
    try:
        from helpers.config_helpers import get_config
        qf = (get_config() or {}).get("downloads", {}).get("quality_filter") or {}
    except Exception:
        qf = {}
    priorities = []
    for p in qf.get("priorities") or []:
        if isinstance(p, dict) and str(p.get("format") or "").strip():
            priorities.append(p)
    return {
        "enabled": bool(qf.get("enabled")),
        "reject_others": bool(qf.get("reject_others", True)),
        "bitrate_tolerance": int(qf.get("bitrate_tolerance") or 5),
        "priorities": priorities,
    }


def _matches_quality_filter(file_path: str) -> tuple[bool, str | None]:
    """True when the file matches a configured format/bitrate priority.

    A priority with no bitrate accepts any bitrate of that format (lossless
    formats like FLAC); a numeric bitrate accepts values within the
    configured tolerance (e.g. 315-325 for MP3 320).
    """
    cfg = _quality_filter_config()
    if not cfg["enabled"] or not cfg["priorities"]:
        return True, None

    ext = os.path.splitext(file_path)[1].lower().lstrip(".")
    bitrate: int | None = None
    try:
        from helpers.metadata_reader import read_mp3_metadata
        meta = read_mp3_metadata(file_path) or {}
        if meta.get("bitrate"):
            bitrate = int(round(float(meta["bitrate"]) / 1000))
    except Exception:
        bitrate = None

    tolerance = cfg["bitrate_tolerance"]
    for priority in cfg["priorities"]:
        if ext != str(priority.get("format") or "").lower():
            continue
        want = priority.get("bitrate_kbps")
        if want in (None, ""):
            return True, None  # lossless / no bitrate requirement
        try:
            want = int(want)
        except (TypeError, ValueError):
            return True, None
        if bitrate is None or abs(bitrate - want) <= tolerance:
            return True, None
    return False, f"{ext or 'unknown'} @ {bitrate or '?'}kbps doesn't match quality priorities"


def _queue_has_active_match(meta: dict) -> bool:
    """Stage A: an active queue item already covers this artist+title.

    Exact case-insensitive match on (artist, title) — a Soulseek download of
    the same track with different casing should not spawn a duplicate
    'unmatched' row.
    """
    artist = str(meta.get("artist") or "").strip()
    title = str(meta.get("title") or "").strip()
    if not artist or not title or artist.lower().startswith("unidentified"):
        return False
    try:
        from sqlalchemy import text
        from db.engine import db_session

        with db_session() as session:
            row = session.execute(
                text("""
                    SELECT id FROM download_queue
                    WHERE LOWER(artist) = LOWER(:artist)
                      AND LOWER(title) = LOWER(:title)
                      AND status IN ('queued', 'searching', 'downloading')
                    LIMIT 1
                """),
                {"artist": artist, "title": title},
            ).fetchone()
            return row is not None
    except Exception:
        return False


def enqueue_discovered_files(files: list[DiscoveredFile]) -> dict[str, int]:
    """Dedupe discovered files against the queue and insert the new ones.

    Shared by the manual ``Discover Files`` action and the periodic
    auto-discovery cycle so both paths enqueue identically.

    New rows carry real metadata extracted from the file's tags (with a
    filename/folder fallback) instead of the old "Unknown" bucket, and are
    skipped when an active queue item already matches artist+title.

    Returns ``{"queued": int, "already_in_queue": int}``.
    """
    from db.repositories.queue_admin import (
        find_existing_discovered_file,
        insert_discovered_file,
    )
    queued = 0
    already_in_queue = 0
    quality_skipped = 0
    quality_filter = _quality_filter_config()
    for f in files:
        existing = find_existing_discovered_file(
            file_path=f.full_path,
            filename=f.filename,
            rel_path=f.rel_path,
        )
        if existing:
            already_in_queue += 1
            continue
        matches_qf, qf_reason = _matches_quality_filter(f.full_path)
        if not matches_qf:
            if quality_filter["reject_others"]:
                quality_skipped += 1
                logger.info(
                    "[DISCOVER] Skipped %s: %s", f.full_path, qf_reason or "quality filter"
                )
                continue
            logger.debug(
                "[DISCOVER] Non-priority file kept (reject_others off): %s — %s",
                f.full_path, qf_reason,
            )
        meta = _extract_discovered_metadata(f.full_path, f.filename)
        if _queue_has_active_match(meta):
            already_in_queue += 1
            # Auto-prune: when the same artist+title already has a file in
            # the queue, compare audio quality and remove the inferior copy.
            # Gated by the config toggle (was previously always-on).
            if _duplicate_cleanup_config("delete_duplicate_files", True):
                try:
                    from services.downloads.quality_dedup_service import prune_inferior_duplicate
                    prune_inferior_duplicate(meta, f.full_path)
                except Exception as _exc:
                    logger.debug("[DISCOVER] duplicate prune skipped: %s", _exc)
            continue
        insert_discovered_file(
            artist=str(meta["artist"] or "Unidentified Artist"),
            title=str(meta["title"] or f.filename),
            album=str(meta["album"] or "Unidentified Release"),
            album_artist=None,
            track_number=meta.get("track_number"),
            disc_number=None,
            year=meta.get("year"),
            duration=None,
            file_path=f.full_path,
            filename=f.filename,
            import_group="default",
        )
        queued += 1
    return {"queued": queued, "already_in_queue": already_in_queue}


def discover_files() -> dict[str, object]:
    """Scan for audio files, add new ones to the download queue, return stats.

    Returns:
        {
            "success": True,
            "stats": {
                "scanned": int,
                "queued": int,
                "already_in_queue": int,
                "already_in_library": int,
                "errors": list[str],
            },
            "files": list[str],
        }
    """
    files = discover_audio_files()
    file_paths = [f.full_path for f in files]
    total = len(file_paths)

    # Enqueue new files (dedupe by path/filename, insert as "unmatched").
    enqueued = enqueue_discovered_files(files)
    queued = enqueued["queued"]
    already_in_queue = enqueued["already_in_queue"]
    quality_skipped = enqueued.get("quality_skipped", 0)

    # Prune empty downloads folders after a discovery pass when enabled
    # (config toggle previously saved with no consumer).
    if _duplicate_cleanup_config("prune_empty_folders", True):
        try:
            import os as _os
            from services.infrastructure.filesystem_service import resolve_downloads_dir
            from services.infrastructure.fs_manager import FileSystemManager
            downloads_root = resolve_downloads_dir(prefer_music_subfolder=False)
            music_root = _os.environ.get("MUSIC_FOLDER", "/music")
            if downloads_root:
                FileSystemManager(downloads_root, music_root).cleanup_empty_dirs(downloads_root)
        except Exception as _exc:
            logger.debug("[DISCOVER] Empty-folder prune skipped: %s", _exc)

    # Count how many already exist in the music library (tracks table).
    already_in_library = 0
    if files:
        try:
            from sqlalchemy import text as _text
            from db.engine import db_session as _db_session
            with _db_session() as session:
                for f in files:
                    row = session.execute(
                        _text("SELECT COUNT(*) FROM tracks WHERE file_path = :path"),
                        {"path": f.full_path},
                    ).fetchone()
                    count = int(row[0]) if row else 0
                    if count > 0:
                        already_in_library += 1
        except Exception as exc:
            logger.debug("[DISCOVER] Library check: %s", exc)

    return {
        "success": True,
        "stats": {
            "scanned": total,
            "queued": queued,
            "already_in_queue": already_in_queue,
            "already_in_library": already_in_library,
            "quality_skipped": quality_skipped,
            "errors": [],
        },
        "files": file_paths,
    }