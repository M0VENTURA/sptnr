"""
Repository helpers for queue database operations.

✅ ALL database access is centralized here.
✅ Services must call this repository — no direct SQL elsewhere.
"""

from __future__ import annotations

import logging
import json
import os
from typing import Any, Dict, List, Optional
from datetime import datetime
from sqlalchemy import text

from db.engine import db_session
from db.utils import row_get

from helpers.config_helpers import get_config
from services.downloads.download_scan_service import resolve_downloads_dir
from helpers.normalization_service import normalize_match_text

from services.queue.queue_constraints import (
    COMPLETED_QUEUE_STATUSES,
)

logger = logging.getLogger(__name__)


# =============================================================================
# CORE GETTERS
# =============================================================================

def _row_to_dict(
    row,
    cursor,
) -> dict[str, Any]:
    if hasattr(row, "keys"):
        return dict(row)
    return dict(zip([c[0] for c in cursor.description], row))


def get_queue_item(queue_id: int) -> Optional[Dict[str, Any]]:
    try:
        with db_session() as session:
            result = session.execute(
                text("SELECT * FROM download_queue WHERE id = :id"),
                {"id": queue_id},
            )
            row = result.fetchone()
            return dict(row._mapping) if row else None
    except Exception as e:
        logger.error(f"Failed to fetch queue item {queue_id}: {e}")
        return None


