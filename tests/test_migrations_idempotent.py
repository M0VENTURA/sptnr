"""Regression tests for idempotent Alembic migrations.

Reproduces the startup failure::

    ERROR: relation "missing_album_tracks" already exists

Root cause: ``db.bootstrap`` pre-creates every table in
``db.schema.TABLES_TO_ENSURE`` (including ``missing_album_tracks``), while
revisions 001/002/004/005/009 issued bare ``op.create_table`` calls.  When
``alembic_version`` lagged behind head, ``upgrade head`` aborted on the first
existing table and the entrypoint's blind ``stamp head`` fallback masked the
failure — so the error recurred on every boot.

These tests run the REAL revision chain (via alembic's command API) against
an in-memory SQLite database in two states:

1. Fresh database — full chain must apply cleanly.
2. Bootstrap-built database — tables pre-created via ``db.schema`` DDL with
   ``alembic_version`` stamped at an OLDER revision; upgrading to head must
   converge without "already exists" errors.

SQLite is used because the test suite standardises on it
(``DATABASE_URL=sqlite:///:memory:``); the guarded DDL helpers are
dialect-agnostic (SQLAlchemy inspector based).
"""

from __future__ import annotations

import os
from typing import Any, Iterator

import pytest

# The suite runs against SQLite; ensure env is set before db.engine imports.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
import sqlalchemy as sa  # noqa: E402

import db.schema as schema_module  # noqa: E402

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _make_alembic_config(engine_url: str) -> Config:
    cfg = Config(os.path.join(_PROJECT_ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_PROJECT_ROOT, "migrations"))
    cfg.set_main_option("sqlalchemy.url", engine_url)
    return cfg


@pytest.fixture()
def sqlite_engine(tmp_path: Any) -> Iterator[sa.Engine]:
    """File-based SQLite engine (in-memory DBs don't survive reconnects)."""
    url = f"sqlite:///{tmp_path / 'migrations_test.db'}"
    engine = sa.create_engine(url)
    yield engine
    engine.dispose()


def _stamp(engine: sa.Engine, revision: str) -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(64) NOT NULL)"
        )
        conn.exec_driver_sql("DELETE FROM alembic_version")
        conn.exec_driver_sql(
            f"INSERT INTO alembic_version (version_num) VALUES ('{revision}')"
        )


def _precreate_bootstrap_tables(engine: sa.Engine) -> None:
    """Create the bootstrap-built schema state using db.schema DDL.

    Only PostgreSQL-flavoured statements are translated where needed:
    - JSONB → TEXT (SQLite has no JSONB)
    - TIMESTAMPTZ → TIMESTAMP
    """
    ddl_overrides = {
        "JSONB": "TEXT",
        "TIMESTAMP WITH TIME ZONE": "TIMESTAMP",
        "'{}'::jsonb": "'{}'",
    }

    def translate(ddl: str) -> str:
        out = ddl
        for pg, lite in ddl_overrides.items():
            out = out.replace(pg, lite)
        return out

    with engine.begin() as conn:
        for _table_name, raw_ddl in schema_module.TABLES_TO_ENSURE.items():
            conn.exec_driver_sql(translate(raw_ddl))


