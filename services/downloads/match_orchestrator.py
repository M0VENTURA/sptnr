"""MBID batch match orchestrator.

Applies a MusicBrainz release match to multiple queue items:
- Batch-updates queue rows with release metadata.
- Prefetches cover art and release details.
- Optionally expands release tracks into individual queue entries.

Called by the UI routes when a user confirms a MusicBrainz match.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List

import structlog
from sqlalchemy import text

from db.engine import db_session
from db.repositories.queue import get_album_queue_tracks
from services.downloads.download_matching_service import (
    _expand_release_tracks,
    _prefetch_mbid_metadata_batch,
    _row_get,
)
from services.enrichment.musicbrainz_service import fetch_release_metadata

logger = structlog.get_logger(__name__)


def apply_mbid_match_batch(
    queue_ids: List[int],
    new_mbid: str,
    new_artist: str | None = None,
    new_album: str | None = None,
    expand_tracks: bool = False,
) -> Dict[str, Any]:
    """Apply one MusicBrainz release match to multiple queue items."""
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

    try:
        with db_session() as session:
            first_row = session.execute(
                text("SELECT artist, album FROM download_queue WHERE id = :qid"),
                {"qid": queue_ids[0]},
            ).fetchone()

            if not first_row:
                return {
                    "success": False,
                    "status_code": 404,
                    "error": "Queue item not found",
                }

            mapping = first_row._mapping
            fallback_artist = mapping.get("artist")
            fallback_album = mapping.get("album")

            target_artist = new_artist or fallback_artist
            target_album = new_album or fallback_album

            release_year = None
            mbid_import_group = f"mbid_{new_mbid}"

            ids_placeholders = ", ".join(f":qid{i}" for i in range(len(queue_ids)))

            result = session.execute(
                text(f"""
                    UPDATE download_queue
                    SET release_mbid = :new_mbid,
                        release_id = :new_mbid,
                        release_source = 'musicbrainz',
                        album_artist = :target_artist,
                        album = :target_album,
                        release_year = COALESCE(release_year, :release_year),
                        import_group = :import_group,
                        status = CASE
                            WHEN TRIM(COALESCE(status, '')) = '' OR status = 'matched' THEN 'queued'
                            WHEN status = 'unmatched' AND TRIM(COALESCE(file_path, '')) != '' THEN 'matched'
                            WHEN status = 'unmatched' THEN 'queued'
                            ELSE status
                        END,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id IN ({ids_placeholders})
                """),
                {
                    "new_mbid": new_mbid,
                    "target_artist": target_artist,
                    "target_album": target_album,
                    "release_year": release_year,
                    "import_group": mbid_import_group,
                    **{f"qid{i}": qid for i, qid in enumerate(queue_ids)},
                },
            )

            updated_count = result.rowcount or 0

    except Exception as exc:
        logger.error("Failed applying MBID batch match", error=str(exc), exc_info=True)
        return {
            "success": False,
            "status_code": 500,
            "error": str(exc),
        }

    # Queue rows are updated immediately, then metadata enrichment happens asynchronously.
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
