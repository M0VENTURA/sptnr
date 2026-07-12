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
    # 1. METADATA - MusicBrainz (via updated api_clients)
    # -------------------------------------------------------------------------

    if not popularity_only:
        try:
            title = _as_str(track.get("title"))
            artist = _as_str(track.get("artist"))

            if title and artist:
                mb = MusicBrainzHttpClient()
                results = mb.search_recordings(f'artist:"{artist}" AND recording:"{title}"', limit=3)

                if results:
                    # Use the top result for metadata merge
                    recording = results[0]
                    update_payload["mbid"] = recording.get("id", "")
                    update_payload["recording_mbid"] = recording.get("id", "")
                    update_payload["musicbrainz_confidence"] = 0.9

                    if recording.get("title"):
                        update_payload["musicbrainz_title"] = recording["title"]
                    if recording.get("length"):
                        import math
                        update_payload["duration"] = int(recording["length"]) / 1000 if recording["length"] else None
                    if recording.get("first-release-date"):
                        update_payload["release_date"] = recording["first-release-date"]

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
    # 5. PERSISTENCE
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
    # 6. RETURN RESULT
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