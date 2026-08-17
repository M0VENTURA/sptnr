"""Tests for the short-interlude ListenBrainz outlier filter.

The filter catches the "DLB" class of anomaly: a short ambient interlude
that carries an LB count far above the album's typical LB/LF relationship
(e.g. 20.6k LB listens on a 45.7k-LF track — higher than every single on
the record).  Such a count is a recording-MBID artifact, and weighting the
inflated LB at 55% would let it outrank genuinely popular album tracks.
"""

from __future__ import annotations

# Album baseline for Eat-the-Elephant-style material: typical tracks carry
# LF several-to-tens of times their LB count, so the median LB/LF ratio is
# low (~0.1).  The DLB interlude sits at LB/LF = 0.45 — ~4.5x the album norm.
ALBUM_PAIRS = [
    (136000, 14000),  # lead single: LF >> LB
    (95000, 11000),
    (78000, 9000),
    (56000, 6500),
    (45700, 20640),   # DLB — the anomalous interlude being tested
    (61000, 7200),
]


class TestIsInterludeLbOutlier:
    def _check(self, duration, lf, lb, pairs=None):
        from services.popularity.popularity_math import is_interlude_lb_outlier

        return is_interlude_lb_outlier(
            duration_seconds=duration,
            lastfm_listeners=lf,
            listenbrainz_listens=lb,
            album_lf_lb_pairs=pairs or ALBUM_PAIRS,
        )

    def test_short_interlude_with_inflated_lb_is_flagged(self):
        # DLB: 75s interlude, LF=45.7k, LB=20.6k → LB/LF ~0.45 vs album median
        # ~0.11 → >3x → outlier.
        assert self._check(75, 45700, 20640) is True

    def test_long_track_never_flagged(self):
        # Same inflated counts on a 4-minute track are NOT an interlude
        # anomaly (a radio edit / short single can legitimately be popular).
        assert self._check(240, 45700, 20640) is False

    def test_proportional_lb_on_short_track_not_flagged(self):
        # A short intro whose LB is proportionally low (deep cut — low on BOTH
        # platforms) stays inside the album spread.
        assert self._check(60, 8000, 900) is False

    def test_missing_duration_not_flagged(self):
        assert self._check(None, 45700, 20640) is False
        assert self._check(0, 45700, 20640) is False

    def test_low_lb_below_min_floor_not_flagged(self):
        assert self._check(75, 20000, 300) is False

    def test_zero_lf_not_flagged(self):
        assert self._check(75, 0, 20640) is False

    def test_too_few_album_pairs_skips(self):
        assert self._check(75, 45700, 20640, pairs=[(1000, 100), (500, 50)]) is False

    def test_custom_ratio_factor_can_loosen_filter(self):
        # A very loose factor (8x) no longer flags the ~4.5x DLB ratio.
        from services.popularity.popularity_math import is_interlude_lb_outlier

        assert is_interlude_lb_outlier(
            duration_seconds=75,
            lastfm_listeners=45700,
            listenbrainz_listens=20640,
            album_lf_lb_pairs=ALBUM_PAIRS,
            ratio_factor=8.0,
        ) is False


class TestInterludeLbOutlierConfig:
    def test_defaults(self):
        from services.popularity.popularity_config import get_interlude_lb_outlier_config

        cfg = get_interlude_lb_outlier_config({})
        assert cfg["enabled"] is True
        assert cfg["max_duration_s"] == 180.0
        assert cfg["ratio_factor"] == 3.0
        assert cfg["min_lb"] == 500

    def test_reads_config_keys(self):
        from services.popularity.popularity_config import get_interlude_lb_outlier_config

        cfg = get_interlude_lb_outlier_config({
            "single_detection": {
                "interlude_lb_outlier_enabled": False,
                "interlude_lb_max_duration_s": 120,
                "interlude_lb_ratio_factor": 5.0,
                "interlude_lb_min_count": 1000,
            }
        })
        assert cfg["enabled"] is False
        assert cfg["max_duration_s"] == 120.0
        assert cfg["ratio_factor"] == 5.0
        assert cfg["min_lb"] == 1000
