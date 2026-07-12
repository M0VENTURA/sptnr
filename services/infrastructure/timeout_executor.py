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

from helpers.config_helpers import get_thread_pool_config

_timeout_executor_lock = threading.Lock()
_pool_config = get_thread_pool_config()
_timeout_executor: ThreadPoolExecutor | None = ThreadPoolExecutor(
    max_workers=_pool_config["max_workers"],
    thread_name_prefix="api_timeout"
)
_interpreter_shutting_down = False


class TimeoutError(Exception):
    pass


def cleanup_timeout_executor():
    global _timeout_executor, _interpreter_shutting_down
    _interpreter_shutting_down = True
    with _timeout_executor_lock:
        if _timeout_executor:
            _timeout_executor.shutdown(wait=False)
            _timeout_executor = None


def ensure_timeout_executor():
    global _timeout_executor
    with _timeout_executor_lock:
        if _interpreter_shutting_down:
            return None
        if _timeout_executor is None or getattr(_timeout_executor, "_shutdown", False):
            _timeout_executor = ThreadPoolExecutor(max_workers=20, thread_name_prefix="api_timeout")
        return _timeout_executor


def run_with_timeout(func, timeout_seconds, error_message, *args, **kwargs):
    executor = ensure_timeout_executor()
    if executor is None:
        raise TimeoutError(error_message)
    future = executor.submit(func, *args, **kwargs)
    try:
        return future.result(timeout=timeout_seconds)
    except FuturesTimeoutError as exc:
        future.cancel()
        raise TimeoutError(error_message) from exc


@contextmanager
def api_timeout(seconds: int, error_message: str = "API call timed out"):
    # Kept as a semantic context manager; call run_with_timeout for hard timeout.
    yield


atexit.register(cleanup_timeout_executor)

