#!/usr/bin/env python3
"""
Scan History Tracker
Tracks individual album scans across different scan types (Navidrome, Popularity, Beets)
"""

import logging
from datetime import datetime
import os
import random
import time
from helpers.db_utils import get_db_connection, _is_postgres_connection, is_postgres_configured

# Module-level flag: True once we have confirmed (or added) the source column so we
# don't query information_schema on every call.  Reset to False if the process
# reconnects to a fresh database.
_scan_history_source_column_ensured = False

# Module-level flag: True once the full schema setup (CREATE TABLE + indexes) has
# been committed in this process.  Avoids running DDL inside the same transaction
# as INSERT, which causes ShareLock/RowExclusiveLock deadlocks under concurrent load.
_scan_history_schema_ensured = False
_recent_scans_cache = []
_recent_scans_last_ok_ts = 0.0
_recent_scans_last_error_log_ts = 0.0


def _is_transient_db_error(exc: Exception) -> bool:
    """Best-effort classification for short-lived DB connectivity faults."""
    msg = str(exc).lower()
    transient_markers = (
        "timeout expired",
        "read timed out",
        "temporary failure in name resolution",
        "could not translate host name",
        "connection refused",
        "connection reset",
        "server closed the connection unexpectedly",
    )
    return any(marker in msg for marker in transient_markers)


def _get_db_path():
    """
    Determine the correct database path with fallback logic.
    
    Priority:
    1. DB_PATH environment variable
    2. /database/sptnr.db (Docker default) if directory exists or can be created
    3. ./database/sptnr.db (relative path for local development)
    """
    # Check environment variable first
    env_path = os.environ.get("DB_PATH")
    if env_path:
        return env_path
    
    # Try Docker default path
    docker_path = "/database/sptnr.db"
    docker_dir = os.path.dirname(docker_path)
    
    # Check if Docker directory exists or can be created
    if os.path.exists(docker_dir):
        return docker_path
    
    try:
        os.makedirs(docker_dir, exist_ok=True)
        return docker_path
    except (PermissionError, OSError):
        # Can't create Docker directory, fall back to relative path
        pass
    
    # Use relative path (for local development/testing)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    local_path = os.path.join(script_dir, "database", "sptnr.db")
    
    # Ensure local database directory exists
    local_dir = os.path.dirname(local_path)
    os.makedirs(local_dir, exist_ok=True)
    
    return local_path


DB_PATH = _get_db_path()


def _db_target_description() -> str:
    """Describe the configured database target for clearer error logging."""
    if is_postgres_configured():
        pg_dsn = (os.environ.get("DATABASE_URL") or os.environ.get("PG_DSN") or "").strip()
        if pg_dsn:
            return "PostgreSQL via DATABASE_URL/PG_DSN"

        pg_host = (os.environ.get("PG_HOST") or "").strip() or "<unset>"
        pg_port = (os.environ.get("PG_PORT") or "5432").strip()
        pg_database = (os.environ.get("PG_DATABASE") or "sptnr").strip()
        return f"PostgreSQL {pg_host}:{pg_port}/{pg_database}"

    return "PostgreSQL (not configured)"


def _table_exists(cursor, table_name: str) -> bool:
    cursor.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = current_schema() AND table_name = %s
        )
        """,
        (table_name,)
    )
    row = cursor.fetchone()
    if isinstance(row, dict):
        return bool(row.get("exists"))
    return bool(row[0]) if row else False


def _ensure_scan_history_schema():
    """
    Create the scan_history table, indexes, and any missing columns in a dedicated
    committed transaction.  Using a separate transaction prevents the DDL locks
    (ShareLock from CREATE INDEX, AccessExclusiveLock from ALTER TABLE) from
    co-existing with the RowExclusiveLock taken by the INSERT in log_album_scan.
    When multiple workers run concurrently, mixing those lock types in the same
    transaction causes deadlocks.

    A module-level flag ensures the DDL is only issued once per process lifetime.
    """
    global _scan_history_schema_ensured, _scan_history_source_column_ensured
    if _scan_history_schema_ensured:
        return

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS scan_history (
                id BIGSERIAL PRIMARY KEY,
                artist TEXT NOT NULL,
                album TEXT NOT NULL,
                scan_type TEXT NOT NULL,
                scan_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                tracks_processed INTEGER DEFAULT 0,
                status TEXT DEFAULT 'completed',
                source TEXT DEFAULT ''
            )
            """
        )

        # Self-heal legacy PostgreSQL schemas where `id` exists but has no default.
        cursor.execute(
            """
            SELECT column_default
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'scan_history'
              AND column_name = 'id'
            """
        )
        default_row = cursor.fetchone()
        if isinstance(default_row, dict):
            id_default = default_row.get("column_default")
        else:
            id_default = default_row[0] if default_row else None

        if not id_default:
            cursor.execute("CREATE SEQUENCE IF NOT EXISTS scan_history_id_seq")
            cursor.execute("SELECT COALESCE(MAX(id), 0) AS max_id FROM scan_history")
            max_id_row = cursor.fetchone()
            if isinstance(max_id_row, dict):
                max_id = int(max_id_row.get("max_id") or 0)
            else:
                max_id = int(max_id_row[0] if max_id_row else 0)

            cursor.execute("SELECT setval('scan_history_id_seq', %s, %s)", (max_id, max_id > 0))
            cursor.execute(
                """
                ALTER TABLE scan_history
                ALTER COLUMN id SET DEFAULT nextval('scan_history_id_seq')
                """
            )
            cursor.execute("ALTER SEQUENCE scan_history_id_seq OWNED BY scan_history.id")
            logging.info("scan_history.id default was missing; attached scan_history_id_seq")

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_scan_history_timestamp
            ON scan_history(scan_timestamp DESC)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_scan_history_artist_album
            ON scan_history(artist, album)
        """)

        # Self-heal: add source column if missing from older schema.
        if not _scan_history_source_column_ensured:
            cursor.execute(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'scan_history'
                  AND column_name = 'source'
                """
            )
            if cursor.fetchone():
                _scan_history_source_column_ensured = True
            else:
                try:
                    cursor.execute(
                        "ALTER TABLE scan_history ADD COLUMN IF NOT EXISTS source TEXT DEFAULT ''"
                    )
                    _scan_history_source_column_ensured = True
                except Exception:
                    pass  # Added concurrently by another worker

        conn.commit()
        _scan_history_schema_ensured = True
    except Exception as e:
        logging.warning(f"scan_history schema setup failed (will retry next call): {e}")
        try:
            if conn:
                conn.rollback()
        except Exception:
            pass
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


