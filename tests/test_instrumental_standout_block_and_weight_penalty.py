"""Regression tests: instrumental versions never take the 5★ standout slot.

A "(instrumental)" version of a track can carry the same massive raw
listener counts as the vocal recording (e.g. "Beware" 80.4 score, +3.80
z-score on an album where it should stay a 4★ cut).  Two fixes:

1. **Standout/marking block** — instrumental titles are excluded from the
   ``z_standout`` upgrade in single detection, from the artist top-%
   ``popularity_marked`` 5★ award in the scan runner, and from the
   ``_has_z_standout_source`` re-verification in star assignment (legacy
   rows).  The track keeps its score and its era-cap rating — only the
   standout upgrade is blocked.
2. **Instrumental weight penalty** — ``single_detection.instrumental_weight_penalty``
   (default 0.8) reduces the Last.fm weight BEFORE the z-score, mirroring the
   live weight penalty, so the instrumental's z-score does not mathematically
   bury the vocal tracks on the album.
"""

from __future__ import annotations

import pytest

from services.catalog.album_classification_service import is_instrumental_track_title
from services.popularity.popularity_config import get_instrumental_weight_penalty


class TestInstrumentalTitleDetection:
    def test_plain_title_is_not_instrumental(self):
        assert is_instrumental_track_title("Beware") is False

    def test_parenthetical_instrumental(self):
        assert is_instrumental_track_title("Beware (instrumental)") is True

    def test_bracketed_instrumental(self):
        assert is_instrumental_track_title("Beware [Instrumental]") is True

    def test_dash_separated_instrumental(self):
        assert is_instrumental_track_title("Beware - Instrumental") is True

    def test_full_instrumental_marker(self):
        assert is_instrumental_track_title("Beware (Full Instrumental)") is True

    def test_instrumental_word_inside_other_word_ignored(self):
        # Whole-word match only — "Instrumentality" is not a version marker.
        assert is_instrumental_track_title("Instrumentality") is False

    def test_live_title_not_instrumental(self):
        assert is_instrumental_track_title("Beware (Live)") is False


class TestInstrumentalWeightPenaltyConfig:
    def test_default_is_0_8(self):
        # 20% reduction on the Last.fm weight by default.
        assert get_instrumental_weight_penalty({}) == 0.8

    def test_custom_value_read_from_config(self):
        assert get_instrumental_weight_penalty(
            {"single_detection": {"instrumental_weight_penalty": 0.7}}
        ) == 0.7

    def test_zero_disables(self):
        assert get_instrumental_weight_penalty(
            {"single_detection": {"instrumental_weight_penalty": 0}}
        ) == 0.0


class TestInstrumentalStandoutBlock:
    """An instrumental never becomes a popularity standout (z_standout)."""

    def _patch_db(self, monkeypatch):
        import services.popularity.popularity_stats_service as pss
        from services.enrichment import single_detection_service as sds

        # Artist catalogue: median 50, spread ~7 — an instrumental at 80.4
        # would otherwise be a massive standout (z ≈ +4).
        monkeypatch.setattr(pss, "calculate_artist_stats", lambda *a, **k: (50.0, 7.0, [45, 48, 50, 52, 55, 47, 51, 49]))
        monkeypatch.setattr(pss, "calculate_album_stats", lambda *a, **k: (55.0, 8.0, [48, 52, 56, 58, 60, 62]))
        monkeypatch.setattr(
            sds, "_detect_discogs",
            lambda *a, **k: {"source": "discogs", "matched": False, "confidence": 0.0, "metadata": {}},
        )
        monkeypatch.setattr(
            sds, "_detect_musicbrainz",
            lambda *a, **k: {"source": "musicbrainz", "matched": False, "confidence": 0.0, "metadata": {}},
        )
        monkeypatch.setattr(
            sds, "_detect_discogs_video",
            lambda *a, **k: {"source": "discogs_video", "matched": False, "confidence": 0.0, "metadata": {}},
        )

        class FakeLastFm:
            def check_track_as_single(self, artist, title):
                return False

            def get_album_track_count(self, artist, album):
                return 0

            def search_album(self, title, artist=None, limit=30):
                return []

        return FakeLastFm()

    def _detect(self, monkeypatch, title, lastfm_listeners, listenbrainz_listens):
        from services.enrichment.single_detection_service import detect_single_for_track

        lastfm_client = self._patch_db(monkeypatch)
        return detect_single_for_track(
            title=title,
            artist="Some Artist",
            album="Some Album",
            album_track_count=10,
            popularity=80.4,
            album_type="album",
            use_advanced_detection=True,
            persist_result=False,
            lastfm_client=lastfm_client,
            lastfm_listeners=lastfm_listeners,
            listenbrainz_listens=listenbrainz_listens,
            album_lf_listeners=[50000.0, 40000.0, 30000.0, 20000.0, 10000.0],
            album_lb_listens=[40000.0, 30000.0, 20000.0, 15000.0, 8000.0],
        )

    def test_instrumental_never_standout(self, monkeypatch):
        # The exact reported case: an instrumental with a massive score and a
        # huge composite listener z.  Blocked from the standout upgrade, so it
        # stays single=low and the era caps bounce it to 4★.
        result = self._detect(
            monkeypatch,
            title="Beware (instrumental)",
            lastfm_listeners=90000,
            listenbrainz_listens=60000,
        )
        decision = result["decision"]
        assert decision["z_composite"] > 1.5, decision
        assert decision["z_standout"] is False, result["reasons"]
        assert "instrumental_version" in result["reasons"], result["reasons"]
        assert result["is_single"] is False, result["reasons"]
        assert result["confidence"] == "low"

    def test_vocal_track_still_standout(self, monkeypatch):
        # The same title without the instrumental marker keeps its standout —
        # the block is title-pattern specific, not a global z_standout kill.
        result = self._detect(
            monkeypatch,
            title="Beware",
            lastfm_listeners=90000,
            listenbrainz_listens=60000,
        )
        decision = result["decision"]
        assert decision["z_composite"] > 1.5, decision
        assert decision["z_standout"] is True, result["reasons"]
        assert result["is_single"] is True, result["reasons"]
        assert result["confidence"] == "high"


