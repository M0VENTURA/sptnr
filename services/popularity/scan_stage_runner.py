"""Staged popularity scan runner."""

from __future__ import annotations

import logging
from typing import Any

from helpers.logging_config import log_unified
from services.popularity.progress_tracker import update
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
from services.scanning.scan_state import is_stop_requested
from services.scanning.scan_history_service import record_scan

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

    if not albums:
        update(stage="complete", progress=100, message="No albums to scan.", processed=0, total_items=0)
        return {"success": True, "albums_processed": 0, "tracks_processed": 0}

    albums_processed = 0
    tracks_processed = 0
    results: list[dict[str, Any]] = []

    # Resolve stop progress file — accept both stop_progress_file (direct)
    # and progress_file (passed via **extra_kwargs by pipeline)
    effective_stop_file = stop_progress_file or extra_kwargs.get("progress_file")

    for album_index, album_row in enumerate(albums, start=1):

        # ✅ Graceful stop support
        if effective_stop_file and is_stop_requested(effective_stop_file):
            logger.info("Scan stopped by user request")
            return False

        artist = album_row.get("artist") or ""
        album = album_row.get("album") or ""
        tracks = album_row.get("tracks") or []

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
        _scan_type = _resolve_scan_type(options)
        record_scan(_scan_type, "started", message=f"{_scan_type} scan: {artist} - {album}", artist=artist, album=album)

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

        album_count = len(album_context.get("tracks") or [])
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
            )

            if track_result is not None:
                results.append(track_result)

            tracks_processed += 1

        # Record completion for this album scan
        try:
            record_scan(_scan_type, "completed", message=f"{_scan_type} scan: {artist} - {album}", artist=artist, album=album)
        except Exception:
            pass  # Non-critical — dashboard data is best-effort

        albums_processed += 1

    update(stage="finalising", progress=98, message="Finalising popularity scan...", processed=total_albums, total_items=total_albums)

    finalise_scan(results=results, options=options)

    update(stage="complete", progress=100, message="Popularity scan complete.", processed=total_albums, total_items=total_albums)

    return {
        "success": True,
        "albums_processed": albums_processed,
        "tracks_processed": tracks_processed,
    }