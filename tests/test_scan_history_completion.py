"""Regression tests: scan completion rows must be recorded even when no
matching 'started' row exists.

Previously ``record_scan(status != 'started')`` only UPDATEd the most recent
'started' row — if none matched (scan launched by a path that never recorded
'started', or the started row used a different artist), the UPDATE matched
nothing and the completion was silently dropped.  The dashboard then showed
no scan history.  The fix INSERTs the completion row when the UPDATE affects
zero rows.
"""

from __future__ import annotations

from services.scanning.scan_history_service import (
    get_recent_album_scans,
    record_scan,
)


class TestRecordScanCompletionWithoutStarted:
    def test_completion_without_started_row_is_recorded(self, db_session):
        """A 'completed' record with no prior 'started' row must still land in
        scan_history (previously the UPDATE matched nothing and dropped it)."""
        record_scan("popularity", "completed", message="scan completed",
                    artist="_SCAN_SESSION_", album="popularity")

        scans = get_recent_album_scans(limit=10)
        matches = [
            s for s in scans
            if s.get("scan_type") == "popularity"
            and s.get("artist") == "_SCAN_SESSION_"
            and s.get("status") == "completed"
        ]
        assert len(matches) >= 1, "completion row missing from scan history"

    def test_started_then_completed_updates_same_row(self, db_session):
        """The normal started→completed flow updates the SAME row (no dup)."""
        record_scan("artist", "started", message="start", artist="Radiohead")
        record_scan("artist", "completed", message="done", artist="Radiohead")

        scans = [
            s for s in get_recent_album_scans(limit=10)
            if s.get("scan_type") == "artist" and s.get("artist") == "Radiohead"
        ]
        assert len(scans) == 1
        assert scans[0]["status"] == "completed"
        assert scans[0]["completed_at"] is not None
