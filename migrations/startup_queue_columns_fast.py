#!/usr/bin/env python3
"""Fast startup migration for required download_queue columns.

This script is intentionally lightweight for container startup:
- Avoids importing app.py (which can be expensive during boot)
- Uses direct DB connections with short timeouts
- Ensures required queue columns exist (PostgreSQL primary, SQLite fallback)
"""

import os
import sqlite3
import sys


def _postgres_env_configured():
    """Return True when Postgres appears configured via DSN or host/user/db env vars.

    Supports both app-native PG_* names and libpq-style PGHOST/PGUSER/PGDATABASE.
    """
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("PG_DSN")
    host = os.environ.get("PG_HOST") or os.environ.get("PGHOST")
    user = os.environ.get("PG_USER") or os.environ.get("PGUSER")
    database = os.environ.get("PG_DATABASE") or os.environ.get("PGDATABASE")
    return bool(dsn or (host and user and database))


def _connect_postgres():
    import psycopg2

    dsn = os.environ.get("DATABASE_URL") or os.environ.get("PG_DSN")
    if dsn:
        return psycopg2.connect(dsn, connect_timeout=5)

    return psycopg2.connect(
        host=os.environ.get("PG_HOST") or os.environ.get("PGHOST"),
        port=int(os.environ.get("PG_PORT") or os.environ.get("PGPORT") or "5432"),
        user=os.environ.get("PG_USER") or os.environ.get("PGUSER"),
        password=os.environ.get("PG_PASSWORD") or os.environ.get("PGPASSWORD") or "",
        dbname=os.environ.get("PG_DATABASE") or os.environ.get("PGDATABASE") or "sptnr",
        connect_timeout=5,
    )


def _ensure_postgres_columns(conn):
    columns = {
        "release_id": "TEXT",
        "release_source": "TEXT",
        "track_number": "TEXT",
        "disc_number": "TEXT",
        "album_artist": "TEXT",
        "year": "TEXT",
        "release_mbid": "TEXT",
        "recording_mbid": "TEXT",
        "release_year": "INTEGER",
        "duration": "INTEGER",
        "matched_file_path": "TEXT",
        "in_collection": "INTEGER DEFAULT 0",
        "collection_track_id": "TEXT",
        "collection_matched_at": "TEXT",
        "copied_individually": "INTEGER DEFAULT 0",
        "copied_individually_at": "TEXT",
        "cover_art_url": "TEXT",
    }

    cur = conn.cursor()
    added = []
    for col, col_type in columns.items():
        cur.execute(f"ALTER TABLE download_queue ADD COLUMN IF NOT EXISTS {col} {col_type}")
        if cur.rowcount and cur.rowcount > 0:
            added.append(col)
    conn.commit()
    return added


def _ensure_sqlite_columns(conn):
    columns = {
        "release_id": "TEXT",
        "release_source": "TEXT",
        "track_number": "TEXT",
        "disc_number": "TEXT",
        "album_artist": "TEXT",
        "year": "TEXT",
        "release_mbid": "TEXT",
        "recording_mbid": "TEXT",
        "release_year": "INTEGER",
        "duration": "INTEGER",
        "matched_file_path": "TEXT",
        "in_collection": "INTEGER DEFAULT 0",
        "collection_track_id": "TEXT",
        "collection_matched_at": "TEXT",
        "copied_individually": "INTEGER DEFAULT 0",
        "copied_individually_at": "TEXT",
        "cover_art_url": "TEXT",
    }

    cur = conn.cursor()
    cur.execute("PRAGMA table_info(download_queue)")
    existing = {row[1] for row in cur.fetchall()}

    added = []
    for col, col_type in columns.items():
        if col not in existing:
            cur.execute(f"ALTER TABLE download_queue ADD COLUMN {col} {col_type}")
            added.append(col)
    conn.commit()
    return added


def _ensure_postgres_track_columns(conn):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = 'tracks' AND column_name = 'release_year'
        )
        """
    )
    exists_row = cur.fetchone()
    exists = bool(exists_row and exists_row[0])
    if not exists:
        cur.execute("ALTER TABLE tracks ADD COLUMN IF NOT EXISTS release_year INTEGER")
        conn.commit()
        return ["release_year"]
    return []


def _ensure_sqlite_track_columns(conn):
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(tracks)")
    existing = {row[1] for row in cur.fetchall()}
    added = []
    if "release_year" not in existing:
        cur.execute("ALTER TABLE tracks ADD COLUMN release_year INTEGER")
        added.append("release_year")
        conn.commit()
    return added


def main():
    try:
        if _postgres_env_configured():
            try:
                conn = _connect_postgres()
                try:
                    queue_added = _ensure_postgres_columns(conn)
                    track_added = _ensure_postgres_track_columns(conn)
                    added = queue_added + [f"tracks.{c}" for c in track_added]
                    if added:
                        print(f"✓ startup schema migration (postgres): added {', '.join(added)}")
                    else:
                        print("✓ startup schema migration (postgres): no changes")
                    return 0
                finally:
                    conn.close()
            except Exception as e:
                # Fast-fail behavior: report and continue boot; app startup will report DB state as needed.
                print(f"⚠ startup schema migration (postgres) skipped: {e}")
                return 0

        db_path = os.environ.get("DB_PATH", "/database/sptnr.db")
        conn = sqlite3.connect(db_path, timeout=5)
        try:
            queue_added = _ensure_sqlite_columns(conn)
            track_added = _ensure_sqlite_track_columns(conn)
            added = queue_added + [f"tracks.{c}" for c in track_added]
            if added:
                print(f"✓ startup schema migration (sqlite): added {', '.join(added)}")
            else:
                print("✓ startup schema migration (sqlite): no changes")
        finally:
            conn.close()
        return 0
    except Exception as e:
        print(f"⚠ startup schema migration failed (non-fatal): {e}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