class TestStarRatingInstrumentalBlock:
    """The star-rating pass re-checks the instrumental exclusion."""

    def _assign(self, track, single_sources, popularity_marked=False):
        from services.popularity.stages.finalise_stage import _assign_stars

        tr = dict(track)
        tr["single_sources"] = single_sources
        tr["popularity_marked"] = popularity_marked
        # The vocal at 80.4 is a clear album/artist standout: album median 70
        # (z≈1.3) and artist median 65 (z≈1.9) both clear the 5★ bands.
        return _assign_stars(
            tr,
            album_scores=[65.0, 68.0, 70.0, 72.0, 80.4],
            artist_scores=[60.0, 62.0, 65.0, 68.0, 80.4],
        )

    def test_instrumental_with_z_standout_source_never_5(self):
        # A legacy row that predates the instrumental gate may still carry
        # ``popularity_z_standout`` in single_sources — the re-verification
        # must refuse it for instrumental titles.
        track = {
            "title": "Beware (instrumental)",
            "popularity_score": 80.4,
            "lastfm_listeners": 90000,
            "single_confidence": "low",
        }
        stars = self._assign(
            track,
            '[{"source": "popularity_z_standout", "matched": true, "confidence": 0.5}]',
        )
        assert stars < 5, stars

    def test_instrumental_with_popularity_marked_never_5(self):
        track = {
            "title": "Beware (instrumental)",
            "popularity_score": 80.4,
            "lastfm_listeners": 90000,
            "single_confidence": "low",
        }
        stars = self._assign(track, "[]", popularity_marked=True)
        assert stars < 5, stars

    def test_vocal_track_keeps_standout_5(self):
        track = {
            "title": "Beware",
            "popularity_score": 80.4,
            "lastfm_listeners": 90000,
            "single_confidence": "low",
        }
        stars = self._assign(
            track,
            '[{"source": "popularity_z_standout", "matched": true, "confidence": 0.5}]',
        )
        assert stars == 5, stars


class TestInstrumentalWeightPenaltyScoring:
    """calculate_combined_popularity_score reduces the LF weight for
    instrumentals, mirroring the live penalty."""

    def _score(self, title):
        from services.popularity.popularity_math import calculate_combined_popularity_score

        return calculate_combined_popularity_score(
            lastfm_listeners=90000,
            lastfm_artist_max_listeners=90000,
            listenbrainz_listens=60000,
            album_lf_listeners=[90000.0, 40000.0, 30000.0, 20000.0, 10000.0],
            album_lb_listens=[60000.0, 30000.0, 20000.0, 15000.0, 8000.0],
            is_single=False,
            has_metadata=False,
            is_featured_track=False,
            is_live_track=False,
            is_instrumental_track=is_instrumental_track_title(title),
            source_audit="VALID",
        )

    def test_instrumental_scores_below_vocal_counterpart(self):
        vocal = self._score("Beware")
        instrumental = self._score("Beware (instrumental)")
        # The 20% LF weight reduction pulls the instrumental below the vocal.
        assert instrumental["combined_score"] < vocal["combined_score"], (instrumental, vocal)

    def test_instrumental_penalty_zero_no_reduction(self):
        from services.popularity.popularity_math import calculate_combined_popularity_score

        zero = calculate_combined_popularity_score(
            lastfm_listeners=90000,
            lastfm_artist_max_listeners=90000,
            listenbrainz_listens=60000,
            album_lf_listeners=[90000.0, 40000.0, 30000.0, 20000.0, 10000.0],
            album_lb_listens=[60000.0, 30000.0, 20000.0, 15000.0, 8000.0],
            is_single=False,
            has_metadata=False,
            is_featured_track=False,
            is_live_track=False,
            is_instrumental_track=True,
            source_audit="VALID",
            instrumental_weight_penalty=0.0,
        )
        vocal = self._score("Beware")
        assert zero["combined_score"] == pytest.approx(vocal["combined_score"], abs=0.01)
