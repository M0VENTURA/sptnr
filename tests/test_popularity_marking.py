"""Tests for the artist top-10% popularity marking + medium→high single bump.

Spec rules 2-3: tracks in the top 10% of the artist's catalogue by popularity
are marked ``popularity_marked``, and a medium-confidence single that is marked
is upgraded to HIGH confidence (which then earns 5★).
"""

from __future__ import annotations

import json


class TestArtistTopMarkedCutoff:
    def _cutoff(self, scan_scores, db_scores=()):
        from services.popularity.scan_stage_runner import _artist_top_marked_cutoff
        return _artist_top_marked_cutoff(scan_scores, db_scores)

    def test_top_ten_percent_cutoff(self):
        # 20 tracks → top 10% = 2 tracks → cutoff = 2nd-highest score.
        scores = list(range(81, 101))  # 81..100
        cutoff, top_n = self._cutoff(scores)
        assert top_n == 2
        assert cutoff == 99.0

    def test_small_catalogue_rounds_up(self):
        # 5 tracks → ceil(5 * 0.10) = 1 track marked.
        cutoff, top_n = self._cutoff([10, 20, 30, 40, 50])
        assert top_n == 1
        assert cutoff == 50.0

    def test_no_scores_returns_none(self):
        cutoff, top_n = self._cutoff([], [])
        assert cutoff is None
        assert top_n == 0

    def test_merges_scan_and_db_scores(self):
        cutoff, top_n = self._cutoff([90], [91, 92, 93])
        assert top_n == 1
        assert cutoff == 93.0

    def test_zero_scores_are_ignored(self):
        cutoff, top_n = self._cutoff([90, 0, 0], [])
        assert top_n == 1
        assert cutoff == 90.0


class TestPopularityMarkingBump:
    def _bump(self, tracks):
        from services.popularity.scan_stage_runner import _apply_popularity_marking_bump
        return _apply_popularity_marking_bump(tracks)

    def _track(self, marked, is_single=True, confidence="medium", sources=""):
        return {
            "track_id": "t1", "title": "Song",
            "popularity_marked": marked,
            "is_single": is_single,
            "single_confidence": confidence,
            "single_sources": sources,
        }

    def test_marked_medium_single_upgraded_to_high(self):
        [t] = self._bump([self._track(marked=True)])
        assert t["single_confidence"] == "high"
        parsed = json.loads(t["single_sources"])
        assert any(s.get("source") == "popularity_marked" and s.get("matched") for s in parsed)

    def test_unmarked_medium_single_unchanged(self):
        [t] = self._bump([self._track(marked=False)])
        assert t["single_confidence"] == "medium"

    def test_marked_low_single_not_upgraded(self):
        # The bump only upgrades MEDIUM → HIGH (spec rule 3); a low/none
        # verdict needs real metadata evidence, popularity alone is not enough.
        [t] = self._bump([self._track(marked=True, confidence="low", is_single=False)])
        assert t["single_confidence"] == "low"

    def test_marked_high_single_kept_high(self):
        [t] = self._bump([self._track(marked=True, confidence="high")])
        assert t["single_confidence"] == "high"

    def test_appends_source_without_duplicating(self):
        sources = '[{"source": "popularity_marked", "matched": true, "confidence": 0.5}]'
        [t] = self._bump([self._track(marked=True, sources=sources)])
        parsed = json.loads(t["single_sources"])
        assert sum(1 for s in parsed if s.get("source") == "popularity_marked") == 1


class TestAlbumRelativeNormalization:
    def _normalize(self, tracks):
        from services.popularity.scan_stage_runner import _apply_album_relative_normalization
        return _apply_album_relative_normalization(tracks)

    def test_remaps_fresh_scores(self):
        tracks = [
            {"track_id": f"t{i}", "title": f"Song {i}", "_raw_combined": raw,
             "popularity_score": raw, "final_score": raw}
            for i, raw in enumerate([95.0, 90.0, 85.0, 80.0, 75.0, 70.0], start=1)
        ]
        changed = self._normalize(tracks)
        assert changed == len(tracks)
        # The album median track (~82.5) now sits near 50; the top track is
        # above it, and nothing clamps at 100.
        scores = sorted(t["popularity_score"] for t in tracks)
        assert scores[0] < scores[-1]
        assert scores[-1] < 100.0
        assert scores[-1] > 50.0

    def test_no_fresh_scores_skips(self):
        tracks = [{"track_id": "t1", "_raw_combined": 0, "popularity_score": 60.0}]
        assert self._normalize(tracks) == 0

    def test_too_few_fresh_scores_skips(self):
        tracks = [
            {"track_id": "t1", "_raw_combined": 80.0, "popularity_score": 80.0},
            {"track_id": "t2", "_raw_combined": 75.0, "popularity_score": 75.0},
        ]
        assert self._normalize(tracks) == 0

    def test_frozen_tracks_keep_stored_score(self):
        # A cached/frozen track (no _raw_combined) is left untouched while its
        # freshly-scored siblings are re-mapped.
        fresh = [
            {"track_id": f"t{i}", "_raw_combined": raw,
             "popularity_score": raw, "final_score": raw}
            for i, raw in enumerate([95.0, 90.0, 85.0, 80.0, 75.0, 70.0], start=1)
        ]
        frozen = {"track_id": "tf", "_raw_combined": 0, "popularity_score": 60.0, "final_score": 60.0}
        tracks = fresh + [frozen]
        self._normalize(tracks)
        assert frozen["popularity_score"] == 60.0
        assert all(t["popularity_score"] != t["_raw_combined"] for t in fresh)
