"""
Popularity/metadata/singles scan pipeline helpers.

Uses scan_state as the single source of truth for:
- progress tracking
- stop requests

Routes should call this module rather than building scan kwargs inline.
"""

from __future__ import annotations

import logging
from typing import Any

from services.popularity.pipeline import run_popularity_scan
from helpers.logging_config import log_unified
from services.scanning.scan_history_service import record_scan

from services.scanning.scan_state import (
    get_scan_progress_path,
    write_progress_with_current_artist,
    is_stop_requested,
)


logger = logging.getLogger(__name__)


def is_popularity_scan_active() -> bool:
    """Return True when a popularity-family scan is already running.

    Checks BOTH the in-process runtime registry and the shared DB scan state.
    The runtime registry is per-worker, so in a multi-worker (hypercorn)
    deployment a manual scan started in worker A is invisible to worker B —
    the shared ``ScanState`` row is the cross-process source of truth.  This
    prevents a scheduled popularity scan from overlapping a manual one (and
    vice versa): both write to the SAME ``popularity_scan`` progress row, so
    whichever finishes first marks the shared state complete while the other
    is still running, which makes a full scan look like it halted mid-letter.
    """
    from services.scanning.runtime_state import is_runtime_running

    if is_runtime_running("popularity"):
        return True

    try:
        from services.scanning.scan_state import read_progress_file

        # The dashboard "All" scan runs under the "full_scan" progress row;
        # targeted popularity scans use "popularity_scan".  Check both so a
        # scan running in another hypercorn worker is never double-started.
        for _scan_type in ("popularity_scan", "full_scan"):
            state = read_progress_file(get_scan_progress_path(_scan_type))
            if state.get("is_running"):
                return True
    except Exception:
        return False
    return False


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================

def run_popularity_mode(
    *,
    mode: str,
    progress_file: str | None = None,
    force_rescan: bool = False,
    resume_from: str | None = None,
) -> None:
    """
    Run a popularity-related scan mode.

    Supported modes:
        metadata
        popularity
        singles
        singles_detection
        all
    """

    if mode == "all":
        # Dashboard "All" = full scan aligned with the artist page: iterate
        # over every artist, running the full artist pipeline (Navidrome
        # import → metadata → combined → essentia) for each, and report
        # progress as % of total artists completed.
        _run_full_scan_as_artist_pipeline(
            force=force_rescan,
            resume_from=resume_from,
        )
        return

    progress_file = progress_file or get_scan_progress_path("popularity_scan")

    try:
        scan_type = "popularity_scan"

        kwargs: dict[str, Any] = {
            "verbose": False,
            "force": force_rescan,
        }

        if resume_from:
            kwargs["resume_from"] = resume_from

        # ---------------------------------------------------------------------
        # Mode mapping
        # ---------------------------------------------------------------------
        if mode == "metadata":
            scan_type = "metadata_lookup_scan"
            kwargs["metadata_only"] = True

        elif mode == "singles":
            scan_type = "singles_scan"
            kwargs["singles_only"] = True

        elif mode == "singles_detection":
            scan_type = "singles_scan"
            kwargs["singles_with_missing_popularity"] = True

        elif mode == "popularity":
            # True popularity-only scan: scores popularity and rates tracks on
            # popularity alone (5★ reserved for standout popularity tracks).
            # No singles detection, metadata or cover work.
            scan_type = "popularity_scan"
            kwargs["popularity_only"] = True

        else:
            logger.warning("Unknown popularity scan mode '%s' — defaulting to full scan", mode)
            scan_type = "full_scan"

        # ---------------------------------------------------------------------
        # Mark scan start
        # ---------------------------------------------------------------------
        write_progress_with_current_artist(
            progress_file,
            scan_type,
            True,
            extra={
                "status": "starting",
                "mode": mode,
                "force": force_rescan,
            },
        )

        # ---------------------------------------------------------------------
        # Execute pipeline (correct entry point ✅)
        # ---------------------------------------------------------------------
        completed = run_popularity_scan(
            progress_file=progress_file,
            **kwargs,
        )

        # ---------------------------------------------------------------------
        # Determine final status
        # ---------------------------------------------------------------------
        stopped = is_stop_requested(progress_file)

        if stopped:
            status = "stopped"
        elif completed is False:
            status = "failed"
        else:
            status = "complete"

        # ---------------------------------------------------------------------
        # Mark scan completion
        # ---------------------------------------------------------------------
        write_progress_with_current_artist(
            progress_file,
            scan_type,
            False,
            extra={
                "status": status,
                "mode": mode,
                "exit_code": 0,
            },
        )

        log_unified(f"{scan_type} finished with status={status}")
        record_scan(mode, status, message=f"{mode} scan {status}", artist="_SCAN_SESSION_", album=mode)

    except Exception as exc:
        logger.error("Popularity pipeline failed: %s", exc, exc_info=True)
        record_scan(mode, "failed", message=f"{mode} scan failed: {exc}", artist="_SCAN_SESSION_", album=mode)

        write_progress_with_current_artist(
            progress_file or get_scan_progress_path("popularity_scan"),
            "popularity_scan",
            False,
            extra={
                "status": "error",
                "error": str(exc),
                "exit_code": 1,
            },
        )

        raise


