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
    mark_failed,
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

        # Record the queue-add to queue.log + the in-memory store so the
        # monitor's Queue Activity view captures every enqueue.
        try:
            from services.queue.queue_diagnostics_service import log_queue_event
            queue_id = item.get("id") if isinstance(item, dict) else None
            log_queue_event(
                "queued",
                f"{artist.strip()} - {title.strip()}"
                + (f" [{album.strip()}]" if album and album.strip() else ""),
                queue_id=queue_id,
                source=source,
            )
        except Exception:
            pass

        # If it was a duplicate, skip the wake-up signal
        if item.get("already_queued"):
            return {"success": True, "already_queued": True, "item": item}

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

    added, skipped, failed = 0, 0, 0
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
            import_group=item.get("import_group") or data.get("import_group"),
            import_type=item.get("import_type") or data.get("import_type"),
        )

        results.append(result)

        if result.get("already_queued"):
            skipped += 1
        elif result.get("success"):
            added += 1
        else:
            failed += 1

    return {
        "success": True,
        "added": added,
        "skipped": skipped,
        "failed": failed,
        "results": results,
    }


# =============================================================================
# QUEUE STATE OPERATIONS
# =============================================================================

def queue_requeue(queue_id: int) -> Dict[str, Any]:
    """Requeue an item.

    Failed/removed/cancelled items go through ``requeue_queue_item`` so the
    retry backoff (``next_retry_at`` / ``retry_count``) is cleared — manual
    retries must be immediate.  Other statuses get a plain status bump.
    """
    from db.repositories.queue import requeue_queue_item

    updated = requeue_queue_item(queue_id) or update_queue_item(
        queue_id, status="queued"
    )

    if not updated:
        return {"success": False, "error": "Queue item not found"}

    return {"success": True, "queue_id": queue_id, "status": "queued"}


