"""Popularity persistence repository."""

from __future__ import annotations
import logging
import time
from typing import Dict

from db.utils import get_db_connection, row_get
from db.context import db_cursor

logger = logging.getLogger(__name__)

_TRACKS_COLUMN_CACHE: set[str] | None = None
_TRACKS_COLUMN_TYPES_CACHE: Dict[str, str] | None = None

PG_INT_TYPES = {"smallint", "integer", "bigint"}
PG_FLOAT_TYPES = {"real", "double precision", "numeric", "decimal"}
PG_BOOL_TYPES = {"boolean"}

DB_LOCK_MAX_RETRIES = 5
DB_LOCK_BASE_DELAY_SECONDS = 0.25


@contextmanager
def get_db_connection_context(conn=None):
    db_conn = conn or get_db_connection()
    try:
        yield db_conn
    finally:
        if conn is None:
            db_conn.close()


def get_tracks_table_columns(cursor) -> set[str]:
    global _TRACKS_COLUMN_CACHE
    if _TRACKS_COLUMN_CACHE:
        return _TRACKS_COLUMN_CACHE

    cursor.execute("""
        SELECT column_name 
        FROM information_schema.columns 
        WHERE table_name = 'tracks'
    """)

    _TRACKS_COLUMN_CACHE = {
        row_get(row, "column_name", 0)
        for row in cursor.fetchall() or []
    }

    return _TRACKS_COLUMN_CACHE


def get_tracks_table_column_types(cursor) -> Dict[str, str]:
    global _TRACKS_COLUMN_TYPES_CACHE
    if _TRACKS_COLUMN_TYPES_CACHE:
        return _TRACKS_COLUMN_TYPES_CACHE

    cursor.execute("""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_name = 'tracks'
    """)

    _TRACKS_COLUMN_TYPES_CACHE = {
        row_get(row, "column_name", 0): row_get(row, "data_type", 1)
        for row in cursor.fetchall() or []
    }

    return _TRACKS_COLUMN_TYPES_CACHE


def coerce_track_value_for_pg_type(column: str, value, pg_type: str):
    if value is None:
        return None

    pg_type = (pg_type or "").lower()

    if pg_type in PG_BOOL_TYPES:
        return bool(value)
    if pg_type in PG_INT_TYPES:
        return int(value) if value != "" else None
    if pg_type in PG_FLOAT_TYPES:
        return float(value) if value != "" else None

    return value


def run_with_db_lock_retry(operation):
    for attempt in range(DB_LOCK_MAX_RETRIES):
        try:
            return operation()
        except Exception as exc:
            if "lock" in str(exc).lower() and attempt < DB_LOCK_MAX_RETRIES - 1:
                time.sleep(DB_LOCK_BASE_DELAY_SECONDS * (attempt + 1))
                continue
            raise


def save_to_db(track_data: dict, conn=None) -> bool:
    """Save or update a track in the database.
    
    Uses db_cursor context manager for safe connection handling.
    
    Args:
        track_data: Dictionary of track data to save
        conn: Optional existing connection (if provided, won't be closed)
        
    Returns:
        True if successfully saved, False otherwise
    """
    if not track_data:
        return False

    def operation():
        # Use provided connection or create new one via context manager
        if conn is not None:
            cursor = conn.cursor()
            try:
                return _execute_save(cursor, track_data, conn)
            finally:
                cursor.close()
        else:
            with db_cursor(commit=True) as (db_conn, cursor):
                return _execute_save(cursor, track_data, db_conn)
    
    return run_with_db_lock_retry(operation)


def _execute_save(cursor, track_data: dict, conn) -> bool:
    """Execute the actual save operation with schema validation."""
    columns = get_tracks_table_columns(cursor)
    types = get_tracks_table_column_types(cursor)

    data = {
        k: coerce_track_value_for_pg_type(k, v, types.get(k, ""))
        for k, v in track_data.items()
        if k in columns
    }

    if "id" not in data:
        raise ValueError("track must include id")

    keys = list(data.keys())

    placeholders = ", ".join(["%s"] * len(keys))
    updates = ", ".join(
        [f"{k}=EXCLUDED.{k}" for k in keys if k != "id"]
    )

    query = f"""
        INSERT INTO tracks ({', '.join(keys)})
        VALUES ({placeholders})
        ON CONFLICT (id)
        DO UPDATE SET {updates}
    """

    cursor.execute(query, [data[k] for k in keys])
    
    # Only commit if not using external connection
    if conn and hasattr(conn, 'commit'):
        conn.commit()

    return True