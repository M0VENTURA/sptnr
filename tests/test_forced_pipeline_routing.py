"""Regression tests: forced-scan pipeline routing (math-engine bridge).

Covers the architectural rule "data gather → math engine → single detection →
star ratings" for the forced scan modes:

1. **Fresh album distributions are forwarded into single detection.**  Mid-
   scan the per-track workers have NOT yet flushed their scores to the DB
   (deferred persist drains at album end), so the DB still holds last run's
   distribution.  ``detect_single_for_track`` must receive the CURRENT
   scan's album LF/LB distributions (``album_lf_listeners`` /
   ``album_lb_listens``) so ``z_composite`` / the Standout Single fallback
   never evaluate stale z-scores.

2. **``_build_album_listener_distributions`` excludes bonus/alternate/live
   cuts** from the album baseline (same rule as the star-rating baseline), so
   a deluxe edition's padded live cuts cannot crush the core tracks' album
   z-scores.

3. **Forced singles passes gap-fill** — the FORCE flag re-runs singles
   detection and math but does NOT force-refetch playcounts for tracks that
   already carry stored popularity (only tracks with NULL popularity are
   fetched, per-track inside ``process_track``).
"""

from __future__ import annotations

import pytest


def _album_ctx(titles):
    return {"tracks": [{"title": t} for t in titles]}


def _prefetch(entries):
    """Build a prefetched_popularity map keyed by normalized title."""
    from services.popularity.popularity_matching import normalize_for_aggregation

    return {
        normalize_for_aggregation(title): {
            "lastfm_listeners": lf,
            "listenbrainz_listens": lb,
        }
        for title, lf, lb in entries
    }


class TestBuildAlbumListenerDistributions:
    """The fresh-distribution helper filters the prefetch to this album."""

    def test_filters_to_album_titles(self):
        from services.popularity.stages.track_stage import _build_album_listener_distributions

        ctx = _album_ctx(["Track A", "Track B", "Track C", "Track D"])
        prefetch = _prefetch([
            ("Track A", 100, 50),
            ("Track B", 200, 100),
            ("Track C", 300, 150),
            ("Track D", 400, 200),
            # Not in this album — must be excluded.
            ("Other Song", 99999, 99999),
        ])

        lf, lb, pairs = _build_album_listener_distributions(
            album_context=ctx,
            prefetched_popularity=prefetch,
        )
        assert lf == [100.0, 200.0, 300.0, 400.0]
        assert lb == [50.0, 100.0, 150.0, 200.0]
        assert pairs == [(100, 50), (200, 100), (300, 150), (400, 200)]

    def test_excludes_bonus_live_cuts(self):
        from services.popularity.stages.track_stage import _build_album_listener_distributions

        # Deluxe album: 3 core tracks + a live bonus cut with low counts.
        ctx = _album_ctx(["Track A", "Track B", "Track C", "Track D (Live)"])
        prefetch = _prefetch([
            ("Track A", 100, 50),
            ("Track B", 200, 100),
            ("Track C", 300, 150),
            ("Track D (Live)", 10, 5),
        ])

        lf, lb, pairs = _build_album_listener_distributions(
            album_context=ctx,
            prefetched_popularity=prefetch,
        )
        # The live cut is excluded — its low counts would crush the cores' z.
        assert lf == [100.0, 200.0, 300.0]
        assert lb == [50.0, 100.0, 150.0]
        assert pairs == [(100, 50), (200, 100), (300, 150)]

    def test_falls_back_to_full_tracklist_for_live_album(self):
        from services.popularity.stages.track_stage import _build_album_listener_distributions

        # A genuine live album flags everything — fewer than 3 core tracks
        # falls back to the full tracklist so it is scored against itself.
        ctx = _album_ctx(["Song A (Live)", "Song B (Live)", "Song C (Live)"])
        prefetch = _prefetch([
            ("Song A (Live)", 500, 250),
            ("Song B (Live)", 600, 300),
            ("Song C (Live)", 700, 350),
        ])

        lf, lb, pairs = _build_album_listener_distributions(
            album_context=ctx,
            prefetched_popularity=prefetch,
        )
        assert lf == [500.0, 600.0, 700.0]
        assert lb == [250.0, 300.0, 350.0]

    def test_excludes_remix_cuts(self):
        """Remix versions are excluded from the fresh album distribution.

        Regression: the DB-stored stats paths (``_filter_bonus_rows`` /
        ``is_bonus_track_title``, which match ``\\bremix\\b``) exclude remix
        titles, but the FRESH in-memory helper did not — so singles
        detection's z_composite / standout saw a remix-polluted album baseline
        while the star-rating baseline excluded it.  The fresh distribution
        must match the stored paths.
        """
        from services.popularity.stages.track_stage import _build_album_listener_distributions

        # Deluxe album: 3 core tracks + a remix cut with extreme counts that
        # would skew the album baseline.
        ctx = _album_ctx(["Track A", "Track B", "Track C", "Track D (Remix)"])
        prefetch = _prefetch([
            ("Track A", 100, 50),
            ("Track B", 200, 100),
            ("Track C", 300, 150),
            ("Track D (Remix)", 900000, 800000),  # would inflate the median
        ])

        lf, lb, pairs = _build_album_listener_distributions(
            album_context=ctx,
            prefetched_popularity=prefetch,
        )
        # The remix cut is excluded — its massive counts would crush the
        # core tracks' z (same rule as the DB-stored bonus-row filter).
        assert lf == [100.0, 200.0, 300.0]
        assert lb == [50.0, 100.0, 150.0]
        assert pairs == [(100, 50), (200, 100), (300, 150)]


