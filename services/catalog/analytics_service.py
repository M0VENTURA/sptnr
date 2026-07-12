"""Analytics service for genre and mood statistics.

Provides aggregated genre, mood, and genre-mood combination counts
from the library for dashboard and analytics display.

Key Functions:
    - get_genre_mood_analytics(): Fetch top genres, moods, and combos.
      Returns three lists: (genres, moods, combos) each with name/count.

Architecture:
    Pure query layer — delegates to ``db/repositories/library.py`` for
    data access. No business logic or mutations performed.

    Callers:
        - routes/analytics.py (dashboard endpoints)
"""

from sqlalchemy import text
from db.engine import db_session
from db.utils import get_db_connection  # TODO: migrate
from db.repositories.library import fetch_genre_mood_analytics


def get_genre_mood_analytics(top_n=50):
    conn = get_db_connection()

    try:
        genres_raw, moods_raw, combos_raw = fetch_genre_mood_analytics(conn, top_n)

        genres = [{"name": row[0], "count": row[1]} for row in genres_raw]
        moods = [{"name": row[0], "count": row[1]} for row in moods_raw]
        combos = [
            {"genre": row[0], "mood": row[1], "count": row[2]}
            for row in combos_raw
        ]

        return genres, moods, combos

    finally:
        conn.close()