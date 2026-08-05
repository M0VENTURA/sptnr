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
        titles: set[str] = set()
        with _db_session() as session:
            result = session.execute(
                _text(
                    "SELECT title FROM missing_releases "
                    "WHERE LOWER(artist) = LOWER(:artist) AND LOWER(COALESCE(category, '')) = 'single'"
                ),
                {"artist": artist},
            )
            titles.update(str(row[0]).strip().lower() for row in result.fetchall() or [] if row[0])
        # Known MusicBrainz singles/EPs from the artist release cache (prefetched
        # once per artist — see release_cache_service).
        try:
            from services.popularity.release_cache_service import get_artist_single_titles
            titles |= get_artist_single_titles(artist, source="musicbrainz")
        except Exception:
            pass
        return titles
    except Exception as exc:
        logger.debug("[scan_runner] Could not pre-load MB singles for '%s': %s", artist, exc)
        return set()


def _load_discogs_single_titles(artist: str) -> set[str]:
    """Return Discogs single/EP titles from the artist release cache.

    Populated once per artist by ``prefetch_artist_releases`` (one Discogs
    artist-releases call); lets singles detection match local tracks against
    known Discogs singles without per-track Discogs searches.
    """
    if not artist:
        return set()
    try:
        from services.popularity.release_cache_service import get_artist_single_titles
        return get_artist_single_titles(artist, source="discogs") or set()
    except Exception as exc:
        logger.debug("[scan_runner] Could not pre-load Discogs singles for '%s': %s", artist, exc)
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
        log_unified(
            "Popularity Scan - No tracks found. All tracks may already have "
            "popularity data (run in Forced mode to rescan)."
        )
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

    # Per-artist Discogs single-title cache (from the artist release cache).
    artist_discogs_singles_cache: dict[str, set[str]] = {}

    # All candidate tracks grouped by artist — the popularity cache prefetch
    # runs ONCE per artist (one getTopTracks + LB batches for the whole
    # catalogue) instead of per album, so the per-track loop makes no
    # popularity API calls.
    artist_all_tracks: dict[str, list[dict[str, Any]]] = {}
    for _cand in albums or []:
        _cand_artist = str(_cand.get("artist") or "")
        if _cand_artist:
            artist_all_tracks.setdefault(_cand_artist, []).extend(_cand.get("tracks") or [])

    last_prefetch_artist: str | None = None
    prefetched_popularity: dict[str, dict[str, Any]] = {}

    # Resolve stop progress file — accept both stop_progress_file (direct)
    # and progress_file (passed via **extra_kwargs by pipeline)
    effective_stop_file = stop_progress_file or extra_kwargs.get("progress_file")

    # Letter progression headers (legacy parity): a full-library scan logs
    # each letter group (#-9, A, B, ...) as it advances, so operators can
    # follow progress through the alphabet in the unified log.
    _last_letter: str | None = None

    for album_index, album_row in enumerate(albums, start=1):

        # ✅ Graceful stop support
        if effective_stop_file and is_stop_requested(effective_stop_file):
            logger.info("Scan stopped by user request")
            finish(success=False)
            return False

        artist = album_row.get("artist") or ""
        album = album_row.get("album") or ""
        tracks = album_row.get("tracks") or []

        # Letter-section header (fires once per letter group).
        _first = (artist or " ")[0].upper()
        _letter = "#" if not _first.isalpha() else _first
        if _letter != _last_letter:
            _last_letter = _letter
            log_unified(f"Popularity Scan - Letter '{_letter}'")

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

        # ── Check if this is a compilation/Various Artists album ──────────
        _is_compilation_artist = artist.lower() in (
            "various artists", "various artists -", "various", 
            "compilation", "soundtrack"
        )

        # ── Per-artist Last.fm listener context (dynamic weight) ────────
        if artist and artist not in artist_lf_context_cache and not _is_compilation_artist:
            try:
                from services.enrichment.single_detection_context_service import get_artist_lastfm_context
                artist_lf_context_cache[artist] = get_artist_lastfm_context(artist, None, None)
            except Exception as exc:
                logger.debug("[scan_runner] Last.fm context fetch failed for %s: %s", artist, exc)
                artist_lf_context_cache[artist] = {"mean": 0, "stdev": 0, "total": 0, "values": []}
        artist_lf_context = artist_lf_context_cache.get(artist) or {}

        # ── Per-artist MB single-title cache (missing_releases) ──────────
        if artist and artist not in artist_mb_singles_cache and not _is_compilation_artist:
            artist_mb_singles_cache[artist] = _load_mb_single_titles(artist)
        mb_cached_singles = artist_mb_singles_cache.get(artist) or set()

        # ── Per-artist Discogs single-title cache (release cache) ─────────
        if artist and artist not in artist_discogs_singles_cache and not _is_compilation_artist:
            artist_discogs_singles_cache[artist] = _load_discogs_single_titles(artist)
        discogs_cached_singles = artist_discogs_singles_cache.get(artist) or set()

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

        # ── Bulk popularity cache prefetch — once per ARTIST ──────────────
        # Pulls Last.fm (artist.getTopTracks) and ListenBrainz (batches) for
        # the artist's ENTIRE catalogue into track_popularity_cache with a
        # handful of API calls, so the per-track loop makes no popularity
        # calls.  All albums of the same artist reuse the same map, and
        # subsequent scans make ZERO calls (fresh cache rows are reused).
        # Forced scans always recheck.
        #
        # Skip prefetch for compilation/Various Artists albums — the "artist"
        # is not a real artist, so prefetching would waste API calls on
        # irrelevant data. Track-level lookups will still work per-track.
        track_dicts = [tc["track"] for tc in track_contexts if tc.get("track")]
        _is_compilation_artist = artist.lower() in (
            "various artists", "various artists -", "various", 
            "compilation", "soundtrack"
        )
        
        if artist and artist != last_prefetch_artist and not _is_compilation_artist:
            last_prefetch_artist = artist
            prefetched_popularity = {}
            try:
                from services.popularity.popularity_cache_service import prefetch_artist_popularity
                prefetched_popularity = prefetch_artist_popularity(
                    artist=artist,
                    tracks=artist_all_tracks.get(artist) or track_dicts,
                    force=bool(options.get("force")),
                    # Album-scoped scans (no cached data for the artist yet)
                    # still persist the artist's full top-tracks catalogue in
                    # one bulk call, so later scans never need per-track calls.
                    cache_full_catalogue=True,
                )
            except Exception as exc:
                logger.warning(
                    "[scan_runner] Popularity cache prefetch failed for %s: %s (falls back to per-track lookups)",
                    artist, exc,
                )

            # ── Artist release cache (albums/EPs/singles) ────────────────
            # One MusicBrainz + one Discogs call per artist fills
            # artist_release_cache; singles detection then matches local
            # tracks against it instead of per-track API searches.
            try:
                from services.popularity.release_cache_service import prefetch_artist_releases
                _discogs_id = ""
                for _t in artist_all_tracks.get(artist) or track_dicts:
                    _discogs_id = str(_t.get("discogs_artist_id") or "").strip()
                    if _discogs_id:
                        break
                prefetch_artist_releases(artist, _discogs_id)
            except Exception as exc:
                logger.warning(
                    "[scan_runner] Release cache prefetch failed for %s: %s (single-title cache unavailable)",
                    artist, exc,
                )

            # ── Missing-releases gap detection + tracklists (cache-driven) ─
            # Compares the cached releases against the library (title + year),
            # persists gaps into missing_releases, and caches tracklists for
            # a few of them so they can be queued for download (legacy parity).
            if not _is_compilation_artist:
                try:
                    from services.popularity.release_cache_service import (
                        populate_missing_release_tracklists,
                        refresh_missing_releases_for_artist,
                    )
                    refresh_missing_releases_for_artist(artist)
                    populate_missing_release_tracklists(artist, limit=3)
                except Exception as exc:
                    logger.debug("[scan_runner] Missing-releases refresh failed for %s: %s", artist, exc)

        # ── Album-tracklist ListenBrainz fallback ───────────────────────────
        # Tracks without a resolved recording MBID get no LB data from the
        # per-MBID batch.  ListenBrainz lists albums with their tracks, so
        # pull the album's tracklist + per-track popularity (resolving the
        # release via MB search when the local tracks lack a release MBID)
        # and match the local tracks by normalized title.  The pulled rows
        # are persisted to track_popularity_cache so later scans reuse them
        # without any API calls.
        try:
            from services.popularity.popularity_sources import get_listenbrainz_album_tracklist
            _missing_lb_tracks = [
                t for t in track_dicts
                if t.get("title")
                and not (prefetched_popularity.get(str(t["title"]).strip().lower()) or {}).get("listenbrainz_listens")
            ]
            if _missing_lb_tracks or bool(options.get("force")):
                _album_lb_by_title = get_listenbrainz_album_tracklist(artist, album, track_dicts) or {}
                _cache_rows: list[dict[str, Any]] = []
                # Apply the album values to ALL of the album's tracks — the
                # album tracklist is authoritative for per-track counts (it
                # matches the ListenBrainz album page), so it overrides any
                # cached value that was resolved from a different recording.
                for _t in track_dicts:
                    if not _t.get("title"):
                        continue
                    _key = str(_t["title"]).strip().lower()
                    _entry = _album_lb_by_title.get(_key)
                    if _entry and _entry.get("listenbrainz_listens"):
                        _cur = prefetched_popularity.setdefault(_key, {})
                        _cur["listenbrainz_listens"] = int(_entry["listenbrainz_listens"] or 0)
                        _cur["listenbrainz_users"] = int(_entry.get("listenbrainz_users") or 0)
                        _cur["recording_mbid"] = _entry.get("recording_mbid")
                        # Freshly fetched during THIS scan — authoritative even
                        # on forced scans (which normally bypass the cache).
                        _cur["_album_tracklist"] = True
                        _cache_rows.append({
                            "artist": artist,
                            "title": str(_t["title"]),
                            "lastfm_listeners": int(_cur.get("lastfm_listeners") or 0),
                            "lastfm_playcount": int(_cur.get("lastfm_playcount") or 0),
                            "listenbrainz_listens": _cur["listenbrainz_listens"],
                            "listenbrainz_users": _cur["listenbrainz_users"],
                            "source": "album_tracklist",
                        })
                        logger.info(
                            "[scan_runner] Album-tracklist LB match for '%s' (%s - %s): %s listens",
                            _t.get("title"), artist, album, _cur["listenbrainz_listens"],
                        )
                if _cache_rows:
                    try:
                        from db.repositories.popularity_cache import upsert_track_popularity_bulk
                        upsert_track_popularity_bulk(_cache_rows)
                    except Exception as exc:
                        logger.debug("[scan_runner] Album-tracklist cache persist failed: %s", exc)
        except Exception as exc:
            logger.debug("[scan_runner] Album-tracklist LB fallback failed for %s - %s: %s", artist, album, exc)

        # Album-level LB listen counts (percentile anchor) — only the
        # CURRENT album's tracks anchor the album percentile, even though
        # the prefetched map covers the whole artist catalogue.
        album_lb_listens: list[int] = []
        for _t in track_dicts:
            _e = (prefetched_popularity or {}).get(str(_t.get("title") or "").strip().lower()) or {}
            _tc = int(_e.get("listenbrainz_listens") or 0)
            if _tc > 0:
                album_lb_listens.append(_tc)

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
            # Tracks older than 2 years with an existing final_score skip the
            # popularity API re-fetch — their popularity is stable.  However,
            # singles detection, cover detection, genre aggregation and star
            # rating still run (legacy parity): the freeze only reuses the
            # cached popularity score, it does NOT skip the track entirely.
            # Forced scans never freeze (legacy ``if not (FORCE_RESCAN or force)``).
            if not options.get("force") and should_freeze_track(prepared_track):
                logger.debug(
                    "[scan_runner] Freezing mature track '%s' (has existing score %.1f) — running singles/cover/genre only",
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
                # Reuse the cached popularity score but still run the rest of
                # the per-track pipeline (metadata/cover/singles/genre).
                frozen_options = dict(options)
                frozen_options["frozen_track"] = True
                frozen_result = process_track(
                    track=prepared_track,
                    track_context=track_context,
                    album_context=album_context,
                    album_result=album_result,
                    options=frozen_options,
                    album_lb_listens=album_lb_listens if album_lb_listens else None,
                    artist_max_lf_listeners=artist_max_lf,
                    artist_lf_context=artist_lf_context,
                    mb_cached_singles=mb_cached_singles,
                    discogs_cached_singles=discogs_cached_singles,
                    prefetched_popularity=prefetched_popularity,
                )
                if frozen_result is not None:
                    results.append(frozen_result)
                tracks_processed += 1
                continue

            track_result = process_track(
                track=prepared_track,
                track_context=track_context,
                album_context=album_context,
                album_result=album_result,
                options=options,
                album_lb_listens=album_lb_listens if album_lb_listens else None,
                artist_max_lf_listeners=artist_max_lf,
                artist_lf_context=artist_lf_context,
                mb_cached_singles=mb_cached_singles,
                discogs_cached_singles=discogs_cached_singles,
                prefetched_popularity=prefetched_popularity,
            )

            if track_result is not None:
                results.append(track_result)

                # Per-track score logging so the dashboard unified log shows
                # exactly how each track was scored (SP / LF / LB / final).
                if isinstance(track_result, dict):
                    title = prepared_track.get("title", "Unknown Track")
                    f_score = track_result.get("popularity_score")
                    sp = track_result.get("spotify_score")
                    lf = track_result.get("lastfm_score")
                    lb = track_result.get("listenbrainz_score")
                    logger.info(
                        "[TRACK_RESULT] '%s' -> Final: %.1f (SP: %.1f | LF: %.1f | LB: %.1f)",
                        title,
                        float(f_score or 0.0),
                        float(sp or 0.0),
                        float(lf or 0.0),
                        float(lb or 0.0),
                    )

            tracks_processed += 1

        # Record completion for this album scan
        try:
            record_scan(scan_type, "completed", message=f"{scan_type} scan: {artist} - {album}", artist=artist, album=album)
        except Exception:
            pass  # Non-critical — dashboard data is best-effort

        albums_processed += 1

    # Nothing was processed (all albums skipped): surface it so the missing
    # finalise output is explainable, not silent.
    if tracks_processed == 0:
        log_unified(
            "Popularity Scan - All albums were skipped (recently scanned or up to "
            "date). Run in Forced mode to rescan."
        )

    update(stage="finalising", progress=98, message="Finalising popularity scan...", processed=total_albums, total_items=total_albums)

    # Star-rating assignment, Navidrome rating sync and NSP playlist creation
    # only run on full / singles passes (legacy parity).  Metadata-only and
    # popularity-only passes must NOT assign star ratings — scores/singles
    # haven't been computed yet, so every track would incorrectly get 1★.
    if not metadata_only and not popularity_only:
        finalise_scan(results=results, options=options)

    update(stage="complete", progress=100, message="Popularity scan complete.", processed=total_albums, total_items=total_albums)

    finish(success=True)

    return {
        "success": True,
        "albums_processed": albums_processed,
        "albums_skipped": skipped_albums,
        "tracks_processed": tracks_processed,
    }
