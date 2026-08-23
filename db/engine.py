"""SQLAlchemy engine, session factory, and helper utilities.

Provides both synchronous (Session) and asynchronous (AsyncSession) session 
management for the Popularr database, powered by PostgreSQL & SQLAlchemy 2.0.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager, contextmanager
from typing import Any, AsyncGenerator, Generator

import structlog
from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import MetaData, create_engine, text as _text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import QueuePool
from tenacity import (
    AsyncRetrying,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Configuration (Powered by pydantic-settings)
# ---------------------------------------------------------------------------

class DatabaseSettings(BaseSettings):
    """Database configuration parsed from environment variables or .env."""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = ""
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_user: str = "popularr"
    pg_password: str = ""
    pg_database: str = "popularr"

    db_pool_size: int = 10
    db_pool_overflow: int = 20
    db_pool_timeout: int = 30
    db_pool_recycle_seconds: int = 50
    sqlalchemy_echo: bool = False
    auto_migrate: bool = True

    @computed_field
    def sync_url(self) -> str:
        if self.database_url:
            return self.database_url
        
        from urllib.parse import quote_plus
        pwd = quote_plus(self.pg_password) if self.pg_password else ""
        auth = f"{self.pg_user}:{pwd}" if pwd else self.pg_user
        return f"postgresql+psycopg2://{auth}@{self.pg_host}:{self.pg_port}/{self.pg_database}"

    @computed_field
    def async_url(self) -> str:
        url = self.sync_url
        if url.startswith("postgresql+psycopg2://"):
            return url.replace("postgresql+psycopg2://", "postgresql+asyncpg://", 1)
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url


db_settings = DatabaseSettings()


# ---------------------------------------------------------------------------
# Base model
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    """Declarative base for all Popularr ORM models."""
    pass


# ---------------------------------------------------------------------------
# Engine factories
# ---------------------------------------------------------------------------

_ENGINE: Engine | None = None
_SESSION_FACTORY: sessionmaker[Session] | None = None
_ASYNC_ENGINE: AsyncEngine | None = None
_ASYNC_SESSION_FACTORY: async_sessionmaker[AsyncSession] | None = None

# Postgres advisory-lock key that serialises concurrent ``alembic upgrade``
# calls across hypercorn workers at boot.
_MIGRATION_ADVISORY_LOCK_KEY = 0x504F50524C52  # "POPRLR" as an int


def get_engine() -> Engine:
    """Return the singleton SQLAlchemy engine."""
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE

    kwargs: dict[str, Any] = {
        "echo": db_settings.sqlalchemy_echo,
        "poolclass": QueuePool,
        "pool_size": db_settings.db_pool_size,
        "max_overflow": db_settings.db_pool_overflow,
        "pool_timeout": db_settings.db_pool_timeout,
        "pool_pre_ping": True,
        "pool_recycle": db_settings.db_pool_recycle_seconds,
    }

    _ENGINE = create_engine(db_settings.sync_url, **kwargs)
    logger.info("SQLAlchemy engine created (postgresql)")
    return _ENGINE


def get_session_factory() -> sessionmaker[Session]:
    global _SESSION_FACTORY
    if _SESSION_FACTORY is None:
        _SESSION_FACTORY = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SESSION_FACTORY


def get_async_engine() -> AsyncEngine:
    global _ASYNC_ENGINE
    if _ASYNC_ENGINE is not None:
        return _ASYNC_ENGINE

    kwargs: dict[str, Any] = {
        "echo": db_settings.sqlalchemy_echo,
        "pool_size": db_settings.db_pool_size,
        "max_overflow": db_settings.db_pool_overflow,
        "pool_timeout": db_settings.db_pool_timeout,
        "pool_pre_ping": True,
        "pool_recycle": db_settings.db_pool_recycle_seconds,
    }

    _ASYNC_ENGINE = create_async_engine(db_settings.async_url, **kwargs)
    logger.info("Async SQLAlchemy engine created (postgresql/asyncpg)")
    return _ASYNC_ENGINE


def get_async_session_factory() -> async_sessionmaker[AsyncSession]:
    global _ASYNC_SESSION_FACTORY
    if _ASYNC_SESSION_FACTORY is None:
        _ASYNC_SESSION_FACTORY = async_sessionmaker(bind=get_async_engine(), expire_on_commit=False)
    return _ASYNC_SESSION_FACTORY


# ---------------------------------------------------------------------------
# Session Context Managers
# ---------------------------------------------------------------------------

def _is_transient_db_error(exc: BaseException) -> bool:
    """Return True for connection-level DB errors worth retrying."""
    if isinstance(exc, (OperationalError, InterfaceError)):
        return True
    
    orig = getattr(exc, "orig", None)
    if orig is not None:
        cls_name = type(orig).__name__.lower()
        return any(k in cls_name for k in ("operationalerror", "interfaceerror", "connectionerror"))
    return False


@contextmanager
def db_session(retries: int = 2) -> Generator[Session, None, None]:
    """Sync context manager yielding a Session, auto-committing on success."""
    def _acquire_session() -> Session:
        return get_session_factory()()

    def _dispose_before_retry(retry_state: Any) -> None:
        if _ENGINE:
            _ENGINE.dispose()
        logger.warning(
            "Transient database error, retrying session acquisition",
            attempt=retry_state.attempt_number,
            exception=str(retry_state.outcome.exception()) if retry_state.outcome else None,
        )

    session = Retrying(
        stop=stop_after_attempt(max(1, retries)),
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
        if _is_transient_db_error(exc) and _ENGINE:
            _ENGINE.dispose()
        raise
    finally:
        session.close()


@asynccontextmanager
async def async_db_session(retries: int = 2) -> AsyncGenerator[AsyncSession, None]:
    """Async context manager yielding an AsyncSession (asyncpg)."""
    async def _acquire_session() -> AsyncSession:
        return get_async_session_factory()()

    def _dispose_before_retry(retry_state: Any) -> None:
        if _ASYNC_ENGINE:
            _ASYNC_ENGINE.sync_engine.dispose()
        logger.warning(
            "Transient async database error, retrying session acquisition",
            attempt=retry_state.attempt_number,
        )

    session = await AsyncRetrying(
        stop=stop_after_attempt(max(1, retries)),
        wait=wait_exponential(multiplier=0.2, exp_base=2, min=0.2, max=2.0),
        retry=retry_if_exception(_is_transient_db_error),
        reraise=True,
        before_sleep=_dispose_before_retry,
    )(_acquire_session)

    try:
        yield session
        await session.commit()
    except Exception as exc:
        await session.rollback()
        if _is_transient_db_error(exc) and _ASYNC_ENGINE:
            _ASYNC_ENGINE.sync_engine.dispose()
        raise
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# Flask / App Helpers
# ---------------------------------------------------------------------------

def get_db() -> Session:
    """Flask-compatible session provider (Memoized to request context)."""
    import flask
    
    # Check if we are inside an active Flask request context
    if flask.has_app_context():
        if "db" not in flask.g:
            flask.g.db = get_session_factory()()
        return flask.g.db
    
    # Fallback for scripts running entirely outside Flask context
    return get_session_factory()()


def close_db(exc: BaseException | None = None) -> None:
    """Flask teardown: close any session stored in `g`."""
    import flask
    if not flask.has_app_context():
        return
        
    db: Session | None = flask.g.pop("db", None)
    if db is not None:
        if exc is not None:
            db.rollback()
        else:
            db.commit()
        db.close()


def get_base_metadata() -> MetaData:
    """Return the declarative base metadata for Alembic."""
    return Base.metadata


def run_migrations_on_startup() -> bool:
    """Run `alembic upgrade head` to apply any pending migrations."""
    if not db_settings.auto_migrate:
        logger.info("AUTO_MIGRATE=False — skipping database migrations")
        return True

    try:
        from alembic import command
        from alembic.config import Config

        _root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        alembic_cfg_path = os.path.join(_root, "alembic.ini")

        if not os.path.isfile(alembic_cfg_path):
            logger.warning("alembic.ini not found — skipping migrations", path=alembic_cfg_path)
            return True

        alembic_cfg = Config(alembic_cfg_path)
        _lock_conn: Connection | None = None

        try:
            _lock_conn = get_engine().connect()
            _lock_conn.execute(_text("SELECT pg_advisory_lock(:k)"), {"k": _MIGRATION_ADVISORY_LOCK_KEY})
            # CRITICAL FIX: Close the active transaction to prevent Postgres 
            # idle-in-transaction timeout from severing the socket mid-migration
            _lock_conn.commit() 
        except Exception as _lk_exc:
            logger.warning("Migration advisory lock unavailable — proceeding without it", error=str(_lk_exc))
            if _lock_conn:
                _lock_conn.close()
                _lock_conn = None

        try:
            command.upgrade(alembic_cfg, "head")
            logger.info("Database migrations applied successfully (up to head)")
        finally:
            if _lock_conn is not None:
                try:
                    _lock_conn.execute(_text("SELECT pg_advisory_unlock(:k)"), {"k": _MIGRATION_ADVISORY_LOCK_KEY})
                    _lock_conn.commit()
                except Exception:
                    pass
                _lock_conn.close()

        return True

    except Exception as exc:
        logger.error("Database migration failed", error=str(exc), exc_info=True)
        return False
