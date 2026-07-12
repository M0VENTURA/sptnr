"""Filesystem cache configuration for download services.

Configures TTL and state for cached file listings used by
pre-scan and folder-exists checks to avoid repeated directory walks.

All values now sourced from ``helpers.config_helpers`` to avoid duplication
with the constants already defined there.

Constants:
- ``_MUSIC_DIR_FILES_CACHE_TTL_SECONDS`` – Cache lifetime for music dir listings.
- ``_DOWNLOADS_DIR_FILES_CACHE_TTL_SECONDS`` – Cache lifetime for downloads dir.
- ``_PRE_SCAN_INTERVAL_SECONDS`` / ``_PRE_SCAN_BATCH_SIZE`` – Background tuning.
"""

from helpers.config_helpers import get_filesystem_cache_config, get_pre_scan_config


# Load cache config from centralized config_helpers
_cache_cfg = get_filesystem_cache_config()
_MUSIC_DIR_FILES_CACHE_TTL_SECONDS = _cache_cfg["music_dir_cache_ttl"]
_DOWNLOADS_DIR_FILES_CACHE_TTL_SECONDS = _cache_cfg["downloads_dir_cache_ttl"]
_music_dir_files_cache: list[str] | None = None
_music_dir_files_cache_ts: float = 0.0
_downloads_dir_files_cache: list[str] | None = None
_downloads_dir_files_cache_ts: float = 0.0

# Pre-scan task tuning from centralized config
_pre_scan_cfg = get_pre_scan_config()
_PRE_SCAN_INTERVAL_SECONDS = _pre_scan_cfg["interval_seconds"]
_PRE_SCAN_BATCH_SIZE = _pre_scan_cfg["batch_size"]
