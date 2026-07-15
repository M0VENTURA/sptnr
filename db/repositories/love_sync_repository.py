"""Repository for user loved-tracks operations (Navidrome ↔ ListenBrainz sync).

Manages the ``user_loved_tracks`` table which stores per-user star/love
state for each track.  Used by the love-sync service to synchronise
between Navidrome starred items and ListenBrainz loved tracks.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, List

logger = logging.getLogger(__name__)


def ensure_user_loved_tracks_table(cursor) -> bool:
    """Create the ``user_loved_tracks`` table if it doesn't exist.

    Safe to call repeatedly (no-op after first run).
    """
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_loved_tracks (
                user_id INTEGER NOT NULL,
                track_id TEXT NOT NULL,
                is_loved INTEGER DEFAULT 0,
                loved_at TEXT,
                PRIMARY KEY (user_id, track_id)
            )
        """)
        return True
    except Exception as exc:
        logger.error("Failed to ensure user_loved_tracks table: %s", exc)
        return False


def unstar_all_for_user(cursor, user_id: int) -> None:
    """Set all tracks for a user to unloved (full-sync reset)."""
    cursor.execute(
        "UPDATE user_loved_tracks SET is_loved = 0, loved_at = NULL WHERE user_id = %s",
        (user_id,),
    )


def upsert_loved_track(cursor, user_id: int, track_id: str) -> None:
    """Insert or update a loved track for a user."""
    now = datetime.now().isoformat()
    cursor.execute(
        """INSERT INTO user_loved_tracks (user_id, track_id, is_loved, loved_at)
           VALUES (%s, %s, 1, %s)
           ON CONFLICT (user_id, track_id)
           DO UPDATE SET is_loved = 1, loved_at = %s""",
        (user_id, track_id, now, now),
    )


def get_loved_track_ids(cursor, user_id: int) -> List[str]:
    """Return list of track IDs that are loved by *user_id*."""
    cursor.execute(
        "SELECT track_id FROM user_loved_tracks WHERE user_id = %s AND is_loved = 1",
        (user_id,),
    )
    return [str(row[0]) for row in cursor.fetchall() or []]


def get_navidrome_users(cursor) -> List[dict[str, Any]]:
    """Return all users from the Navidrome users table.

    Returns list of dicts with ``id`` and ``name`` keys.
    """
    try:
        cursor.execute(
            "SELECT id, user_name AS name FROM navidrome_users ORDER BY id"
        )
        rows = cursor.fetchall() or []
        results: List[dict[str, Any]] = []
        for row in rows:
            if hasattr(row, "get"):
                results.append({"id": row.get("id", 0), "name": row.get("name", "")})
            else:
                results.append({"id": row[0], "name": row[1] if len(row) > 1 else ""})
        return results
    except Exception:
        return []


def get_track_mbid(cursor, track_id: str) -> str:
    """Return the MusicBrainz recording MBID for a local track ID."""
    try:
        cursor.execute(
            "SELECT musicbrainz_trackid FROM tracks WHERE CAST(id AS TEXT) = %s",
            (track_id,),
        )
        row = cursor.fetchone()
        if row:
            return str(row[0] if hasattr(row, "get") else row[0])
    except Exception:
        pass
    return ""
