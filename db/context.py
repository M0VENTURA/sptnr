"""Database context managers."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

import structlog

from db.utils import get_db_connection

logger = structlog.get_logger(__name__)


@contextmanager
def db_cursor(commit: bool = False) -> Iterator[tuple[Any, Any]]:
    """Open a raw DBAPI connection/cursor and always clean up safely.

    Note: For new code, prefer `db_session` or `async_db_session` from 
    `db.engine`. This is maintained for legacy operations requiring raw 
    psycopg2 cursors.

    Args:
        commit: Commit the transaction after a successful block when True.

    Yields:
        Tuple of (connection, cursor). Caller may commit manually when
        commit=False.
    """
    logger.warning(
        "Legacy database context manager used", 
        function="db_cursor", 
        recommendation="Migrate to db.engine.db_session or async_db_session"
    )
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        yield conn, cursor
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()
