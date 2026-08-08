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

        elif mode == "all":
            scan_type = "full_scan"
            # No kwargs needed — full scan does everything

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