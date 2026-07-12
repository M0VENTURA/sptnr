"""Metadata repository.

SQL-only helper functions for services.metadata.
All functions now use SQLAlchemy ``db_session()`` internally.
"""

from __future__ import annotations

from typing import Any
from sqlalchemy import text
from db.engine import db_session


# -----------------------------------------------------------------------------
# Album favourites / art / tracklists
# -----------------------------------------------------------------------------

def album_is_favourite(conn: Any = None, artist: str = "", album: str = "") -> bool:
    with db_session() as session:
        result = session.execute(
            text("""
                SELECT id FROM bookmarks
                WHERE type = :t
                  AND LOWER(artist) = LOWER(:artist)
                  AND LOWER(album) = LOWER(:album)
                LIMIT 1
            """),
            {"t": "album_favourite", "artist": artist, "album": album},
        )
        return result.fetchone() is not None


def set_album_favourite_db(conn: Any = None, artist: str = "", album: str = "", is_favourite: bool = False) -> None:
    with db_session() as session:
        if is_favourite:
            session.execute(
                text("""
                    INSERT INTO bookmarks (type, artist, album)
                    VALUES (:t, :artist, :album)
                    ON CONFLICT DO NOTHING
                """),
                {"t": "album_favourite", "artist": artist, "album": album},
            )
        else:
            session.execute(
                text("""
                    DELETE FROM bookmarks
                    WHERE type = :t
                      AND LOWER(artist) = LOWER(:artist)
                      AND LOWER(album) = LOWER(:album)
                """),
                {"t": "album_favourite", "artist": artist, "album": album},
            )


def fetch_album_art_blob(conn: Any = None, artist: str = "", album: str = ""):
    with db_session() as session:
        result = session.execute(text("""
            SELECT image_data, image_mime_type
            FROM album_art
            WHERE LOWER(COALESCE(artist_name, '')) = LOWER(:artist)
              AND LOWER(COALESCE(album_name, '')) = LOWER(:album)
            LIMIT 1
        """), {"artist": artist, "album": album})

        row = result.fetchone()
        if not row:
            return None, None
        return (row[0], row[1] or "image/jpeg")


def fetch_album_art_urls(conn: Any = None, artist: str = "", album: str = "") -> list[str]:
    with db_session() as session:
        result = session.execute(text("""
            SELECT url
            FROM album_art_urls
            WHERE LOWER(artist_name) = LOWER(:artist)
              AND LOWER(album_name) = LOWER(:album)
        """), {"artist": artist, "album": album})
        return [str(row[0]) for row in result.fetchall() or []]


def save_album_art_db(conn: Any = None, artist: str = "", album: str = "",
                      image_data: bytes | None = None, mime: str = "", source: str = "") -> None:
    with db_session() as session:
        session.execute(text("""
            INSERT INTO album_art (artist_name, album_name, image_data, image_mime_type, source, downloaded_at)
            VALUES (:artist, :album, :data, :mime, :source, CURRENT_TIMESTAMP)
            ON CONFLICT (artist_name, album_name)
            DO UPDATE SET
                image_data = EXCLUDED.image_data,
                image_mime_type = EXCLUDED.image_mime_type,
                source = EXCLUDED.source,
                downloaded_at = CURRENT_TIMESTAMP
        """), {"artist": artist, "album": album, "data": image_data, "mime": mime, "source": source})


def fetch_album_tracklist(conn: Any = None, artist: str = "", album: str = ""):
    with db_session() as session:
        result = session.execute(text("""
            SELECT id, title, track_number, duration, artist
            FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
              AND album = :album
            ORDER BY COALESCE(disc_number, 1),
                     COALESCE(track_number, 999),
                     title
        """), {"artist": artist, "album": album})
        return result.fetchall() or []


def fetch_album_queue_track_stubs(conn: Any = None, artist: str = "", album: str = ""):
    with db_session() as session:
        result = session.execute(text("""
            SELECT id, file_path
            FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
              AND album = :album
              AND file_path LIKE '__queued_for_download__%%'
        """), {"artist": artist, "album": album})
        return result.fetchall() or []


