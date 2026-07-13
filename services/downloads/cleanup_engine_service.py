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


# ==============================================================================
# PUBLIC BUSINESS LOGIC
# ==============================================================================

def cleanup_stale_downloads() -> dict[str, int]:
    """Clean up stale/orphaned download entries. Thin wrapper for queue_orchestrator."""
    from services.downloads.download_verification_service import verify_moved_files
    return verify_moved_files()


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
