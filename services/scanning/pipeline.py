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
    3. Combined Scan: ONE popularity pass per artist — metadata resolution +
       popularity + singles detection + genres + covers in a single scrape
       (the standalone metadata pre-pass was removed; the combined pass is a
       strict superset of it and resolves MB metadata BEFORE scoring, so a
       forced artist scan no longer scrapes the APIs twice)
    4. Essentia: mood/feature scan (optional)
    
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

import structlog

from typing import Any, Callable
from services.popularity.pipeline import run_popularity_scan
from helpers.logging_config import log_unified
from services.scanning.scan_history_service import record_scan

logger = structlog.get_logger(__name__)

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
    get_library_checkpoint_path,
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

# In-process guard: prevents the same artist pipeline from running twice
# concurrently (double form submits on the artist page, artist page + dashboard
# triggers). Two overlapping runs double the Last.fm/ListenBrainz/MusicBrainz
# API load per track, which triggers rate limits and yields inconsistent data.
_artist_scan_lock = threading.Lock()
_running_artists: set[str] = set()


def _try_claim_artist(artist_name: str) -> bool:
    """Claim the artist for this pipeline run. False if already running."""
    global _running_artists
    key = str(artist_name or "").strip().lower()
    if not key:
        return False
    with _artist_scan_lock:
        if key in _running_artists:
            return False
        _running_artists.add(key)
        return True


def _release_artist(artist_name: str) -> None:
    global _running_artists
    key = str(artist_name or "").strip().lower()
    with _artist_scan_lock:
        _running_artists.discard(key)


# -------------------------------------------------------------------------
# Artist pipeline
# -------------------------------------------------------------------------

def run_artist_scan_pipeline(
    artist_name: str,
    force: bool = False,
    progress_callback: Callable[[str, int, int, str | None], None] | None = None,
) -> None:
    if not _try_claim_artist(artist_name):
        log_unified(f"⏭️ Artist scan already running for: {artist_name} — skipping duplicate trigger")
        return
    try:
        _run_artist_scan_pipeline_inner(artist_name, force, progress_callback=progress_callback)
    finally:
        _release_artist(artist_name)


