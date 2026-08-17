"""Regression tests for the dashboard 'All' scan aligned with the artist page.

Covers:
- ``_run_full_scan_as_artist_pipeline`` iterates over every artist, running
  the full artist pipeline for each, and reports % of total artists.
- A stop request halts the loop and marks the scan "stopped".
- ``run_popularity_mode(mode="all")`` routes to the per-artist loop (not the
  old single combined pass).
"""

from __future__ import annotations

import re


class _ProgressRecorder:
    """Records ``write_progress_with_current_artist`` calls."""

    def __init__(self):
        self.calls = []

    def __call__(self, path, scan_type, is_running, current_artist=None, extra=None):
        self.calls.append({
            "path": path,
            "scan_type": scan_type,
            "is_running": is_running,
            "current_artist": current_artist,
            "extra": dict(extra or {}),
        })


def _patch_env(monkeypatch, artists, stop_after=None):
    import services.scanning.pipelines.popularity_pipeline as pp
    import db.repositories.library as library_mod
    import services.scanning.pipeline as pipeline_mod
    import services.popularity.stages.load_stage as load_stage_mod

    calls = []

    def fake_run(artist, force=False, progress_callback=None):
        calls.append((artist, force))
        if callable(progress_callback):
            # Simulate the real artist pipeline's stage sequence: a 4-album
            # metadata pass, the combined pass (popularity then singles at the
            # midpoint), and a coarse essentia pass.
            for _i in range(4):
                progress_callback("metadata", _i, 4, f"{artist} - Album {_i + 1}")
            for _i in range(4):
                progress_callback("popularity", _i, 4, f"{artist} - Album {_i + 1}")
            for _i in range(4):
                progress_callback("singles", _i, 4, f"{artist} - Album {_i + 1}")
            progress_callback("essentia", 0, 1, artist)
            progress_callback("essentia", 1, 1, artist)

    monkeypatch.setattr(library_mod, "get_all_artists", lambda: list(artists))
    monkeypatch.setattr(pipeline_mod, "run_artist_scan_pipeline", fake_run)
    monkeypatch.setattr(
        load_stage_mod, "_artist_key",
        lambda v: re.sub(r"[^a-z0-9]+", "", (v or "").lower()),
    )
    monkeypatch.setattr(pp, "get_scan_progress_path", lambda t: t)

    recorder = _ProgressRecorder()
    monkeypatch.setattr(pp, "write_progress_with_current_artist", recorder)

    if stop_after is None:
        monkeypatch.setattr(pp, "is_stop_requested", lambda path: False)
    else:
        counter = {"n": 0}

        def _stop(path):
            counter["n"] += 1
            return counter["n"] > stop_after

        monkeypatch.setattr(pp, "is_stop_requested", _stop)

    monkeypatch.setattr(pp, "record_scan", lambda *a, **k: None)
    monkeypatch.setattr(pp, "log_unified", lambda *a, **k: None)

    return pp, recorder, calls


class TestFullScanAsArtistPipeline:
    def test_runs_every_artist_with_stage_aware_percent(self, monkeypatch):
        pp, recorder, calls = _patch_env(monkeypatch, ["Artist A", "Artist B", "Artist C"])

        pp._run_full_scan_as_artist_pipeline()

        assert calls == [("Artist A", False), ("Artist B", False), ("Artist C", False)]

        # Running writes come from the progress callback (14 per artist).
        running = [c for c in recorder.calls if c["is_running"] and c["current_artist"]]
        assert len(running) == 3 * 14

        # Monotonic overall % across the whole full scan (never jumps back).
        pcts = [c["extra"]["percent_complete"] for c in running]
        assert all(pcts[i] <= pcts[i + 1] for i in range(len(pcts) - 1))

        # Stage labels appear in order per artist: 4×Metadata, 4×Popularity,
        # 4×Singles Detection, 2×Essentia.
        stages = [c["extra"]["current_stage"] for c in running]
        assert stages[:4] == ["Metadata"] * 4
        assert stages[4:8] == ["Popularity"] * 4
        assert stages[8:12] == ["Singles Detection"] * 4
        assert stages[12:14] == ["Essentia"] * 2

        # A 4-album metadata stage on a 3-artist scan: the 1st album of Artist
        # A is 1/4 of the first stage slot → (1/4 × 100/3 / 4) ≈ 2%.
        assert running[0]["extra"]["percent_complete"] >= 2

        # Each artist's share tops out at ~33 / 66 / 100%.
        assert running[13]["extra"]["percent_complete"] <= 34
        assert running[27]["extra"]["percent_complete"] <= 67
        assert running[41]["extra"]["percent_complete"] == 100

        final = recorder.calls[-1]
        assert final["is_running"] is False
        assert final["extra"]["status"] == "complete"
        assert final["extra"]["percent_complete"] == 100

    def test_force_flag_propagated(self, monkeypatch):
        pp, recorder, calls = _patch_env(monkeypatch, ["Artist A"])

        pp._run_full_scan_as_artist_pipeline(force=True)

        assert calls == [("Artist A", True)]

    def test_stop_request_halts_loop(self, monkeypatch):
        # Stop is checked before each artist: after the 1st artist the 2nd
        # check (n=2 > 1) returns True, so only Artist A runs.
        pp, recorder, calls = _patch_env(monkeypatch, ["Artist A", "Artist B", "Artist C"], stop_after=1)

        pp._run_full_scan_as_artist_pipeline()

        assert calls == [("Artist A", False)]

        final = recorder.calls[-1]
        assert final["is_running"] is False
        assert final["extra"]["status"] == "stopped"
        assert final["extra"]["percent_complete"] == 0

    def test_empty_library_marks_complete(self, monkeypatch):
        pp, recorder, calls = _patch_env(monkeypatch, [])

        pp._run_full_scan_as_artist_pipeline()

        assert calls == []
        final = recorder.calls[-1]
        assert final["is_running"] is False
        assert final["extra"]["status"] == "complete"
        assert final["extra"]["percent_complete"] == 100


class TestRunPopularityModeRoutesAll:
    def test_all_routes_to_artist_pipeline_not_combined_pass(self, monkeypatch):
        import services.scanning.pipelines.popularity_pipeline as pp
        from unittest.mock import MagicMock

        monkeypatch.setattr(pp, "_run_full_scan_as_artist_pipeline", MagicMock())
        monkeypatch.setattr(pp, "run_popularity_scan", MagicMock())

        pp.run_popularity_mode(mode="all", force_rescan=True)

        pp._run_full_scan_as_artist_pipeline.assert_called_once_with(force=True, resume_from=None)
        pp.run_popularity_scan.assert_not_called()
