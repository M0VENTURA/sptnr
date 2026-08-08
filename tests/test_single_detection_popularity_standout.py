"""Regression tests: popularity standouts are detected as singles.

Reproduces the Stray Kids scans where genuinely dominant tracks were missed:

- District 9 (180k listeners vs ~92k for the next track, album-z ~1.8) with a
  Last.fm confirmation was marked ``single=low`` (status=none, hi=0, med=1):
  the high z-band required ``medium >= 2`` and a lone weak source was dropped.
- "U (Stray Kids feat. Tablo)" had the highest album z in HOP (1.80) but zero
  source matches — and its feature-artist name meant the popularity-stats
  lookups (grouped by album artist "Stray Kids") found nothing, so even the
  z-score / standout signal could not compute.

Fix: a catalog-size-aware popularity standout (``z_standout``) now confirms
``medium`` in the high z-band on its own, the dynamic z threshold was lowered
so strong outliers (z≈1.8) actually qualify, and the stats artist is resolved
to the album artist for "feat." tracks.
"""

from __future__ import annotations

import pytest


def _final(**kw):
    from services.enrichment.single_detection_service import determine_final_status

    defaults = dict(
        discogs=False,
        musicbrainz=False,
        album_z=0.0,
        artist_z=0.0,
        radio_edit=False,
        has_metadata=False,
        is_title_track=False,
        is_compilation=False,
        zscore_high=1.0,
        zscore_medium=0.6,
        high_sources=0,
        medium_sources=0,
    )
    defaults.update(kw)
    return determine_final_status(**defaults)


class TestPopularityStandoutConfirmsSingle:
    """A strong popularity outlier reaches medium/high in the high z-band."""

    def test_standout_alone_is_medium(self):
        # "U" case: album-z 1.80, zero metadata matches, only the popularity
        # standout signal (counted as one medium source).
        assert _final(
            album_z=1.8,
            artist_z=0.8,
            high_sources=0,
            medium_sources=1,
            z_standout=True,
        ) == "medium"

    def test_standout_with_weak_source_is_high(self):
        # District 9 case: standout + Last.fm confirmation -> two medium
        # sources in the high band -> high.
        assert _final(
            album_z=1.8,
            artist_z=0.88,
            high_sources=0,
            medium_sources=2,
            z_standout=True,
        ) == "high"

    def test_standout_with_metadata_confirmation_is_high(self):
        assert _final(
            discogs=True,
            album_z=1.8,
            artist_z=0.8,
            high_sources=1,
            medium_sources=1,
            z_standout=True,
            has_metadata=True,
        ) == "high"

    def test_high_z_no_sources_no_standout_still_none(self):
        # Tehran/Crossroads guardrail: z ~1.2 with no corroboration must stay
        # unflagged — z_standout (>= ~1.6-1.8) is required to confirm.
        assert _final(
            album_z=1.2,
            artist_z=1.2,
            high_sources=0,
            medium_sources=0,
        ) == "none"

    def test_high_z_one_weak_source_no_standout_still_none(self):
        # One weak signal without a popularity standout is not enough.
        assert _final(
            album_z=1.4,
            artist_z=1.4,
            high_sources=0,
            medium_sources=1,
            radio_edit=True,
            has_metadata=False,
        ) == "none"

    def test_medium_band_one_weak_source_still_none(self):
        # The medium band (0.6-1.0) still requires medium >= 2.
        assert _final(
            album_z=0.8,
            artist_z=0.8,
            high_sources=0,
            medium_sources=1,
            radio_edit=True,
            has_metadata=True,
        ) == "none"


class TestDynamicZThreshold:
    """Strong outliers qualify as standouts across catalog sizes."""

    def _thresh(self, n):
        from services.enrichment.single_detection_service import get_dynamic_z_threshold
        return get_dynamic_z_threshold(n)

    def test_tiny_catalog(self):
        assert self._thresh(3) == 1.5

    def test_small_catalog(self):
        assert self._thresh(8) == 1.7

    def test_medium_catalog(self):
        assert self._thresh(20) == 1.8

    def test_large_catalog(self):
        assert self._thresh(100) == 1.7

    def test_very_large_catalog(self):
        assert self._thresh(300) == 1.6

    def test_district9_style_outlier_qualifies(self):
        # album-z ~1.8 must beat the threshold for a mid-to-large catalog so
        # the standout fires instead of falling into the boundary rounding.
        assert 1.8 >= self._thresh(100)
        assert 1.8 >= self._thresh(50)


class TestFeatureTrackStatsResolution:
    """A "Artist feat. Guest" track resolves stats to the album artist."""

    def test_feature_track_uses_album_artist_stats(self, monkeypatch):
        import services.popularity.popularity_stats_service as pss
        from services.enrichment.single_detection_service import detect_single_for_track

        def fake_artist_stats(conn, artist):
            if artist == "Stray Kids":
                return (50.0, 10.0, [50, 45, 40, 55, 48, 42, 60, 52, 44, 47])
            return (0.0, 0.0, [])

        def fake_album_stats(conn, artist, album):
            if artist == "Stray Kids" and album == "HOP":
                return (45.0, 8.0, [45, 42, 40, 44, 41])
            return (0.0, 0.0, [])

        monkeypatch.setattr(pss, "calculate_artist_stats", fake_artist_stats)
        monkeypatch.setattr(pss, "calculate_album_stats", fake_album_stats)
        monkeypatch.setattr(
            "services.enrichment.single_detection_service._detect_musicbrainz",
            lambda *a, **k: {"source": "musicbrainz", "matched": False, "confidence": 0.0, "metadata": {}},
        )

        # "U" by "Stray Kids feat. Tablo" — the raw-name stats lookup finds
        # nothing, so detection falls back to the album artist "Stray Kids".
        result = detect_single_for_track(
            title="U",
            artist="Stray Kids feat. Tablo",
            album="HOP",
            album_track_count=14,
            popularity=60.0,
            album_type="album",
            use_advanced_detection=False,
            persist_result=False,
        )

        decision = result["decision"]
        assert decision["album_z"] > 1.5, decision
        assert decision["z_standout"] is True, result["reasons"]
        assert result["is_single"] is True
        assert result["confidence"] == "medium"
