"""SQLAlchemy engine, session factory, and helper utilities.

Provides both synchronous (``Session`` / ``db_session``) and asynchronous
(``AsyncSession`` / ``async_db_session``) session management for the
Popularr database.

Usage (sync — existing Flask routes)::

    from db.engine import db_session

    with db_session() as session:
        tracks = session.query(Track).all()

Usage (async — Quart routes)::

    from db.engine import async_db_session

    async with async_db_session() as session:
        result = await session.execute(select(Track))

The async session runs on the ``asyncpg`` driver (proper asyncpg adoption)
so async handlers never block the event loop on DB work.

Environment variables:
    DATABASE_URL  – Full connection string (overrides all PG_* vars)
    PG_HOST       – PostgreSQL host (default: localhost)
    PG_PORT       – PostgreSQL port (default: 5432)
    PG_USER       – PostgreSQL user (default: popularr)
    PG_PASSWORD   – PostgreSQL password
    PG_DATABASE   – PostgreSQL database name (default: popularr)
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager, contextmanager
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
    """Build the PostgreSQL connection string from environment variables.

    ``DATABASE_URL`` wins when set; otherwise the ``PG_*`` variables are
    combined into a ``postgresql+psycopg2`` URL.  PostgreSQL is required —
    there is no SQLite fallback.  If PG_HOST is set but unreachable the
    error propagates (fail fast).
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

    raise RuntimeError(
        "PostgreSQL configuration missing: set DATABASE_URL or PG_HOST "
        "(plus PG_PORT/PG_USER/PG_PASSWORD/PG_DATABASE as needed)"
    )


