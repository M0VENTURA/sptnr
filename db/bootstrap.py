"""Full PostgreSQL schema bootstrap for Popularr."""

from __future__ import annotations

import threading
import time
from typing import Any

import structlog
from sqlalchemy import text

from db.engine import db_session, run_migrations_on_startup
from db.schema import COLUMN_REGISTRY, INDEXES_TO_ENSURE, TABLES_TO_ENSURE
from db.schema_helpers import get_postgres_column_types, get_table_columns, table_exists
from db.utils import get_db_connection, is_transient_pg_startup_error

logger = structlog.get_logger(__name__)

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
        if "already exists" not in str(exc).lower() and "duplicate" not in str(exc).lower():
            raise
    finally:
        try: cursor.execute(text("RELEASE SAVEPOINT popularr_schema_table_create"))
        except Exception: pass

def _ensure_columns(cursor: Any, table_name: str, columns: dict[str, str]) -> None:
    if not table_exists(cursor, table_name): 
        return
    
    existing = get_table_columns(cursor, table_name)
    for col_name, col_def in columns.items():
        if col_name not in existing:
            try:
                cursor.execute(text(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {col_name} {col_def}"))
                logger.info("Added column", table=table_name, column=col_name)
            except Exception as e:
                logger.warning("Could not add column", table=table_name, column=col_name, error=str(e))

def _ensure_index(cursor: Any, ddl: str) -> None:
    try: cursor.execute(text(ddl))
    except Exception as e:
        if "already exists" not in str(e).lower() and "duplicate" not in str(e).lower():
            raise

def _try_advisory_lock(conn_or_session: Any, key: int | str, attempts: int = 10) -> bool:
    for _ in range(max(1, attempts)):
        query = "SELECT pg_try_advisory_lock(hashtext(:key))" if isinstance(key, str) else "SELECT pg_try_advisory_lock(:key)"
        result = conn_or_session.execute(text(query), {"key": key})
        if bool(result.scalar()): 
            return True
        time.sleep(0.3)
    return False

def _release_advisory_lock(conn_or_session: Any, key: int | str) -> None:
    try:
        query = "SELECT pg_advisory_unlock(hashtext(:key))" if isinstance(key, str) else "SELECT pg_advisory_unlock(:key)"
        conn_or_session.execute(text(query), {"key": key})
    except Exception: 
        pass

# =============================================================================
# DATA FIXUPS & MIGRATION LOGIC
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
            if not table_exists(session, "tracks") or not _try_advisory_lock(session, _ALBUM_ART_DATA_LOCK_KEY, attempts=1): 
                return True
            session.execute(text("UPDATE tracks SET album_artist = artist WHERE album_artist IS NULL"))
            _release_advisory_lock(session, _ALBUM_ART_DATA_LOCK_KEY)
        return True
    except Exception as e:
        logger.error("Backfill failed", error=str(e))
        return False

def ensure_artists_name_unique_constraint() -> bool:
    try:
        with db_session() as session:
            if not table_exists(session, "artists"): return False
            session.execute(text("DELETE FROM artists WHERE ctid NOT IN (SELECT DISTINCT ON (name) ctid FROM artists ORDER BY name, id ASC)"))
            session.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS idx_artists_name_unique ON artists (name)"))
        return True
    except Exception as e:
        logger.warning("Unique constraint error", error=str(e))
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
        logger.error("Album art schema error", error=str(e))
        return False

def ensure_upcoming_releases_schema() -> bool:
    try:
        with db_session() as session:
            if not table_exists(session, "upcoming_releases"): return True
            
            session.execute(text("""
                DELETE FROM upcoming_releases a
                USING upcoming_releases b
                WHERE a.id <> b.id
                  AND a.artist_name = b.artist_name
                  AND a.album_name = b.album_name
                  AND a.source <> 'MusicBrainz Daily Collection'
                  AND b.source = 'MusicBrainz Daily Collection'
            """))
            session.execute(text("""
                DELETE FROM upcoming_releases a
                USING upcoming_releases b
                WHERE a.id > b.id
                  AND a.artist_name = b.artist_name
                  AND a.album_name = b.album_name
                  AND a.source = b.source
            """))
            try: session.execute(text("ALTER TABLE upcoming_releases DROP CONSTRAINT IF EXISTS uq_upcoming_artist_album_source"))
            except Exception: pass
            
            session.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_upcoming_artist_album ON upcoming_releases (artist_name, album_name)"))
            session.execute(text("UPDATE upcoming_releases SET last_seen_at = COALESCE(last_seen_at, updated_at, created_at, CURRENT_TIMESTAMP)"))
        return True
    except Exception as e:
        logger.error("Upcoming releases schema error", error=str(e))
        return False

def migrate_queue_source_data() -> bool:
    try:
        with db_session() as session:
            if not table_exists(session, "download_queue"): return True
            cols = get_table_columns(session, "download_queue")
            if "source" in cols and "source_id" in cols:
                session.execute(text(
                    "UPDATE download_queue SET source = source_id "
                    "WHERE (source IS NULL OR source = '') "
                    "AND source_id IS NOT NULL AND source_id != ''"
                ))
        return True
    except Exception as e:
        logger.warning("Queue source column backfill error", error=str(e))
        return False

def migrate_single_confidence_type() -> bool:
    try:
        with db_session() as session:
            if not table_exists(session, "tracks"): return True
            if "single_confidence" not in get_table_columns(session, "tracks"): return True
            
            types = get_postgres_column_types(session, "tracks", ["single_confidence"])
            current_type = (types.get("single_confidence") or "").lower()
            numeric_types = {"smallint", "integer", "bigint", "real", "double precision", "numeric", "decimal"}
            
            if current_type in numeric_types:
                session.execute(text("""
                    ALTER TABLE tracks
                    ALTER COLUMN single_confidence TYPE TEXT
                    USING (
                        CASE
                            WHEN single_confidence IS NULL THEN NULL
                            WHEN single_confidence >= 0.9 THEN 'high'
                            WHEN single_confidence >= 0.5 THEN 'medium'
                            ELSE 'low'
                        END
                    )
                """))
                logger.info("Migrated tracks.single_confidence to TEXT", previous_type=current_type)
        return True
    except Exception as exc:
        logger.warning("Could not migrate single_confidence column", error=str(exc))
        return False

# =============================================================================
# MAIN BOOTSTRAP & ENTRY
# =============================================================================

def ensure_full_schema() -> bool:
    lock_acquired = False
    try:
        with db_session() as session:
            lock_acquired = _try_advisory_lock(session, _SCHEMA_BOOTSTRAP_LOCK_NAME)
            if not lock_acquired: return False
            
            # 1. Base Tables
            for table, ddl in TABLES_TO_ENSURE.items(): 
                _ensure_table(session, table, ddl)
            
            # 2. Base Columns
            for table, cols in COLUMN_REGISTRY.items(): 
                _ensure_columns(session, table, cols)
            
            # 3. Triggers, Constraints & Indexes
            ensure_status_changed_trigger(session)
            ensure_musicbrainz_release_unique_constraint(session)
            for ddl in INDEXES_TO_ENSURE: 
                _ensure_index(session, ddl)
                
        # 4. Independent Data Fixups & Migrations
        ensure_album_artist_column_data()
        ensure_artists_name_unique_constraint()
        ensure_album_art_schema()
        ensure_upcoming_releases_schema()
        
        # Type alterations and data backfills
        migrate_single_confidence_type()
        migrate_queue_source_data()
        
        return True
    except Exception as exc:
        if is_transient_pg_startup_error(exc): return False
        logger.error("Bootstrap failed", exc_info=True)
        raise
    finally:
        if lock_acquired:
            with db_session() as session: _release_advisory_lock(session, _SCHEMA_BOOTSTRAP_LOCK_NAME)

def verify_all_tables_exist() -> dict[str, Any]:
    expected = set(TABLES_TO_ENSURE.keys())
    with db_session() as session:
        present = {t for t in expected if table_exists(session, t)}
    return {"ok": expected.issubset(present), "missing": list(expected - present)}

def _reset_stale_scan_states() -> None:
    try:
        from services.scanning.scan_state import reset_stale_scan_states
        reset_stale_scan_states()
    except Exception as exc:
        logger.warning("Stale scan-state reset skipped", error=str(exc))

def _prune_genre_playlists_at_boot() -> None:
    def _run() -> None:
        try:
            from services.popularity.stages.finalise_stage import prune_genre_playlists_for_deletion
            prune_genre_playlists_for_deletion()
        except Exception as exc:
            logger.warning("Genre playlist prune skipped at boot", error=str(exc))

    threading.Thread(target=_run, daemon=True, name="boot-genre-playlist-prune").start()

def init_database_and_schema() -> bool:
    max_attempts = 4
    for attempt in range(1, max_attempts + 1):
        try:
            if ensure_full_schema():
                verify_all_tables_exist()
                
                # Automatically apply any newer Alembic migrations on top of the legacy bootstrap
                run_migrations_on_startup()
                
                _reset_stale_scan_states()
                _prune_genre_playlists_at_boot()
                return True
        except Exception as exc:
            msg = str(exc)
            if attempt < max_attempts and ("shutting down" in msg or "database system is shutting down" in msg):
                logger.warning("Postgres not ready yet, retrying...", attempt=attempt, max_attempts=max_attempts)
                time.sleep(5)
                continue
            logger.warning("Schema bootstrap failed", attempt=attempt, max_attempts=max_attempts, error=msg)
            break
        break

    threading.Thread(target=_run_deferred_startup_migrations, daemon=True, name="deferred-startup-migrations").start()
    return False

def _run_deferred_startup_migrations() -> None:
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
        logger.warning("PostgreSQL did not become available for deferred schema bootstrap")
        return
    try:
        ensure_full_schema()
        verify_all_tables_exist()
        run_migrations_on_startup()
        _reset_stale_scan_states()
        _prune_genre_playlists_at_boot()
    except Exception as exc:
        logger.error("Deferred schema bootstrap failed", exc_info=True)


if __name__ == "__main__":
    print("")
    print("── PostgreSQL Schema Bootstrap ──────────────────────────────")
    immediate = init_database_and_schema()
    if immediate:
        print("  ✓ Schema tables created/verified & Migrations Applied")
    else:
        print("  ⚠ Partial schema bootstrap — some tables may be deferred")

    result = verify_all_tables_exist()
    if result.get("ok"):
        print(f"  ✓ All {len(COLUMN_REGISTRY)} table groups verified")
    else:
        missing = result.get("missing", [])
        print(f"  ⚠ Missing tables (will be created on first use): {', '.join(missing)}")
    print("────────────────────────────────────────────────────────────")
