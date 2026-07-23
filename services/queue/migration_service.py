"""Queue migration service.

Backfills legacy queue rows into the current grouped/source conventions
that the rest of the queue pipeline expects.

The main gap bridged by this module is the ``import_group`` column —
it was added to the queue workflow *after* the initial schema was created,
so rows inserted before that point have no ``import_group`` value.

What the migration does:
    1. Adds the ``import_group`` column to ``download_queue`` if missing.
    2. Groups legacy rows by (artist, album) and assigns a shared UUID.
    3. Fills in any NULL ``source`` values with a best-guess default.
    4. Sets ``status_changed_at`` for rows that have a NULL value.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from db.engine import db_session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

_COLUMN_IMPORT_GROUP = "import_group"


def _column_exists(column: str, table: str = "download_queue") -> bool:
    """Return True when *column* exists on *table*."""
    try:
        with db_session() as session:
            result = session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = :table AND column_name = :col"
                ),
                {"table": table, "col": column},
            )
            return result.fetchone() is not None
    except Exception:
        return False


def _ensure_import_group_column() -> bool:
    """Create the ``import_group`` column if it does not exist.

    Returns True if the column was added, False if it already existed.
    """
    if _column_exists(_COLUMN_IMPORT_GROUP):
        logger.debug("[migration_service] import_group column already exists")
        return False

    try:
        with db_session() as session:
            session.execute(
                text(
                    "ALTER TABLE download_queue "
                    "ADD COLUMN import_group TEXT"
                )
            )
            session.commit()
        logger.info("[migration_service] Added import_group column to download_queue")
        return True
    except Exception as exc:
        logger.error("[migration_service] Failed to add import_group column: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Migration logic
# ---------------------------------------------------------------------------

def migrate_existing_queue_items_to_grouped_setup(
    limit: int | None = None,
) -> dict[str, Any]:
    """Backfill legacy queue rows into the current grouped/source conventions.

    Args:
        limit: Maximum number of rows to process in this run (None = all).

    Returns:
        A dict with keys:
            - success: bool
            - rows_updated: int
            - groups_created: int
            - columns_added: list[str]
            - error: str (only on failure)
    """
    result: dict[str, Any] = {
        "success": True,
        "rows_updated": 0,
        "groups_created": 0,
        "columns_added": [],
        "error": None,
    }

    # ---- Step 1: Ensure schema ----
    if _ensure_import_group_column():
        result["columns_added"].append("import_group")

    # ---- Step 2: Fix NULL source values ----
    try:
        with db_session() as session:
            fixed = session.execute(
                text(
                    "UPDATE download_queue "
                    "SET source = 'soulseek' "
                    "WHERE source IS NULL OR source = ''"
                ),
            )
            session.commit()
            if fixed.rowcount:
                logger.info("[migration_service] Fixed %d NULL source values", fixed.rowcount)
    except Exception as exc:
        logger.error("[migration_service] Failed to fix source values: %s", exc)
        result["error"] = str(exc)
        return result

    # ---- Step 3: Fix NULL status_changed_at ----
    try:
        with db_session() as session:
            fixed_ts = session.execute(
                text(
                    "UPDATE download_queue "
                    "SET status_changed_at = created_at "
                    "WHERE status_changed_at IS NULL"
                ),
            )
            session.commit()
            if fixed_ts.rowcount:
                logger.info("[migration_service] Fixed %d NULL status_changed_at values", fixed_ts.rowcount)
    except Exception as exc:
        logger.debug("[migration_service] Failed to fix status_changed_at: %s", exc)

    # ---- Step 4: Group legacy rows by (artist, album) ----
    try:
        with db_session() as session:
            # Find rows that need an import_group
            ungrouped = session.execute(
                text(
                    "SELECT id, artist, album FROM download_queue "
                    "WHERE import_group IS NULL OR import_group = '' "
                    "ORDER BY id"
                ),
            )
            rows = ungrouped.fetchall()

            if not rows:
                logger.info("[migration_service] No ungrouped rows found")
                return result

            # Group by (artist, album) → assign a single UUID per group
            groups: dict[tuple[str, str], str] = {}
            for row in rows:
                artist = (row[1] or "").strip().lower() if row[1] else ""
                album = (row[2] or "").strip().lower() if row[2] else ""
                key = (artist, album)
                if key not in groups:
                    groups[key] = str(uuid.uuid4())

            # Apply import_group in batches
            batch_size = 100
            total_updated = 0
            for idx in range(0, len(rows), batch_size):
                batch = rows[idx: idx + batch_size]
                with db_session() as update_session:
                    for row in batch:
                        row_id = row[0]
                        artist = (row[1] or "").strip().lower() if row[1] else ""
                        album = (row[2] or "").strip().lower() if row[2] else ""
                        group_id = groups.get((artist, album))
                        if group_id:
                            update_session.execute(
                                text(
                                    "UPDATE download_queue "
                                    "SET import_group = :gid "
                                    "WHERE id = :rid AND (import_group IS NULL OR import_group = '')"
                                ),
                                {"gid": group_id, "rid": row_id},
                            )
                    update_session.commit()
                total_updated += len(batch)

            result["rows_updated"] = total_updated
            result["groups_created"] = len(groups)

            logger.info(
                "[migration_service] Grouped %d rows into %d groups",
                total_updated,
                len(groups),
            )

    except Exception as exc:
        logger.error("[migration_service] Grouping failed: %s", exc, exc_info=True)
        result["success"] = False
        result["error"] = str(exc)

    return result
