"""Tests for the album top-genres fallback.

Requirement: "If tracks don't have 3 genres on them it should use the top
Genres from the album."  A sparse track (few/no tags of its own) inherits
the album's top genres so every track carries a full genre identity.
"""

from __future__ import annotations

import pytest


class TestAlbumTopGenres:
    def _helper(self):
        from services.popularity.stages.track_stage import _album_top_genres
        return _album_top_genres

    def test_empty_album_returns_empty(self, monkeypatch):
        assert self._helper()(None) == []
        assert self._helper()([]) == []

    def test_aggregates_sibling_genre_columns(self, monkeypatch):
        from helpers import config_helpers
        monkeypatch.setattr(config_helpers, "get_config", lambda: {"genres": {"min_weight": 0.0}})

        album_tracks = [
            {
                "title": "Song A",
                "musicbrainz_genres": '["nu metal", "alternative metal"]',
                "lastfm_tags": '["nu-metal"]',
                "discogs_genres": '["NuMetal"]',
            },
            {
                "title": "Song B",
                "musicbrainz_genres": '["alternative metal"]',
                "lastfm_tags": '["alternative metal"]',
            },
        ]
        result = self._helper()(album_tracks, max_genres=3)
        # nu metal (mb 0.40 + lf 0.10 + discogs 0.25 = 0.75) ranks above
        # alternative metal (mb 0.40 + mb 0.40 + lf 0.10 = 0.90) — wait:
        # alternative metal appears in TWO mb rows → 0.80 + lf 0.10 = 0.90.
        assert "alternative metal" in result
        assert "nu metal" in result

    def test_sparse_sibling_still_contributes(self, monkeypatch):
        from helpers import config_helpers
        monkeypatch.setattr(config_helpers, "get_config", lambda: {"genres": {"min_weight": 0.0}})

        album_tracks = [
            {"title": "A", "musicbrainz_genres": '["rock"]'},
            {"title": "B", "navidrome_genres": "Metal"},
        ]
        result = self._helper()(album_tracks, max_genres=3)
        assert "rock" in result
        assert "Metal".lower() in {g.lower() for g in result}

    def test_junk_siblings_filtered(self, monkeypatch):
        from helpers import config_helpers
        monkeypatch.setattr(config_helpers, "get_config", lambda: {"genres": {"min_weight": 0.0}})

        album_tracks = [
            {"title": "A", "lastfm_tags": '["2014", "beautiful", "nu metal"]'},
        ]
        result = self._helper()(album_tracks, max_genres=3)
        assert "nu metal" in result
        assert "2014" not in result
        assert "beautiful" not in result

    def test_dict_tag_entries_parsed(self, monkeypatch):
        from helpers import config_helpers
        monkeypatch.setattr(config_helpers, "get_config", lambda: {"genres": {"min_weight": 0.0}})

        album_tracks = [
            {"title": "A", "musicbrainz_genres": '[{"name": "progressive metal", "count": 2}]'},
        ]
        result = self._helper()(album_tracks, max_genres=3)
        assert result == ["progressive metal"]
