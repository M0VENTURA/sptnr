"""Regression tests for stale scan-state / stop-flag reset.

Covers:
- ``reset_stale_scan_states`` must clear ``stop_requested`` on NON-running
  rows too — a stop flag left behind by a previous session (a "Stop" click,
  a crashed worker, a reboot) must not abort the next scan the instant it
  starts (the artist pipeline saw "Stop requested — import halted" right
  after a reboot because the reset only touched ``is_running`` rows).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from db.engine import db_session


@pytest.fixture(autouse=True)
def _scan_states_table():
    """Ensure the scan_states table exists (conftest only creates tracks).

    Uses raw SQL — the ``ScanState`` ORM model declares a JSONB column which
    the SQLite dialect cannot render in ``create()``.
    """
    from db.engine import get_engine

    with get_engine().connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS scan_states (
                scan_type VARCHAR PRIMARY KEY,
                is_running BOOLEAN DEFAULT FALSE,
                status VARCHAR DEFAULT 'idle',
                stop_requested BOOLEAN DEFAULT FALSE,
                current_artist VARCHAR,
                last_scanned_artist VARCHAR,
                extra_data TEXT DEFAULT '{}',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.commit()
    yield


def _scan_states() -> dict[str, tuple[bool, bool, str]]:
    """Return {scan_type: (is_running, stop_requested, status)}."""
    with db_session() as session:
        rows = session.execute(
            text("SELECT scan_type, is_running, stop_requested, status FROM scan_states")
        ).fetchall()
        return {r[0]: (bool(r[1]), bool(r[2]), r[3]) for r in rows}


def _seed_state(scan_type: str, is_running: bool, stop_requested: bool, status: str) -> None:
    with db_session() as session:
        session.execute(
            text("""
                INSERT INTO scan_states (scan_type, is_running, stop_requested, status)
                VALUES (:scan_type, :is_running, :stop_requested, :status)
                ON CONFLICT (scan_type) DO UPDATE SET
                    is_running = :is_running,
                    stop_requested = :stop_requested,
                    status = :status
            """),
            {"scan_type": scan_type, "is_running": is_running, "stop_requested": stop_requested, "status": status},
        )


class TestResetStaleScanStates:
    def test_clears_stop_flag_on_non_running_rows(self):
        """A stopped row with stop_requested=True must be cleared on reset."""
        from services.scanning.scan_state import reset_stale_scan_states

        _seed_state("navidrome_scan", is_running=False, stop_requested=True, status="stop_requested")
        _seed_state("popularity_scan", is_running=True, stop_requested=False, status="running")

        reset_stale_scan_states()

        states = _scan_states()
        # The non-running stale-stop row gets its flag cleared.
        nav_running, nav_stop, nav_status = states["navidrome_scan"]
        assert nav_running is False
        assert nav_stop is False
        assert nav_status == "idle"
        # The interrupted running row is also cleared.
        pop_running, pop_stop, pop_status = states["popularity_scan"]
        assert pop_running is False
        assert pop_stop is False

    def test_returns_count_of_interrupted_rows(self):
        """The return value counts only the is_running rows (interrupted)."""
        from services.scanning.scan_state import reset_stale_scan_states

        _seed_state("navidrome_scan", is_running=False, stop_requested=True, status="stop_requested")
        _seed_state("full_scan", is_running=True, stop_requested=False, status="running")
        _seed_state("library_scan", is_running=True, stop_requested=False, status="running")

        assert reset_stale_scan_states() == 2
