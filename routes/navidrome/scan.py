"""Navidrome scan routes.

These are Navidrome-specific entrypoints only. Generic scan orchestration,
status and stop-all routes remain under ``routes/scan_routes``.
"""

from __future__ import annotations

from quart import flash, jsonify, redirect, request, url_for

import services.scanning.runtime_state as runtime_state
from routes.navidrome import navidrome_bp, get_navidrome_client
from routes.scan_routes._common import form_bool, is_process_alive, run_async
from services.scanning.pipelines.navidrome_pipeline import run_navidrome_import_scan
from services.scanning.scan_state import progress_path, request_scan_stop


@navidrome_bp.route("/api/navidrome/scan/start", methods=["POST"])
def api_start_navidrome_scan():
    """Trigger a Navidrome server-side library rescan."""
    client = get_navidrome_client()
    if not client:
        return jsonify({"success": False, "error": "Navidrome not configured"})
    success = client.start_scan()
    return jsonify({"success": success, "message": "Scan started" if success else "Failed to start scan"})


@navidrome_bp.route("/api/navidrome/scan/status", methods=["GET"])
def api_get_navidrome_scan_status():
    """Return Navidrome server-side library scan status."""
    try:
        client = get_navidrome_client()
        if not client:
            return jsonify({"scanning": False, "success": False, "error": "Navidrome not configured"})
        return jsonify(client.get_scan_status())
    except Exception as exc:
        logger = __import__('logging').getLogger(__name__)
        logger.error("Navidrome scan status failed: %s", exc, exc_info=True)
        return jsonify({"scanning": False, "success": False, "error": str(exc)})


@navidrome_bp.route("/scan/navidrome", methods=["POST"])
async def scan_navidrome():
    """Start the local Navidrome import-only scan pipeline."""
    mode = request.args.get("mode", "all")
    restart_requested = form_bool(request.args.get("restart"))
    force_start = form_bool(request.args.get("force_start"))

    with runtime_state.scan_lock:
        if is_process_alive(runtime_state.scan_process_navidrome) and not force_start:
            return jsonify({"scan_running": True, "message": "A Navidrome scan is already running."}), 409

        thread = run_async(
            run_navidrome_import_scan,
            mode=mode,
            restart_requested=restart_requested,
            daemon=False,
        )
        runtime_state.scan_process_navidrome = {"thread": thread, "type": "navidrome"}

    await flash("✅ Navidrome import started", "success")
    return redirect(url_for("ui.dashboard"))


@navidrome_bp.route("/scan/stop-navidrome", methods=["POST"])
async def scan_stop_navidrome():
    """Request a graceful stop for the local Navidrome import scan."""
    with runtime_state.scan_lock:
        request_scan_stop(progress_path("navidrome_scan_progress.json"), "navidrome_scan")
        runtime_state.scan_process_navidrome = None
    await flash("Navidrome sync scan stop requested", "info")
    return redirect(url_for("ui.dashboard"))