def queue_force_start(queue_id: int) -> Dict[str, Any]:
    """Bypass retry/backoff timers and push an item straight back to 'queued'.

    Unlike ``queue_requeue`` (which only resets failed/removed/cancelled),
    this also clears ``next_retry_at`` for ``backed_off`` / ``pending_release``
    items so the very next worker cycle picks them up immediately.
    """
    try:
        from db.repositories.queue import get_queue_item, update_queue_item

        item = get_queue_item(queue_id)
        if not item:
            return {"success": False, "error": "Queue item not found"}
        if item.get("file_path"):
            return {"success": False, "error": "Item already has a downloaded file — use Transfer or Match instead"}
        if str(item.get("status") or "").lower() in ("completed", "imported", "in_collection"):
            return {"success": False, "error": "Item is already completed"}

        updated = update_queue_item(
            queue_id,
            status="queued",
            retry_count=0,
            failure_reason=None,
            next_retry_at=None,
        )
        return {
            "success": bool(updated),
            "queue_id": queue_id,
            "status": "queued",
            "message": f"Item #{queue_id} forced back to active processing",
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


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

            deleted = int(
                result.rowcount or 0
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

def queue_status(args: Any = None) -> Dict[str, Any]:
    """Get queue items grouped for the queue page.

    Mirrors the legacy ``/api/queue/status`` payload: ``active`` (includes
    failed items — the UI splits them out), ``completed`` and
    ``newly_completed`` lists, plus per-status counts for compatibility.
    """
    try:
        from db.repositories.queue import (
            get_active_queue,
            get_completed_queue,
            get_queue_status_counts,
        )
        limit = 200
        if args and hasattr(args, "get"):
            try:
                limit = min(int(args.get("limit", 200)), 500)
            except (TypeError, ValueError):
                pass
        active = get_active_queue(limit=limit)
        completed = get_completed_queue(limit=min(limit, 50))
        counts = get_queue_status_counts()
        return {
            "success": True,
            "active": active,
            "completed": completed,
            "newly_completed": [],
            "total_active": len(active),
            "total_completed": len(completed),
            **counts,
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}

def queue_update(queue_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Update a queue item's metadata (wraps DB repository)."""
    try:
        from db.repositories.queue import update_queue_item

        # Convert incoming keys to DB column names
        field_map = {
            "status": "status",
            "priority": "priority",
            "artist": "artist",
            "title": "title",
            "album": "album",
            "album_artist": "album_artist",
            "year": "year",
        }
        kwargs = {}
        for json_key, col_name in field_map.items():
            if json_key in payload:
                kwargs[col_name] = payload[json_key]

        updated = update_queue_item(queue_id, **kwargs)
        return {"success": updated is not None, "item": updated}
    except Exception as exc:
        return {"success": False, "error": str(exc)}

def queue_imported(args: Any = None) -> Dict[str, Any]:
    """Get list of imported/completed queue items (wraps DB repository)."""
    try:
        from db.repositories.queue import get_completed_queue
        limit = 50
        if args and hasattr(args, "get"):
            try:
                limit = min(int(args.get("limit", 50)), 200)
            except (TypeError, ValueError):
                pass
        items = get_completed_queue(limit=limit)
        return {"success": True, "items": items}
    except Exception as exc:
        return {"success": False, "error": str(exc)}

def queue_delete(queue_id: int, delete_download_file: bool = False) -> Dict[str, Any]:
    """Delete a queue item, optionally removing the downloaded file."""
    try:
        # Get queue item info to find its file path
        from db.repositories.queue import get_queue_item, delete_queue_item as _delete_from_db
        import os

        if delete_download_file:
            item = get_queue_item(queue_id)
            if item:
                file_path = (
                    item.get("file_path") or item.get("music_file_path")
                    or item.get("matched_file_path") or item.get("found_filename") or ""
                )
                # Normalise Windows backslash separators (remote Soulseek
                # filenames) so the file can be found on Linux.
                file_path = str(file_path or "").replace("\\", "/")
                if file_path and os.path.isfile(file_path):
                    try:
                        os.remove(file_path)
                    except OSError as exc:
                        pass

        deleted = _delete_from_db(queue_id)
        return {"success": bool(deleted), "deleted": deleted}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def queue_cancel(queue_id: int) -> Dict[str, Any]:
    """Cancel a queue item: cancel its active slskd transfer and mark failed.

    A cancelled item stays visible under Failed so the user can retry it —
    the retry button re-queues it for a fresh download.
    """
    try:
        from db.repositories.queue import get_queue_item, update_queue_item

        item = get_queue_item(queue_id)
        if not item:
            return {"success": False, "error": "Queue item not found"}

        status = str(item.get("status") or "").lower()

        # Cancel the in-flight slskd transfer (best-effort) when one exists.
        found_filename = (item.get("found_filename") or "").strip()
        if found_filename or status in ("downloading", "searching", "processing"):
            try:
                from api_clients.slskd_http import get_slskd_client
                from services.downloads.slskd_service import SlskdService

                client = get_slskd_client()
                if client is not None:
                    slskd = SlskdService(http_client=client)
                    for transfer in slskd.get_active_downloads():
                        filename = (transfer.get("filename") or "").strip()
                        if filename and (
                            filename.replace("\\", "/").lower()
                            == (found_filename or "").replace("\\", "/").lower()
                        ):
                            transfer_id = str(transfer.get("id") or "")
                            username = str(transfer.get("username") or "")
                            if transfer_id and username:
                                slskd.cancel_download(username, transfer_id, remove=True)
                            break
            except Exception as exc:
                logger.debug("[QUEUE] Cancel transfer best-effort failed for %s: %s", queue_id, exc)

        updated = update_queue_item(
            queue_id,
            status="failed",
            failure_reason="Cancelled by user",
        )

        return {"success": updated is not None, "queue_id": queue_id, "status": "failed"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}

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



def process_queue_item(item: dict[str, Any]) -> dict[str, Any]:
    """Process a single queue item — search, download, and import.

    This is the main processor function called by the queue orchestrator.
    Delegates to the pipeline service for the actual search/download work.

    Args:
        item: Queue item dict with at least ``id``, ``artist``, ``title``.

    Returns:
        Dict with ``success``, optionally ``error`` and ``queue_id``.
    """
    queue_id = item.get("id")
    if not queue_id:
        return {"success": False, "error": "missing_queue_id"}

    try:
        from services.downloads.download_pipeline_service import (
            process_queue_item as _pipeline_process,
        )
        from api_clients.slskd_http import get_slskd_client
        from services.downloads.slskd_service import SlskdService

        # get_slskd_client() returns the raw HTTP client; the pipeline needs
        # the higher-level SlskdService (search_and_filter/download_file).
        http_client = get_slskd_client()
        if http_client is None:
            logger.warning(
                "Soulseek unavailable — returning queue item %s to queue (pending auto retry)",
                queue_id,
            )
            try:
                from helpers.logging_config import log_unified
                from services.queue.queue_diagnostics_service import log_queue_event
                _queue_msg = f"{(item.get('artist') or '')} - {(item.get('title') or '')} → failed: soulseek_unavailable (slskd disabled/misconfigured)"
                log_unified(f"[QUEUE] {_queue_msg}")
                log_queue_event("failed", _queue_msg, queue_id=queue_id)
            except Exception:
                pass
            # mark_failed (not a raw status update) sends the item back to
            # 'queued' with next_retry_at set so it re-enters the queue
            # automatically after the retry delay while slskd stays down.
            mark_failed(queue_id, "soulseek_unavailable")
            return {
                "success": False,
                "error": "soulseek_unavailable",
                "queue_id": queue_id,
            }

        slskd = SlskdService(http_client=http_client)
        result = _pipeline_process(item, slskd)
        result.setdefault("queue_id", queue_id)
        return result
    except ImportError:
        # Pipeline not available — minimal fallback
        from db.repositories.queue import update_queue_item
        artist = (item.get("artist") or "").strip()
        title = (item.get("title") or "").strip()
        logger.warning(
            "Pipeline service not available — marking queue item %s (%s - %s) as unmatched",
            queue_id, artist, title,
        )
        update_queue_item(queue_id, status="unmatched", notes="Pipeline unavailable")
        return {"success": True, "skipped": True, "queue_id": queue_id, "reason": "pipeline_unavailable"}
    except Exception as exc:
        # exc_info=True so the log shows the full traceback — a bare message
        # like "name 'query' is not defined" gives no clue which frame raised.
        logger.error("Queue item %s processing failed: %s", queue_id, exc, exc_info=True)
        return {"success": False, "error": str(exc), "queue_id": queue_id}


def get_completed_queue_items(
    limit: int = 50,
) -> list[dict[str, Any]]:
    return get_completed_queue(limit)


# =============================================================================
# MANUAL PROCESSING (process-one / process-albums endpoints)
# =============================================================================

def process_single_file(data: Dict[str, Any]) -> Dict[str, Any]:
    """Process a single downloaded file: tag it and move it to the library.

    Modern re-implementation of the legacy ``process-one`` endpoint.  Finds
    the queue item owning ``path`` and delegates to the standard completed-
    item pipeline (tag update -> move -> mark imported).
    """
    try:
        path = str((data or {}).get("path") or "").strip()
        if not path:
            return {"success": False, "error": "path is required"}

        import os
        if not os.path.isfile(path):
            return {"success": False, "error": f"File not found: {path}"}

        from db.repositories.queue import get_queue_item_by_path
        item = get_queue_item_by_path(path)
        if not item:
            return {
                "success": False,
                "error": "No queue item found for this file",
            }

        # Ensure the pipeline sees a usable file path even when the item only
        # has the download copy recorded in ``found_filename``.
        item["file_path"] = item.get("file_path") or item.get("found_filename")
        return process_completed_queue_item(item)
    except Exception as exc:
        logger.exception("[process_single_file] failed")
        return {"success": False, "error": str(exc)}


def process_albums(data: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Process downloaded album groups: tag + move every track to the library.

    Modern re-implementation of the legacy ``process-albums`` endpoint.
    Groups queue items that have a downloaded file by (album artist, album)
    and runs each track through the standard import pipeline.  Only fully
    downloaded (``completed`` / ``unmatched``) items are touched — anything
    still mid-flight is left alone.
    """
    stats: Dict[str, Any] = {"checked": 0, "processed": 0, "errors": []}
    try:
        from db.repositories.queue import get_completed_queue

        items = get_completed_queue(limit=200)

        albums: Dict[tuple, list] = {}
        for item in items:
            if item.get("status") not in ("completed", "unmatched"):
                continue
            if not (item.get("file_path") or item.get("found_filename")):
                continue
            artist = item.get("album_artist") or item.get("artist") or "Unknown"
            album = item.get("album") or ""
            key = (str(album).strip().lower(), str(artist).strip().lower())
            albums.setdefault(key, []).append(item)

        for album_items in albums.values():
            stats["checked"] += 1
            processed = True
            for item in album_items:
                item["file_path"] = item.get("file_path") or item.get("found_filename")
                result = process_completed_queue_item(item)
                if not result.get("success"):
                    processed = False
                    stats["errors"].append(str(result.get("error") or "unknown error"))
            if processed:
                stats["processed"] += 1

        return {
            "success": True,
            "stats": stats,
            "message": (
                f"Checked {stats['checked']} albums. "
                f"{stats['processed']} processed."
            ),
        }
    except Exception as exc:
        logger.exception("[process_albums] failed")
        return {"success": False, "error": str(exc)}
