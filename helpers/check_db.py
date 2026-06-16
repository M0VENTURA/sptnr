#!/usr/bin/env python3
"""PostgreSQL-only schema bootstrap helpers.

This module previously contained SQLite schema migration logic.
The runtime now supports PostgreSQL only, so update_schema performs
lightweight PostgreSQL-safe checks and exits cleanly when PostgreSQL
is unavailable.
"""

from __future__ import annotations

import logging
import time

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
        _lock_acquired = False
        for _lock_attempt in range(10):
            try:
                cursor.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS acquired", ("sptnr_schema_bootstrap",))
                if cursor.fetchone()[0]:
                    _lock_acquired = True
                    break
            except Exception as _adv_err:
                logging.debug("Schema bootstrap advisory lock unavailable: %s", _adv_err)
                break
            time.sleep(0.3)
        if not _lock_acquired:
            logging.warning("Could not acquire schema bootstrap advisory lock; deferring schema initialization")
            return False

        # Core tables required for all app functionality.
        # These were previously only created by database/database.py init_db(),
        # which was never called during startup, causing fresh installs to fail.
        _ensure_table(
            cursor,
            "artists",
            """
            CREATE TABLE IF NOT EXISTS artists (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE
            )
            """,
        )

        _ensure_table(
            cursor,
            "tracks",
            """
            CREATE TABLE IF NOT EXISTS tracks (
                id TEXT PRIMARY KEY
            )
            """,
        )

        _ensure_table(
            cursor,
            "artist_stats",
            """
            CREATE TABLE IF NOT EXISTS artist_stats (
                artist_id TEXT PRIMARY KEY,
                artist_name TEXT NOT NULL,
                album_count INTEGER,
                track_count INTEGER,
                last_updated TEXT
            )
            """,
        )

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

        _ensure_table(
            cursor,
            "weekly_sync_state",
            """
            CREATE TABLE IF NOT EXISTS weekly_sync_state (
                username TEXT NOT NULL,
                source TEXT NOT NULL,
                last_synced_week TEXT,
                last_synced_at TEXT,
                navidrome_playlist_id TEXT,
                PRIMARY KEY (username, source)
            )
            """,
        )

        _ensure_table(
            cursor,
            "weekly_playlist_tracks",
            """
            CREATE TABLE IF NOT EXISTS weekly_playlist_tracks (
                id SERIAL PRIMARY KEY,
                username TEXT NOT NULL,
                source TEXT NOT NULL,
                artist_name TEXT,
                track_name TEXT,
                release_name TEXT,
                recording_mbid TEXT,
                release_mbid TEXT,
                match_status TEXT NOT NULL DEFAULT 'missing',
                local_track_id INTEGER,
                file_path TEXT,
                queue_id INTEGER,
                synced_at TEXT NOT NULL,
                week_key TEXT NOT NULL
            )
            """,
        )
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_weekly_playlist_unique ON weekly_playlist_tracks (username, source, artist_name, track_name)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_weekly_playlist_status ON weekly_playlist_tracks (username, source, match_status)"
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
                if _lock_acquired:
                    cursor = conn.cursor()
                    cursor.execute("SELECT pg_advisory_unlock(hashtext(%s))", ("sptnr_schema_bootstrap",))
            except Exception:
                pass
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# Tables that must exist for basic app operation.
_CRITICAL_TABLES = frozenset(
    {
        "artists",
        "tracks",
        "artist_stats",
    }
)

# Tables created/verified at startup or lazily by feature modules.
# Listed here so verify_all_tables_exist() can report any unexpected gaps.
_FEATURE_TABLES = frozenset(
    {
        "album_art",
        "artist_images",
        "artist_metadata",
        "correction_ignores",
        "discogs_cache_metadata",
        "discogs_singles_cache",
        "download_queue",
        "flash_results",
        "folder_album_matches",
        "folder_track_matches",
        "genre_updates",
        "lastfm_recommendations",
        "lastfm_scheduler_config",
        "lastfm_sync_history",
        "listenbrainz_playlist_scheduler_state",
        "listenbrainz_playlist_tracks",
        "missing_album_tracks",
        "musicbrainz_release_tracks",
        "musicbrainz_releases",
        "recommendation_candidates",
        "scan_history",
        "slsk_banned_words",
        "slskd_search_logs",
        "upcoming_releases",
        "weekly_playlist_tracks",
        "weekly_sync_state",
    }
)


def verify_all_tables_exist() -> dict:
    """Check that all expected tables exist in the current PostgreSQL schema.

    Returns a dict with keys:
      - ``critical_ok`` (bool): all _CRITICAL_TABLES present
      - ``feature_ok`` (bool): all _FEATURE_TABLES present
      - ``missing_critical`` (list): missing critical table names
      - ``missing_feature`` (list): missing feature table names
      - ``present`` (list): all tables that were found
    """
    conn = None
    result = {
        "critical_ok": False,
        "feature_ok": False,
        "missing_critical": [],
        "missing_feature": [],
        "present": [],
    }
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        present = set()
        for table_name in sorted(_CRITICAL_TABLES | _FEATURE_TABLES):
            if _table_exists(cursor, table_name):
                present.add(table_name)

        missing_critical = sorted(_CRITICAL_TABLES - present)
        missing_feature = sorted(_FEATURE_TABLES - present)

        result["critical_ok"] = not missing_critical
        result["feature_ok"] = not missing_feature
        result["missing_critical"] = missing_critical
        result["missing_feature"] = missing_feature
        result["present"] = sorted(present)

        if missing_critical:
            logging.warning(
                "CRITICAL tables missing from database: %s",
                ", ".join(missing_critical),
            )
        if missing_feature:
            logging.info(
                "Feature tables not yet created (will be created on-demand): %s",
                ", ".join(missing_feature),
            )
        logging.info(
            "Table verification complete — %d/%d critical, %d/%d feature tables present",
            len(_CRITICAL_TABLES) - len(missing_critical),
            len(_CRITICAL_TABLES),
            len(_FEATURE_TABLES) - len(missing_feature),
            len(_FEATURE_TABLES),
        )
    except Exception as exc:
        if is_transient_pg_startup_error(exc):
            logging.info("Table verification deferred while PostgreSQL starts: %s", exc)
        else:
            logging.error("Table verification failed: %s", exc)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    return result
