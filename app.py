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

from flask import Flask

from helpers.logging_config import setup_logging
from db.bootstrap import init_database_and_schema
from helpers.flash_manager import register_flash_helpers
from helpers.app_bootstrap import register_all_blueprints
from helpers.app_hooks import register_app_hooks
from helpers.template_filters import register_filters
from helpers.file_manager import ensure_default_log_files
from helpers.task_manager import initialize_app_services

# SQLAlchemy engine — initialises the connection pool on startup
from db.engine import get_engine, run_migrations_on_startup


setup_logging("WebUI")

# Initialise the SQLAlchemy engine early so the pool is ready before any
# request or background worker needs it.
get_engine()

# Apply any pending Alembic migrations.  Safe to call repeatedly —
# Alembic tracks which migrations have already been applied in the
# ``alembic_version`` table.  Set AUTO_MIGRATE=0 to skip.
run_migrations_on_startup()



setup_logging("WebUI")

app = Flask(__name__)
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
