"""Library statistics and artist listing queries."""


from __future__ import annotations
from typing import Any, List, Dict
from db.utils import get_db_connection, row_get
from db.context import db_cursor
import logging 

def fetch_artist_library_stats(conn: Any) -> tuple | None:
    """Fetch total library album/track/five-star counts using an existing connection."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT
                COUNT(DISTINCT album) AS album_count,
                COUNT(*) AS track_count,
                COALESCE(SUM(CASE WHEN stars = 5 THEN 1 ELSE 0 END), 0) AS five_star_count
            FROM tracks
            """
        )
        return cursor.fetchone()
    finally:
        try:
            cursor.close()
        except Exception:
            pass


def fetch_all_artists_with_stats(conn: Any, has_album_artist: bool) -> list[Any]:
    """Fetch artists with album, track, five-star, and last-updated counts."""
    cursor = conn.cursor()
    try:
        if has_album_artist:
            query = """
                WITH normalized_tracks AS (
                    SELECT
                        COALESCE(
                            NULLIF(TRIM(musicbrainz_album_mbid), ''),
                            LOWER(TRIM(album)) || '|' || COALESCE(NULLIF(TRIM(CAST(year AS TEXT)), ''), '')
                        ) AS album_key,
                        album,
                        CASE
                            WHEN album_artist IS NOT NULL AND TRIM(album_artist) != '' THEN TRIM(album_artist)
                            WHEN artist IS NOT NULL AND TRIM(artist) != '' THEN TRIM(artist)
                            ELSE NULL
                        END AS candidate_album_artist,
                        last_scanned AS artist_last_updated,
                        stars,
                        CASE
                            WHEN LOWER(TRIM(COALESCE(album_artist, ''))) IN (
                                'various artists', 'various', 'v/a', 'va',
                                'compilation', 'soundtrack', 'original soundtrack'
                            ) THEN 1 ELSE 0
                        END AS is_compilation_artist
                    FROM tracks
                    WHERE album IS NOT NULL AND TRIM(album) != ''
                ),
                ranked_album_artists AS (
                    SELECT
                        album_key,
                        candidate_album_artist,
                        COUNT(*) AS matched_track_count,
                        MAX(artist_last_updated) AS artist_last_updated,
                        MAX(is_compilation_artist) AS is_compilation_artist,
                        ROW_NUMBER() OVER (
                            PARTITION BY album_key
                            ORDER BY MAX(is_compilation_artist) DESC, COUNT(*) DESC, candidate_album_artist ASC
                        ) AS artist_rank
                    FROM normalized_tracks
                    WHERE candidate_album_artist IS NOT NULL AND candidate_album_artist != ''
                    GROUP BY album_key, candidate_album_artist
                ),
                canonical_albums AS (
                    SELECT album_key, candidate_album_artist AS canonical_album_artist, artist_last_updated
                    FROM ranked_album_artists
                    WHERE artist_rank = 1
                ),
                best_artist_name AS (
                    SELECT DISTINCT ON (LOWER(canonical_album_artist))
                        canonical_album_artist AS display_name
                    FROM canonical_albums
                    ORDER BY LOWER(canonical_album_artist)
                )
                SELECT
                    ban.display_name AS display_name,
                    COUNT(DISTINCT ca.album_key) AS album_count,
                    COUNT(nt.album_key) AS track_count,
                    COALESCE(SUM(CASE WHEN nt.stars = 5 THEN 1 ELSE 0 END), 0) AS five_star_count,
                    MAX(ca.artist_last_updated) AS last_updated
                FROM canonical_albums ca
                JOIN normalized_tracks nt ON nt.album_key = ca.album_key
                JOIN best_artist_name ban ON LOWER(ca.canonical_album_artist) = LOWER(ban.display_name)
                GROUP BY ban.display_name
                HAVING COUNT(DISTINCT ca.album_key) > 0
                ORDER BY display_name
            """
        else:
            query = """
                SELECT
                    artist AS display_name,
                    COUNT(DISTINCT album) AS album_count,
                    COUNT(*) AS track_count,
                    COALESCE(SUM(CASE WHEN stars = 5 THEN 1 ELSE 0 END), 0) AS five_star_count,
                    MAX(last_scanned) AS last_updated
                FROM tracks
                WHERE artist IS NOT NULL AND artist != ''
                GROUP BY artist
                HAVING COUNT(DISTINCT album) > 0
                ORDER BY display_name
            """
        cursor.execute(query)
        return cursor.fetchall() or []
    finally:
        try:
            cursor.close()
        except Exception:
            pass

