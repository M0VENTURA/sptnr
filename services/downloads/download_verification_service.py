"""
File verification service for download management.

Verifies that files successfully moved from /downloads to /music remain
accessible.  Handles immediate verification after move, periodic checks
of old moved files, and requeuing files that go missing from the music
library.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import text

from db.engine import db_session

logger = structlog.get_logger(__name__)

_MUSIC_ROOT = os.environ.get("MUSIC_ROOT", "/music")


# ---------------------------------------------------------------------------
# Immediate verification after a move
# ---------------------------------------------------------------------------

def verify_file_in_music(
    queue_id: int,
    target_path: str,
) -> dict[str, Any]:
    """Verify that a file exists and is readable at its target path in /music."""
    result: dict[str, Any] = {
        "success": False,
        "exists": False,
        "verified_at": None,
        "error": None,
    }

    if not target_path:
        result["error"] = "No target_path provided"
        return result

    if not os.path.isfile(target_path):
        logger.warning("File not found at target path", queue_id=queue_id, target_path=target_path)
        result["error"] = f"File not found at {target_path}"
        return result

    if not os.access(target_path, os.R_OK):
        logger.warning("File exists but is not readable", queue_id=queue_id, target_path=target_path)
        result["exists"] = True
        result["error"] = "File exists but is not readable"
        return result

    file_size = os.path.getsize(target_path)
    if file_size == 0:
        logger.warning("File is empty (0 bytes)", queue_id=queue_id, target_path=target_path)
        result["exists"] = True
        result["error"] = "File size is 0 bytes"
        return result

    verified_at = datetime.utcnow().isoformat()
    logger.info(
        "File verification SUCCESS",
        queue_id=queue_id,
        target_path=target_path,
        file_size_bytes=file_size,
    )

    try:
        with db_session() as session:
            session.execute(
                text("""
                    UPDATE download_queue
                    SET verified_in_music_at = :verified_at,
                        music_file_path = :path
                    WHERE id = :qid
                """),
                {"verified_at": verified_at, "path": target_path, "qid": queue_id},
            )
    except Exception as exc:
        logger.warning(
            "File verified but DB timestamp update failed (non-fatal)",
            queue_id=queue_id,
            error=str(exc),
        )

    result["success"] = True
    result["exists"] = True
    result["verified_at"] = verified_at
    return result


def mark_queue_item_moved(queue_id: int, target_path: str) -> None:
    """Mark a queue item as moved and set moved_at timestamp."""
    try:
        with db_session() as session:
            session.execute(
                text("""
                    UPDATE download_queue
                    SET moved_at = CURRENT_TIMESTAMP,
                        music_file_path = :path
                    WHERE id = :qid
                """),
                {"path": target_path, "qid": queue_id},
            )
    except Exception as exc:
        logger.error("Failed to mark queue item as moved", queue_id=queue_id, error=str(exc))


# ---------------------------------------------------------------------------
# Periodic check of old moved files
# ---------------------------------------------------------------------------

def check_missing_moved_files(minutes_old: int = 30) -> dict[str, Any]:
    """Check files moved to /music that have since disappeared."""
    result: dict[str, Any] = {
        "checked": 0,
        "found_missing": 0,
        "requeued": 0,
    }

    cutoff = (datetime.utcnow() - timedelta(minutes=minutes_old)).isoformat()

    try:
        with db_session() as session:
            stale = session.execute(
                text("""
                    SELECT id, music_file_path, artist, album, title
                    FROM download_queue
                    WHERE status = 'imported'
                      AND moved_at IS NOT NULL
                      AND verified_in_music_at IS NOT NULL
                      AND moved_at < :cutoff
                    ORDER BY moved_at ASC
                """),
                {"cutoff": cutoff},
            ).fetchall() or []

            matched = session.execute(
                text("""
                    SELECT id, file_path, artist, album, title
                    FROM download_queue
                    WHERE status = 'matched'
                      AND TRIM(COALESCE(file_path, '')) != ''
                """),
            ).fetchall() or []

        for item in stale:
            _check_and_requeue(item, result)

        for item in matched:
            _check_and_requeue_matched(item, result)

    except Exception as exc:
        logger.error("Error checking missing moved files", error=str(exc), exc_info=True)
        result["error"] = str(exc)

    return result


def _check_and_requeue(item: Any, result: dict) -> None:
    """Check a single imported item and requeue if the file is missing."""
    mapping = item._mapping
    qid = mapping.get("id")
    path = mapping.get("music_file_path") or ""
    artist = mapping.get("artist") or ""
    title = mapping.get("title") or ""

    if not path or os.path.isfile(path):
        return

    result["checked"] += 1
    result["found_missing"] += 1
    logger.warning("File missing from /music", queue_id=qid, artist=artist, title=title, path=path)

    try:
        with db_session() as session:
            session.execute(
                text("""
                    UPDATE download_queue
                    SET status = 'completed',
                        verified_in_music_at = NULL,
                        moved_at = NULL
                    WHERE id = :qid
                """),
                {"qid": qid},
            )
        result["requeued"] += 1
    except Exception as exc:
        logger.error("Failed to requeue missing file", queue_id=qid, error=str(exc))


def _check_and_requeue_matched(item: Any, result: dict) -> None:
    """Check a matched item and reset to queued if source file is gone."""
    mapping = item._mapping
    qid = mapping.get("id")
    path = mapping.get("file_path") or ""
    artist = mapping.get("artist") or ""
    title = mapping.get("title") or ""

    if not path or os.path.isfile(path):
        return

    result["checked"] += 1
    result["found_missing"] += 1
    logger.warning("Matched source file missing on disk", queue_id=qid, artist=artist, title=title, path=path)

    try:
        with db_session() as session:
            session.execute(
                text("""
                    UPDATE download_queue
                    SET status = 'queued',
                        file_path = NULL,
                        matched_file_path = NULL,
                        music_file_path = NULL,
                        found_filename = NULL,
                        failure_reason = 'Matched file no longer exists on disk; re-queued'
                    WHERE id = :qid
                      AND status = 'matched'
                """),
                {"qid": qid},
            )
        result["requeued"] += 1
    except Exception as exc:
        logger.error("Failed to reset matched item", queue_id=qid, error=str(exc))


# ---------------------------------------------------------------------------
# Transfer with optional verification
# ---------------------------------------------------------------------------

def transfer_and_verify(
    source_path: str,
    dest_path: str,
    queue_id: int | None = None,
) -> dict[str, Any]:
    """Move a file from downloads to music, then verify it landed correctly."""
    from services.infrastructure.filesystem_service import transfer_download_to_music

    transfer_result = transfer_download_to_music(source_path, dest_path)
    if not transfer_result.get("success"):
        return transfer_result

    dest = transfer_result.get("target_path", dest_path)

    if queue_id:
        mark_queue_item_moved(queue_id, dest)

    return verify_file_in_music(queue_id or 0, dest)


# ---------------------------------------------------------------------------
# Periodic cleanup
# ---------------------------------------------------------------------------

def cleanup_old_failed(days: int = 30) -> int:
    """Delete queue rows with status='failed' older than *days*."""
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    DELETE FROM download_queue
                    WHERE status = 'failed'
                      AND updated_at < CURRENT_TIMESTAMP - INTERVAL '1 day' * :days
                """),
                {"days": days},
            )
            deleted = result.rowcount or 0
            if deleted:
                logger.info("Cleaned up old failed downloads", deleted_count=deleted)
            return deleted
    except Exception as exc:
        logger.error("Failed to clean up old failed downloads", error=str(exc))
        return 0
