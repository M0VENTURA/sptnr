"""ListenBrainz enrichment helpers.

Handles:
- popularity lookup
- age-adjusted scoring
"""

from __future__ import annotations

import threading
from typing import Any

import structlog

from api_clients.listenbrainz import ListenBrainzClient, score_by_age

logger = structlog.get_logger(__name__)

# =============================================================================
# THREAD-SAFE CLIENT SINGLETON
# =============================================================================

_SHARED_LB_CLIENT: ListenBrainzClient | None = None
_INIT_LOCK = threading.Lock()


def get_shared_lb_client(enabled: bool = True) -> ListenBrainzClient:
    """Return the process-wide shared ListenBrainzClient singleton."""
    global _SHARED_LB_CLIENT
    if _SHARED_LB_CLIENT is None:
        with _INIT_LOCK:
            if _SHARED_LB_CLIENT is None:
                _SHARED_LB_CLIENT = ListenBrainzClient(enabled=enabled)
    return _SHARED_LB_CLIENT


# =============================================================================
# MAIN SERVICE
# =============================================================================

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

    # ✅ Use the shared client to respect connection pooling and global rate limits
    client = get_shared_lb_client(enabled=enabled)

    try:
        data = client.get_recording_popularity(mbid)
    except Exception as exc:
        logger.debug("ListenBrainz popularity fetch failed", recording_mbid=mbid, error=str(exc))
        data = {}

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
