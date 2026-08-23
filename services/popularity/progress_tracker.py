"""In-memory popularity scan progress tracker.

Tracks scan state (running/stopped, stage, progress %) for the
WebUI dashboard. Thread-safe via a single lock.

Not persisted — progress resets on process restart.
"""

from __future__ import annotations

import threading
import time
from typing import Any, TypedDict


class ProgressState(TypedDict, total=False):
    """Shape of the in-memory scan progress state."""

    running: bool
    started_at: float | None
    finished_at: float | None
    current_stage: str | None
    progress: int
    message: str
    current_item: str | None
    total_items: int | None
    processed_items: int | None


_lock = threading.Lock()

_state: ProgressState = {
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


def start(total_items: int | None = None) -> None:
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


def update(
    stage: str | None = None,
    progress: int | None = None,
    message: str | None = None,
    current_item: str | None = None,
    processed: int | None = None,
    total_items: int | None = None,
) -> None:
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


def finish(success: bool = True) -> None:
    with _lock:
        _state["running"] = False
        _state["finished_at"] = time.time()
        _state["progress"] = 100 if success else _state["progress"]
        _state["message"] = "Completed" if success else "Failed"


def get_state() -> dict[str, Any]:
    """Return a snapshot of the current scan state."""
    with _lock:
        return dict(_state)