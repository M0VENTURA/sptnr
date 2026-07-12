"""Alembic environment configuration for Popularr.

Loads the SQLAlchemy engine from ``db.engine`` (respecting DATABASE_URL /
PG_* / DB_PATH env vars) so the same connection logic applies to both the
app and migrations.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.config import fileConfig

from alembic import context

# Ensure the project root is on sys.path so we can import db.engine
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from db.engine import get_base_metadata, get_engine

# Alembic Config object
config = context.config

# Set sqlalchemy.url from our dynamic resolver if not already in alembic.ini
if not config.get_main_option("sqlalchemy.url"):
    engine = get_engine()
    config.set_main_option("sqlalchemy.url", str(engine.url))

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


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    Creates an Engine directly from the Alembic config and associates a
    connection with the migration context.
    """
    connectable = get_engine()

    with connectable.connect() as connection:
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
