"""Database context managers."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from db.utils import get_db_connection


@contextmanager
def db_cursor(commit: bool = False) -> Iterator[tuple[Any, Any]]:
    """Open a DB connection/cursor and always clean up safely.

    Args:
        commit: Commit the transaction after a successful block when True.

    Yields:
        Tuple of (connection, cursor). Caller may commit manually when
        commit=False.
    """
    conn = get_db_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        yield conn, cursor
        if commit:
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass
