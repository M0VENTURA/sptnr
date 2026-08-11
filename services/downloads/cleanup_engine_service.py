"""Download cleanup engine.

Handles post-download file cleanup:
- Removing empty parent directories.
- Cleaning up orphaned download artifacts.
- Ensuring downloaded files are under monitored paths.

Delegates filesystem operations to ``services.infrastructure.filesystem_service``.
"""

import os
import time
import logging

from typing import TypedDict, Optional

from services.infrastructure.filesystem_service import (
    resolve_downloads_dir,
    is_path_under_directory,
    cleanup_empty_parents,
)

from services.infrastructure.filesystem_cache_service import (
    get_download_files,
)

from helpers.normalization_service import normalize_match_text

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------------------
# TYPES
# ------------------------------------------------------------------------------

class QueueItem(TypedDict, total=False):
    id: str
    artist: str
    title: str


# ------------------------------------------------------------------------------
# CONSTANTS
# ------------------------------------------------------------------------------

_CLEANUP_SIBLING_MIN_AGE_SECONDS: int = 30

# Re-scanning /downloads on EVERY worker cycle re-adds files the user has
# explicitly removed from the queue (the files still exist on disk) and
# spams "Duplicate skipped" every cycle.  Discovery is rate-limited to once
# per interval (legacy auto-discovery behaviour); the manual "Discover
# Files" action and the downloads route still trigger it instantly.
# Overridable via DISCOVERY_INTERVAL_SECONDS.
_CLEANUP_DISCOVERY_MIN_INTERVAL_SECONDS: int = int(
    os.getenv("DISCOVERY_INTERVAL_SECONDS", "300")
)

_discovery_last_run_at: float = 0.0


# ==============================================================================
# PUBLIC BUSINESS LOGIC
# ==============================================================================

def cleanup_stale_downloads() -> dict[str, int]:
    """Clean up stale/orphaned download entries."""
    global _discovery_last_run_at

    now = time.time()
    if now - _discovery_last_run_at >= _CLEANUP_DISCOVERY_MIN_INTERVAL_SECONDS:
        _discovery_last_run_at = now
        from services.downloads.download_scan_service import discover_files
        result = discover_files()
    else:
        # Inside the rate-limit window: skip the folder re-scan so files the
        # user removed from the queue are not re-added every cycle.
        result = []
    # Legacy parity: download folders whose files were all imported into the
    # library (queue rows matched/moved) are stale — delete them so the
    # monitor page's folder lists stay clean.
    auto_deleted = 0
    try:
        from services.downloads.download_folder_service import auto_delete_imported_folders
        auto_deleted = auto_delete_imported_folders()
    except Exception as exc:
        logger.debug("[CLEANUP] Auto-delete imported folders failed: %s", exc)
    return {
        "scanned": len(result) if isinstance(result, (list, dict)) else 1,
        "folders_deleted": auto_deleted,
    }


def cleanup_sibling_downloads(
    queue_item: QueueItem,
    keep_path: str | None = None,
) -> None:
    """
    Remove duplicate downloads for a queue item.
    """

    artist: str = queue_item.get("artist") or ""
    title: str = queue_item.get("title") or ""
    item_id: str = queue_item.get("id") or "unknown"

    artist_norm: str = normalize_match_text(artist)
    title_norm: str = normalize_match_text(title)

    if not artist_norm or not title_norm:
        return

    downloads_root: str = resolve_downloads_dir()

    if not os.path.isdir(downloads_root):
        return

    keep_abs: str | None = os.path.abspath(keep_path) if keep_path else None

    artist_lower: str = artist.lower()
    title_lower: str = title.lower()

    for fpath in get_download_files():

        # ---------------------------------------------------
        # Skip file we are keeping
        # ---------------------------------------------------
        if keep_abs and os.path.abspath(fpath) == keep_abs:
            continue

        # ---------------------------------------------------
        # Skip too-recent files
        # ---------------------------------------------------
        try:
            age_seconds: float = time.time() - os.path.getmtime(fpath)
            if age_seconds < _CLEANUP_SIBLING_MIN_AGE_SECONDS:
                continue
        except OSError:
            continue

        basename_lower: str = os.path.basename(fpath).lower()

        if artist_lower not in basename_lower and title_lower not in basename_lower:
            continue

        rel_path: str = os.path.relpath(fpath, downloads_root).lower()

        if artist_norm not in rel_path or title_norm not in rel_path:
            continue

        _safe_delete(
            file_path=fpath,
            item_id=item_id,
            reason="duplicate",
            root=downloads_root,
        )


def delete_mismatched_download(
    file_path: str,
    item_id: str,
    reason: str,
) -> bool:
    """
    Delete a mismatched downloaded file safely.
    """

    downloads_root: str = resolve_downloads_dir()

    return _safe_delete(
        file_path=file_path,
        item_id=item_id,
        reason=reason,
        root=downloads_root,
    )


# ==============================================================================
# INTERNAL HELPERS
# ==============================================================================

def _safe_delete(
    file_path: str,
    item_id: str,
    reason: str,
    root: str,
) -> bool:
    """
    Central deletion logic.
    """

    if not file_path:
        return False

    if not is_path_under_directory(file_path, root):
        logger.warning(
            "Queue %s: REFUSING delete outside downloads dir: %s",
            item_id,
            file_path,
        )
        return False

    try:
        if not os.path.isfile(file_path):
            logger.debug(
                "Queue %s: file already gone: %s",
                item_id,
                file_path,
            )
            return False

        os.remove(file_path)

        logger.info(
            "Queue %s: [DELETE] (%s) -> %s",
            item_id,
            reason,
            file_path,
        )

        cleanup_empty_parents(file_path, root)

        return True

    except OSError as exc:
        logger.warning(
            "Queue %s: failed deleting %s: %s",
            item_id,
            file_path,
            exc,
        )
        return False
