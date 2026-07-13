"""
Download Retry Service (refactored)

Handles retrying failed downloads with automatic method fallback.
If a download fails after multiple attempts via Soulseek, it will be
re-tried via qBittorrent and vice versa.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict

from sqlalchemy import text
from db.engine import db_session
from db.repositories.queue import get_ready_for_processing, mark_processing

logger = logging.getLogger(__name__)

# When a download fails 3+ times with the same method, switch to fallback.
_METHOD_FAIL_THRESHOLD = 3

# Fallback chain: Soulseek -> qBittorrent -> Soulseek
_METHOD_FALLBACK: dict[str, str] = {
    "soulseek": "qbittorrent",
    "slskd": "qbittorrent",
    "qbittorrent": "soulseek",
}


def run_retry_manager(
    db_path: str | None = None,
    navidrome_url: str | None = None,
    navidrome_token: str | None = None,
) -> Dict[str, int]:
    """
    Process retry queue with automatic method fallback.

    Returns:
        ``{"retried": int, "completed": int, "method_switched": int}``
    """
    retried = 0
    completed = 0
    method_switched = 0

    try:
        items = get_ready_for_processing(limit=50)
        now = datetime.utcnow()

        for item in items:
            queue_id = item.get("id")
            if queue_id is None:
                continue

            try:
                queue_id = int(queue_id)
                retry_count = int(item.get("retry_count") or 0)
                current_source = str(item.get("source") or "soulseek").strip().lower()

                # ── Method fallback logic ──
                if retry_count >= _METHOD_FAIL_THRESHOLD:
                    fallback = _METHOD_FALLBACK.get(current_source)
                    if fallback and fallback != current_source:
                        _switch_method(queue_id, fallback)
                        method_switched += 1
                        logger.info(
                            "[RETRY] Queue %s: switched method %s -> %s "
                            "(after %s failures)",
                            queue_id, current_source, fallback, retry_count,
                        )

                mark_processing(queue_id)
                retried += 1

            except Exception as item_err:
                logger.error("[RETRY] Failed retry for %s: %s", queue_id, item_err)

    except Exception as exc:
        logger.error("[RETRY] Fatal error: %s", exc)

    return {
        "retried": retried,
        "completed": completed,
        "method_switched": method_switched,
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
        logger.error("Failed to switch method for queue %s: %s", queue_id, exc)


def retry_due_items() -> dict[str, int]:
    """Retry all due queue items. Thin wrapper for queue_orchestrator."""
    return run_retry_manager()


def run_retry_manager_with_navidrome_check(
    navidrome_url: str | None = None,
    navidrome_token: str | None = None,
) -> Dict[str, Any]:
    """Extended retry that checks Navidrome before re-queuing.

    If a track already exists in Navidrome, it's marked as 'completed'
    instead of being retried.
    """
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

                # Check Navidrome first.
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
                                continue
                    except Exception:
                        pass

                # Apply the standard retry logic.
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
                logger.error("[RETRY] Failed retry for %s: %s", queue_id, item_err)
                result["errors"].append(f"Queue {queue_id}: {item_err}")

    except Exception as exc:
        logger.error("[RETRY] Fatal error: %s", exc)
        result["errors"].append(str(exc))

    return result