"""Queue cleanup service.

Provides queue maintenance and cleanup operations:
    - queue_cleanup(): Remove stale/expired queue entries.
    - queue_reset_moving(): Reset items stuck in 'moving' state.
    - cleanup_stuck_items(): Reset items stuck in 'searching'/'downloading'.

Architecture:
    Uses direct repository calls (``db.repositories.queue``) exclusively.
    No legacy helpers or raw SQL. Returns consistent (response, status)
    tuples for route integration.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Mapping

from sqlalchemy import text

from db.engine import db_session
from db.repositories import queue as queue_repository
from helpers.response_helpers import _ok, _fail, _safe_int

logger = logging.getLogger(__name__)


# =============================================================================
# ✅ CLEANUP OPERATIONS
# =============================================================================

# Items stuck in 'searching' for more than 300 seconds are likely hung.
# _SLSKD_SEARCH_MAX_WAIT_SECONDS is 150s, so legitimate searches can stay in
# 'searching' for up to ~2.5 min. 300s (5 min, 2x) gives ample margin before
# declaring an item truly stuck (e.g. after a crash left the status unreset).
_STUCK_SEARCH_SECONDS = 300

# Items stuck in 'downloading' for longer than 6 hours are presumed dead —
# the downloads watcher normally completes them within minutes.
_STUCK_DOWNLOAD_SECONDS = 6 * 3600


def reset_abandoned_items() -> dict[str, int]:
    """Reset items left in 'searching'/'processing' by a previous worker.

    These statuses only exist while a worker is actively processing the item,
    so at worker startup any item still in them was abandoned by a dead or
    restarted worker — without this they would stay invisible and unretryable
    forever. 'downloading' items are NOT touched: the slskd transfer may still
    be running server-side.
    """
    reset = 0
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    UPDATE download_queue
                    SET status = 'queued',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE status IN ('searching', 'processing')
                """)
            )
            reset = int(result.rowcount or 0)
        if reset:
            logger.warning(
                "Reset %s abandoned queue item(s) at worker startup",
                reset,
            )
        return {"abandoned_reset": reset}
    except Exception as exc:
        logger.exception("reset_abandoned_items failed")
        return {"abandoned_reset": 0, "error": str(exc)}


def cleanup_stuck_items() -> dict[str, int]:
    """Reset queue items stuck in 'searching'/'downloading'.

    Mirrors the legacy ``cleanup_stuck_searching_items`` behaviour:
    - ``searching`` items older than 5 minutes are reset to ``queued`` for retry.
    - ``downloading`` items older than 6 hours are reset to ``failed``.

    Returns:
        ``{"searching_reset": int, "downloading_reset": int}``
    """
    searching_reset = 0
    downloading_reset = 0
    processing_reset = 0
    try:
        now = datetime.utcnow()

        # Stuck 'searching' → back to 'queued' so the worker retries it.
        with db_session() as session:
            result = session.execute(
                text("""
                    UPDATE download_queue
                    SET status = 'queued',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE status = 'searching'
                      AND updated_at < :cutoff
                """),
                {"cutoff": (now - timedelta(seconds=_STUCK_SEARCH_SECONDS)).isoformat()},
            )
            searching_reset = int(result.rowcount or 0)

        # Stuck 'processing' (mid-search, worker crashed) → back to 'queued'.
        # The pipeline marks items 'processing' for the duration of the
        # Soulseek search, so a crash leaves them invisible AND unretryable;
        # the same 5-minute cutoff as 'searching' applies.
        with db_session() as session:
            result = session.execute(
                text("""
                    UPDATE download_queue
                    SET status = 'queued',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE status = 'processing'
                      AND updated_at < :cutoff
                """),
                {"cutoff": (now - timedelta(seconds=_STUCK_SEARCH_SECONDS)).isoformat()},
            )
            processing_reset = int(result.rowcount or 0)

        # Stuck 'downloading' → failed (the watcher never confirmed it).
        with db_session() as session:
            result = session.execute(
                text("""
                    UPDATE download_queue
                    SET status = 'failed',
                        failure_reason = 'Stuck in downloading state',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE status = 'downloading'
                      AND updated_at < :cutoff
                """),
                {"cutoff": (now - timedelta(seconds=_STUCK_DOWNLOAD_SECONDS)).isoformat()},
            )
            downloading_reset = int(result.rowcount or 0)

        if searching_reset or downloading_reset or processing_reset:
            logger.warning(
                "Reset stuck queue items — searching=%s, processing=%s, downloading=%s",
                searching_reset, processing_reset, downloading_reset,
            )
        return {"searching_reset": searching_reset, "processing_reset": processing_reset, "downloading_reset": downloading_reset}

    except Exception as exc:
        logger.exception("cleanup_stuck_items failed")
        return {"searching_reset": 0, "processing_reset": 0, "downloading_reset": 0, "error": str(exc)}


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
