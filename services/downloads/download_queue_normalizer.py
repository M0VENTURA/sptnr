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

from __future__ import annotations

import structlog
from sqlalchemy import text

from db.engine import db_session

logger = structlog.get_logger(__name__)


# ============================================================
# MAIN ENTRY
# ============================================================

def normalize_download_queue() -> None:
    """Run all normalization steps."""
    logger.info("Running queue normalization")

    normalize_invalid_status()
    normalize_stuck_items()

    logger.info("Queue normalization complete")


# ============================================================
# STATUS NORMALIZATION
# ============================================================

def normalize_invalid_status() -> None:
    """Ensure all statuses are valid."""
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
        logger.warning("Fixed invalid statuses", count=count)


# ============================================================
# STUCK ITEM RECOVERY
# ============================================================

def normalize_stuck_items() -> None:
    """Reset items stuck in transient states."""
    with db_session() as session:
        result = session.execute(text("""
            UPDATE download_queue
            SET status = 'queued'
            WHERE status IN ('searching', 'downloading')
              AND updated_at < NOW() - INTERVAL '2 hours'
        """))

        count = result.rowcount or 0

    if count:
        logger.warning("Reset stuck items", count=count)


# ============================================================
# OPTIONAL: FUTURE CLEANUPS
# ============================================================

def cleanup_failed_retries() -> None:
    """Reset retry counts or stale failures if needed."""
    with db_session() as session:
        result = session.execute(text("""
            UPDATE download_queue
            SET retry_count = 0
            WHERE retry_count > 10
        """))

        count = result.rowcount or 0

    if count:
        logger.info("Reset retry count on items", count=count)
