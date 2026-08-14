"""Regression tests: download/search failures never park in ``failed``.

Product rule (per user): when a track does not download correctly it must go
back to a *pending* state and automatically return to the queue — it must
never be left in the terminal ``failed`` status.

Covered here:
- ``mark_failed`` always returns the item to ``queued`` with a future
  ``next_retry_at`` (the executed UPDATE never targets ``'failed'``).
- ``_schedule_search_retry`` keeps backing off (``backed_off``) even after
  many search misses instead of abandoning the item to ``failed``.
"""

from __future__ import annotations

from services.downloads import download_pipeline_service as dps


# ---------------------------------------------------------------------------
# mark_failed — never the terminal 'failed' status
# ---------------------------------------------------------------------------

def test_mark_failed_sql_never_parks_as_failed(monkeypatch):
    """mark_failed must return the item to 'queued' (pending) — the executed
    UPDATE must never set the terminal 'failed' status, regardless of how
    many times the item has already failed."""
    from db.repositories.queue import mark_failed

    executed = []

    class _FakeResult:
        def fetchone(self):
            return None

    class _RecordingSession:
        def execute(self, statement, params=None):
            executed.append((str(statement), dict(params or {})))
            return _FakeResult()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        "db.repositories.queue.db_session",
        lambda *a, **kw: _RecordingSession(),
    )

    result = mark_failed(7, "peer_no_free_slots")

    assert result == {"success": True, "id": 7}
    assert executed, "mark_failed must execute an UPDATE"
    sql, params = executed[0]
    # The failure parks the item back in the queue (pending), never failed.
    assert "SET status = 'queued'" in sql
    assert "'failed'" not in sql
    assert params["qid"] == 7
    assert params["reason"] == "peer_no_free_slots"
    # A retry window is always scheduled so the worker re-picks the item.
    assert "next_retry_at" in sql


def test_get_active_queue_includes_pending_retry_statuses(monkeypatch):
    """backed_off / pending_release are PENDING (waiting to return to the
    queue) — they must be included in the active queue listing so the UI can
    show them while their retry window passes."""
    from db.repositories.queue import get_active_queue

    executed = []

    class _FakeResult:
        def fetchall(self):
            return []

    class _RecordingSession:
        def execute(self, statement, params=None):
            executed.append((str(statement), dict(params or {})))
            return _FakeResult()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        "db.repositories.queue.db_session",
        lambda *a, **kw: _RecordingSession(),
    )

    get_active_queue(limit=10)

    assert executed, "get_active_queue must execute a SELECT"
    sql = executed[0][0]
    assert "'backed_off'" in sql
    assert "'pending_release'" in sql


# ---------------------------------------------------------------------------
# _schedule_search_retry — never abandons to 'failed'
# ---------------------------------------------------------------------------

def test_schedule_search_retry_keeps_backing_off_never_failed(monkeypatch):
    """Even after many search misses, _schedule_search_retry parks the item in
    'backed_off' (auto-return to the queue) — never 'failed'."""
    scheduled = {}

    monkeypatch.setattr(
        "db.repositories.queue.schedule_queue_retry",
        lambda qid, status, next_retry_at, reason="": scheduled.update(
            {
                "qid": qid,
                "status": status,
                "next_retry_at": next_retry_at,
                "reason": reason,
            }
        ),
    )
    monkeypatch.setattr(dps, "_resolve_item_release_date", lambda item: None)
    monkeypatch.setattr(dps, "log_unified", lambda *a, **k: None)
    monkeypatch.setattr(dps, "_log_queue_event", lambda *a, **k: None)

    item = {"artist": "A", "title": "T", "retry_count": 50}
    dps._schedule_search_retry(7, item, "no_results")

    assert scheduled.get("status") == "backed_off"
    assert scheduled.get("qid") == 7
    assert "backing off" in scheduled.get("reason", "")
