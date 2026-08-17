"""Regression tests for old-album rescan windows and singles-pass popularity refresh.

Covers:
- ``_album_release_is_old``: albums older than ``features.old_album_age_months``
  (default 48) are "old" and get the longer per-mode rescan windows; unknown
  release year is treated as recent.
- A singles scan refreshes popularity for tracks WITH stored popularity when
  the album is outside the popularity window (``refresh_popularity_if_due``),
  otherwise stored popularity is reused.
"""

from __future__ import annotations


def _run_singles_track(monkeypatch, track, options):
    """Run process_track in a singles pass, recording whether the popularity
    fetch ran (mirrors TestSinglesPassPopularityGating in test_album_type_persistence)."""
    import services.popularity.stages.track_stage as ts

    calls = {"fetch_popularity": 0, "singles": 0}
    monkeypatch.setattr(ts, "LastFmClient", lambda *a, **k: object())
    monkeypatch.setattr(ts, "ListenBrainzClient", lambda *a, **k: object())
    monkeypatch.setattr(ts, "get_shared_mb_service", lambda *a, **k: object())
    monkeypatch.setattr(ts, "get_shared_mb_client", lambda *a, **k: object())
    monkeypatch.setattr(ts, "get_search_aggregated_lastfm_popularity", lambda *a, **k: {})

    def fake_agg(*a, **k):
        calls["fetch_popularity"] += 1
        return {"listeners": 5000, "track_play": 9000, "matched_tracks": []}

    monkeypatch.setattr(ts, "get_aggregated_lastfm_popularity", fake_agg)
    monkeypatch.setattr(ts, "get_aggregated_listenbrainz_popularity", lambda *a, **k: {})
    monkeypatch.setattr(
        "helpers.config_helpers.get_config",
        lambda: {"api_integrations": {"lastfm": {"api_key": "test-key"}}},
    )

    def fake_detect(**kwargs):
        calls["singles"] += 1
        return {"is_single": True, "confidence": "high", "confidence_score": 0.9,
                "single_status": "high", "sources": [{"source": "musicbrainz", "matched": True}],
                "reasons": ["mb"]}

    monkeypatch.setattr(ts, "detect_single_for_track", fake_detect)
    monkeypatch.setattr(ts, "insert_or_update_track", lambda *a, **k: None)

    result = ts.process_track(
        track=track,
        track_context={"artist": track.get("artist"), "album": track.get("album")},
        album_context={"album": track.get("album"), "artist": track.get("artist"), "tracks": [track]},
        album_result={"detected_album_type": "album", "is_heterogeneous": False},
        options=options,
    )
    return result, calls


def _stored_track():
    return {
        "id": "t1", "artist": "Muse", "album": "Absolution", "title": "Hysteria",
        "final_score": 72.0, "popularity": 72.0,
        "lastfm_listeners": 1000, "listenbrainz_listens": 2000,
        "lb_percentile": 0.9, "single_detection_last_updated": None,
    }


class TestAlbumReleaseIsOld:
    def test_old_album_is_old(self):
        from services.popularity.scan_stage_runner import _album_release_is_old

        # 2020 album with the 48-month default is well over 4 years old.
        tracks = [{"year": 2020}, {"release_year": 2020}]
        assert _album_release_is_old(tracks) is True

    def test_recent_album_not_old(self):
        from services.popularity.scan_stage_runner import _album_release_is_old

        tracks = [{"year": 2025}, {"year": 2026}]
        assert _album_release_is_old(tracks) is False

    def test_unknown_year_not_old(self):
        from services.popularity.scan_stage_runner import _album_release_is_old

        assert _album_release_is_old([{"year": None}, {"title": "x"}]) is False
        assert _album_release_is_old([]) is False

    def test_custom_threshold(self, monkeypatch):
        import helpers.config_helpers as ch
        from services.popularity.scan_stage_runner import _album_release_is_old

        # 12-month threshold: a 2025 album is old.
        monkeypatch.setattr(ch, "get_feature", lambda k, d=None: 12 if k == "old_album_age_months" else d)
        assert _album_release_is_old([{"year": 2025}]) is True

        # 60-month threshold: the same 2025 album is recent.
        monkeypatch.setattr(ch, "get_feature", lambda k, d=None: 60 if k == "old_album_age_months" else d)
        assert _album_release_is_old([{"year": 2025}]) is False


class TestSinglesPassRefreshesStalePopularity:
    def test_singles_reuses_stored_without_refresh_flag(self, monkeypatch):
        result, calls = _run_singles_track(
            monkeypatch, _stored_track(), options={"singles_only": True}
        )
        assert result is not None
        # Stored popularity reused — NO popularity fetch.
        assert calls["fetch_popularity"] == 0
        assert result["popularity_score"] == 72.0
        assert calls["singles"] == 1

    def test_singles_refreshes_stored_when_due(self, monkeypatch):
        result, calls = _run_singles_track(
            monkeypatch,
            _stored_track(),
            options={"singles_only": True, "refresh_popularity_if_due": True},
        )
        assert result is not None
        # Stored popularity is refreshed even though the track has data.
        assert calls["fetch_popularity"] == 1
        assert result["lastfm_listeners"] == 5000
        assert calls["singles"] == 1
