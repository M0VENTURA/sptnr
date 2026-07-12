"""
General scan start/stop/status routes.
"""

from __future__ import annotations

from flask import flash, jsonify, redirect, request, url_for

from routes.scan_routes import scans_bp
from routes.scan_routes._common import form_bool, run_async

from services.scanning.runtime_state import (
    scan_lock,
    is_runtime_running,
    clear_runtime,
)

from services.scanning.scan_state import (
    get_scan_progress_path,
    read_progress_file,
    request_scan_stop,
)

from services.scanning.pipeline import start_library_scan
from services.scanning.pipelines.artist_pipeline import run_artist_pipeline
from services.scanning.pipelines.popularity_pipeline import run_popularity_mode
from services.scanning.pipelines.navidrome_pipeline import run_navidrome_import_scan


# -------------------------------------------------------------------------
# Start scan
# -------------------------------------------------------------------------

@scans_bp.route("/scan/start", methods=["POST"])
def scan_start():
    scan_type = request.form.get("scan_type", "full")
    artist = (request.form.get("artist") or "").strip()
    force = form_bool(request.form.get("force"))

    # -------------------------------------------------
    # Artist-only scan
    # -------------------------------------------------
    if scan_type == "artist":
        if not artist:
            flash("Error: No artist name provided", "danger")
            return redirect(url_for("dashboard"))

        run_async(run_artist_pipeline, artist, force)

        flash(f"Scan started for artist: {artist}", "success")
        return redirect(url_for("artist_detail", name=artist))

    # -------------------------------------------------
    # Prevent duplicate scans
    # -------------------------------------------------
    with scan_lock:
        if is_runtime_running("library"):
            flash("A scan is already running", "warning")
            return redirect(url_for("dashboard"))

    # -------------------------------------------------
    # Full/library scan
    # -------------------------------------------------
    if scan_type in {"full", "force"}:
        result = start_library_scan(
            artist_filter=None,
            resume=True,
            force=(scan_type == "force"),
        )

        flash(f"Scan started: {scan_type}", "success")
        return redirect(url_for("dashboard"))

    # -------------------------------------------------
    # Navidrome scan
    # -------------------------------------------------
    if scan_type == "navidrome":
        run_async(run_navidrome_import_scan, mode="all")

        flash("Navidrome import scan started", "success")
        return redirect(url_for("dashboard"))

    # -------------------------------------------------
    # Popularity scans
    # -------------------------------------------------
    if scan_type in {"popularity", "metadata", "singles", "singles_detection"}:
        run_async(run_popularity_mode, mode=scan_type)

        flash(f"{scan_type} scan started", "success")
        return redirect(url_for("dashboard"))

    flash(f"Unknown scan type: {scan_type}", "danger")
    return redirect(url_for("dashboard"))


# -------------------------------------------------------------------------
# Stop scan (generic)
# -------------------------------------------------------------------------

@scans_bp.route("/scan/stop", methods=["POST"])
def scan_stop():
    path = get_scan_progress_path("library_scan")

    request_scan_stop(path)

    flash("Stop requested for library scan", "info")
    return redirect(url_for("dashboard"))


# -------------------------------------------------------------------------
# Stop specific scans
# -------------------------------------------------------------------------

@scans_bp.route("/scan/stop-popularity", methods=["POST"])
def scan_stop_popularity():
    request_scan_stop(get_scan_progress_path("popularity_scan"))
    request_scan_stop(get_scan_progress_path("singles_scan"))

    flash("Popularity scan stop requested", "info")
    return redirect(url_for("dashboard"))


@scans_bp.route("/scan/stop-singles", methods=["POST"])
def scan_stop_singles():
    request_scan_stop(get_scan_progress_path("singles_scan"))

    flash("Single detection scan stop requested", "info")
    return redirect(url_for("dashboard"))


@scans_bp.route("/scan/stop-navidrome", methods=["POST"])
def scan_stop_navidrome():
    request_scan_stop(get_scan_progress_path("navidrome_scan"))

    flash("Navidrome scan stop requested", "info")
    return redirect(url_for("dashboard"))


# -------------------------------------------------------------------------
# Stop all scans
# -------------------------------------------------------------------------

@scans_bp.route("/scan/stop-all", methods=["POST"])
def scan_stop_all():
    for scan_type in [
        "navidrome_scan",
        "popularity_scan",
        "singles_scan",
        "essentia_mood_scan",
        "combined_scan",
        "missing_releases_scan",
        "mp3_import",
    ]:
        request_scan_stop(get_scan_progress_path(scan_type))

        # Clear runtime tracking (safe cleanup)
        clear_runtime(scan_type.replace("_scan", ""))

    flash("Stop requested for all scans", "success")
    return redirect(url_for("dashboard"))


# -------------------------------------------------------------------------
# Status
# -------------------------------------------------------------------------

@scans_bp.route("/scan/status")
def scan_status():
    running = is_runtime_running("library")

    return jsonify({"running": running})


# -------------------------------------------------------------------------
# Clear stuck scans
# -------------------------------------------------------------------------

@scans_bp.route("/scan/clear-stuck", methods=["POST"])
def scan_clear_stuck():
    cleared = 0

    for scan_type in [
        "navidrome_scan",
        "popularity_scan",
        "singles_scan",
        "essentia_mood_scan",
        "combined_scan",
        "missing_releases_scan",
        "mp3_import",
    ]:
        path = get_scan_progress_path(scan_type)
        state = read_progress_file(path)

        if state.get("is_running"):
            request_scan_stop(path)
            cleared += 1

    flash(
        f"✅ Cleared {cleared} stuck scan(s)"
        if cleared else "No stuck scans found",
        "success" if cleared else "info",
    )

    return redirect(url_for("dashboard"))