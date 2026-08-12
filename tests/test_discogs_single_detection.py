"""Regression tests: Discogs single confirmation for title tracks.

Reproduces the +44 "When Your Heart Stops Beating" miss. The track IS a
single on Discogs (7"/limited single), but the Discogs source reported
``matched: False``:

- ``DiscogsService.is_single`` relied on a free-text database search with
  ``per_page=5``. Discogs ranks the full-length album editions above the
  7"/promo single, so the single routinely missed the top-5 window even
  though it is genuinely on the artist's own release list.
- With Discogs unmatched, the title track only reached ``medium`` via the
  title-track boost (MusicBrainz is a medium source), so it was never shown
  as a confirmed single (5-star "Detected Singles").

Fix: ``DiscogsService.is_single`` now first matches against the artist's OWN
release list (``/artists/{id}/releases``, authoritative for single/EP
format classification) before falling back to the database search.
"""

from __future__ import annotations

import pytest


class FakeDiscogsHttp:
    """Simulates Discogs API responses for the +44 catalogue.

    The free-text database search returns only album editions (the 7" single
    is ranked below the top-5 window), while the artist-releases endpoint
    lists every release — including the single.
    """

    def __init__(self, token="", **kw):
        self.token = token
        self.search_calls = 0

    def search_database(self, params, timeout=10.0):
        self.search_calls += 1
        return [
            {"title": "When Your Heart Stops Beating", "format": ["CD", "Album"], "artist": "+44"},
            {"title": "When Your Heart Stops Beating", "format": ["Vinyl", "LP", "Album"], "artist": "+44"},
            {"title": "When Your Heart Stops Beating", "format": ["CD", "Album"], "artist": "+44"},
            {"title": "When Your Heart Stops Beating", "format": ["CD", "Album"], "artist": "+44"},
            {"title": "Lycanthrope", "format": ["CD", "Single"], "artist": "+44"},
        ]

    def get_artist_releases(self, artist_id, per_page=100, timeout=10.0):
        return [
            {"title": "When Your Heart Stops Beating", "format": ["CD", "Album"], "role": "Main", "year": 2006},
            {"title": "When Your Heart Stops Beating", "format": ["Vinyl", "7\"", "Single", "Limited Edition"], "role": "Main", "year": 2006},
            {"title": "Lycanthrope", "format": ["CD", "Single", "Promo"], "role": "Main", "year": 2006},
        ]

    def get_artist_releases_all(self, artist_id, max_pages=10):
        return self.get_artist_releases(artist_id)


class TestDiscogsServiceIsSingle:
    def test_single_found_via_artist_release_list(self):
        from services.enrichment.discogs_service import DiscogsService

        http = FakeDiscogsHttp(token="test-token")
        svc = DiscogsService(token="test-token", http_client=http)
        svc.get_artist_id = lambda artist, timeout=10.0: "640496"

        assert svc.is_single("When Your Heart Stops Beating", "+44") is True
        # The authoritative artist-release list matched — the fragile top-5
        # free-text search should not even be needed.
        assert http.search_calls == 0

    def test_release_title_punctuation_normalized(self):
        # "What's The Deal?" (track) vs "What's The Deal?" (single release)
        # must match after punctuation stripping.
        from services.enrichment.discogs_service import DiscogsService

        class Http(FakeDiscogsHttp):
            def search_database(self, params, timeout=10.0):
                self.search_calls += 1
                return []

            def get_artist_releases(self, artist_id, per_page=100, timeout=10.0):
                return [
                    {"title": "What's The Deal?", "format": ["Vinyl", "7\"", "Single"], "role": "Main"},
                ]

        http = Http(token="test-token")
        svc = DiscogsService(token="test-token", http_client=http)
        svc.get_artist_id = lambda artist, timeout=10.0: "640496"

        assert svc.is_single("What's The Deal?", "Some Artist") is True

    def test_non_main_role_not_confirmed(self):
        from services.enrichment.discogs_service import DiscogsService

        class Http(FakeDiscogsHttp):
            def get_artist_releases(self, artist_id, per_page=100, timeout=10.0):
                return [
                    {"title": "When Your Heart Stops Beating", "format": ["Vinyl", "7\"", "Single"], "role": "Appearance"},
                ]

        http = Http(token="test-token")
        svc = DiscogsService(token="test-token", http_client=http)
        svc.get_artist_id = lambda artist, timeout=10.0: "640496"

        assert svc.is_single("When Your Heart Stops Beating", "+44") is False

    def test_no_single_anywhere_is_false(self):
        from services.enrichment.discogs_service import DiscogsService

        class Http(FakeDiscogsHttp):
            def get_artist_releases(self, artist_id, per_page=100, timeout=10.0):
                return [
                    {"title": "Some Album", "format": ["CD", "Album"], "role": "Main"},
                ]

        http = Http(token="test-token")
        svc = DiscogsService(token="test-token", http_client=http)
        svc.get_artist_id = lambda artist, timeout=10.0: "640496"

        assert svc.is_single("Random Deep Cut", "+44") is False


