#!/usr/bin/env python3
"""PostgreSQL-only schema bootstrap helpers.

This module previously contained SQLite schema migration logic.
The runtime now supports PostgreSQL only, so update_schema performs
lightweight PostgreSQL-safe checks and exits cleanly when PostgreSQL
is unavailable.
"""

from __future__ import annotations

import logging

from helpers.db_utils import get_db_connection, _table_exists

DB_TIMEOUT = 120.0
required_columns = {}


def _ensure_table(cursor, table_name: str, ddl: str) -> None:
    if _table_exists(cursor, table_name, is_pg=True):
        return
    cursor.execute(ddl)
    logging.info("Created missing PostgreSQL table: %s", table_name)


def update_schema(_db_path: str | None = None) -> None:
    """Initialize required PostgreSQL tables/columns used at app startup.

    The _db_path parameter is retained for backward-compatible call sites.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

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

        conn.commit()
    except Exception as exc:
        logging.error("PostgreSQL schema initialization failed: %s", exc)
        raise
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
