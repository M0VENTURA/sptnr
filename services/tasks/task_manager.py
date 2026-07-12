"""Central async task manager.

Provides a simple in-memory async task execution system with status
 tracking. Tasks run in background threads and their status can be
 polled via ``get_task()``.

Key Functions:
    - run_async_task(): Submit a function to run in a background thread.
    - get_task(): Get current status of a previously submitted task.
    - set_task(): Update task metadata (status, result, timestamps).

Architecture:
    Uses a thread-safe dict for task state storage. Task results and
    status are available for polling. Not persisted across restarts.
"""
from __future__ import annotations
import threading
import uuid
import logging
from datetime import datetime
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)

_TASKS: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.Lock()

def set_task(task_id: str, **fields):
    with _LOCK:
        task = _TASKS.setdefault(task_id, {"task_id": task_id})
        task.update(fields)
        return dict(task)


def get_task(task_id: str) -> Dict[str, Any] | None:
    with _LOCK:
        task = _TASKS.get(task_id)
        return dict(task) if task else None


def run_async_task(name: str, func: Callable, *args, task_id: str | None = None, **kwargs) -> str:
    tid = task_id or str(uuid.uuid4())
    set_task(tid, status="running", name=name, started_at=datetime.utcnow().isoformat())

    def _worker():
        try:
            result = func(*args, **kwargs)
            set_task(tid, status="completed", success=True, result=result, completed_at=datetime.utcnow().isoformat())
        except Exception as e:
            logger.exception("Async task %s failed", name)
            set_task(tid, status="failed", success=False, error=str(e), completed_at=datetime.utcnow().isoformat())

    threading.Thread(target=_worker, daemon=True, name=f"{name}-{tid[:8]}").start()
    return tid