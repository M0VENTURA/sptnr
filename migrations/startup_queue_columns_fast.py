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
    return bool(os.environ.get("DATABASE_URL") or os.environ.get("PG_DSN") or os.environ.get("PG_HOST"))


def _connect_postgres():
    import psycopg2

    dsn = os.environ.get("DATABASE_URL") or os.environ.get("PG_DSN")
    if dsn:
        return psycopg2.connect(dsn, connect_timeout=5)

    return psycopg2.connect(
        host=os.environ.get("PG_HOST"),
        port=int(os.environ.get("PG_PORT", "5432")),
        user=os.environ.get("PG_USER"),
        password=os.environ.get("PG_PASSWORD", ""),
        dbname=os.environ.get("PG_DATABASE", "sptnr"),
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


def main():
    try:
        if _postgres_env_configured():
            try:
                conn = _connect_postgres()
                try:
                    added = _ensure_postgres_columns(conn)
                    if added:
                        print(f"✓ startup queue migration (postgres): added {', '.join(added)}")
                    else:
                        print("✓ startup queue migration (postgres): no changes")
                    return 0
                finally:
                    conn.close()
            except Exception as e:
                # Fast-fail behavior: report and continue boot; app startup will report DB state as needed.
                print(f"⚠ startup queue migration (postgres) skipped: {e}")
                return 0

        db_path = os.environ.get("DB_PATH", "/database/sptnr.db")
        conn = sqlite3.connect(db_path, timeout=5)
        try:
            added = _ensure_sqlite_columns(conn)
            if added:
                print(f"✓ startup queue migration (sqlite): added {', '.join(added)}")
            else:
                print("✓ startup queue migration (sqlite): no changes")
        finally:
            conn.close()
        return 0
    except Exception as e:
        print(f"⚠ startup queue migration failed (non-fatal): {e}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
