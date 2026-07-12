"""Full PostgreSQL schema bootstrap for Popularr."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Iterable

from sqlalchemy import text

from db.engine import db_session
from db.schema import (
    COLUMN_REGISTRY,
    INDEXES_TO_ENSURE,
    TABLES_TO_ENSURE,
    DOWNLOAD_QUEUE_COLUMNS_TO_ENSURE,
    TRACK_COLUMNS_TO_ENSURE,
)
from db.schema_helpers import table_exists, get_table_columns
from db.utils import get_db_connection, is_transient_pg_startup_error

_SCHEMA_BOOTSTRAP_LOCK_NAME = "popularr_schema_bootstrap"
_ALBUM_ART_DATA_LOCK_KEY = 915317411
_ALBUM_ART_SCHEMA_LOCK_KEY = 1986627450

# =============================================================================
# CORE HELPERS
# =============================================================================

def _ensure_table(cursor: Any, table_name: str, ddl: str) -> None:
    cursor.execute(text("SAVEPOINT popularr_schema_table_create"))
    try:
        cursor.execute(text(ddl))
    except Exception as exc:
        cursor.execute(text("ROLLBACK TO SAVEPOINT popularr_schema_table_create"))
        if "already exists" not in str(exc).lower() and "duplicate" not in str(exc).lower(): raise
    finally:
        try: cursor.execute(text("RELEASE SAVEPOINT popularr_schema_table_create"))
        except Exception: pass

def _ensure_columns(cursor: Any, table_name: str, columns: dict[str, str]) -> None:
    if not table_exists(cursor, table_name): return
    existing = get_table_columns(cursor, table_name)
    for col_name, col_def in columns.items():
        if col_name not in existing:
            try:
                cursor.execute(text(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {col_name} {col_def}"))
                logging.info("Added column: %s.%s", table_name, col_name)
            except Exception as e:
                logging.warning("Could not add %s.%s: %s", table_name, col_name, e)

def _ensure_index(cursor: Any, ddl: str) -> None:
    try: cursor.execute(text(ddl))
    except Exception as e:
        if "already exists" not in str(e).lower() and "duplicate" not in str(e).lower(): raise

def _try_advisory_lock(conn_or_session: Any, key: int | str, attempts: int = 10) -> bool:
    for _ in range(max(1, attempts)):
        result = conn_or_session.execute(
            text("SELECT pg_try_advisory_lock(hashtext(:key))" if isinstance(key, str) else "SELECT pg_try_advisory_lock(:key)"),
            {"key": key},
        )
        if bool(result.scalar()): return True
        time.sleep(0.3)
    return False

def _release_advisory_lock(conn_or_session: Any, key: int | str) -> None:
    try:
        conn_or_session.execute(
            text("SELECT pg_advisory_unlock(hashtext(:key))" if isinstance(key, str) else "SELECT pg_advisory_unlock(:key)"),
            {"key": key},
        )
    except Exception: pass

# =============================================================================
# COMPATIBILITY SHIMS & MIGRATION LOGIC
# =============================================================================

def ensure_status_changed_trigger(cursor: Any) -> None:
    cursor.execute(text("""
        CREATE OR REPLACE FUNCTION fn_dq_status_changed_at() RETURNS TRIGGER LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.status IS DISTINCT FROM OLD.status THEN NEW.status_changed_at = CURRENT_TIMESTAMP; END IF;
            RETURN NEW;
        END; $$"""))
    cursor.execute(text("DROP TRIGGER IF EXISTS trg_dq_status_changed_at ON download_queue"))
    cursor.execute(text("CREATE TRIGGER trg_dq_status_changed_at BEFORE UPDATE ON download_queue FOR EACH ROW EXECUTE FUNCTION fn_dq_status_changed_at()"))

def ensure_musicbrainz_release_unique_constraint(cursor: Any) -> None:
    cursor.execute(text("DELETE FROM musicbrainz_releases WHERE id IN (SELECT id FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY release_id ORDER BY updated_at DESC) as rn FROM musicbrainz_releases) t WHERE rn > 1)"))
    cursor.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_musicbrainz_releases_release_id ON musicbrainz_releases (release_id)"))

def ensure_album_artist_column_data() -> bool:
    try:
        with db_session() as session:
            if not table_exists(session, "tracks") or not _try_advisory_lock(session, _ALBUM_ART_DATA_LOCK_KEY, attempts=1): return True
            session.execute(text("UPDATE tracks SET album_artist = artist WHERE album_artist IS NULL"))
            _release_advisory_lock(session, _ALBUM_ART_DATA_LOCK_KEY)
        return True
    except Exception as e:
        logging.error("Backfill failed: %s", e)
        return False

def ensure_artists_name_unique_constraint() -> bool:
    try:
        with db_session() as session:
            if not table_exists(session, "artists"): return False
            session.execute(text("DELETE FROM artists WHERE ctid NOT IN (SELECT DISTINCT ON (name) ctid FROM artists ORDER BY name, id ASC)"))
            session.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS idx_artists_name_unique ON artists (name)"))
        return True
    except Exception as e:
        logging.warning("Unique constraint error: %s", e)
        return False

def ensure_album_art_schema() -> bool:
    try:
        with db_session() as session:
            if not _try_advisory_lock(session, _ALBUM_ART_SCHEMA_LOCK_KEY, attempts=5): return False
            _ensure_table(session, "album_art", TABLES_TO_ENSURE["album_art"])
            session.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_album_art_artist_album ON album_art (artist_name, album_name)"))
            _release_advisory_lock(session, _ALBUM_ART_SCHEMA_LOCK_KEY)
        return True
    except Exception as e:
        logging.error("Album art schema error: %s", e)
        return False

def _ensure_subset(table: str, keys: Iterable[str], registry: dict[str, str]) -> bool:
    try:
        with db_session() as session:
            if table_exists(session, table):
                _ensure_columns(session, table, {k: registry[k] for k in keys if k in registry})
        return True
    except Exception as e:
        logging.warning("Schema sync error for %s: %s", table, e)
        return False

def ensure_album_artist_column() -> bool: return _ensure_subset("tracks", ["album_artist"], TRACK_COLUMNS_TO_ENSURE) and ensure_album_artist_column_data()
def ensure_queue_mbid_columns() -> bool: return _ensure_subset("download_queue", ("release_id", "release_source", "release_mbid", "recording_mbid", "release_year", "matched_file_path", "music_file_path", "match_confidence", "match_method", "metadata", "slskd_username", "slskd_transfer_id", "slskd_state", "slskd_queue_position", "slskd_last_sync_at"), DOWNLOAD_QUEUE_COLUMNS_TO_ENSURE)
def ensure_track_release_year_column() -> bool: return _ensure_subset("tracks", ["release_year"], TRACK_COLUMNS_TO_ENSURE)
def ensure_musicbrainz_album_mbid_column() -> bool: return _ensure_subset("tracks", ["musicbrainz_album_mbid"], TRACK_COLUMNS_TO_ENSURE)
def ensure_writer_column() -> bool: return _ensure_subset("tracks", ["writer"], TRACK_COLUMNS_TO_ENSURE)
def ensure_cover_columns() -> bool: return _ensure_subset("tracks", ("is_cover", "is_cover_reason", "original_cover_artist", "cover_manual_override", "is_live", "is_acoustic", "is_remix"), TRACK_COLUMNS_TO_ENSURE)
def ensure_mood_columns() -> bool: return _ensure_subset("tracks", ("mood", "mood_confidence", "mood_source", "mood_last_updated"), TRACK_COLUMNS_TO_ENSURE)
def ensure_essentia_feature_columns() -> bool: return _ensure_subset("tracks", ("danceability", "essentia_last_updated", "essentia_model_version"), TRACK_COLUMNS_TO_ENSURE)
def ensure_popularity_freeze_columns() -> bool: return _ensure_subset("tracks", ("popularity_frozen", "popularity_frozen_at"), TRACK_COLUMNS_TO_ENSURE)
def ensure_manual_genres_column() -> bool: return _ensure_subset("tracks", ["manual_genres"], TRACK_COLUMNS_TO_ENSURE)
def ensure_verification_columns() -> bool: return _ensure_subset("tracks", ("verification_status", "verification_checked_at", "verification_error"), TRACK_COLUMNS_TO_ENSURE)
def ensure_pending_mb_updates_column() -> bool: return _ensure_subset("tracks", ["pending_mb_updates"], TRACK_COLUMNS_TO_ENSURE)
def ensure_mb_ignored_fields_column() -> bool: return _ensure_subset("tracks", ["mb_ignored_fields"], TRACK_COLUMNS_TO_ENSURE)

# =============================================================================
# MAIN BOOTSTRAP & ENTRY
# =============================================================================

def ensure_full_schema(_db_path: str | None = None) -> bool:
    lock_acquired = False
    try:
        with db_session() as session:
            lock_acquired = _try_advisory_lock(session, _SCHEMA_BOOTSTRAP_LOCK_NAME)
            if not lock_acquired: return False
            for table, ddl in TABLES_TO_ENSURE.items(): _ensure_table(session, table, ddl)
            for table, cols in COLUMN_REGISTRY.items(): _ensure_columns(session, table, cols)
            ensure_status_changed_trigger(session)
            ensure_musicbrainz_release_unique_constraint(session)
            for ddl in INDEXES_TO_ENSURE: _ensure_index(session, ddl)
        ensure_album_artist_column_data()
        ensure_artists_name_unique_constraint()
        ensure_album_art_schema()
        return True
    except Exception as exc:
        if is_transient_pg_startup_error(exc): return False
        logging.error("Bootstrap failed: %s", exc, exc_info=True)
        raise
    finally:
        if lock_acquired:
            with db_session() as session: _release_advisory_lock(session, _SCHEMA_BOOTSTRAP_LOCK_NAME)

def verify_all_tables_exist() -> dict[str, Any]:
    expected = set(TABLES_TO_ENSURE.keys())
    with db_session() as session:
        present = {t for t in expected if table_exists(session, t)}
    return {"ok": expected.issubset(present), "missing": list(expected - present)}

def init_database_and_schema() -> bool:
    if ensure_full_schema():
        verify_all_tables_exist()
        return True
    threading.Thread(target=_run_deferred_startup_migrations, daemon=True, name="deferred-startup-migrations").start()
    return False

def _run_deferred_startup_migrations() -> None:
    """Retry full schema bootstrap after PostgreSQL becomes available."""
    max_wait_seconds = 300
    poll_interval_seconds = 15
    elapsed = 0
    while elapsed < max_wait_seconds:
        time.sleep(poll_interval_seconds)
        elapsed += poll_interval_seconds
        try:
            conn = get_db_connection()
            conn.close()
            break
        except Exception:
            continue
    else:
        logging.warning("PostgreSQL did not become available for deferred schema bootstrap")
        return
    try:
        ensure_full_schema()
        verify_all_tables_exist()
    except Exception as exc:
        logging.error("Deferred schema bootstrap failed: %s", exc, exc_info=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Running PostgreSQL schema bootstrap...")
    immediate = init_database_and_schema()
    print(f"Immediate success: {immediate}")
    print(verify_all_tables_exist())
