"""
Popularity pipeline entrypoints.

Stable WebUI/scheduler entrypoint for popularity scans.
"""

from __future__ import annotations

import importlib
import logging
import os

from typing import Any, Callable

from services.popularity.progress_tracker import update
from services.scanning.scan_state import (
    write_progress_with_current_artist,
    clear_stop_request,
)

logger = logging.getLogger(__name__)

DEFAULT_SCANNER_MODULE = "services.popularity.scan_stage_runner"


# =============================================================================
# Errors
# =============================================================================

class PopularityPipelineError(RuntimeError):
    pass


# =============================================================================
# Scanner resolution
# =============================================================================

def _load_scanner_module():
    module_name = os.environ.get(
        "POPULARITY_STAGE_RUNNER_MODULE",
        DEFAULT_SCANNER_MODULE,
    )

    try:
        return importlib.import_module(module_name)
    except Exception as exc:
        raise PopularityPipelineError(
            f"Could not import scanner module '{module_name}'"
        ) from exc


def _resolve_scanner_callable(scanner_module) -> Callable[..., Any]:
    """Return the scan entry point from a loaded scanner module."""
    staged = getattr(scanner_module, "run_scan", None)
    if staged:
        return staged

    scan_fn = getattr(scanner_module, "popularity_scan", None)
    if scan_fn:
        return scan_fn

    raise PopularityPipelineError(
        f"{scanner_module.__name__} has no valid scan entry point."
    )


# =============================================================================
# Core entrypoint
# =============================================================================

def run_popularity_scan(
    *,
    verbose: bool = False,
    resume_from: str | None = None,
    artist_filter: str | None = None,
    album_filter: str | None = None,
    force: bool = False,
    singles_only: bool = False,
    singles_with_missing_popularity: bool = False,
    popularity_only: bool = False,
    metadata_only: bool = False,
    progress_file: str | None = None,
    caller_scan_type: str | None = None,
    **extra_kwargs: Any,
):
    """Run the popularity scan pipeline. Entry point for CLI, WebUI, and scheduler."""
    logger.info("[POPULARITY_PIPELINE] Starting scan (artist=%s, verbose=%s, force=%s)",
                 artist_filter or "ALL", verbose, force)

    # ✅ CLEAR STALE STOP FLAGS: Ensure the scan starts with a clean slate
    if progress_file:
        try:
            clear_stop_request(progress_file)
        except Exception as e:
            logger.warning("Failed to clear stop request flag: %s", e)

    # Persist the DB scan-state "running" marker so the dashboard /api/scan-progress
    # feed shows targeted album/artist scans too (previously only
    # ``run_popularity_mode`` wrote progress, so a scan started from the album or
    # artist page was invisible to the dashboard and looked like it had halted).
    _scan_type_label = _derive_progress_scan_type(
        metadata_only=metadata_only,
        singles_only=singles_only,
        singles_with_missing_popularity=singles_with_missing_popularity,
        popularity_only=popularity_only,
    )
    if progress_file:
        try:
            write_progress_with_current_artist(
                progress_file,
                _scan_type_label,
                True,
                current_artist=artist_filter,
                extra={
                    "status": "running",
                    "mode": _scan_type_label,
                    "force": force,
                },
            )
        except Exception as e:
            logger.debug("Failed to mark scan running in DB state: %s", e)

    update(stage="initialising", progress=1, message="Starting popularity scan...")

    scanner_module = _load_scanner_module()
    scanner = _resolve_scanner_callable(scanner_module)

    kwargs = {
        "verbose": verbose,
        "resume_from": resume_from,
        "artist_filter": artist_filter,
        "album_filter": album_filter,
        "force": force,
        "singles_only": singles_only,
        "singles_with_missing_popularity": singles_with_missing_popularity,
        "popularity_only": popularity_only,
        "metadata_only": metadata_only,
        "progress_file": progress_file,
        "caller_scan_type": caller_scan_type,
    }

    kwargs.update(extra_kwargs)

    logger.info("Running popularity scan via %s", scanner_module.__name__)

    from helpers.logging_config import log_unified
    try:
        result = scanner(**kwargs)

        # Check if the scan was gracefully stopped by the user
        if result is False or (isinstance(result, dict) and result.get("status") == "stopped"):
            update(stage="stopped", message="Scan stopped by user")
            log_unified("[POPULARITY] Scan stopped by user request")
            from services.popularity.progress_tracker import finish as _tracker_finish
            _tracker_finish(success=False)
        else:
            update(stage="complete", progress=100, message="Scan complete")
            log_unified("[POPULARITY] Scan complete")

        if progress_file:
            try:
                write_progress_with_current_artist(
                    progress_file,
                    _scan_type_label,
                    False,
                    current_artist=artist_filter,
                    extra={
                        "status": "complete" if result is not False else "stopped",
                        "mode": _scan_type_label,
                        "exit_code": 0,
                    },
                )
            except Exception as e:
                logger.debug("Failed to mark scan complete in DB state: %s", e)

        return result
    except Exception:
        update(stage="failed", message="Scan failed")
        log_unified("[POPULARITY] Scan failed")
        from services.popularity.progress_tracker import finish as _tracker_finish
        _tracker_finish(success=False)

        if progress_file:
            try:
                write_progress_with_current_artist(
                    progress_file,
                    _scan_type_label,
                    False,
                    current_artist=artist_filter,
                    extra={
                        "status": "error",
                        "mode": _scan_type_label,
                        "error": "Scan failed",
                        "exit_code": 1,
                    },
                )
            except Exception as e:
                logger.debug("Failed to mark scan failed in DB state: %s", e)

        raise


