"""Route tests for Popularr API v1."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_api_v1_track_not_found(client):
    """GET /api/v1/tracks/nonexistent returns 404."""
    response = await client.get("/api/v1/tracks/nonexistent")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_api_search_no_query(client):
    """POST /api/search with no query returns 400."""
    response = await client.post("/api/search", json={})
    assert response.status_code == 400
    data = await response.get_json()
    assert "error" in data


@pytest.mark.asyncio
async def test_api_search_short_query(client):
    """POST /api/search with a single char returns 400."""
    response = await client.post("/api/search", json={"query": "a"})
    assert response.status_code == 400
