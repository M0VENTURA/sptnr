"""
DOWNLOAD QUEUE NORMALIZER

Handles:
- Queue data cleanup
- Invalid state correction
- Lightweight normalization

Called by:
- scheduler
- manual maintenance
"""

import logging
from sqlalchemy import text
from db.engine import db_session

logger = logging.getLogger(__name__)


# ============================================================
# MAIN ENTRY
# ============================================================

def normalize_download_queue() -> None:
    """
    Run all normalization steps.
    """

    logger.info("[QUEUE] Running normalization")

    normalize_invalid_status()
    normalize_stuck_items()

    logger.info("[QUEUE] Normalization complete")


# ============================================================
# STATUS NORMALIZATION
# ============================================================

def normalize_invalid_status() -> None:
    """
    Ensure all statuses are valid.
    """

    valid_statuses = {
        "queued",
        "searching",
        "downloading",
        "matched",
        "completed",
        "failed",
        "unmatched",
        "imported",
        "in_collection"
    }

    with db_session() as session:
        result = session.execute(text("""
            UPDATE download_queue
            SET status = 'queued'
            WHERE status NOT IN :statuses
        """), {"statuses": tuple(valid_statuses)})

        count = result.rowcount or 0

    if count:
        logger.warning("[QUEUE] Fixed %s invalid statuses", count)


# ============================================================
# STUCK ITEM RECOVERY
# ============================================================

def normalize_stuck_items() -> None:
    """
    Reset items stuck in transient states.
    """

    with db_session() as session:
        result = session.execute(text("""
            UPDATE download_queue
            SET status = 'queued'
            WHERE status IN ('searching', 'downloading')
              AND updated_at < NOW() - INTERVAL '2 hours'
        """))

        count = result.rowcount or 0

    if count:
        logger.warning("[QUEUE] Reset %s stuck items", count)


# ============================================================
# OPTIONAL: FUTURE CLEANUPS
# ============================================================

def cleanup_failed_retries() -> None:
    """
    Reset retry counts or stale failures if needed.
    """

    with db_session() as session:
        result = session.execute(text("""
            UPDATE download_queue
            SET retry_count = 0
            WHERE retry_count > 10
        """))

        count = result.rowcount or 0

    if count:
        logger.info("[QUEUE] Reset retry count on %s items", count)