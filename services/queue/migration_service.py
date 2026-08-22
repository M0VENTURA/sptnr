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

import uuid
from typing import Any, Dict

import structlog
from sqlalchemy import text

from db.engine import db_session

logger = structlog.get_logger(__name__)


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
    """Create the ``import_group`` column if it does not exist."""
    if _column_exists(_COLUMN_IMPORT_GROUP):
        logger.debug("import_group column already exists")
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
        logger.info("Added import_group column to download_queue")
        return True
    except Exception as exc:
        logger.error("Failed to add import_group column", error=str(exc))
        return False


# ---------------------------------------------------------------------------
# Migration logic
# ---------------------------------------------------------------------------

def migrate_existing_queue_items_to_grouped_setup(
    limit: int | None = None,
) -> Dict[str, Any]:
    """Backfill legacy queue rows into the current grouped/source conventions."""
    result: Dict[str, Any] = {
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
                logger.info("Fixed NULL source values", count=fixed.rowcount)
    except Exception as exc:
        logger.error("Failed to fix source values", error=str(exc))
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
                logger.info("Fixed NULL status_changed_at values", count=fixed_ts.rowcount)
    except Exception as exc:
        logger.debug("Failed to fix status_changed_at", error=str(exc))

    # ---- Step 4: Group legacy rows by (artist, album) ----
    try:
        with db_session() as session:
            ungrouped = session.execute(
                text(
                    "SELECT id, artist, album FROM download_queue "
                    "WHERE import_group IS NULL OR import_group = '' "
                    "ORDER BY id"
                ),
            )
            rows = ungrouped.fetchall()

            if not rows:
                logger.info("No ungrouped rows found")
                return result

            groups: dict[tuple[str, str], str] = {}
            for row in rows:
                mapping = getattr(row, "_mapping", None)
                row_id = mapping.get("id") if mapping else row[0]
                art = mapping.get("artist") if mapping else row[1]
                alb = mapping.get("album") if mapping else row[2]

                artist = (art or "").strip().lower() if art else ""
                album = (alb or "").strip().lower() if alb else ""
                key = (artist, album)
                if key not in groups:
                    groups[key] = str(uuid.uuid4())

            batch_size = 100
            total_updated = 0
            for idx in range(0, len(rows), batch_size):
                batch = rows[idx: idx + batch_size]
                with db_session() as update_session:
                    for row in batch:
                        mapping = getattr(row, "_mapping", None)
                        row_id = mapping.get("id") if mapping else row[0]
                        art = mapping.get("artist") if mapping else row[1]
                        alb = mapping.get("album") if mapping else row[2]

                        artist = (art or "").strip().lower() if art else ""
                        album = (alb or "").strip().lower() if alb else ""
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
                "Grouped legacy rows into groups",
                rows_updated=total_updated,
                groups_created=len(groups),
            )

    except Exception as exc:
        logger.error("Grouping failed", error=str(exc), exc_info=True)
        result["success"] = False
        result["error"] = str(exc)

    return result
