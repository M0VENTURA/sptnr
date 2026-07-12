"""
JSON API routes for scan status, launches and dashboard polling.

These routes are API-facing controllers only.

Responsibilities:
- Parse JSON/query input
- Start lightweight scan requests
- Return runtime/progress JSON for dashboard
- Avoid business logic and direct progress-file manipulation

Progress files are read through services.scanning.scan_state.
Runtime state is read through services.scanning.runtime_state.
"""

from __future__ import annotations

from datetime import datetime

from quart import jsonify, request
from sqlalchemy import text
from db.engine import db_session
from routes.scan_routes import scans_bp
from routes.scan_routes._common import run_async
from services.scanning.pipelines.popularity_pipeline import run_popularity_mode
from services.scanning.runtime_state import (
    scan_lock,
    is_runtime_running,
    set_runtime,
    clear_runtime,
)
import services.scanning.runtime_state as runtime_state  # for legacy globals

from services.scanning.scan_state import (
    get_scan_progress_path,
    read_progress_file,
)

from services.scanning.pipelines.artist_pipeline import run_artist_pipeline


# -------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------

SCAN_STATUS_LABELS = {
    "library": "Main Library Scan",
    "navidrome": "Navidrome Sync",
    "popularity": "Popularity Update",
    "singles": "Single Detection",
    "essentia_mood": "Essentia Mood",
    "combined": "Combined Scan",
    "missing_releases": "Missing Releases Scan",
    "mp3_import": "MP3 Import",
    "artist": "Artist Scan",
}


PROGRESS_SCAN_TYPES = [
    "library_scan",
    "navidrome_scan",
    "metadata_lookup_scan",
    "popularity_scan",
    "singles_scan",
    "essentia_mood_scan",
    "combined_scan",
    "missing_releases_scan",
    "mp3_import",
]


def _runtime_status_payload() -> dict:
    """Return known runtime states using the runtime registry."""
    return {
        key: {
            "name": label,
            "running": is_runtime_running(key),
        }
        for key, label in SCAN_STATUS_LABELS.items()
    }


# -------------------------------------------------------------------------
# API: Start artist scan
# -------------------------------------------------------------------------

@scans_bp.route("/api/scan/artist", methods=["POST"])
def api_scan_single_artist():
    """Start an artist scan from JSON input."""
    data = request.get_json(silent=True) or {}

    artist = str(data.get("artist", "")).strip()
    force = bool(data.get("force", False))

    if not artist:
        return jsonify({
            "success": False,
            "error": "Artist name is required",
        }), 400

    with scan_lock:
        if is_runtime_running("artist"):
            return jsonify({
                "success": False,
                "error": "An artist scan is already running",
            }), 409

        def _worker():
            try:
                run_artist_pipeline(artist, force)
            finally:
                clear_runtime("artist")

        thread = run_async(_worker)
        set_runtime("artist", {"thread": thread, "type": "artist"})

    return jsonify({
        "success": True,
        "message": f"Scan started for artist: {artist}",
        "artist": artist,
        "mode": "Forced" if force else "Changes Only",
    })


# -------------------------------------------------------------------------
# API: Runtime status
# -------------------------------------------------------------------------

@scans_bp.route("/api/scan-status")
def api_scan_status():
    """
    Return status for known in-process scan references.

    Note:
    Runtime state is per Flask worker only.
    Progress files remain the cross-process source of truth.
    """
    with scan_lock:
        return jsonify(_runtime_status_payload())


# -------------------------------------------------------------------------
# API: Recent scan history
# -------------------------------------------------------------------------

@scans_bp.route("/api/recent-scans")
def api_recent_scans():
    """Return latest album scan events for dashboard refresh."""
    try:
        limit = min(request.args.get("limit", 100, type=int), 100)

        from services.scanning.scan_history_service import get_recent_album_scans

        scans = get_recent_album_scans(limit=limit)

        response = jsonify({
            "scans": scans,
            "fetched_at": datetime.utcnow().isoformat() + "Z",
        })

    except Exception as exc:
        response = jsonify({
            "scans": [],
            "error": str(exc),
            "fetched_at": datetime.utcnow().isoformat() + "Z",
        })

    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"

    return response


# -------------------------------------------------------------------------
# API: Progress polling
# -------------------------------------------------------------------------

@scans_bp.route("/api/scan-progress")
def api_scan_progress():
    """
    Unified scan progress endpoint.

    - Uses progress_service as the single source of truth
    - Merges scan_state (persistent) + progress_tracker (live stage data)
    - Preserves legacy payload structure for dashboard compatibility
    """
    try:
        from services.scanning.pipelines.progress_service import get_scan_progress

        result = get_scan_progress()

        active_scans = result.get("active_scans", [])

        # -----------------------------------------------------------------
        # Primary scan selection (matches legacy behaviour: first active)
        # -----------------------------------------------------------------
        primary = active_scans[0] if active_scans else {
            "scan_type": "none",
            "is_running": False,
            "percent_complete": 0,
            "status": "idle",
            "current_artist": None,
            "current_album": None,
            "current_stage": None,
            "message": None,
        }

        # -----------------------------------------------------------------
        # Final payload (backward compatible)
        # -----------------------------------------------------------------
        payload = {
            **primary,
            "is_running": result.get("is_running", False),
            "active_scans": active_scans,
            "active_scan_count": result.get("active_scan_count", 0),
        }

        return jsonify(payload)

    except Exception as exc:
        # Safe fallback — never break dashboard polling
        return jsonify({
            "scan_type": "error",
            "is_running": False,
            "percent_complete": 0,
            "status": "error",
            "active_scans": [],
            "active_scan_count": 0,
            "error": str(exc),
        }), 500