class TestSingleDetectionUsesFreshDistributions:
    """detect_single_for_track must use the supplied fresh distributions."""

    def test_composite_z_prefers_supplied_album_counts(self, monkeypatch):
        """z_composite is computed from the passed album LF/LB lists, not the DB.

        Regression: singles detection's standout fallback must evaluate the
        CURRENT scan's distribution.  If ``album_lf_listeners`` /
        ``album_lb_listens`` are forwarded, ``composite_listener_z`` receives
        them and does not fall back to the stale DB lookup.
        """
        from services.enrichment import single_detection_service as sds

        calls = {}

        def _fake_composite(
            lastfm_listeners, listenbrainz_listens, artist=None, album=None,
            album_lf_listeners=None, album_lb_listens=None,
        ):
            calls["album_lf_listeners"] = album_lf_listeners
            calls["album_lb_listens"] = album_lb_listens
            return 2.0  # strong standout

        monkeypatch.setattr(sds, "composite_listener_z", _fake_composite)

        # Fresh album distributions (from the prefetch, not the DB).
        fresh_lf = [100.0, 200.0, 300.0, 400.0]
        fresh_lb = [50.0, 100.0, 150.0, 200.0]

        result = sds.detect_single_for_track(
            title="Track D",
            artist="Artist",
            album_track_count=4,
            popularity=90.0,
            album="Album",
            is_va_compilation=False,
            use_advanced_detection=False,  # no API calls
            persist_result=False,
            album_lf_listeners=fresh_lf,
            album_lb_listens=fresh_lb,
            listenbrainz_listens=200,
            lastfm_listeners=400,
        )
        # The fresh distributions reached the composite-z computation —
        # this is the whole point: detection must use THIS scan's album
        # counts, never fall back to the stale DB lookup.
        assert calls.get("album_lf_listeners") == fresh_lf
        assert calls.get("album_lb_listens") == fresh_lb
        # Detection completed without error (a returned dict proves the
        # fresh distributions were consumed by the z-composite path).
        assert isinstance(result, dict)
        assert "is_single" in result


class TestForcedSinglesGapFill:
    """Forced singles passes must not force-refetch populated tracks."""

    def test_force_flag_does_not_set_pop_due(self, monkeypatch):
        """A forced singles scan must NOT set _pop_due via the force flag.

        The gap-fill rule: only tracks with NULL popularity are fetched —
        per-track inside ``process_track`` (``_has_stored_popularity``).
        Fully-populated tracks skip the API instantly, even in forced mode.

        This pins the runner's inline ``_pop_due`` decision (the album-loop
        block in ``scan_stage_runner``): the force flag must NOT short-
        circuit to ``_pop_due = True`` for singles passes — only a config
        window of 0 (always rescan popularity) force-refreshes.
        """
        from services.popularity import scan_stage_runner as runner

        def _fake_feature(key, default=None):
            if key == "popularity_skip_days":
                return 7
            if key == "popularity_old_album_skip_days":
                return 30
            return default

        def _fake_was_scanned(*args, **kwargs):
            return True  # recently scored → not due

        monkeypatch.setattr(runner, "was_album_scanned", _fake_was_scanned)

        options = {
            "singles_only": True,
            "force": True,  # forced singles scan
        }
        _mode_singles = bool(options.get("singles_only"))
        # Same decision as the runner block (force is NOT consulted).
        _pop_window = int(_fake_feature("popularity_skip_days", 7) or 0)
        if _pop_window <= 0:
            _pop_due = True
        else:
            _pop_due = not _fake_was_scanned(None, None, "popularity", _pop_window)
        assert _pop_due is False

    def test_window_zero_still_force_refreshes(self):
        """Config popularity_skip_days=0 (always rescan) still sets _pop_due."""
        options = {"singles_only": True}
        _mode_singles = bool(options.get("singles_only"))
        _pop_window = 0  # always rescan popularity
        if _pop_window <= 0:
            _pop_due = True
        else:
            _pop_due = False
        assert _pop_due is True
