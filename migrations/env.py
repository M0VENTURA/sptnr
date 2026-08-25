"""Alembic environment configuration for Popularr.

Loads the SQLAlchemy engine from ``db.engine`` (respecting DATABASE_URL /
PG_* env vars) so the same connection logic applies to both the
app and migrations.

An explicitly-set ``sqlalchemy.url`` in the Alembic config takes precedence
(standard Alembic behaviour — the test suite uses it to point the chain at a
scratch database).  When unset (production entrypoint), the shared app engine
from ``db.engine`` is used so migrations honour DATABASE_URL / PG_* exactly
like the application.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.config import fileConfig

from alembic import context
import sqlalchemy as sa

# Ensure the project root is on sys.path so we can import db.engine
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from db.engine import get_base_metadata, get_engine

# Alembic Config object
config = context.config

# Set sqlalchemy.url from our dynamic resolver if not already configured.
# NOTE: an explicitly-configured URL is left untouched — run_migrations_online()
# builds a throwaway engine from it (test-suite isolation).
#
# CRITICAL: must NOT use ``str(engine.url)`` here — SQLAlchemy 2.0's
# ``URL.__str__`` MASKS the password as ``***`` for security, so the
# migration engine would try to authenticate with the literal password
# "***" and fail with "password authentication failed for user ..." even
# though the app (which uses the URL object directly) connects fine.
# ``render_as_string(hide_password=False)`` emits the real password.
if not config.get_main_option("sqlalchemy.url"):
    engine = get_engine()
    url_with_password = engine.url.render_as_string(hide_password=False)
    config.set_main_option("sqlalchemy.url", url_with_password)

# Configure logging from alembic.ini if present
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Target metadata for auto-generation
target_metadata = get_base_metadata()

logger = logging.getLogger("alembic.env")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Configures the context with just a URL and not an Engine. Calls to
    ``context.execute()`` emit the given SQL string to the script output.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def _widen_version_num(connection: "sa.Connection") -> None:
    """Widen ``alembic_version.version_num`` to ``varchar(64)``.

    The default Alembic version table uses ``varchar(32)`` for the revision
    id.  Revisions ``007_essential_playlist_artist_index`` (35 chars) and
    ``008_add_missing_releases_tracklist`` (34 chars) exceed that, so
    ``UPDATE alembic_version SET version_num = ...`` failed with
    ``value too long for type character varying(32)`` on every boot — the
    migrations were never recorded, and every ``alembic upgrade head``
    retried (and failed) them, leaving the scan pipeline in a broken state
    (the dashboard "All" scan completed instantly).

    Idempotent: no-op once the column is already varchar(64)+.
    """
    try:
        from sqlalchemy import inspect as sa_inspect, text as sa_text
        inspector = sa_inspect(connection)
        if "alembic_version" not in inspector.get_table_names():
            return
        col_type = None
        for col in inspector.get_columns("alembic_version"):
            if col["name"] == "version_num":
                col_type = str(col["type"]).lower()
                break
        if not col_type or "varchar" not in col_type:
            return
        # Extract the declared length; if it's already >= 64, nothing to do.
        import re
        m = re.search(r"varchar\((\d+)\)", col_type)
        if m and int(m.group(1)) >= 64:
            return
        connection.execute(sa_text("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(64)"))
        logger.info("Widened alembic_version.version_num to VARCHAR(64)")
    except Exception as exc:
        logger.warning("Could not widen alembic_version.version_num: %s", exc)


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    Creates an Engine directly from the Alembic config and associates a
    connection with the migration context.
    """
    explicit_url = config.get_main_option("sqlalchemy.url")
    if explicit_url:
        connectable = sa.create_engine(explicit_url)
    else:
        connectable = get_engine()

    with connectable.connect() as connection:
        # Widen the version column BEFORE Alembic records any revision, so
        # the long 007/008 ids fit and the chain can finally advance.  The
        # ALTER COLUMN ... TYPE syntax is PostgreSQL-only.
        #
        # CRITICAL: the ALTER must be COMMITTED immediately.  SQLAlchemy 2.0
        # autobegins a transaction on the first execute; if we let that
        # transaction stay open, Alembic's ``context.begin_transaction()``
        # below can roll it back when it starts its own migration
        # transaction — the column reverts to VARCHAR(32), the 007+ version
        # stamps fail again ("value too long"), and every boot re-runs the
        # whole 006→010 chain.  An explicit commit pins the wider column
        # before any migration runs.
        if connectable.dialect.name == "postgresql":
            _widen_version_num(connection)
            connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()