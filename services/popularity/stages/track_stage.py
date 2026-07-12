"""
Per-track popularity/enrichment stage.

This is the ONLY place that connects:
- enrichment external APIs
- popularity scoring
- single detection
- persistence
"""

from __future__ import annotations

import logging
from typing import Any

# Enrichment
from services.enrichment.musicbrainz_service import (
    lookup_recording_metadata,
    merge_metadata,
)

from services.enrichment.lastfm_service import (
    get_lastfm_track_info,
)

from services.enrichment.listenbrainz_service import (
    get_listenbrainz_score_for_recording,
)

# Popularity
from services.popularity.popularity_math import (
    calculate_combined_popularity_score,
)

# Detection
from services.enrichment.single_detection_service import (
    detect_single_for_track,
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
    """
    Return a merged track view for downstream logic.

    Original track values are preserved unless an enrichment/scoring stage
    has supplied a replacement in update_payload.
    """
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

    """
    Process one track through metadata enrichment, popularity scoring,
    single detection, and persistence.
    """

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
    # 1. METADATA - MusicBrainz
    # -------------------------------------------------------------------------

    if not popularity_only:
        try:
            title = _as_str(track.get("title"))
            artist = _as_str(track.get("artist"))

            if title and artist:
                mb_data = lookup_recording_metadata(
                    title,
                    artist,
                )

                if mb_data:
                    merged_metadata = merge_metadata(
                        track,
                        mb_data,
                    )

                    update_payload.update(merged_metadata)

                    recording_mbid = mb_data.get("recording_mbid")
                    confidence = mb_data.get("confidence")

                    if recording_mbid:
                        update_payload["recording_mbid"] = recording_mbid
                        update_payload["mbid"] = recording_mbid

                    if confidence is not None:
                        update_payload["musicbrainz_confidence"] = confidence

        except Exception as e:
            logger.debug(
                "[track_stage][MB] %s: %s",
                track_id,
                e,
            )

    # -------------------------------------------------------------------------
    # 2. POPULARITY
    # -------------------------------------------------------------------------

    if not metadata_only:
        try:
            effective_track = _build_effective_track(
                track,
                update_payload,
            )

            artist = _as_str(
                track_context.get("artist")
                or effective_track.get("artist")
            )

            title = _as_str(
                track_context.get("lastfm_title")
                or effective_track.get("title")
            )

            release_date = _as_str(
                effective_track.get("year")
                or effective_track.get("release_year")
            )

            recording_mbid = (
                effective_track.get("recording_mbid")
                or effective_track.get("mbid")
                or effective_track.get("musicbrainz_trackid")
            )

            lf = get_lastfm_track_info(
                artist,
                title,
            )

            lastfm_listeners = _as_int(
                lf.get("listeners") if isinstance(lf, dict) else 0
            )

            lb = get_listenbrainz_score_for_recording(
                _as_str(recording_mbid),
                release_date=release_date,
            )

            listenbrainz_listens = _as_int(
                lb.get("listen_count") if isinstance(lb, dict) else 0
            )

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
            logger.debug(
                "[track_stage][SCORING] %s: %s",
                track_id,
                e,
            )