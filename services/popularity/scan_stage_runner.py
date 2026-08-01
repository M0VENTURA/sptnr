"""Staged popularity scan runner."""

from __future__ import annotations

import logging
from typing import Any

from helpers.logging_config import log_unified
from services.popularity.progress_tracker import finish, start, update
from services.popularity.popularity_cache_policy import should_freeze_track
from services.popularity.scan_hooks import (
    apply_context_fields_to_track,
    get_stat_eligible_tracks,
    prepare_tracks_for_album,
)
from services.popularity.popularity_sources import (
    get_listenbrainz_batch_for_tracks,
    get_lastfm_artist_max_listeners,
)
from services.popularity.stages.album_stage import enrich_album
from services.popularity.stages.finalise_stage import finalise_scan
from services.popularity.stages.load_stage import load_candidates
from services.popularity.stages.track_stage import process_track
from services.scanning.scan_state import (
    is_stop_requested,
    save_artist_scan_checkpoint,
    write_progress_with_current_artist,
)
from services.scanning.scan_history_service import record_scan, was_album_scanned

logger = logging.getLogger(__name__)


def _resolve_scan_type(options: dict[str, Any]) -> str:
    """Return a human-readable scan-type label from the runner options."""
    if options.get("metadata_only"):
        return "metadata"
    if options.get("singles_only") or options.get("singles_with_missing_popularity"):
        return "singles"
    if options.get("popularity_only"):
        return "popularity"
    return "combined"


def _load_mb_single_titles(artist: str) -> set[str]:
    """Return MusicBrainz single titles cached in ``missing_releases``.

    Mirrors the legacy pre-load: singles that MusicBrainz knows about but that
    may not be in the user's library yet. These are used to confirm single
    status without a per-track MusicBrainz API call.
    """
    if not artist:
        return set()
    try:
        from sqlalchemy import text as _text
        from db.engine import db_session as _db_session
        with _db_session() as session:
            result = session.execute(
                _text(
                    "SELECT title FROM missing_releases "
                    "WHERE LOWER(artist) = LOWER(:artist) AND LOWER(COALESCE(category, '')) = 'single'"
                ),
                {"artist": artist},
            )
            return {str(row[0]).strip().lower() for row in result.fetchall() or [] if row[0]}
    except Exception as exc:
        logger.debug("[scan_runner] Could not pre-load MB singles for '%s': %s", artist, exc)
        return set()


