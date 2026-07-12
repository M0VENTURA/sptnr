"""Artist and artist-collection repository queries."""

from __future__ import annotations

import logging
from typing import Any

from db.context import db_cursor
from db.utils import row_get


def insert_artist(artist_id: str, name: str) -> None:
    """Insert an artist if it does not already exist."""
    with db_cursor(commit=True) as (_conn, cursor):
        cursor.execute(
            """
            INSERT INTO artists (id, name)
            VALUES (%s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (artist_id, name),
        )


def is_album_artist_in_collection(artist: str) -> bool:
    """Return True if an artist appears as a canonical album artist."""
    if not artist:
        return False
    try:
        with db_cursor() as (_conn, cursor):
            cursor.execute(
                """
                SELECT 1
                FROM tracks
                WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(%s)
                LIMIT 1
                """,
                (artist,),
            )
            return cursor.fetchone() is not None
    except Exception as exc:
        logging.debug("[AUTO-QUEUE] Could not verify album artist '%s': %s", artist, exc)
        return False


def get_artists_in_collection(cursor: Any, names: list[str]) -> set[str]:
    """Return lowercased artist names that exist in tracks."""
    names = [name for name in names if name]
    if not names:
        return set()
    placeholders = ", ".join(["%s"] * len(names))
    try:
        cursor.execute(
            f"""
            SELECT DISTINCT COALESCE(NULLIF(album_artist, ''), artist) AS artist_name
            FROM tracks
            WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) IN ({placeholders})
            """,
            [name.lower() for name in names],
        )
        result: set[str] = set()
        for row in cursor.fetchall() or []:
            artist_name = row_get(row, "artist_name", 0)
            if artist_name:
                result.add(str(artist_name).lower())
        return result
    except Exception:
        return set()