class TestDetectDiscogsSource:
    """The single-detection Discogs source honours the service match."""

    def test_detect_discogs_matches_via_service(self, monkeypatch):
        from services.enrichment import discogs_service as ds_module
        from services.enrichment.single_detection_service import _detect_discogs

        class FakeService:
            def __init__(self, *args, **kwargs):
                pass

            def is_single(self, title, artist, album_context=None):
                return True

            def get_single_release_year(self, title, artist):
                return 2006

        monkeypatch.setattr(ds_module, "DiscogsService", FakeService)

        result = _detect_discogs(
            "When Your Heart Stops Beating", "+44",
            "When Your Heart Stops Beating",
            "test-token",
        )
        assert result["matched"] is True
        # Exact verified match = base weight (0.85) × ratio 1.0 → full (high)
        # confidence. Discogs only confirms a single on high-confidence matches.
        assert result["confidence"] == 0.85
        assert result["metadata"].get("release_year") == 2006

    def test_detect_discogs_fast_path_cached_single(self):
        from services.enrichment.single_detection_service import _detect_discogs

        # Fast path: the artist release cache already knows the single title.
        result = _detect_discogs(
            "When Your Heart Stops Beating", "+44",
            "When Your Heart Stops Beating",
            "test-token",
            cached_single_titles={"when your heart stops beating"},
        )
        assert result["matched"] is True
        assert result.get("cached") is True


class TestPlus44TitleTrackFullDetection:
    """End-to-end: +44 title track is confirmed as a high-confidence single."""

    def _run(self, monkeypatch, discogs_matched):
        from services.enrichment.single_detection_service import detect_single_for_track

        def fake_discogs(*args, **kwargs):
            return {
                "source": "discogs",
                "matched": discogs_matched,
                "confidence": 0.8 if discogs_matched else 0.0,
                "metadata": {"release_year": 2006} if discogs_matched else {},
            }

        def fake_mb(*args, **kwargs):
            return {"source": "musicbrainz", "matched": True, "confidence": 0.9, "metadata": {}}

        monkeypatch.setattr(
            "services.enrichment.single_detection_service._detect_discogs", fake_discogs
        )
        monkeypatch.setattr(
            "services.enrichment.single_detection_service._detect_musicbrainz", fake_mb
        )

        return detect_single_for_track(
            title="When Your Heart Stops Beating",
            artist="+44",
            album="When Your Heart Stops Beating",
            album_track_count=14,
            popularity=68.1,
            album_type="album",
            use_advanced_detection=True,
            persist_result=False,
            discogs_token="test-token",
            listenbrainz_listens=6,
        )

    def test_discogs_match_confirms_title_track_high(self, monkeypatch):
        # Discogs IS the authority here: with it matching, the title track is
        # a confirmed single even though its blended score sits below the
        # album median (LB near-zero drags it down).
        result = self._run(monkeypatch, discogs_matched=True)
        assert result["is_single"] is True
        assert result["confidence"] == "high"
        assert "discogs_matched" in result["reasons"]

    def test_without_discogs_only_medium(self, monkeypatch):
        # Regression guard: with Discogs STILL failing (the old behaviour), a
        # lone MusicBrainz match keeps the title track at medium — never high.
        result = self._run(monkeypatch, discogs_matched=False)
        assert result["is_single"] is True
        assert result["confidence"] == "medium"
