"""
Love Sync Service — synchronise loved/starred tracks between Navidrome and ListenBrainz.

Bidirectional sync:
  Navidrome ──get_starred──▶ user_loved_tracks table
  user_loved_tracks ──▶ ListenBrainz feedback API (love_track / unlove_track)

Usage:
    from services.love_sync_service import sync_all_users
    result = sync_all_users()
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from api_clients.navidrome import NavidromeClient
from api_clients.listenbrainz import ListenBrainzUserClient
from db.repositories.love_sync_repository import (
    ensure_user_loved_tracks_table,
    get_loved_track_ids,
    get_navidrome_users,
    get_track_mbid,
    unstar_all_for_user,
    upsert_loved_track,
)
from db.utils import get_db_connection

logger = logging.getLogger(__name__)


def sync_all_users(navidrome_client: Optional[NavidromeClient] = None) -> Dict[str, Any]:
    """Sync loved tracks for all Navidrome users.

    For each user with a ListenBrainz token in the DB:
    1. Fetch starred tracks from Navidrome
    2. Update ``user_loved_tracks`` table (full-sync approach)
    3. For each newly-loved track, push ``love_track`` to ListenBrainz

    Args:
        navidrome_client: Reusable Navidrome client. Created fresh if None.

    Returns:
        Dict with per-user sync results.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    ensure_user_loved_tracks_table(cursor)
    conn.commit()

    nd_client = navidrome_client or NavidromeClient()
    results: Dict[str, Any] = {"users": [], "total_loved": 0, "total_unloved": 0}

    users = get_navidrome_users(cursor)
    if not users:
        logger.info("Love sync: no Navidrome users found")
        conn.close()
        return {**results, "error": "no_users"}

    for user in users:
        user_id = int(user.get("id", 0))
        user_name = str(user.get("name", ""))

        if not user_id:
            continue

        try:
            # 1. Get user's ListenBrainz token
            lb_token = _get_listenbrainz_token(cursor, user_id)

            # 2. Get starred tracks from Navidrome
            starred = nd_client.get_starred_items() or {}
            starred_songs = starred.get("song", []) if isinstance(starred, dict) else []
            starred_ids = [s.get("id", "") for s in starred_songs if s.get("id")]

            # 3. Full-sync: unstar all, then star current
            unstar_all_for_user(cursor, user_id)
            loved_count = 0
            for track_id in starred_ids:
                upsert_loved_track(cursor, user_id, track_id)
                loved_count += 1
                # Push to ListenBrainz if token available and track has MBID
                if lb_token:
                    _push_love_to_listenbrainz(cursor, lb_token, track_id)

            conn.commit()
            logger.info("Love sync for user '%s': %d tracks loved", user_name, loved_count)
            results["users"].append({
                "name": user_name,
                "loved": loved_count,
                "listeningbrainz_token": bool(lb_token),
            })
            results["total_loved"] += loved_count
        except Exception as exc:
            logger.error("Love sync failed for user '%s': %s", user_name, exc)
            try:
                conn.rollback()
            except Exception:
                pass
            results["users"].append({"name": user_name, "error": str(exc)})

    conn.close()
    return results


def _get_listenbrainz_token(cursor, user_id: int) -> Optional[str]:
    """Fetch the ListenBrainz user token for a Navidrome user."""
    try:
        cursor.execute(
            "SELECT listenbrainz_token FROM navidrome_users WHERE id = %s",
            (user_id,),
        )
        row = cursor.fetchone()
        if row:
            raw = str(row[0] if hasattr(row, "get") else row[0]).strip()
            return raw if raw else None
    except Exception:
        pass
    return None


def _push_love_to_listenbrainz(cursor, token: str, track_id: str) -> None:
    """Attempt to push a love for *track_id* to ListenBrainz via its MBID."""
    mbid = get_track_mbid(cursor, track_id)
    if not mbid:
        logger.debug("Love sync: no MBID for track %s, skipping ListenBrainz push", track_id)
        return
    try:
        lb = ListenBrainzUserClient(user_token=token)
        lb.love_track(mbid)
    except Exception as exc:
        logger.debug("Love sync: failed to push love to ListenBrainz for %s: %s", track_id, exc)
