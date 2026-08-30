"""Regression tests: Navidrome playlist sync uses form-encoded POST.

The old ``update_playlist_songs`` sent the whole song list as repeated
``songIdToAdd`` QUERY params — a 1119-song "Nu Metal - Top Tracks" playlist
blew past the URL length limit (``URL component 'query' too long``) and
every sync failed.  Subsonic's REST endpoints accept POST with params in the
request body; Navidrome implements it.  The fix sends large playlists as a
form-encoded POST body (no URL cap), and ``createPlaylist`` uses the same
safe path (it previously mis-serialised the ``songId`` list through the
query-param helper).
"""

from __future__ import annotations

import pytest

from api_clients.navidrome import NavidromeClient


class _FakeSession:
    """Records the POST body and returns a canned subsonic-response."""

    def __init__(self, ok=True, status_code=200):
        self._ok = ok
        self._status_code = status_code
        self.posted = []  # (url, data, kwargs)

    def post(self, url, data=None, **kwargs):
        # Record the dict as-is; _body_dict flattens list values into the
        # repeated form pairs httpx's encode_urlencoded_data would produce.
        self.posted.append((url, dict(data or {}), kwargs))
        response = _FakeResponse(self._status_code)
        return response

    def get(self, url, params=None, **kwargs):
        raise AssertionError("update_playlist_songs must NOT use GET")


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            from httpx import HTTPStatusError, Request, Response as HXResponse
            req = Request("POST", "http://x")
            raise HTTPStatusError("err", request=req, response=HXResponse(self.status_code, request=req))

    def json(self):
        return {"subsonic-response": {"status": "ok"}}


class _FakeClient(NavidromeClient):
    """Client with fetch_playlist stubbed (no network)."""

    def __init__(self, session, current_count=0):
        super().__init__(base_url="http://navidrome:4533", username="u", password="p")
        self.session = session
        self._current_count = current_count

    def fetch_playlist(self, playlist_id):
        return {"id": playlist_id, "tracks": [{"id": f"t{i}"} for i in range(self._current_count)]}


def _body_dict(body):
    """Flatten a dict-of-lists into a dict-of-lists of str values.

    Mirrors httpx's ``encode_urlencoded_data``: a dict whose value is a list
    becomes repeated ``key=value`` pairs (``songIdToAdd=a&songIdToAdd=b``),
    scalars stay single.  Values are stringified (httpx uses
    ``primitive_value_to_str``).
    """
    out: dict[str, list[str]] = {}
    for k, v in (body or {}).items():
        if isinstance(v, (list, tuple)):
            out.setdefault(k, []).extend(str(item) for item in v)
        else:
            out.setdefault(k, []).append(str(v))
    return out


def test_update_playlist_uses_dict_body_not_tuple_list():
    """The POST body must be a DICT, never a raw list of tuples.

    httpx 0.28's ``encode_request`` treats a non-Mapping ``data`` (e.g. a
    list of ``(key, value)`` tuples) as RAW byte content — ``b"".join(...)``
    then fails with ``sequence item 1: expected a bytes-like object, tuple
    found`` (the reported regression).  A dict is the only form httpx
    url-encodes correctly, flattening list values into repeated pairs.
    """
    session = _FakeSession()
    client = _FakeClient(session, current_count=0)

    ok = client.update_playlist_songs("playlist-1", ["a", "b"])

    assert ok is True
    _, body, _ = session.posted[0]
    # The recorded data must be a dict (httpx encode_urlencoded_data input).
    assert isinstance(body, dict)
    assert body["playlistId"] == "playlist-1"
    assert body["songIdToAdd"] == ["a", "b"]


def test_update_playlist_large_song_list_uses_post_body():
    """A 1000+ song playlist syncs via POST body, never a query string."""
    session = _FakeSession()
    client = _FakeClient(session, current_count=0)

    song_ids = [f"song-{i:04d}" for i in range(1500)]  # 1500 songs
    ok = client.update_playlist_songs("playlist-1", song_ids)

    assert ok is True
    assert len(session.posted) == 1
    url, body, kwargs = session.posted[0]
    assert url.endswith("/rest/updatePlaylist")
    # Form-encoded POST body (no query params).
    assert "params" not in kwargs or not kwargs.get("params")

    body = _body_dict(body)
    # All 1500 songIds present as repeated form fields.
    assert len(body.get("songIdToAdd", [])) == 1500
    assert body["songIdToAdd"][0] == "song-0000"
    assert body["songIdToAdd"][-1] == "song-1499"
    # Auth + playlistId present.
    assert body.get("playlistId") == ["playlist-1"]
    assert any(k in body for k in ("u", "t", "p"))


