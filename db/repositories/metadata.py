"""Metadata repository.

SQL-only helper functions for services.metadata.
These functions intentionally accept an existing connection to match the repo
style you already use in db/repositories/library.py.
"""

from __future__ import annotations

from typing import Any
from db.utils import row_get


# -----------------------------------------------------------------------------
# Album favourites / art / tracklists
# -----------------------------------------------------------------------------

def album_is_favourite(conn: Any, artist: str, album: str) -> bool:
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT id FROM bookmarks
            WHERE type = %s
              AND LOWER(artist) = LOWER(%s)
              AND LOWER(album) = LOWER(%s)
            """,
            ("album_favourite", artist, album),
        )
        return cursor.fetchone() is not None
    finally:
        cursor.close()


def set_album_favourite_db(conn: Any, artist: str, album: str, is_favourite: bool) -> None:
    cursor = conn.cursor()
    try:
        if is_favourite:
            cursor.execute(
                """
                INSERT INTO bookmarks (type, artist, album)
                VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                ("album_favourite", artist, album),
            )
        else:
            cursor.execute(
                """
                DELETE FROM bookmarks
                WHERE type = %s
                  AND LOWER(artist) = LOWER(%s)
                  AND LOWER(album) = LOWER(%s)
                """,
                ("album_favourite", artist, album),
            )
    finally:
        cursor.close()



def fetch_album_art_blob(conn: Any, artist: str, album: str):
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT image_data, image_mime_type
            FROM album_art
            WHERE LOWER(COALESCE(artist_name, '')) = LOWER(%s)
              AND LOWER(COALESCE(album_name, '')) = LOWER(%s)
            LIMIT 1
        """, (artist, album))

        row = cursor.fetchone()

        if not row:
            return None, None

        return (
            row_get(row, "image_data", 0),
            row_get(row, "image_mime_type", 1) or "image/jpeg",
        )

    finally:
        cursor.close()



def fetch_album_art_urls(conn: Any, artist: str, album: str) -> list[str]:
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT url
            FROM album_art_urls
            WHERE LOWER(artist_name) = LOWER(%s)
              AND LOWER(album_name) = LOWER(%s)
        """, (artist, album))

        return [row[0] for row in cursor.fetchall() or []]

    finally:
        cursor.close()

def save_album_art_db(conn: Any, artist: str, album: str, image_data: bytes, mime: str, source: str) -> None:
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO album_art (artist_name, album_name, image_data, image_mime_type, source, downloaded_at)
            VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (artist_name, album_name)
            DO UPDATE SET
                image_data = EXCLUDED.image_data,
                image_mime_type = EXCLUDED.image_mime_type,
                source = EXCLUDED.source,
                downloaded_at = CURRENT_TIMESTAMP
        """, (artist, album, image_data, mime, source))
        conn.commit()
    finally:
        cursor.close()

def fetch_album_tracklist(conn: Any, artist: str, album: str):
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT id, title, track_number, duration, artist
            FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s
              AND album = %s
            ORDER BY COALESCE(disc_number, 1),
                     COALESCE(track_number, 999),
                     title
        """, (artist, album))

        return cursor.fetchall() or []
    finally:
        cursor.close()



def fetch_album_queue_track_stubs(conn: Any, artist: str, album: str):
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT id, file_path
            FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s
              AND album = %s
              AND file_path LIKE '__queued_for_download__%%'
        """, (artist, album))

        return cursor.fetchall() or []
    finally:
        cursor.close()


def fetch_queue_status(conn: Any, queue_id: int) -> str:
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT status FROM download_queue WHERE id = %s",
            (queue_id,),
        )

        row = cursor.fetchone()
        return row_get(row, "status", 0, "queued") if row else "queued"

    finally:
        cursor.close()


def fetch_album_tracks_for_tag_update(conn: Any, artist: str, album: str):
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT id, title, file_path
            FROM tracks
            WHERE artist = %s AND album = %s
        """, (artist, album))

        return cursor.fetchall() or []
    finally:
        cursor.close()


