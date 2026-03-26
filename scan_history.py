#!/usr/bin/env python3
"""
Scan History Tracker
Tracks individual album scans across different scan types (Navidrome, Popularity, Beets)
"""

import logging
from datetime import datetime
import os
import time
from helpers.db_utils import get_db_connection, _is_postgres_connection, is_postgres_configured


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


def log_album_scan(artist: str, album: str, scan_type: str, tracks_processed: int = 0, status: str = "completed", source: str = ""):
    """
    Log an album scan to the scan_history table with retry logic for database locks.
    
    Args:
        artist: Artist name
        album: Album name
        scan_type: Type of scan ('navidrome', 'popularity', 'singles', 'unified', or 'beets')
        tracks_processed: Number of tracks processed
        status: Status of the scan ('completed', 'error', 'skipped')
        source: Optional source information (e.g., which APIs were used for detection)
    """
    logging.info(f"log_album_scan called: artist='{artist}', album='{album}', type={scan_type}, tracks={tracks_processed}, status={status}")
    
    max_retries = 3
    retry_delay = 0.5  # Start with 500ms delay
    
    for attempt in range(max_retries):
        try:
            conn = get_db_connection()
            placeholder = "%s"
            cursor = conn.cursor()
            
            # Create table if it doesn't exist
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

            # Self-heal: add source column if missing from older schema (e.g. from
            # the original add_scan_history.sql migration that lacked this column).
            try:
                cursor.execute(
                    "ALTER TABLE scan_history ADD COLUMN IF NOT EXISTS source TEXT DEFAULT ''"
                )
            except Exception:
                pass  # Column already exists

            # Insert scan record with explicit timestamp so legacy schemas lacking
            # scan_timestamp defaults still produce valid dashboard times.
            scan_timestamp = datetime.utcnow().isoformat() + "Z"
            cursor.execute("""
                INSERT INTO scan_history (artist, album, scan_type, scan_timestamp, tracks_processed, status, source)
                VALUES ({}, {}, {}, {}, {}, {}, {})
            """.format(placeholder, placeholder, placeholder, placeholder, placeholder, placeholder, placeholder),
            (artist, album, scan_type, scan_timestamp, tracks_processed, status, source))
            
            conn.commit()
            conn.close()
            
            logging.info(f"Successfully logged {scan_type} scan for '{artist}' - '{album}' to scan_history")
            return  # Success, exit function
            
        except Exception as e:
            logging.error(f"Error logging album scan for '{artist}' - '{album}': {e}")
            logging.error(f"DB target={_db_target_description()}")
            return

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
    try:
        conn = get_db_connection()
        placeholder = "%s"
        cursor = conn.cursor()

        # Check if scan_history table exists
        if not _table_exists(cursor, "scan_history"):
            conn.close()
            return []

        # Self-heal: add source column if missing from older schema.
        try:
            cursor.execute(
                "ALTER TABLE scan_history ADD COLUMN IF NOT EXISTS source TEXT DEFAULT ''"
            )
            conn.commit()
        except Exception:
            pass  # Column already exists

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
        return scans
    except Exception as e:
        logging.error(f"Error getting recent scans: {e}")
        logging.error(f"DB target={_db_target_description()}")
        return []
