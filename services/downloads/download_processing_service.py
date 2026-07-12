"""
QUEUE PROCESSING SERVICE

Handles:
- Adding items to queue
- Batch ingestion
- Queue state transitions
- Post-download processing orchestration
"""

from __future__ import annotations

import re
import logging

from typing import Any, Dict, List


from sqlalchemy import text
from db.engine import db_session

from db.repositories.queue import (
    insert_queue_item,
    update_queue_item,
    get_completed_queue,
    purge_all,
)


from services.metadata.tag_file_service import update_file_metadata
from services.downloads.download_organize_service import rename_and_move_file

logger = logging.getLogger(__name__)


# =============================================================================
# HELPERS
# =============================================================================

def is_musicbrainz_backed(queue_item: dict) -> bool:
    source = str(queue_item.get("release_source") or "").lower()

    if source == "musicbrainz":
        return True

    mbid_pattern = r"^[0-9a-fA-F-]{36}$"

    return any(
        re.match(mbid_pattern, str(queue_item.get(field) or ""))
        for field in ("release_id", "release_mbid", "recording_mbid")
    )


# =============================================================================
# ADD
# =============================================================================

def add_to_queue(
    artist: str,
    title: str,
    album: str | None = None,
    source: str = "soulseek",
    priority: int = 5,
    **kwargs
) -> Dict[str, Any]:

    if not artist or not title:
        return {"success": False, "error": "Artist and title are required"}

    try:
        item = insert_queue_item(
            artist=artist.strip(),
            title=title.strip(),
            album=album.strip() if album else None,
            source=source,
            priority=priority,
            **kwargs,
        )

        # Signal the event-driven queue worker to wake up immediately
        # instead of waiting for the next 30-second polling cycle.
        try:
            from services.queue.queue_signal import signal_new_item
            signal_new_item()
        except Exception:
            pass  # Non-critical — worker will pick it up on next cycle

        return {"success": True, "item": item}

    except Exception as e:
        logger.error("[QUEUE] Add failed: %s", e)
        return {"success": False, "error": str(e)}


def queue_add(
    payload: Dict[str, Any],
) -> Dict[str, Any]:

    artist = str(
        payload.get("artist") or ""
    ).strip()

    title = str(
        payload.get("title") or ""
    ).strip()

    album = payload.get("album")

    return add_to_queue(
        artist=artist,
        title=title,
        album=str(album).strip() if album else None,
        source=str(
            payload.get("source", "soulseek")
        ),
        priority=int(
            payload.get("priority", 5)
        ),
    )

# =============================================================================
# BATCH ADD
# =============================================================================

def queue_add_batch(data: Dict[str, Any]) -> Dict[str, Any]:
    items = data.get("items", [])

    if not isinstance(items, list):
        return {"success": False, "error": "items must be a list"}

    added, failed = 0, 0
    results: List[Dict[str, Any]] = []

    for item in items:
        result = add_to_queue(
            artist=item.get("artist"),
            title=item.get("title"),
            album=item.get("album"),
            source=item.get("source", data.get("source", "soulseek")),
            priority=item.get("priority", 5),
            track_number=item.get("track_number"),
            album_artist=item.get("album_artist"),
            year=item.get("year"),
            release_id=item.get("release_id"),
            release_mbid=item.get("release_mbid"),
            recording_mbid=item.get("recording_mbid"),
            disc_number=item.get("disc_number"),
            duration=item.get("duration"),
        )

        results.append(result)

        if result.get("success"):
            added += 1
        else:
            failed += 1

    return {
        "success": True,
        "added": added,
        "failed": failed,
        "results": results,
    }


# =============================================================================
# QUEUE STATE OPERATIONS
# =============================================================================

def queue_requeue(queue_id: int) -> Dict[str, Any]:
    updated = update_queue_item(queue_id, status="queued")

    if not updated:
        return {"success": False, "error": "Queue item not found"}

    return {"success": True, "queue_id": queue_id, "status": "queued"}


def queue_requeue_all_unmatched() -> Dict[str, Any]:
    try:
        with db_session() as session:

            result = session.execute(
                text("""
                    UPDATE download_queue
                    SET status = 'queued',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE status = 'unmatched'
                """)
            )

            count = int(result.rowcount or 0)

        return {
            "success": True,
            "updated_count": count,
        }

    except Exception as exc:
        return {
            "success": False,
            "error": str(exc),
        }


def queue_retry_all_failed() -> Dict[str, Any]:
    try:
        with db_session() as session:

            result = session.execute(
                text("""
                    UPDATE download_queue
                    SET status = 'queued',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE status = 'failed'
                """)
            )

            count = int(result.rowcount or 0)

        return {
            "success": True,
            "updated_count": count,
        }

    except Exception as exc:
        return {
            "success": False,
            "error": str(exc),
        }

def queue_clear(
    data: Dict[str, Any],
) -> Dict[str, Any]:

    try:
        filters = data.get("filters", {}) or {}

        with db_session() as session:

            if filters.get("status"):
                result = session.execute(
                    text("DELETE FROM download_queue WHERE status = :status"),
                    {"status": filters["status"]},
                )
            else:
                result = session.execute(
                    text("DELETE FROM download_queue WHERE status != :status"),
                    {"status": "imported"},
                )

            cursor.execute(
                query,
                params,
            )

            deleted = int(
                cursor.rowcount or 0
            )

        return {
            "success": True,
            "deleted": deleted,
        }

    except Exception as exc:
        return {
            "success": False,
            "error": str(exc),
        }

def queue_purge_all() -> Dict[str, Any]:
    return purge_all()


# =============================================================================
# PROCESSING PIPELINE
# =============================================================================

def process_completed_queue_item(queue_item: Dict[str, Any]) -> Dict[str, Any]:
    try:
        queue_id = queue_item.get("id")
        file_path = queue_item.get("file_path")

        if queue_id is None:
            return {
                "success": False,
                "error": "Missing queue id",
            }

        queue_id = int(queue_id)

        if not file_path:
            return {
                "success": False,
                "error": "Missing file_path",
    }

        metadata = {
            "track_number": queue_item.get("track_number"),
            "artist": queue_item.get("artist"),
            "album_artist": queue_item.get("album_artist") or queue_item.get("artist"),
            "album": queue_item.get("album"),
            "year": queue_item.get("year"),
            "title": queue_item.get("title"),
            "disc_number": queue_item.get("disc_number"),
        }

        # ✅ Step 1 — update tags
        update_file_metadata(file_path, metadata)

        # ✅ Step 2 — move file
        result = rename_and_move_file(file_path, metadata)

        if not result.get("success"):
            return result

        target_path = result.get("target_path")

        # ✅ Step 3 — mark imported
        update_queue_item(
            queue_id,
            status="imported",
            file_path=target_path,
        )

        return {"success": True, "target_path": target_path}

    except Exception as e:
        logger.exception("process_completed_queue_item failed")
        return {"success": False, "error": str(e)}


def process_pending_completed_items(limit: int = 10) -> Dict[str, Any]:
    stats = {"processed": 0, "failed": 0}

    try:
        items = get_completed_queue(limit)

        for item in items:
            result = process_completed_queue_item(item)

            if result.get("success"):
                stats["processed"] += 1
            else:
                stats["failed"] += 1

        return {"success": True, "stats": stats}

    except Exception as e:
        return {"success": False, "error": str(e)}



def get_completed_queue_items(
    limit: int = 50,
) -> list[dict[str, Any]]:
    return get_completed_queue(limit)