def update_track_genres(conn: Any, track_id: Any, genres_str: str) -> int:
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE tracks
            SET genres = %s,
                manual_genres = %s
            WHERE id = %s
        """, (genres_str, genres_str, track_id))

        return cursor.rowcount or 0
    finally:
        cursor.close()


def update_album_mbid_fields(conn: Any, artist: str, album: str, mbid: str | None, rg_mbid: str | None, cover_url: str | None) -> int:
    cursor = conn.cursor()
    try:
        updates = []
        params = []

        if mbid:
            updates.append("musicbrainz_album_mbid = %s")
            params.append(mbid)

        if rg_mbid:
            updates.append("musicbrainz_releasegroupid = %s")
            params.append(rg_mbid)

        if cover_url:
            updates.append("cover_art_url = %s")
            params.append(cover_url)

        if not updates:
            return 0

        params.extend([artist, album])

        cursor.execute(f"""
            UPDATE tracks
            SET {', '.join(updates)}
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s
              AND album = %s
        """, params)

        return cursor.rowcount or 0

    finally:
        cursor.close()


def update_album_discogs_fields(conn: Any, artist: str, album: str, discogs_id: str, is_single: bool) -> int:
    cursor = conn.cursor()
    try:
        if is_single:
            cursor.execute("""
                UPDATE tracks
                SET discogs_album_id = %s,
                    is_single = TRUE,
                    single_confidence = 'high',
                    stars = 5
                WHERE artist = %s AND album = %s
            """, (discogs_id, artist, album))
        else:
            cursor.execute("""
                UPDATE tracks
                SET discogs_album_id = %s
                WHERE artist = %s AND album = %s
            """, (discogs_id, artist, album))

        return cursor.rowcount or 0
    finally:
        cursor.close()


def ignore_missing_track_db(
    conn: Any,
    missing_id: str | None,
    artist: str,
    album: str,
    title: str,
    disc_number: int | None
) -> int:
    cursor = conn.cursor()
    try:
        if missing_id:
            cursor.execute(
                "UPDATE missing_album_tracks SET ignored = TRUE WHERE id = %s",
                (missing_id,)
            )
        else:
            cursor.execute("""
                UPDATE missing_album_tracks
                SET ignored = TRUE
                WHERE artist_name = %s
                  AND album_name = %s
                  AND title = %s
                  AND disc_number = %s
            """, (artist, album, title, disc_number))

        return cursor.rowcount or 0
    finally:
        cursor.close()


# -----------------------------------------------------------------------------
# Artist corrections / metadata
# -----------------------------------------------------------------------------

def fetch_track_for_delete(conn: Any, track_id: str):
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT id, file_path, artist, album, title
            FROM tracks
            WHERE CAST(id AS TEXT) = %s
            LIMIT 1
        """, (track_id,))

        return cursor.fetchone()
    finally:
        cursor.close()


def delete_track_row(conn: Any, track_id: str) -> int:
    cursor = conn.cursor()
    try:
        cursor.execute(
            "DELETE FROM tracks WHERE CAST(id AS TEXT) = %s",
            (track_id,),
        )
        return cursor.rowcount or 0
    finally:
        cursor.close()


def merge_album_names(conn: Any, artist: str, source_albums: list[str], canonical_name: str) -> int:
    cursor = conn.cursor()

    placeholders = ", ".join(["%s"] * len(source_albums))

    try:
        cursor.execute(f"""
            UPDATE tracks
            SET album = %s
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s
              AND album IN ({placeholders})
        """, [canonical_name, artist] + list(source_albums))

        return cursor.rowcount or 0

    finally:
        cursor.close()



def count_album_disc_numbers(conn: Any, artist: str, album: str) -> int:
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT COUNT(DISTINCT disc_number)
            FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s
              AND album = %s
        """, (artist, album))

        row = cursor.fetchone()
        return int(row_get(row, "count", 0, 0) or 0)


    finally:
        cursor.close()


def clear_album_disc_numbers(conn: Any, artist: str, album: str) -> int:
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE tracks
            SET disc_number = NULL
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s
              AND album = %s
        """, (artist, album))

        return cursor.rowcount or 0

    finally:
        cursor.close()


def artist_track_count(conn: Any, artist: str) -> int:
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT COUNT(*)
            FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s
        """, (artist,))

        row = cursor.fetchone()
        return int(row_get(row, "count", 0, 0) or 0)

    finally:
        cursor.close()


def fetch_cached_missing_releases(conn: Any, artist: str):
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT title, release_id FROM missing_releases WHERE artist = %s",
            (artist,),
        )
        return cursor.fetchall() or []
    finally:
        cursor.close()



# -----------------------------------------------------------------------------
# Artist scan support
# -----------------------------------------------------------------------------

def fetch_artist_albums(conn: Any, artist: str) -> list[str]:
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT DISTINCT album
            FROM tracks
            WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(%s)
        """, (artist,))

        return [
            r[0] if not hasattr(r, "get") else r.get("album")
            for r in cursor.fetchall()
        ]

    finally:
        cursor.close()


from db.utils import row_get

def fetch_artist_mbid(conn: Any, artist: str) -> str | None:
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT musicbrainz_albumartistid
            FROM tracks
            WHERE album_artist = %s
            LIMIT 1
        """, (artist,))

        row = cursor.fetchone()
        return row_get(row, "musicbrainz_albumartistid", 0)

    finally:
        cursor.close()



def fetch_all_distinct_artists(conn: Any) -> list[str]:
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT DISTINCT artist FROM tracks WHERE artist IS NOT NULL AND TRIM(artist) != ''")
        return [r[0] if not hasattr(r, "get") else r.get("artist") for r in cursor.fetchall()]
    finally:
        cursor.close()