# =============================================================================
# FULL SCAN (dashboard "All") — aligned with the artist page pipeline
# =============================================================================

def _run_full_scan_as_artist_pipeline(
    *,
    force: bool = False,
    resume_from: str | None = None,
) -> None:
    """Dashboard 'All' scan: iterate over every artist, running the full
    artist pipeline (Navidrome import → metadata → combined → essentia) for
    each, and report progress as % of total artists completed.

    This aligns the dashboard's combined scan with the artist page's full
    scan — the same per-artist pipeline runs for every artist in the library,
    and the footer shows the percentage of total artists completed.
    """
    from db.repositories.library import get_all_artists
    from services.scanning.pipeline import run_artist_scan_pipeline
    from services.popularity.stages.load_stage import _artist_key

    progress_file = get_scan_progress_path("full_scan")

    try:
        artists = get_all_artists()
    except Exception as exc:
        log_unified(f"[FULL_SCAN] Failed to load artist list: {exc}")
        logger.error("[FULL_SCAN] get_all_artists failed: %s", exc, exc_info=True)
        record_scan("all", "failed", message=f"full scan failed: {exc}", artist="_SCAN_SESSION_", album="all")
        return
    total = len(artists)

    log_unified(
        f"[FULL_SCAN] Starting full scan — {total} artist(s) queued"
        f"{' (forced)' if force else ''}"
    )
    if not artists:
        log_unified(
            "[FULL_SCAN] No artists found in the library — nothing to scan. "
            "Check the library has been imported (Navidrome sync)."
        )

    # Honour resume_from (legacy parity): skip artists before the resume
    # point, tolerating case/punctuation variants.
    if resume_from:
        _resume_key = _artist_key(resume_from)
        _started = False
        _filtered: list[str] = []
        for _a in artists:
            if not _started:
                if (
                    _a.lower() == resume_from.lower()
                    or _artist_key(_a) == _resume_key
                    or (_resume_key and len(_resume_key) >= 3 and _resume_key in _artist_key(_a))
                ):
                    _started = True
            if _started:
                _filtered.append(_a)
        artists = _filtered
        total = len(artists)

    write_progress_with_current_artist(
        progress_file,
        "full_scan",
        True,
        extra={
            "status": "starting",
            "mode": "all",
            "force": force,
            "total_artists": total,
            "processed_artists": 0,
            "percent_complete": 0,
        },
    )

    status = "complete"
    # Stage bands for the overall percentage.  Each artist contributes an
    # equal share; within an artist the four stages (Metadata, Popularity,
    # Singles Detection, Essentia) each take a quarter of that share.  The
    # artist pipeline now runs ONE combined pass (the standalone metadata
    # pass was removed — it re-scraped every API the combined pass scrapes);
    # the pass's album loop is split so the dashboard shows "Metadata" for
    # the first quarter of albums, "Popularity" for the middle half and
    # "Singles Detection" for the last quarter (each album genuinely runs
    # metadata resolution → popularity → singles in that order).  With a
    # 4-album artist this gives 1 album into metadata = ~6%, metadata done
    # = 25%, popularity done = 75%, etc.
    _STAGE_IDX = {"metadata": 0, "popularity": 1, "singles": 2, "essentia": 3}
    _STAGE_LABEL = {
        "metadata": "Metadata",
        "popularity": "Popularity",
        "singles": "Singles Detection",
        "essentia": "Essentia",
    }
    try:
        for i, artist in enumerate(artists):
            if is_stop_requested(progress_file):
                status = "stopped"
                log_unified("[FULL_SCAN] Stop requested — halting artist loop")
                break

            artist_base = (i / total) * 100.0 if total else 100.0
            artist_share = (100.0 / total) if total else 100.0
            stage_width = artist_share / 4.0

            def _cb(stage, idx, t, item, _i=i, _base=artist_base, _sw=stage_width, _artist=artist):
                try:
                    si = _STAGE_IDX.get(stage, 0)
                    frac = min(1.0, (int(idx) + 1) / float(t)) if t else 1.0
                    overall = max(0, min(100, int(_base + si * _sw + frac * _sw)))
                    write_progress_with_current_artist(
                        progress_file,
                        "full_scan",
                        True,
                        current_artist=_artist,
                        extra={
                            "status": "running",
                            "mode": "all",
                            "percent_complete": overall,
                            "current_stage": _STAGE_LABEL.get(stage, stage),
                            "current_item": item or _artist,
                            "processed_artists": _i,
                            "total_artists": total,
                        },
                    )
                except Exception:
                    pass

            log_unified(f"[FULL_SCAN] Artist {i + 1}/{total}: {artist}")
            try:
                run_artist_scan_pipeline(artist, force=force, progress_callback=_cb)
                log_unified(f"[FULL_SCAN] Artist {i + 1}/{total} done: {artist}")
            except Exception as _aexc:
                # run_artist_scan_pipeline swallows most errors internally, but
                # a raised one must not silently end the whole scan loop as if
                # it completed.
                log_unified(f"[FULL_SCAN] Artist {i + 1}/{total} FAILED: {artist} — {_aexc}")
                logger.warning("[FULL_SCAN] Artist %s failed: %s", artist, _aexc)
    except Exception as exc:
        status = "error"
        logger.error("[FULL_SCAN] Artist loop failed: %s", exc, exc_info=True)
        raise
    finally:
        write_progress_with_current_artist(
            progress_file,
            "full_scan",
            False,
            extra={
                "status": status,
                "mode": "all",
                "exit_code": 0,
                "percent_complete": 100 if status == "complete" else 0,
                "processed_artists": total if status == "complete" else 0,
                "total_artists": total,
            },
        )
        log_unified(f"[FULL_SCAN] Finished with status={status}")
        record_scan("all", status, message=f"full scan {status}", artist="_SCAN_SESSION_", album="all")


