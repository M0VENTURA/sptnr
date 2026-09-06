"""Queue matching service.

Main entry point for matching downloaded files against queue items.
Coordinates between scoring, metadata matching, and filename matching.

Key Functions:
    - match_queue_item(): Find the best Soulseek candidate for a queue item.

Architecture:
    Public entry point that delegates to:
    - ``queue_scoring`` for Soulseek candidate scoring.
    - ``queue_metadata_matcher`` for metadata-based matching.
    - ``queue_matching_helpers`` for filename fallback matching.

    No normalization logic duplication — all text processing delegated
    to ``helpers.normalization_service``.
"""

from __future__ import annotations
import logging
from typing import Any, List, Dict

logger = logging.getLogger(__name__)

from helpers.types_queue import QueueItem
from helpers.metadata_reader import read_mp3_metadata
from services.queue.queue_scoring import _score_soulseek_candidate
from services.queue.queue_metadata_matcher import _metadata_matches_queue_item
from services.queue.queue_matching_helpers import filename_matches_queue_item
from helpers.config_helpers import get_matching_thresholds
from services.enrichment.discogs_service import _parse_discogs_duration

_matching_cfg = get_matching_thresholds()
SCORE_THRESHOLD = _matching_cfg["score_threshold"]


# =============================================================================
# ✅ PUBLIC API
# =============================================================================
def _get_discogs_token() -> str:
    """
    Resolve Discogs token from environment or config.
    """

    import os
    from helpers.config_helpers import get_config

    token = os.environ.get("DISCOGS_TOKEN", "")

    if token:
        return token

    try:
        cfg = get_config()
        return (
            cfg.get("api_integrations", {})
            .get("discogs", {})
            .get("token", "")
        )
    except Exception:
        return ""


def file_matches_queue_item(file_path: str, queue_item: QueueItem) -> tuple[bool, str]:
    """
    Master matching entry point.

    Returns:
        (True/False, reason)
    """

    # Extract duration for the scoring fallback to prevent logic dropouts
    candidate_duration = None
    try:
        metadata = read_mp3_metadata(file_path) or {}
        duration_ms = metadata.get("duration_ms")
        if duration_ms:
            candidate_duration = duration_ms / 1000.0
    except Exception:
        pass

    # 1. Metadata check (highest confidence)
    metadata_result = _metadata_matches_queue_item(file_path, queue_item)

    if metadata_result is True:
        return True, "metadata"

    if metadata_result is False:
        return False, "metadata_mismatch"

    # 2. Filename fallback
    if filename_matches_queue_item(file_path, queue_item):
        return True, "filename"

    # 3. Scoring fallback (now properly passing candidate duration)
    score = _score_soulseek_candidate(file_path, queue_item, candidate_duration)

    return (score >= SCORE_THRESHOLD, "score")


def _fetch_discogs_tracks(release_id: str) -> List[Dict[str, Any]]:

    from api_clients.discogs import DiscogsClient

    token = _get_discogs_token()

    if not token:
        logger.warning("Discogs token not configured")
        return []

    try:
        client = DiscogsClient(token)
        release = client.get_release(release_id)

        if not isinstance(release, dict):
            return []

        tracklist = release.get("tracklist") or []

        tracks: List[Dict[str, Any]] = []

        for track in tracklist:
            tracks.append({
                "track_number": track.get("position"),
                "title": track.get("title", ""),
                "artist": track.get("artist", ""),
                "duration": _parse_discogs_duration(track.get("duration", "")),
                "recording_mbid": None,
            })

        return tracks

    except Exception as e:
        logger.error("[DISCOGS] %s", e, exc_info=True)
        return []
