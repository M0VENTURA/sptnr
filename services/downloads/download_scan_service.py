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
from services.infrastructure.filesystem_service import resolve_downloads_dir

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

    discovered: list[DiscoveredFile] = []
    for root, _, files in os.walk(downloads_dir):
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


def discover_files() -> dict[str, object]:
    """Scan for audio files and return statistics.

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

    # Count how many are already in the download queue
    already_in_queue = 0
    already_in_library = 0
    if total > 0:
        try:
            from db.utils import get_db_connection

            conn = get_db_connection()
            cursor = conn.cursor()
            # Count rows in download_queue whose file_path matches discovered files
            for path in file_paths:
                cursor.execute(
                    "SELECT COUNT(*) FROM download_queue WHERE file_path = %s",
                    (path,),
                )
                row = cursor.fetchone()
                # Rows are RealDictRow (dict-like); never index by position.
                count = int(row.get("count") or 0) if row else 0
                if count > 0:
                    already_in_queue += 1
            conn.close()
        except Exception as exc:
            logger.debug("[DISCOVER] Queue check: %s", exc)

    return {
        "success": True,
        "stats": {
            "scanned": total,
            "queued": total - already_in_queue,
            "already_in_queue": already_in_queue,
            "already_in_library": already_in_library,
            "errors": [],
        },
        "files": file_paths,
    }