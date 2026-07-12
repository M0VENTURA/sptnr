"""Playlist track search service.

Provides free-text search across the local track library for use
during manual playlist creation and editing workflows.

Key Functions:
    - search_songs_in_db(): Search tracks by title or artist name
      using case-insensitive LIKE matching. Returns up to 50 results.

Architecture:
    Simple database query layer with no external API dependencies.
    Results include id, title, artist, and album for display in
    the playlist editing UI.
"""

from sqlalchemy import text
from db.engine import db_session

def search_songs_in_db(query):
    """Search tracks by title or artist."""
    with db_session() as session:
        result = session.execute(text("""
            SELECT id, title, artist, album
            FROM tracks
            WHERE LOWER(title) LIKE :query
               OR LOWER(artist) LIKE :query
            LIMIT 50
        """), {"query": f"%{query.lower()}%"})
        return [dict(r._mapping) for r in result.fetchall()]
