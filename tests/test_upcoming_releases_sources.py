"""Tests for the upcoming-releases Source filter API (issue #877).

Verifies that:
  - ``GET /api/upcoming-releases/sources`` lists each scraper-rule key with a
    release count (and groups MusicBrainz rows under a pseudo-key);
  - ``GET /api/upcoming-releases?source=<key>`` filters by the exact rule.
"""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from routes import upcoming_releases_routes as routes

_CREATE_TABLE = """
    CREATE TABLE upcoming_releases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        artist_name TEXT NOT NULL,
        album_name TEXT NOT NULL,
        source TEXT NOT NULL,
        source_key TEXT,
        release_date TEXT,
        release_year INTEGER,
        artist_in_collection BOOLEAN DEFAULT 0,
        album_in_collection BOOLEAN DEFAULT 0,
        release_group_mbid TEXT,
        match_source TEXT,
        primary_type TEXT,
        mbid_match_status TEXT DEFAULT 'unmatched',
        mbid_source TEXT,
        mbid_confidence TEXT,
        mbid_match_score REAL,
        mbid_last_checked_at TEXT,
        mbid_manual_override BOOLEAN DEFAULT 0,
        status TEXT DEFAULT 'discovered',
        last_seen_at TEXT,
        created_at TEXT,
        updated_at TEXT
    )
"""


@pytest.fixture()
def seeded_db(monkeypatch):
    """Point the upcoming-releases routes at a file-backed SQLite DB with rows
    from several scraper rules plus a MusicBrainz row."""
    tmp = tempfile.mkdtemp()
    engine = create_engine(f"sqlite:///{os.path.join(tmp, 'test.db')}")
    sess_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with engine.begin() as conn:
        conn.execute(text(_CREATE_TABLE))
        conn.execute(text(
            "INSERT INTO upcoming_releases (artist_name, album_name, source, source_key, release_date) VALUES "
            "('Kpop Artist', 'Al1', 'K-Pop/Korean Music 2026', '2026_kpop', '2026-02-01'),"
            "('Metal Artist', 'Al2', 'Heavy Metal 2026', '2026_heavy_metal', '2026-02-02'),"
            "('Metal Artist 2', 'Al3', 'Heavy Metal 2026', '2026_heavy_metal', NULL),"
            "('MB Artist', 'Al4', 'MusicBrainz Daily Collection', NULL, '2026-02-04')"
        ))

    @contextmanager
    def _db_session(*_args, **_kwargs):
        session = sess_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(routes, "db_session", _db_session)
    yield
    engine.dispose()


@pytest.mark.asyncio
async def test_sources_endpoint(seeded_db, client):
    response = await client.get("/api/upcoming-releases/sources")
    assert response.status_code == 200
    data = await response.get_json()
    assert data["success"] is True
    by_key = {s["key"]: s["count"] for s in data["sources"]}
    assert by_key["2026_heavy_metal"] == 2
    assert by_key["2026_kpop"] == 1
    assert by_key["musicbrainz_daily_collection"] == 1


@pytest.mark.asyncio
async def test_source_filter_by_rule_key(seeded_db, client):
    response = await client.get("/api/upcoming-releases?source=2026_heavy_metal")
    assert response.status_code == 200
    data = await response.get_json()
    assert data["total"] == 2
    artists = [r["artist_name"] for r in data["releases"]]
    assert artists == ["Metal Artist", "Metal Artist 2"]


@pytest.mark.asyncio
async def test_source_filter_musicbrainz(seeded_db, client):
    response = await client.get("/api/upcoming-releases?source=musicbrainz_daily_collection")
    assert response.status_code == 200
    data = await response.get_json()
    artists = [r["artist_name"] for r in data["releases"]]
    assert artists == ["MB Artist"]


@pytest.mark.asyncio
async def test_source_filter_all_returns_everything(seeded_db, client):
    response = await client.get("/api/upcoming-releases?source=all")
    assert response.status_code == 200
    data = await response.get_json()
    assert data["total"] == 4