def fetch_genre_mood_analytics(conn: Any, top_n: int = 50):
    """Fetch top genres, moods, and genre+mood combinations."""
    cursor = conn.cursor()

    try:
        # ---- Top Genres ----
        cursor.execute(
            """
            SELECT genre, COUNT(*) AS count
            FROM tracks
            WHERE genre IS NOT NULL AND TRIM(genre) != ''
            GROUP BY genre
            ORDER BY count DESC
            LIMIT %s
            """,
            (top_n,),
        )
        genres = cursor.fetchall() or []

        # ---- Top Moods ----
        cursor.execute(
            """
            SELECT mood, COUNT(*) AS count
            FROM tracks
            WHERE mood IS NOT NULL AND TRIM(mood) != ''
            GROUP BY mood
            ORDER BY count DESC
            LIMIT %s
            """,
            (top_n,),
        )
        moods = cursor.fetchall() or []

        # ---- Genre + Mood combinations ----
        cursor.execute(
            """
            SELECT genre, mood, COUNT(*) AS count
            FROM tracks
            WHERE genre IS NOT NULL AND mood IS NOT NULL
              AND TRIM(genre) != '' AND TRIM(mood) != ''
            GROUP BY genre, mood
            ORDER BY count DESC
            LIMIT %s
            """,
            (top_n,),
        )
        combos = cursor.fetchall() or []

        return genres, moods, combos

    finally:
        try:
            cursor.close()
        except Exception:
            pass

        # db/repositories/library.py


def get_tracks_for_album(artist: str, album: str) -> List[Dict[str, Any]]:
    """
    Return all tracks for a given artist/album.

    Uses album_artist fallback logic:
        COALESCE(NULLIF(album_artist, ''), artist)
    """

    conn = get_db_connection()

    try:
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT *
            FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s
              AND album = %s
            ORDER BY disc_number NULLS FIRST, track_number NULLS FIRST
            """,
            (artist, album),
        )

        columns = [col[0] for col in cursor.description]

        return [
            {columns[i]: value for i, value in enumerate(row)}
            for row in cursor.fetchall() or []
        ]

    finally:
        conn.close()

def get_all_artists() -> list[str]:
    """
    Return all distinct artists based on album_artist fallback logic.
    """

    conn = get_db_connection()

    try:
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT DISTINCT
                COALESCE(NULLIF(TRIM(album_artist), ''), TRIM(artist)) AS artist_name
            FROM tracks
            WHERE artist IS NOT NULL AND TRIM(artist) != ''
            ORDER BY artist_name
            """
        )

        return [row[0] for row in cursor.fetchall() or [] if row[0]]

    finally:
        conn.close()

def get_albums_for_artist(artist: str) -> list[str]:
    """
    Return all albums for a given artist using album_artist fallback logic.
    """

    conn = get_db_connection()

    try:
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT DISTINCT album
            FROM tracks
            WHERE COALESCE(NULLIF(TRIM(album_artist), ''), TRIM(artist)) = %s
              AND album IS NOT NULL
              AND TRIM(album) != ''
            ORDER BY album
            """,
            (artist,),
        )

        return [row[0] for row in cursor.fetchall() or [] if row[0]]

    finally:
        conn.close()




def upsert_musicbrainz_release(
    release_id,
    release_title,
    artist,
    release_year,
    total_tracks,
    monitoring_folder_path,
    method="slskd",
    album_artist=None,
    release_source=None,
):
    """Create or update release entry in database."""

    try:
        with db_cursor(commit=True) as (_, cursor):

            # ✅ Check existing
            cursor.execute("""
                SELECT id FROM musicbrainz_releases
                WHERE release_id = %s
            """, (release_id,))
            existing = cursor.fetchone()

            if existing:
                release_db_id = existing[0]

                cursor.execute("""
                    UPDATE musicbrainz_releases
                    SET status = 'active',
                        album_artist = COALESCE(NULLIF(%s, ''), album_artist),
                        release_source = COALESCE(NULLIF(%s, ''), release_source),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                """, (
                    album_artist,
                    release_source or "musicbrainz",
                    release_db_id,
                ))

                logging.info(f"[RELEASE_ENTRY] Updated {release_db_id}")

            else:
                cursor.execute("""
                    INSERT INTO musicbrainz_releases
                    (release_id, release_title, artist, release_year,
                     total_tracks, monitoring_folder_path,
                     status, method, album_artist, release_source,
                     created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s,
                            'active', %s, %s, %s,
                            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    RETURNING id
                """, (
                    release_id,
                    release_title,
                    artist,
                    release_year,
                    total_tracks,
                    str(monitoring_folder_path),
                    method,
                    album_artist,
                    release_source or "musicbrainz",
                ))

                release_db_id = cursor.fetchone()[0]
                logging.info(f"[RELEASE_ENTRY] Created {release_db_id}")

        return release_db_id

    except Exception as e:
        logging.error(f"[RELEASE_ENTRY] Error: {e}")
        raise
