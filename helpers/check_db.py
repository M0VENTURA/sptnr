#!/usr/bin/env python3
"""PostgreSQL-only schema bootstrap helpers.

This module previously contained SQLite schema migration logic.
The runtime now supports PostgreSQL only, so update_schema performs
lightweight PostgreSQL-safe checks and exits cleanly when PostgreSQL
is unavailable.
"""

from __future__ import annotations

import logging

from helpers.db_utils import get_db_connection, _table_exists, is_transient_pg_startup_error

DB_TIMEOUT = 120.0
required_columns = {}


def _ensure_table(cursor, table_name: str, ddl: str) -> None:
    if _table_exists(cursor, table_name):
        return
    cursor.execute("SAVEPOINT sptnr_schema_table_create")
    try:
        cursor.execute(ddl)
        logging.info("Created missing PostgreSQL table: %s", table_name)
    except Exception as exc:
        # CREATE TABLE IF NOT EXISTS can still race under concurrent startup.
        # Roll back this statement and continue when another worker won the race.
        cursor.execute("ROLLBACK TO SAVEPOINT sptnr_schema_table_create")
        message = str(exc).lower()
        if "already exists" in message or "pg_type_typname_nsp_index" in message:
            logging.info("PostgreSQL table %s already exists (concurrent create), continuing", table_name)
        else:
            raise
    finally:
        cursor.execute("RELEASE SAVEPOINT sptnr_schema_table_create")


def update_schema(_db_path: str | None = None) -> bool:
    """Initialize required PostgreSQL tables/columns used at app startup.

    The _db_path parameter is retained for backward-compatible call sites.

    Returns True when the schema was successfully initialized, False when
    initialization was deferred because PostgreSQL is not yet available.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT pg_advisory_lock(hashtext(%s))", ("sptnr_schema_bootstrap",))

        _ensure_table(
            cursor,
            "listenbrainz_playlist_scheduler_state",
            """
            CREATE TABLE IF NOT EXISTS listenbrainz_playlist_scheduler_state (
                username TEXT PRIMARY KEY,
                last_synced_week TEXT,
                last_synced_at TEXT,
                last_rematch_at TEXT
            )
            """,
        )

        _ensure_table(
            cursor,
            "genre_updates",
            """
            CREATE TABLE IF NOT EXISTS genre_updates (
                id BIGSERIAL PRIMARY KEY,
                artist_name TEXT,
                album_name TEXT,
                track_id TEXT,
                genres_before TEXT,
                genres_after TEXT,
                action_type TEXT,
                affected_track_count INTEGER,
                change_summary TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
        )

        _ensure_table(
            cursor,
            "recommendation_candidates",
            """
            CREATE TABLE IF NOT EXISTS recommendation_candidates (
                candidate_id TEXT PRIMARY KEY,
                app_user TEXT NOT NULL,
                generator_key TEXT NOT NULL,
                candidate_index INTEGER NOT NULL DEFAULT 0,
                playlist_name TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                refreshed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_recommendation_candidates_user_generator ON recommendation_candidates (app_user, generator_key, candidate_index)"
        )

        conn.commit()
        return True
    except Exception as exc:
        if is_transient_pg_startup_error(exc):
            logging.info("PostgreSQL schema initialization deferred while PostgreSQL starts: %s", exc)
            return False
        logging.error("PostgreSQL schema initialization failed: %s", exc)
        raise
    finally:
        if conn:
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT pg_advisory_unlock(hashtext(%s))", ("sptnr_schema_bootstrap",))
            except Exception:
                pass
        if conn:
            try:
                conn.close()
            except Exception:
                pass
