"""Popularity persistence repository."""

from __future__ import annotations
import logging
import time
from typing import Dict

from sqlalchemy import text

from db.engine import db_session

logger = logging.getLogger(__name__)

_TRACKS_COLUMN_CACHE: set[str] | None = None
_TRACKS_COLUMN_TYPES_CACHE: Dict[str, str] | None = None

PG_INT_TYPES = {"smallint", "integer", "bigint"}
PG_FLOAT_TYPES = {"real", "double precision", "numeric", "decimal"}
PG_BOOL_TYPES = {"boolean"}

DB_LOCK_MAX_RETRIES = 5
DB_LOCK_BASE_DELAY_SECONDS = 0.25


def get_tracks_table_columns(session=None) -> set[str]:
    global _TRACKS_COLUMN_CACHE
    if _TRACKS_COLUMN_CACHE:
        return _TRACKS_COLUMN_CACHE

    own_session = session is None
    if own_session:
        from db.engine import db_session as _db_session
        with _db_session() as s:
            return _do_get_tracks_table_columns(s)
    return _do_get_tracks_table_columns(session)


def _do_get_tracks_table_columns(session) -> set[str]:
    global _TRACKS_COLUMN_CACHE
    result = session.execute(
        text("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'tracks'
        """)
    )
    _TRACKS_COLUMN_CACHE = {
        str(row[0])
        for row in result.fetchall() or []
    }
    return _TRACKS_COLUMN_CACHE


def get_tracks_table_column_types(session=None) -> Dict[str, str]:
    global _TRACKS_COLUMN_TYPES_CACHE
    if _TRACKS_COLUMN_TYPES_CACHE:
        return _TRACKS_COLUMN_TYPES_CACHE

    own_session = session is None
    if own_session:
        from db.engine import db_session as _db_session
        with _db_session() as s:
            return _do_get_tracks_table_column_types(s)
    return _do_get_tracks_table_column_types(session)


def _do_get_tracks_table_column_types(session) -> Dict[str, str]:
    global _TRACKS_COLUMN_TYPES_CACHE
    result = session.execute(
        text("""
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = 'tracks'
        """)
    )
    _TRACKS_COLUMN_TYPES_CACHE = {
        str(row[0]): str(row[1])
        for row in result.fetchall() or []
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

    Uses db_session for safe connection handling.

    Args:
        track_data: Dictionary of track data to save
        conn: Optional existing connection (deprecated, kept for compatibility)

    Returns:
        True if successfully saved, False otherwise
    """
    if not track_data:
        return False

    def operation():
        with db_session() as session:
            return _execute_save(session, track_data)

    return run_with_db_lock_retry(operation)


def _execute_save(session, track_data: dict) -> bool:
    """Execute the actual save operation with schema validation."""
    columns = get_tracks_table_columns(session)
    types = get_tracks_table_column_types(session)

    data = {
        k: coerce_track_value_for_pg_type(k, v, types.get(k, ""))
        for k, v in track_data.items()
        if k in columns
    }

    if "id" not in data:
        raise ValueError("track must include id")

    keys = list(data.keys())

    named_placeholders = ", ".join([f":{k}" for k in keys])
    update_set = ", ".join(
        [f"{k}=EXCLUDED.{k}" for k in keys if k != "id"]
    )

    query = text(f"""
        INSERT INTO tracks ({', '.join(keys)})
        VALUES ({named_placeholders})
        ON CONFLICT (id)
        DO UPDATE SET {update_set}
    """)

    params = {k: data[k] for k in keys}
    session.execute(query, params)

    return True