"""Tests for the queue advisory-lock transaction hygiene.

Symptom: Postgres logged ``unexpected EOF on client connection with an open
transaction`` during queue processing.  ``_pg_advisory_lock`` held a session
in an open transaction for the ENTIRE queue batch (Soulseek searches,
MusicBrainz calls, filesystem moves — often > 60 s), and
``idle_in_transaction_session_timeout`` (default 60 s) killed the
connection, dropping the lock and the work under it.

PostgreSQL advisory locks are SESSION-scoped (they survive COMMIT), so the
fix is to COMMIT after acquiring (and after unlocking) — the lock stays held
but the session is no longer idle-in-transaction.
"""

from __future__ import annotations

import pytest


class _FakeLockSession:
    """A session double that records every executed statement and commit."""

    def __init__(self):
        self.statements = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def execute(self, stmt, params=None):
        self.statements.append(str(stmt))
        if "pg_try_advisory_lock" in str(stmt):
            return _FakeResult(True)
        return _FakeResult(None)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class _FakeResult:
    def __init__(self, scalar_value):
        self._scalar = scalar_value

    def scalar(self):
        return self._scalar


def test_advisory_lock_commits_after_acquire(monkeypatch):
    """The lock session must be committed after acquiring the advisory lock —
    the session-scoped lock survives the commit but the connection is no
    longer idle-in-transaction (the EOF / timeout fix)."""
    from services.queue import queue_lock as ql

    session = _FakeLockSession()
    calls = {"factory": 0}

    def _fake_factory():
        calls["factory"] += 1
        # get_session_factory() returns a sessionmaker; the lock calls it ()
        # to get a session.
        return lambda: session

    # The function imports get_session_factory from db.engine inside its body.
    monkeypatch.setattr("db.engine.get_session_factory", _fake_factory)
    monkeypatch.setattr(ql, "_using_postgres", lambda: True)

    with ql.queue_cycle_lock(key="test-key", max_attempts=1, attempt_interval=0) as acquired:
        assert acquired is True
        # The acquire SELECT happened and the session was committed.
        assert any("pg_try_advisory_lock" in s for s in session.statements)
        assert session.commits >= 1

    # After release: unlock executed AND committed, then the session closed.
    assert any("pg_advisory_unlock" in s for s in session.statements)
    assert session.commits >= 2
    assert session.closed is True


def test_advisory_lock_not_acquired_skips_commit(monkeypatch):
    """When the lock is not acquired, no unlock and no extra commit happen —
    the session is just closed."""
    from services.queue import queue_lock as ql

    class _BusySession(_FakeLockSession):
        def execute(self, stmt, params=None):
            self.statements.append(str(stmt))
            return _FakeResult(False)  # lock busy

    session = _BusySession()

    monkeypatch.setattr("db.engine.get_session_factory", lambda: (lambda: session))
    monkeypatch.setattr(ql, "_using_postgres", lambda: True)

    with ql.queue_cycle_lock(key="test-key", max_attempts=1, attempt_interval=0) as acquired:
        assert acquired is False

    assert session.commits == 0
    assert not any("pg_advisory_unlock" in s for s in session.statements)
    assert session.closed is True
