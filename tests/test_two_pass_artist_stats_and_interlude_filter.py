"""Tests for two-pass artist pre-calculation + interlude/skit duration floor.

Two-pass artist pre-calculation: ``detect_single_for_track`` accepts an
``artist_stats_override`` (the scan runner's pass-1 pre-scan) so the first
album of a fresh artist gets a real ``artist_z`` instead of ≈0 against an
empty DB (the L.D. 50 / Dig scan-order artifact).

Interlude / skit duration floor: tracks shorter than
``statistics.exclude_from_median_below_seconds`` (default 90s) are excluded
from the album/artist median & MAD used for z-scores, so ambient interludes
(Monolith, Golden Ratio, ...) cannot compress the album median to ~50 and
shrink the variance that hides real standouts.
"""

from __future__ import annotations

import pytest

from services.catalog.album_classification_service import (
    is_bonus_track_title,
    should_exclude_track_from_stats,
)
from services.popularity.popularity_stats_service import calculate_album_stats


class TestInterludeDurationFloor:
    """Tracks shorter than the configured floor are excluded from stats."""

    def test_short_interlude_is_excluded(self):
        # A 30-second ambient interlude must not anchor the album median.
        assert should_exclude_track_from_stats(
            "Monolith", "L.D. 50", duration=30,
        ) is True

    def test_normal_track_is_kept(self):
        assert should_exclude_track_from_stats(
            "Severed", "L.D. 50", duration=285,
        ) is False

    def test_zero_duration_keeps_track(self):
        # Unknown/zero duration is not an interlude signal.
        assert should_exclude_track_from_stats(
            "Dig", "L.D. 50", duration=None,
        ) is False

    def test_floor_disabled_keeps_short_track(self):
        # exclude_below_seconds=0 disables the filter.
        assert should_exclude_track_from_stats(
            "Monolith", "L.D. 50", duration=30,
            exclude_below_seconds=0,
        ) is False

    def test_custom_floor(self):
        # A 2-minute track is short when the floor is 180s.
        assert should_exclude_track_from_stats(
            "Short One", "Album", duration=100,
            exclude_below_seconds=180,
        ) is True

    def test_bonus_title_still_excluded_without_duration(self):
        # Existing live/alternate rules still apply.
        assert is_bonus_track_title("Song (Live)") is True
        assert should_exclude_track_from_stats("Song (Live)", "Album") is True


class TestTwoPassArtistStatsOverride:
    """detect_single_for_track uses the pre-computed artist catalogue."""

    def _patch_db_empty(self, monkeypatch):
        # A first-scanned album track with NO DB stats: simulate a fresh
        # artist where the DB-backed lookup returns nothing.
        import services.popularity.popularity_stats_service as pss

        monkeypatch.setattr(pss, "calculate_artist_stats", lambda *a, **k: (0.0, 0.0, []))
        monkeypatch.setattr(pss, "calculate_album_stats", lambda *a, **k: (50.0, 10.0, [40, 50, 60, 55, 45, 52, 48]))

    def test_override_used_for_artist_z(self, monkeypatch):
        from services.enrichment import single_detection_service as sds

        self._patch_db_empty(monkeypatch)

        # Override: a catalogue with median 50 and MAD ~5 (spread ~10.4).
        # A track at popularity 75 sits ~2.4 z above the median — the
        # pre-computed catalogue gives it a real artist_z instead of ≈0.
        result = sds.detect_single_for_track(
            title="Dig",
            artist="Mudvayne",
            album="L.D. 50",
            album_track_count=13,
            popularity=75.0,
            album_type="album",
            use_advanced_detection=False,
            persist_result=False,
            artist_stats_override=[50, 50, 55, 45, 52, 48, 53, 47, 51, 49],
        )
        decision = result["decision"]
        assert decision["artist_z"] > 1.5, decision

    def test_no_override_falls_back_to_db(self, monkeypatch):
        from services.enrichment import single_detection_service as sds

        self._patch_db_empty(monkeypatch)

        # Without an override, the DB-backed stats are used (empty here, so
        # artist_z stays ~0 — the pre-scan is what fixes this in the runner).
        result = sds.detect_single_for_track(
            title="Dig",
            artist="Mudvayne",
            album="L.D. 50",
            album_track_count=13,
            popularity=75.0,
            album_type="album",
            use_advanced_detection=False,
            persist_result=False,
        )
        decision = result["decision"]
        assert decision["artist_z"] <= 1.5, decision
