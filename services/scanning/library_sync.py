"""Incremental Navidrome library sync service.

Provides incremental synchronisation between Navidrome and the Popularr
database. Features single-flight worker state to prevent concurrent syncs
and WebUI-visible status tracking.

Key Functions:
    - request_library_sync(): Request a sync run (coalesces concurrent requests).
    - get_library_sync_state(): Return current sync status for UI display.
    - perform_library_sync(): Execute the actual sync operation.

Architecture:
    Uses threading locks for safe concurrent access. Sync state is tracked
    separately from the sync lock to avoid contention. WebUI polls the state
    via ``get_library_sync_state()``.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from api_clients.navidrome import NavidromeClient
from db.repositories.tracks import bulk_upsert_navidrome_tracks
from db.utils import get_db_connection, row_get
from helpers.logging_config import log_debug, log_error, log_unified
from helpers.config_helpers import get_scan_pipeline_config
from services.scanning.navidrome_import import scan_artist_to_db

# Load scanning pipeline config
_scan_cfg = get_scan_pipeline_config()

# -------------------------------------------------------------------------
# Single-flight worker state
# -------------------------------------------------------------------------

_library_sync_lock = threading.Lock()
_library_sync_state: dict[str, bool] = {"running": False, "pending": False}
_last_processed_scan_marker: int | None = None

# ✅ NEW: UI-visible state (separate lock = no contention)
_library_status_lock = threading.Lock()
_library_status: dict[str, Any] = {
    "running": False,
    "pending": False,
    "started_at": None,
    "finished_at": None,
    "last_run": None,
    "artists_processed": 0,
    "artists_failed": 0,
    "tracks_attempted": 0,
    "message": "Idle",
}


# -------------------------------------------------------------------------
# Public state for UI
# -------------------------------------------------------------------------

def get_library_sync_state() -> dict[str, Any]:
    """Return a full snapshot for dashboard usage."""
    with _library_status_lock:
        return dict(_library_status)


# -------------------------------------------------------------------------
# Request entrypoint
# -------------------------------------------------------------------------

def request_library_sync() -> dict[str, Any]:
    """Request a non-blocking library diff sync."""

    with _library_sync_lock:
        if _library_sync_state["running"]:
            if not _library_sync_state["pending"]:
                log_debug("[LIBRARY_SYNC] Sync already running; coalescing request")
                _library_sync_state["pending"] = True

                # ✅ reflect in UI
                with _library_status_lock:
                    _library_status["pending"] = True
            return {"coalesced": True, "running": True}

        _library_sync_state["running"] = True
        _library_sync_state["pending"] = False

    # ✅ mark running in UI
    with _library_status_lock:
        _library_status.update({
            "running": True,
            "pending": False,
            "started_at": time.time(),
            "finished_at": None,
            "message": "Starting library sync...",
            "artists_processed": 0,
            "artists_failed": 0,
            "tracks_attempted": 0,
        })

    thread = threading.Thread(
        target=_run_library_sync_worker,
        daemon=True,
        name="library-sync-worker"
    )
    thread.start()

    return {"started": True, "running": True}


# -------------------------------------------------------------------------
# Worker wrapper
# -------------------------------------------------------------------------

def _run_library_sync_worker() -> None:
    try:
        log_debug("[LIBRARY_SYNC] Library sync worker started")

        result = perform_library_sync()

        # ✅ update result in UI
        with _library_status_lock:
            _library_status.update({
                "message": "Completed",
                "last_run": time.time(),
                "artists_processed": result.get("artists_processed", 0),
                "artists_failed": result.get("artists_failed", 0),
                "tracks_attempted": result.get("tracks_attempted", 0),
            })

    except Exception as exc:
        logging.error("[LIBRARY_SYNC] Unexpected error: %s", exc, exc_info=True)

        with _library_status_lock:
            _library_status["message"] = "Failed"

    finally:
        with _library_sync_lock:
            _library_sync_state["running"] = False
            pending = bool(_library_sync_state["pending"])
            _library_sync_state["pending"] = False

        with _library_status_lock:
            _library_status["running"] = False
            _library_status["pending"] = False
            _library_status["finished_at"] = time.time()

        if pending:
            log_debug("[LIBRARY_SYNC] Scheduling follow-up sync")
            request_library_sync()


# -------------------------------------------------------------------------
# Core sync logic (unchanged except UI updates)
# -------------------------------------------------------------------------

def perform_library_sync() -> dict[str, Any]:
    global _last_processed_scan_marker

    started_at = time.time()

    cfg = get_navidrome_config()
    if not cfg:
        return {"success": False, "skipped": True}

    client = NavidromeClient(
        base_url=cfg["base_url"],
        username=cfg["user"],
        password=cfg["pass"],
    )

    scan_status = client.get_scan_status()

    if not scan_status.get("success") or scan_status.get("scanning"):
        return {"success": False, "skipped": True}

    marker = scan_status.get("count")
    if marker is not None and marker == _last_processed_scan_marker:
        return {"success": True, "skipped": True}

    candidate_artists = get_candidate_artists(client)
    if not candidate_artists:
        return {"success": True, "skipped": True}

    log_unified(f"[LIBRARY_SYNC] Starting sync for {len(candidate_artists)} artists")

    batch_size = _scan_cfg["library_sync_batch_size"]
    all_tracks_to_upsert = []
    seen_track_ids = set()

    artists_processed = 0
    artists_failed = 0
    tracks_attempted = 0

    total = len(candidate_artists)

    for i, (artist_name, artist_id) in enumerate(candidate_artists.items(), 1):
        try:
            # ✅ live status update
            with _library_status_lock:
                _library_status["message"] = f"Processing {artist_name} ({i}/{total})"

            result = sync_artist_with_diff(artist_name, artist_id)
            artists_processed += 1

            for track in result.get("tracks", []):
                track_id = track.get("id")
                if track_id and track_id in seen_track_ids:
                    continue
                if track_id:
                    seen_track_ids.add(track_id)
                all_tracks_to_upsert.append(track)

            if len(all_tracks_to_upsert) >= batch_size:
                tracks_attempted += run_bulk_commit(all_tracks_to_upsert)
                all_tracks_to_upsert.clear()

        except Exception as exc:
            artists_failed += 1
            log_error(f"[LIBRARY_SYNC] Artist failed: {artist_name}: {exc}")

    if all_tracks_to_upsert:
        tracks_attempted += run_bulk_commit(all_tracks_to_upsert)

    _last_processed_scan_marker = marker

    return {
        "success": True,
        "artists_processed": artists_processed,
        "artists_failed": artists_failed,
        "tracks_attempted": tracks_attempted,
        "duration": time.time() - started_at,
    }


# -------------------------------------------------------------------------
# Remaining functions unchanged
# -------------------------------------------------------------------------