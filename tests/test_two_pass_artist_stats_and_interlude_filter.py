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


class TestIntroTitleExclusion:
    """Intro / interlude titles never anchor the stats baseline."""

    def test_dig_intro_is_excluded(self):
        # The exact Mudvayne "By the People" case: a spoken intro with ~200
        # listens must not crater the album floor.
        assert should_exclude_track_from_stats(
            "Dig Intro", "By the People, for the People", duration=42,
        ) is True

    def test_plain_intro_title_excluded(self):
        assert should_exclude_track_from_stats("Intro", "Album") is True

    def test_interlude_title_excluded(self):
        assert should_exclude_track_from_stats(
            "Golden Ratio (Interlude)", "Album", duration=35,
        ) is True

    def test_bracketed_intro_excluded(self):
        assert should_exclude_track_from_stats(
            "Silenced [Intro]", "Album", duration=28,
        ) is True

    def test_real_track_with_intro_word_kept(self):
        # "Introduction" (full word) is not matched by the suffix regex.
        assert should_exclude_track_from_stats(
            "Introduction", "Album", duration=210,
        ) is False

    def test_empty_regex_disables(self):
        assert should_exclude_track_from_stats(
            "Dig Intro", "Album", duration=42,
            exclude_title_regex="",
        ) is False


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

    def test_log_listens_artist_z(self, monkeypatch):
        """artist_z from log10(listens) against the global listen catalogue.

        Mudvayne's real distribution: Dig at 530k listeners vs a catalogue
        dominated by 10k-90k tracks.  The log-scale z must put Dig clearly at
        the top (artist_z >> 1) instead of the near-zero the score-based
        album-relative baseline produced.
        """
        from services.enrichment import single_detection_service as sds

        self._patch_db_empty(monkeypatch)

        # Artist catalogue by raw Last.fm listeners (Mudvayne-like spread:
        # one 530k monster, several 40-90k, many 5-15k demos, plus the
        # 200-listen intros — which the runner strips before this override).
        catalogue = [530000, 93000, 46000, 90000, 40000, 25000, 15000,
                     14000, 11000, 10000, 8000, 6000, 5000, 3000, 2000, 1000]
        result = sds.detect_single_for_track(
            title="Dig",
            artist="Mudvayne",
            album="L.D. 50",
            album_track_count=13,
            popularity=73.4,
            lastfm_listeners=530900,
            album_type="album",
            use_advanced_detection=False,
            persist_result=False,
            artist_listen_override=catalogue,
        )
        decision = result["decision"]
        # log10(530900)≈5.72 vs median log10≈log10(13000)≈4.11 → z well above 2.
        assert decision["artist_z"] > 2.0, decision

    def test_log_listens_artist_z_low_track(self, monkeypatch):
        """A mid-catalogue track gets a modest (not huge) artist_z."""
        from services.enrichment import single_detection_service as sds

        self._patch_db_empty(monkeypatch)

        catalogue = [530000, 93000, 46000, 90000, 40000, 25000, 15000,
                     14000, 11000, 10000, 8000, 6000, 5000, 3000, 2000, 1000]
        result = sds.detect_single_for_track(
            title="Severed",
            artist="Mudvayne",
            album="L.D. 50",
            album_track_count=13,
            popularity=50.0,
            lastfm_listeners=120000,
            album_type="album",
            use_advanced_detection=False,
            persist_result=False,
            artist_listen_override=catalogue,
        )
        decision = result["decision"]
        # log10(120000)≈5.08 vs median≈4.11 → z around +0.7-1.2 — mid-pack.
        assert 0.3 < decision["artist_z"] < 2.0, decision
