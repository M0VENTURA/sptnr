"""Download Retry Service (refactored).

Handles retrying failed downloads. Soulseek (slskd) is the only download
method; legacy rows carrying a ``qbittorrent`` source are normalised to
Soulseek when they are retried.
"""

from __future__ import annotations

from typing import Any, Dict

import structlog
from sqlalchemy import text

from db.engine import db_session
from db.repositories.queue import (
    get_ready_for_processing,
    mark_processing,
    requeue_due_failed_items,
)

logger = structlog.get_logger(__name__)

# When a download fails 3+ times with the same method, switch to fallback.
_METHOD_FAIL_THRESHOLD = 3


# =============================================================================
# AUTO-RETRY SCHEDULER (failed -> queued with backoff)
# =============================================================================

def _retry_scheduler_enabled() -> bool:
    """Honour ``features.retry_scheduler.auto_start`` (default: enabled)."""
    try:
        from helpers.config_helpers import get_feature
        cfg = get_feature("retry_scheduler", {}) or {}
        return bool(cfg.get("auto_start", True))
    except Exception:
        return True


def requeue_failed_items(limit: int = 50) -> int:
    """Requeue failed items that are due for automatic retry."""
    if not _retry_scheduler_enabled():
        return 0
        
    try:
        requeued = requeue_due_failed_items(limit=limit)
        if requeued:
            logger.info("Requeued failed item(s) for retry", count=len(requeued))
        return len(requeued)
    except Exception as exc:
        logger.error("Failed to requeue items", error=str(exc))
        return 0


# =============================================================================
# RETRY MANAGER (method fallback)
# =============================================================================

_METHOD_FALLBACK: dict[str, str] = {
    "soulseek": "soulseek",
    "slskd": "soulseek",
    "qbittorrent": "soulseek",
}


def run_retry_manager(
    db_path: str | None = None,
    navidrome_url: str | None = None,
    navidrome_token: str | None = None,
) -> Dict[str, int]:
    """Requeue failed items that are due for an automatic retry."""
    requeued = requeue_failed_items()
    return {
        "retried": requeued,
        "completed": 0,
        "method_switched": 0,
        "requeued": requeued,
    }


def _switch_method(queue_id: int, new_method: str) -> None:
    """Switch a queue item to a fallback download method."""
    try:
        with db_session() as session:
            session.execute(
                text("""
                    UPDATE download_queue
                    SET source = :source,
                        retry_count = 0,
                        status = 'queued',
                        failure_reason = 'Switched to ' || :source,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :qid
                """),
                {"source": new_method, "qid": queue_id},
            )
    except Exception as exc:
        logger.error("Failed to switch method for queue item", queue_id=queue_id, error=str(exc))


def retry_due_items() -> dict[str, int]:
    """Retry all due queue items. Thin wrapper for queue_orchestrator."""
    return run_retry_manager()


def run_retry_manager_with_navidrome_check(
    navidrome_url: str | None = None,
    navidrome_token: str | None = None,
) -> Dict[str, Any]:
    """Extended retry that checks Navidrome before re-queuing."""
    import requests

    result: Dict[str, Any] = {
        "retried": 0,
        "completed": 0,
        "already_in_navidrome": 0,
        "method_switched": 0,
        "errors": [],
    }

    try:
        items = get_ready_for_processing(limit=50)

        for item in items:
            queue_id = item.get("id")
            if queue_id is None:
                continue

            try:
                queue_id = int(queue_id)
                artist = str(item.get("artist") or "")
                title = str(item.get("title") or "")

                if navidrome_url and artist and title:
                    try:
                        resp = requests.get(
                            f"{navidrome_url.rstrip('/')}/rest/search3.view",
                            params={"query": f"{artist} {title}", "songCount": 1},
                            headers={"X-Auth-Token": navidrome_token} if navidrome_token else {},
                            timeout=8,
                        )
                        if resp.status_code == 200:
                            data = resp.json()
                            songs = (
                                (data.get("subsonic-response") or {})
                                .get("searchResult3", {})
                                .get("song", [])
                            )
                            if songs:
                                with db_session() as session:
                                    session.execute(
                                        text("""
                                            UPDATE download_queue
                                            SET status = 'completed',
                                                failure_reason = 'Already in Navidrome',
                                                updated_at = CURRENT_TIMESTAMP
                                            WHERE id = :qid
                                        """),
                                        {"qid": queue_id},
                                    )
                                result["already_in_navidrome"] += 1
                                result["completed"] += 1
                                logger.info(
                                    "Queue item already in Navidrome — marked completed",
                                    queue_id=queue_id,
                                    artist=artist,
                                    title=title,
                                )
                                try:
                                    from services.queue.queue_diagnostics_service import log_queue_event
                                    log_queue_event("completed", f"{artist} - {title} → already in Navidrome, marked completed", queue_id=queue_id)
                                except Exception:
                                    pass
                                continue
                    except Exception as nav_err:
                        logger.debug("Navidrome check failed", queue_id=queue_id, error=str(nav_err))

                retry_count = int(item.get("retry_count") or 0)
                current_source = str(item.get("source") or "soulseek").strip().lower()

                if retry_count >= _METHOD_FAIL_THRESHOLD:
                    fallback = _METHOD_FALLBACK.get(current_source)
                    if fallback and fallback != current_source:
                        _switch_method(queue_id, fallback)
                        result["method_switched"] += 1

                mark_processing(queue_id)
                result["retried"] += 1

            except Exception as item_err:
                logger.error("Failed retry for queue item", queue_id=queue_id, error=str(item_err))
                result["errors"].append(f"Queue {queue_id}: {item_err}")

    except Exception as exc:
        logger.error("Fatal error in retry manager", error=str(exc))
        result["errors"].append(str(exc))

    logger.info(
        "Retry pass complete",
        retried=result["retried"],
        completed=result["completed"],
        already_in_navidrome=result["already_in_navidrome"],
        method_switched=result["method_switched"],
        errors=len(result["errors"]),
    )

    return result
