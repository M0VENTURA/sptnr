"""Repository for user loved-tracks operations (Navidrome ↔ ListenBrainz sync).

Manages the ``user_loved_tracks`` table which stores per-user star/love
state for each track.  Used by the love-sync service to synchronise
between Navidrome starred items and ListenBrainz loved tracks.

All functions take a SQLAlchemy ``session`` (from ``db_session()``) and use
``text()`` with named binds — the caller owns commit/rollback.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, List

from sqlalchemy import text

logger = logging.getLogger(__name__)


def ensure_user_loved_tracks_table(session) -> bool:
    """Create the ``user_loved_tracks`` table if it doesn't exist.

    Safe to call repeatedly (no-op after first run).
    """
    try:
        session.execute(text("""
            CREATE TABLE IF NOT EXISTS user_loved_tracks (
                user_id INTEGER NOT NULL,
                track_id TEXT NOT NULL,
                is_loved INTEGER DEFAULT 0,
                loved_at TEXT,
                PRIMARY KEY (user_id, track_id)
            )
        """))
        return True
    except Exception as exc:
        logger.error("Failed to ensure user_loved_tracks table: %s", exc)
        return False


def unstar_all_for_user(session, user_id: int) -> None:
    """Set all tracks for a user to unloved (full-sync reset)."""
    session.execute(
        text("UPDATE user_loved_tracks SET is_loved = 0, loved_at = NULL WHERE user_id = :user_id"),
        {"user_id": user_id},
    )


def upsert_loved_track(session, user_id: int, track_id: str) -> None:
    """Insert or update a loved track for a user."""
    now = datetime.now().isoformat()
    session.execute(
        text("""
            INSERT INTO user_loved_tracks (user_id, track_id, is_loved, loved_at)
            VALUES (:user_id, :track_id, 1, :now)
            ON CONFLICT (user_id, track_id)
            DO UPDATE SET is_loved = 1, loved_at = :now
        """),
        {"user_id": user_id, "track_id": track_id, "now": now},
    )


def get_loved_track_ids(session, user_id: int) -> List[str]:
    """Return list of track IDs that are loved by *user_id*."""
    result = session.execute(
        text("SELECT track_id FROM user_loved_tracks WHERE user_id = :user_id AND is_loved = 1"),
        {"user_id": user_id},
    )
    return [str(row[0]) for row in result.fetchall() or []]


def get_navidrome_users(session) -> List[dict[str, Any]]:
    """Return all users from the Navidrome users table.

    Returns list of dicts with ``id`` and ``name`` keys.
    """
    try:
        result = session.execute(text("SELECT id, user_name AS name FROM navidrome_users ORDER BY id"))
        rows = result.fetchall() or []
        results: List[dict[str, Any]] = []
        for row in rows:
            mapping = row._mapping if hasattr(row, "_mapping") else row
            results.append({"id": mapping.get("id", 0), "name": mapping.get("name", "")})
        return results
    except Exception:
        return []


def get_track_mbid(session, track_id: str) -> str:
    """Return the MusicBrainz recording MBID for a local track ID."""
    try:
        result = session.execute(
            text("SELECT musicbrainz_trackid FROM tracks WHERE CAST(id AS TEXT) = :track_id"),
            {"track_id": track_id},
        )
        row = result.fetchone()
        if row:
            mapping = row._mapping if hasattr(row, "_mapping") else row
            return str(mapping.get("musicbrainz_trackid") or "")
    except Exception:
        pass
    return ""
