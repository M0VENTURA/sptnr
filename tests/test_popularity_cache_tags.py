"""Tests for Last.fm top-tags captured by the bulk popularity cache prefetch.

The ``artist.getTopTracks`` call returns a ``toptags`` block per track.  The
cache service must extract those tags and carry them through the prefetch
entries so the track stage can persist ``lastfm_tags`` (the tag source the
artist page aggregates) without a per-track ``track.getInfo`` call.
"""

from __future__ import annotations

import json


class FakeLastFmClient:
    def __init__(self, tracks):
        self._tracks = tracks
        self.calls = 0

    def get_artist_top_tracks(self, artist, limit=200):
        self.calls += 1
        return self._tracks


def _track(name, listeners, playcount, tags=None):
    track = {"name": name, "listeners": listeners, "playcount": playcount}
    if tags:
        track["toptags"] = {"tag": [{"name": t} for t in tags]}
    return track


class TestExtractTopTags:
    def _extract(self, track):
        from services.popularity.popularity_cache_service import _extract_top_tags
        return _extract_top_tags(track)

    def test_extracts_names(self):
        raw = self._extract(_track("Song", 100, 200, tags=["Rock", "Alt"]))
        assert json.loads(raw) == ["Rock", "Alt"]

    def test_missing_toptags_returns_empty(self):
        assert self._extract(_track("Song", 100, 200)) == ""

    def test_deduplicates(self):
        raw = self._extract(_track("Song", 100, 200, tags=["Rock", "rock"]))
        assert json.loads(raw) == ["Rock"]


class TestPrefetchCarriesTags:
    def test_entries_include_tags(self):
        from services.popularity import popularity_cache_service as svc
        client = FakeLastFmClient([
            _track("Heartbeat", 1000, 5000, tags=["Synthpop", "Electronic"]),
            _track("Other", 100, 200),
        ])
        tracks = [{"title": "Heartbeat", "recording_mbid": None}]
        # Clear module caches so the fake client is actually called.
        svc._lf_top_tracks_cache.clear()
        svc._lf_top_tracks_titles.clear()
        svc._lf_top_tracks_tags.clear()
        entries = svc.prefetch_artist_popularity(
            "Fake Artist", tracks, lastfm_client=client, cache_full_catalogue=False,
        )
        entry = entries.get("heartbeat")
        assert entry is not None
        assert entry["lastfm_listeners"] == 1000
        assert json.loads(entry["lastfm_tags"]) == ["Synthpop", "Electronic"]

    def test_persist_rows_include_tags(self):
        from services.popularity import popularity_cache_service as svc
        client = FakeLastFmClient([
            _track("Heartbeat", 1000, 5000, tags=["Synthpop"]),
        ])
        tracks = [{"title": "Heartbeat", "recording_mbid": None}]
        svc._lf_top_tracks_cache.clear()
        svc._lf_top_tracks_titles.clear()
        svc._lf_top_tracks_tags.clear()
        rows = []
        import services.popularity.popularity_cache_service as mod
        original = mod.cache_repo.upsert_track_popularity_bulk
        mod.cache_repo.upsert_track_popularity_bulk = lambda r: rows.extend(r) or len(r)
        try:
            svc.prefetch_artist_popularity(
                "Fake Artist", tracks, lastfm_client=client, cache_full_catalogue=False,
            )
        finally:
            mod.cache_repo.upsert_track_popularity_bulk = original
        assert any(r.get("lastfm_tags") and json.loads(r["lastfm_tags"]) == ["Synthpop"]
                   for r in rows)
