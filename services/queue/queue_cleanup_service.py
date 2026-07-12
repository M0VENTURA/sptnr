"""Queue cleanup service.

Provides queue maintenance and cleanup operations:
    - queue_cleanup(): Remove stale/expired queue entries.
    - queue_reset_moving(): Reset items stuck in 'moving' state.

Architecture:
    Uses direct repository calls (``db.repositories.queue``) exclusively.
    No legacy helpers or raw SQL. Returns consistent (response, status)
    tuples for route integration.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from db.repositories import queue as queue_repository
from helpers.response_helpers import _ok, _fail, _safe_int

logger = logging.getLogger(__name__)


# =============================================================================
# ✅ CLEANUP OPERATIONS
# =============================================================================

def queue_cleanup():
    try:
        result = queue_repository.cleanup()

        if isinstance(result, tuple):
            return result

        if isinstance(result, dict):
            return result, 200 if result.get("success", True) else 500

        return _ok(result=result)

    except Exception as exc:
        logger.exception("queue_cleanup failed")
        return _fail(str(exc), 500)


def queue_reset_moving(data: Mapping[str, Any]):
    try:
        queue_ids = data.get("queue_ids") or []
        stale_minutes = _safe_int(data.get("stale_minutes"), 5)

        result = queue_repository.reset_moving(queue_ids, stale_minutes)

        if isinstance(result, tuple):
            return result

        if isinstance(result, dict):
            return result, 200 if result.get("success", True) else 500

        return _ok(reset=result)

    except Exception as exc:
        logger.exception("queue_reset_moving failed")
        return _fail(str(exc), 500)


def queue_cleanup_copied_sources():
    try:
        result = queue_repository.cleanup_copied_sources()

        if isinstance(result, tuple):
            return result

        if isinstance(result, dict):
            return result, 200 if result.get("success", True) else 500

        return _ok(cleaned=result)

    except Exception as exc:
        logger.exception("queue_cleanup_copied_sources failed")
        return _fail(str(exc), 500)


def queue_cleanup_orphaned(data: Mapping[str, Any]):
    try:
        result = queue_repository.cleanup_orphaned(dict(data or {}))

        if isinstance(result, tuple):
            return result

        if isinstance(result, dict):
            return result, 200 if result.get("success", True) else 500

        return _ok(cleaned=result)

    except Exception as exc:
        logger.exception("queue_cleanup_orphaned failed")
        return _fail(str(exc), 500)


def queue_verify_and_prune(data: Mapping[str, Any]):
    try:
        result = queue_repository.verify_and_prune(dict(data or {}))

        if isinstance(result, tuple):
            return result

        if isinstance(result, dict):
            return result, 200 if result.get("success", True) else 500

        return _ok(result=result)

    except Exception as exc:
        logger.exception("queue_verify_and_prune failed")
        return _fail(str(exc), 500)


def queue_delete_folder(data: Mapping[str, Any]):
    try:
        result = queue_repository.delete_folder(dict(data or {}))

        if isinstance(result, tuple):
            return result

        if isinstance(result, dict):
            return result, 200 if result.get("success", True) else 500

        return _ok(result=result)

    except Exception as exc:
        logger.exception("queue_delete_folder failed")
        return _fail(str(exc), 500)


def queue_remove_group(data: Mapping[str, Any]):
    try:
        result = queue_repository.remove_group(dict(data or {}))

        if isinstance(result, tuple):
            return result

        if isinstance(result, dict):
            return result, 200 if result.get("success", True) else 500

        return _ok(result=result)

    except Exception as exc:
        logger.exception("queue_remove_group failed")
        return _fail(str(exc), 500)


# =============================================================================
# ✅ COMPATIBILITY ROUTES (keep if used)
# =============================================================================

def clear(request):
    try:
        data = request.get_json(force=True) if request else {}
        result = queue_repository.clear_queue(data)
        return result, 200 if result.get("success") else 500
    except Exception as exc:
        logger.exception("clear failed")
        return _fail(str(exc), 500)


def purge_all():
    try:
        result = queue_repository.purge_all()
        return result, 200 if result.get("success") else 500
    except Exception as exc:
        logger.exception("purge_all failed")
        return _fail(str(exc), 500)
