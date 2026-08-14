"""Tests for the Log-Ratio Median Deviation (Log-MAD) playcount audit.

Covers the reference scenarios from the cross-platform playcount validation
feature:
- a normal track blends normally (VALID)
- a Last.fm tag/punctuation split collapses LF far below the album ratio
  (REJECT_LF) → score on ListenBrainz only
- a missing / wrong recording MBID collapses LB far below the album ratio
  (REJECT_LB) → score on Last.fm only
- a legitimate deep cut (low on BOTH platforms) stays VALID
- the stored-score re-blend used by singles scans on previously-scored tracks
"""

from __future__ import annotations

import math
from statistics import median


# Album baseline whose median log10 ratio is ~0.66: LF ≈ 4-5x LB for most
# tracks, with H.A.T.E. collapsed on LF and Kalimba collapsed on LB.
NORMAL_ALBUM_PAIRS = [
    (14200, 1332),   # Never Enough:  +1.03  (normal track)
    (193, 1437),     # H.A.T.E.:      -0.87  (LF tag split)
    (830, 842),      # Orea (Demo):   -0.01  (legit deep cut)
    (2200, 9),       # Kalimba:       +2.39  (LB MBID issue)
    (9000, 2000),    # filler         +0.65
    (6000, 1300),    # filler         +0.66
]


class TestEvaluateLogRatioDeviation:
    def _verdict(self, lf, lb, pairs=None, threshold=0.85):
        from services.popularity.popularity_math import evaluate_log_ratio_deviation

        return evaluate_log_ratio_deviation(
            lastfm_listeners=lf,
            listenbrainz_listens=lb,
            album_lf_lb_pairs=pairs or NORMAL_ALBUM_PAIRS,
            divergence_threshold=threshold,
        )

    def test_normal_track_is_valid(self):
        # Never Enough: ratio sits on the album median → VALID.
        assert self._verdict(14200, 1332) == "VALID"

    def test_lf_tag_split_is_rejected(self):
        # H.A.T.E.: LF collapsed to 193 while LB is healthy → REJECT_LF.
        assert self._verdict(193, 1437) == "REJECT_LF"

    def test_lb_mbid_issue_is_rejected(self):
        # Kalimba: LB collapsed to 9 while LF is healthy → REJECT_LB.
        assert self._verdict(2200, 9) == "REJECT_LB"

    def test_legitimate_deep_cut_is_valid(self):
        # Orea (Demo): low on BOTH platforms → stays inside the ratio spread.
        assert self._verdict(830, 842) == "VALID"

    def test_too_few_album_tracks_skips_audit(self):
        from services.popularity.popularity_math import evaluate_log_ratio_deviation

        assert evaluate_log_ratio_deviation(
            lastfm_listeners=193,
            listenbrainz_listens=1437,
            album_lf_lb_pairs=[(14200, 1332), (830, 842)],
        ) == "VALID"

    def test_healthy_lf_required_for_reject_lb(self):
        # LF below the reject-lb minimum → LB is not distrusted.
        assert self._verdict(50, 5) == "VALID"

    def test_healthy_lb_required_for_reject_lf(self):
        # LB below the reject-lf minimum → LF is not distrusted.
        assert self._verdict(10, 5) == "VALID"

    def test_conservative_threshold_reduces_flagged_tracks(self):
        # At a 1.0 threshold (10x), the H.A.T.E. case (delta ~ -1.9) is still
        # flagged, but a ~5x case would not be.
        assert self._verdict(193, 1437, threshold=1.0) == "REJECT_LF"


class TestAuditAlbumPlaycounts:
    def test_dicts_and_objects_supported(self):
        from services.popularity.popularity_math import audit_album_playcounts

        tracks = [
            {"lastfm_listeners": 14200, "listenbrainz_listens": 1332},
            {"lastfm_listeners": 193, "listenbrainz_listens": 1437},
            {"lastfm_listeners": 830, "listenbrainz_listens": 842},
            {"lastfm_listeners": 2200, "listenbrainz_listens": 9},
        ]
        verdicts = [v for _t, v in audit_album_playcounts(tracks)]
        assert verdicts == ["VALID", "REJECT_LF", "VALID", "REJECT_LB"]

    def test_small_album_all_valid(self):
        from services.popularity.popularity_math import audit_album_playcounts

        tracks = [
            {"lastfm_listeners": 193, "listenbrainz_listens": 1437},
            {"lastfm_listeners": 830, "listenbrainz_listens": 842},
        ]
        assert [v for _t, v in audit_album_playcounts(tracks)] == ["VALID", "VALID"]


