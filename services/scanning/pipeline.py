"""Scan Pipeline Orchestration

This module provides high-level orchestration for library and Navidrome scanning operations.

Key Responsibilities:
    - Coordinate multi-stage scanning pipelines (Navidrome → Library)
    - Manage scan state, checkpoints, and progress tracking
    - Handle resume capability via checkpoint persistence
    - Provide artist-level scan entry points
    
Pipeline Stages:
    1. Navidrome Sync: Fetch latest data from Navidrome server
    2. Artist Import: Process each artist through scan_artist_to_db()
    3. Metadata Scan: First pass metadata extraction
    4. Popularity Scan: Second pass popularity calculation
    
State Management:
    - Checkpoints saved after each artist completes
    - Progress files written for WebUI display
    - Stop requests can interrupt gracefully
    - Resume from last checkpoint on restart

Usage:
    >>> from services.scanning.pipeline import run_artist_scan_pipeline
    >>> run_artist_scan_pipeline("The Beatles", force=False)

Architecture:
    This is the top-level orchestration layer:
    
    User Request
        ↓
    pipeline.py (this file)
        ↓
    navidrome_import.py / popularity/pipeline.py
        ↓
    Lower-level scanners and processors

Thread Safety:
    - Uses threading for background scan execution
    - State stored in module-level dict (scan_process_navidrome)
    - Progress files use atomic writes
"""

from __future__ import annotations

import threading

from typing import Any
from services.popularity.pipeline import run_popularity_scan
from helpers.logging_config import log_unified

from services.scanning.scan_state import (
    get_navidrome_progress_path,
    get_library_progress_path,
    get_navidrome_checkpoint_path,
    write_progress_with_current_artist,
    is_stop_requested,
    save_artist_scan_checkpoint,
    load_scan_checkpoint,
    clear_scan_checkpoint,
    mark_navidrome_first_full_import_complete,
)
from services.scanning.navidrome_scan_service import build_artist_index
from services.scanning.navidrome_import import (
    pre_import_sync_album_artists,
    scan_artist_to_db,
)


# -------------------------------------------------------------------------
# Runtime state
# -------------------------------------------------------------------------

scan_process_navidrome: dict[str, Any] | None = None


# -------------------------------------------------------------------------
# Artist pipeline
# -------------------------------------------------------------------------

def run_artist_scan_pipeline(artist_name: str, force: bool = False):
    logger.info("[SCAN_PIPELINE] Starting artist pipeline: %s (force=%s)", artist_name, force)
    try:
        log_unified(f"Artist scan started: {artist_name}")

        scan_artist_to_db(artist_name, artist_id=None, verbose=True, force=force)

        # metadata first pass
        
        run_popularity_scan(
            verbose=True,
            force=force,
            resume_from=artist_name,
            metadata_only=True,
        )

        # scoring pass
        run_popularity_scan(
            verbose=True,
            force=force,
            resume_from=artist_name,
        )

        # optional essentia
        try:
            from essentia_mood_scan import run_essentia_mood_scan
            run_essentia_mood_scan(artist_filter=artist_name, force=force)
        except Exception as exc:
            logger.debug("[SCAN_PIPELINE] Essentia scan skipped for '%s': %s", artist_name, exc)

        save_artist_scan_checkpoint(artist_name)

        log_unified(f"Artist scan complete: {artist_name}")

    except Exception as exc:
        log_unified(f"Artist scan failed: {exc}")


# -------------------------------------------------------------------------
# ✅ Full library scan (UI-driven)
# -------------------------------------------------------------------------

def run_full_library_scan(force: bool = False):
    progress = get_library_progress_path()
    checkpoint_path = get_library_checkpoint_path()

    try:
        write_progress_with_current_artist(progress, "library_scan", True)

        artists = list((build_artist_index() or {}).items())

        checkpoint = load_scan_checkpoint(checkpoint_path)
        resume_from = checkpoint.get("last_scanned_artist")

        resume_mode = bool(resume_from)

        for name, data in artists:
            if resume_mode:
                if name == resume_from:
                    resume_mode = False
                continue

            if is_stop_requested(progress):
                log_unified("Scan stopped by user")
                break

            write_progress_with_current_artist(
                progress,
                "library_scan",
                True,
                current_artist=name,
            )

            run_artist_scan_pipeline(name, force=force)

            save_artist_scan_checkpoint(name, checkpoint_path)

        clear_scan_checkpoint(checkpoint_path)

        write_progress_with_current_artist(progress, "library_scan", False)

        log_unified("Full library scan complete")

    except Exception as exc:
        log_unified(f"Full scan failed: {exc}")
        write_progress_with_current_artist(progress, "library_scan", False)


# -------------------------------------------------------------------------
# ✅ Boot Navidrome import
# -------------------------------------------------------------------------

def start_boot_navidrome_import():
    global scan_process_navidrome

    def run():
        global scan_process_navidrome

        progress = get_navidrome_progress_path()
        checkpoint_path = get_navidrome_checkpoint_path()

        try:
            write_progress_with_current_artist(progress, "navidrome_scan", True)

            pre_import_sync_album_artists()

            artists = list((build_artist_index() or {}).items())

            for name, data in artists:
                if is_stop_requested(progress):
                    log_unified("Boot scan stopped by user")
                    break

                write_progress_with_current_artist(
                    progress,
                    "navidrome_scan",
                    True,
                    current_artist=name,
                )

                artist_id = data.get("id")
                if not artist_id:
                    continue

                scan_artist_to_db(name, artist_id)

                save_artist_scan_checkpoint(name, checkpoint_path)

            clear_scan_checkpoint(checkpoint_path)

            write_progress_with_current_artist(progress, "navidrome_scan", False)

            mark_navidrome_first_full_import_complete("boot")

            log_unified("Boot Navidrome import complete")

        finally:
            scan_process_navidrome = None

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    scan_process_navidrome = {"thread": thread}