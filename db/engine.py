"""SQLAlchemy engine, session factory, and helper utilities.

Provides both synchronous (``Session`` / ``db_session``) and asynchronous
(``AsyncSession`` / ``async_session_factory``) session management for the
Popularr database.

Usage (sync — existing Flask routes)::

    from db.engine import db_session

    with db_session() as session:
        tracks = session.query(Track).all()

Usage (async — future Quart/FastAPI routes)::

    from db.engine import async_session_factory

    async with async_session_factory() as session:
        tracks = await session.execute(select(Track))

Environment variables:
    DATABASE_URL  – Full connection string (overrides all PG_* vars)
    PG_HOST       – PostgreSQL host (default: localhost)
    PG_PORT       – PostgreSQL port (default: 5432)
    PG_USER       – PostgreSQL user (default: popularr)
    PG_PASSWORD   – PostgreSQL password
    PG_DATABASE   – PostgreSQL database name (default: popularr)
    DB_PATH       – SQLite fallback path (default: /database/popularr.db)
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool, QueuePool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Base model
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    """Declarative base for all Popularr ORM models."""
    pass


# ---------------------------------------------------------------------------
# Connection string resolution
# ---------------------------------------------------------------------------

def _resolve_database_url() -> str:
    """Build the database connection string from environment variables.

    Returns a PostgreSQL URL if PG_* vars are set.
    Falls back to SQLite ONLY when no PostgreSQL configuration exists at
    all — this covers the first-run / setup-wizard scenario.  If PG_HOST
    is set but unreachable the error propagates (fail fast).
    """
    explicit = os.environ.get("DATABASE_URL", "").strip()
    if explicit:
        return explicit

    pg_host = os.environ.get("PG_HOST", "").strip()
    if pg_host:
        pg_port = os.environ.get("PG_PORT", "5432").strip()
        pg_user = os.environ.get("PG_USER", "popularr").strip()
        pg_pass = os.environ.get("PG_PASSWORD", "").strip()
        pg_db = os.environ.get("PG_DATABASE", "popularr").strip()

        from urllib.parse import quote_plus
        encoded_pass = quote_plus(pg_pass) if pg_pass else ""

        if encoded_pass:
            return f"postgresql+psycopg2://{pg_user}:{encoded_pass}@{pg_host}:{pg_port}/{pg_db}"
        return f"postgresql+psycopg2://{pg_user}@{pg_host}:{pg_port}/{pg_db}"

    # SQLite fallback — only reached when no PostgreSQL is configured at all,
    # meaning the app is running in first-run / setup-wizard mode.
    db_path = os.environ.get("DB_PATH", "/database/popularr.db")
    logger.info("No PG_HOST/DATABASE_URL set — using SQLite at %s (first-run mode)", db_path)
    return f"sqlite:///{db_path}"


# ---------------------------------------------------------------------------
# Engine factory
# ---------------------------------------------------------------------------

_ENGINE: Engine | None = None
_SESSION_FACTORY: sessionmaker[Session] | None = None


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite:")


def _configure_sqlite_pragmas(engine: Engine) -> None:
    """Enable WAL mode and performance pragmas for SQLite connections."""

    @event.listens_for(engine, "connect")
    def _set_pragma(dbapi_connection: Any, connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA cache_size=-64000")  # 64 MB
        cursor.close()


def get_engine() -> Engine:
    """Return the singleton SQLAlchemy engine, creating it if necessary."""
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE

    url = _resolve_database_url()
    sqlite = _is_sqlite(url)

    kwargs: dict[str, Any] = {
        "echo": os.environ.get("SQLALCHEMY_ECHO", "0") == "1",
        "future": True,
    }

    if sqlite:
        # SQLite doesn't need pooling — using NullPool avoids "QueuePool limit
        # of size 5 overflow 10" errors with multi-threaded access.
        kwargs["poolclass"] = NullPool
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        # PostgreSQL gets a small connection pool
        kwargs["poolclass"] = QueuePool
        kwargs["pool_size"] = int(os.environ.get("DB_POOL_SIZE", "5"))
        kwargs["max_overflow"] = int(os.environ.get("DB_POOL_OVERFLOW", "10"))
        kwargs["pool_pre_ping"] = True  # verify connections before use

    _ENGINE = create_engine(url, **kwargs)

    if sqlite:
        _configure_sqlite_pragmas(_ENGINE)

    logger.info(
        "SQLAlchemy engine created (%s, pool=%s)",
        "sqlite" if sqlite else "postgresql",
        kwargs.get("poolclass", QueuePool).__name__,
    )

    return _ENGINE


def get_session_factory() -> sessionmaker[Session]:
    """Return the singleton session factory."""
    global _SESSION_FACTORY
    if _SESSION_FACTORY is None:
        _SESSION_FACTORY = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SESSION_FACTORY


# ---------------------------------------------------------------------------
# Synchronous session context manager
# ---------------------------------------------------------------------------

def _is_transient_db_error(exc: Exception) -> bool:
    """Return True for connection-level DB errors worth retrying."""
    if exc is None:
        return False
    try:
        from sqlalchemy.exc import OperationalError as SAOperationalError
        from sqlalchemy.exc import InterfaceError as SAInterfaceError
        if isinstance(exc, (SAOperationalError, SAInterfaceError)):
            return True
    except Exception:
        pass
    orig = getattr(exc, "orig", None)
    if orig is not None:
        cls_name = type(orig).__name__.lower()
        if any(k in cls_name for k in ("operationalerror", "interfaceerror", "connectionerror")):
            return True
    return False


@contextmanager
def db_session(retries: int = 2) -> Iterator[Session]:
    """Context manager that yields a SQLAlchemy session and closes on exit.

    Commits on success, rolls back on exception.

    Survives transient PostgreSQL connection drops (restart / idle timeout):
    session acquisition is retried on a fresh factory a few times, disposing
    the engine pool so the next attempt checks out a live connection.

    Note: only acquisition (before the single ``yield``) can be retried.
    A contextmanager generator must yield exactly once — re-yielding after
    an exception makes ``contextlib`` raise "generator didn't stop after
    throw".  Failures inside the caller's block therefore surface to the
    caller, which may retry the whole operation (e.g. the queue worker's
    next cycle).

    Usage::

        from db.engine import db_session
        from db.models import Track

        with db_session() as session:
            track = session.query(Track).filter(Track.id == "123").first()
    """
    retries = max(1, int(retries))
    last_error: Exception | None = None

    # Retry only session acquisition — the caller's block cannot be re-run.
    session: Session | None = None
    for attempt in range(retries):
        try:
            session = get_session_factory()()
            break
        except Exception as exc:
            last_error = exc
            if not _is_transient_db_error(exc) or attempt >= retries - 1:
                raise
            try:
                _ENGINE and _ENGINE.dispose()
            except Exception:
                pass
    if session is None:
        if last_error is not None:
            raise last_error
        raise RuntimeError("db_session: could not acquire a session")

    try:
        yield session
        session.commit()
    except Exception as exc:
        session.rollback()
        if _is_transient_db_error(exc):
            # Dispose the pool so the NEXT call checks out a live connection,
            # then re-raise — the caller decides whether to retry.
            try:
                _ENGINE and _ENGINE.dispose()
            except Exception:
                pass
        raise
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Session dependency for Flask (per-request scoped)
# ---------------------------------------------------------------------------

def get_db() -> Session:
    """Flask-compatible session provider.  Call from ``before_request``.

    Usage in ``app.py``::

        from db.engine import get_db, close_db

        @app.before_request
        def _open_db():
            g.db = get_db()

        @app.teardown_request
        def _close_db(exc=None):
            close_db(exc)
    """
    return get_session_factory()()


def close_db(exc: BaseException | None = None) -> None:
    """Flask teardown: close any session stored in ``g``."""
    import flask
    db = flask.g.pop("db", None)
    if db is not None:
        if exc is not None:
            db.rollback()
        else:
            db.commit()
        db.close()


# ---------------------------------------------------------------------------
# Metadata helper for Alembic
# ---------------------------------------------------------------------------

def get_base_metadata():
    """Return the declarative base metadata for Alembic ``target_metadata``."""
    return Base.metadata


# ---------------------------------------------------------------------------
# Auto-migration on startup
# ---------------------------------------------------------------------------

def run_migrations_on_startup() -> bool:
    """Run ``alembic upgrade head`` to apply any pending migrations.

    Controlled by the ``AUTO_MIGRATE`` environment variable:
      - ``"0"``, ``"false"``, ``"no"`` → skip (safe for read-only replicas)
      - Any other value or unset → run migrations

    Returns ``True`` on success (or when skipped), ``False`` on failure.
    """
    skip = os.environ.get("AUTO_MIGRATE", "1").strip().lower() in {"0", "false", "no"}
    if skip:
        logger.info("AUTO_MIGRATE=0 — skipping database migrations")
        return True

    try:
        from alembic.config import Config
        from alembic import command

        # Locate alembic.ini relative to the project root
        _root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        alembic_cfg_path = os.path.join(_root, "alembic.ini")

        if not os.path.isfile(alembic_cfg_path):
            logger.warning("alembic.ini not found at %s — skipping migrations", alembic_cfg_path)
            return True

        alembic_cfg = Config(alembic_cfg_path)
        command.upgrade(alembic_cfg, "head")

        logger.info("Database migrations applied successfully (up to head)")
        return True

    except Exception as exc:
        logger.error("Database migration failed: %s", exc, exc_info=True)
        # Do NOT block app startup — the legacy schema bootstrap already
        # ensures tables exist.  Migration failures are logged and can be
        # investigated later.
        return False
