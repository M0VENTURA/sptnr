"""Regression tests: promo-only Discogs singles resolve to medium, not high.

Some tracks are confirmed on Discogs only by a PROMOTIONAL release (format
contains "Promo", e.g. "CD, Single, Promo" / 7" promo). A promo is genuine
evidence the track was issued as a single, but it is promotional — weaker
than a commercial single. Legacy parity (``old_system/discogs_verification.py``
Rule 3) resolved promos to ``medium`` confidence.

Fix: ``DiscogsService.get_single_status`` exposes ``is_promo``; a promo-only
Discogs match is counted as a MEDIUM source and the final verdict is capped
at ``'medium'`` unless an independent high-confidence source also confirms.
"""

from __future__ import annotations

import pytest


class FakeDiscogsHttp:
    """Simulates Discogs API responses with a promo-only single on the artist list."""

    def __init__(self, token="", **kw):
        self.token = token
        self.search_calls = 0

    def search_database(self, params, timeout=10.0):
        self.search_calls += 1
        return []

    def get_artist_releases(self, artist_id, per_page=100, timeout=10.0):
        return [
            {"title": "Some Album", "format": ["CD", "Album"], "role": "Main", "year": 2006},
            {"title": "Lycanthrope", "format": ["CD", "Single", "Promo"], "role": "Main", "year": 2006},
            {"title": "Promo Single Track", "format": ["Vinyl", "7\"", "Promo"], "role": "Main", "year": 2006},
            {"title": "155", "format": ["CD", "Single", "Limited Edition"], "role": "Main", "year": 2006},
        ]


class TestDiscogsServiceGetSingleStatus:
    def _svc(self, http=None):
        from services.enrichment.discogs_service import DiscogsService
        svc = DiscogsService(token="test-token", http_client=http or FakeDiscogsHttp(token="test-token"))
        svc.get_artist_id = lambda artist, timeout=10.0: "640496"
        return svc

    def test_promo_only_release_reported_as_promo(self):
        svc = self._svc()
        status = svc.get_single_status("Lycanthrope", "+44")
        assert status["is_single"] is True
        assert status["is_promo"] is True
        assert "promo" in status["format"]

    def test_commercial_single_not_promo(self):
        svc = self._svc()
        status = svc.get_single_status("155", "+44")
        assert status["is_single"] is True
        assert status["is_promo"] is False

    def test_non_single_not_promo(self):
        svc = self._svc()
        status = svc.get_single_status("Random Deep Cut", "+44")
        assert status["is_single"] is False
        assert status["is_promo"] is False

    def test_is_single_still_returns_bool(self):
        svc = self._svc()
        assert svc.is_single("Lycanthrope", "+44") is True
        assert svc.is_single("Random Deep Cut", "+44") is False

    def test_commercial_single_preferred_over_promo_when_both_exist(self):
        from services.enrichment.discogs_service import DiscogsService

        class Http(FakeDiscogsHttp):
            def get_artist_releases(self, artist_id, per_page=100, timeout=10.0):
                return [
                    {"title": "Both Ways", "format": ["CD", "Single", "Promo"], "role": "Main", "year": 2006},
                    {"title": "Both Ways", "format": ["Vinyl", "7\"", "Single"], "role": "Main", "year": 2006},
                ]

        svc = DiscogsService(token="test-token", http_client=Http(token="test-token"))
        svc.get_artist_id = lambda artist, timeout=10.0: "640496"
        status = svc.get_single_status("Both Ways", "Some Artist")
        assert status["is_single"] is True
        assert status["is_promo"] is False


class TestDetectDiscogsPromoSource:
    def test_full_path_promo_returns_medium_confidence(self, monkeypatch):
        from services.enrichment import discogs_service as ds_module
        from services.enrichment.single_detection_service import _detect_discogs

        class FakeService:
            def __init__(self, *args, **kwargs):
                pass

            def get_single_status(self, title, artist, album_context=None):
                return {
                    "is_single": True,
                    "is_promo": True,
                    "release_year": 2006,
                    "release_id": "1",
                    "format": "cd single promo",
                }

        monkeypatch.setattr(ds_module, "_get_service", lambda token: FakeService())

        result = _detect_discogs("Lycanthrope", "+44", "Album", "test-token")
        assert result["matched"] is True
        assert result["confidence"] == 0.5
        assert result["metadata"].get("is_promo") is True

    def test_full_path_commercial_keeps_high_confidence(self, monkeypatch):
        from services.enrichment import discogs_service as ds_module
        from services.enrichment.single_detection_service import _detect_discogs

        class FakeService:
            def __init__(self, *args, **kwargs):
                pass

            def get_single_status(self, title, artist, album_context=None):
                return {
                    "is_single": True,
                    "is_promo": False,
                    "release_year": 2006,
                    "release_id": "2",
                    "format": "vinyl 7 single",
                }

        monkeypatch.setattr(ds_module, "_get_service", lambda token: FakeService())

        result = _detect_discogs("155", "+44", "Album", "test-token")
        assert result["matched"] is True
        assert result["confidence"] == 0.8
        assert result["metadata"].get("is_promo") is False

    def test_fast_path_cached_promo_flags_promo(self):
        from services.enrichment.single_detection_service import _detect_discogs

        result = _detect_discogs(
            "Lycanthrope", "+44", "Album", "test-token",
            cached_single_titles={"lycanthrope"},
            cached_promo_titles={"lycanthrope"},
        )
        assert result["matched"] is True
        assert result["confidence"] == 0.5
        assert result["metadata"].get("is_promo") is True
        assert result.get("cached") is True

    def test_fast_path_cached_commercial_not_promo(self):
        from services.enrichment.single_detection_service import _detect_discogs

        result = _detect_discogs(
            "155", "+44", "Album", "test-token",
            cached_single_titles={"155"},
            cached_promo_titles=set(),
        )
        assert result["matched"] is True
        assert result["confidence"] == 0.8
        assert result["metadata"].get("is_promo") is False