def fetch_queue_status(conn: Any = None, queue_id: int = 0) -> str:
    with db_session() as session:
        result = session.execute(
            text("SELECT status FROM download_queue WHERE id = :id"),
            {"id": queue_id},
        )
        row = result.fetchone()
        return str(row[0]) if row else "queued"


def fetch_album_tracks_for_tag_update(conn: Any = None, artist: str = "", album: str = ""):
    with db_session() as session:
        result = session.execute(text("""
            SELECT id, title, file_path
            FROM tracks
            WHERE artist = :artist AND album = :album
        """), {"artist": artist, "album": album})
        return result.fetchall() or []


def update_track_genres(conn: Any = None, track_id: Any = None, genres_str: str = "") -> int:
    with db_session() as session:
        result = session.execute(text("""
            UPDATE tracks
            SET genres = :genres,
                manual_genres = :genres
            WHERE id = :id
        """), {"genres": genres_str, "id": track_id})
        return result.rowcount or 0


def update_album_mbid_fields(conn: Any = None, artist: str = "", album: str = "",
                              mbid: str | None = None, rg_mbid: str | None = None,
                              cover_url: str | None = None) -> int:
    with db_session() as session:
        updates = []
        params: dict[str, Any] = {"artist": artist, "album": album}

        if mbid:
            updates.append("musicbrainz_album_mbid = :mbid")
            params["mbid"] = mbid

        if rg_mbid:
            updates.append("musicbrainz_releasegroupid = :rg_mbid")
            params["rg_mbid"] = rg_mbid

        if cover_url:
            updates.append("cover_art_url = :cover_url")
            params["cover_url"] = cover_url

        if not updates:
            return 0

        result = session.execute(text(f"""
            UPDATE tracks
            SET {', '.join(updates)}
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
              AND album = :album
        """), params)
        return result.rowcount or 0


def update_album_discogs_fields(conn: Any = None, artist: str = "", album: str = "",
                                 discogs_id: str = "", is_single: bool = False) -> int:
    with db_session() as session:
        if is_single:
            result = session.execute(text("""
                UPDATE tracks
                SET discogs_album_id = :did,
                    is_single = TRUE,
                    single_confidence = 'high',
                    stars = 5
                WHERE artist = :artist AND album = :album
            """), {"did": discogs_id, "artist": artist, "album": album})
        else:
            result = session.execute(text("""
                UPDATE tracks
                SET discogs_album_id = :did
                WHERE artist = :artist AND album = :album
            """), {"did": discogs_id, "artist": artist, "album": album})
        return result.rowcount or 0


def ignore_missing_track_db(
    conn: Any = None,
    missing_id: str | None = None,
    artist: str = "",
    album: str = "",
    title: str = "",
    disc_number: int | None = None
) -> int:
    with db_session() as session:
        if missing_id:
            result = session.execute(
                text("UPDATE missing_album_tracks SET ignored = TRUE WHERE id = :id"),
                {"id": missing_id},
            )
        else:
            result = session.execute(text("""
                UPDATE missing_album_tracks
                SET ignored = TRUE
                WHERE artist_name = :artist
                  AND album_name = :album
                  AND title = :title
                  AND disc_number = :disc
            """), {"artist": artist, "album": album, "title": title, "disc": disc_number})

        return result.rowcount or 0


# -----------------------------------------------------------------------------
# Artist corrections / metadata
# -----------------------------------------------------------------------------

def find_track_row(conn: Any = None, track_id: str = ""):
    with db_session() as session:
        result = session.execute(text("""
            SELECT id, file_path, artist, album, title
            FROM tracks
            WHERE CAST(id AS TEXT) = :id
            LIMIT 1
        """), {"id": track_id})
        return result.fetchone()


