"""Regression tests: live/acoustic/remix variants must not inherit the
canonical track's Last.fm popularity.

Reproduces the Electric Callboy / Ad Infinitum scans: "(acoustic)" and
"(instrumental)" versions of a track were scored with the SAME Last.fm
listener count as the studio recording (e.g. "See You in Hell (acoustic)"
showed 25.6k LF, identical to "See You in Hell").  The search aggregation
correlates versions via ``fuzzy_match_score`` (RapidFuzz token_set_ratio,
word-subset insensitive), so "see you in hell acoustic" scores 1.0 against
"see you in hell" and the live cut inherited the canonical track's counts.
"""

from __future__ import annotations

import pytest

from services.popularity.popularity_matching import title_variants_compatible
from services.popularity.popularity_sources import (
    get_aggregated_lastfm_popularity,
    get_search_aggregated_lastfm_popularity,
)


class FakeLiveVariantLastFmClient:
    """Stub mirroring Last.fm: the canonical studio track is popular; the
    acoustic/instrumental/live versions have their own small audiences."""

    def __init__(self):
        self.search_calls: list[tuple] = []

    def search_track(self, artist, title, limit=20):
        self.search_calls.append((artist, title, limit))
        # The search returns every published version; the aggregator must
        # only keep the versions whose variant markers match the local title.
        return [
            {"name": "See You in Hell", "artist": artist, "listeners": 25600, "url": "/see-you-in-hell"},
            {"name": "See You in Hell (acoustic)", "artist": artist, "listeners": 600, "url": "/see-you-in-hell-acoustic"},
            {"name": "See You in Hell (instrumental)", "artist": artist, "listeners": 300, "url": "/see-you-in-hell-instrumental"},
            {"name": "See You in Hell (Live)", "artist": artist, "listeners": 1200, "url": "/see-you-in-hell-live"},
        ]

    def get_artist_top_tracks(self, artist, limit=200):
        return []

    def get_track_info(self, artist, title, track_mbid=None):
        return {"listeners": 0, "track_play": 0}


@pytest.fixture(autouse=True)
def _clear_caches():
    from services.popularity import popularity_cache_service as _pcs
    from services.popularity import popularity_sources as _ps

    _ps._lastfm_artist_catalog_cache.clear()
    _pcs._lf_top_tracks_cache.clear()
    _pcs._lf_top_tracks_titles.clear()
    _pcs._lf_top_tracks_tags.clear()
    yield
    _ps._lastfm_artist_catalog_cache.clear()
    _pcs._lf_top_tracks_cache.clear()
    _pcs._lf_top_tracks_titles.clear()
    _pcs._lf_top_tracks_tags.clear()


class TestTitleVariantsCompatible:
    def test_plain_vs_acoustic_incompatible(self):
        assert title_variants_compatible("See You in Hell", "See You in Hell (acoustic)") is False

    def test_plain_vs_live_incompatible(self):
        assert title_variants_compatible("See You in Hell", "See You in Hell (Live)") is False

    def test_plain_vs_remix_incompatible(self):
        assert title_variants_compatible("Hypa Hypa", "Hypa Hypa (Gestört aber Geil remix)") is False

    def test_plain_vs_instrumental_incompatible(self):
        assert title_variants_compatible("Upside Down", "Upside Down (instrumental)") is False

    def test_same_variant_compatible(self):
        assert title_variants_compatible("See You in Hell (acoustic)", "See You in Hell (acoustic)") is True

    def test_plain_vs_feat_compatible(self):
        # feat. splits are the SAME performance — still merged.
        assert title_variants_compatible("Herzblut", "Herzblut (feat. Melissa Bonny)") is True

    def test_plain_vs_radio_edit_compatible(self):
        # Soft markers (radio/edit/version) may be absent from either side.
        assert title_variants_compatible("Herzblut", "Herzblut (Radio Edit)") is True


class TestSearchAggregationSeparatesVariants:
    def test_acoustic_title_gets_only_acoustic_counts(self):
        lf = FakeLiveVariantLastFmClient()
        res = get_search_aggregated_lastfm_popularity(
            "Ad Infinitum", "See You in Hell (acoustic)", lastfm_client=lf,
        )
        assert res["listeners"] == 600
        assert len(res["matched_tracks"]) == 1

    def test_plain_title_gets_only_canonical_counts(self):
        lf = FakeLiveVariantLastFmClient()
        res = get_search_aggregated_lastfm_popularity(
            "Ad Infinitum", "See You in Hell", lastfm_client=lf,
        )
        assert res["listeners"] == 25600
        assert len(res["matched_tracks"]) == 1


class TestAggregatedPopularitySeparatesVariants:
    def test_acoustic_variant_not_boosted_by_canonical_search(self):
        lf = FakeLiveVariantLastFmClient()
        res = get_aggregated_lastfm_popularity(
            "Ad Infinitum", "See You in Hell (acoustic)", lastfm_client=lf,
        )
        # The fallback search must not sum the canonical 25.6k into the
        # acoustic track's count.
        assert res["listeners"] == 600

    def test_canonical_not_inflated_by_acoustic(self):
        lf = FakeLiveVariantLastFmClient()
        res = get_aggregated_lastfm_popularity(
            "Ad Infinitum", "See You in Hell", lastfm_client=lf,
        )
        assert res["listeners"] == 25600
