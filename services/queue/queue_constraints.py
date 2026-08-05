"""
Queue constants and status definitions.

Single source of truth for:

- Queue status values
- Queue status groups
- Status display metadata
- Queue status validation

Do not duplicate queue status strings elsewhere.
Import from this module instead.
"""

from __future__ import annotations

# =============================================================================
# QUEUE STATUS GROUPS
# =============================================================================

ACTIVE_QUEUE_STATUSES: frozenset[str] = frozenset({
    "queued",
    "searching",
    "processing",
    "downloading",
    "unmatched",
    "queried",
    "copy_recommended",
    "moving",
})

PROCESSING_STATUSES: frozenset[str] = frozenset({
    "queued",
    "searching",
    "processing",
    "downloading",
})

MATCHABLE_QUEUE_STATUSES: frozenset[str] = frozenset({
    "queued",
    "searching",
    "downloading",
    "matched",
    "completed",
    "unmatched",
    "queried",
    "discovered",
    "pending_match",
    "possible_duplicate",
    "duplicate",
})

COMPLETED_QUEUE_STATUSES: frozenset[str] = frozenset({
    "completed",
    "unmatched",
    "possible_duplicate",
    "moving",
})

COLLECTION_STATUSES: frozenset[str] = frozenset({
    "completed",
    "imported",
    "in_collection",
})

FAILED_STATUSES: frozenset[str] = frozenset({
    "failed",
    "removed",
    "cancelled",
    "deleted",
})

TERMINAL_QUEUE_STATUSES: frozenset[str] = frozenset({
    "completed",
    "failed",
    "imported",
    "in_collection",
    "removed",
    "cancelled",
    "deleted",
})

# =============================================================================
# ALL VALID STATUSES
# =============================================================================

ALL_QUEUE_STATUSES: frozenset[str] = frozenset({
    "queued",
    "searching",
    "processing",
    "downloading",
    "matched",
    "completed",
    "failed",
    "unmatched",
    "moving",
    "queried",
    "copy_recommended",
    "possible_duplicate",
    "imported",
    "awaiting_selection",
    "in_collection",
    "removed",
    "cancelled",
    "deleted",
    "pending_match",
    "duplicate",
    "discovered",
})

# =============================================================================
# STATUS DISPLAY CONFIGURATION
# =============================================================================

STATUS_DISPLAY_CONFIG: dict[str, dict[str, str]] = {
    "queued": {
        "label": "Queued",
        "css": "bg-warning text-dark",
        "icon": "clock",
    },
    "searching": {
        "label": "Searching",
        "css": "bg-warning text-dark",
        "icon": "search",
    },
    "processing": {
        "label": "Processing",
        "css": "bg-info",
        "icon": "arrow-repeat",
    },
    "downloading": {
        "label": "Downloading",
        "css": "bg-primary",
        "icon": "download",
    },
    "completed": {
        "label": "Completed",
        "css": "bg-success",
        "icon": "check-circle",
    },
    "failed": {
        "label": "Failed",
        "css": "bg-danger",
        "icon": "x-circle",
    },
    "unmatched": {
        "label": "Unmatched",
        "css": "bg-warning text-dark",
        "icon": "exclamation-triangle",
    },
    "moving": {
        "label": "Moving",
        "css": "bg-info text-dark",
        "icon": "arrow-right-circle",
    },
    "queried": {
        "label": "Queried",
        "css": "bg-secondary",
        "icon": "question-circle",
    },
    "copy_recommended": {
        "label": "Copy Recommended",
        "css": "bg-info text-dark",
        "icon": "files",
    },
    "possible_duplicate": {
        "label": "Possible Duplicate",
        "css": "bg-secondary",
        "icon": "copy",
    },
    "imported": {
        "label": "Imported",
        "css": "bg-success",
        "icon": "check2-all",
    },
    "awaiting_selection": {
        "label": "Select File",
        "css": "bg-primary",
        "icon": "hand-index",
    },
    "in_collection": {
        "label": "In Collection",
        "css": "bg-success",
        "icon": "collection",
    },
    "matched": {
        "label": "Matched",
        "css": "bg-info text-dark",
        "icon": "check-circle",
    },
    "pending_match": {
        "label": "Pending Match",
        "css": "bg-secondary",
        "icon": "hourglass",
    },
    "duplicate": {
        "label": "Duplicate",
        "css": "bg-secondary",
        "icon": "copy",
    },
    "discovered": {
        "label": "Discovered",
        "css": "bg-info text-dark",
        "icon": "search",
    },
}

DEFAULT_STATUS_DISPLAY: dict[str, str] = {
    "label": "Unknown",
    "css": "bg-secondary",
    "icon": "question",
}

# =============================================================================
# HELPERS
# =============================================================================

def is_valid_queue_status(status: str | None) -> bool:
    """
    Return True if status is a recognised queue status.
    """

    return bool(status) and status in ALL_QUEUE_STATUSES


def get_status_display(status: str | None) -> dict[str, str]:
    """
    Return display configuration for a queue status.
    """

    if not status:
        return DEFAULT_STATUS_DISPLAY.copy()

    return STATUS_DISPLAY_CONFIG.get(
        status,
        {
            **DEFAULT_STATUS_DISPLAY,
            "label": status,
        },
    )


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    "ACTIVE_QUEUE_STATUSES",
    "PROCESSING_STATUSES",
    "MATCHABLE_QUEUE_STATUSES",
    "COMPLETED_QUEUE_STATUSES",
    "COLLECTION_STATUSES",
    "FAILED_STATUSES",
    "TERMINAL_QUEUE_STATUSES",
    "ALL_QUEUE_STATUSES",
    "STATUS_DISPLAY_CONFIG",
    "DEFAULT_STATUS_DISPLAY",
    "is_valid_queue_status",
    "get_status_display",
]