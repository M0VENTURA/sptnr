"""Shared timeout executor for bounded API work.

Provides a shared ``ThreadPoolExecutor`` for running API calls with
timeout constraints. The pool size is configurable via config.yaml
under ``system.thread_pools.max_workers``.

Key Functions:
    - run_with_timeout(): Execute a callable with a hard timeout.
"""

from __future__ import annotations

import atexit
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any, Callable, TypeVar

import structlog

from helpers.config_helpers import get_thread_pool_config

logger = structlog.get_logger(__name__)

_timeout_executor_lock = threading.Lock()
_pool_config = get_thread_pool_config()
_MAX_WORKERS = _pool_config.get("max_workers", 20)

_timeout_executor: ThreadPoolExecutor | None = ThreadPoolExecutor(
    max_workers=_MAX_WORKERS,
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
            # FIX: Use the configured _MAX_WORKERS instead of a hardcoded 20
            _timeout_executor = ThreadPoolExecutor(
                max_workers=_MAX_WORKERS, 
                thread_name_prefix="api_timeout"
            )
        return _timeout_executor


def run_with_timeout(func: Callable[..., T], timeout_seconds: float, error_message: str, *args: Any, **kwargs: Any) -> T:
    """Execute a callable with a hard timeout constraint via the thread pool.
    
    WARNING: Python cannot forcefully kill running threads. If the underlying 
    function hangs on I/O, this will return a TimeoutError to the caller, but 
    the background thread will remain consumed until the I/O natively fails. 
    Always ensure underlying network clients (like httpx) have their own strict timeouts.
    """
    executor = ensure_timeout_executor()
    if executor is None:
        raise TimeoutError(error_message)
        
    future = executor.submit(func, *args, **kwargs)
    try:
        return future.result(timeout=timeout_seconds)
    except FuturesTimeoutError as exc:
        future.cancel()  # Only stops the task if it hasn't started yet
        logger.warning("Operation timed out", timeout_seconds=timeout_seconds, error_message=error_message)
        raise TimeoutError(error_message) from exc

# FIX: Removed the deceptive `api_timeout` context manager entirely. 
# If any code was relying on it, it will now throw an ImportError, 
# forcing you to rewrite those calls to safely use `run_with_timeout`.

atexit.register(cleanup_timeout_executor)