class TestDetermineFinalStatusPromoCap:
    def test_promo_only_capped_at_medium_even_at_high_z(self):
        from services.enrichment.single_detection_service import determine_final_status

        # Promo counts as a single MEDIUM source. No independent high source.
        result = determine_final_status(
            album_z=1.5, artist_z=1.5,
            discogs=True,
            zscore_high=1.0, zscore_medium=0.6,
            high_sources=0, medium_sources=1,
            discogs_promo=True,
        )
        assert result == "medium"

    def test_promo_with_independent_high_source_stays_high(self):
        from services.enrichment.single_detection_service import determine_final_status

        result = determine_final_status(
            album_z=1.5, artist_z=1.5,
            discogs=True, musicbrainz=True,
            zscore_high=1.0, zscore_medium=0.6,
            high_sources=1, medium_sources=1,
            discogs_promo=True,
        )
        assert result == "high"


class TestReleaseCachePromoPersistence:
    def test_fetch_discogs_releases_marks_promos(self, monkeypatch):
        from services.popularity import release_cache_service as rcs

        class FakeHttp:
            def get_artist_releases(self, artist_id, per_page=100, timeout=10.0):
                return [
                    {"title": "Promo Track", "format": ["CD", "Single", "Promo"], "role": "Main", "year": 2006},
                    {"title": "Normal Single", "format": ["Vinyl", "7\"", "Single"], "role": "Main", "year": 2006},
                    {"title": "Appearance", "format": ["CD", "Single"], "role": "Appearance", "year": 2006},
                ]

        monkeypatch.setattr("api_clients.discogs_http.DiscogsHttpClient", lambda **kw: FakeHttp())
        monkeypatch.setattr(
            "helpers.config_helpers.get_config",
            lambda: {"api_integrations": {"discogs": {"token": "test-token"}}},
        )

        rows = rcs._fetch_discogs_releases("Some Artist", "1234")
        by_title = {r["title"]: r for r in rows}
        assert by_title["Promo Track"]["is_promo"] is True
        assert by_title["Promo Track"]["release_type"] == "single"
        assert by_title["Normal Single"]["is_promo"] is False
        assert "Appearance" not in by_title


class TestPromoFullDetection:
    """End-to-end: a promo-only Discogs match never reaches high confidence."""

    def _run(self, monkeypatch, discogs_promo):
        from services.enrichment.single_detection_service import detect_single_for_track

        def fake_discogs(*args, **kwargs):
            return {
                "source": "discogs",
                "matched": True,
                "confidence": 0.5 if discogs_promo else 0.8,
                "metadata": {"is_promo": discogs_promo} if discogs_promo else {},
            }

        def fake_mb(*args, **kwargs):
            return {"source": "musicbrainz", "matched": False, "confidence": 0.0, "metadata": {}}

        monkeypatch.setattr(
            "services.enrichment.single_detection_service._detect_discogs", fake_discogs
        )
        monkeypatch.setattr(
            "services.enrichment.single_detection_service._detect_musicbrainz", fake_mb
        )

        return detect_single_for_track(
            title="Lycanthrope",
            artist="+44",
            album="When Your Heart Stops Beating",
            album_track_count=14,
            popularity=97.5,
            album_type="album",
            use_advanced_detection=True,
            persist_result=False,
            discogs_token="test-token",
        )

    def test_promo_discogs_match_is_medium(self, monkeypatch):
        result = self._run(monkeypatch, discogs_promo=True)
        assert result["is_single"] is True
        assert result["confidence"] == "medium"
        assert "discogs_matched" in result["reasons"]

    def test_commercial_discogs_match_is_high(self, monkeypatch):
        result = self._run(monkeypatch, discogs_promo=False)
        assert result["is_single"] is True
        assert result["confidence"] == "high"