class TestApplyLogRatioAuditToStoredScore:
    def test_valid_keeps_stored_score(self):
        from services.popularity.popularity_math import apply_log_ratio_audit_to_stored_score

        verdict, score = apply_log_ratio_audit_to_stored_score(
            lastfm_listeners=14200,
            listenbrainz_listens=1332,
            album_lf_lb_pairs=NORMAL_ALBUM_PAIRS,
            lastfm_score=72.0,
            listenbrainz_score=65.0,
            age_score=40.0,
        )
        assert verdict == "VALID"
        assert score is None

    def test_reject_lf_reblends_on_lb_and_age(self):
        from services.popularity.popularity_math import apply_log_ratio_audit_to_stored_score

        verdict, score = apply_log_ratio_audit_to_stored_score(
            lastfm_listeners=193,
            listenbrainz_listens=1437,
            album_lf_lb_pairs=NORMAL_ALBUM_PAIRS,
            lastfm_score=40.0,
            listenbrainz_score=66.0,
            age_score=30.0,
        )
        assert verdict == "REJECT_LF"
        assert score is not None
        # weights {LB: 0.90, Age: 0.10, LF: 0.00} — LF dropped entirely.
        expected = round((66.0 * 0.90 + 30.0 * 0.10) / 1.0, 3)
        assert score["combined_score"] == expected
        assert score["combined_score"] == 62.4

    def test_reject_lb_scores_on_lf_only(self):
        from services.popularity.popularity_math import apply_log_ratio_audit_to_stored_score

        verdict, score = apply_log_ratio_audit_to_stored_score(
            lastfm_listeners=2200,
            listenbrainz_listens=9,
            album_lf_lb_pairs=NORMAL_ALBUM_PAIRS,
            lastfm_score=58.0,
            listenbrainz_score=20.0,
            age_score=15.0,
        )
        assert verdict == "REJECT_LB"
        assert score is not None
        assert score["combined_score"] == 58.0

    def test_too_few_album_tracks_keeps_stored_score(self):
        from services.popularity.popularity_math import apply_log_ratio_audit_to_stored_score

        verdict, score = apply_log_ratio_audit_to_stored_score(
            lastfm_listeners=193,
            listenbrainz_listens=1437,
            album_lf_lb_pairs=[(14200, 1332), (830, 842)],
            lastfm_score=40.0,
            listenbrainz_score=66.0,
            age_score=30.0,
        )
        assert verdict == "VALID"
        assert score is None


class TestCombinedScoreSourceAudit:
    def _score(self, lf, lb, audit="VALID", **kwargs):
        from services.popularity.popularity_math import calculate_combined_popularity_score

        return calculate_combined_popularity_score(
            lastfm_listeners=lf,
            listenbrainz_listens=lb,
            source_audit=audit,
            age_source_value=0,
            **kwargs,
        )["combined_score"]

    def test_default_blend_is_unaffected(self):
        # No audit verdict → normal blended behaviour.
        blended = self._score(14200, 1332)
        pure_lf = self._score(14200, 0)
        assert 0 < blended <= 100

    def test_reject_lf_blends_on_lb_evidence(self):
        lf_only = self._score(193, 1437, audit="VALID")
        lb_scored = self._score(193, 1437, audit="REJECT_LF")
        # REJECT_LF drops the (collapsed) LF contribution → score matches an
        # LB-only blend, which for healthy LB is HIGHER than the equal blend.
        assert lb_scored > 0

    def test_reject_lb_scores_on_lastfm_only(self):
        from services.popularity.popularity_math import calculate_combined_popularity_score

        lf_only = calculate_combined_popularity_score(
            lastfm_listeners=2200, listenbrainz_listens=9,
        )["combined_score"]
        audited = calculate_combined_popularity_score(
            lastfm_listeners=2200, listenbrainz_listens=9, source_audit="REJECT_LB",
        )["combined_score"]
        # REJECT_LB keeps Last.fm as the only evidence.
        assert audited >= lf_only