def _derive_progress_scan_type(
    *,
    metadata_only: bool = False,
    singles_only: bool = False,
    singles_with_missing_popularity: bool = False,
    popularity_only: bool = False,
) -> str:
    """Map runner options to the progress-file scan-type label."""
    if metadata_only:
        return "metadata_lookup_scan"
    if singles_only or singles_with_missing_popularity:
        return "singles_scan"
    return "popularity_scan"


# =============================================================================
# Artist entrypoint
# =============================================================================

def run_popularity_from_artist(
    *,
    artist: str,
    force_rescan: bool = False,
    progress_file: str | None = None,
    verbose: bool = False,
):
    logger.info("Starting popularity scan from artist '%s'", artist)

    if progress_file:
        payload: dict[str, Any] = {
            "resume_from": artist,
        }
        write_progress_with_current_artist(
            progress_file,
            "popularity_scan",
            True,
            current_artist=artist,
            extra={
                "status": "running",
                "stop_requested": False,  # ✅ Overwrite any lingering stop state
                "resume_from": artist,
                "processed_artists": 0,
                "total_artists": 0,
                "percent_complete": 0,
            },
        )

    try:
        completed = run_popularity_scan(
            verbose=verbose,
            force=force_rescan,
            resume_from=artist,
            progress_file=progress_file,
            caller_scan_type="popularity",
        )

        if progress_file:
            payload = {
                "resume_from": artist,
            }

            # Check for the dictionary stop payload
            if completed is False or (isinstance(completed, dict) and completed.get("status") == "stopped"):
                payload["status"] = "stopped"
                payload["exit_code"] = 1
                logger.info("Scan stopped for '%s'", artist)
            else:
                payload["status"] = "complete"
                payload["exit_code"] = 0
                payload["percent_complete"] = 100
                logger.info("Scan complete for '%s'", artist)

            write_progress_with_current_artist(
                progress_file,
                "popularity_scan",
                False,
                current_artist=artist,
                extra=payload,
            )

        return completed

    except Exception as exc:
        logger.error("Scan failed for '%s': %s", artist, exc, exc_info=True)

        if progress_file:
            write_progress_with_current_artist(
                progress_file,
                "popularity_scan",
                False,
                current_artist=artist,
                extra={
                    "status": "error",
                    "resume_from": artist,
                    "error": str(exc),
                    "exit_code": 1,
                },
            )

        raise


# =============================================================================
# Convenience wrappers
# =============================================================================

def run_metadata_only_scan(**kwargs):
    kwargs["metadata_only"] = True
    return run_popularity_scan(**kwargs)


def run_popularity_only_scan(**kwargs):
    kwargs["popularity_only"] = True
    return run_popularity_scan(**kwargs)
