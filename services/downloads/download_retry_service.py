"""
Download Retry Service (refactored)

Handles retrying failed downloads.
"""

import logging
from typing import Dict

from db.repositories.queue import get_ready_for_processing, mark_processing

logger = logging.getLogger(__name__)

def run_retry_manager(
    db_path=None,
    navidrome_url=None,
    navidrome_token=None,
) -> Dict[str, int]:
    """
    Process retry queue.

    Returns:
        {
            "retried": int,
            "completed": int,
        }
    """

    retried = 0
    completed = 0

    try:
        items = get_ready_for_processing(
            limit=50,
        )

        for item in items:

            queue_id = item.get("id")

            if queue_id is None:
                continue

            try:
                queue_id = int(queue_id)

                mark_processing(
                    queue_id,
                )

                retried += 1

            except Exception as item_err:
                logger.error(
                    "[RETRY_MANAGER] Failed retry for %s: %s",
                    queue_id,
                    item_err,
                    exc_info=True,
                )

    except Exception as exc:
        logger.error(
            "[RETRY_MANAGER] Fatal error: %s",
            exc,
        )

    return {
        "retried": retried,
        "completed": completed,
    }