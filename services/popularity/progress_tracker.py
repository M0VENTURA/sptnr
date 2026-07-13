"""In-memory popularity scan progress tracker.

Tracks scan state (running/stopped, stage, progress %) for the
WebUI dashboard. Thread-safe via a single lock.

Not persisted — progress resets on process restart.
"""

from __future__ import annotations
import threading
import time

_lock = threading.Lock()

_state = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "current_stage": None,
    "progress": 0,
    "message": "",
    "current_item": None,
    "total_items": None,
    "processed_items": None,
}


def start(total_items: int | None = None):
    """Mark the popularity scan as started with optional total item count."""
    with _lock:
        _state.update({
            "running": True,
            "started_at": time.time(),
            "finished_at": None,
            "current_stage": "starting",
            "progress": 0,
            "message": "Starting scan...",
            "current_item": None,
            "total_items": total_items,
            "processed_items": 0,
        })


def update(stage=None, progress=None, message=None, current_item=None, processed=None, total_items=None):
    with _lock:
        if stage is not None:
            _state["current_stage"] = stage
        if progress is not None:
            _state["progress"] = max(0, min(100, int(progress)))
        if message is not None:
            _state["message"] = message
        if current_item is not None:
            _state["current_item"] = current_item
        if processed is not None:
            _state["processed_items"] = processed
        if total_items is not None:
            _state["total_items"] = total_items


def finish(success=True):
    with _lock:
        _state["running"] = False
        _state["finished_at"] = time.time()
        _state["progress"] = 100 if success else _state["progress"]
        _state["message"] = "Completed" if success else "Failed"


def get_state():
    """Return a snapshot of the current scan state."""
    with _lock:
        return dict(_state)