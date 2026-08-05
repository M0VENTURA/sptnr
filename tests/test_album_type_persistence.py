"""Regression tests for scan-pipeline bugs.

Covers:
- ``track_stage`` must not clobber album-type columns with the stale
  in-memory value loaded before album enrichment ran (the album page showed
  "Unknown" after a full scan).
- ``ensure_album_type`` returns the detected/reused verdict so the runner's
  singles-only pass can feed it to per-track singles detection.
"""

from __future__ import annotations

import pytest


class TestAlbumTypeNotClobbered:
    """track_stage persistence must preserve album-type columns."""

    def test_strip_album_type_columns_removes_unset_columns(self):
        from services.popularity.stages.track_stage import _strip_album_type_columns

        track = {
            "id": "t1",
            "title": "Song",
            "musicbrainz_albumtype": "",  # stale value loaded before enrich_album
            "spotify_album_type": "album",
            "releasetype": "album",
            "final_score": 50.0,
        }
        payload = {"final_score": 55.0}  # track stage only updates scoring

        stripped = _strip_album_type_columns(track, payload)

        # Album-type columns the track stage didn't update are dropped so the
        # DB keeps whatever the album stage persisted.
        assert "musicbrainz_albumtype" not in stripped
        assert "spotify_album_type" not in stripped
        assert "releasetype" not in stripped
        assert stripped["id"] == "t1"
        assert stripped["title"] == "Song"
        assert stripped["final_score"] == 50.0

    def test_strip_album_type_columns_keeps_explicit_updates(self):
        from services.popularity.stages.track_stage import _strip_album_type_columns

        track = {"id": "t1", "musicbrainz_albumtype": ""}
        payload = {"musicbrainz_albumtype": "single"}

        stripped = _strip_album_type_columns(track, payload)

        # If the track stage explicitly produced an album type, it is kept.
        assert stripped["musicbrainz_albumtype"] == "single"


class TestEnsureAlbumTypeReturnsVerdict:
    """ensure_album_type should return the reused/detected album type."""

    def test_returns_stored_consistent_verdict(self):
        from services.popularity.stages.album_stage import ensure_album_type

        album_row = {
            "artist": "Muse",
            "album": "Absolution",
            "tracks": [
                {"id": "t1", "musicbrainz_albumtype": "album"},
                {"id": "t2", "musicbrainz_albumtype": "album"},
            ],
        }
        assert ensure_album_type(album_row) == "album"

    def test_returns_none_when_no_artist(self):
        from services.popularity.stages.album_stage import ensure_album_type

        assert ensure_album_type({"artist": "", "album": "X"}) is None

    def test_detects_when_missing(self, monkeypatch):
        from services.popularity.stages.album_stage import ensure_album_type
        import services.popularity.stages.album_stage as album_stage

        # No stored verdict + no network: name-based detection returns "album".
        monkeypatch.setattr(album_stage, "_lookup_musicbrainz_album_type", lambda artist, album: (None, None))

        # Avoid opening a real DB connection — the persist helper is patched
        # away so only the detection path is exercised.
        persisted = {}

        def fake_persist(conn, cursor, artist, album, tracks, album_type, rg_mbid):
            persisted["album_type"] = album_type

        monkeypatch.setattr(album_stage, "_persist_album_type_to_tracks", fake_persist)

        class _FakeConn:
            def cursor(self):
                return object()

            def close(self):
                pass

        monkeypatch.setattr(album_stage, "get_db_connection", lambda: _FakeConn())

        album_row = {
            "artist": "Muse",
            "album": "Absolution",
            "album_artist": "Muse",
            "tracks": [{"id": "t1", "musicbrainz_albumtype": None}],
        }
        assert ensure_album_type(album_row) == "album"
        assert persisted.get("album_type") == "album"


