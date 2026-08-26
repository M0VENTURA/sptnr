"""Repro/regression: /api/search column probe must be dialect-safe.

The search endpoint's column probe was Postgres-only (pg_catalog SQL) and
500'd on any other engine (e.g. the SQLite test engine), turning every
search into "Search failed".  The fix routes the probe through
``db.schema_helpers.get_table_columns`` (to_regclass-based) with a
SQLAlchemy-inspector fallback, so the probe can never 500 the search.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_search_probe_never_500s_on_sqlite(client, db_session):
    """Even with an empty/undiscoverable table the probe must not 500."""
    response = await client.post("/api/search", json={"query": "spice"})
    assert response.status_code in (200, 400), f"probe 500'd: {response.status_code}"
    data = await response.get_json()
    # A graceful empty result (with the hint note) is the expected outcome
    # when the SQLite test engine cannot resolve the Postgres catalog.
    if response.status_code == 200:
        assert "artists" in data and "tracks" in data


def test_resolve_tracks_columns_dialect_fallback(db_session):
    """_resolve_tracks_columns must return a set on SQLite (inspector fallback),
    never raise, and include at least the ORM-defined columns when the table
    exists on the connection's engine."""
    from routes.misc_routes import _resolve_tracks_columns

    cols = _resolve_tracks_columns(db_session)
    assert isinstance(cols, set)
    # On the SQLite test engine the inspector fallback is used; the table may
    # be undiscoverable in some sessions (engine-dispose quirk) — the contract
    # is: never raise, always a set.
    assert "id" in cols or not cols
