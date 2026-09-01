"""Scan Pipeline Orchestration

High-level orchestration for library and Navidrome scanning operations.
"""

from __future__ import annotations

import threading
from typing import Any, Callable
import structlog

from helpers.logging_config import log_unified
from services.scanning.scan_history_service import record_scan
from services.scanning.scan_state import (
    get_navidrome_progress_path,
    get_library_progress_path,
    get_navidrome_checkpoint_path,
    get_scan_progress_path,
    write_progress_with_current_artist,
    is_stop_requested,
    save_artist_scan_checkpoint,
    load_scan_checkpoint,
    clear_scan_checkpoint,
    mark_navidrome_first_full_import_complete,
    get_library_checkpoint_path,
    clear_stop_request,
)
from services.scanning.navidrome_scan_service import build_artist_index
from services.scanning.navidrome_import import (
    pre_import_sync_album_artists,
    scan_artist_to_db,
)
from services.popularity.pipeline import run_popularity_scan

logger = structlog.get_logger(__name__)

# Runtime state & concurrency guard
scan_process_navidrome: dict[str, Any] | None = None
_artist_scan_lock = threading.Lock()
_running_artists: set[str] = set()


def _try_claim_artist(artist_name: str) -> bool:
    key = str(artist_name or "").strip().lower()
    if not key:
        return False
    with _artist_scan_lock:
        if key in _running_artists:
            return False
        _running_artists.add(key)
        return True


def _release_artist(artist_name: str) -> None:
    key = str(artist_name or "").strip().lower()
    with _artist_scan_lock:
        _running_artists.discard(key)


