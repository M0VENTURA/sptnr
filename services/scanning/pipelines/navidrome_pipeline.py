"""
Navidrome import-only scan pipeline.

Uses scan_state as the single source of truth for:
- progress
- stop requests
- checkpoints
"""

from __future__ import annotations

import logging
import os
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

        # Build artist index using NavidromeClient directly
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
        
        artist_map: dict[str, dict[str, Any]] = {}
        if nav_client:
            try:
                artist_map = nav_client.build_artist_index() or {}
            except Exception as exc:
                logging.error("Failed to build artist index: %s", exc)
        
        artists = list(artist_map.items())
        total = len(artists)

        start_index = 0

        if mode in {"resume", "resume_force"}:
            checkpoint = load_scan_checkpoint(checkpoint_file)
            last_artist = checkpoint.get("last_scanned_artist")

            if last_artist:
                for idx, (artist_name, _) in enumerate(artists):
                    if artist_name == last_artist:
                        start_index = idx + 1
                        break

        for index, (artist_name, info) in enumerate(
            artists[start_index:], start=start_index + 1
        ):
            if is_stop_requested(progress_file):
                write_progress_with_current_artist(
                    progress_file,
                    "navidrome_scan",
                    False,
                    current_artist=artist_name,
                    extra={"status": "stopped", "exit_code": 0},
                )
                return

            artist_id = info.get("id") if isinstance(info, dict) else None
            if not artist_id:
                continue

            write_progress_with_current_artist(
                progress_file,
                "navidrome_scan",
                True,
                current_artist=artist_name,
                extra={
                    "status": "running",
                    "processed_artists": index,
                    "total_artists": total,
                    "percent_complete": int((index / total) * 100) if total else 0,
                },
            )

            scan_artist_to_db(
                artist_name,
                artist_id,
                verbose=False,
                force=force_rescan,
                filter_missing=filter_missing,
                processed_artists=index,
                total_artists=total,
            )

            save_artist_scan_checkpoint(artist_name, checkpoint_file)

        clear_scan_checkpoint(checkpoint_file)

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