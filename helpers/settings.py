"""
Pydantic Settings for Popularr.

Replaces the previous YAML-only config with type-safe, env-var-driven settings.
All values can be set via environment variables (``POPULARLR_*``) or a
``config.yaml`` file (legacy). New code should prefer importing ``settings``
and accessing attributes directly.

Usage::

    from helpers.settings import settings

    # Access any setting with IDE autocomplete
    db_url = settings.pg_host
    nav_url = settings.navidrome_url

Environment variables take precedence over ``config.yaml`` values.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


# ---------------------------------------------------------------------------
# Settings — all configuration in one type-safe class
# ---------------------------------------------------------------------------

class Settings(BaseSettings):
    """Application settings, loaded from environment variables with YAML fallback.

    Env var naming: ``POPULARLR_<SECTION>_<KEY>``, e.g. ``POPULARLR_PG_HOST``.
    Nested values use double underscore, e.g. ``POPULARLR_API_INTEGRATIONS__LASTFM__API_KEY``.
    """

    model_config = SettingsConfigDict(
        env_prefix="POPULARLR_",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
    )

    # ── PostgreSQL ────────────────────────────────────────────────────────
    pg_host: str = Field(default="localhost", description="PostgreSQL host")
    pg_port: int = Field(default=5432, description="PostgreSQL port")
    pg_user: str = Field(default="popularr", description="PostgreSQL user")
    pg_password: str = Field(default="", description="PostgreSQL password")
    pg_database: str = Field(default="popularr", description="PostgreSQL database name")
    database_url: str = Field(default="", description="Full connection string (overrides all PG_* vars)")
    db_pool_size: int = Field(default=5, description="SQLAlchemy pool size")
    db_pool_overflow: int = Field(default=10, description="SQLAlchemy max overflow")

    # ── Navidrome ─────────────────────────────────────────────────────────
    nav_url: str = Field(default="", description="Navidrome server URL")
    nav_user: str = Field(default="", description="Navidrome username")
    nav_pass: str = Field(default="", description="Navidrome password")

    # ── API Integrations ──────────────────────────────────────────────────
    lastfm_api_key: str = Field(default="", description="Last.fm API key")
    lastfm_api_secret: str = Field(default="", description="Last.fm API secret")
    discogs_token: str = Field(default="", description="Discogs personal access token")

    # ── Downloads ─────────────────────────────────────────────────────────
    downloads_folder: str = Field(default="/downloads", description="Downloads directory")
    auto_import: bool = Field(default=False, description="Auto-import downloads")

    # ── Essentia ML ───────────────────────────────────────────────────────
    essentia_enabled: bool = Field(default=False, description="Enable Essentia ML analysis")
    essentia_script_path: str = Field(default="/opt/Essentia-to-Metadata/tag_music.py")
    essentia_models_dir: str = Field(default="/opt/essentia_models")

    # ── Music root ────────────────────────────────────────────────────────
    music_root: str = Field(default="/music", description="Music library root directory")

    # ── Config paths ──────────────────────────────────────────────────────
    config_path: str = Field(default="/config/config.yaml", description="Config file path")
    log_path: str = Field(default="/config/app.log", description="Log file path")

    # ── Scheduler ─────────────────────────────────────────────────────────
    scheduler_timezone: str = Field(default="Australia/Melbourne")

    # ── Features ──────────────────────────────────────────────────────────
    use_local_assets: bool = Field(default=False, description="Use vendored assets instead of CDN")

    # ── Gunicorn / Hypercorn ──────────────────────────────────────────────
    bind: str = Field(default="0.0.0.0:5000")
    workers: int = Field(default=4)
    # Root log level. Debug is intentionally off by default (verbose + large
    # debug.log); enable via config.html or POPULARLR_LOG_LEVEL for troubleshooting.
    log_level: str = Field(default="info")

    # ── Scan ──────────────────────────────────────────────────────────────
    scan_interval: int = Field(default=360, description="Scan interval in minutes")

    # ── Queue ─────────────────────────────────────────────────────────────
    queue_processor_interval: int = Field(default=30, description="Queue processor interval in seconds")

    # ── Paths ─────────────────────────────────────────────────────────────
    state_dir: str = Field(default="/config", description="State directory")
    music_folder: str = Field(default="/music", description="Music folder (alias for music_root)")


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the singleton Settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


# Convenience accessor
settings = get_settings()
