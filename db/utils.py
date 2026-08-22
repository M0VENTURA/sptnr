"""Database utilities and helpers.

Provides generic row/JSON conversion, SQL dialect helpers, and access
to raw database connections via the SQLAlchemy connection pool.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog
from sqlalchemy.exc import OperationalError

logger = structlog.get_logger(__name__)


def ensure_psycopg2_loaded() -> bool:
    """Check if psycopg2 is available."""
    try:
        import psycopg2  # noqa: F401
        return True
    except ImportError:
        return False


def is_transient_pg_startup_error(exc: BaseException | str) -> bool:
    """Return True for common transient PostgreSQL startup/connectivity errors."""
    if isinstance(exc, OperationalError):
        return True
        
    msg = str(exc or "").lower()
    transient_phrases = (
        "the database system is starting up",
        "the database system is in recovery mode",
        "cannot connect now",
        "terminating connection",
        "timeout expired",
        "connection timed out",
        "could not connect to server",
        "connection refused",
        "server closed the connection unexpectedly",
        "temporarily unavailable",
        "unexpected eof on client connection",
        "recent connection failures are in backoff",
    )
    return any(phrase in msg for phrase in transient_phrases)


def is_postgres_configured() -> bool:
    """Return True if PostgreSQL connection settings are present."""
    from db.engine import db_settings
    return bool(db_settings.database_url or db_settings.pg_host)


def get_db_connection() -> Any:
    """Get a raw DBAPI (psycopg2/sqlite) connection from the SQLAlchemy pool.

    Prefer ``db.engine.db_session`` / ``async_db_session`` for new code.
    This legacy path exists for the few callers that genuinely need a raw
    cursor (DDL migrations, long-running scans with per-row commits).  Use
    ``get_db_connection_raw(reason=...)`` to signal an intentional use so the
    deprecation warning is not logged for known-good callers.
    """
    logger.warning(
        "Legacy raw database connection requested",
        function="get_db_connection",
        recommendation="Use db.engine.get_engine() or db.engine.db_session() instead",
    )

    from db.engine import get_engine
    return get_engine().raw_connection()


def get_db_connection_raw(reason: str = "") -> Any:
    """Get a raw DBAPI connection for an INTENTIONAL raw-cursor use.

    Same as ``get_db_connection`` but marks the caller as a known good use
    (advisory locks that must hold a dedicated connection, long-running scans
    with per-row commits), so the legacy-deprecation warning is not logged
    for every queue cycle.
    """
    from db.engine import get_engine
    return get_engine().raw_connection()


def is_postgres_connection(conn: Any) -> bool:
    """Return True when a connection appears to be a psycopg2 PostgreSQL connection."""
    if conn is None:
        return False
    raw = getattr(conn, "_conn", conn)
    try:
        module_name = raw.__class__.__module__.lower()
        return "psycopg2" in module_name
    except Exception:
        return False


def _is_postgres_connection(conn: Any) -> bool:
    """Backward-compatible alias."""
    logger.warning(
        "Legacy function alias used", 
        function="_is_postgres_connection", 
        recommendation="Use is_postgres_connection instead"
    )
    return is_postgres_connection(conn)


def is_postgres_session(session: Any) -> bool:
    """Return True when a SQLAlchemy session is bound to PostgreSQL."""
    try:
        bind = session.get_bind() if hasattr(session, "get_bind") else getattr(session, "bind", None)
        dialect = getattr(bind, "dialect", None)
        return (getattr(dialect, "name", "") or "").lower() == "postgresql"
    except Exception:
        return False


def interval_minutes_expr(session: Any, delay_bind: str) -> str:
    """Return a SQL fragment adding ``:delay_bind`` minutes to the current time."""
    if is_postgres_session(session):
        return f"CURRENT_TIMESTAMP + ({delay_bind} * INTERVAL '1 minute')"
    return f"datetime('now', '+' || CAST({delay_bind} AS TEXT) || ' minutes')"


def numeric_track_number_expr(session: Any, column: str = "track_number") -> str:
    """Return a portable numeric sort expression for ``column``."""
    col = f"TRIM(COALESCE({column}, ''))"
    if is_postgres_session(session):
        return (
            f"CASE WHEN NULLIF({col}, '') ~ '^\\d+$' "
            f"THEN {col}::integer ELSE 9999 END"
        )
    return (
        f"CASE WHEN {col} <> '' AND {col} GLOB '[0-9]*' "
        f"THEN CAST({col} AS INTEGER) ELSE 9999 END"
    )


def row_get(row: Any, key: str, index: int | None = None, default: Any = None) -> Any:
    """Read a value from dict-like, Row-like, or tuple-like DB rows."""
    if row is None:
        return default
    
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        try:
            return mapping[key]
        except Exception:
            pass
            
    if hasattr(row, "keys"):
        try:
            return row.get(key, default)
        except AttributeError:
            try:
                return row[key]
            except Exception:
                return default
                
    if index is not None:
        try:
            return row[index]
        except (IndexError, KeyError, TypeError):
            return default
            
    return default


def row_first_value(row: Any, default: Any = None) -> Any:
    """Return the first value from a row-like object."""
    if row is None:
        return default
    if isinstance(row, dict):
        for value in row.values():
            return value
        return default
    if hasattr(row, "keys"):
        try:
            for key in row.keys():
                return row[key]
        except Exception:
            return default
    try:
        return row[0]
    except (KeyError, IndexError, TypeError):
        return default


def convert_row_to_json_serializable(obj: Any) -> Any:
    """Convert DB rows and common non-JSON values to JSON-safe values."""
    try:
        from jinja2 import Undefined
    except ImportError:
        Undefined = None

    if obj is None:
        return None
        
    if Undefined is not None and isinstance(obj, Undefined):
        return None
        
    if hasattr(obj, "keys") and not isinstance(obj, dict):
        obj = dict(obj)
        
    if isinstance(obj, dict):
        return {k: convert_row_to_json_serializable(v) for k, v in obj.items()}
        
    if isinstance(obj, (list, tuple, set)):
        return [convert_row_to_json_serializable(v) for v in obj]
        
    if isinstance(obj, datetime):
        return obj.isoformat()
        
    try:
        from decimal import Decimal
        if isinstance(obj, Decimal):
            return float(obj)
    except ImportError:
        pass
        
    return obj


def get_execute_values() -> Any:
    """Return psycopg2.extras.execute_values for legacy bulk inserts."""
    logger.warning(
        "Legacy bulk insert requested", 
        function="get_execute_values", 
        recommendation="Migrate to SQLAlchemy bulk inserts (e.g., session.scalars(insert().values(...)))"
    )
    
    try:
        from psycopg2.extras import execute_values
        return execute_values
    except ImportError:
        raise RuntimeError("psycopg2.extras.execute_values is unavailable.")
