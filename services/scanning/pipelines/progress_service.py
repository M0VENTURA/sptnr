"""
Unified scan progress aggregation service.

Responsible for:
- Reading all scan progress files (scan_state)
- Merging active scans into a single API response
- Enhancing with in-memory stage progress (progress_tracker)
- Providing a stable contract for WebUI polling

This replaces legacy:
- unified_scan.get_scan_progress()
- _validate_and_cleanup_progress_file()
- scan_process_* checks in routes
"""

from __future__ import annotations

import os
from typing import Any

from services.scanning.scan_state import (
    get_database_dir,
    read_progress_file,
)

from services.popularity.progress_tracker import get_state as get_tracker_state

# -------------------------------------------------------------------------
# Known scan types (extendable)
# -------------------------------------------------------------------------

SCAN_TYPES = [
    "library_scan",
    "navidrome_scan",
    "popularity_scan",
    "singles_scan",
    "essentia_mood_scan",
    "combined_scan",
    "missing_releases_scan",
    "mp3_import",
]


# -------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------

def _build_progress_path(scan_type: str) -> str:
    db_dir = get_database_dir()
    return os.path.join(db_dir, f"{scan_type}_progress.json")


def _normalise_entry(scan_type: str, state: dict[str, Any]) -> dict[str, Any]:
    """
    Convert raw progress JSON into a consistent API entry.
    """

    return {
        "scan_type": state.get("scan_type") or scan_type,
        "is_running": bool(state.get("is_running", False)),
        "percent_complete": int(state.get("percent_complete", 0) or 0),
        "current_stage": state.get("current_stage"),
        "current_artist": state.get("current_artist"),
        "current_album": state.get("current_album"),
        "current_item": state.get("current_item"),

        # Progress counters
        "processed_artists": state.get("processed_artists"),
        "total_artists": state.get("total_artists"),
        "processed_items": state.get("processed_items"),
        "total_items": state.get("total_items"),

        # Optional extras
        "status": state.get("status"),
        "message": state.get("message"),
        "last_updated": state.get("last_updated"),
    }


def _merge_tracker_into_entry(entry: dict[str, Any]) -> None:
    """
    Enhance active scan with in-memory stage-level detail.

    Only applies to pipelines that use progress_tracker (popularity, etc.).
    """

    tracker = get_tracker_state()

    if not tracker.get("running"):
        return

    # Only merge into primary scan types (avoid polluting navidrome/library)
    if entry["scan_type"] not in {"popularity_scan", "library_scan", "combined_scan"}:
        return

    entry.update({
        "current_stage": tracker.get("current_stage"),
        "percent_complete": tracker.get("progress") or entry.get("percent_complete"),
        "message": tracker.get("message") or entry.get("message"),
        "current_item": tracker.get("current_item") or entry.get("current_item"),
        "processed_items": tracker.get("processed_items"),
        "total_items": tracker.get("total_items"),
    })


# -------------------------------------------------------------------------
# Main API entrypoint
# -------------------------------------------------------------------------

def get_scan_progress() -> dict[str, Any]:
    """
    Return unified progress for all scans.

    Output structure:
    {
        "is_running": bool,
        "active_scan_count": int,
        "active_scans": [...],
    }
    """

    active_scans: list[dict[str, Any]] = []

    for scan_type in SCAN_TYPES:
        path = _build_progress_path(scan_type)

        state = read_progress_file(path)
        if not state:
            continue

        if not state.get("is_running", False):
            continue

        entry = _normalise_entry(scan_type, state)

        # Enhance with live tracker data where appropriate
        _merge_tracker_into_entry(entry)

        active_scans.append(entry)

    # ---------------------------------------------------------------------
    # Deduplicate by scan_type (safety for race conditions)
    # ---------------------------------------------------------------------

    deduped: list[dict[str, Any]] = []
    seen = set()

    for entry in active_scans:
        scan_type = entry.get("scan_type")
        if scan_type in seen:
            continue
        seen.add(scan_type)
        deduped.append(entry)

    active_scans = deduped

    return {
        "is_running": bool(active_scans),
        "active_scan_count": len(active_scans),
        "active_scans": active_scans,
    }