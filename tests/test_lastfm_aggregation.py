"""Tests for featured-artist Last.fm popularity correlation.

Covers the "separate method of correlating all versions of a song" behaviour:
the album version and the single / feat. version of a song are separate
Last.fm tracks, and the low-listen album version must not shadow the
high-listen single.
"""

from __future__ import annotations

import pytest

from services.popularity.popularity_matching import normalize_for_aggregation
from services.popularity.popularity_sources import (
    get_aggregated_lastfm_popularity,
    get_search_aggregated_lastfm_popularity,
)


class FakeLastFmClient:
    """Stubbed LastFmClient exposing the methods the aggregators call."""

    def __init__(self):
        self.search_calls: list[tuple] = []
        self.top_calls: list[tuple] = []

    def search_track(self, artist, title, limit=20):
        self.search_calls.append((artist, title, limit))
        return [
            {"name": "Herzblut", "artist": "D'artagnan", "listeners": 1200, "url": "/herzblut"},
            {"name": "Herzblut (feat. Melissa Bonny)", "artist": "D'artagnan", "listeners": 45000, "url": "/herzblut-feat"},
            {"name": "Herzblut (feat. Melissa Bonny)", "artist": "D'artagnan", "listeners": 45000, "url": "/herzblut-feat"},
            {"name": "Other Song", "artist": "D'artagnan", "listeners": 99999, "url": "/other"},
        ]

    def get_artist_top_tracks(self, artist, limit=200):
        self.top_calls.append((artist, limit))
        return [
            {"name": "Herzblut", "listeners": 1200, "playcount": 5000},
            {"name": "Hey Brother", "listeners": 90000, "playcount": 400000},
        ]

    def get_track_info(self, artist, title, track_mbid=None):
        return {"listeners": 1200, "track_play": 5000}


@pytest.fixture(autouse=True)
def _clear_catalog_cache():
    """The artist catalogue cache is module-level — reset it per test."""
    from services.popularity import popularity_sources as _ps

    _ps._lastfm_artist_catalog_cache.clear()
    yield
    _ps._lastfm_artist_catalog_cache.clear()


def test_normalize_aggregation_correlates_featured_versions():
    """'Herzblut' and 'Herzblut (feat. Melissa Bonny)' collapse to one key."""
    assert normalize_for_aggregation("Herzblut") == "herzblut"
    assert (
        normalize_for_aggregation("Herzblut (feat. Melissa Bonny)") == "herzblut"
    )
    assert normalize_for_aggregation("Other Song") != "herzblut"


def test_normalize_aggregation_correlates_album_and_single_versions():
    """Album-version markers collapse too, but live takes stay separate."""
    assert (
        normalize_for_aggregation("Song (Album Version)")
        == normalize_for_aggregation("Song")
    )
    assert (
        normalize_for_aggregation("Song - Album Version")
        == normalize_for_aggregation("Song")
    )
    assert normalize_for_aggregation("Song (Live)") != normalize_for_aggregation("Song")


def test_normalize_aggregation_correlates_unparenthesized_feat():
    """'Herzblut feat. Melissa Bonny' (no brackets) matches 'Herzblut'."""
    assert (
        normalize_for_aggregation("Herzblut feat. Melissa Bonny") == "herzblut"
    )
    assert normalize_for_aggregation("Herzblut featuring Melissa Bonny") == "herzblut"
    assert (
        normalize_for_aggregation("Herzblut ft. Melissa Bonny")
        == normalize_for_aggregation("Herzblut")
    )


def test_search_aggregation_sums_all_versions():
    """Search correlation sums the album + single versions and de-dupes."""
    lf = FakeLastFmClient()
    res = get_search_aggregated_lastfm_popularity(
        "dArtagnan feat. Melissa Bonny",
        "Herzblut",
        lastfm_client=lf,
    )
    assert res["listeners"] == 46200  # 1200 (album) + 45000 (single)
    assert len(res["matched_tracks"]) == 2


def test_featured_artist_uses_search_not_album_only():
    """A feat. artist must pick up the high-listen single, not the album row."""
    lf = FakeLastFmClient()
    res = get_aggregated_lastfm_popularity(
        "dArtagnan feat. Melissa Bonny",
        "Herzblut",
        lastfm_client=lf,
    )
    assert res["listeners"] == 46200
