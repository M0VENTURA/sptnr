"""Regression tests for /api/artist/import-release.

Covers:
- The route coerces payload values to str BEFORE .strip() — a Discogs integer
  release ID arrives as a JSON number and previously crashed with
  AttributeError: 'int' object has no attribute 'strip'.
- import_release routes by ID shape: MusicBrainz UUID (with release-group
  fallback) vs Discogs integer ID.
- Imported tracks carry a stable ``id`` (required by save_to_db).
"""

from __future__ import annotations

import re

import pytest


def _mb_release_payload(release_id="11111111-2222-3333-4444-555555555555"):
    return {
        "id": release_id,
        "date": "2021-08-23",
        "media": [
            {
                "position": 1,
                "tracks": [
                    {"title": "Song One", "length": 200000, "recording": {"id": "aaaaaaaa-1111-2222-3333-444444444444", "title": "Song One"}},
                    {"title": "Song Two", "length": 180000, "recording": {"id": "bbbbbbbb-1111-2222-3333-444444444444", "title": "Song Two"}},
                ],
            }
        ],
    }


def _discogs_release_payload(release_id=1234567):
    return {
        "id": release_id,
        "year": 2021,
        "tracklist": [
            {"position": "A1", "title": "Song One", "duration": "3:20"},
            {"position": "A2", "title": "Song Two", "duration": "3:00"},
        ],
    }


class TestRouteCoercesPayloadToStr:
    def test_numeric_release_id_does_not_crash(self, monkeypatch):
        # Directly exercise the route's coercion logic: numeric release_id
        # (Discogs) must become a string, not raise AttributeError.
        from routes import artist_routes as ar

        calls = {}

        class _FakePayload:
            def get(self, key, default=None):
                # Simulates JSON: release_id is an int, artist/title are str.
                return {"artist": "Stray Kids", "release_id": 1234567, "title": "NOEASY"}.get(key, default)

        async def _fake_json():
            return _FakePayload()

        async def _fake_import(artist, release_id, title):
            calls["artist"] = artist
            calls["release_id"] = release_id
            calls["title"] = title
            return {"success": True}, 200

        import types

        # Patch the request context with a fake request + route dependencies.
        async def _run():
            fake_req = types.SimpleNamespace(get_json=_fake_json)
            monkeypatch.setattr(ar, "request", fake_req)
            monkeypatch.setattr(ar, "scan_import_release", _fake_import)
            return await ar.api_import_release()

        import asyncio
        resp = asyncio.run(_run())

        assert isinstance(calls["release_id"], str)
        assert calls["release_id"] == "1234567"
        assert calls["artist"] == "Stray Kids"
        assert calls["title"] == "NOEASY"
        assert resp[1] == 200


class TestImportReleaseRouting:
    def _patch_save(self, monkeypatch):
        import services.metadata.artist_scan_service as svc

        saved = []

        def fake_save(track_record):
            saved.append(track_record)
            return True

        monkeypatch.setattr(svc, "save_to_db", fake_save)
        return saved

    def test_musicbrainz_uuid_imports_tracks_with_ids(self, monkeypatch):
        import services.metadata.artist_scan_service as svc

        saved = self._patch_save(monkeypatch)
        from api_clients import musicbrainz_http as mbh

        class _FakeMB:
            def get_release(self, release_id, inc="recordings"):
                return _mb_release_payload()

            def get(self, endpoint, **kwargs):
                return {}

        monkeypatch.setattr(mbh, "MusicBrainzHttpClient", _FakeMB)

        result, code = svc.import_release("Stray Kids", "11111111-2222-3333-4444-555555555555", "NOEASY")

        assert code == 200
        assert result["tracks_imported"] == 2
        assert len(saved) == 2
        # Every track has a stable id (required by save_to_db).
        for rec in saved:
            assert rec["id"]
        assert saved[0]["id"] == "aaaaaaaa-1111-2222-3333-444444444444"
        assert saved[1]["id"] == "bbbbbbbb-1111-2222-3333-444444444444"
        assert saved[0]["track_number"] == 1
        assert saved[0]["year"] == "2021"

    def test_musicbrainz_release_group_fallback(self, monkeypatch):
        import services.metadata.artist_scan_service as svc

        saved = self._patch_save(monkeypatch)
        from api_clients import musicbrainz_http as mbh

        rg_mbid = "99999999-8888-7777-6666-555555555555"
        release_mbid = "11111111-2222-3333-4444-555555555555"
        calls = []

        class _FakeMB:
            def get_release(self, release_id, inc="recordings"):
                calls.append(("get_release", release_id))
                if release_id == release_mbid:
                    return _mb_release_payload(release_mbid)
                raise Exception("404 Not Found")

            def get(self, endpoint, **kwargs):
                return {"releases": [{"id": release_mbid}]}

        monkeypatch.setattr(mbh, "MusicBrainzHttpClient", _FakeMB)

        result, code = svc.import_release("Stray Kids", rg_mbid, "NOEASY")

        assert code == 200
        assert result["tracks_imported"] == 2
        # The release-group MBID 404'd, then the fallback resolved a release.
        assert ("get_release", rg_mbid) in calls
        assert ("get_release", release_mbid) in calls

    def test_discogs_integer_id_imports_tracks(self, monkeypatch):
        import services.metadata.artist_scan_service as svc

        saved = self._patch_save(monkeypatch)
        from api_clients import discogs as dc

        class _FakeDiscogs:
            def __init__(self, token):
                pass

            def get_release(self, release_id):
                return _discogs_release_payload(int(release_id))

        monkeypatch.setattr(dc, "DiscogsClient", _FakeDiscogs)
        monkeypatch.setattr(
            "helpers.config_helpers.get_config",
            lambda: {"api_integrations": {"discogs": {"token": "test-token"}}},
        )

        result, code = svc.import_release("Stray Kids", "1234567", "NOEASY")

        assert code == 200
        assert result["tracks_imported"] == 2
        assert len(saved) == 2
        # Composite fallback id when the Discogs tracklist has no recording MBID.
        assert saved[0]["id"] == "1234567_1_1"
        assert saved[0]["title"] == "Song One"
        assert saved[1]["id"] == "1234567_1_2"

    def test_unknown_id_shape_returns_error(self, monkeypatch):
        import services.metadata.artist_scan_service as svc

        self._patch_save(monkeypatch)
        from api_clients import discogs as dc

        class _FakeDiscogs:
            def __init__(self, token):
                pass

            def get_release(self, release_id):
                return None

        monkeypatch.setattr(dc, "DiscogsClient", _FakeDiscogs)

        result, code = svc.import_release("Stray Kids", "not-a-real-id", "X")

        assert code == 404
        assert result["error"] == "Release not found on Discogs"
