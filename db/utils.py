"""Core PostgreSQL connection and row utility helpers.

This replaces the old helpers/db_utils.py as the source of truth.
Anything that is not connection handling or generic row/JSON conversion has
been moved into db.schema_helpers or db.repositories.*.
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Any

try:
    import psycopg2  # type: ignore
    import psycopg2.extras  # type: ignore
    from psycopg2.extras import execute_values  # type: ignore

except ImportError:  # pragma: no cover - depends on runtime image
    psycopg2 = None
    execute_values = None


_PG_LAST_FAILURE_MONOTONIC = 0.0
_PG_FAILURE_BACKOFF_SECONDS = float(os.environ.get("PG_FAILURE_BACKOFF_SECONDS", "30"))
_PG_IDLE_IN_TRANSACTION_TIMEOUT_MS = int(os.environ.get("PG_IDLE_IN_TRANSACTION_TIMEOUT_MS", "60000"))
_PG_CONNECT_MAX_ATTEMPTS = int(os.environ.get("PG_CONNECT_MAX_ATTEMPTS", "3"))
_PG_CONNECT_RETRY_DELAYS = (2.0, 5.0)

_PG_TRANSIENT_ERROR_MARKERS = (
    "the database system is starting up",
    "the database system is in recovery mode",
    "cannot connect now",
    "terminating connection due to administrator command",
    "timeout expired",
    "connection timed out",
    "could not connect to server",
    "connection refused",
    "server closed the connection unexpectedly",
    "temporarily unavailable",
    "could not translate host name",
    "name or service not known",
    "temporary failure in name resolution",
    "recent connection failures are in backoff",
)


class AutoRollbackPGConnection:
    """Wrap a psycopg2 connection so close() rolls back before closing.

    This helps avoid leaving PostgreSQL connections idle-in-transaction when
    callers forget to explicitly commit or roll back before closing.
    """

    __slots__ = ("_conn", "_closed")

    def __init__(self, conn: Any) -> None:
        """Store the wrapped psycopg2 connection."""
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_closed", False)

    def close(self) -> None:
        """Rollback any open transaction, then close the wrapped connection."""
        if object.__getattribute__(self, "_closed"):
            return
        object.__setattr__(self, "_closed", True)
        conn = object.__getattribute__(self, "_conn")
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

    def __enter__(self) -> "AutoRollbackPGConnection":
        """Return this wrapper when used as a context manager."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        """Commit on successful context exit; rollback on exception."""
        conn = object.__getattribute__(self, "_conn")
        if exc_type is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        else:
            try:
                conn.commit()
            except Exception:
                pass
        return False

    def __getattr__(self, name: str) -> Any:
        """Proxy unknown attributes to the wrapped connection."""
        return getattr(object.__getattribute__(self, "_conn"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        """Proxy normal attributes to the wrapped connection."""
        if name in {"_conn", "_closed"}:
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_conn"), name, value)


def ensure_psycopg2_loaded() -> bool:
    """Lazy-load psycopg2 if it was not available at module import time."""
    global psycopg2, execute_values
    if psycopg2 is not None:
        return True
    try:
        import psycopg2 as _psycopg2
        import psycopg2.extras  # noqa: F401
        from psycopg2.extras import execute_values as _execute_values
        psycopg2 = _psycopg2
        execute_values = _execute_values
        return True
    except ImportError:
        return False


def is_transient_pg_startup_error(error: Exception | str) -> bool:
    """Return True for common transient PostgreSQL startup/connectivity errors."""
    message = str(error or "").lower()
    return any(marker in message for marker in _PG_TRANSIENT_ERROR_MARKERS)


def is_postgres_configured() -> bool:
    """Return True if PostgreSQL connection settings are present."""
    pg_dsn = (os.environ.get("DATABASE_URL") or os.environ.get("PG_DSN") or "").strip()
    pg_host = (os.environ.get("PG_HOST") or os.environ.get("PGHOST") or "").strip()
    pg_user = (os.environ.get("PG_USER") or os.environ.get("PGUSER") or "").strip()
    pg_database = (os.environ.get("PG_DATABASE") or os.environ.get("PGDATABASE") or "").strip()
    return bool(pg_dsn or (pg_host and pg_user and pg_database))


def _build_pg_options() -> str | None:
    """Build PostgreSQL startup options for new connections."""
    options_parts: list[str] = []
    if _PG_IDLE_IN_TRANSACTION_TIMEOUT_MS > 0:
        options_parts.append(f"-c idle_in_transaction_session_timeout={_PG_IDLE_IN_TRANSACTION_TIMEOUT_MS}")
    return " ".join(options_parts) or None


def get_db_connection() -> AutoRollbackPGConnection:
    """Create and return an AutoRollbackPGConnection PostgreSQL connection."""
    if not ensure_psycopg2_loaded() or psycopg2 is None:
        raise RuntimeError("psycopg2 is not installed.")
    if not is_postgres_configured():
        raise RuntimeError("PostgreSQL is not configured in the environment.")

    global _PG_LAST_FAILURE_MONOTONIC
    now = time.monotonic()
    if _PG_LAST_FAILURE_MONOTONIC > 0:
        elapsed = now - _PG_LAST_FAILURE_MONOTONIC
        if elapsed < _PG_FAILURE_BACKOFF_SECONDS:
            remaining = int(_PG_FAILURE_BACKOFF_SECONDS - elapsed)
            raise RuntimeError(f"PostgreSQL recent connection failures are in backoff for another ~{remaining}s")

    pg_dsn = os.environ.get("DATABASE_URL") or os.environ.get("PG_DSN")
    pg_host = os.environ.get("PG_HOST") or os.environ.get("PGHOST") or ""
    pg_port = int(os.environ.get("PG_PORT") or os.environ.get("PGPORT") or "5432")
    pg_user = os.environ.get("PG_USER") or os.environ.get("PGUSER") or ""
    pg_password = os.environ.get("PG_PASSWORD") or os.environ.get("PGPASSWORD") or ""
    pg_database = os.environ.get("PG_DATABASE") or os.environ.get("PGDATABASE") or "popularr"
    options = _build_pg_options()

    last_exc: Exception = RuntimeError("PostgreSQL connection failed: no attempts made")
    for attempt in range(_PG_CONNECT_MAX_ATTEMPTS):
        try:
            connect_kwargs: dict[str, Any] = {
                "cursor_factory": psycopg2.extras.RealDictCursor,
                "connect_timeout": 10,
                "keepalives": 1,
                "keepalives_idle": 60,
                "keepalives_interval": 10,
                "keepalives_count": 5,
            }
            if options:
                connect_kwargs["options"] = options
            if pg_dsn:
                raw = psycopg2.connect(pg_dsn, **connect_kwargs)
            else:
                raw = psycopg2.connect(
                    host=pg_host,
                    port=pg_port,
                    user=pg_user,
                    password=pg_password,
                    dbname=pg_database,
                    **connect_kwargs,
                )
            _PG_LAST_FAILURE_MONOTONIC = 0.0
            return AutoRollbackPGConnection(raw)
        except Exception as exc:
            last_exc = exc
            if is_transient_pg_startup_error(exc) and attempt < (_PG_CONNECT_MAX_ATTEMPTS - 1):
                delay = _PG_CONNECT_RETRY_DELAYS[min(attempt, len(_PG_CONNECT_RETRY_DELAYS) - 1)]
                time.sleep(delay)
                continue
            break

    _PG_LAST_FAILURE_MONOTONIC = time.monotonic()
    raise RuntimeError(f"PostgreSQL connection failed: {last_exc}") from last_exc


def is_postgres_connection(conn: Any) -> bool:
    """Return True when a connection appears to be a psycopg2 PostgreSQL connection."""
    raw = getattr(conn, "_conn", conn)
    module_name = raw.__class__.__module__.lower()
    return "psycopg2" in module_name


def _is_postgres_connection(conn: Any) -> bool:
    """Backward-compatible alias for older imports."""
    return is_postgres_connection(conn)


def row_get(row: Any, key: str, index: int | None = None, default: Any = None) -> Any:
    """Read a value from dict-like or tuple/list-like DB rows."""
    if row is None:
        return default
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
    # Jinja2 template Undefined objects (a missing context variable leaked into
    # data that gets JSON-serialised) are not JSON-serializable — the previous
    # import of ``Undefined`` here was never used, so ``json.dumps`` raised
    # "Object of type Undefined is not JSON serializable".
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


def get_execute_values():
    """Return psycopg2.extras.execute_values, loading psycopg2 if required."""
    if not ensure_psycopg2_loaded() or execute_values is None:
        raise RuntimeError("psycopg2.extras.execute_values is unavailable.")
    return execute_values
