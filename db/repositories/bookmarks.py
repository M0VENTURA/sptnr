"""Bookmark/favourite repository queries."""

from __future__ import annotations

import logging

from sqlalchemy import text

from db.engine import db_session


def is_artist_favourited(artist_name: str) -> bool:
    """Return True if the given artist is marked as a favourite."""
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT 1
                    FROM bookmarks
                    WHERE (type = :t OR bookmark_type = :t)
                      AND LOWER(COALESCE(name, artist_name, title, '')) = LOWER(:name)
                    LIMIT 1
                """),
                {"t": "artist_favourite", "name": artist_name},
            )
            if result.fetchone():
                return True

            result = session.execute(
                text("""
                    SELECT 1
                    FROM user_loved_artists
                    WHERE LOWER(artist) = LOWER(:name)
                      AND is_loved = TRUE
                    LIMIT 1
                """),
                {"name": artist_name},
            )
            return result.fetchone() is not None
    except Exception as exc:
        logging.debug("Error checking favourite status for '%s': %s", artist_name, exc)
        return False