def test_update_playlist_removes_existing_entries():
    """Current entries are removed via songIndexToRemove in the body."""
    session = _FakeSession()
    client = _FakeClient(session, current_count=7)

    ok = client.update_playlist_songs("playlist-1", ["a", "b", "c"])

    assert ok is True
    _, body, _ = session.posted[0]
    body = _body_dict(body)
    # Indices 0..6 removed, then 3 songs added.
    assert [int(v) for v in body.get("songIndexToRemove", [])] == [0, 1, 2, 3, 4, 5, 6]
    assert body.get("songIdToAdd") == ["a", "b", "c"]


def test_update_playlist_empties_playlist():
    """Empty song list = remove every entry, add nothing."""
    session = _FakeSession()
    client = _FakeClient(session, current_count=4)

    ok = client.update_playlist_songs("playlist-1", [])

    assert ok is True
    _, body, _ = session.posted[0]
    body = _body_dict(body)
    assert [int(v) for v in body.get("songIndexToRemove", [])] == [0, 1, 2, 3]
    assert "songIdToAdd" not in body


class _NonJsonResponse(_FakeResponse):
    """A 2xx response whose body is NOT JSON (Navidrome sometimes returns an
    HTML/plain body for mutation endpoints)."""

    def __init__(self, body: str):
        super().__init__(200)
        self._body = body
        self.content = body.encode("utf-8")
        self.text = body

    def json(self):
        import json
        raise json.JSONDecodeError("Expecting value", self.text, 0)


class _NonJsonSession:
    def __init__(self, body: str):
        self._body = body
        self.posted = []

    def post(self, url, data=None, **kwargs):
        self.posted.append((url, dict(data or {}), kwargs))
        return _NonJsonResponse(self._body)

    def get(self, url, params=None, **kwargs):
        raise AssertionError("must not GET")


def test_update_playlist_non_json_2xx_treated_as_success():
    """Regression: Navidrome's updatePlaylist returned a non-JSON 2xx body,
    and ``_post_subsonic_response`` raised JSONDecodeError → every playlist
    sync logged "Navidrome updatePlaylist POST failed".  A 2xx with a
    non-JSON body is a SUCCESS for a mutation endpoint — it must return
    ``{"status": "ok"}`` without error."""
    import json

    from api_clients.navidrome import NavidromeClient

    session = _NonJsonSession("<html><body>ok</body></html>")
    client = _FakeClient(session)

    # Directly exercise the POST path (bypasses fetch_playlist).
    data = client._post_subsonic_response("updatePlaylist", playlistId="p1", songIdToAdd=["a"])
    assert data.get("status") == "ok"

    # And through update_playlist_songs (which asserts ok).
    ok = client.update_playlist_songs("playlist-1", ["a"])
    assert ok is True


def test_create_playlist_uses_post_body():
    """createPlaylist sends name + songId list via POST body (no query)."""
    session = _FakeSession()
    client = _FakeClient(session)

    song_ids = [f"song-{i:04d}" for i in range(1200)]
    data = client.create_playlist("New Music", song_ids)

    assert data.get("status") == "ok"
    assert len(session.posted) == 1
    url, body, kwargs = session.posted[0]
    assert url.endswith("/rest/createPlaylist")
    body = _body_dict(body)
    assert body.get("name") == ["New Music"]
    assert len(body.get("songId", [])) == 1200


def test_sync_playlist_by_name_uses_create_playlist_method(monkeypatch):
    """The service's create path calls the client's POST method."""
    from services.playlists import playlist_navidrome_service as pns

    created = {}

    class _FakeNavidromeClient:
        def fetch_all_playlists(self):
            return []

        def create_playlist(self, name, song_ids):
            created["name"] = name
            created["song_ids"] = song_ids
            return {"status": "ok", "playlist": {"id": "pid-1"}}

    client = _FakeNavidromeClient()
    result = pns.sync_playlist_by_name(client, "New Music", ["a", "b", "c"])

    assert result["created"] is True
    assert result["playlist_id"] == "pid-1"
    assert created["name"] == "New Music"
    assert created["song_ids"] == ["a", "b", "c"]
