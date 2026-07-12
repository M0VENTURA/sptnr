#!/usr/bin/env python3
"""
Application entrypoint for Popularr Web UI.

This file is intentionally minimal and orchestration-focused.

Responsibilities:
- Configure logging
- Create Flask app instance
- Register extensions (flash helpers, filters, blueprints, hooks)
- Ensure required files and config exist
- Initialize database schema
- Start background services
- Launch the web server

Important:
This file should NOT contain business logic or database logic.
All heavy logic lives in:
- db/
- helpers/
- services/
"""

import os
import secrets

from quart import Quart

from helpers.logging_config import setup_logging
from db.bootstrap import init_database_and_schema
from helpers.flash_manager import register_flash_helpers
from helpers.app_bootstrap import register_all_blueprints
from helpers.app_hooks import register_app_hooks
from helpers.template_filters import register_filters
from helpers.file_manager import ensure_default_log_files
from helpers.task_manager import initialize_app_services

# SQLAlchemy engine — initialises the connection pool on startup.
# Gracefully degrades if sqlalchemy is not installed (e.g. older Docker image).
try:
    from db.engine import get_engine, run_migrations_on_startup
    _sqlalchemy_available = True
except Exception:
    _sqlalchemy_available = False

    def get_engine():
        return None

    def run_migrations_on_startup():
        return False


setup_logging("WebUI")

# Initialise the SQLAlchemy engine early so the pool is ready before any
# request or background worker needs it.
if _sqlalchemy_available:
    try:
        get_engine()
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("SQLAlchemy engine init failed: %s", exc)

    # Apply any pending Alembic migrations.
    try:
        run_migrations_on_startup()
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Alembic migration failed: %s", exc)



setup_logging("WebUI")

app = Quart(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(16))
app.config["PERMANENT_SESSION_LIFETIME"] = 86400

CONFIG_PATH = os.environ.get("CONFIG_PATH") or "/config/config.yaml"
LOG_PATH = os.environ.get("LOG_PATH", "/config/app.log")

ensure_default_log_files(CONFIG_PATH, LOG_PATH)

register_flash_helpers(app)
register_filters(app)
register_all_blueprints(app)
register_app_hooks(app)

init_database_and_schema()

# Pass the Flask app into background service bootstrap so workers that need
# an application context can create one safely without importing app.py.
initialize_app_services(app)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
