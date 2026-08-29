"""Regression tests for full scans continuing through the whole library.

Covers the "full scan stops at the first letter group" symptom:

- A scheduled popularity scan must never overlap a manually started scan —
  both write to the SAME ``popularity_scan`` progress row, so whichever
  finishes first flips the shared state to complete while the other is still
  running, which makes a full scan look like it halted mid-letter.
- ``run_full_library_scan`` must clear a stale resume checkpoint (artist no
  longer in the index) instead of silently skipping every artist.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text

import db.engine as db_engine_mod


@pytest.fixture(autouse=True)
def _clean_runtime():
    from services.scanning.runtime_state import clear_runtime

    clear_runtime("popularity")
    yield
    clear_runtime("popularity")


def _fresh_db() -> None:
    """Swap the singleton engine for a fresh in-memory SQLite engine."""
    engine = create_engine("sqlite:///:memory:")
    db_engine_mod._ENGINE = engine
    db_engine_mod._SESSION_FACTORY = None
    with db_engine_mod.db_session() as session:
        session.execute(text("""
            CREATE TABLE scan_states (
                scan_type TEXT PRIMARY KEY,
                is_running BOOLEAN,
                status TEXT,
                stop_requested BOOLEAN,
                current_artist TEXT,
                last_scanned_artist TEXT,
                extra_data TEXT,
                updated_at TEXT
            )
        """))


def _set_scan_running(scan_type: str = "popularity_scan", running: bool = True) -> None:
    with db_engine_mod.db_session() as session:
        session.execute(text("""
            INSERT INTO scan_states (scan_type, is_running, status)
            VALUES (:t, :r, :s)
            ON CONFLICT(scan_type) DO UPDATE SET
                is_running = :r, status = :s
        """), {"t": scan_type, "r": running, "s": "running" if running else "complete"})


class TestIsPopularityScanActive:
    def test_true_when_shared_state_running(self):
        _fresh_db()
        _set_scan_running("popularity_scan", running=True)

        from services.scanning.pipelines.popularity_pipeline import is_popularity_scan_active

        assert is_popularity_scan_active() is True

    def test_true_when_full_scan_shared_state_running(self):
        # The dashboard "All" scan runs under the "full_scan" progress row;
        # the guard must treat it as an active popularity-family scan too.
        _fresh_db()
        _set_scan_running("full_scan", running=True)

        from services.scanning.pipelines.popularity_pipeline import is_popularity_scan_active

        assert is_popularity_scan_active() is True

    def test_false_when_shared_state_complete(self):
        _fresh_db()
        _set_scan_running("popularity_scan", running=False)

        from services.scanning.pipelines.popularity_pipeline import is_popularity_scan_active

        assert is_popularity_scan_active() is False

    def test_false_when_no_state(self):
        _fresh_db()

        from services.scanning.pipelines.popularity_pipeline import is_popularity_scan_active

        assert is_popularity_scan_active() is False

    def test_true_when_runtime_running(self):
        _fresh_db()
        from services.scanning.runtime_state import set_runtime

        set_runtime("popularity", {"thread": threading.current_thread(), "type": "test"})

        from services.scanning.pipelines.popularity_pipeline import is_popularity_scan_active

        assert is_popularity_scan_active() is True


class TestScheduledPopularityScanGuard:
    def test_skips_when_scan_active(self):
        _fresh_db()
        _set_scan_running("popularity_scan", running=True)

        from services.scheduler import scheduler_service as sched
        from services.scanning.pipelines import popularity_pipeline as pipeline

        with (
            patch.object(pipeline, "run_popularity_mode", new=MagicMock()) as run_mock,
            patch.object(pipeline, "is_popularity_scan_active", return_value=True),
        ):
            sched._run_scheduled_popularity_scan()
            run_mock.assert_not_called()

    def test_runs_when_idle(self):
        _fresh_db()

        from services.scheduler import scheduler_service as sched
        from services.scanning.pipelines import popularity_pipeline as pipeline

        with patch.object(pipeline, "run_popularity_mode", new=MagicMock()) as run_mock:
            sched._run_scheduled_popularity_scan()
            run_mock.assert_called_once()

    def test_clears_runtime_after_run(self):
        _fresh_db()

        from services.scheduler import scheduler_service as sched
        from services.scanning.runtime_state import is_runtime_running
        from services.scanning.pipelines import popularity_pipeline as pipeline

        with patch.object(pipeline, "run_popularity_mode", new=MagicMock()):
            sched._run_scheduled_popularity_scan()

        assert is_runtime_running("popularity") is False


class TestValidatedResumeArtist:
    def test_stale_checkpoint_cleared(self):
        _fresh_db()
        # Persist a stale checkpoint artist that is NOT in the index.
        with db_engine_mod.db_session() as session:
            session.execute(text("""
                INSERT INTO scan_states (scan_type, is_running, status, last_scanned_artist)
                VALUES ('library', FALSE, 'idle', 'Gone Artist')
            """))

        from services.scanning.pipeline import _validated_resume_artist

        artists = [("A Day to Remember", {"id": "1"}), ("Muse", {"id": "2"})]
        resume = _validated_resume_artist(artists, "library", force=False)

        assert resume is None
        with db_engine_mod.db_session() as session:
            row = session.execute(
                text("SELECT last_scanned_artist FROM scan_states WHERE scan_type = 'library'")
            ).fetchone()
            assert row[0] is None

    def test_valid_checkpoint_kept(self):
        _fresh_db()
        with db_engine_mod.db_session() as session:
            session.execute(text("""
                INSERT INTO scan_states (scan_type, is_running, status, last_scanned_artist)
                VALUES ('library', FALSE, 'idle', 'Muse')
            """))

        from services.scanning.pipeline import _validated_resume_artist

        artists = [("A Day to Remember", {"id": "1"}), ("Muse", {"id": "2"})]
        resume = _validated_resume_artist(artists, "library", force=False)

        assert resume == "Muse"

    def test_force_resumes_from_checkpoint(self):
        """A FORCED scan (no restart) resumes from the last checkpoint in
        forced mode — it must NOT clear/ignore the resume point (previously
        force cleared the checkpoint and always restarted from the top)."""
        _fresh_db()
        with db_engine_mod.db_session() as session:
            session.execute(text("""
                INSERT INTO scan_states (scan_type, is_running, status, last_scanned_artist)
                VALUES ('library', FALSE, 'idle', 'Muse')
            """))

        from services.scanning.pipeline import _validated_resume_artist

        artists = [("A Day to Remember", {"id": "1"}), ("Muse", {"id": "2"})]
        resume = _validated_resume_artist(artists, "library", force=True)

        # Forced resumes from the checkpoint — only RESTART goes to the top.
        assert resume == "Muse"

    def test_restart_ignores_checkpoint(self):
        """A RESTART (with or without force) always starts from the top —
        the checkpoint is ignored so the whole library is revisited."""
        _fresh_db()
        with db_engine_mod.db_session() as session:
            session.execute(text("""
                INSERT INTO scan_states (scan_type, is_running, status, last_scanned_artist)
                VALUES ('library', FALSE, 'idle', 'Muse')
            """))

        from services.scanning.pipeline import _validated_resume_artist

        artists = [("A Day to Remember", {"id": "1"}), ("Muse", {"id": "2"})]
        assert _validated_resume_artist(artists, "library", force=False, restart=True) is None
        assert _validated_resume_artist(artists, "library", force=True, restart=True) is None

    def test_stale_checkpoint_cleared_on_forced_resume(self):
        """A stale checkpoint (artist gone) is cleared even in forced-resume
        mode so the forced scan does not stall in skip mode."""
        _fresh_db()
        with db_engine_mod.db_session() as session:
            session.execute(text("""
                INSERT INTO scan_states (scan_type, is_running, status, last_scanned_artist)
                VALUES ('library', FALSE, 'idle', 'Gone Artist')
            """))

        from services.scanning.pipeline import _validated_resume_artist

        artists = [("A Day to Remember", {"id": "1"}), ("Muse", {"id": "2"})]
        resume = _validated_resume_artist(artists, "library", force=True)

        assert resume is None
        with db_engine_mod.db_session() as session:
            row = session.execute(
                text("SELECT last_scanned_artist FROM scan_states WHERE scan_type = 'library'")
            ).fetchone()
            assert row[0] is None
