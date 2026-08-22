"""In-process scan runtime state.

Per-worker runtime state for scan operations. Since Flask workers do not
share Python memory, this state is only valid within a single worker.

Limitations:
    - These globals are per-process, not shared across workers.
    - Used for avoiding duplicate thread starts within one worker.
    - Provides lightweight UI status for the current process.

Cross-Process Source of Truth:
    Progress files from ``scan_state.py`` are the reliable cross-process
    state mechanism, suitable for multi-worker deployments.
"""

from __future__ import annotations

import threading
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

scan_lock = threading.RLock()


# -------------------------------------------------------------------------
# Legacy globals retained for compatibility during route migration
# -------------------------------------------------------------------------

scan_process: Any = None
scan_process_navidrome: Any = None
scan_process_popularity: Any = None
scan_process_singles: Any = None
scan_process_essentia_mood: Any = None
scan_process_combined: Any = None
scan_process_missing_releases: Any = None
scan_process_mp3_import: Any = None


# -------------------------------------------------------------------------
# Preferred registry API
# -------------------------------------------------------------------------

_runtime: dict[str, Any] = {
    "library": None,
    "navidrome": None,
    "popularity": None,
    "singles": None,
    "essentia_mood": None,
    "combined": None,
    "missing_releases": None,
    "mp3_import": None,
}


def set_runtime(name: str, value: Any) -> None:
    with scan_lock:
        _runtime[name] = value


def get_runtime(name: str) -> Any | None:
    with scan_lock:
        return _runtime.get(name)


def clear_runtime(name: str) -> None:
    with scan_lock:
        _runtime[name] = None


def is_runtime_running(name: str) -> bool:
    with scan_lock:
        obj = _runtime.get(name)

    return is_process_alive(obj)


def is_process_alive(obj: Any) -> bool:
    if obj is None:
        return False

    try:
        if isinstance(obj, dict):
            thread = obj.get("thread")
            if thread is None:
                return False
            if hasattr(thread, "is_alive"):
                return bool(thread.is_alive())
            if hasattr(thread, "poll"):
                return thread.poll() is None
            return False

        if hasattr(obj, "is_alive"):
            return bool(obj.is_alive())

        if hasattr(obj, "poll"):
            return obj.poll() is None

    except Exception as exc:
        logger.debug("Failed to check process/thread liveness", error=str(exc))
        return False

    return False
