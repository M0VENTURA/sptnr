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