def delete_track_row(conn: Any = None, track_id: str = "") -> int:
    """Delete a track row by ID.

    When a raw psycopg2 connection is passed, runs on that connection
    so the delete participates in the caller's transaction.
    """
    sql = text("DELETE FROM tracks WHERE CAST(id AS TEXT) = :id")
    params = {"id": track_id}

    if conn is not None and not hasattr(conn, "execute"):
        # Caller passed a psycopg2 connection — get a cursor
        cursor = conn.cursor()
        cursor.execute(sql.text, params)
        return cursor.rowcount or 0

    with db_session() as session:
        result = session.execute(sql, params)
        return result.rowcount or 0


def fetch_track_for_delete(conn: Any = None, track_id: str = "") -> tuple | None:
    """Fetch track data by ID for deletion processing.

    When a raw psycopg2 connection/cursor is passed (e.g. from
    ``artist_service.delete_track``), the query runs on that connection
    so it participates in the caller's transaction.  Otherwise falls
    back to ``db_session()``.
    """
    sql = text("""
        SELECT id, file_path, artist, album, title
        FROM tracks
        WHERE CAST(id AS TEXT) = :id
        LIMIT 1
    """)
    params = {"id": track_id}

    if conn is not None:
        # Caller passed a psycopg2 connection or cursor — use it directly
        cursor = conn.cursor() if not hasattr(conn, "execute") else conn
        cursor.execute(sql.text, params)
        return cursor.fetchone()

    with db_session() as session:
        result = session.execute(sql, params)
        return result.fetchone()


def merge_album_names(conn: Any = None, artist: str = "", source_albums: list[str] | None = None, canonical_name: str = "") -> int:
    if not source_albums:
        return 0
    with db_session() as session:
        placeholders = ", ".join([f":s{i}" for i in range(len(source_albums))])
        params: dict[str, Any] = {"canonical": canonical_name, "artist": artist}
        params.update({f"s{i}": s for i, s in enumerate(source_albums)})
        result = session.execute(text(f"""
            UPDATE tracks
            SET album = :canonical
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
              AND album IN ({placeholders})
        """), params)
        return result.rowcount or 0



def count_album_disc_numbers(conn: Any = None, artist: str = "", album: str = "") -> int:
    with db_session() as session:
        result = session.execute(text("""
            SELECT COUNT(DISTINCT disc_number)
            FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
              AND album = :album
        """), {"artist": artist, "album": album})
        return int(result.scalar() or 0)


def clear_album_disc_numbers(conn: Any = None, artist: str = "", album: str = "") -> int:
    with db_session() as session:
        result = session.execute(text("""
            UPDATE tracks
            SET disc_number = NULL
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
              AND album = :album
        """), {"artist": artist, "album": album})
        return result.rowcount or 0


def artist_track_count(conn: Any = None, artist: str = "") -> int:
    with db_session() as session:
        result = session.execute(text("""
            SELECT COUNT(*)
            FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
        """), {"artist": artist})
        return int(result.scalar() or 0)


def fetch_cached_missing_releases(conn: Any = None, artist: str = ""):
    with db_session() as session:
        result = session.execute(
            text("SELECT title, release_id FROM missing_releases WHERE artist = :artist"),
            {"artist": artist},
        )
        return result.fetchall() or []



# -----------------------------------------------------------------------------
# Artist scan support
# -----------------------------------------------------------------------------

def fetch_artist_albums(conn: Any = None, artist: str = "") -> list[str]:
    with db_session() as session:
        result = session.execute(text("""
            SELECT DISTINCT album
            FROM tracks
            WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
        """), {"artist": artist})
        return [str(r[0]) for r in result.fetchall()]


def fetch_artist_mbid(conn: Any = None, artist: str = "") -> str | None:
    with db_session() as session:
        result = session.execute(text("""
            SELECT musicbrainz_albumartistid
            FROM tracks
            WHERE album_artist = :artist
            LIMIT 1
        """), {"artist": artist})
        row = result.fetchone()
        return str(row[0]) if row else None


def fetch_all_distinct_artists(conn: Any = None) -> list[str]:
    with db_session() as session:
        result = session.execute(text("SELECT DISTINCT artist FROM tracks WHERE artist IS NOT NULL AND TRIM(artist) != ''"))
        return [str(r[0]) for r in result.fetchall()]
