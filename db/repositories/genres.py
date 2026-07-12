"""Genre repository and genre utility functions."""

from __future__ import annotations

import json
import logging
import re

from db.context import db_cursor
from db.utils import row_get


def aggregate_genres_from_tracks(artist_name: str) -> list[str]:
    """Aggregate genres from tracks for an artist."""
    artist_lower = (artist_name or "").lower()
    if artist_lower == "various artists" or "soundtrack" in artist_lower:
        return []
    genres: set[str] = set()
    try:
        with db_cursor() as (_conn, cursor):
            cursor.execute(
                """
                SELECT navidrome_genres
                FROM tracks
                WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s
                """,
                (artist_name,),
            )
            for row in cursor.fetchall() or []:
                genre_value = row_get(row, "navidrome_genres", 0)
                if not genre_value or not isinstance(genre_value, str):
                    continue
                try:
                    parsed = json.loads(genre_value)
                    if isinstance(parsed, list):
                        genres.update(str(item).strip() for item in parsed if item)
                        continue
                except Exception:
                    pass
                for genre in re.split(r"[\,]+", genre_value):
                    genre = genre.strip().strip("\"'[]")
                    if genre:
                        genres.add(genre)
    except Exception as exc:
        logging.error("Error aggregating genres for %s: %s", artist_name, exc)
    return sorted(genres)


def correct_genre_capitalization(genre_str: str) -> str:
    """Normalize common genre capitalization."""
    if not genre_str:
        return genre_str
    genre_lower = genre_str.lower().strip()
    genre_map = {
        "rock": "Rock", "pop": "Pop", "jazz": "Jazz", "classical": "Classical",
        "hip-hop": "Hip-Hop", "hiphop": "Hip-Hop", "r&b": "R&B", "rnb": "R&B",
        "electronic": "Electronic", "blues": "Blues", "country": "Country",
        "soul": "Soul", "funk": "Funk", "metal": "Metal", "punk": "Punk",
        "alternative": "Alternative", "indie": "Indie", "folk": "Folk",
        "reggae": "Reggae", "latin": "Latin", "dance": "Dance", "house": "House",
        "techno": "Techno", "trance": "Trance", "dubstep": "Dubstep",
        "rap": "Rap", "gospel": "Gospel", "edm": "EDM", "ambient": "Ambient",
        "experimental": "Experimental", "avant-garde": "Avant-Garde", "world": "World",
        "afrobeat": "Afrobeat", "reggaeton": "Reggaeton", "trap": "Trap", "grime": "Grime",
    }
    if genre_lower in genre_map:
        return genre_map[genre_lower]
    return " ".join(word.capitalize() for word in genre_str.split())


def log_genre_update(
    artist_name=None,
    album_name=None,
    track_id=None,
    genres_before="",
    genres_after="",
    action_type="manual",
    affected_count=1,
    change_summary="",
) -> None:
    """Insert a genre update audit row."""
    try:
        with db_cursor(commit=True) as (_conn, cursor):
            cursor.execute(
                """
                INSERT INTO genre_updates (
                    artist_name, album_name, track_id, genres_before, genres_after,
                    action_type, affected_track_count, change_summary, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                """,
                (
                    artist_name, album_name, track_id, genres_before, genres_after,
                    action_type, affected_count, change_summary,
                ),
            )
    except Exception as exc:
        logging.error("Failed to log genre update: %s", exc)
