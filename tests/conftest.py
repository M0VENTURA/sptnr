"""
Pytest configuration and shared fixtures for Popularr.

Provides:
- ``app`` fixture — Quart application instance
- ``client`` fixture — test client for HTTP requests
- ``db_session`` fixture — isolated database session per test
- ``sample_track`` fixture — a sample track record for testing
"""

from __future__ import annotations

import os
import pytest

os.environ.setdefault("CONFIG_PATH", "/dev/null")
# Production is PostgreSQL-only; an explicit DATABASE_URL keeps the unit
# test suite self-contained on an in-memory SQLite engine.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

# ---------------------------------------------------------------------------
# App fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def app():
    """Create and configure the Quart application for testing."""
    from app import app as _app
    _app.config["TESTING"] = True
    return _app


@pytest.fixture
def client(app):
    """Create an HTTP test client."""
    return app.test_client()


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_session():
    """Provide an isolated database session per test."""
    from db.engine import db_session as _db_session
    with _db_session() as session:
        yield session


@pytest.fixture
def sample_track(db_session):
    """Insert and return a sample track record."""
    from sqlalchemy import text as _text
    db_session.execute(
        _text("""
            INSERT INTO tracks (id, artist, album, title, file_path)
            VALUES (:id, :artist, :album, :title, :file_path)
            ON CONFLICT DO NOTHING
        """),
        {
            "id": "test-track-001",
            "artist": "Test Artist",
            "album": "Test Album",
            "title": "Test Track",
            "file_path": "/music/test.mp3",
        },
    )
    db_session.commit()
    return {"id": "test-track-001", "artist": "Test Artist", "album": "Test Album", "title": "Test Track"}
