"""Shared timeout executor for bounded API work.

Provides a shared ``ThreadPoolExecutor`` for running API calls with
timeout constraints. The pool size is configurable via config.yaml
under ``system.thread_pools.max_workers``.

Key Functions:
    - run_with_timeout(): Execute a callable with a hard timeout.
    - api_timeout(): Context manager for timeout-scoped API work.
"""

from __future__ import annotations

import atexit
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from contextlib import contextmanager
from typing import Any, Callable, Iterator, TypeVar

import structlog

from helpers.config_helpers import get_thread_pool_config

logger = structlog.get_logger(__name__)

_timeout_executor_lock = threading.Lock()
_pool_config = get_thread_pool_config()
_timeout_executor: ThreadPoolExecutor | None = ThreadPoolExecutor(
    max_workers=_pool_config.get("max_workers", 20),
    thread_name_prefix="api_timeout",
)
_interpreter_shutting_down = False

T = TypeVar("T")


class TimeoutError(Exception):
    """Raised when an operation exceeds its configured timeout limit."""
    pass


def cleanup_timeout_executor() -> None:
    global _timeout_executor, _interpreter_shutting_down
    _interpreter_shutting_down = True
    with _timeout_executor_lock:
        if _timeout_executor:
            _timeout_executor.shutdown(wait=False)
            _timeout_executor = None
            logger.debug("Timeout thread pool executor shut down.")


def ensure_timeout_executor() -> ThreadPoolExecutor | None:
    global _timeout_executor
    with _timeout_executor_lock:
        if _interpreter_shutting_down:
            return None
        if _timeout_executor is None or getattr(_timeout_executor, "_shutdown", False):
            _timeout_executor = ThreadPoolExecutor(max_workers=20, thread_name_prefix="api_timeout")
        return _timeout_executor


def run_with_timeout(func: Callable[..., T], timeout_seconds: float, error_message: str, *args: Any, **kwargs: Any) -> T:
    """Execute a callable with a hard timeout constraint via the thread pool."""
    executor = ensure_timeout_executor()
    if executor is None:
        raise TimeoutError(error_message)
        
    future = executor.submit(func, *args, **kwargs)
    try:
        return future.result(timeout=timeout_seconds)
    except FuturesTimeoutError as exc:
        future.cancel()
        logger.warning("Operation timed out", timeout_seconds=timeout_seconds, error_message=error_message)
        raise TimeoutError(error_message) from exc


@contextmanager
def api_timeout(seconds: float, error_message: str = "API call timed out") -> Iterator[None]:
    """Semantic context manager for timeout-scoped operations."""
    # Kept as a semantic context manager; use run_with_timeout for enforcement.
    yield


atexit.register(cleanup_timeout_executor)
