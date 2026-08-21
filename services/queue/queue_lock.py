"""Cross-process mutual exclusion for the download queue cycle.

The standalone ``queue_worker`` process and the APScheduler
``download_queue_processor`` job both dispatch queue items independently.
Without serialisation they can claim and process the same items
concurrently, overlapping DB writes and filesystem moves.

This module provides ``queue_cycle_lock()`` — a best-effort lock that
yields ``True`` when this process acquired the lock and may proceed, or
``False`` when another worker/scheduler cycle already holds it (the caller
should skip that cycle instead of piling up behind it).

Backends:
    - PostgreSQL (primary): session-scoped advisory lock
      (``pg_try_advisory_lock``). Lock ownership is tied to the holding
      DB connection, so a killed worker releases it automatically.
    - File lock fallback (non-PostgreSQL engine URLs only — e.g. explicit
      test overrides): ``fcntl.flock`` on a lock file under the temp dir.

Usage::

    from services.queue.queue_lock import queue_cycle_lock

    with queue_cycle_lock() as acquired:
        if not acquired:
            return _ok(skipped=True, reason="Another queue cycle is running")
        ...process batch...
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import text

logger = logging.getLogger(__name__)

_LOCK_KEY = "popularr_queue_cycle"


def _using_postgres() -> bool:
    try:
        from db.engine import get_engine
        return str(get_engine().url).startswith("postgresql")
    except Exception:
        return False


@contextmanager
def _pg_advisory_lock(
    key: str,
    max_attempts: int,
    interval: float,
) -> Iterator[bool]:
    """Hold a PostgreSQL advisory lock on a dedicated connection.

    The lock session must stay open for the whole critical section, so the
    session is acquired, locked and kept alive until release — never returned
    to the pool mid-lock (a pooled connection would otherwise keep the lock
    while being reused by unrelated code).

    CRITICAL: the session is COMMITTED after acquiring (and after releasing)
    so the transaction is never left idle-in-transaction.  PostgreSQL advisory
    locks (``pg_try_advisory_lock``) are SESSION-scoped, not transaction-
    scoped — committing the SELECT does NOT release the lock, but it DOES stop
    ``idle_in_transaction_session_timeout`` (default 60 s) from killing the
    connection mid-batch.  The queue batch can run for minutes (Soulseek
    searches, MusicBrainz calls, filesystem moves); without the commit the
    server logs "unexpected EOF on client connection with an open transaction"
    and the lock (and any work under it) is lost.
    """
    from db.engine import get_session_factory

    session = None
    acquired = False
    for _ in range(max(1, max_attempts)):
        try:
            session = get_session_factory()()
            result = session.execute(
                text("SELECT pg_try_advisory_lock(hashtext(:key))"),
                {"key": key},
            )
            if bool(result.scalar()):
                acquired = True
                # Session-scoped lock survives the commit; ending the
                # transaction here keeps the session out of the idle-in-
                # transaction danger zone for the whole critical section.
                try:
                    session.commit()
                except Exception:
                    session.rollback()
                break
        except Exception as exc:
            logger.debug("[QUEUE_LOCK] advisory lock error: %s", exc)
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
                session = None
            break
        session.close()
        session = None
        time.sleep(interval)

    try:
        yield acquired
    finally:
        if acquired and session is not None:
            try:
                session.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:key))"),
                    {"key": key},
                )
                # Commit the unlock too — same idle-in-transaction reasoning.
                try:
                    session.commit()
                except Exception:
                    session.rollback()
            except Exception as exc:
                logger.debug("[QUEUE_LOCK] advisory unlock error: %s", exc)
            try:
                session.close()
            except Exception:
                pass


@contextmanager
def _file_lock(
    key: str,
    max_attempts: int,
    interval: float,
) -> Iterator[bool]:
    """Hold an exclusive ``flock`` on a temp lock file (SQLite fallback)."""
    import fcntl

    path = os.path.join(tempfile.gettempdir(), f"popularr_{key}.lock")
    fd: int | None = None
    acquired = False
    for _ in range(max(1, max_attempts)):
        try:
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            break
        except OSError:
            if fd is not None:
                os.close(fd)
                fd = None
            time.sleep(interval)

    try:
        yield acquired
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                os.close(fd)
            except Exception:
                pass


@contextmanager
def queue_cycle_lock(
    key: str = _LOCK_KEY,
    max_attempts: int = 20,
    attempt_interval: float = 0.5,
) -> Iterator[bool]:
    """Best-effort cross-process mutual exclusion for queue cycles.

    Yields ``True`` when this process acquired the lock and may proceed;
    ``False`` when another worker/scheduler cycle already holds it.
    """
    if _using_postgres():
        with _pg_advisory_lock(key, max_attempts, attempt_interval) as acquired:
            yield acquired
        return
    with _file_lock(key, max_attempts, attempt_interval) as acquired:
        yield acquired
