"""MBID batch match orchestrator.

Applies a MusicBrainz release match to multiple queue items:
- Batch-updates queue rows with release metadata.
- Prefetches cover art and release details.
- Optionally expands release tracks into individual queue entries.

Called by the UI routes when a user confirms a MusicBrainz match.
"""

import logging
import threading
from typing import Any, Dict, List
from sqlalchemy import text
from db.engine import db_session
from db.utils import get_db_connection
from services.downloads.download_matching_service import _expand_release_tracks, _prefetch_mbid_metadata_batch, _row_get
from db.repositories.queue import get_album_queue_tracks
from services.enrichment.musicbrainz_service import fetch_release_metadata

logger = logging.getLogger(__name__)


def apply_mbid_match_batch(
    queue_ids: List[int],
    new_mbid: str,
    new_artist: str | None = None,
    new_album: str | None = None,
    expand_tracks: bool = False,
) -> Dict[str, Any]:
    """
    Apply one MusicBrainz release match to multiple queue items.

    Migrated from:
        /api/queue/apply-mbid-match-batch

    Responsibilities:
    - validate queue IDs
    - resolve fallback artist/album
    - update queue rows with release MBID
    - preserve old status transition behavior
    - assign import_group
    - prefetch MusicBrainz release metadata
    - optionally expand release tracks into queue rows
    """

    queue_ids = [
        int(qid)
        for qid in queue_ids
        if str(qid).isdigit() and int(qid) > 0
    ]

    # Preserve input order while deduplicating.
    queue_ids = list(dict.fromkeys(queue_ids))

    new_mbid = (new_mbid or "").strip()
    new_artist = (new_artist or "").strip()
    new_album = (new_album or "").strip()

    if not queue_ids:
        return {
            "success": False,
            "status_code": 400,
            "error": "queue_ids is required",
        }

    if not new_mbid:
        return {
            "success": False,
            "status_code": 400,
            "error": "new_mbid is required",
        }

    conn = get_db_connection()
    cursor = conn.cursor()
    placeholder = "%s"

    try:
        first_queue_id = queue_ids[0]

        cursor.execute(
            f"""
            SELECT artist, album
            FROM download_queue
            WHERE id = {placeholder}
            """,
            (first_queue_id,),
        )

        first_row = cursor.fetchone()

        if not first_row:
            return {
                "success": False,
                "status_code": 404,
                "error": "Queue item not found",
            }

        fallback_artist = _row_get(first_row, "artist", 0)
        fallback_album = _row_get(first_row, "album", 1)

        target_artist = new_artist or fallback_artist
        target_album = new_album or fallback_album

        release_year = None
        mbid_import_group = f"mbid_{new_mbid}"

        ids_placeholders = ", ".join([placeholder] * len(queue_ids))

        cursor.execute(
            f"""
            UPDATE download_queue
            SET release_mbid = {placeholder},
                release_id = {placeholder},
                release_source = 'musicbrainz',
                album_artist = {placeholder},
                album = {placeholder},
                release_year = COALESCE(release_year, {placeholder}),
                import_group = {placeholder},
                status = CASE
                    WHEN TRIM(COALESCE(status, '')) = '' OR status = 'matched' THEN 'queued'
                    WHEN status = 'unmatched' AND TRIM(COALESCE(file_path, '')) != '' THEN 'matched'
                    WHEN status = 'unmatched' THEN 'queued'
                    ELSE status
                END,
                updated_at = CURRENT_TIMESTAMP
            WHERE id IN ({ids_placeholders})
            """,
            (
                new_mbid,
                new_mbid,
                target_artist,
                target_album,
                release_year,
                mbid_import_group,
                *queue_ids,
            ),
        )

        updated_count = cursor.rowcount or 0

        conn.commit()

    except Exception as exc:
        conn.rollback()
        logger.exception("[QUEUE_MATCH_BATCH] Failed applying MBID batch")

        return {
            "success": False,
            "status_code": 500,
            "error": str(exc),
        }

    finally:
        conn.close()

    # Preserve old behavior:
    # queue rows are updated immediately, then metadata enrichment happens separately.
    threading.Thread(
        target=_prefetch_mbid_metadata_batch,
        args=(new_mbid, list(queue_ids)),
        daemon=True,
        name=f"queue-match-batch-prefetch-{new_mbid[:8]}",
    ).start()

    if expand_tracks:
        threading.Thread(
            target=_expand_release_tracks,
            args=(new_mbid, target_artist, target_album),
            daemon=True,
            name=f"queue-match-batch-expand-{new_mbid[:8]}",
        ).start()

    return {
        "success": True,
        "message": "Folder queue match updated",
        "queue_ids": queue_ids,
        "updated_count": updated_count,
        "failed_count": max(len(queue_ids) - int(updated_count or 0), 0),
        "release_mbid": new_mbid,
        "artist": target_artist,
        "album": target_album,
        "tracks_pending": bool(expand_tracks),
        "expand_tracks": bool(expand_tracks),
        "status_code": 200,
    }