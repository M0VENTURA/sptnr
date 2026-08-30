"""Tests: remote Navidrome sync consolidation.

The reported issue: automatic remote ``startScan`` triggers fired from every
file-tag write, pausing the server and locking the database.  The fix removes
those auto-triggers and consolidates to ONE sync-and-wait before the full
Navidrome import.

Verified here:
1. ``trigger_and_wait_for_scan`` calls startScan, polls getScanStatus until
   ``scanning`` is False, and returns True.
2. It times out (returns False) when Navidrome keeps scanning past the
   deadline, and never spins forever.
3. The per-tag-write trigger helpers are now no-ops (``_trigger_navidrome_scan``
   / ``_trigger_scan_after_tag_write`` return True without firing a scan).
"""

from __future__ import annotations


class _FakeNavidromeClient:
    def __init__(self, scan_states: list[bool]):
        # Each get_scan_status call pops the next "scanning" value; the last
        # one repeats.
        self._states = scan_states
        self.started = 0
        self.status_calls = 0

    def start_scan(self) -> bool:
        self.started += 1
        return True

    def get_scan_status(self) -> dict:
        self.status_calls += 1
        scanning = self._states[min(self.status_calls - 1, len(self._states) - 1)]
        return {"success": True, "scanning": scanning, "count": 1420}


class TestTriggerAndWaitForScan:
    def test_waits_until_scanning_false(self, monkeypatch):
        from api_clients.navidrome import NavidromeClient

        client = _FakeNavidromeClient([True, True, False])

        # Speed up the poll interval so the test doesn't sleep 5s per poll.
        monkeypatch.setattr("time.sleep", lambda s: None)

        ok = NavidromeClient.trigger_and_wait_for_scan(
            client,
            poll_interval_seconds=0.01,
            max_wait_seconds=10,
        )
        assert ok is True
        assert client.started == 1
        assert client.status_calls >= 3  # polled until scanning became False

    def test_times_out_when_never_finishes(self, monkeypatch):
        from api_clients.navidrome import NavidromeClient

        client = _FakeNavidromeClient([True])  # always scanning
        monkeypatch.setattr("time.sleep", lambda s: None)

        ok = NavidromeClient.trigger_and_wait_for_scan(
            client,
            poll_interval_seconds=0.01,
            max_wait_seconds=0.05,
        )
        assert ok is False
        assert client.started == 1
        assert client.status_calls > 1

    def test_start_scan_failure_returns_false(self, monkeypatch):
        from api_clients.navidrome import NavidromeClient

        client = _FakeNavidromeClient([False])
        client.start_scan = lambda: False

        ok = NavidromeClient.trigger_and_wait_for_scan(
            client,
            poll_interval_seconds=0.01,
            max_wait_seconds=10,
        )
        assert ok is False
        assert client.status_calls == 0  # never polled


class TestAutoTriggersRemoved:
    def test_track_route_trigger_is_noop(self):
        """The per-tag-write remote sync must no longer fire a scan."""
        from routes.track_routes import _trigger_navidrome_scan

        assert _trigger_navidrome_scan() is True

    def test_misc_route_trigger_is_noop(self):
        """The genre/tag-write remote sync must no longer fire a scan."""
        from routes.misc_routes import _trigger_scan_after_tag_write

        assert _trigger_scan_after_tag_write() is True