def _resolve_async_database_url() -> str:
    """Derive the asyncpg connection URL from the resolved sync URL.

    The same ``DATABASE_URL`` / ``PG_*`` config drives both engines; only the
    driver changes (``psycopg2`` → ``asyncpg``).  SQLite test URLs are passed
    through unchanged (asyncpg cannot serve SQLite — async sessions are only
    used against PostgreSQL in production).
    """
    url = _resolve_database_url()
    if url.startswith("postgresql+psycopg2://"):
        return url.replace("postgresql+psycopg2://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


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
        # Only reachable via an explicit DATABASE_URL override (e.g. tests):
        # in-memory SQLite needs StaticPool so every session shares the SAME
        # connection (otherwise each checkout gets a fresh empty database);
        # file-backed SQLite needs NullPool to avoid multi-threaded errors.
        if url == "sqlite:///:memory:" or url.endswith(":memory:"):
            from sqlalchemy.pool import StaticPool
            kwargs["poolclass"] = StaticPool
            kwargs["connect_args"] = {"check_same_thread": False}
        else:
            kwargs["poolclass"] = NullPool
            kwargs["connect_args"] = {"check_same_thread": False}
    else:
        # PostgreSQL gets a connection pool sized to scan concurrency
        # (up to 8 parallel track workers plus background services) with
        # an explicit pool_timeout so contention fails fast instead of
        # hanging and cascading (e.g. Navidrome scan-status timeouts).
        kwargs["poolclass"] = QueuePool
        kwargs["pool_size"] = int(os.environ.get("DB_POOL_SIZE", "10"))
        kwargs["max_overflow"] = int(os.environ.get("DB_POOL_OVERFLOW", "20"))
        kwargs["pool_timeout"] = int(os.environ.get("DB_POOL_TIMEOUT", "30"))
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
# Asynchronous engine / sessions (asyncpg)
# ---------------------------------------------------------------------------
# Proper asyncpg adoption: a dedicated async engine + async session factory
# backed by the ``postgresql+asyncpg`` driver.  Async Quart routes should use
# ``async_db_session()`` (mirrors ``db_session()`` with commit/rollback) so
# their DB work never blocks the event loop.  The async engine shares the
# ``PG_*`` / ``DATABASE_URL`` config with the sync engine (only the driver
# differs) and keeps its own connection pool.

_ASYNC_ENGINE: Any = None
_ASYNC_SESSION_FACTORY: Any = None


def get_async_engine() -> Any:
    """Return the singleton async (asyncpg) SQLAlchemy engine."""
    global _ASYNC_ENGINE
    if _ASYNC_ENGINE is not None:
        return _ASYNC_ENGINE

    from sqlalchemy.ext.asyncio import create_async_engine

    url = _resolve_async_database_url()
    kwargs: dict[str, Any] = {
        "echo": os.environ.get("SQLALCHEMY_ECHO", "0") == "1",
        "future": True,
    }
    if not url.startswith("sqlite:"):
        kwargs["pool_size"] = int(os.environ.get("DB_POOL_SIZE", "10"))
        kwargs["max_overflow"] = int(os.environ.get("DB_POOL_OVERFLOW", "20"))
        kwargs["pool_timeout"] = int(os.environ.get("DB_POOL_TIMEOUT", "30"))
        kwargs["pool_pre_ping"] = True

    _ASYNC_ENGINE = create_async_engine(url, **kwargs)
    logger.info("Async (asyncpg) SQLAlchemy engine created")
    return _ASYNC_ENGINE


def get_async_session_factory() -> Any:
    """Return the singleton async session factory (``async_sessionmaker``)."""
    global _ASYNC_SESSION_FACTORY
    if _ASYNC_SESSION_FACTORY is None:
        from sqlalchemy.ext.asyncio import async_sessionmaker
        _ASYNC_SESSION_FACTORY = async_sessionmaker(
            bind=get_async_engine(), expire_on_commit=False
        )
    return _ASYNC_SESSION_FACTORY


@asynccontextmanager
async def async_db_session(retries: int = 2):
    """Async context manager yielding an ``AsyncSession`` (asyncpg).

    Commits on success, rolls back on exception, and retries session
    acquisition (not the caller's block) on transient connection failures —
    the async counterpart of ``db_session`` for asyncpg adoption.

    Usage::

        from db.engine import async_db_session

        async with async_db_session() as session:
            result = await session.execute(text("SELECT 1"))
    """
    retries = max(1, int(retries))

    # Retry only session acquisition — the caller's block cannot be re-run.
    from tenacity import (
        AsyncRetrying,
        retry_if_exception,
        stop_after_attempt,
        wait_exponential,
    )

    async def _acquire_session():
        return get_async_session_factory()()

    def _dispose_before_retry(retry_state: Any) -> None:
        try:
            _ASYNC_ENGINE and _ASYNC_ENGINE.dispose()
        except Exception:
            pass

    session = await AsyncRetrying(
        stop=stop_after_attempt(retries),
        wait=wait_exponential(multiplier=0.2, exp_base=2, min=0.2, max=2.0),
        retry=retry_if_exception(_is_transient_db_error),
        reraise=True,
        before_sleep=_dispose_before_retry,
    )(_acquire_session)

    try:
        yield session
        await session.commit()
    except Exception as exc:
        try:
            await session.rollback()
        except Exception:
            pass
        if _is_transient_db_error(exc):
            try:
                _ASYNC_ENGINE and _ASYNC_ENGINE.dispose()
            except Exception:
                pass
        raise
    finally:
        await session.close()


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

    # Retry only session acquisition — the caller's block cannot be re-run.
    # tenacity drives the retry/backoff (``db_session`` used to hand-roll the
    # loop); the pool is disposed before each retry so the next attempt checks
    # out a live connection after a Postgres restart / idle timeout.
    from tenacity import (
        Retrying,
        retry_if_exception,
        stop_after_attempt,
        wait_exponential,
    )

    def _acquire_session() -> Session:
        return get_session_factory()()

    def _dispose_before_retry(retry_state: Any) -> None:
        try:
            _ENGINE and _ENGINE.dispose()
        except Exception:
            pass
        logger.warning(
            "[db] Transient error acquiring session (attempt %s) — retrying: %s",
            retry_state.attempt_number,
            retry_state.outcome.exception() if retry_state.outcome else None,
        )

    session = Retrying(
        stop=stop_after_attempt(retries),
        wait=wait_exponential(multiplier=0.2, exp_base=2, min=0.2, max=2.0),
        retry=retry_if_exception(_is_transient_db_error),
        reraise=True,
        before_sleep=_dispose_before_retry,
    )(_acquire_session)

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