def get_completed_group_queue_items(import_group: str) -> List[Dict[str, Any]]:
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT id, file_path, artist, album, title, track_number, disc_number, album_artist, year
                    FROM download_queue
                    WHERE import_group = :group AND status = 'completed'
                    ORDER BY id
                """),
                {"group": import_group},
            )
            rows = result.fetchall()
            return [dict(r._mapping) for r in rows]
    except Exception as exc:
        logger.error(f"[get_completed_group_queue_items] {exc}")
        return []


def item_has_status(queue_id: int, expected_status: str) -> bool:
    try:
        with db_session() as session:
            result = session.execute(
                text("SELECT status FROM download_queue WHERE id = :id"),
                {"id": queue_id},
            )
            row = result.fetchone()
            return str(row[0]).lower() == str(expected_status).lower() if row else False
    except Exception as e:
        logger.error(f"Failed to check status for queue item {queue_id}: {e}")
        return False


def get_queue_file_path(queue_id: int) -> Optional[str]:
    try:
        with db_session() as session:
            result = session.execute(
                text("SELECT file_path FROM download_queue WHERE id=:id"),
                {"id": queue_id},
            )
            row = result.fetchone()
            return row[0] if row else None
    except Exception as e:
        logger.error(f"[get_queue_file_path] {e}")
        return None


# =============================================================================
# CORE MUTATIONS
# =============================================================================

def insert_queue_item(
    artist: str,
    title: str,
    album: str | None = None,
    source: str = "soulseek",
    priority: int = 5,
    **kwargs,
) -> dict[str, Any]:
    """Insert a new item into the download queue.

    Args:
        artist: Artist name.
        title: Track title.
        album: Optional album name.
        source: Download source (default ``"soulseek"``).
        priority: Queue priority (1-10, default 5).
        **kwargs: Additional fields (track_number, disc_number, album_artist,
                  year, release_id, release_mbid, recording_mbid, duration, etc.).

    Returns:
        The inserted row as a dict.
    """
    with db_session() as session:
        result = session.execute(
            text("""
                INSERT INTO download_queue
                    (artist, title, album, source, priority, track_number, disc_number,
                     album_artist, year, release_id, release_mbid, recording_mbid,
                     duration, status, created_at, updated_at)
                VALUES (:artist, :title, :album, :source, :priority, :track_number, :disc_number,
                        :album_artist, :year, :release_id, :release_mbid, :recording_mbid,
                        :duration, 'queued', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                RETURNING id
            """),
            {
                "artist": artist, "title": title, "album": album, "source": source,
                "priority": priority,
                "track_number": kwargs.get("track_number"),
                "disc_number": kwargs.get("disc_number"),
                "album_artist": kwargs.get("album_artist"),
                "year": kwargs.get("year"),
                "release_id": kwargs.get("release_id"),
                "release_mbid": kwargs.get("release_mbid"),
                "recording_mbid": kwargs.get("recording_mbid"),
                "duration": kwargs.get("duration"),
            },
        )
        new_id = result.scalar()

    return get_queue_item(new_id) or {}


def update_queue_item(queue_id: int, **kwargs) -> Optional[Dict[str, Any]]:
    if not kwargs:
        return get_queue_item(queue_id)

    set_clauses = []
    params = {}

    for key, value in kwargs.items():
        set_clauses.append(f"{key} = :{key}")
        params[key] = json.dumps(value) if isinstance(value, (dict, list)) else value

    params["id"] = queue_id

    query = text(f"""
        UPDATE download_queue
        SET {', '.join(set_clauses)}, updated_at = CURRENT_TIMESTAMP
        WHERE id = :id
        RETURNING *
    """)

    try:
        with db_session() as session:
            result = session.execute(query, params)
            row = result.fetchone()
            return dict(row._mapping) if row else None
    except Exception as e:
        logger.error(f"[update_queue_item] {e}")
        return None


def delete_queue_item(queue_id: int) -> bool:
    try:
        with db_session() as session:
            result = session.execute(
                text("DELETE FROM download_queue WHERE id=:id"),
                {"id": queue_id},
            )
            return result.rowcount > 0
    except Exception as e:
        logger.error(f"[delete_queue_item] {e}")
        return False


# =============================================================================
# GROUP / MATCHING HELPERS
# =============================================================================# =============================================================================
# GROUP / MATCHING HELPERS
# =============================================================================

def get_items_by_group(group_id: str) -> List[Dict[str, Any]]:
    try:
        with db_session() as session:
            result = session.execute(
                text("SELECT * FROM download_queue WHERE import_group = :group"),
                {"group": group_id},
            )
            return [dict(r._mapping) for r in result.fetchall()]
    except Exception as e:
        logger.error(f"[get_items_by_group] {e}")
        return []


def apply_release_mbid(queue_ids: list, mbid: str, artist: str, album: str) -> int:
    if not queue_ids:
        return 0

    try:
        with db_session() as session:
            placeholders = ",".join([f":id_{i}" for i in range(len(queue_ids))])
            params = {"mbid": mbid, "artist": artist, "album": album}
            params.update({f"id_{i}": qid for i, qid in enumerate(queue_ids)})
            result = session.execute(
                text(f"""
                    UPDATE download_queue
                    SET release_mbid = :mbid,
                        album_artist = :artist,
                        album = :album
                    WHERE id IN ({placeholders})
                """),
                params,
            )
            return result.rowcount
    except Exception as e:
        logger.error(f"[apply_release_mbid] {e}")
        return 0


# =============================================================================
# STATUS / PROCESSING
# =============================================================================
# =============================================================================


def get_queue_status_counts() -> Dict[str, int]:
    try:
        with db_session() as session:
            result = session.execute(
                text("SELECT status, COUNT(*) FROM download_queue GROUP BY status")
            )
            return {str(r[0]): int(r[1]) for r in result.fetchall()}
    except Exception as e:
        logger.error(f"[get_queue_status_counts] {e}")
        return {}


def get_processing_snapshot() -> Dict[str, int]:
    counts = get_queue_status_counts()
    return {
        "queued": counts.get("queued", 0),
        "failed": counts.get("failed", 0),
        "completed": counts.get("completed", 0),
        "total": sum(counts.values())
    }


def get_ready_for_processing(limit: int = 100) -> List[Dict]:
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT *
                    FROM download_queue
                    WHERE status = 'queued'
                    ORDER BY created_at ASC
                    LIMIT :limit
                """),
                {"limit": limit},
            )
            return [dict(r._mapping) for r in result.fetchall()]
    except Exception as e:
        logger.error(f"[get_ready_for_processing] {e}")
        return []
    
    # =============================================================================
# COMPLETED / POST-DOWNLOAD QUEUE
# =============================================================================

def get_completed_queue(limit: int = 50) -> List[Dict[str, Any]]:
    """
    Get completed downloads (and unmatched items) ready for post-processing.

    Includes:
    - completed
    - unmatched
    - possible_duplicate
    - moving
    """
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT *
                    FROM download_queue
                    WHERE status = ANY(:statuses)
                    ORDER BY updated_at DESC
                    LIMIT :limit
                """),
                {"statuses": list(COMPLETED_QUEUE_STATUSES), "limit": limit},
            )

            return [dict(r._mapping) for r in result.fetchall()]

    except Exception as e:
        logger.error(f"[get_completed_queue] {e}")
        return []

# =============================================================================
# GROUP QUERIES
# =============================================================================

    
# queue.py
def get_completed_by_group(group_id: str) -> List[Dict[str, Any]]:
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT id, file_path, artist, album, title,
                           track_number, disc_number, album_artist, year
                    FROM download_queue
                    WHERE import_group = :group
                    AND status = 'completed'
                """),
                {"group": group_id},
            )

            return [dict(r._mapping) for r in result.fetchall()]

    except Exception as e:
        logger.error(f"[get_completed_by_group] {e}")
        return []


