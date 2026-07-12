"""ListenBrainz enrichment helpers.

Handles:
- popularity lookup
- age-adjusted scoring
"""

from __future__ import annotations

from typing import Any

from api_clients.listenbrainz import ListenBrainzClient, score_by_age
from services.popularity.popularity_sources import get_aggregated_listenbrainz_popularity
from services.popularity.popularity_matching import normalize_for_aggregation


def get_listenbrainz_score_for_recording(
    mbid: str,
    *,
    release_date: str = "",
    enabled: bool = True,
) -> dict[str, Any]:
    """Return ListenBrainz popularity with optional age weighting."""

    if not mbid:
        return {
            "recording_mbid": mbid,
            "listen_count": 0,
            "user_count": None,
            "age_score": 0,
            "days_since_release": None,
        }

    client = ListenBrainzClient(enabled=enabled)

    data = client.get_recording_popularity(mbid)

    listen_count = int(data.get("total_listen_count") or 0)

    if release_date:
        age_score, days_since = score_by_age(listen_count, release_date)
    else:
        age_score, days_since = float(listen_count), None

    return {
        "recording_mbid": mbid,
        "listen_count": listen_count,
        "user_count": data.get("total_user_count"),
        "age_score": age_score,
        "days_since_release": days_since,
    }

    