def log_album_scan(artist: str, album: str, scan_type: str, tracks_processed: int = 0, status: str = "completed", source: str = ""):
    """
    Log an album scan to the scan_history table with retry logic for deadlocks and
    transient database errors.

    Args:
        artist: Artist name
        album: Album name
        scan_type: Type of scan ('navidrome', 'popularity', 'singles', 'unified', or 'beets')
        tracks_processed: Number of tracks processed
        status: Status of the scan ('completed', 'error', 'skipped')
        source: Optional source information (e.g., which APIs were used for detection)
    """
    logging.info(f"log_album_scan called: artist='{artist}', album='{album}', type={scan_type}, tracks={tracks_processed}, status={status}")

    # Ensure schema is ready in its own committed transaction before we INSERT.
    # Keeping DDL and DML in separate transactions eliminates the ShareLock /
    # RowExclusiveLock deadlock that occurs when many workers call this concurrently.
    _ensure_scan_history_schema()

    max_retries = 3
    retry_delay = 0.5  # seconds; doubled on each retry

    for attempt in range(max_retries):
        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor()

            # Insert scan record with explicit timestamp so legacy schemas lacking
            # scan_timestamp defaults still produce valid dashboard times.
            scan_timestamp = datetime.utcnow().isoformat() + "Z"
            cursor.execute(
                """
                INSERT INTO scan_history (artist, album, scan_type, scan_timestamp, tracks_processed, status, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (artist, album, scan_type, scan_timestamp, tracks_processed, status, source),
            )

            conn.commit()
            logging.info(f"Successfully logged {scan_type} scan for '{artist}' - '{album}' to scan_history")
            return  # Success

        except Exception as e:
            try:
                if conn:
                    conn.rollback()
            except Exception:
                pass

            err_str = str(e).lower()
            is_deadlock = "deadlock" in err_str
            is_transient = _is_transient_db_error(e)

            if attempt < max_retries - 1 and (is_deadlock or is_transient):
                jitter = random.uniform(0, retry_delay)
                sleep_time = retry_delay + jitter
                logging.warning(
                    f"log_album_scan retrying (attempt {attempt + 2}/{max_retries}) "
                    f"after {'deadlock' if is_deadlock else 'transient error'} "
                    f"for '{artist}' - '{album}': {e}; sleeping {sleep_time:.2f}s"
                )
                time.sleep(sleep_time)
                retry_delay *= 2
            else:
                logging.error(f"Error logging album scan for '{artist}' - '{album}': {e}")
                logging.error(f"DB target={_db_target_description()}")
                return
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

def was_album_scanned(artist: str, album: str, scan_type: str, days_threshold: int = None) -> bool:
    """
    Check if an album was already successfully scanned by a specific scan type.
    
    Args:
        artist: Artist name
        album: Album name
        scan_type: Type of scan to check ('navidrome', 'popularity', 'singles', 'unified', 'beets')
        days_threshold: Optional number of days to check. If provided, returns True only if scanned 
                       within the last N days. If None, checks if ever scanned (legacy behavior).
        
    Returns:
        True if album was already successfully scanned (within days_threshold if provided), False otherwise
    """
    try:
        conn = get_db_connection()
        placeholder = "%s"
        cursor = conn.cursor()
        
        # Check if scan_history table exists
        if not _table_exists(cursor, "scan_history"):
            # Table doesn't exist yet, assume not scanned
            conn.close()
            return False
        
        # Check for successful scans of this album with this scan type
        # Using LIMIT 1 for efficiency - we only need to know if any record exists
        if days_threshold is not None:
            # Time-based check: only consider scans within the last N days
            cursor.execute(
                f"""
                SELECT 1 FROM scan_history
                WHERE artist = {placeholder} AND album = {placeholder} AND scan_type = {placeholder} AND status = 'completed'
                AND scan_timestamp > (CURRENT_TIMESTAMP - ({placeholder} || ' days')::interval)
                LIMIT 1
                """,
                (artist, album, scan_type, str(days_threshold))
            )
        else:
            # Legacy behavior: check if ever scanned
            cursor.execute(
                f"""
                SELECT 1 FROM scan_history
                WHERE artist = {placeholder} AND album = {placeholder} AND scan_type = {placeholder} AND status = 'completed'
                LIMIT 1
                """,
                (artist, album, scan_type)
            )
        
        result = cursor.fetchone()
        conn.close()
        
        return result is not None
    except Exception as e:
        logging.error(f"Error checking album scan history: {e}")
        logging.error(f"DB target={_db_target_description()}")
        # Return False on error to ensure albums will be scanned even if there's a database error,
        # preventing data loss at the cost of potential duplicate scans
        return False

def get_recent_album_scans(limit: int = 10):
    """
    Get recent album scans with scan type information.

    Args:
        limit: Maximum number of scans to return

    Returns:
        List of dicts with scan information
    """
    global _recent_scans_cache, _recent_scans_last_ok_ts, _recent_scans_last_error_log_ts

    max_attempts = 2
    last_error = None

    for attempt in range(max_attempts):
        try:
            conn = get_db_connection()
            placeholder = "%s"
            cursor = conn.cursor()

            # Check if scan_history table exists
            if not _table_exists(cursor, "scan_history"):
                conn.close()
                _recent_scans_cache = []
                _recent_scans_last_ok_ts = time.time()
                return []

            # Self-heal: add source column if missing from older schema.
            # Use the module-level flag so information_schema is only queried once
            # per process (same logic as log_album_scan).
            global _scan_history_source_column_ensured
            if not _scan_history_source_column_ensured:
                cursor.execute(
                    """
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'scan_history'
                      AND column_name = 'source'
                    """
                )
                if cursor.fetchone():
                    _scan_history_source_column_ensured = True
                else:
                    try:
                        cursor.execute(
                            "ALTER TABLE scan_history ADD COLUMN IF NOT EXISTS source TEXT DEFAULT ''"
                        )
                        conn.commit()
                        _scan_history_source_column_ensured = True
                    except Exception:
                        pass  # Added concurrently by another worker

            # PostgreSQL puts NULLs FIRST on DESC by default; old rows
            # inserted before the explicit-timestamp fix (a5abcbb Mar 16)
            # have NULL scan_timestamp and float to the top, hiding newer entries.
            order_clause = "NULLS LAST"
            cursor.execute(
                f"""
                SELECT artist, album, scan_type, scan_timestamp, tracks_processed, status, source
                FROM scan_history
                WHERE status != 'skipped'
                ORDER BY scan_timestamp DESC {order_clause}
                LIMIT {placeholder}
                """,
                (limit,)
            )

            scans = []
            for row in cursor.fetchall():
                raw_ts = row["scan_timestamp"] if hasattr(row, "keys") else row[3]
                if hasattr(raw_ts, "isoformat"):
                    scan_ts = raw_ts.isoformat()
                elif raw_ts is not None:
                    scan_ts = str(raw_ts).replace(" ", "T", 1)
                else:
                    scan_ts = ""
                keys = row.keys() if hasattr(row, "keys") else None
                scans.append({
                    "artist": row["artist"] if keys else row[0],
                    "album": row["album"] if keys else row[1],
                    "scan_type": row["scan_type"] if keys else row[2],
                    "scan_timestamp": scan_ts,
                    "tracks_processed": row["tracks_processed"] if keys else row[4],
                    "status": row["status"] if keys else row[5],
                    "source": (row["source"] if keys and "source" in keys else (row[6] if not keys and len(row) > 6 else ""))
                })

            conn.close()
            _recent_scans_cache = scans
            _recent_scans_last_ok_ts = time.time()
            return scans
        except Exception as e:
            last_error = e
            if attempt < (max_attempts - 1) and _is_transient_db_error(e):
                time.sleep(0.25)
                continue
            break

    now = time.time()
    transient = _is_transient_db_error(last_error) if last_error else False
    if transient:
        # Throttle noisy transient connectivity logs to at most once every 30s.
        if (now - _recent_scans_last_error_log_ts) >= 30.0:
            logging.warning(f"Transient error getting recent scans: {last_error}")
            logging.warning(f"DB target={_db_target_description()}")
            _recent_scans_last_error_log_ts = now
        return (_recent_scans_cache or [])[:max(0, int(limit))]

    logging.error(f"Error getting recent scans: {last_error}")
    logging.error(f"DB target={_db_target_description()}")
    return (_recent_scans_cache or [])[:max(0, int(limit))]
