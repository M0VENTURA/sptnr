"""Filesystem file-list cache.

Caches directory listings to avoid repeated filesystem walks during
batch pre-scans and folder-exists checks.

Provides:
- ``get_download_files`` – Cached listing of the downloads directory.
- ``invalidate_cache`` – Force cache refresh.
"""

from __future__ import annotations

import os
import time
from typing import List

import structlog

from services.infrastructure.filesystem_service import resolve_downloads_dir

logger = structlog.get_logger(__name__)

# ==============================================================================
# CACHE STATE
# ==============================================================================

_DOWNLOADS_CACHE: list[str] | None = None
_DOWNLOADS_CACHE_TS: float = 0.0
_CACHE_TTL_SECONDS = 30


# ==============================================================================
# PUBLIC API
# ==============================================================================

def get_download_files() -> list[str]:
    """Return a cached list of all audio files under the downloads directory."""
    global _DOWNLOADS_CACHE, _DOWNLOADS_CACHE_TS

    now = time.monotonic()

    if (
        _DOWNLOADS_CACHE is not None
        and now - _DOWNLOADS_CACHE_TS < _CACHE_TTL_SECONDS
    ):
        return _DOWNLOADS_CACHE

    root = resolve_downloads_dir()
    files: list[str] = []

    try:
        from services.infrastructure.filesystem_service import resolve_original_archive_dir
        archive_dir = resolve_original_archive_dir()
    except Exception:
        archive_dir = ""

    if os.path.isdir(root):
        for r, dirnames, filenames in os.walk(root):
            if archive_dir:
                dirnames[:] = [
                    d for d in dirnames
                    if os.path.normpath(os.path.join(r, d)) != archive_dir
                ]
            for f in filenames:
                if _is_audio_file(f):
                    files.append(os.path.join(r, f))

    _DOWNLOADS_CACHE = files
    _DOWNLOADS_CACHE_TS = now

    logger.debug("Refreshed downloads filesystem cache", file_count=len(files))
    return files


def invalidate_cache() -> None:
    """Force refresh of the downloads filesystem cache on the next call."""
    global _DOWNLOADS_CACHE, _DOWNLOADS_CACHE_TS
    _DOWNLOADS_CACHE = None
    _DOWNLOADS_CACHE_TS = 0.0
    logger.debug("Invalidated downloads filesystem cache")


# ==============================================================================
# INTERNAL HELPERS
# ==============================================================================

def _is_audio_file(filename: str) -> bool:
    return filename.lower().endswith(
        (
            ".mp3",
            ".flac",
            ".m4a",
            ".ogg",
            ".wav",
            ".aac",
            ".opus",
            ".aiff",
        )
    )
