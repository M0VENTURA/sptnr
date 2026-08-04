"""
Per-track popularity/enrichment stage.

This is the ONLY place that connects:
- enrichment external APIs
- popularity scoring
- single detection
- persistence

Uses the updated ``api_clients`` classes directly for lookups.
"""

from __future__ import annotations

import logging
from typing import Any

# API clients (updated versions)
from api_clients.musicbrainz_http import MusicBrainzHttpClient
from api_clients.lastfm import LastFmClient
from api_clients.listenbrainz import ListenBrainzClient

# Enrichment services (better metadata than raw API clients)
from services.enrichment.musicbrainz_service import MusicBrainzService

# Popularity
from services.popularity.popularity_math import (
    calculate_combined_popularity_score,
    calculate_listenbrainz_percentile,
)
from services.popularity.popularity_config import (
    LASTFM_WEIGHT,
    get_live_weight_penalty,
    get_metadata_score_floor,
    get_single_boost,
)

# Provider aggregation helpers (split-variant merging, cross-release lookups)
from services.popularity.popularity_sources import (
    get_aggregated_lastfm_popularity,
    get_aggregated_listenbrainz_popularity,
)

# Detection
from services.enrichment.single_detection_service import (
    detect_single_for_track,
)

# Cover detection
from services.enrichment.cover_detection_service import (
    detect_cover_song,
)

# Genre aggregation
from services.enrichment.genre_aggregation_service import (
    aggregate_genres,
)

# DB
from db.repositories.tracks import (
    insert_or_update_track,
)
from helpers.normalization_service import safe_int, safe_str

# Re-fetch threshold provider — returns hours based on track release age.
from services.popularity.popularity_cache_policy import (
    get_cache_duration_hours,
    should_use_cached_score,
)

# Score adjustments (artist-context and album-deviation)
from services.popularity.popularity_adjustments import (
    apply_mean_popularity_adjustment,
    apply_album_deviation_adjustment,
)

logger = logging.getLogger(__name__)


_as_str = safe_str
_as_int = safe_int


def _build_effective_track(
    track: dict[str, Any],
    update_payload: dict[str, Any],
) -> dict[str, Any]:
    effective_track = dict(track)
    effective_track.update(update_payload)
    return effective_track