def run_artist_scan_pipeline(
    artist_name: str,
    force: bool = False,
    progress_callback: Callable[[str, int, int, str | None, float | None], None] | None = None,
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
    progress_callback: Callable[[str, int, int, str | None, float | None], None] | None = None,
) -> None:
    def _cb(stage: str, idx: int, total: int, item: str | None = None, track_fraction: float | None = None) -> None:
        if callable(progress_callback):
            try:
                progress_callback(stage, idx, total, item or artist_name, track_fraction)
            except Exception:
                pass

    if callable(progress_callback):
        _cb("metadata", 0, 1, artist_name)

    if callable(progress_callback):
        import_progress_file: str | None = None
        popularity_progress_file: str | None = None
        stop_progress_file = get_scan_progress_path("full_scan")
    else:
        import_progress_file = "navidrome_scan"
        popularity_progress_file = "popularity_scan"
        stop_progress_file = None

    log_unified(f"[SCAN_PIPELINE] Starting artist pipeline: {artist_name} (force={force})")
    record_scan("artist", "started", message=f"Artist scan: {artist_name}", artist=artist_name)
    
    try:
        try:
            for _st in ("navidrome_scan", "popularity_scan"):
                clear_stop_request(_st)
        except Exception as _clear_exc:
            logger.debug("[SCAN_PIPELINE] Stop-flag clear failed", artist=artist_name, error=str(_clear_exc))

        from db.repositories.scan_repository import lookup_artist_id
        artist_id = lookup_artist_id(artist_name)
        
        if not artist_id:
            try:
                from helpers.text_utils import normalize_artist_key as _norm_key
                index = build_artist_index() or {}
                target_key = _norm_key(artist_name)
                for _name, _info in index.items():
                    if _info.get("id") and _norm_key(_name) == target_key:
                        artist_id = str(_info.get("id"))
                        log_unified(f"[SCAN_PIPELINE] Resolved Navidrome artist id for '{artist_name}' from index")
                        break
            except Exception as _idx_exc:
                logger.debug("[SCAN_PIPELINE] Navidrome index fallback failed", artist=artist_name, error=str(_idx_exc))

        if artist_id:
            scan_artist_to_db(
                artist_name,
                artist_id,
                verbose=True,
                force=force,
                diff_mode=True,  # Enable differential mode for performance
                progress_file=import_progress_file or stop_progress_file,
            )
        else:
            log_unified(f"Navidrome import skipped for '{artist_name}' (no Navidrome artist ID found)")

        def _stage_band_cb(album_index: int, total_albums: int, current_item: str | None = None, track_fraction: float | None = None) -> None:
            band = max(1, (total_albums + 3) // 4)
            if album_index < band:
                stage = "metadata"
            elif album_index >= (total_albums or 1) - band:
                stage = "singles"
            else:
                stage = "popularity"
            _cb(stage, album_index, total_albums, current_item, track_fraction)

        run_popularity_scan(
            verbose=True,
            force=force,
            artist_filter=artist_name,
            progress_file=popularity_progress_file,
            stop_progress_file=stop_progress_file,
            progress_callback=_stage_band_cb,
        )

        if callable(progress_callback):
            _cb("essentia", 0, 1, artist_name)
        try:
            from services.scanning.pipelines.essentia_scanner import run_essentia_mood_scan
            run_essentia_mood_scan(artist_filter=artist_name, force=force)
        except Exception as exc:
            logger.debug("[SCAN_PIPELINE] Essentia scan skipped", artist=artist_name, error=str(exc))
        
        if callable(progress_callback):
            _cb("essentia", 1, 1, artist_name)

        save_artist_scan_checkpoint(artist_name)
        log_unified(f"Artist scan complete: {artist_name}")
        record_scan("artist", "completed", message=f"Artist scan complete: {artist_name}", artist=artist_name)

    except Exception as exc:
        log_unified(f"Artist scan failed: {exc}")
        record_scan("artist", "failed", message=f"Artist scan failed: {exc}", artist=artist_name)


def start_library_scan(
    artist_filter: str | None = None,
    resume: bool = True,
    force: bool = False,
    restart: bool = False,
) -> dict[str, Any]:
    if restart or not resume:
        clear_scan_checkpoint()

    if artist_filter:
        try:
            run_artist_scan_pipeline(artist_filter, force=force)
        except Exception as exc:
            logger.error("Artist scan failed", artist=artist_filter, error=str(exc))
            return {"success": False, "message": f"Artist scan failed: {exc}"}
        return {"success": True, "message": f"Scan started for artist: {artist_filter}"}

    try:
        run_full_library_scan(force=force, restart=restart)
    except Exception as exc:
        logger.error("Full library scan failed", error=str(exc))
        return {"success": False, "message": f"Full scan failed: {exc}"}

    return {"success": True, "message": "Full library scan started"}


def _validated_resume_artist(
    artists: list[tuple[str, Any]],
    checkpoint_path: str | None,
    force: bool,
    restart: bool = False,
) -> str | None:
    if restart:
        return None

    checkpoint = load_scan_checkpoint(checkpoint_path)
    resume_from = checkpoint.get("last_scanned_artist")
    if not resume_from:
        return None

    index_names = {str(name) for name, _ in artists}
    if resume_from not in index_names:
        log_unified(f"Full library scan - resume artist '{resume_from}' not found; starting from beginning")
        try:
            clear_scan_checkpoint(checkpoint_path)
        except Exception as exc:
            logger.debug("[SCAN_PIPELINE] Could not clear stale checkpoint", error=str(exc))
        return None
    return str(resume_from)


def run_full_library_scan(force: bool = False, restart: bool = False):
    progress = get_library_progress_path()
    checkpoint_path = get_library_checkpoint_path()

    try:
        clear_stop_request(progress)
    except Exception as exc:
        logger.debug("[SCAN_PIPELINE] Stop-flag clear failed", error=str(exc))

    record_scan("full", "started", message="Full library scan")
    try:
        write_progress_with_current_artist(progress, "library_scan", True)
        artists = list((build_artist_index() or {}).items())
        resume_from = _validated_resume_artist(artists, checkpoint_path, force, restart)
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

            write_progress_with_current_artist(progress, "library_scan", True, current_artist=name)

            try:
                run_artist_scan_pipeline(name, force=force)
            except Exception as exc:
                logger.exception("[SCAN_PIPELINE] Artist scan crashed (continuing)", artist=name, error=str(exc))
                log_unified(f"Artist scan failed for '{name}': {exc} — continuing")

            save_artist_scan_checkpoint(name, checkpoint_path)

        clear_scan_checkpoint(checkpoint_path)
        write_progress_with_current_artist(progress, "library_scan", False)
        log_unified("Full library scan complete")
        record_scan("full", "completed", message="Full library scan complete")

    except Exception as exc:
        log_unified(f"Full scan failed: {exc}")
        write_progress_with_current_artist(progress, "library_scan", False)
        record_scan("full", "failed", message=f"Full scan failed: {exc}")


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

                write_progress_with_current_artist(progress, "navidrome_scan", True, current_artist=name)

                artist_id = data.get("id")
                if not artist_id:
                    continue

                # Enabled diff_mode here to make startup imports significantly faster
                scan_artist_to_db(name, artist_id, diff_mode=True)
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