@scans_bp.route("/api/popularity/run", methods=["POST"])
def api_popularity_run_compat():
    data = request.get_json(silent=True) or {}

    # Support both explicit mode field and legacy boolean flags
    mode = str(data.get("mode", "popularity")).strip().lower()

    if mode not in ("popularity", "metadata", "singles", "singles_detection", "all"):
        mode = "popularity"

    # Legacy boolean flag overrides
    if data.get("metadata_only"):
        mode = "metadata"
    elif data.get("singles_only"):
        mode = "singles"

    with scan_lock:
        if is_runtime_running("popularity"):
            return jsonify({
                "success": False,
                "error": "A popularity scan is already running",
            }), 409

        def _worker():
            try:
                run_popularity_mode(mode=mode)
            finally:
                clear_runtime("popularity")
                runtime_state.scan_process_popularity = None

        thread = run_async(_worker)
        set_runtime("popularity", {"thread": thread, "type": mode})
        runtime_state.scan_process_popularity = {"thread": thread, "type": mode}

    return jsonify({
        "success": True,
        "message": f"{mode.capitalize()} scan started",
    })


# -------------------------------------------------------------------------
# API: Essentia mood scan
# -------------------------------------------------------------------------

@scans_bp.route("/api/essentia/run", methods=["POST"])
def api_essentia_run():
    """Start an Essentia mood/genre scan via JSON API."""
    from services.scanning.pipelines.essentia_pipeline import run_essentia_pipeline

    with scan_lock:
        if is_runtime_running("essentia_mood"):
            return jsonify({
                "success": False,
                "error": "An Essentia scan is already running",
            }), 409

        progress_file = get_scan_progress_path("essentia_mood_scan")

        def _worker():
            try:
                run_essentia_pipeline(progress_file=progress_file)
            finally:
                clear_runtime("essentia_mood")

        thread = run_async(_worker)
        set_runtime("essentia_mood", {"thread": thread, "type": "essentia_mood"})

    return jsonify({
        "success": True,
        "message": "Essentia mood/genre scan started",
    })


# -------------------------------------------------------------------------
# API: Navidrome import pipeline
# -------------------------------------------------------------------------

@scans_bp.route("/api/navidrome/import", methods=["POST"])
def api_navidrome_import():
    """Start the local Navidrome import pipeline via JSON API."""
    from services.scanning.pipelines.navidrome_pipeline import run_navidrome_import_scan

    data = request.get_json(silent=True) or {}
    mode = str(data.get("mode", "all")).strip().lower() or "all"

    with scan_lock:
        if is_runtime_running("navidrome"):
            return jsonify({
                "success": False,
                "error": "A Navidrome import scan is already running",
            }), 409

        progress_file = get_scan_progress_path("navidrome_scan")

        def _worker():
            try:
                run_navidrome_import_scan(mode=mode, progress_file=progress_file)
            finally:
                clear_runtime("navidrome")

        thread = run_async(_worker)
        set_runtime("navidrome", {"thread": thread, "type": "navidrome"})

    return jsonify({
        "success": True,
        "message": f"Navidrome import scan started (mode={mode})",
    })


# -------------------------------------------------------------------------
# API: Legacy popularity status endpoint (compat for dashboard.html)
# -------------------------------------------------------------------------

@scans_bp.route("/api/popularity/status", methods=["GET"])
def api_popularity_status_compat():
    """
    Backward-compatible popularity status endpoint.

    The legacy dashboard.html polls /api/popularity/status every 2 seconds
    and expects { success, running, message, progress, processed_items, total_items }.
    This bridges to the modern scan-progress service.
    """
    try:
        from services.scanning.pipelines.progress_service import get_scan_progress

        result = get_scan_progress()

        # Find the active popularity scan, if any
        pop_scan = None
        for active in result.get("active_scans", []):
            if active.get("scan_type") in ("popularity_scan", "singles_scan"):
                pop_scan = active
                break

        if pop_scan:
            return jsonify({
                "success": True,
                "running": pop_scan.get("is_running", False),
                "message": pop_scan.get("message") or pop_scan.get("status") or "Running...",
                "progress": pop_scan.get("percent_complete", 0),
                "processed_items": pop_scan.get("processed_items") or pop_scan.get("processed_artists") or 0,
                "total_items": pop_scan.get("total_items") or pop_scan.get("total_artists") or 0,
            })

        return jsonify({
            "success": True,
            "running": False,
            "message": "Idle",
            "progress": 0,
            "processed_items": 0,
            "total_items": 0,
        })

    except Exception as exc:
        return jsonify({
            "success": False,
            "running": False,
            "message": str(exc),
            "progress": 0,
            "processed_items": 0,
            "total_items": 0,
        }), 500