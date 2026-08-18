"""Tests for the dashboard "All" scan startup fix.

The dashboard "All" option posts ``mode: "all"`` to ``/api/popularity/run``.
Previously the route recorded a "started" scan_history row BEFORE the
duplicate guards; a stale ``scan_states`` row (crashed scan, ``is_running``
stuck True) made the cross-process guard reject the start with 409 — the
orphaned "started" record rendered as "completed" on the dashboard, the
footer stayed idle, and nothing appeared in the logs (the worker never ran).

The fix:
1. Records "started" only AFTER both duplicate guards pass.
2. Self-heals STALE scan-state rows: when the guard trips but no live worker
   owns the row, the stale rows are cleared and the scan proceeds.
3. Logs worker exceptions so a silent daemon-thread death is diagnosable.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clear_runtime():
    from services.scanning.runtime_state import clear_runtime
    clear_runtime("popularity")
    yield
    clear_runtime("popularity")


class TestPopularityRunRouteStaleRowSelfHeal:
    async def test_stale_scan_state_row_is_cleared_and_scan_starts(self, app, client, monkeypatch):
        """A ``scan_states`` row flagged running with NO live worker is stale:
        the route must clear it and start the scan instead of rejecting."""
        from sqlalchemy import text
        from db.engine import db_session

        with db_session() as session:
            session.execute(text(
                "CREATE TABLE IF NOT EXISTS scan_states ("
                "scan_type TEXT PRIMARY KEY, is_running BOOLEAN, status TEXT, "
                "stop_requested BOOLEAN, current_artist TEXT, "
                "last_scanned_artist TEXT, extra_data TEXT, updated_at TEXT)"
            ))
            session.execute(text(
                "INSERT INTO scan_states (scan_type, is_running, status) "
                "VALUES ('full_scan', TRUE, 'running') "
                "ON CONFLICT(scan_type) DO UPDATE SET is_running = TRUE, status = 'running'"
            ))

        from routes.scan_routes import api as api_mod
        from services.scanning import runtime_state

        monkeypatch.setattr(api_mod, "is_runtime_running", lambda name: False)
        monkeypatch.setattr(
            runtime_state, "is_process_alive",
            lambda obj: False,  # no live thread owns the stale row
        )
        started = {"called": False}
        fake_run = MagicMock(side_effect=lambda mode="popularity", force_rescan=False: started.update({"called": True}))
        monkeypatch.setattr(api_mod, "run_popularity_mode", fake_run)

        resp = await client.post("/api/popularity/run", json={"mode": "all", "force": False})
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data.get("success") is True

        # The scan actually started (worker not blocked by the phantom row).
        assert started["called"] is True

        # The stale row was cleared.
        from services.scanning.scan_state import get_scan_progress_path, read_progress_file
        state = read_progress_file(get_scan_progress_path("full_scan"))
        assert not state.get("is_running")

    async def test_live_scan_still_rejected(self, app, client, monkeypatch):
        """A scan whose owning thread is ALIVE must still be rejected — the
        self-heal must not clear a genuinely running scan."""
        import threading

        from routes.scan_routes import api as api_mod
        from services.scanning import runtime_state

        monkeypatch.setattr(api_mod, "is_runtime_running", lambda name: False)
        monkeypatch.setattr(
            runtime_state, "is_process_alive",
            lambda obj: True,  # the thread is live
        )

        # Simulate an in-process live popularity worker.
        runtime_state.set_runtime("popularity", {"thread": threading.current_thread(), "type": "all"})

        fake_run = MagicMock()
        monkeypatch.setattr(api_mod, "run_popularity_mode", fake_run)

        resp = await client.post("/api/popularity/run", json={"mode": "all", "force": False})
        assert resp.status_code == 409
        fake_run.assert_not_called()