def process_track(
    *,
    track: dict[str, Any],
    track_context: dict[str, Any],
    album_context: dict[str, Any],
    album_result: dict[str, Any],
    options: dict[str, Any],
    album_lb_data: dict[str, dict[str, int | None]] | None = None,
    album_lb_listens: list[int] | None = None,
    artist_max_lf_listeners: int = 0,
    artist_lf_context: dict[str, Any] | None = None,
    mb_cached_singles: set | None = None,
    discogs_cached_singles: set | None = None,
    prefetched_popularity: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:

    raw_track_id = track.get("id")
    if not raw_track_id:
        return None

    track_id = _as_str(raw_track_id)
    track_title = _as_str(track.get("title"))
    track_artist = _as_str(track.get("artist"))
    from helpers.logging_config import log_unified
    log_unified(f"[TRACK_STAGE] {track_artist} - {track_title}")
    logger.debug("[TRACK_STAGE] Processing track: %s - %s (%s)", track_artist, track_title, track_id)

    metadata_only = bool(options.get("metadata_only"))
    popularity_only = bool(options.get("popularity_only"))
    frozen_track = bool(options.get("frozen_track"))

    update_payload: dict[str, Any] = {}
    score_data: dict[str, Any] = {}
    lb_percentile: float = 0.0
    lastfm_listeners: int = 0
    listenbrainz_listens: int = 0

    # -------------------------------------------------------------------------
    # 1. METADATA - MusicBrainz (via enrichment service for better matching)
    # -------------------------------------------------------------------------

    if not popularity_only and not frozen_track:
        try:
            title = _as_str(track.get("title"))
            artist = _as_str(track.get("artist"))

            if title and artist:
                # The per-track MusicBrainz lookup is the dominant per-track
                # API cost — skip it when the track already has a resolved
                # recording MBID and the scan isn't forced (metadata is
                # stable between scans).
                _has_mbid = bool(
                    _as_str(track.get("recording_mbid") or track.get("mbid") or track.get("musicbrainz_trackid"))
                )
                _has_genres = bool(
                    track.get("musicbrainz_genres") or track.get("musicbrainz_tags") or track.get("discogs_genres")
                )
                _force_meta = bool(options.get("force"))
                mb_data = None
                if _has_mbid and not _force_meta:
                    logger.debug("[track_stage] Skipping MB metadata lookup for %s (MBID already resolved)", track_id)
                else:
                    mb_service = MusicBrainzService()
                    mb_data = mb_service.lookup_recording_metadata(title, artist)

                if mb_data:
                    recording_mbid = mb_data.get("recording_mbid")
                    confidence = mb_data.get("confidence")

                    if recording_mbid:
                        update_payload["recording_mbid"] = recording_mbid
                        update_payload["mbid"] = recording_mbid
                    if confidence is not None:
                        update_payload["musicbrainz_confidence"] = confidence

                    # Writer backfill from MusicBrainz work relationships
                    # (legacy composer lookup parity) — only when missing.
                    if recording_mbid:
                        _existing_writer = _as_str(track.get("writer") or "")
                        if not _existing_writer or _existing_writer.strip().lower() in ("[]", "null", "none", ""):
                            try:
                                writers = mb_service.get_composers_for_recording(recording_mbid)
                                if writers:
                                    import json
                                    update_payload["writer"] = json.dumps(writers)
                            except Exception as exc:
                                logger.debug("[track_stage][WRITER] %s: %s", track_id, exc)
                    if mb_data.get("title"):
                        update_payload["musicbrainz_title"] = mb_data["title"]
                    if mb_data.get("album"):
                        # Use the folder name from file_path as the primary
                        # reference for album matching — it reflects the actual
                        # file structure and is more reliable than the `album`
                        # column (which may have been overwritten by a previous
                        # bad MusicBrainz match). Falls back to the existing
                        # `album` column if file_path is not available.
                        existing_album = _as_str(track.get("album") or "")
                        fp = _as_str(track.get("file_path") or "")
                        folder_name = ""
                        if fp:
                            import os as _os
                            # Extract parent folder name from file path
                            # e.g. "/music/Artist/Album/track.flac" → "Album"
                            parts = _os.path.normpath(fp).split(_os.sep)
                            if len(parts) >= 2:
                                folder_name = parts[-2]
                        primary_ref = folder_name or existing_album

                        mb_album = _as_str(mb_data["album"])
                        match_ratio = 0.0
                        if primary_ref and mb_album:
                            from difflib import SequenceMatcher
                            match_ratio = SequenceMatcher(None, primary_ref.lower(), mb_album.lower()).ratio()
                            if match_ratio >= 0.6:
                                update_payload["album"] = mb_album
                            elif folder_name and existing_album:
                                # If MB doesn't match the folder, check if old
                                # album column is closer — keep existing if so
                                old_ratio = SequenceMatcher(None, existing_album.lower(), mb_album.lower()).ratio()
                                if old_ratio >= 0.6:
                                    update_payload["album"] = mb_album
                        elif mb_album:
                            update_payload["album"] = mb_album

                        if not update_payload.get("album"):
                            logger.debug(
                                "[track_stage] Skipping album rename (folder='%s', album='%s') → '%s' (ratio=%.2f)",
                                folder_name or "?", existing_album, mb_album, match_ratio,
                            )
                    if mb_data.get("artist"):
                        update_payload["artist"] = mb_data["artist"]
                    if mb_data.get("year"):
                        update_payload["year"] = mb_data["year"]

            # Also fetch genre/tag data from MusicBrainz via genre-aware endpoint
            if title and artist and (not _has_genres or _force_meta):
                try:
                    mb_raw = MusicBrainzHttpClient()
                    recs = mb_raw.search_recordings_with_genres(
                        f'artist:"{artist.replace(chr(34), "")}" AND recording:"{title.replace(chr(34), "")}"',
                        limit=3,
                    )
                    if recs:
                        rec = recs[0]
                        mb_genres = rec.get("genres") or []
                        mb_tags = rec.get("tags") or []
                        if mb_genres:
                            update_payload["musicbrainz_genres"] = [
                                g.get("name", "") for g in mb_genres if g.get("name")
                            ]
                        if mb_tags:
                            update_payload["musicbrainz_tags"] = [
                                t.get("name", "") for t in mb_tags if t.get("name")
                            ]
                except Exception as e:
                    logger.debug("[track_stage][MB_GENRE] %s: %s", track_id, e)

            # Fetch Discogs genres for the track
            if title and artist and (not _has_genres or _force_meta):
                try:
                    from api_clients.discogs_http import DiscogsHttpClient
                    discogs = DiscogsHttpClient(token="")
                    results = discogs.search_database({
                        "q": f'{artist} {title}',
                        "type": "release",
                        "per_page": 3,
                    })
                    if results and len(results) > 0:
                        genres = results[0].get("genre", []) or []
                        styles = results[0].get("style", []) or []
                        if genres or styles:
                            update_payload["discogs_genres"] = list(set(genres + styles))
                except Exception as e:
                    logger.debug("[track_stage][DISCOGS_GENRE] %s: %s", track_id, e)

        except Exception as e:
            logger.debug("[track_stage][MB] %s: %s", track_id, e)

    # -------------------------------------------------------------------------
    # 2. POPULARITY (via updated api_clients)
    # -------------------------------------------------------------------------

    if not metadata_only:
        try:
            effective_track = _build_effective_track(track, update_payload)

            artist = _as_str(
                track_context.get("artist") or effective_track.get("artist")
            )
            title = _as_str(
                track_context.get("lastfm_title") or effective_track.get("title")
            )
            release_date = _as_str(
                effective_track.get("year") or effective_track.get("release_year")
            )
            recording_mbid = (
                effective_track.get("recording_mbid")
                or effective_track.get("mbid")
                or effective_track.get("musicbrainz_trackid")
            )

            # ── Staleness check ───────────────────────────────────────────
            # Skip API calls if fresh-enough data is already in the DB.
            # Cache duration varies by track age: older tracks change less.
            from datetime import datetime, timezone

            def _as_utc(value):
                # DB TIMESTAMP columns return NAIVE datetimes; ``now_ts`` is
                # UTC-aware.  Coerce stored values to aware-UTC so the
                # subtraction never mixes offset-naive and offset-aware.
                if isinstance(value, datetime):
                    if value.tzinfo is None:
                        return value.replace(tzinfo=timezone.utc)
                    return value.astimezone(timezone.utc)
                return None

            now_ts = datetime.now(timezone.utc)
            _track_year = effective_track.get("year") or effective_track.get("release_year")
            _cache_ttl = get_cache_duration_hours(_track_year)
            last_lf_ts = _as_utc(effective_track.get("lastfm_last_updated"))
            last_mb_ts = _as_utc(effective_track.get("musicbrainz_last_updated"))
            has_fresh_lf = (
                last_lf_ts is not None
                and (now_ts - last_lf_ts).total_seconds() < _cache_ttl * 3600
            )
            has_fresh_mb = (
                last_mb_ts is not None
                and (now_ts - last_mb_ts).total_seconds() < _cache_ttl * 3600
            )

            # ── Overall cache gate ────────────────────────────────────────
            # If the track has a fresh Spotify-style cached score AND already
            # has a valid final_score, skip all API re-fetches entirely.
            # Frozen mature tracks are ALWAYS routed through the cached path
            # so the popularity score is reused without API calls while the
            # rest of the pipeline (singles/cover/genre) still runs.
            # Forced scans bypass the cache (legacy ``if not (FORCE_RESCAN or force)``).
            # Cached scores with BOTH provider counts at zero are treated as
            # suspect (a failed fetch from a broken scan, missing key, or
            # outage) and re-fetched so scans self-heal instead of caching 0s.
            # Cached scores with only junk-level provider counts (both sources
            # below 25) are treated as suspect — a wrong-artist Last.fm match
            # or a failed fetch from an earlier scan — and re-fetched so scans
            # self-heal instead of caching garbage for the whole TTL.
            _force = bool(options.get("force"))
            _has_credible_data = (
                int(effective_track.get("lastfm_listeners") or 0) >= 25
                or int(effective_track.get("listenbrainz_listens") or 0) >= 25
            )
            # Forced scans ALWAYS recheck: bypass the cached score even for
            # frozen mature tracks (legacy ``FORCE_RESCAN`` behaviour).
            _cached = (
                not _force
                and (frozen_track or should_use_cached_score(effective_track))
            ) and bool(
                effective_track.get("final_score") and _has_credible_data
            )
            if _cached:
                logger.debug(
                    "[track_stage] Using cached score for %s (final_score=%.1f)",
                    track_id,
                    effective_track["final_score"],
                )
                lastfm_listeners = _as_int(effective_track.get("lastfm_listeners") or 0)
                lastfm_playcount = _as_int(effective_track.get("lastfm_playcount") or 0)
                listenbrainz_listens = _as_int(effective_track.get("listenbrainz_listens") or 0)
                listenbrainz_users = _as_int(effective_track.get("listenbrainz_users") or 0)
                score_data = {
                    "combined_score": float(effective_track.get("final_score", 0)),
                    "lastfm_score": float(effective_track.get("lastfm_score", 0)),
                    "listenbrainz_score": float(effective_track.get("listenbrainz_score", 0)),
                    "age_score": float(effective_track.get("age_score", 0)),
                }
                update_payload["_cached"] = True
            else:
                # --- Last.fm ---
                lastfm_listeners = _as_int(effective_track.get("lastfm_listeners") or 0)
                lastfm_playcount = _as_int(effective_track.get("lastfm_playcount") or 0)
                # Bulk-cache fast-path: when the scan prefetched artist-wide
                # popularity into track_popularity_cache, use it instead of a
                # per-track API call.  Forced scans always recheck.
                _prefetch_entry = None
                if not _force:
                    _prefetch_entry = (prefetched_popularity or {}).get(
                        str(title or "").strip().lower()
                    )
                # Fresh-but-suspect values are re-fetched so scans self-heal:
                # zero counts (failed fetch / missing key), or both sources
                # below 25 (wrong-artist match cached by an earlier scan).
                # Forced scans always re-fetch regardless of freshness.
                if (
                    _force
                    or not has_fresh_lf
                    or lastfm_listeners == 0
                    or (lastfm_listeners < 25 and listenbrainz_listens < 25)
                ):
                    if _prefetch_entry and _prefetch_entry.get("lastfm_listeners"):
                        lastfm_listeners = _as_int(_prefetch_entry.get("lastfm_listeners") or 0)
                        lastfm_playcount = _as_int(_prefetch_entry.get("lastfm_playcount") or 0)
                        update_payload["lastfm_listeners"] = lastfm_listeners
                        update_payload["lastfm_playcount"] = lastfm_playcount
                        update_payload["lastfm_last_updated"] = now_ts
                        update_payload["_from_prefetch"] = True
                    else:
                        try:
                            from helpers.config_helpers import get_config
                            _lf_cfg = get_config().get("api_integrations", {}).get("lastfm", {})
                            _lf_api_key = _lf_cfg.get("api_key", "")
                            if _lf_api_key:
                                lf = LastFmClient(_lf_api_key)
                                # Prefer the aggregated fetch which merges split
                                # Last.fm variants ("Song" vs "Song (Radio Edit)")
                                # and falls back to a single track.getInfo lookup.
                                agg = get_aggregated_lastfm_popularity(artist, title, lastfm_client=lf)
                                if agg and (agg.get("listeners") or 0) > 0:
                                    lastfm_listeners = _as_int(agg.get("listeners") or 0)
                                    lastfm_playcount = _as_int(agg.get("track_play") or agg.get("playcount") or 0)
                                    lf_result = {}
                                else:
                                    lf_result = lf.get_track_info(artist, title)
                                    lastfm_listeners = _as_int(lf_result.get("listeners") if isinstance(lf_result, dict) else 0)
                                    lastfm_playcount = _as_int(lf_result.get("track_play") if isinstance(lf_result, dict) else 0)
                                update_payload["lastfm_listeners"] = lastfm_listeners
                                update_payload["lastfm_playcount"] = lastfm_playcount
                                update_payload["lastfm_last_updated"] = now_ts
                                toptags = lf_result.get("toptags", {}) if isinstance(lf_result, dict) else {}
                                tag_list = toptags.get("tag", []) if isinstance(toptags, dict) else []
                                if tag_list:
                                    import json
                                    update_payload["lastfm_tags"] = json.dumps(
                                        [t.get("name", "") for t in tag_list if isinstance(t, dict) and t.get("name")]
                                    )
                            else:
                                lastfm_listeners = 0
                                lastfm_playcount = 0
                        except Exception:
                            lastfm_listeners = 0
                            lastfm_playcount = 0

                # --- ListenBrainz ---
                listenbrainz_listens = _as_int(effective_track.get("listenbrainz_listens") or 0)
                listenbrainz_users = _as_int(effective_track.get("listenbrainz_users") or 0)
                last_lb_ts = _as_utc(effective_track.get("listenbrainz_last_updated"))
                has_fresh_lb = (
                    last_lb_ts is not None
                    and (now_ts - last_lb_ts).total_seconds() < _cache_ttl * 3600
                )
                # Fresh-but-zero is suspect (broken prior scan): re-fetch.
                # Forced scans always re-fetch regardless of freshness.
                if _force or not has_fresh_lb or listenbrainz_listens == 0:
                    # Bulk-cache fast-path first: prefetched artist-wide data
                    # from track_popularity_cache (non-forced scans only).
                    if _prefetch_entry and _prefetch_entry.get("listenbrainz_listens"):
                        listenbrainz_listens = _as_int(_prefetch_entry.get("listenbrainz_listens") or 0)
                        listenbrainz_users = _as_int(_prefetch_entry.get("listenbrainz_users") or 0)
                    else:
                        if album_lb_data and recording_mbid and recording_mbid in album_lb_data:
                            lb_entry = album_lb_data[recording_mbid]
                            if lb_entry:
                                listenbrainz_listens = _as_int(lb_entry.get("total_listen_count") or 0)
                                listenbrainz_users = _as_int(lb_entry.get("total_user_count") or 0)
                        if listenbrainz_listens == 0 and recording_mbid:
                            try:
                                lb = ListenBrainzClient()
                                lb_result = lb.get_recording_popularity(recording_mbid) if recording_mbid else {}
                                listenbrainz_listens = _as_int(lb_result.get("listen_count") if isinstance(lb_result, dict) else 0)
                                listenbrainz_users = _as_int(lb_result.get("user_count") if isinstance(lb_result, dict) else 0)
                            except Exception:
                                listenbrainz_listens = 0
                                listenbrainz_users = 0
                    update_payload["listenbrainz_listens"] = listenbrainz_listens
                    update_payload["listenbrainz_users"] = listenbrainz_users
                    update_payload["listenbrainz_last_updated"] = now_ts

                # ── Cross-release ListenBrainz aggregation ─────────────────
                # When Last.fm is low/unreliable, search for ALL recordings of
                # this track by the same artist (single vs album version) and
                # aggregate their ListenBrainz popularity — mirrors the legacy
                # scanner behaviour and rescues tracks whose MBID is split.
                # Also run when the title/artist carries a "feat." marker —
                # those variants split listener counts most aggressively.
                _feat_variant = (
                    "feat" in str(title or "").lower()
                    or "feat" in str(artist or "").lower()
                )
                if (lastfm_listeners < 20 or _feat_variant) and recording_mbid and title and artist:
                    try:
                        agg_lb = get_aggregated_listenbrainz_popularity(
                            title=title,
                            artist=artist,
                            primary_mbid=recording_mbid,
                        )
                        agg_total = _as_int((agg_lb or {}).get("total_listen_count") or 0)
                        if agg_total > listenbrainz_listens:
                            listenbrainz_listens = agg_total
                            update_payload["listenbrainz_listens"] = listenbrainz_listens
                            logger.debug("[track_stage] Cross-release LB boost for %s: %s listens", track_id, agg_total)
                    except Exception as exc:
                        logger.debug("[track_stage] Cross-release LB aggregation failed for %s: %s", track_id, exc)

                is_live_flag = bool(
                    effective_track.get("is_live")
                    or effective_track.get("album_context_live")
                    or album_context.get("is_live_album")
                )
                is_featured_flag = bool(
                    "feat" in str(artist or "").lower()
                    or "feat" in str(title or "").lower()
                )
                has_mb_meta = bool(recording_mbid)
                prior_single = bool(effective_track.get("is_single"))

                # Dynamic Last.fm weight from artist listener context (legacy
                # parity): boosts the Last.fm weight for catalogue outliers and
                # reduces it for underperformers.
                lastfm_weight_override = None
                if artist_lf_context and (artist_lf_context.get("total") or 0) > 0 and lastfm_listeners > 0:
                    try:
                        from services.enrichment.single_detection_context_service import get_dynamic_lastfm_weight
                        lastfm_weight_override = get_dynamic_lastfm_weight(
                            artist_lf_context,
                            int(lastfm_listeners or 0),
                            LASTFM_WEIGHT,
                        )
                    except Exception as exc:
                        logger.debug("[track_stage] Dynamic LF weight failed for %s: %s", track_id, exc)

                # Adjustable scoring knobs from config (single_detection section).
                try:
                    cfg_single_boost = get_single_boost()
                    cfg_floor = get_metadata_score_floor()
                    cfg_live_penalty = get_live_weight_penalty()
                except Exception:
                    cfg_single_boost, cfg_floor, cfg_live_penalty = 1.15, 5.0, 0.5

                # Album Last.fm listener distribution (legacy parity): score
                # Last.fm against the album's own listener spread so the track
                # with the most listeners ranks accordingly even when the
                # artist has a bigger hit elsewhere.  The distribution comes
                # from the bulk artist cache (artist.getTopTracks).
                _album_lf_listeners = None
                try:
                    _lf_vals = [
                        int(e.get("lastfm_listeners") or 0)
                        for e in (prefetched_popularity or {}).values()
                        if int(e.get("lastfm_listeners") or 0) > 0
                    ]
                    if len(_lf_vals) >= 2:
                        _album_lf_listeners = _lf_vals
                except Exception:
                    _album_lf_listeners = None

                score_data = calculate_combined_popularity_score(
                    lastfm_listeners=lastfm_listeners,
                    lastfm_artist_max_listeners=artist_max_lf_listeners,
                    listenbrainz_listens=listenbrainz_listens,
                    album_lb_listens=album_lb_listens,
                    album_lf_listeners=_album_lf_listeners,
                    age_source_value=listenbrainz_listens,
                    release_date=release_date,
                    is_single=prior_single,
                    has_metadata=has_mb_meta,
                    is_featured_track=is_featured_flag,
                    is_live_track=is_live_flag,
                    lastfm_weight_override=lastfm_weight_override,
                    single_boost=cfg_single_boost,
                    metadata_score_floor=cfg_floor,
                    live_weight_penalty=cfg_live_penalty,
                )

            # LB percentile within the album (used by star-rating rescue path)
            try:
                lb_percentile = calculate_listenbrainz_percentile(listenbrainz_listens, album_lb_listens) if album_lb_listens else 0.0
            except Exception:
                lb_percentile = 0.0

            # Apply score_data (whether cached or freshly computed)
            update_payload.update(score_data)

            # Map combined_score → final_score so it persists to the DB.
            combined = score_data.get("combined_score", 0.0)
            update_payload["final_score"] = combined
            update_payload["popularity"] = combined

            # ── Score adjustments ─────────────────────────────────────────
            # 1. Artist-context adjustment (median+MAD z-score + pre-2005 decay)
            if combined > 0:
                _adjusted = apply_mean_popularity_adjustment(
                    track_popularity=combined,
                    artist_name=artist,
                    release_year=_as_int(
                        effective_track.get("year") or effective_track.get("release_year")
                    ) or None,
                )
                if _adjusted != combined:
                    update_payload["final_score"] = _adjusted
                    update_payload["popularity"] = _adjusted
                    update_payload["popularity_adjusted"] = True
                    combined = _adjusted

                # 2. Album-deviation adjustment (standout within album)
                _album_adjusted = apply_album_deviation_adjustment(
                    track_popularity=combined,
                    artist_name=artist,
                    album_name=_as_str(
                        album_context.get("album") or track.get("album") or ""
                    ),
                    artist_mean_popularity=None,  # computed internally
                )
                if _album_adjusted != combined:
                    update_payload["final_score"] = _album_adjusted
                    update_payload["popularity"] = _album_adjusted
                    update_payload["album_deviation_adjusted"] = True

        except Exception as e:
            # Surface scoring failures at WARNING so a scan that ends with all
            # zero scores (→ 1★ for everything) is diagnosable in the unified log.
            logger.warning("[track_stage][SCORING] %s: %s", track_id, e, exc_info=True)

        # ── Per-track step summary (scanning log) ─────────────────────────
        # One line per track showing whether popularity came from cache or a
        # fresh fetch, the provider counts, and the resulting score.
        try:
            _final_score = float(update_payload.get("final_score") or 0)
            if update_payload.get("_cached"):
                _src = "cached"
            elif update_payload.get("_from_prefetch"):
                _src = "prefetched"
            else:
                _src = "fresh"
            log_unified(
                f"[TRACK_STAGE] {track_artist} - {track_title} → popularity {_src} "
                f"score={_final_score:.1f} "
                f"(LF={lastfm_listeners:,} listeners, LB={listenbrainz_listens:,} listens)"
            )
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # 3. COVER DETECTION (via enrichment service)
    # -------------------------------------------------------------------------

    if not popularity_only:
        try:
            title = _as_str(effective_track.get("title") or track.get("title") or "")
            if title:
                # Pass existing DB cover state so already-confirmed covers
                # are skipped on subsequent scans (unless the scan options
                # indicate a forced re-check).
                raw_track = track_context.get("track", {}) if isinstance(track_context, dict) else {}
                cover_data = {
                    "is_cover": raw_track.get("is_cover") or track.get("is_cover"),
                    "original_cover_artist": raw_track.get("original_cover_artist") or "",
                    "cover_manual_override": raw_track.get("cover_manual_override") or track.get("cover_manual_override") or False,
                }
                force_cover = bool(options.get("force_cover_detection"))
                is_cover, reason = detect_cover_song(
                    title, track_artist,
                    track_data=cover_data,
                    force=force_cover,
                )
                if is_cover:
                    update_payload["is_cover"] = True
                    update_payload["is_cover_reason"] = reason
                    log_unified(f"[TRACK_STAGE] {track_artist} - {track_title} → cover detected ({reason})")
        except Exception as e:
            logger.debug("[track_stage][COVER] %s: %s", track_id, e)

    # -------------------------------------------------------------------------
    # 4. SINGLES DETECTION
    # -------------------------------------------------------------------------

    # Freshness gate: once singles detection has run for a track and the scan
    # isn't forced, reuse the stored result until the cache TTL passes — the
    # per-track Discogs/MusicBrainz searches only run again when the data is
    # stale (or the scan is forced), matching "update only if changed".
    _sd_fresh = False
    if not bool(options.get("force")):
        try:
            from datetime import datetime as _sd_dt, timezone as _sd_tz
            _sd_raw = track.get("single_detection_last_updated")
            if _sd_raw:
                _sd_ts = _sd_raw
                if isinstance(_sd_ts, str):
                    _sd_ts = _sd_dt.fromisoformat(str(_sd_ts).replace("Z", "+00:00"))
                if _sd_ts.tzinfo is None:
                    _sd_ts = _sd_ts.replace(tzinfo=_sd_tz.utc)
                _sd_ttl_hours = get_cache_duration_hours(
                    track.get("year") or track.get("release_year")
                )
                _sd_fresh = (_sd_dt.now(_sd_tz.utc) - _sd_ts).total_seconds() < _sd_ttl_hours * 3600
                if _sd_fresh:
                    logger.debug("[track_stage] Singles detection fresh for %s — skipping", track_id)
        except Exception:
            _sd_fresh = False

    if not metadata_only and not popularity_only and not _sd_fresh:
        try:
            from datetime import datetime as _dt, timezone as _tz
            sd_now = _dt.now(_tz.utc)
            effective_track = _build_effective_track(track, update_payload)
            sd_title = _as_str(effective_track.get("title") or "")
            sd_artist = _as_str(effective_track.get("artist") or "")
            sd_album = _as_str(album_context.get("album") or track.get("album") or "")
            sd_album_type = _as_str(album_result.get("detected_album_type") or options.get("album_type") or "")
            sd_popularity = float(effective_track.get("combined_score") or effective_track.get("popularity_score") or 0)

            album_track_count = len(album_context.get("tracks") or []) or 1

            # Resolve optional detection inputs the service can use as
            # corroborating evidence (Discogs token, Last.fm client).
            sd_discogs_token = ""
            try:
                import os as _os
                from helpers.config_helpers import get_config as _get_cfg
                sd_discogs_token = _os.environ.get("DISCOGS_TOKEN", "")
                if not sd_discogs_token:
                    sd_discogs_token = (_get_cfg().get("api_integrations", {}).get("discogs", {}) or {}).get("token", "") or ""
                if sd_discogs_token.lower() in ("your_discogs_token", "your_token", "placeholder"):
                    sd_discogs_token = ""
            except Exception:
                sd_discogs_token = ""
            sd_lastfm_client = None
            try:
                from helpers.config_helpers import get_config as _get_cfg
                _lf_key = (_get_cfg().get("api_integrations", {}).get("lastfm", {}) or {}).get("api_key", "") or ""
                if _lf_key:
                    sd_lastfm_client = LastFmClient(_lf_key)
            except Exception:
                sd_lastfm_client = None

            sd_result = detect_single_for_track(
                title=sd_title,
                artist=sd_artist,
                album_track_count=album_track_count,
                popularity=sd_popularity,
                album_type=sd_album_type or None,
                album=sd_album,
                isrc=effective_track.get("isrc") or None,
                duration=(
                    float(effective_track["duration"])
                    if effective_track.get("duration")
                    else None
                ),
                use_advanced_detection=True,
                persist_result=False,  # We persist via track_stage
                mb_cached_singles=mb_cached_singles,
                discogs_cached_singles=discogs_cached_singles,
                artist_mbid=(
                    effective_track.get("musicbrainz_artistid")
                    or effective_track.get("musicbrainz_artist_id")
                ),
                listenbrainz_listens=int(listenbrainz_listens or 0),
                discogs_token=sd_discogs_token or None,
                lastfm_client=sd_lastfm_client,
                mb_client=MusicBrainzHttpClient(),
            )

            if sd_result:
                import json as _json
                update_payload["is_single"] = sd_result.get("is_single", False)
                update_payload["single_confidence"] = sd_result.get("confidence", "low")
                update_payload["single_confidence_score"] = sd_result.get("confidence_score", 0.0)
                update_payload["single_status"] = sd_result.get("single_status", "none")
                update_payload["single_sources"] = _json.dumps(sd_result.get("sources", []), default=str)
                update_payload["single_detection_last_updated"] = sd_now
                log_unified(
                    f"[TRACK_STAGE] {track_artist} - {track_title} → single="
                    f"{sd_result.get('confidence', 'low')} "
                    f"(status={sd_result.get('single_status', 'none')}, "
                    f"sources={len(sd_result.get('sources') or [])})"
                )

        except Exception as e:
            logger.debug("[track_stage][SINGLE] %s: %s", track_id, e)

    # -------------------------------------------------------------------------
    # 5. GENRE AGGREGATION (using enrichment service)
    # -------------------------------------------------------------------------

    if not metadata_only and not popularity_only:
        try:
            effective_track = _build_effective_track(track, update_payload)
            source_map = {}

            for key, source_name in [
                ("musicbrainz_genres", "musicbrainz"),
                ("discogs_genres", "discogs"),
                ("lastfm_tags", "lastfm"),
                ("listenbrainz_genres", "listenbrainz"),
                ("spotify_genres", "spotify"),
            ]:
                raw = effective_track.get(key) or track.get(key) or ""
                if raw:
                    import json
                    try:
                        genres = json.loads(raw) if isinstance(raw, str) else raw
                    except Exception:
                        genres = [g.strip() for g in str(raw).split(",") if g.strip()]
                    if genres:
                        source_map[source_name] = genres

            if source_map:
                from services.enrichment.genre_aggregation_service import aggregate_genres
                aggregated = aggregate_genres(source_map)
                if aggregated:
                    update_payload["aggregated_genres"] = aggregated

        except Exception as e:
            logger.debug("[track_stage][GENRE] %s: %s", track_id, e)

    # -------------------------------------------------------------------------
    # 6. PERSISTENCE
    # -------------------------------------------------------------------------

    effective_track = _build_effective_track(track, update_payload)

    try:
        insert_or_update_track(track_id, effective_track)
    except Exception as e:
        logger.debug("[track_stage][DB] %s: %s", track_id, e)

    # -------------------------------------------------------------------------
    # 7. RETURN RESULT
    # -------------------------------------------------------------------------

    return {
        "track_id": track_id,
        "artist": track_artist,
        "album": effective_track.get("album", ""),
        "title": effective_track.get("title", ""),
        "lastfm_listeners": int(lastfm_listeners or 0),
        "listenbrainz_listens": int(listenbrainz_listens or 0),
        "lb_percentile": float(lb_percentile or 0.0),
        "popularity_score": float(score_data.get("combined_score", 0)),
        "final_score": float(score_data.get("combined_score", 0)),
        "spotify_score": float(score_data.get("spotify_score", 0)),
        "lastfm_score": float(score_data.get("lastfm_score", 0)),
        "listenbrainz_score": float(score_data.get("listenbrainz_score", 0)),
        "is_single": bool(update_payload.get("is_single", False)),
        "single_confidence": str(update_payload.get("single_confidence", "low")),
        "single_sources": update_payload.get("single_sources", ""),
        "is_live": bool(track.get("is_live") or track.get("album_context_live")),
    }