#!/usr/bin/env python3
"""Fast startup migration for required download_queue columns.

This script is intentionally lightweight for container startup:
- Avoids importing app.py (which can be expensive during boot)
- Uses direct DB connections with short timeouts
- Ensures required queue columns exist (PostgreSQL only)
"""

import os
import sys
import time


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

    connect_timeout = int(os.environ.get("STARTUP_MIGRATION_CONNECT_TIMEOUT", "5"))
    pg_options = os.environ.get(
        "STARTUP_MIGRATION_PG_OPTIONS",
        "-c lock_timeout=5000 -c statement_timeout=30000 -c application_name=startup_queue_migration",
    )

    dsn = os.environ.get("DATABASE_URL") or os.environ.get("PG_DSN")
    if dsn:
        return psycopg2.connect(dsn, connect_timeout=connect_timeout, options=pg_options)

    return psycopg2.connect(
        host=os.environ.get("PG_HOST") or os.environ.get("PGHOST"),
        port=int(os.environ.get("PG_PORT") or os.environ.get("PGPORT") or "5432"),
        user=os.environ.get("PG_USER") or os.environ.get("PGUSER"),
        password=os.environ.get("PG_PASSWORD") or os.environ.get("PGPASSWORD") or "",
        dbname=os.environ.get("PG_DATABASE") or os.environ.get("PGDATABASE") or "sptnr",
        connect_timeout=connect_timeout,
        options=pg_options,
    )


def _try_advisory_xact_lock(cur, lock_name: str, attempts: int = 20, sleep_seconds: float = 0.5) -> bool:
    """Try to acquire a transaction advisory lock without hanging startup forever."""
    for _ in range(max(1, attempts)):
        cur.execute("SELECT pg_try_advisory_xact_lock(hashtext(%s))", (lock_name,))
        row = cur.fetchone()
        if row and bool(row[0]):
            return True
        time.sleep(max(0.05, sleep_seconds))
    return False


def _ensure_postgres_columns(conn):
    # Ensure the table exists with a minimum viable schema.  On a fresh Postgres
    # DB (or after a DROP+recreate during troubleshooting) the table may not exist
    # yet, and the ALTER TABLE statements below would fail with UndefinedTable.
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS download_queue (
            id          BIGSERIAL PRIMARY KEY,
            artist      TEXT      NOT NULL,
            title       TEXT      NOT NULL,
            album       TEXT,
            search_query TEXT,
            source      TEXT      DEFAULT 'soulseek',
            source_id   TEXT,
            status      TEXT      DEFAULT 'queued',
            priority    INTEGER   DEFAULT 5,
            found_filename TEXT,
            file_path   TEXT      UNIQUE,
            metadata    JSONB,
            retry_count INTEGER   DEFAULT 0,
            max_retries INTEGER   DEFAULT 5,
            failure_reason TEXT,
            last_failure_time TIMESTAMP,
            retry_delay_minutes INTEGER DEFAULT 30,
            next_retry_at TIMESTAMP,
            imported_at TIMESTAMP,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()

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
        "auto_delete_at": "TIMESTAMP",
        "copied_individually": "INTEGER DEFAULT 0",
        "copied_individually_at": "TEXT",
        "cover_art_url": "TEXT",
        "queue_folder": "TEXT",
    }

    added = []
    for col, col_type in columns.items():
        try:
            cur.execute(
                f"ALTER TABLE download_queue ADD COLUMN IF NOT EXISTS {col} {col_type}"
            )
            conn.commit()
            if cur.rowcount and cur.rowcount > 0:
                added.append(col)
        except Exception as col_err:
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"⚠ Could not add column {col}: {col_err}")
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


def _ensure_postgres_musicbrainz_release_conflict_target(conn):
    cur = conn.cursor()
    # Avoid indefinite startup hangs if another process is currently migrating.
    if not _try_advisory_xact_lock(cur, "uq_musicbrainz_releases_release_id"):
        print(
            "⚠ startup schema migration (postgres): could not acquire advisory lock for "
            "musicbrainz_releases unique-index check; skipping this step",
            flush=True,
        )
        return []

    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_indexes
            WHERE schemaname = current_schema()
              AND tablename = 'musicbrainz_releases'
              AND indexdef ILIKE 'CREATE UNIQUE INDEX% (release_id)%'
        )
        """
    )
    exists_row = cur.fetchone()
    has_unique = bool(exists_row and exists_row[0])
    if has_unique:
        return []

    cur.execute(
        """
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY release_id
                       ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST, id DESC
                   ) AS rn
            FROM musicbrainz_releases
            WHERE release_id IS NOT NULL
        )
        DELETE FROM musicbrainz_releases m
        USING ranked r
        WHERE m.id = r.id
          AND r.rn > 1
        """
    )

    cur.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_musicbrainz_releases_release_id
        ON musicbrainz_releases (release_id)
        """
    )
    conn.commit()
    return ["musicbrainz_releases.release_id_unique"]


def main():
    try:
        if not _postgres_env_configured():
            print("✗ startup schema migration failed: PostgreSQL is required but not configured")
            return 1

        conn = _connect_postgres()
        try:
            queue_added = _ensure_postgres_columns(conn)
            track_added = _ensure_postgres_track_columns(conn)
            mb_added = _ensure_postgres_musicbrainz_release_conflict_target(conn)
            added = queue_added + [f"tracks.{c}" for c in track_added] + mb_added
            if added:
                print(f"✓ startup schema migration (postgres): added {', '.join(added)}")
            else:
                print("✓ startup schema migration (postgres): no changes")
            return 0
        finally:
            conn.close()
    except Exception as e:
        print(f"✗ startup schema migration failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