def _run_artist_scan_pipeline_inner(
    artist_name: str,
    force: bool = False,
    progress_callback: Callable[[str, int, int, str | None], None] | None = None,
) -> None:
    # Optional per-album progress hook (stage, album_index, total_albums,
    # item).  The dashboard full-scan orchestration uses it to report a
    # stage-aware, monotonic overall percentage; standalone artist scans
    # (artist page) call without it and keep their per-pipeline progress rows.
    def _cb(stage: str, idx: int, total: int, item: str | None = None) -> None:
        if callable(progress_callback):
            try:
                progress_callback(stage, idx, total, item or artist_name)
            except Exception:
                pass

    if callable(progress_callback):
        _cb("metadata", 0, 1, artist_name)

    log_unified(f"[SCAN_PIPELINE] Starting artist pipeline: {artist_name} (force={force})")
    record_scan("artist", "started", message=f"Artist scan: {artist_name}", artist=artist_name)
    try:
        log_unified(f"Artist scan started: {artist_name}")

        # Navidrome import — scoped to THIS artist.  The artist_id is
        # required: scan_artist_to_db silently no-ops when it is None.
        # progress_file wires dashboard stop requests into the import loop.
        from db.repositories.scan_repository import lookup_artist_id
        artist_id = lookup_artist_id(artist_name)
        if not artist_id:
            # Fallback: the artist_stats cache may be empty (fresh DB / first
            # scan), so resolve the Navidrome artist id straight from the
            # Navidrome index instead of skipping the import.  Without this a
            # forced artist scan silently imports nothing and the popularity
            # scan then reports "No tracks found".
            try:
                from helpers.text_utils import normalize_artist_key as _norm_key
                index = build_artist_index() or {}
                target_key = _norm_key(artist_name)
                for _name, _info in index.items():
                    if _info.get("id") and _norm_key(_name) == target_key:
                        artist_id = str(_info.get("id"))
                        log_unified(f"[SCAN_PIPELINE] Resolved Navidrome artist id for '{artist_name}' from index (artist_stats empty)")
                        break
            except Exception as _idx_exc:
                logger.debug(
                    "[SCAN_PIPELINE] Navidrome index fallback failed",
                    artist=artist_name,
                    error=str(_idx_exc),
                )
        if artist_id:
            scan_artist_to_db(artist_name, artist_id, verbose=True, force=force, progress_file="navidrome_scan")
        else:
            log_unified(f"Navidrome import skipped for '{artist_name}' (no Navidrome artist ID found)")

        # SINGLE combined pass — scoped to this artist only (artist_filter,
        # NOT resume_from, which would scan the whole library onwards).
        #
        # The combined pass is a STRICT SUPERSET of the old metadata pass:
        # it resolves MusicBrainz metadata (album batch + per-track fallback
        # + writer backfill) BEFORE popularity scoring (metadata
        # pre-resolution runs first inside process_track), then does
        # popularity + singles + genre + cover per track, then the album-
        # level enrichment (art, artist metadata, tags).  Running a separate
        # metadata_only pass first made a forced artist scan scrape the APIs
        # TWICE: the metadata pass resolved MBIDs/tags, then the combined
        # pass re-resolved them (forced scans bypass the MBID freshness gate)
        # and re-fetched every popularity count.  A 26-minute Battle Beast
        # scan was ~10 minutes of duplicate API work — the metadata pass is
        # gone and one combined pass does the whole scrape once.
        #
        # The dashboard's 4-stage progress model (Metadata / Popularity /
        # Singles Detection / Essentia) is preserved by splitting the combined
        # pass's album loop into bands: each album genuinely runs metadata
        # resolution → popularity → singles in that order, so the first
        # quarter of albums map to "Metadata", the last quarter to "Singles
        # Detection" and the middle half to "Popularity".
        def _stage_band_cb(album_index: int, total_albums: int, current_item: str | None = None) -> None:
            """Map the runner's (idx, total, item) callback to (stage, ...).

            The dashboard's 4-stage progress model (Metadata / Popularity /
            Singles Detection) is derived from the album loop position: the
            first quarter of albums map to "Metadata", the last quarter to
            "Singles Detection", and the middle half to "Popularity".
            """
            band = max(1, (total_albums + 3) // 4)
            if album_index < band:
                stage = "metadata"
            elif album_index >= (total_albums or 1) - band:
                stage = "singles"
            else:
                stage = "popularity"
            _cb(stage, album_index, total_albums, current_item)

        run_popularity_scan(
            verbose=True,
            force=force,
            artist_filter=artist_name,
            progress_file="popularity_scan",
            progress_callback=_stage_band_cb,
        )

        # optional essentia
        if callable(progress_callback):
            _cb("essentia", 0, 1, artist_name)
        try:
            from services.scanning.pipelines.essentia_scanner import run_essentia_mood_scan
            run_essentia_mood_scan(artist_filter=artist_name, force=force)
        except Exception as exc:
            logger.debug(
                "[SCAN_PIPELINE] Essentia scan skipped",
                artist=artist_name,
                error=str(exc),
            )
        if callable(progress_callback):
            _cb("essentia", 1, 1, artist_name)

        save_artist_scan_checkpoint(artist_name)

        log_unified(f"Artist scan complete: {artist_name}")
        record_scan("artist", "completed", message=f"Artist scan complete: {artist_name}", artist=artist_name)

    except Exception as exc:
        log_unified(f"Artist scan failed: {exc}")
        record_scan("artist", "failed", message=f"Artist scan failed: {exc}", artist=artist_name)


# -------------------------------------------------------------------------
# ✅ Full library scan (UI-driven)
# -------------------------------------------------------------------------

def start_library_scan(
    artist_filter: str | None = None,
    resume: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    """Start a full library scan with optional filtering and resume.

    This is the route-facing entry point called by ``control.py``.
    It wraps ``run_full_library_scan`` and accepts the same parameters
    that the old monolithic ``start_library_scan`` expected.

    Args:
        artist_filter: If set, scan only this artist (not yet implemented).
        resume: If True, resume from the last checkpoint; if False, start fresh.
        force: If True, force re-scan even if data hasn't changed.

    Returns:
        A dict with ``success`` and ``message`` keys.
    """
    if not resume or force:
        from services.scanning.scan_state import clear_scan_checkpoint
        clear_scan_checkpoint()

    if artist_filter:
        # Single-artist scan.
        try:
            run_artist_scan_pipeline(artist_filter, force=force)
        except Exception as exc:
            logger.error(
                "Artist scan failed",
                artist=artist_filter,
                error=str(exc),
            )
            return {"success": False, "message": f"Artist scan failed: {exc}"}
        return {"success": True, "message": f"Scan started for artist: {artist_filter}"}

    # Full library scan.
    try:
        run_full_library_scan(force=force)
    except Exception as exc:
        logger.error("Full library scan failed", error=str(exc))
        return {"success": False, "message": f"Full scan failed: {exc}"}

    return {"success": True, "message": "Full library scan started"}


def _validated_resume_artist(
    artists: list[tuple[str, Any]],
    checkpoint_path: str | None,
    force: bool,
) -> str | None:
    """Resolve the resume artist, clearing stale checkpoints that would truncate a scan.

    ``run_full_library_scan`` resumes from the last scanned artist.  A stale
    checkpoint — artist renamed in Navidrome, removed from the library, or
    stored under different casing — would otherwise leave the resume loop
    stuck in "skip" mode for every artist and silently do nothing (a full
    scan that never advances past the first letter).  When the checkpoint
    artist is not present in the current index the checkpoint is cleared so
    the scan starts from the top and continues through the whole library.
    """
    checkpoint = load_scan_checkpoint(checkpoint_path) if not force else {}
    resume_from = checkpoint.get("last_scanned_artist")
    if not resume_from:
        return None

    index_names = {str(name) for name, _ in artists}
    if resume_from not in index_names:
        log_unified(
            f"Full library scan - resume artist '{resume_from}' not found in "
            "the current library; starting from the beginning"
        )
        try:
            clear_scan_checkpoint(checkpoint_path)
        except Exception as exc:
            logger.debug(
                "[SCAN_PIPELINE] Could not clear stale checkpoint",
                error=str(exc),
            )
        return None
    return str(resume_from)


def run_full_library_scan(force: bool = False):
    progress = get_library_progress_path()
    checkpoint_path = get_library_checkpoint_path()

    record_scan("full", "started", message="Full library scan")
    try:
        write_progress_with_current_artist(progress, "library_scan", True)

        artists = list((build_artist_index() or {}).items())

        # A forced scan always starts from the top of the library — a resume
        # checkpoint left behind by a single-artist scan must never truncate
        # it (legacy: force disables the timestamp/score skips).
        resume_from = _validated_resume_artist(artists, checkpoint_path, force)

        resume_mode = bool(resume_from)

        for name, _data in artists:
            if resume_mode:
                if name == resume_from:
                    resume_mode = False
                continue

            if is_stop_requested(progress):
                log_unified("Scan stopped by user")
                record_scan("full", "failed", message="Scan stopped by user")
                break

            write_progress_with_current_artist(
                progress,
                "library_scan",
                True,
                current_artist=name,
            )

            # A single artist must never abort the whole library scan: wrap it
            # so a failure logs, records, and the loop continues with the next
            # artist instead of falling through to the outer ``except``.
            try:
                run_artist_scan_pipeline(name, force=force)
            except Exception as exc:
                logger.exception(
                    "[SCAN_PIPELINE] Artist scan crashed (continuing)",
                    artist=name,
                    error=str(exc),
                )
                log_unified(f"Artist scan failed for '{name}': {exc} — continuing with next artist")

            save_artist_scan_checkpoint(name, checkpoint_path)

        clear_scan_checkpoint(checkpoint_path)

        write_progress_with_current_artist(progress, "library_scan", False)

        log_unified("Full library scan complete")
        record_scan("full", "completed", message="Full library scan complete")

    except Exception as exc:
        log_unified(f"Full scan failed: {exc}")
        write_progress_with_current_artist(progress, "library_scan", False)
        record_scan("full", "failed", message=f"Full scan failed: {exc}")


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
