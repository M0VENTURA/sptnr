"""Tests for collab / multi-artist Last.fm catalogue merging.

Last.fm splits scrobbles for a collab track across EACH artist's own
catalogue — "BABYMETAL & Electric Callboy" indexes RATATATA under both
"BABYMETAL" and "Electric Callboy" artist rows.  Querying only the primary
artist returns a fraction of the real popularity.  These tests cover the
collab split that queries every sub-artist's catalogue and merges counts.
"""

from __future__ import annotations

import pytest

from services.popularity.popularity_matching import get_artist_lookup_candidates
from services.popularity.popularity_sources import (
    get_aggregated_lastfm_popularity,
)


class FakeCollabLastFmClient:
    """Stub whose artist catalogues are keyed by exact artist string.

    The full "A & B" credit returns an EMPTY catalogue (Last.fm does not
    index the combined name), while each sub-artist returns the same track
    with split counts — mirroring the RATATATA case.
    """

    def __init__(self, catalogues: dict[str, list[dict]]):
        self.catalogues = catalogues
        self.top_calls: list[tuple[str, int]] = []
        self.search_calls: list[tuple[str, str, int]] = []

    def get_artist_top_tracks(self, artist, limit=200):
        self.top_calls.append((artist, limit))
        return list(self.catalogues.get(artist, []))

    def search_track(self, artist, title, limit=20):
        self.search_calls.append((artist, title, limit))
        return []

    def get_track_info(self, artist, title, track_mbid=None):
        return {"listeners": 0, "track_play": 0}


@pytest.fixture(autouse=True)
def _clear_caches():
    """Both source-level and map-level caches are module-global — reset them."""
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


def test_collab_credit_merges_both_artist_catalogues():
    """'BABYMETAL & Electric Callboy' sums counts from both artist catalogues."""
    lf = FakeCollabLastFmClient(
        {
            "BABYMETAL": [
                {"name": "RATATATA", "listeners": 90000, "playcount": 500000},
                {"name": "Monochrome", "listeners": 6000, "playcount": 30000},
            ],
            "Electric Callboy": [
                {"name": "RATATATA", "listeners": 1100, "playcount": 5000},
                {"name": "Hypa Hypa", "listeners": 40000, "playcount": 250000},
            ],
        }
    )
    res = get_aggregated_lastfm_popularity(
        "BABYMETAL & Electric Callboy",
        "RATATATA",
        lastfm_client=lf,
    )
    assert res["listeners"] == 90000 + 1100
    assert res["track_play"] == 500000 + 5000
    # Both sub-artist catalogues must have been queried.
    assert any(a == "BABYMETAL" for a, _ in lf.top_calls)
    assert any(a == "Electric Callboy" for a, _ in lf.top_calls)
    assert len(res["matched_tracks"]) == 2


def test_collab_split_supports_x_and_and_separators():
    """'x' and 'and' joins are split the same way as '&'."""
    lf = FakeCollabLastFmClient(
        {
            "Run": [
                {"name": "Hard To Love", "listeners": 20000, "playcount": 100000},
            ],
            "Redlight King": [
                {"name": "Hard To Love", "listeners": 500, "playcount": 2000},
            ],
        }
    )
    res = get_aggregated_lastfm_popularity(
        "Run x Redlight King",
        "Hard To Love",
        lastfm_client=lf,
    )
    assert res["listeners"] == 20500


def test_collab_split_not_fired_when_primary_catalogue_matches():
    """If the full credit resolves, sub-artist counts must NOT be double-added."""
    lf = FakeCollabLastFmClient(
        {
            "Run x Redlight King": [
                {"name": "Hard To Love", "listeners": 25000, "playcount": 120000},
            ],
            "Run": [
                {"name": "Hard To Love", "listeners": 20000, "playcount": 100000},
            ],
            "Redlight King": [
                {"name": "Hard To Love", "listeners": 500, "playcount": 2000},
            ],
        }
    )
    res = get_aggregated_lastfm_popularity(
        "Run x Redlight King",
        "Hard To Love",
        lastfm_client=lf,
    )
    assert res["listeners"] == 25000
    assert len(res["matched_tracks"]) == 1


def test_artist_lookup_candidates_include_collab_parts():
    """Provider lookup candidates expose each sub-artist for direct queries."""
    cands = get_artist_lookup_candidates("BABYMETAL & Electric Callboy")
    assert "BABYMETAL & Electric Callboy" in cands
    assert "BABYMETAL" in cands
    assert "Electric Callboy" in cands