def run_scan(
    *,
    verbose: bool = False,
    resume_from: str | None = None,
    artist_filter: str | None = None,
    album_filter: str | None = None,
    skip_header: bool = False,
    force: bool = False,
    filter_missing: bool = False,
    singles_only: bool = False,
    singles_with_missing_popularity: bool = False,
    popularity_only: bool = False,
    metadata_only: bool = False,
    clear_single_detection_sources: list | None = None,
    stop_progress_file: str | None = None,
    caller_scan_type: str | None = None,
    **extra_kwargs: Any,
) -> dict[str, Any]:

    options = {
        "verbose": verbose,
        "resume_from": resume_from,
        "artist_filter": artist_filter,
        "album_filter": album_filter,
        "skip_header": skip_header,
        "force": force,
        "filter_missing": filter_missing,
        "singles_only": singles_only,
        "singles_with_missing_popularity": singles_with_missing_popularity,
        "popularity_only": popularity_only,
        "metadata_only": metadata_only,
        "clear_single_detection_sources": clear_single_detection_sources,
        "stop_progress_file": stop_progress_file,
        "caller_scan_type": caller_scan_type,
        **extra_kwargs,
    }

    update(stage="loading", progress=3, message="Loading scan candidates...")
    albums = load_candidates(options)
    total_albums = len(albums)

    # Mark the in-memory tracker as running so the WebUI progress service
    # (which only merges stage detail when ``running`` is True) picks up the
    # live stage updates emitted below.
    start(total_items=total_albums)

    if not albums:
        update(stage="complete", progress=100, message="No albums to scan.", processed=0, total_items=0)
        finish(success=True)
        return {"success": True, "albums_processed": 0, "tracks_processed": 0}

    albums_processed = 0
    tracks_processed = 0
    skipped_albums = 0
    results: list[dict[str, Any]] = []
    last_checkpoint_artist: str | None = None

    # Resolved once up-front (used for history records and skip checks).
    scan_type = _resolve_scan_type(options)

    # Per-artist Last.fm listener-context cache (used for dynamic weighting).
    artist_lf_context_cache: dict[str, dict[str, Any]] = {}

    # Per-artist MB single-title cache (from missing_releases) used to confirm
    # singles without per-track MusicBrainz API calls.
    artist_mb_singles_cache: dict[str, set[str]] = {}

    # Resolve stop progress file — accept both stop_progress_file (direct)
    # and progress_file (passed via **extra_kwargs by pipeline)
    effective_stop_file = stop_progress_file or extra_kwargs.get("progress_file")

    for album_index, album_row in enumerate(albums, start=1):

        # ✅ Graceful stop support
        if effective_stop_file and is_stop_requested(effective_stop_file):
            logger.info("Scan stopped by user request")
            finish(success=False)
            return False

        artist = album_row.get("artist") or ""
        album = album_row.get("album") or ""
        tracks = album_row.get("tracks") or []

        # ── Album skip (album_skip_days + skip-if-unchanged) ────────────
        # Mirrors the legacy scanner: albums already scanned within the
        # configured window, or whose tracks are all scored + singles
        # assessed, are skipped unless forced or explicitly filtered.
        skip_album = False
        if not force and not album_filter and not metadata_only:
            try:
                from helpers.config_helpers import get_feature
                skip_days = int(get_feature("album_skip_days", 7) or 0)
            except Exception:
                skip_days = 7
            if skip_days > 0:
                if was_album_scanned(artist, album, scan_type, skip_days):
                    skip_album = True
                    log_unified(f"Popularity Scan - Skipping album \"{album}\" (scanned within last {skip_days} days)")
                elif tracks:
                    all_scored = all(float(t.get("final_score") or 0) > 0 for t in tracks)
                    all_assessed = all(t.get("single_detection_last_updated") for t in tracks)
                    if all_scored and all_assessed:
                        skip_album = True
                        log_unified(f"Popularity Scan - Skipping album \"{album}\" (no changes detected)")
        if skip_album:
            skipped_albums += 1
            continue

        # ── Per-artist progress checkpoint ───────────────────────────────
        # Mirrors the legacy scanner: persist an in-progress checkpoint once
        # per artist so an interrupted scan can resume from this point.
        if effective_stop_file and artist and artist != last_checkpoint_artist:
            try:
                write_progress_with_current_artist(
                    effective_stop_file,
                    "popularity_scan",
                    True,
                    current_artist=artist,
                    extra={"status": "running", "stop_requested": False},
                )
                save_artist_scan_checkpoint(artist, effective_stop_file)
                last_checkpoint_artist = artist
            except Exception as exc:
                logger.debug("[scan_runner] Progress checkpoint write failed: %s", exc)

        # ── Per-artist Last.fm listener context (dynamic weight) ────────
        if artist and artist not in artist_lf_context_cache:
            try:
                from services.enrichment.single_detection_context_service import get_artist_lastfm_context
                artist_lf_context_cache[artist] = get_artist_lastfm_context(artist, None, None)
            except Exception as exc:
                logger.debug("[scan_runner] Last.fm context fetch failed for %s: %s", artist, exc)
                artist_lf_context_cache[artist] = {"mean": 0, "stdev": 0, "total": 0, "values": []}
        artist_lf_context = artist_lf_context_cache.get(artist) or {}

        # ── Per-artist MB single-title cache (missing_releases) ──────────
        if artist and artist not in artist_mb_singles_cache:
            artist_mb_singles_cache[artist] = _load_mb_single_titles(artist)
        mb_cached_singles = artist_mb_singles_cache.get(artist) or set()

        progress = 5 + int((album_index / total_albums) * 90)

        update(
            stage="album",
            progress=progress,
            message=f"Preparing {artist} - {album}",
            current_item=f"{artist} - {album}",
            processed=album_index,
            total_items=total_albums,
        )

        album_context, track_contexts = prepare_tracks_for_album(
            artist=artist,
            album=album,
            tracks=tracks,
            album_artist=album_row.get("album_artist"),
            spotify_album_type=album_row.get("spotify_album_type"),
            musicbrainz_album_type=album_row.get("musicbrainz_album_type"),
        )

        stat_eligible_tracks = get_stat_eligible_tracks(track_contexts)

        # Determine actual scan type from options for history display
        record_scan(scan_type, "started", message=f"{scan_type} scan: {artist} - {album}", artist=artist, album=album)

        album_result = enrich_album(
            album_row=album_row,
            album_context=album_context,
            stat_eligible_tracks=stat_eligible_tracks,
            options=options,
        )

        # ── Pre-fetch ListenBrainz data for ALL tracks in this album ──────
        # This lets us compute album-level LB percentiles and avoid N+1
        # per-track API calls.
        album_lb_data: dict[str, dict[str, int | None]] = {}
        try:
            track_dicts = [tc["track"] for tc in track_contexts if tc.get("track")]
            album_lb_data = get_listenbrainz_batch_for_tracks(track_dicts) or {}
        except Exception as exc:
            logger.debug("[scan_runner] Album LB batch fetch failed: %s", exc)

        # Build album-level LB listen-count list for percentile scoring.
        album_lb_listens: list[int] = []
        for mbid_key, lb_stats in album_lb_data.items():
            tc = int(lb_stats.get("total_listen_count") or 0) if lb_stats else 0
            if tc > 0:
                album_lb_listens.append(tc)

        # ── Pre-fetch Last.fm artist peak listener count ──────────────────
        # Used to normalise each track's LF score relative to the artist's
        # most popular track.  Cached in-memory so repeated artist lookups
        # across multiple albums cost at most one API call per artist.
        artist_max_lf = get_lastfm_artist_max_listeners(artist)

        # ✅ FIXED: Now properly counting track_contexts instead of the empty album_context array
        album_count = len(track_contexts)
        log_unified(f"[POPULARITY] Album {album_index}/{total_albums}: {artist} - {album} ({album_count} tracks)")

        for track_context in track_contexts:
            prepared_track = apply_context_fields_to_track(track_context)

            # ── Mature-track freeze ──────────────────────────────────────
            # Tracks older than 2 years with an existing final_score are
            # skipped — their popularity is stable and unlikely to change.
            if should_freeze_track(prepared_track):
                logger.debug(
                    "[scan_runner] Freezing mature track '%s' (has existing score %.1f)",
                    prepared_track.get("title", "?"),
                    prepared_track.get("final_score", 0),
                )
                # Persist the freeze state so the flag survives restarts
                # (legacy behaviour: popularity_frozen = TRUE).
                if not prepared_track.get("popularity_frozen"):
                    try:
                        from sqlalchemy import text as _text
                        from db.engine import db_session as _db_session
                        with _db_session() as session:
                            session.execute(
                                _text(
                                    "UPDATE tracks SET popularity_frozen = TRUE, "
                                    "popularity_frozen_at = CURRENT_TIMESTAMP "
                                    "WHERE id = :id AND COALESCE(popularity_frozen, FALSE) = FALSE"
                                ),
                                {"id": prepared_track.get("id")},
                            )
                    except Exception as exc:
                        logger.debug(
                            "[scan_runner] Could not persist freeze flag for %s: %s",
                            prepared_track.get("id"),
                            exc,
                        )
                tracks_processed += 1
                continue

            track_result = process_track(
                track=prepared_track,
                track_context=track_context,
                album_context=album_context,
                album_result=album_result,
                options=options,
                album_lb_data=album_lb_data,
                album_lb_listens=album_lb_listens if album_lb_listens else None,
                artist_max_lf_listeners=artist_max_lf,
                artist_lf_context=artist_lf_context,
                mb_cached_singles=mb_cached_singles,
            )

            if track_result is not None:
                results.append(track_result)
                
                # ✅ FIXED: Added detailed score logging output!
                if isinstance(track_result, dict):
                    title = prepared_track.get("title", "Unknown Track")
                    f_score = track_result.get("final_score")
                    
                    if f_score is not None:
                        sp = track_result.get("spotify_score") or 0.0
                        lf = track_result.get("lastfm_score") or 0.0
                        lb = track_result.get("listenbrainz_score") or 0.0
                        logger.info(
                            "[TRACK_RESULT] '%s' -> Final: %.1f (SP: %.1f | LF: %.1f | LB: %.1f)", 
                            title, f_score, sp, lf, lb
                        )

            tracks_processed += 1

        # Record completion for this album scan
        try:
            record_scan(scan_type, "completed", message=f"{scan_type} scan: {artist} - {album}", artist=artist, album=album)
        except Exception:
            pass  # Non-critical — dashboard data is best-effort

        albums_processed += 1

    update(stage="finalising", progress=98, message="Finalising popularity scan...", processed=total_albums, total_items=total_albums)

    finalise_scan(results=results, options=options)

    update(stage="complete", progress=100, message="Popularity scan complete.", processed=total_albums, total_items=total_albums)

    finish(success=True)

    return {
        "success": True,
        "albums_processed": albums_processed,
        "albums_skipped": skipped_albums,
        "tracks_processed": tracks_processed,
    }
