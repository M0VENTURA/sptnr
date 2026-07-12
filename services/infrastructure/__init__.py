"""services.infrastructure package.

Foundational services that have no knowledge of business-domain concepts
(albums, artists, tracks, queue items, releases, MusicBrainz, etc.).
Owns generic technical concerns: filesystem helpers, path resolution,
configuration access, cache management, HTTP wrappers, and timeouts.

Owned functions:
- Path resolution:  resolve_downloads_dir(), resolve_music_dir()
- File operations:  transfer_download_to_music(), get_import_destination_path()
- Safety checks:    is_path_under_directory(), cleanup_empty_parents()
- Utilities:        apply_release_year_mtime(), SUPPORTED_AUDIO_FORMATS
- Listing:          _get_files_in_folder(), get_folder_group_details()

Cross-provider rate limiting: APIRateLimiter, get_rate_limiter
Bounded thread-pool execution: run_with_timeout, TimeoutError
File-system caching: get_download_files()
Centralised file-system manager: FileSystemManager
Singleton access: Infrastructure, get_infra
"""

from __future__ import annotations

from .api_rate_limiter import APIRateLimiter, get_rate_limiter
from .base import Infrastructure, get_infra
from .filesystem_cache_service import get_download_files
from .filesystem_service import cleanup_empty_parents, is_path_under_directory, resolve_downloads_dir
from .fs_manager import FileSystemManager
from .timeout_executor import api_timeout, cleanup_timeout_executor, ensure_timeout_executor, run_with_timeout, TimeoutError

__all__ = [
    "api_timeout",
    "APIRateLimiter",
    "cleanup_empty_parents",
    "cleanup_timeout_executor",
    "ensure_timeout_executor",
    "FileSystemManager",
    "get_download_files",
    "get_infra",
    "get_rate_limiter",
    "Infrastructure",
    "is_path_under_directory",
    "resolve_downloads_dir",
    "run_with_timeout",
    "TimeoutError",
]