# =============================================================================
# STATUS HELPERS (STATE TRANSITIONS)
# =============================================================================

def mark_failed(queue_id: int, reason: str) -> Optional[Dict[str, Any]]:
    return update_queue_item(
        queue_id,
        status="failed",
        failure_reason=reason
    )

def mark_imported(queue_id: int, target_path: str) -> Optional[Dict[str, Any]]:
    return update_queue_item(
        queue_id,
        status="imported",
        music_file_path=target_path,
        copied_individually=1,
        copied_individually_at=datetime.now().isoformat()
    )


def mark_processing(queue_id: int):
    return update_queue_item(queue_id, status="processing")



def mark_completed(queue_id: int):
    return update_queue_item(queue_id, status="completed")

# =============================================================================
# MATCH TARGETS / REMATCH HELPERS
# =============================================================================

def get_queue_match_targets(
    artist: str,
    album: str,
    selected_queue_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Return queue items that can be used as manual rematch targets.
    """

    active_statuses = (
        'queued', 'searching', 'downloading',
        'matched', 'completed',
        'unmatched', 'queried',
        'discovered', 'pending_match',
        'possible_duplicate', 'duplicate'
    )

    try:
        with db_session() as session:

            status_placeholders = ", ".join([f":status_{i}" for i in range(len(active_statuses))])
            params = {f"status_{i}": s for i, s in enumerate(active_statuses)}
            params["artist"] = artist
            params["album"] = album
            params["selected_queue_id"] = selected_queue_id

            result = session.execute(
                text(f"""
                SELECT
                    id,
                    artist,
                    title,
                    album,
                    status,
                    track_number,
                    release_mbid,
                    found_filename,
                    created_at
                FROM download_queue
                WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
                  AND LOWER(COALESCE(NULLIF(album, ''), '')) = LOWER(:album)
                  AND status IN ({status_placeholders})
                ORDER BY
                    CASE
                        WHEN :selected_queue_id IS NOT NULL AND id = :selected_queue_id THEN 0
                        ELSE 1
                    END,
                    CASE
                        WHEN NULLIF(TRIM(COALESCE(track_number, '')), '') ~ '^\\d+$'
                            THEN TRIM(track_number)::integer
                        ELSE 9999
                    END,
                    COALESCE(NULLIF(TRIM(track_number), ''), '9999'),
                    id
                LIMIT 250
                """),
                params,
            )

            return [
                {
                    "id": r[0],
                    "artist": r[1] or '',
                    "title": r[2] or '',
                    "album": r[3] or '',
                    "status": r[4] or '',
                    "track_number": r[5],
                    "release_mbid": r[6] or '',
                    "found_filename": r[7] or '',
                    "created_at": r[8],
                }
                for r in result.fetchall() or []
            ]

    except Exception as e:
        logger.error(f"[get_queue_match_targets] {e}")
        return []

def get_album_queue_tracks(
    artist: str,
    album: str,
) -> list[dict[str, Any]]:
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT id, title, file_path, status, track_number,
                           disc_number, artist, album_artist
                    FROM download_queue
                    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
                      AND LOWER(COALESCE(album, '')) = LOWER(:album)
                    ORDER BY
                        CASE WHEN NULLIF(TRIM(COALESCE(track_number, '')), '') ~ '^\d+$'
                            THEN TRIM(track_number)::integer ELSE 9999 END,
                        title
                """),
                {"artist": artist, "album": album},
            )
            return [dict(r._mapping) for r in result.fetchall()]
    except Exception as e:
        logger.error(f"[get_album_queue_tracks] {e}")
        return []




# =============================================================================
# BACKWARD-COMPATIBLE REDIRECTS
# (functions moved to db.repositories.queue_admin)
# =============================================================================
from db.repositories.queue_admin import (
    clear_queue, purge_all, get_imported, cleanup,
    cleanup_copied_sources, cleanup_orphaned,
    count_pending_by_release, verify_and_prune,
    apply_release_mbid,
    find_existing_discovered_file, find_duplicate_queue_item,
    insert_discovered_file, delete_duplicate_queue_entries,
    mark_in_collection, get_active_queue_signatures,
    get_queue_items_by_folder, slskd_eligibility_diagnostics,
    delete_folder, remove_group, reset_moving,
)
