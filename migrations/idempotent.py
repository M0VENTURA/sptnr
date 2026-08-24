"""Idempotent-DDL helpers shared by Popularr Alembic revisions.

Popularr databases can be built by TWO paths:

1. ``alembic upgrade head`` (fresh installs — entrypoint runs this first), or
2. ``db.bootstrap.ensure_full_schema()`` (legacy installs, and the runtime
   repair path — it creates every table in ``db.schema.TABLES_TO_ENSURE``).

Because both paths target the same objects, a revision that issues a bare
``CREATE TABLE`` fails with ``relation ... already exists`` whenever the
bootstrap ran first (or an earlier boot's stamp left ``alembic_version``
below head).  That failure aborted the whole migration chain, and the
entrypoint's old fallback blindly ``alembic stamp head`` — marking DDL that
never ran as applied.  The result was the recurring startup noise::

    ERROR: relation "missing_album_tracks" already exists

These helpers give revisions existence-guards so ``upgrade head`` converges
on ANY starting state instead of erroring:

- :func:`table_exists` / :func:`index_exists` — inspector-based checks.
- :func:`create_table_if_missing` — run a ``op.create_table`` closure only
  when the relation is absent.
- :func:`create_index_if_missing` — execute a raw ``CREATE INDEX IF NOT
  EXISTS`` statement only when the index is absent.

Only the standard SQLAlchemy inspector is used, so the helpers work on both
PostgreSQL (production) and SQLite (test suite).
"""

from __future__ import annotations

from typing import Any, Callable

import sqlalchemy as sa

__all__ = [
    "table_exists",
    "index_exists",
    "create_table_if_missing",
    "create_index_if_missing",
]


def table_exists(bind: Any, table_name: str) -> bool:
    """Return True when ``table_name`` exists in the bound database."""
    inspector = sa.inspect(bind)
    try:
        return table_name in inspector.get_table_names()
    except Exception:
        # Unreflectable state (permissions, transient disconnect): treat as
        # absent so the caller's CREATE attempt surfaces the real error.
        return False


def index_exists(bind: Any, index_name: str, table_name: str | None = None) -> bool:
    """Return True when ``index_name`` exists (optionally scoped to a table)."""
    inspector = sa.inspect(bind)
    try:
        if table_name is not None and not table_exists(bind, table_name):
            return False
        raw: Any = inspector.get_indexes(table_name) if table_name else []
        for entry in list(raw):
            if str(entry.get("name")) == index_name:
                return True
        return False
    except Exception:
        return False


def create_table_if_missing(bind: Any, table_name: str, factory: Callable[[], None]) -> bool:
    """Invoke ``factory()`` (issuing the CREATE TABLE) only when absent.

    Returns True when the table was created, False when it already existed
    (the factory is NOT invoked in that case).
    """
    if table_exists(bind, table_name):
        return False
    factory()
    return True


def create_index_if_missing(bind: Any, index_name: str, table_name: str, ddl_sql: str) -> bool:
    """Execute raw ``ddl_sql`` (a CREATE INDEX statement) only when absent.

    Returns True when the index was created, False when it already existed.
    Using the inspector (rather than relying solely on ``IF NOT EXISTS``)
    keeps behaviour identical across dialects and lets callers detect whether
    DDL actually ran.
    """
    if index_exists(bind, index_name, table_name):
        return False
    bind.execute(sa.text(ddl_sql))
    return True