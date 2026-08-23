"""Regression tests for the dashboard scanning-log filter.

The ``_scan_activity_filter`` keeps ONLY popularity/singles scan activity in
the dashboard scanning panel (and the /logs unified view).  It must surface
the per-track score lines, the star-rating lines, and the singles-detection
summary — otherwise operators lose exactly the output the scan is supposed
to show (which is what this filter was hiding).
"""

from __future__ import annotations

from services.log_service import _scheduler_noise_filter, _scan_activity_filter


def _kept(line: str) -> bool:
    return bool(
        _scan_activity_filter().search(line)
        and not _scheduler_noise_filter().search(line)
    )


class TestScanResultLinesVisible:
    """Per-track score / rating / singles output must pass the filter."""

    def test_consolidated_per_track_line(self):
        line = '[TRACK] 🎵 "Hysteria" | Score: 85.0 (LF: 12.3k, LB: 45.6k) | Single: HIGH [MusicBrainz, Discogs]'
        assert _kept(line)

    def test_per_track_score_line(self):
        line = "[TRACK_RESULT] 'Hysteria' -> Final: 85.0 (SP: 70.0 | LF: 80.0 | LB: 90.0)"
        assert _kept(line)

    def test_per_track_rating_line(self):
        line = "[TRACK_RESULT] Muse - Hysteria → 5★ (score=85.0, album_z=1.20, artist_z=1.10, single=True/high)"
        assert _kept(line)

    def test_singles_detection_summary(self):
        line = "Singles Detection - Detected 1 single(s) in 'Absolution'"
        assert _kept(line)

    def test_scan_results_table_banner(self):
        line = "📊 SCAN RESULTS: Muse — Absolution (10 Tracks)"
        assert _kept(line)

    def test_scan_results_table_header(self):
        line = "RATING  TRACK TITLE  Z-SCORE  SCORE  LF LISTENS  SINGLE CONF"
        assert _kept(line)

    def test_scan_results_table_row(self):
        line = "★★★★★     Hysteria                     +1.20   85.0     12,345  HIGH (Musicbrainz, Discogs)"
        assert _kept(line)

    def test_star_distribution_line(self):
        line = "⭐ Distribution: 5★: 1 | 4★: 2 | 3★: 3 | 2★: 1 | 1★: 0"
        assert _kept(line)

    def test_navidrome_rating_sync_line(self):
        line = "🔗 Navidrome: synced 3 rating(s) for 'Muse'"
        assert _kept(line)

    def test_frozen_track_result_line(self):
        line = "[TRACK_RESULT] 'Hysteria' -> Final: 85.0 (frozen, SP: 70.0 | LF: 80.0 | LB: 90.0)"
        assert _kept(line)


class TestFullScanSessionLinesVisible:
    """The dashboard 'All' (full-scan) worker's progress must be visible.

    ``_run_full_scan_as_artist_pipeline`` emits ``[FULL_SCAN] ...`` and the
    per-artist pipeline emits ``[SCAN_PIPELINE] ...`` lines.  Before these
    were added to ``_scan_activity_filter`` the user saw "nothing in logs"
    while the Recent Scans panel showed the full scan — the worker WAS
    running (and could be failing instantly on an empty artist list) but
    every line it wrote was silently filtered out of both the dashboard
    scanning panel and the /logs unified view.
    """

    def test_full_scan_start_line(self):
        line = "[FULL_SCAN] Starting full scan — 42 artist(s) queued"
        assert _kept(line)

    def test_full_scan_no_artists_line(self):
        line = "[FULL_SCAN] No artists found in the library — nothing to scan. Check the library has been imported (Navidrome sync)."
        assert _kept(line)

    def test_full_scan_artist_progress_line(self):
        line = "[FULL_SCAN] Artist 1/42: Beast in Black"
        assert _kept(line)

    def test_full_scan_artist_done_line(self):
        line = "[FULL_SCAN] Artist 1/42 done: Beast in Black"
        assert _kept(line)

    def test_full_scan_artist_failed_line(self):
        line = "[FULL_SCAN] Artist 2/42 FAILED: Some Artist — boom"
        assert _kept(line)

    def test_full_scan_finished_line(self):
        line = "[FULL_SCAN] Finished with status=complete"
        assert _kept(line)

    def test_scan_pipeline_start_line(self):
        line = "[SCAN_PIPELINE] Starting artist pipeline: Beast in Black (force=False)"
        assert _kept(line)

    def test_popularity_worker_start_finish_lines(self):
        assert _kept("[POPULARITY] Worker starting mode=all force=True")
        assert _kept("[POPULARITY] Worker finished mode=all")
        assert _kept("[POPULARITY] Worker failed: boom")


class TestNoiseStillFiltered:
    """Queue / watcher churn must stay out of the scanning panel."""

    def test_queue_import_line_filtered(self):
        line = "[QUEUE] Muse - Hysteria → imported to library (match=metadata)"
        assert not _kept(line)

    def test_queue_failure_line_filtered(self):
        line = "[QUEUE] Muse - Hysteria → failed: no file found while marked downloading (stale)"
        assert not _kept(line)

    def test_discover_audio_files_line_filtered(self):
        line = "[SCAN] Discovered 42 audio files"
        assert not _kept(line)

    def test_scheduler_bookkeeping_filtered(self):
        line = "APScheduler: registered download_queue_processor (every 60 s)"
        assert not _kept(line)


class TestAlbumProgressLinesVisible:
    """Per-album progress lines must be visible in the scanning panel.

    The ``[N/M] Processing: ...`` line is emitted at the top of each album's
    pass — before the potentially long prefetch phase.  When it was missing
    from ``_scan_activity_filter`` the panel showed "Popularity Scan - Letter
    'D'" then silence for minutes, which looked exactly like a stalled scan
    (it was the artist prefetch running without any visible output).
    """

    def test_processing_line_visible(self):
        line = '[1/3] Processing: "Songs of a Lost World" (3 Tracks)'
        assert _kept(line)

    def test_processing_line_with_album_index(self):
        line = '[12/42] Processing: "Absolution" (10 Tracks)'
        assert _kept(line)

    def test_prefetch_start_line_visible(self):
        line = "[POPULARITY] Prefetching popularity + release data for 'Muse' (budget 360s)"
        assert _kept(line)

    def test_prefetch_complete_line_visible(self):
        line = "[POPULARITY] Prefetch complete for 'Muse' in 42.3s (120 tracks pre-loaded)"
        assert _kept(line)
