"""Download pipeline service.

Orchestrates the end-to-end download processing pipeline:
1. Fetch ready-to-process queue items.
2. Resolve MusicBrainz release metadata.
3. Execute downloads via slskd.
4. Update queue status and library records.

Coordinates between ``downloads/slskd_service``, ``enrichment/musicbrainz_service``,
and ``db/repositories``.
"""

from __future__ import annotations

import logging

from typing import Any, Dict

from services.downloads.slskd_service import SlskdService
from services.enrichment.musicbrainz_service import (
    fetch_musicbrainz_release_metadata,
)
from db.repositories.queue import (
    update_queue_item,
    mark_failed,
    mark_processing,
    get_ready_for_processing,
)
from db.repositories.library import upsert_musicbrainz_release
from services.infrastructure.filesystem_service import (
    create_monitoring_folder,
)

from services.queue.queue_processing_service import add_release_tracks_to_queue

logger = logging.getLogger(__name__)


# =============================================================================
# QUERY BUILDER
# =============================================================================

def build_search_query(item: dict) -> str:
    artist = item.get("artist")
    title = item.get("title")
    album = item.get("album")

    parts = [artist, title]
    if album:
        parts.append(album)

    return " ".join(p for p in parts if p)


# =============================================================================
# PROCESS SINGLE ITEM
# =============================================================================

def process_queue_item(item: dict, slskd: SlskdService) -> dict:
    queue_id = item.get("id")
    logger.debug("[DOWNLOAD_PIPELINE] Processing queue item: %s", queue_id)

    if not queue_id:
        logger.error("[PIPELINE] Missing queue_id in item: %s", item)
        return {
            "success": False,
            "error": "missing_queue_id"
        }

    query = build_search_query(item)

    try:
        # ✅ mark processing
        mark_processing(queue_id)

        # ✅ search
        results = slskd.search_and_filter(query)

        if not results:
            mark_failed(queue_id, "no_results")
            return {"success": False, "status": "no_results"}

        best = results[0]

        # ✅ update searching → downloading
        update_queue_item(queue_id, status="downloading")

        # ✅ download
        success = slskd.download_file(
            best["username"],
            best["filename"],
            size=int(best.get("size_mb", 0) * 1024 * 1024)
        )

        if not success:
            mark_failed(queue_id, "download_failed")
            return {"success": False, "status": "download_failed"}

        # ✅ success → completed
        update_queue_item(
            queue_id,
            found_filename=best["filename"],
            status="completed"
        )

        return {
            "success": True,
            "status": "completed",
            "query": query,
            "match": best
        }

    except Exception as e:
        logger.error("[PIPELINE] Error processing %s: %s", queue_id, e, exc_info=True)
        mark_failed(queue_id, str(e))
        return {"success": False, "error": str(e)}


# =============================================================================
# BULK PROCESSOR
# =============================================================================

def run_pipeline(slskd: SlskdService, limit: int = 10) -> Dict[str, Any]:
    queue = get_ready_for_processing(limit)

    results = {
        "processed": 0,
        "success": 0,
        "failed": 0,
        "details": []
    }

    for item in queue:
        result = process_queue_item(item, slskd)

        results["processed"] += 1
        results["details"].append(result)

        if result.get("success"):
            results["success"] += 1
        else:
            results["failed"] += 1

    return results


# =============================================================================
# SYNC TRANSFERS
# =============================================================================

def sync_transfers(slskd: SlskdService) -> Dict[str, Any]:
    transfers = slskd.get_active_downloads()

    updated = 0

    for t in transfers:
        try:
            queue_id = t.get("queue_id")

            if not queue_id:
                continue

            update_queue_item(
                queue_id,
                status="downloading",
                progress=t.get("progress"),
                speed=t.get("speed"),
            )

            updated += 1

        except Exception as e:
            logger.debug(f"[SYNC] failed to update transfer: {e}")

    return {
        "success": True,
        "updated": updated,
        "total": len(transfers)
    }

def start_release_download(release_id, release_title, artist, method='slskd'):

    try:
        logger.info(f"[START_DOWNLOAD] {release_id}")

        mb_data = fetch_musicbrainz_release_metadata(release_id)

        if not mb_data:
            return {"success": False, "error": "MusicBrainz fetch failed"}

        release_year = mb_data.get("release_year")
        tracks = mb_data.get("tracks", [])
        total_tracks = len(tracks)

        release_album_artist = mb_data.get("artist") or artist

        monitoring_folder = create_monitoring_folder(
            artist, release_title, release_year
        )

        mb_release_db_id = upsert_musicbrainz_release(
            release_id,
            release_title,
            artist,
            release_year,
            total_tracks,
            monitoring_folder,
            method,
            album_artist=release_album_artist,
            release_source='musicbrainz',
        )

        queue_source = 'soulseek' if method.lower() == 'slskd' else 'qbittorrent'

        queue_ids = add_release_tracks_to_queue(
            release_id,
            tracks,
            artist,
            release_title,
            album_artist=release_album_artist,
            queue_source=queue_source,
        )

        return {
            "success": True,
            "mb_release_db_id": mb_release_db_id,
            "queue_items_created": len(queue_ids),
        }

    except Exception as e:
        logger.error("[START_DOWNLOAD] Failed: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}