# =============================================================================
# HELPERS
# =============================================================================

def _build_targeted_popularity_kwargs(
    *,
    artist: str | None = None,
    album: str | None = None,
    force: bool = False,
    scan_type: str = "popularity",
) -> dict[str, Any]:
    """Build kwargs for targeted artist/album scans."""

    kwargs: dict[str, Any] = {
        "verbose": True,
        "force": force,
        # Targeted scans honour dashboard stop requests: the runner checks
        # is_stop_requested(progress_file) per album, and the dashboard
        # stop-all button flags "popularity_scan".
        "progress_file": "popularity_scan",
    }

    if artist:
        kwargs["artist_filter"] = artist

    if album:
        kwargs["album_filter"] = album

    if scan_type == "metadata":
        kwargs["metadata_only"] = True

    elif scan_type == "singles":
        kwargs["singles_only"] = True

    elif scan_type == "singles_detection":
        kwargs["singles_with_missing_popularity"] = True

    elif scan_type == "popularity":
        # Popularity-only: score + rate on popularity alone, no singles
        # detection / metadata / cover work (matches the dashboard's
        # "Popularity" scan mode).
        kwargs["popularity_only"] = True

    return kwargs


# =============================================================================
# TARGETED SCANS
# =============================================================================

def run_popularity_artist_scan(
    artist: str,
    *,
    force: bool = False,
    scan_type: str = "popularity",
) -> Any:
    """Run a scan for a single artist."""

    kwargs = _build_targeted_popularity_kwargs(
        artist=artist,
        force=force,
        scan_type=scan_type,
    )

    return run_popularity_scan(**kwargs)


def run_popularity_album_scan(
    artist: str,
    album: str,
    *,
    force: bool = False,
    scan_type: str = "popularity",
) -> Any:
    """Run a scan for a single album."""

    kwargs = _build_targeted_popularity_kwargs(
        artist=artist,
        album=album,
        force=force,
        scan_type=scan_type,
    )

    return run_popularity_scan(**kwargs)