class TestSinglesOnlyPass:
    """process_track must run singles detection in singles-detection-only mode
    while skipping the metadata/popularity/cover/genre sections."""

    def _run_process_track(self, monkeypatch, track, options=None, track_context=None):
        import services.popularity.stages.track_stage as ts

        calls = {"metadata": 0, "popularity": 0, "cover": 0, "genre": 0, "singles": 0, "persist": 0}
        results = []

        def _track_phase(*a, **k):
            if options and options.get("metadata_only"):
                return "Metadata"
            if options and (options.get("singles_only") or options.get("singles_with_missing_popularity")):
                return "Singles"
            if options and options.get("popularity_only"):
                return "Popularity"
            return "Full"

        original = ts.process_track

        # Patch the expensive API clients so no network/DB is touched, and
        # record which sections actually ran by wrapping detection entry points.
        monkeypatch.setattr(ts, "MusicBrainzHttpClient", lambda *a, **k: object())
        monkeypatch.setattr(ts, "LastFmClient", lambda *a, **k: object())
        monkeypatch.setattr(ts, "ListenBrainzClient", lambda *a, **k: object())
        monkeypatch.setattr(ts, "MusicBrainzService", lambda *a, **k: object())
        monkeypatch.setattr(ts, "get_aggregated_lastfm_popularity", lambda *a, **k: {})
        monkeypatch.setattr(ts, "get_aggregated_listenbrainz_popularity", lambda *a, **k: {})
        monkeypatch.setattr(ts, "get_search_aggregated_lastfm_popularity", lambda *a, **k: {})

        def fake_detect(title=None, artist=None, album_track_count=1, popularity=0,
                        album_type=None, album=None, isrc=None, duration=None,
                        use_advanced_detection=True, persist_result=False,
                        mb_cached_singles=None, discogs_cached_singles=None,
                        artist_mbid=None, listenbrainz_listens=0,
                        discogs_token=None, lastfm_client=None, mb_client=None):
            calls["singles"] += 1
            return {"is_single": True, "confidence": "high", "confidence_score": 0.9,
                    "single_status": "high", "sources": [{"source": "musicbrainz", "matched": True}],
                    "reasons": ["mb"]}

        monkeypatch.setattr(ts, "detect_single_for_track", fake_detect)

        def fake_insert(track_id, data):
            calls["persist"] += 1

        monkeypatch.setattr(ts, "insert_or_update_track", fake_insert)

        result = original(
            track=track,
            track_context=track_context or {"artist": track.get("artist"), "album": track.get("album")},
            album_context={"album": track.get("album"), "artist": track.get("artist"), "tracks": [track]},
            album_result={"detected_album_type": "album", "is_heterogeneous": False},
            options=options or {},
        )
        return result, calls

    def test_singles_only_runs_detection_without_popularity(self, monkeypatch):
        track = {
            "id": "t1",
            "artist": "Muse",
            "album": "Absolution",
            "title": "Hysteria",
            "final_score": 50.0,
            "lastfm_listeners": 1000,
            "listenbrainz_listens": 2000,
            "lb_percentile": 0.9,
            "single_detection_last_updated": None,
        }
        result, calls = self._run_process_track(
            monkeypatch,
            track,
            options={"singles_detection_only": True},
        )
        assert result is not None
        assert calls["singles"] == 1
        # Stored popularity is carried through, not zeroed.
        assert result["popularity_score"] == 50.0
        assert result["is_single"] is True
        assert result["single_confidence"] == "high"

    def test_singles_only_carries_stored_single_state_when_fresh(self, monkeypatch):
        # When singles detection is fresh, stored is_single/confidence/sources
        # must be carried into the result so per-album output still shows them.
        import datetime as _dt

        track = {
            "id": "t1",
            "artist": "Muse",
            "album": "Absolution",
            "title": "Hysteria",
            "final_score": 50.0,
            "is_single": True,
            "single_confidence": "high",
            "single_sources": '[{"source":"musicbrainz","matched":true}]',
            "single_detection_last_updated": _dt.datetime.now(_dt.timezone.utc),
            "lastfm_listeners": 1000,
            "listenbrainz_listens": 2000,
        }
        result, calls = self._run_process_track(
            monkeypatch,
            track,
            options={"singles_detection_only": True},
        )
        assert result is not None
        # Fresh gate means detection itself is skipped, but the stored verdict
        # is preserved in the returned result.
        assert result["is_single"] is True
        assert result["single_confidence"] == "high"
        assert result["single_sources"]
        assert result["popularity_score"] == 50.0

