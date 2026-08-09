"""Regression tests for transfer-failure handling.

Covers the peer-failure memory that stops retries from repeatedly picking the
same rejecting/unresponsive peer, and the diagnostic + peer-block behaviour of
``_reconcile_transfer_state`` for failed/success-but-unfound transfers.
"""

from __future__ import annotations

import time

import pytest

from services.downloads import download_pipeline_service as dps
from services.downloads import download_completion_service as dcs


# ---------------------------------------------------------------------------
# Peer failure memory (download_pipeline_service)
# ---------------------------------------------------------------------------

class TestPeerFailureMemory:
    def teardown_method(self):
        dps._blocked_peers.clear()

    def test_block_then_recognised(self):
        assert dps._is_peer_blocked("alice", "/Album/01 - Track.flac") is False
        dps._block_peer("alice", "/Album/01 - Track.flac")
        assert dps._is_peer_blocked("alice", "/Album/01 - Track.flac") is True

    def test_expired_block_is_forgotten(self):
        dps._block_peer("bob", "song.mp3")
        key = ("bob", "song.mp3")
        dps._blocked_peers[key] = time.time() - 1  # expired
        assert dps._is_peer_blocked("bob", "song.mp3") is False
        assert key not in dps._blocked_peers

    def test_filter_drops_blocked_peers_keeps_others(self):
        dps._block_peer("carol", "x.mp3")
        results = [
            {"username": "carol", "filename": "x.mp3"},
            {"username": "dave", "filename": "x.mp3"},
            {"username": "erin", "filename": "y.mp3"},
        ]
        filtered = dps._filter_blocked_peers(results)
        assert filtered == [
            {"username": "dave", "filename": "x.mp3"},
            {"username": "erin", "filename": "y.mp3"},
        ]

    def test_skip_peers_without_username_or_filename(self):
        dps._block_peer(None, "x.mp3")
        dps._block_peer("carol", None)
        assert dps._blocked_peers == {}


# ---------------------------------------------------------------------------
# _reconcile_transfer_state diagnostics + peer block
# ---------------------------------------------------------------------------

class _FakeSlskd:
    FAILED_STATES = frozenset(["Completed, Errored", "Completed, TimedOut"])
    ACTIVE_STATES = frozenset(["Requested", "Queued, Remotely", "InProgress"])
    STATE_QUEUED_REMOTELY = "Queued, Remotely"
    STATE_SUCCEEDED = "Completed, Succeeded"

    def __init__(self):
        self.cancelled = []

    @staticmethod
    def state_text(raw):
        return str(raw or "").strip()

    def is_success_state(self, raw):
        return self.state_text(raw) == self.STATE_SUCCEEDED

    def cancel_download(self, username, transfer_id, remove=True):
        self.cancelled.append((username, transfer_id, remove))
        return True


class TestReconcileFailedPeerBlock:
    def setup_method(self):
        dps._blocked_peers.clear()

    def test_failed_state_blocks_peer_and_marks_failed(self, monkeypatch):
        slskd = _FakeSlskd()
        marked = []
        monkeypatch.setattr(
            "db.repositories.queue.mark_failed",
            lambda qid, reason: marked.append((qid, reason)),
        )

        item = {"id": 15, "found_filename": "/Peer/Spice Girls - Greatest Hits.flac"}
        transfer = {
            "id": "t1",
            "username": "ishalioh",
            "filename": "/Peer/Spice Girls - Greatest Hits.flac",
            "state": "Completed, Errored",
        }

        result = dcs._reconcile_transfer_state(item, slskd, active=[transfer])
        assert result is True
        assert marked and marked[0][0] == 15
        assert "slskd transfer failed" in marked[0][1]
        # The pipeline is now told to avoid this peer+file on retry.
        assert dps._is_peer_blocked("ishalioh", "/Peer/Spice Girls - Greatest Hits.flac") is True

    def test_success_but_no_file_logs_paths_and_removes_transfer(self, monkeypatch, caplog):
        import logging
        slskd = _FakeSlskd()
        marked = []
        monkeypatch.setattr(
            "db.repositories.queue.mark_failed",
            lambda qid, reason: marked.append((qid, reason)),
        )
        monkeypatch.setattr(dcs, "_monitored_downloads_dir", lambda: "/downloads/Music")

        item = {"id": 15, "found_filename": "/Peer/file.mp3"}
        transfer = {
            "id": "t1",
            "username": "ishalioh",
            "filename": "/Peer/file.mp3",
            "localFilePath": "/slskd/downloads/file.mp3",
            "state": "Completed, Succeeded",
        }

        with caplog.at_level(logging.WARNING, logger="services.downloads.download_completion_service"):
            result = dcs._reconcile_transfer_state(item, slskd, active=[transfer])

        assert result is True
        assert marked and "local file not found" in marked[0][1]
        assert slskd.cancelled == [("ishalioh", "t1", True)]
        # The diagnostic message makes the path mismatch self-diagnosing.
        assert "/slskd/downloads/file.mp3" in caplog.text
        assert "/downloads/Music" in caplog.text
        # The peer is remembered so the retry picks a different peer.
        assert dps._is_peer_blocked("ishalioh", "/Peer/file.mp3") is True

    def test_remotely_queued_stale_blocks_peer(self, monkeypatch):
        slskd = _FakeSlskd()
        marked = []
        monkeypatch.setattr(
            "db.repositories.queue.mark_failed",
            lambda qid, reason: marked.append((qid, reason)),
        )

        item = {"id": 16, "found_filename": "/Peer/x.flac", "updated_at": "2026-08-06 01:00:00"}
        now = dcs._db_now_naive()
        # Force staleness by making updated_at far in the past.
        from datetime import timedelta
        past = now - timedelta(hours=2)
        item["updated_at"] = past.strftime("%Y-%m-%d %H:%M:%S")

        transfer = {
            "id": "t2",
            "username": "unresponsive",
            "filename": "/Peer/x.flac",
            "state": "Queued, Remotely",
        }

        result = dcs._reconcile_transfer_state(item, slskd, active=[transfer], now=now)
        assert result is True
        assert marked and "queued too long" in marked[0][1]
        assert dps._is_peer_blocked("unresponsive", "/Peer/x.flac") is True


# ---------------------------------------------------------------------------
# _monitored_downloads_dir diagnostic
# ---------------------------------------------------------------------------

class TestMonitoredDownloadsDir:
    def test_returns_resolved_downloads_dir(self, monkeypatch):
        monkeypatch.setattr(
            "services.downloads.download_scan_service.resolve_downloads_dir",
            lambda *a, **k: "/custom/downloads",
        )
        assert dcs._monitored_downloads_dir() == "/custom/downloads"
