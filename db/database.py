"""Public database facade.

This is intentionally thin. Real implementation lives in db.bootstrap,
db.utils, db.cleanup, and db.repositories.*.
"""

from __future__ import annotations

from db.bootstrap import init_database_and_schema
from db.repositories.artists import insert_artist, is_album_artist_in_collection, get_artists_in_collection
from db.repositories.tracks import insert_or_update_track, get_tracks_by_artist, get_top_tracks
from db.utils import get_db_connection, is_postgres_connection


def init_db() -> bool:
    """Initialize the full application database schema."""
    return init_database_and_schema()


__all__ = [
    "get_db_connection",
    "is_postgres_connection",
    "init_db",
    "insert_artist",
    "insert_or_update_track",
    "get_tracks_by_artist",
    "get_top_tracks",
    "is_album_artist_in_collection",
    "get_artists_in_collection",
]
