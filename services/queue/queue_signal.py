"""Event-driven queue notification signal.

Replaces the 30-second polling loop with an instant wake-up mechanism.
When a new download is queued via the WebUI/API, ``signal_new_item()``
wakes the background worker immediately so it can start processing
without waiting for the next scheduler tick.

Architecture:
    Uses ``threading.Event`` for cross-thread signalling within the
    same process.  The scheduler's queue-processor job calls
    ``wait_for_item(timeout=30)`` which blocks until either:
        a) A new item is queued (returns True), or
        b) The timeout expires (returns False — periodic safety net).

    The API/route layer calls ``signal_new_item()`` when inserting
    into the download queue, giving the user sub-second perceived latency.

Thread Safety:
    ``threading.Event`` is inherently thread-safe and works correctly
    across multiple producer threads (concurrent API requests).
"""

from __future__ import annotations

import logging
import threading
from typing import Final

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Singleton event
# ---------------------------------------------------------------------------

newItem_event: Final[threading.Event] = threading.Event()

# How many items were signalled since last processed (for batching)
_signal_count: int = 0
_signal_lock: threading.Lock = threading.Lock()


def signal_new_item(count: int = 1) -> None:
    """Wake the background worker — a new download has been queued.

    Safe to call from any thread or async context.  Multiple rapid
    signals are coalesced into a single wake-up (the Event is set,
    not incremented).
    """
    global _signal_count
    with _signal_lock:
        _signal_count += count
    newItem_event.set()
    logger.debug("[QUEUE_SIGNAL] New item signalled (pending=%s)", _signal_count)


def wait_for_item(timeout: float = 30.0) -> bool:
    """Block until a new item arrives or timeout expires.

    Returns:
        True  — a new item was signalled (caller should process).
        False — timeout expired (periodic safety-net poll).
    """
    woke = newItem_event.wait(timeout=timeout)
    if woke:
        newItem_event.clear()
    return woke


def drain_signal_count() -> int:
    """Return and reset the number of signalled items since last drain.

    Useful for the worker to know how many items to expect in a batch.
    """
    global _signal_count
    with _signal_lock:
        count = _signal_count
        _signal_count = 0
    return count
