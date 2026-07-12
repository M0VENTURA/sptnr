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
from api_clients.lastfm_http import LastFmHttpClient
from api_clients.listenbrainz import ListenBrainzClient

# Enrichment services (better metadata than raw API clients)
from services.enrichment.musicbrainz_service import MusicBrainzService

# Popularity
from services.popularity.popularity_math import (
    calculate_combined_popularity_score,
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
    update_track_single_status,
)
from helpers.normalization_service import safe_int, safe_str

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
) -> dict[str, Any] | None:

    raw_track_id = track.get("id")
    if not raw_track_id:
        return None

    track_id = _as_str(raw_track_id)
    track_title = _as_str(track.get("title"))
    track_artist = _as_str(track.get("artist"))
    logger.debug("[TRACK_STAGE] Processing track: %s - %s (%s)", track_artist, track_title, track_id)

    metadata_only = bool(options.get("metadata_only"))
    popularity_only = bool(options.get("popularity_only"))

    update_payload: dict[str, Any] = {}
    score_data: dict[str, Any] = {}

    # -------------------------------------------------------------------------
    # 1. METADATA - MusicBrainz (via enrichment service for better matching)
    # -------------------------------------------------------------------------

    if not popularity_only:
        try:
            title = _as_str(track.get("title"))
            artist = _as_str(track.get("artist"))

            if title and artist:
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
                    if mb_data.get("title"):
                        update_payload["musicbrainz_title"] = mb_data["title"]
                    if mb_data.get("album"):
                        update_payload["album"] = mb_data["album"]
                    if mb_data.get("artist"):
                        update_payload["artist"] = mb_data["artist"]
                    if mb_data.get("year"):
                        update_payload["year"] = mb_data["year"]

            # Also fetch genre/tag data from MusicBrainz via genre-aware endpoint
            if title and artist:
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
            if title and artist:
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

            # Use Last.fm HTTP client for track info
            try:
                lf = LastFmHttpClient()
                lf_result = lf.get_track_info(artist, title)
                lastfm_listeners = _as_int(lf_result.get("listeners") if isinstance(lf_result, dict) else 0)
            except Exception:
                lastfm_listeners = 0

            # Use ListenBrainz client for recording score
            try:
                lb = ListenBrainzClient()
                lb_result = lb.get_recording_popularity(recording_mbid) if recording_mbid else {}
                listenbrainz_listens = _as_int(lb_result.get("listen_count") if isinstance(lb_result, dict) else 0)
            except Exception:
                listenbrainz_listens = 0

            score_data = calculate_combined_popularity_score(
                lastfm_listeners=lastfm_listeners,
                lastfm_artist_max_listeners=0,
                listenbrainz_listens=listenbrainz_listens,
                album_lb_listens=None,
                age_source_value=listenbrainz_listens,
                release_date=release_date,
            )
            update_payload.update(score_data)

        except Exception as e:
            logger.debug("[track_stage][SCORING] %s: %s", track_id, e)

    # -------------------------------------------------------------------------
    # 3. COVER DETECTION (via enrichment service)
    # -------------------------------------------------------------------------

    if not popularity_only:
        try:
            title = _as_str(effective_track.get("title") or track.get("title") or "")
            if title:
                is_cover, reason = detect_cover_song(title, track_artist)
                if is_cover:
                    update_payload["is_cover"] = True
                    update_payload["is_cover_reason"] = reason
        except Exception as e:
            logger.debug("[track_stage][COVER] %s: %s", track_id, e)

    # -------------------------------------------------------------------------
    # 4. SINGLES DETECTION
    # -------------------------------------------------------------------------

    if not metadata_only and not popularity_only:
        try:
            effective_track = _build_effective_track(track, update_payload)
            sd_title = _as_str(effective_track.get("title") or "")
            sd_artist = _as_str(effective_track.get("artist") or "")
            sd_album = _as_str(album_context.get("album") or track.get("album") or "")
            sd_album_type = _as_str(album_result.get("detected_album_type") or options.get("album_type") or "")
            sd_popularity = float(effective_track.get("combined_score") or effective_track.get("popularity_score") or 0)

            album_track_count = len(album_context.get("tracks") or []) or 1

            sd_result = detect_single_for_track(
                title=sd_title,
                artist=sd_artist,
                album_track_count=album_track_count,
                popularity=sd_popularity,
                album_type=sd_album_type or None,
                album=sd_album,
                use_advanced_detection=True,
                persist_result=False,  # We persist via track_stage
            )

            if sd_result:
                update_payload["is_single"] = sd_result.get("is_single", False)
                update_payload["single_confidence"] = sd_result.get("confidence", "low")
                update_payload["single_sources"] = sd_result.get("sources", [])

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

    # Persist single detection status separately
    if "is_single" in update_payload:
        try:
            update_track_single_status(
                track_id,
                bool(update_payload["is_single"]),
                str(update_payload.get("single_confidence", "low")),
            )
        except Exception as e:
            logger.debug("[track_stage][SINGLE_DB] %s: %s", track_id, e)

    # -------------------------------------------------------------------------
    # 7. RETURN RESULT
    # -------------------------------------------------------------------------

    return {
        "track_id": track_id,
        "artist": track_artist,
        "album": effective_track.get("album", ""),
        "title": effective_track.get("title", ""),
        "lastfm_listeners": int(score_data.get("lastfm_score", 0)),
        "listenbrainz_listens": int(score_data.get("listenbrainz_score", 0)),
        "popularity_score": float(score_data.get("combined_score", 0)),
        "is_single": bool(update_payload.get("is_single", False)),
        "single_confidence": str(update_payload.get("single_confidence", "low")),
        "is_live": bool(track.get("is_live") or track.get("album_context_live")),
    }