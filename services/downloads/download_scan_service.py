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

from helpers.config_helpers import get_config, get_supported_audio_formats
from services.infrastructure.filesystem_service import _prefer_music_subfolder

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = get_supported_audio_formats()

@dataclass(slots=True)
class DiscoveredFile:
    filename: str
    full_path: str
    rel_path: str
    extension: str
    folder: str

def resolve_downloads_dir() -> str:
    env_dir = os.environ.get("DOWNLOADS_DIR")
    if env_dir: return _prefer_music_subfolder(env_dir)
    try:
        cfg = get_config()
        configured = (cfg.get("downloads") or {}).get("monitor_folder")
        if configured: return _prefer_music_subfolder(configured)
    except Exception as exc:
        logger.warning("Could not resolve downloads folder: %s", exc)
    return "/downloads/Music"

def resolve_torrents_dir() -> str:
    root = os.environ.get("DOWNLOADS_DIR", "/downloads")
    torrents_dir = os.path.join(root, "torrents")
    return torrents_dir if os.path.isdir(torrents_dir) else resolve_downloads_dir()


def resolve_downloads_monitor_dir(_config: object | None = None) -> str:
    """Compatibility shim for older callers expecting a monitor-folder resolver."""
    return resolve_downloads_dir()


def discover_audio_files() -> list[DiscoveredFile]:
    """Filesystem-only scan for audio files."""
    downloads_dir = resolve_torrents_dir()
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
    logger.info("[SCAN] Discovered %s audio files", len(discovered))
    return discovered


def scan_downloads(_metadata_reader=None) -> dict[str, object]:
    """Compatibility wrapper for the downloads package API."""
    return {"success": True, "files": [file.full_path for file in discover_audio_files()]}


def get_scan_progress() -> dict[str, object]:
    return {"success": True, "progress": 0, "status": "idle"}


def verify_moved_files(_minutes_old: int = 30) -> dict[str, object]:
    return {"success": True, "verified": 0}


def check_completed_downloads() -> dict[str, object]:
    """Check for newly completed downloads. Thin wrapper for queue_orchestrator."""
    return scan_downloads()


def discover_files() -> dict[str, object]:
    return {"success": True, "files": [file.full_path for file in discover_audio_files()]}