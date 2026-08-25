"""Regression: the track-level genre aggregation must include
``navidrome_genres`` as a low-authority fallback source.

Previously the aggregation block only built ``source_map`` from the external
sources (MB/Discogs/Last.fm/LB/Spotify).  A track whose external columns were
empty (no MBID → no MB genres, no Discogs token, etc.) produced an empty
source_map, so ``genres`` was never updated during a metadata scan — the
album page then showed every genre attributed to "Navidrome" because only the
import-time ``navidrome_genres`` column had data.  Including navidrome in the
source map means the aggregated ``genres`` is always recomputed (navidrome
weight 0.30), and a real external genre outranks it (MB 0.40 / Discogs 0.25).
"""

from __future__ import annotations


class TestTrackGenreAggregationIncludesNavidrome:
    def test_navidrome_only_track_gets_genres(self, monkeypatch):
        """A track with ONLY navidrome_genres (no external source data) must
        still produce an aggregated genre set (not lose its genre field)."""
        import helpers.config_helpers as ch
        monkeypatch.setattr(ch, "get_config", lambda: {"genres": {"min_weight": 0.0}})

        from services.enrichment.genre_aggregation_service import aggregate_genres

        result = aggregate_genres(
            {"navidrome": ["Electronic", "Industrial", "Rock"]},
            max_genres=3,
        )
        assert "Electronic" in result or "electronic" in result
        assert len(result) >= 1

    def test_external_source_participates_with_navidrome(self, monkeypatch):
        """An external MB genre must participate in the same vote as the
        Navidrome tags (it is not discarded)."""
        import helpers.config_helpers as ch
        monkeypatch.setattr(ch, "get_config", lambda: {"genres": {"min_weight": 0.0}})

        from services.enrichment.genre_aggregation_service import aggregate_genres

        result = aggregate_genres(
            {
                "musicbrainz": ["nu metal"],
                "navidrome": ["Electronic", "Industrial"],
            },
            max_genres=5,
        )
        # The MB genre is present in the output (weight 0.40).
        assert any(g.lower() == "nu metal" for g in result)
        # Navidrome tags also present.
        assert any(g.lower() in ("electronic", "industrial") for g in result)

    def test_track_stage_builds_source_map_with_navidrome(self, monkeypatch):
        """The track_stage aggregation loop must treat navidrome_genres as a
        source (the exact regression)."""
        import inspect
        from services.popularity.stages import track_stage as ts

        # Find the source-key tuple list in the aggregation block.
        src = inspect.getsource(ts)
        # The aggregation loop must include ("navidrome_genres", "navidrome").
        assert '"navidrome_genres", "navidrome"' in src