class TestMigrationChainIdempotency:
    def test_fresh_database_upgrade_head_succeeds(self, sqlite_engine: sa.Engine) -> None:
        """A fresh database gets the complete chain without errors."""
        cfg = _make_alembic_config(str(sqlite_engine.url))
        command.upgrade(cfg, "head")

        inspector = sa.inspect(sqlite_engine)
        tables = set(inspector.get_table_names())
        assert "tracks" in tables
        assert "download_queue" in tables
        assert "upcoming_releases" in tables
        assert "folder_matches" in tables
        assert "user_favourites" in tables
        assert "missing_album_tracks" in tables

    def test_upgrade_head_twice_is_noop(self, sqlite_engine: sa.Engine) -> None:
        """Running upgrade head twice must not error (idempotent re-run)."""
        cfg = _make_alembic_config(str(sqlite_engine.url))
        command.upgrade(cfg, "head")
        # Second run: everything already applied — must be a clean no-op.
        command.upgrade(cfg, "head")

    def test_bootstrap_built_db_upgrade_converges(self, sqlite_engine: sa.Engine) -> None:
        """THE regression: bootstrap-built tables + stale alembic_version.

        Simulates the production state that produced
        ``relation "missing_album_tracks" already exists``: all tables exist
        (created by db.bootstrap), but alembic_version points at an older
        revision.  upgrade head must skip existing objects and converge.
        """
        # 1. Pre-create all bootstrap tables.
        _precreate_bootstrap_tables(sqlite_engine)

        # 2. Stamp at 008 — one revision BEFORE missing_album_tracks was
        #    introduced, exactly like the failing deployment.
        _stamp(sqlite_engine, "008_add_missing_releases_tracklist")

        # 3. Upgrade to head — previously raised
        #    "relation missing_album_tracks already exists".
        cfg = _make_alembic_config(str(sqlite_engine.url))
        command.upgrade(cfg, "head")  # must not raise

        inspector = sa.inspect(sqlite_engine)
        assert "missing_album_tracks" in set(inspector.get_table_names())

        # Version recorded at head.
        with sqlite_engine.connect() as conn:
            version = conn.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar()
        assert version == "010_add_pg_trgm_indexes"

    def test_partially_bootstrapped_db_upgrade_converges(self, sqlite_engine: sa.Engine) -> None:
        """Partial bootstrap state (some tables, older stamp) also converges."""
        # Create only SOME of the tables, stamp at 001.  Use full-column
        # definitions for tracks (revision 007's functional index references
        # tracks.artist / tracks.album_artist), plus the minimal bootstrap
        # stubs for artists / scan_history / bookmarks.
        partial_ddls = [
            schema_module.TABLES_TO_ENSURE["artists"],
            schema_module.TABLES_TO_ENSURE["scan_history"],
            schema_module.TABLES_TO_ENSURE["bookmarks"],
        ]
        def translate(ddl: str) -> str:
            return (
                ddl.replace("JSONB", "TEXT")
                .replace("TIMESTAMP WITH TIME ZONE", "TIMESTAMP")
                .replace("'{}'::jsonb", "'{}'")
            )

        with sqlite_engine.begin() as conn:
            for stmt in partial_ddls:
                conn.exec_driver_sql(translate(stmt))

        # Full tracks table via alembic op semantics is overkill here; a raw
        # CREATE TABLE with the columns later revisions index on suffices.
        with sqlite_engine.begin() as c2:
            c2.exec_driver_sql(
                """
                CREATE TABLE tracks (
                    id TEXT PRIMARY KEY,
                    artist TEXT,
                    album_artist TEXT,
                    album TEXT,
                    title TEXT,
                    file_path TEXT,
                    duration DOUBLE PRECISION,
                    stars INTEGER,
                    final_score DOUBLE PRECISION,
                    is_single BOOLEAN DEFAULT FALSE
                )
                """
            )

        _stamp(sqlite_engine, "001_initial_schema")
        cfg = _make_alembic_config(str(sqlite_engine.url))
        command.upgrade(cfg, "head")  # must not raise

        inspector = sa.inspect(sqlite_engine)
        tables = set(inspector.get_table_names())
        assert "missing_album_tracks" in tables
        assert "user_favourites" in tables


class TestIdempotentHelpers:
    def test_create_table_if_missing_skips_existing(self, sqlite_engine: sa.Engine) -> None:
        from migrations.idempotent import create_table_if_missing

        with sqlite_engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE helper_probe (id INTEGER PRIMARY KEY)")

        called = False

        def _factory() -> None:
            nonlocal called
            called = True
            raise AssertionError("factory must NOT run when table exists")

        result = create_table_if_missing(sqlite_engine, "helper_probe", _factory)
        assert result is False
        assert called is False

    def test_create_table_if_missing_creates_absent(self, sqlite_engine: sa.Engine) -> None:
        from migrations.idempotent import create_table_if_missing

        created: list[bool] = []

        def _factory() -> None:
            created.append(True)

        result = create_table_if_missing(sqlite_engine, "helper_new", _factory)
        assert result is True
        assert created == [True]

    def test_index_exists_detects_indexes(self, sqlite_engine: sa.Engine) -> None:
        from migrations.idempotent import index_exists

        with sqlite_engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE idx_probe (val TEXT)")
            conn.exec_driver_sql("CREATE INDEX idx_probe_val ON idx_probe (val)")

        assert index_exists(sqlite_engine, "idx_probe_val", "idx_probe") is True
        assert index_exists(sqlite_engine, "idx_missing_idx", "idx_probe") is False