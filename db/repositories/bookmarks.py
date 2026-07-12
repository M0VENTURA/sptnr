"""Bookmark/favourite repository queries."""

from __future__ import annotations

import logging

from db.context import db_cursor


def is_artist_favourited(artist_name: str) -> bool:
    """Return True if the given artist is marked as a favourite."""
    try:
        with db_cursor() as (_conn, cursor):
            cursor.execute(
                """
                SELECT 1
                FROM bookmarks
                WHERE (type = %s OR bookmark_type = %s)
                  AND LOWER(COALESCE(name, artist_name, title, '')) = LOWER(%s)
                LIMIT 1
                """,
                ("artist_favourite", "artist_favourite", artist_name),
            )
            if cursor.fetchone():
                return True

            cursor.execute(
                """
                SELECT 1
                FROM user_loved_artists
                WHERE LOWER(artist) = LOWER(%s)
                  AND is_loved = TRUE
                LIMIT 1
                """,
                (artist_name,),
            )
            return cursor.fetchone() is not None
    except Exception as exc:
        logging.debug("Error checking favourite status for '%s': %s", artist_name, exc)
        return False
