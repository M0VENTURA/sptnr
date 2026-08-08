"""Tests for the popularity scoring math (soft-ceiling z-score mapping).

Covers the regressions where the scoring collapsed popular tracks together:
- the artist-context median+MAD adjustment mapped every track well above its
  artist's median onto the same 100 ceiling (e.g. 128k vs 156k listeners both
  scored exactly 100.0), losing all discrimination;
- blending a weaker ListenBrainz / age signal into the average dragged a
  genuinely popular track below its strongest single source of evidence
  (a 139k-listener track with LB data scored below a 133k-listener track with
  none; the 179k-listener album lead scored lowest);
- the flat *1.15 single boost saturated every top single (364k vs 128k
  listeners both scored ~95-97).
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


class TestBlendNeverDropsBelowStrongestEvidence:
    """A blend of corroborating sources must not drag a track below its best one.

    5-STAR reproductions: ITEM (139,568 LF / 5,330 LB) scored *below*
    Collision (133,646 LF / no LB), and the album lead TOPLINE (179,819 LF /
    1,814 LB) scored lowest, because the weaker LB count out-voted the strong
    Last.fm footprint in the weighted average.
    """

    def test_lb_evidence_does_not_drag_below_lf_log(self):
        from services.popularity.popularity_math import (
            calculate_combined_popularity_score,
            calculate_lastfm_popularity_score,
        )

        with_lb = calculate_combined_popularity_score(
            lastfm_listeners=139568,
            listenbrainz_listens=5330,
            age_source_value=5330,
            release_date="2023",
        )
        without_lb = calculate_combined_popularity_score(
            lastfm_listeners=133646,
            listenbrainz_listens=0,
        )
        # The strongest absolute evidence (the LF log score) is the floor.
        assert with_lb["combined_score"] >= calculate_lastfm_popularity_score(139568, 0)
        assert with_lb["combined_score"] >= without_lb["combined_score"]

    def test_featured_album_lead_with_undercounted_lb_keeps_lf_score(self):
        from services.popularity.popularity_math import (
            calculate_combined_popularity_score,
            calculate_lastfm_popularity_score,
        )

        d = calculate_combined_popularity_score(
            lastfm_listeners=179819,
            listenbrainz_listens=1814,
            is_featured_track=True,
            age_source_value=1814,
            release_date="2023",
        )
        floor = round(calculate_lastfm_popularity_score(179819, 0), 3)
        assert d["combined_score"] >= floor


class TestSingleBoostFade:
    """The confirmed-single boost tapers near the ceiling instead of saturating."""

    def test_fade_unit_behaviour(self):
        from services.popularity.popularity_math import single_boost_fade

        assert single_boost_fade(50.0) == 1.0
        assert single_boost_fade(95.0) == 0.0
        assert 0.0 < single_boost_fade(70.0) < 1.0

    def test_high_singles_stay_apart_and_below_ceiling(self):
        from services.popularity.popularity_math import calculate_combined_popularity_score

        top = calculate_combined_popularity_score(
            lastfm_listeners=364373, is_single=True, single_boost=1.15
        )
        mid = calculate_combined_popularity_score(
            lastfm_listeners=128085, is_single=True, single_boost=1.15
        )
        assert top["combined_score"] > mid["combined_score"]
        assert top["combined_score"] < 100.0
        assert mid["combined_score"] < 100.0

    def test_boost_adds_more_to_a_mid_single_than_an_already_top_single(self):
        from services.popularity.popularity_math import calculate_combined_popularity_score

        def boosted(lf):
            return calculate_combined_popularity_score(
                lastfm_listeners=lf, is_single=True, single_boost=1.15
            )["combined_score"]

        def unboosted(lf):
            return calculate_combined_popularity_score(
                lastfm_listeners=lf, is_single=False, single_boost=1.15
            )["combined_score"]

        # The fade means the bonus shrinks as the raw score nears the ceiling.
        assert (boosted(128085) - unboosted(128085)) > (boosted(364373) - unboosted(364373))


class TestArtistAdjustmentDampening:
    """The artist median+MAD re-map is damped so it can't re-compress the top."""

    def test_raw_blend_is_between_zero_and_one(self):
        from services.popularity.popularity_adjustments import ARTIST_ADJUSTMENT_RAW_BLEND

        assert 0.0 < ARTIST_ADJUSTMENT_RAW_BLEND < 1.0
