"""Tests for the MusicBrainz release search's library-dedupe behaviour.

Regression: the album-page "Lookup on MusicBrainz" flow (and folder-match /
re-match flows) search for a SPECIFIC album the user already owns.  The shared
``/api/musicbrainz/search`` endpoint used to strip every release-group already
in the library in that case, so searching for an album in the user's
collection returned "No results found" even though MusicBrainz had it
(old_system's album lookup never deduped).

The dedupe is now limited to discovery-style searches (artist-only browsing /
free-text queries); an explicit ``album`` (or ``track``) term always includes
owned release-groups so the targeted lookups keep working.
"""

from __future__ import annotations

import pytest

from routes import musicbrainz_routes as mb_routes


OWNED_RGID = "11111111-1111-1111-1111-111111111111"
OTHER_RGID = "22222222-2222-2222-2222-222222222222"


def _make_group(rgid: str, title: str = "Tangaroa", primary: str = "Album") -> dict:
    return {
        "id": rgid,
        "title": title,
        "primary-type": primary,
        "secondary-types": None,
        "artist-credit": [{"name": "Alien Weaponry", "joinphrase": ""}],
        "first-release-date": "2021-09-17",
    }


def _make_payload() -> dict:
    return {
        "release-groups": [_make_group(OWNED_RGID), _make_group(OTHER_RGID, "Tangaroa", "Single")],
        "release-group-count": 2,
    }


class _FakeMbClient:
    """Minimal MusicBrainzHttpClient stand-in: returns fixed release-groups.

    Mirrors the real API shape per endpoint: the ``release-group/`` search
    index returns ``release-groups``; the ``release/`` index (used for
    track-targeted searches) returns ``releases`` each carrying a
    ``release-group``.
    """

    def get(self, endpoint: str, *, params=None, timeout: float = 10.0) -> dict:
        if endpoint.startswith("release/"):
            return {
                "releases": [
                    {"id": "rel-" + OWNED_RGID, "release-group": _make_group(OWNED_RGID)},
                    {"id": "rel-" + OTHER_RGID, "release-group": _make_group(OTHER_RGID, "Tangaroa", "Single")},
                ]
            }
        return _make_payload()


@pytest.fixture()
def fake_mb(monkeypatch):
    monkeypatch.setattr(mb_routes, "_get_mb_client", lambda: _FakeMbClient())


@pytest.fixture()
def dedupe_spy(monkeypatch):
    """Replace the real (Postgres-only) dedupe with one that removes the
    release-group the library owns, and record whether it was invoked."""
    calls = {"count": 0}

    def _fake_dedupe(releases):
        calls["count"] += 1
        return [r for r in releases if str(r.get("id") or "") != OWNED_RGID]

    monkeypatch.setattr(mb_routes, "_dedupe_owned_releases", _fake_dedupe)
    return calls


async def test_explicit_album_search_keeps_owned_release(client, fake_mb, dedupe_spy):
    """Searching for a specific album (album page lookup) must NOT strip the
    release-group the user already owns."""
    resp = await client.post("/api/musicbrainz/search", json={
        "artist": "Alien Weaponry",
        "album": "Tangaroa",
    })
    data = await resp.get_json()
    assert resp.status_code == 200
    assert data["success"] is True
    ids = {str(r.get("id")) for r in data["releases"]}
    assert OWNED_RGID in ids, "owned release-group must appear in a targeted album search"
    # The dedupe is discovery-only — it must not have run for an explicit album.
    assert dedupe_spy["count"] == 0


async def test_explicit_track_search_keeps_owned_release(client, fake_mb, dedupe_spy):
    """A track-targeted search (re-match flows) must also keep owned groups."""
    resp = await client.post("/api/musicbrainz/search", json={
        "artist": "Alien Weaponry",
        "track": "Kai Tangata",
    })
    data = await resp.get_json()
    assert resp.status_code == 200
    ids = {str(r.get("id")) for r in data["releases"]}
    assert OWNED_RGID in ids
    assert dedupe_spy["count"] == 0


async def test_artist_only_search_still_dedupes(client, fake_mb, dedupe_spy):
    """Discovery browsing (artist-only) keeps the library dedupe so the tab
    count reflects only albums the user does not already own."""
    resp = await client.post("/api/musicbrainz/search", json={
        "artist": "Alien Weaponry",
        "artist_only": True,
    })
    data = await resp.get_json()
    assert resp.status_code == 200
    ids = {str(r.get("id")) for r in data["releases"]}
    assert OWNED_RGID not in ids, "owned release-group must be stripped from discovery results"
    assert OTHER_RGID in ids
    assert dedupe_spy["count"] == 1


async def test_include_owned_still_respected(client, fake_mb, dedupe_spy):
    """The explicit ``include_owned`` opt-out must skip the dedupe even for
    discovery searches (folder-match / re-match flows)."""
    resp = await client.post("/api/musicbrainz/search", json={
        "artist": "Alien Weaponry",
        "artist_only": True,
        "include_owned": True,
    })
    data = await resp.get_json()
    assert resp.status_code == 200
    ids = {str(r.get("id")) for r in data["releases"]}
    assert OWNED_RGID in ids
    assert dedupe_spy["count"] == 0
