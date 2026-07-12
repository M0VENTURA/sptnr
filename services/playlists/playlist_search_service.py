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

from db.utils import get_db_connection

def search_songs_in_db(query):
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("""
            SELECT id, title, artist, album
            FROM tracks
            WHERE LOWER(title) LIKE %s
               OR LOWER(artist) LIKE %s
            LIMIT 50
        """, (f"%{query.lower()}%", f"%{query.lower()}%"))

        return cursor.fetchall()
    finally:
        conn.close()
