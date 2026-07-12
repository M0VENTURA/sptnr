"""Filesystem file-list cache.

Caches directory listings to avoid repeated filesystem walks during
batch pre-scans and folder-exists checks.

Provides:
- ``get_download_files`` – Cached listing of the downloads directory.
- ``invalidate_cache`` – Force cache refresh.
"""

import os
import time
from typing import List

from services.infrastructure.filesystem_service import resolve_downloads_dir

# ==============================================================================
# CACHE STATE
# ==============================================================================

_DOWNLOADS_CACHE: List[str] | None = None
_DOWNLOADS_CACHE_TS: float = 0.0
_CACHE_TTL_SECONDS = 30


# ==============================================================================
# PUBLIC API
# ==============================================================================

def get_download_files() -> List[str]:
    """
    Return a cached list of all audio files under the downloads directory.

    Avoids repeated expensive os.walk() calls.
    """

    global _DOWNLOADS_CACHE, _DOWNLOADS_CACHE_TS

    now = time.monotonic()

    # ✅ Serve from cache
    if (
        _DOWNLOADS_CACHE is not None
        and now - _DOWNLOADS_CACHE_TS < _CACHE_TTL_SECONDS
    ):
        return _DOWNLOADS_CACHE

    root = resolve_downloads_dir()
    files: List[str] = []

    # ✅ Rebuild cache
    if os.path.isdir(root):
        for r, _, filenames in os.walk(root):
            for f in filenames:
                if _is_audio_file(f):
                    files.append(os.path.join(r, f))

    _DOWNLOADS_CACHE = files
    _DOWNLOADS_CACHE_TS = now

    return files


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

