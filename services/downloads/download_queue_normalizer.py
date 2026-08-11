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
    try:
        from helpers.logging_config import log_queue
        log_queue("[QUEUE] Running normalization")
    except Exception:
        pass

    normalize_invalid_status()
    normalize_stuck_items()

    logger.info("[QUEUE] Normalization complete")
    try:
        from helpers.logging_config import log_queue
        log_queue("[QUEUE] Normalization complete")
    except Exception:
        pass


# ============================================================
# STATUS NORMALIZATION
# ============================================================

def normalize_invalid_status() -> None:
    """
    Ensure all statuses are valid.

    Uses the canonical ``ALL_QUEUE_STATUSES`` set from queue_constraints so
    this can never silently reset legitimate statuses (e.g. ``processing``
    or ``moving``) back to ``queued``.  The statuses are inlined into the
    SQL — psycopg2 cannot adapt a Python set/list for ``NOT IN (:statuses)``.
    """

    from services.queue.queue_constraints import ALL_QUEUE_STATUSES

    status_sql = ", ".join(f"'{s}'" for s in sorted(ALL_QUEUE_STATUSES))

    with db_session() as session:
        result = session.execute(text(f"""
            UPDATE download_queue
            SET status = 'queued'
            WHERE status NOT IN ({status_sql})
        """))

        count = result.rowcount or 0

    if count:
        logger.warning("[QUEUE] Fixed %s invalid statuses", count)
        try:
            from helpers.logging_config import log_queue
            log_queue(f"[QUEUE] Fixed {count} invalid statuses")
        except Exception:
            pass


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
        try:
            from helpers.logging_config import log_queue
            log_queue(f"[QUEUE] Reset {count} stuck items")
        except Exception:
            pass


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
        try:
            from helpers.logging_config import log_queue
            log_queue(f"[QUEUE] Reset retry count on {count} items")
        except Exception:
            pass