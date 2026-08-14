"""Per-user favourites repository (Navidrome star sync).

Stores the favourite (heart) state per Navidrome user for tracks, albums and
artists in the ``user_favourites`` table.  The active user is scoped by
``username`` so one user's hearts never leak into another user's view.

This is the ONLY layer that writes to ``user_favourites``.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import text

from db.engine import db_session

logger = logging.getLogger(__name__)


def _normalise_entity(entity_type: str) -> str:
    """Canonical entity type: track / album / artist."""
    value = str(entity_type or "").strip().lower()
    aliases = {
        "track": "track", "song": "track",
        "album": "album",
        "artist": "artist",
    }
    return aliases.get(value, value)


def is_favourite(username: str, entity_type: str, entity_id: str) -> bool:
    """True when *username* has hearted this entity."""
    entity_type = _normalise_entity(entity_type)
    if not username or not entity_id:
        return False
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT 1 FROM user_favourites
                    WHERE username = :username
                      AND entity_type = :entity_type
                      AND entity_id = :entity_id
                      AND is_favourite = TRUE
                    LIMIT 1
                """),
                {"username": username, "entity_type": entity_type, "entity_id": entity_id},
            )
            return result.fetchone() is not None
    except Exception as exc:
        logger.debug("Favourite lookup failed (%s/%s/%s): %s", username, entity_type, entity_id, exc)
        return False


def set_favourite(
    username: str,
    entity_type: str,
    entity_id: str,
    is_favourite: bool,
    navidrome_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Insert or update the per-user favourite state for an entity.

    Returns the stored row (dict) or None on failure.
    """
    entity_type = _normalise_entity(entity_type)
    if not username or not entity_id:
        return None
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    INSERT INTO user_favourites
                        (username, entity_type, entity_id, navidrome_id, is_favourite, updated_at)
                    VALUES
                        (:username, :entity_type, :entity_id, :navidrome_id, :is_favourite, CURRENT_TIMESTAMP)
                    ON CONFLICT (username, entity_type, entity_id) DO UPDATE SET
                        navidrome_id = COALESCE(EXCLUDED.navidrome_id, user_favourites.navidrome_id),
                        is_favourite = EXCLUDED.is_favourite,
                        updated_at = CURRENT_TIMESTAMP
                    RETURNING id, username, entity_type, entity_id, navidrome_id,
                              is_favourite, created_at, updated_at
                """),
                {
                    "username": username,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "navidrome_id": navidrome_id or None,
                    "is_favourite": bool(is_favourite),
                },
            )
            row = result.fetchone()
            return dict(row._mapping) if row else None
    except Exception as exc:
        logger.error("[FAVOURITES] set failed (%s/%s/%s): %s", username, entity_type, entity_id, exc)
        return None


def get_favourite_ids(username: str, entity_type: str) -> list[str]:
    """All entity IDs hearted by *username* for an entity type."""
    entity_type = _normalise_entity(entity_type)
    if not username:
        return []
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT entity_id FROM user_favourites
                    WHERE username = :username
                      AND entity_type = :entity_type
                      AND is_favourite = TRUE
                    ORDER BY updated_at DESC
                """),
                {"username": username, "entity_type": entity_type},
            )
            return [str(r[0]) for r in result.fetchall() or []]
    except Exception as exc:
        logger.debug("Favourite ids lookup failed (%s/%s): %s", username, entity_type, exc)
        return []


def get_favourite_navidrome_ids(username: str, entity_type: str) -> list[str]:
    """Navidrome IDs hearted by *username* for an entity type (for star sync)."""
    entity_type = _normalise_entity(entity_type)
    if not username:
        return []
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT COALESCE(navidrome_id, entity_id) FROM user_favourites
                    WHERE username = :username
                      AND entity_type = :entity_type
                      AND is_favourite = TRUE
                      AND COALESCE(navidrome_id, entity_id) <> ''
                """),
                {"username": username, "entity_type": entity_type},
            )
            return [str(r[0]) for r in result.fetchall() or []]
    except Exception as exc:
        logger.debug("Favourite navidrome ids lookup failed (%s/%s): %s", username, entity_type, exc)
        return []
