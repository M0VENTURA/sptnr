"""
Navidrome import-only scan pipeline.

Uses scan_state as the single source of truth for:
- progress
- stop requests
- checkpoints
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import threading
from typing import Any

from helpers.logging_config import log_unified
from services.scanning.navidrome_import import scan_artist_to_db
from services.scanning.scan_state import (
    clear_scan_checkpoint,
    clear_stop_request,
    get_scan_checkpoint_path,
    get_scan_progress_path,
    is_stop_requested,
    load_scan_checkpoint,
    save_artist_scan_checkpoint,
    write_progress_with_current_artist,
)

# Default worker count for parallel artist import.
# Can be overridden via config or env var.
_PARALLEL_WORKERS = int(os.environ.get("POPULARLR_IMPORT_WORKERS", "4"))


def run_navidrome_import_scan(
    *,
    mode: str = "all",
    restart_requested: bool = False,
    progress_file: str | None = None,
    checkpoint_file: str | None = None,
) -> None:

    progress_file = progress_file or get_scan_progress_path("navidrome_scan")
    checkpoint_file = checkpoint_file or get_scan_checkpoint_path("navidrome_scan")

    force_rescan = mode in {"force", "resume_force"}
    filter_missing = mode == "missing"
    workers = _PARALLEL_WORKERS if not force_rescan else 1  # serial for force scans

    try:
        os.environ["SPTNR_SKIP_SINGLES"] = "1"

        if restart_requested:
            clear_scan_checkpoint(checkpoint_file)

        clear_stop_request(progress_file)

        write_progress_with_current_artist(
            progress_file,
            "navidrome_scan",
            True,
            extra={"status": "starting", "mode": mode},
        )

        # Build client
        from api_clients.navidrome import NavidromeClient
        from helpers.config_helpers import get_navidrome_config

        nav_config = get_navidrome_config()
        nav_client = None
        if nav_config:
            nav_client = NavidromeClient(
                base_url=nav_config.get("base_url"),
                username=nav_config.get("user"),
                password=nav_config.get("pass"),
            )

        # Check scan marker — skip if nothing changed since last run
        current_marker: int | None = None
        if nav_client and not force_rescan:
            try:
                scan_status = nav_client.get_scan_status()
                if scan_status.get("success"):
                    current_marker = scan_status.get("count")
                    checkpoint = load_scan_checkpoint(checkpoint_file)
                    last_marker = checkpoint.get("scan_marker")
                    if last_marker is not None and current_marker == last_marker:
                        logging.info(
                            "Navidrome scan marker unchanged (%s) — skipping, nothing to import",
                            current_marker,
                        )
                        write_progress_with_current_artist(
                            progress_file,
                            "navidrome_scan",
                            False,
                            extra={
                                "status": "skipped",
                                "message": "No new tracks since last scan",
                                "exit_code": 0,
                            },
                        )
                        log_unified("Navidrome Import - Skipped (no changes)")
                        return
            except Exception as exc:
                logging.debug("Could not check scan marker: %s", exc)

        # Build artist index
        artist_map: dict[str, dict[str, Any]] = {}
        if nav_client:
            try:
                artist_map = nav_client.build_artist_index() or {}
            except Exception as exc:
                logging.error("Failed to build artist index: %s", exc)

        artists = list(artist_map.items())
        total = len(artists)
        logging.info("Navidrome import: %d artists to process (workers=%d)", total, workers)

        start_index = 0

        if mode in {"resume", "resume_force"}:
            checkpoint = load_scan_checkpoint(checkpoint_file)
            last_artist = checkpoint.get("last_scanned_artist")

            if last_artist:
                for idx, (artist_name, _) in enumerate(artists):
                    if artist_name == last_artist:
                        start_index = idx + 1
                        break

        # ------------------------------------------------------------------
        # Parallel artist processing
        # ------------------------------------------------------------------
        _progress_lock = threading.Lock()
        _processed_count = [0]  # mutable for closure
        _stop_requested = [False]

        def _process_one(artist_name: str, info: dict[str, Any]) -> bool:
            """Import one artist. Returns True on success."""
            if _stop_requested[0]:
                return False

            artist_id = info.get("id") if isinstance(info, dict) else None
            if not artist_id:
                return False

            scan_artist_to_db(
                artist_name,
                artist_id,
                verbose=False,
                force=force_rescan,
                filter_missing=filter_missing,
                diff_mode=not force_rescan,
            )

            with _progress_lock:
                _processed_count[0] += 1
                idx = _processed_count[0]
                pct = int((idx / total) * 100) if total else 0

                write_progress_with_current_artist(
                    progress_file,
                    "navidrome_scan",
                    True,
                    current_artist=artist_name,
                    extra={
                        "status": "running",
                        "processed_artists": idx,
                        "total_artists": total,
                        "percent_complete": pct,
                    },
                )
                save_artist_scan_checkpoint(
                    artist_name,
                    checkpoint_path=checkpoint_file,
                )
            return True

        if workers > 1 and total > 1:
            # Parallel mode
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(_process_one, name, info): name
                    for name, info in artists[start_index:]
                }
                for future in concurrent.futures.as_completed(futures):
                    if is_stop_requested(progress_file):
                        _stop_requested[0] = True
                        for f in futures:
                            f.cancel()
                        break
                    try:
                        future.result()
                    except Exception as exc:
                        logging.error("Artist import failed: %s", exc)
        else:
            # Serial mode (resume or forced)
            for index, (artist_name, info) in enumerate(
                artists[start_index:], start=start_index + 1
            ):
                if is_stop_requested(progress_file):
                    break
                _process_one(artist_name, info)

        clear_scan_checkpoint(checkpoint_file)

        # Save final marker so next run can skip if nothing changed
        if nav_client and not force_rescan:
            # Re-fetch marker after scan if we skipped the initial check
            try:
                if current_marker is None:
                    current_marker = nav_client.get_scan_status().get("count")
                if current_marker is not None:
                    save_artist_scan_checkpoint(
                        "__marker__",
                        checkpoint_path=checkpoint_file,
                        extra={"scan_marker": current_marker},
                    )
            except Exception as exc:
                logging.debug("Could not save scan marker: %s", exc)

        write_progress_with_current_artist(
            progress_file,
            "navidrome_scan",
            False,
            extra={"status": "complete", "exit_code": 0},
        )

        log_unified("Navidrome Import - Complete")

        try:
            from services.scanning.pipeline import run_post_navidrome_hooks
            run_post_navidrome_hooks("manual_post_import")
        except Exception as exc:
            logging.debug("Post-Navidrome follow-up skipped: %s", exc)

    except Exception as exc:
        logging.error("Navidrome import scan failed: %s", exc, exc_info=True)

        write_progress_with_current_artist(
            progress_file,
            "navidrome_scan",
            False,
            extra={"status": "error", "error": str(exc), "exit_code": 1},
        )
        raise

    finally:
        os.environ.pop("SPTNR_SKIP_SINGLES", None)