"""Tests for the queue advisory-lock connection hygiene.

Two regressions addressed:

1. ``unexpected EOF on client connection with an open transaction`` — the
   old lock held a session in an open transaction for the ENTIRE queue batch
   (Soulseek searches, MusicBrainz calls, filesystem moves — often > 60 s),
   and ``idle_in_transaction_session_timeout`` (default 60 s) killed the
   connection, dropping the lock and the work under it.  The lock now COMMITS
   after acquiring (and after unlocking) — the session-scoped lock survives
   the commit but the connection is no longer idle-in-transaction.

2. ``you don't own a lock of type ExclusiveLock`` — the first fix used a
   POOLED SQLAlchemy session; after the commit the session could be returned
   to the pool / recycled, so the unlock ran on a DIFFERENT connection than
   the lock.  The lock now uses a DEDICATED raw psycopg2 connection
   (``db.utils.get_db_connection``), so lock and unlock always hit the SAME
   connection.
"""

from __future__ import annotations

import pytest


class _FakeCursor:
    """A cursor double recording executed SQL and returning a fake row."""

    def __init__(self, acquired_value):
        self._acquired = acquired_value
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append(str(sql))

    def fetchone(self):
        return {"acquired": self._acquired}


class _FakeConnection:
    """A raw-connection double mirroring AutoRollbackPGConnection's surface."""

    def __init__(self, acquired_value):
        self.acquired_value = acquired_value
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.cursor_obj = None

    def cursor(self):
        self.cursor_obj = _FakeCursor(self.acquired_value)
        return self.cursor_obj

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True

    def statements(self):
        return self.cursor_obj.statements if self.cursor_obj else []


def test_advisory_lock_commits_after_acquire(monkeypatch):
    """The lock connection must be committed after acquiring the advisory
    lock — the session-scoped lock survives the commit but the connection is
    no longer idle-in-transaction (the EOF / timeout fix)."""
    from services.queue import queue_lock as ql

    conn = _FakeConnection(True)
    monkeypatch.setattr("db.utils.get_db_connection_raw", lambda **kw: conn)
    monkeypatch.setattr(ql, "_using_postgres", lambda: True)

    with ql.queue_cycle_lock(key="test-key", max_attempts=1, attempt_interval=0) as acquired:
        assert acquired is True
        # The acquire SELECT happened and the connection was committed.
        assert any("pg_try_advisory_lock" in s for s in conn.statements())
        assert conn.commits >= 1

    # After release: unlock executed on the SAME connection AND committed,
    # then the connection closed.
    assert any("pg_advisory_unlock" in s for s in conn.statements())
    assert conn.commits >= 2
    assert conn.closed is True


def test_advisory_lock_not_acquired_skips_commit(monkeypatch):
    """When the lock is not acquired, no unlock and no extra commit happen —
    the connection is just closed."""
    from services.queue import queue_lock as ql

    conn = _FakeConnection(False)
    monkeypatch.setattr("db.utils.get_db_connection_raw", lambda **kw: conn)
    monkeypatch.setattr(ql, "_using_postgres", lambda: True)

    with ql.queue_cycle_lock(key="test-key", max_attempts=1, attempt_interval=0) as acquired:
        assert acquired is False

    assert conn.commits == 0
    assert not any("pg_advisory_unlock" in s for s in conn.statements())
    assert conn.closed is True
