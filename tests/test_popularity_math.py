"""Tests for the popularity scoring math (soft-ceiling z-score mapping).

Covers the regression where the artist-context median+MAD adjustment
collapsed every track well above its artist's median onto the same 100
ceiling (e.g. 128k vs 156k listeners both scored exactly 100.0), losing
all discrimination between genuinely different popularities.
"""

from __future__ import annotations

import math

from services.popularity.popularity_math import zscore_to_popularity


class TestZScoreToPopularitySoftCeiling:
    """The z-score→popularity mapping must keep discriminating at the top."""

    def test_midpoint_preserved(self):
        assert zscore_to_popularity(0.0) == 50.0

    def test_low_negative_scores_floor(self):
        assert zscore_to_popularity(-10.0) == 0.0

    def test_high_positive_scores_do_not_saturate_at_100(self):
        # The legacy linear map returned 100.0 for any z >= 3. Distinct high
        # popularities must keep distinct scores instead of hitting the ceiling.
        assert zscore_to_popularity(3.0) < 100.0
        assert zscore_to_popularity(4.0) < 100.0

    def test_top_of_scale_is_monotonic_and_asymptotic(self):
        assert zscore_to_popularity(3.0) < zscore_to_popularity(4.0)
        assert zscore_to_popularity(4.0) < zscore_to_popularity(6.0)
        assert zscore_to_popularity(6.0) < zscore_to_popularity(8.0)
        assert zscore_to_popularity(20.0) > 99.0

    def test_tracks_with_different_popularity_stay_apart(self):
        # Stray Kids - 5-STAR reproduction: raw combined scores 83.13 and 81.72
        # (156,916 vs 128,085 listeners) both mapped to exactly 100.0 under the
        # old hard clamp. With the soft ceiling the two remain ordered.
        high = zscore_to_popularity(3.31)
        mid = zscore_to_popularity(3.17)
        assert high > mid
        assert high < 100.0
        assert mid < 100.0


class TestLastfmLogScaleDiscrimination:
    """The raw log-scale scores already discriminate mid-popularity tracks."""

    def test_log_scale_does_not_saturate_at_100(self):
        from services.popularity.popularity_math import calculate_lastfm_popularity_score

        assert calculate_lastfm_popularity_score(156916) < 100.0
        assert calculate_lastfm_popularity_score(128085) < 100.0
        assert calculate_lastfm_popularity_score(156916) > calculate_lastfm_popularity_score(128085)
