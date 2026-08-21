"""Tests for the album lookup release-picker prompt on ambiguous groups.

Symptom: on the album page, "Lookup on MusicBrainz" returned ONE row per
release-group; selecting a group auto-applied the group's "best" (guessed)
release even when the group has many concrete releases (editions / formats /
countries).  The user wanted to be asked which release they mean whenever
more than one exists.

The shared ``/api/musicbrainz/search`` now enriches every release-group
result with its concrete releases (``releases`` key, normalised to the
release-picker contract), so the album-page callback can prompt immediately
without an extra API call.
"""

from __future__ import annotations

import pytest

from routes import musicbrainz_routes as mb_routes

RGID = "33333333-3333-3333-3333-333333333333"


def _make_group(rgid: str = RGID, title: str = "Parallels") -> dict:
    return {
        "id": rgid,
        "title": title,
        "primary-type": "Album",
        "secondary-types": None,
        "artist-credit": [{"name": "Fleshgod Apocalypse", "joinphrase": ""}],
        "first-release-date": "2013-05-27",
    }


class _FakeMbClient:
    """Search returns one group; browse returns 2 concrete releases with media."""

    def get(self, endpoint: str, *, params=None, timeout: float = 10.0) -> dict:
        if endpoint.startswith("release/"):
            return {
                "releases": [
                    {
                        "id": "rel-a",
                        "title": "Parallels",
                        "date": "2013-05-27",
                        "country": "IT",
                        "status": "Official",
                        "media": [{"format": "CD", "track-count": 10}],
                    },
                ]
            }
        return {"release-groups": [_make_group()]}

    def browse_releases_for_group(self, release_group_mbid: str, inc: str = "media", limit: int = 50):
        return [
            {
                "id": "rel-a",
                "title": "Parallels",
                "date": "2013-05-27",
                "country": "IT",
                "status": "Official",
                "disambiguation": "",
                "media": [{"format": "CD", "track-count": 10}],
            },
            {
                "id": "rel-b",
                "title": "Parallels (Deluxe)",
                "date": "2013-11-01",
                "country": "US",
                "status": "Official",
                "disambiguation": "deluxe",
                "media": [{"format": "CD", "track-count": 13}],
            },
        ]


@pytest.fixture()
def fake_mb(monkeypatch):
    monkeypatch.setattr(mb_routes, "_get_mb_client", lambda: _FakeMbClient())


async def test_search_results_include_concrete_releases(client, fake_mb):
    """A release-group search result carries its concrete releases so the
    frontend can prompt which edition the user wants (opt-in via
    with_releases)."""
    resp = await client.post("/api/musicbrainz/search", json={
        "artist": "Fleshgod Apocalypse",
        "album": "Parallels",
        "with_releases": True,
    })
    data = await resp.get_json()
    assert resp.status_code == 200
    assert data["success"] is True

    group = next((r for r in data["releases"] if r.get("id") == RGID), None)
    assert group is not None
    releases = group.get("releases")
    assert releases is not None, "release-group results must carry their concrete releases"
    assert len(releases) == 2

    # Normalised to the release-picker contract (formats list, track_count,
    # disc_count) so the picker renders without a second API call.
    by_id = {r["id"]: r for r in releases}
    assert by_id["rel-a"]["formats"] == ["CD"]
    assert by_id["rel-a"]["track_count"] == 10
    assert by_id["rel-a"]["disc_count"] == 1
    assert by_id["rel-b"]["track_count"] == 13
    assert by_id["rel-b"]["disambiguation"] == "deluxe"
    # Sorted chronologically (blank dates last).
    assert [r["date"] for r in releases] == ["2013-05-27", "2013-11-01"]


async def test_search_default_skips_concrete_releases(client, fake_mb):
    """The universal search (no with_releases) must NOT browse every group's
    releases — that is the expensive N-API-calls path that made search slow.
    The groups come back without a releases key."""
    resp = await client.post("/api/musicbrainz/search", json={
        "artist": "Fleshgod Apocalypse",
        "album": "Parallels",
    })
    data = await resp.get_json()
    assert resp.status_code == 200
    group = next((r for r in data["releases"] if r.get("id") == RGID), None)
    assert group is not None
    assert "releases" not in group or not group["releases"]


async def test_single_release_group_still_enriched(client, fake_mb, monkeypatch):
    """Even a group with a single concrete release is enriched (the frontend
    can apply it directly)."""
    class _SingleClient(_FakeMbClient):
        def browse_releases_for_group(self, release_group_mbid, inc="media", limit=50):
            return [
                {
                    "id": "rel-a",
                    "title": "Parallels",
                    "date": "2013-05-27",
                    "country": "IT",
                    "media": [{"format": "CD", "track-count": 10}],
                },
            ]

    monkeypatch.setattr(mb_routes, "_get_mb_client", lambda: _SingleClient())
    resp = await client.post("/api/musicbrainz/search", json={
        "artist": "Fleshgod Apocalypse",
        "album": "Parallels",
        "with_releases": True,
    })
    data = await resp.get_json()
    group = next((r for r in data["releases"] if r.get("id") == RGID), None)
    assert group is not None
    assert len(group.get("releases") or []) == 1
