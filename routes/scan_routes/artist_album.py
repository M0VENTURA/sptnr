"""
Artist, album and track rescan routes.

Routes should remain thin:
- parse request data
- dispatch to pipelines
- flash + redirect
"""

from __future__ import annotations

from urllib.parse import unquote

from quart import flash, redirect, request, url_for

from routes.scan_routes import scans_bp
from routes.scan_routes._common import (
    form_bool,
    redirect_for_album,
    redirect_for_artist,
    run_async,
)

from services.scanning.runtime_state import (
    scan_lock,
    is_runtime_running,
    set_runtime,
    clear_runtime,
)

from services.scanning.pipelines.album_pipeline import run_album_pipeline
from services.scanning.pipelines.artist_pipeline import run_artist_pipeline
from services.scanning.pipelines.navidrome_targeted_pipeline import (
    run_navidrome_album_pipeline,
    run_navidrome_artist_pipeline,
)
from services.scanning.pipelines.popularity_pipeline import (
    run_popularity_album_scan,
    run_popularity_artist_scan,
)


def _start_targeted_popularity_scan(
    runner,
    *args,
    **kwargs,
) -> bool:
    """Start a targeted popularity scan under the runtime guard.

    Returns True when the scan was started, False when one is already running
    (mirrors the dashboard's /api/popularity/run duplicate protection).
    """
    with scan_lock:
        if is_runtime_running("popularity"):
            return False

        def _worker():
            try:
                runner(*args, **kwargs)
            finally:
                clear_runtime("popularity")

        thread = run_async(_worker)
        set_runtime("popularity", {"thread": thread, "type": kwargs.get("scan_type") or "popularity"})
    return True


@scans_bp.route("/scan/artist-custom", methods=["POST"])
async def scan_artist_custom():
    """Run a specific scan type for an artist."""
    form = await request.form
    scan_type = form.get("scan_type", "full")
    artist = (form.get("artist") or "").strip()
    force = form_bool(form.get("force"))

    if not artist:
        await flash("Error: No artist name provided", "danger")
        return redirect(url_for("ui.dashboard"))

    mode_label = "Forced" if force else "Changes Only"

    if scan_type == "full":
        run_async(run_artist_pipeline, artist, force)

    elif scan_type == "navidrome":
        run_async(run_navidrome_artist_pipeline, artist, force)

    elif scan_type in {"popularity", "metadata", "singles"}:
        started = _start_targeted_popularity_scan(
            run_popularity_artist_scan,
            artist,
            force=force,
            scan_type=scan_type,
        )
        if not started:
            await flash("A popularity scan is already running — skipped duplicate trigger", "warning")
            return redirect_for_artist(artist)

    elif scan_type == "essentia":
        return redirect(url_for("scans.scan_essentia_mood"), code=307)

    else:
        await flash(f"Unknown scan type: {scan_type}", "danger")
        return redirect_for_artist(artist)

    await flash(
        f"{scan_type.title()} scan started for artist: {artist} ({mode_label})",
        "success",
    )
    return redirect_for_artist(artist)


@scans_bp.route("/scan/album-custom", methods=["POST"])
async def scan_album_custom():
    """Run a specific scan type for an album."""
    form = await request.form
    scan_type = form.get("scan_type", "full")
    artist = (form.get("artist") or "").strip()
    album = (form.get("album") or "").strip()
    force = form_bool(form.get("force"))

    if not artist or not album:
        await flash("Error: Artist and album name are required", "danger")
        return redirect(url_for("ui.dashboard"))

    mode_label = "Forced" if force else "Changes Only"

    if scan_type == "full":
        run_async(run_album_pipeline, artist, album, force)

    elif scan_type == "navidrome":
        run_async(run_navidrome_album_pipeline, artist, album, force)

    elif scan_type in {"popularity", "metadata", "singles"}:
        started = _start_targeted_popularity_scan(
            run_popularity_album_scan,
            artist,
            album,
            force=force,
            scan_type=scan_type,
        )
        if not started:
            await flash("A popularity scan is already running — skipped duplicate trigger", "warning")
            return redirect_for_album(artist, album)

    elif scan_type == "essentia":
        return redirect(url_for("scans.scan_essentia_mood"), code=307)

    else:
        await flash(f"Unknown scan type: {scan_type}", "danger")
        return redirect_for_album(artist, album)

    await flash(
        f"{scan_type.title()} scan started for album '{album}' by {artist} ({mode_label})",
        "success",
    )
    return redirect_for_album(artist, album)


@scans_bp.route(
    "/track/<path:artist>/<path:album>/<path:track_id>/rescan",
    methods=["POST"],
)
async def scan_track_rescan(artist, album, track_id):
    """
    Trigger a track rescan.

    Current behaviour intentionally runs the artist pipeline, matching your
    existing implementation. Later this can be replaced by a dedicated
    track-level pipeline.
    """
    artist = unquote(artist)
    track_id = unquote(track_id)

    run_async(run_artist_pipeline, artist, False)

    await flash(f"Track rescan started for {artist}", "info")
    return redirect(url_for("ui.track_detail", track_id=track_id))