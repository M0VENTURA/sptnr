"""Tests for the genre consensus aggregation model.

Covers the three genre fixes:
1. Junk-tag filter — "2014"/"beautiful" (lone Last.fm moods/years) can
   never surface in the ``genres`` column or a ``{Genre} - Top Tracks``
   playlist name.
2. Split-vote stacking — "nu-metal"/"nu metal"/"NuMetal" merge onto one
   vote key so weights accumulate instead of being split.
3. Consensus threshold (``genres.min_weight``) — a lone low-weight tag is
   discarded unless a second source backs it.

The ``genres.min_weight`` default (0.30) and ``genres.junk_filter`` default
(True) are monkeypatched where a test needs a specific value.
"""

from __future__ import annotations

import pytest

from services.enrichment import genre_aggregation_service as gas


@pytest.fixture(autouse=True)
def _clean_module_state():
    yield


@pytest.fixture
def cfg(monkeypatch):
    """Set the genre config (min_weight / junk_filter / weights)."""
    from helpers import config_helpers

    _state = {}

    def _set(**kwargs):
        _state.update(kwargs)

    def _apply(**kwargs):
        _state.update(kwargs)

        cfg = {"genres": {}}
        if "min_weight" in _state:
            cfg["genres"]["min_weight"] = _state["min_weight"]
        if "junk_filter" in _state:
            cfg["genres"]["junk_filter"] = _state["junk_filter"]
        if "weights" in _state:
            cfg["genres"]["weights"] = _state["weights"]
        monkeypatch.setattr(config_helpers, "get_config", lambda: cfg)

    _apply()
    return _set


# ---------------------------------------------------------------------------
# Junk-tag filter
# ---------------------------------------------------------------------------

class TestJunkGenreFilter:
    def test_years_are_junk(self):
        assert gas.is_junk_genre("2014")
        assert gas.is_junk_genre("2015")
        assert gas.is_junk_genre("1980s")

    def test_moods_are_junk(self):
        assert gas.is_junk_genre("beautiful")
        assert gas.is_junk_genre("romantic")
        assert gas.is_junk_genre("seen live")
        assert gas.is_junk_genre("my playlist")

    def test_real_genres_survive(self):
        assert not gas.is_junk_genre("nu metal")
        assert not gas.is_junk_genre("alternative rock")
        assert not gas.is_junk_genre("post-hardcore")
        assert not gas.is_junk_genre("k-pop")

    def test_junk_filter_config_can_disable(self, cfg):
        cfg(junk_filter=False)
        assert not gas.is_junk_genre("beautiful")

    def test_junk_filter_on_by_default(self, cfg):
        assert gas.is_junk_genre("2014")


# ---------------------------------------------------------------------------
# Split-vote stacking
# ---------------------------------------------------------------------------

class TestSplitVoteStacking:
    def test_hyphen_variants_stack(self, cfg):
        cfg(min_weight=0.0)
        # "nu-metal" (Last.fm 0.10) + "nu metal" (Essentia 0.20) +
        # "NuMetal" (Discogs 0.25) — all merge onto one key, 0.55 total.
        result = gas.aggregate_genres({
            "lastfm": ["nu-metal"],
            "essentia": ["nu metal"],
            "discogs": ["NuMetal"],
        }, max_genres=3)
        assert result == ["nu metal"]  # Discogs spelling wins (0.25 > others)

    def test_pop_punk_variants_stack(self, cfg):
        cfg(min_weight=0.0)
        result = gas.aggregate_genres({
            "lastfm": ["pop-punk"],
            "musicbrainz": ["Pop Punk"],
        }, max_genres=3)
        assert result == ["pop punk"]  # MusicBrainz spelling wins (0.40)

    def test_split_vote_clears_threshold_together(self, cfg):
        cfg(min_weight=0.25)
        # Individually: lastfm 0.10 + essentia 0.20 = 0.30 → passes.
        result = gas.aggregate_genres({
            "lastfm": ["nu-metal"],
            "essentia": ["nu metal"],
        }, max_genres=3)
        assert result == ["nu metal"]

    def test_split_vote_without_second_source_fails_threshold(self, cfg):
        cfg(min_weight=0.25)
        # Lone Last.fm tag = 0.10 → below 0.25 → discarded.
        result = gas.aggregate_genres({
            "lastfm": ["nu-metal"],
        }, max_genres=3)
        assert result == []


# ---------------------------------------------------------------------------
# Consensus threshold
# ---------------------------------------------------------------------------

class TestConsensusThreshold:
    def test_lone_lastfm_tag_filtered(self, cfg):
        cfg(min_weight=0.25)
        # "k-pop" tagged by a single Last.fm user = 0.10 → filtered.
        result = gas.aggregate_genres({"lastfm": ["k-pop"]}, max_genres=3)
        assert result == []

    def test_multi_source_confirmation_passes(self, cfg):
        cfg(min_weight=0.25)
        result = gas.aggregate_genres({
            "lastfm": ["k-pop"],
            "musicbrainz": ["k-pop"],
        }, max_genres=3)
        assert result == ["k-pop"]

    def test_discogs_alone_passes(self, cfg):
        cfg(min_weight=0.25)
        # Discogs alone (0.25) equals the default threshold → passes.
        result = gas.aggregate_genres({"discogs": ["alternative metal"]}, max_genres=3)
        assert result == ["alternative metal"]

    def test_zero_disables_gate(self, cfg):
        cfg(min_weight=0.0)
        result = gas.aggregate_genres({"lastfm": ["weird-lone-tag"]}, max_genres=3)
        # Hyphenated spelling is kept as the readable display form.
        assert result == ["weird-lone-tag"]

    def test_junk_blocked_before_vote(self, cfg):
        cfg(min_weight=0.0)
        # Even with the threshold disabled, junk never surfaces.
        result = gas.aggregate_genres({"lastfm": ["2014", "beautiful"]}, max_genres=3)
        assert result == []


# ---------------------------------------------------------------------------
# Junk tag can never reach a playlist name
# ---------------------------------------------------------------------------

class TestNoJunkPlaylistNames:
    def test_years_never_become_playlist_genres(self, cfg):
        cfg(min_weight=0.0)
        result = gas.aggregate_genres({
            "lastfm": ["2014", "2015", "alternative metal"],
        }, max_genres=5)
        assert "2014" not in result
        assert "2015" not in result
        assert "alternative metal" in result
