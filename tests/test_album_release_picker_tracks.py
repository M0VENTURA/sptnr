"""Tests for the album release-picker track-list endpoint contract.

The release-picker's "Tracks" button renders the ACTUAL MusicBrainz track
numbers per release (multi-disc editions, gaps) so the user can tell which
version of the album they are looking at.  The route must return the legacy
``{success, release_mbid, tracks}`` envelope with each track carrying
``track_number`` / ``disc_number`` — NOT a bare list (which the renderer
treated as "no track data").
"""

from __future__ import annotations

import pytest

from routes import album_routes as ar


class _FakeTracks:
    """Returns a bare track list (what get_musicbrainz_release_tracks does)."""

    def __init__(self, tracks):
        self._tracks = tracks

    def __call__(self, release_mbid):
        return self._tracks


def _make_tracks():
    return [
        {
            "disc_number": 1,
            "track_number": 1,
            "title": "Song One",
            "duration": 240000,
            "recording_mbid": "rec-0001",
        },
        {
            "disc_number": 2,
            "track_number": 1,
            "title": "Song Two",
            "duration": 250000,
            "recording_mbid": "rec-0002",
        },
    ]


async def test_release_tracks_returns_envelope(client, monkeypatch):
    """A bare track list is wrapped in the legacy {success, tracks} envelope."""
    monkeypatch.setattr(ar, "get_musicbrainz_release_tracks", _FakeTracks(_make_tracks()))
    resp = await client.post("/api/album/musicbrainz/release/tracks", json={
        "release_mbid": "rel-0000-0000-0000-000000000001",
    })
    assert resp.status_code == 200
    data = await resp.get_json()
    assert data["success"] is True
    assert data["release_mbid"] == "rel-0000-0000-0000-000000000001"
    assert len(data["tracks"]) == 2
    assert data["tracks"][0]["track_number"] == 1
    assert data["tracks"][1]["disc_number"] == 2


async def test_release_tracks_empty_list(client, monkeypatch):
    """An empty track list still returns the envelope, not an error."""
    monkeypatch.setattr(ar, "get_musicbrainz_release_tracks", _FakeTracks([]))
    resp = await client.post("/api/album/musicbrainz/release/tracks", json={
        "release_mbid": "rel-0000-0000-0000-000000000002",
    })
    assert resp.status_code == 200
    data = await resp.get_json()
    assert data["success"] is True
    assert data["tracks"] == []


async def test_release_tracks_missing_mbid(client):
    resp = await client.post("/api/album/musicbrainz/release/tracks", json={})
    assert resp.status_code == 400
    data = await resp.get_json()
    assert data["success"] is False
