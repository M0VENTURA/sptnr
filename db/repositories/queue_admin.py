"""
Repository helpers for queue admin/management operations.

Split from ``db/repositories/queue.py`` (1008 lines → 2 files).

Contains:
- Clear, purge, and bulk cleanup
- Filesystem verification and pruning
- Discovery file management
- Duplicate detection and merging
- Folder-level operations
- Diagnostics

✅ All database access uses ``db_cursor`` from ``db.context``.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from db.context import db_cursor
from db.utils import row_get
from helpers.config_helpers import get_config
from services.downloads.download_scan_service import resolve_downloads_dir
from helpers.normalization_service import normalize_match_text
from services.queue.queue_constraints import COMPLETED_QUEUE_STATUSES

logger = logging.getLogger(__name__)


# =============================================================================
# BULK / CLEANUP
# =============================================================================

def clear_queue(filters: Optional[dict] = None) -> dict:
    try:
        with db_cursor(commit=True) as (conn, cursor):
            if filters and "status" in filters:
                cursor.execute("DELETE FROM download_queue WHERE status = %s", (filters["status"],))
                return {"success": True, "queue_items_deleted": cursor.rowcount}
            cursor.execute("DELETE FROM folder_track_matches")
            cursor.execute("DELETE FROM folder_album_matches")
            cursor.execute("DELETE FROM download_queue")
            return {"success": True}
    except Exception as e:
        logger.error(f"[clear_queue] {e}", exc_info=True)
        return {"success": False, "error": str(e)}


def purge_all() -> dict:
    try:
        with db_cursor(commit=True) as (conn, cursor):
            cursor.execute("DELETE FROM download_queue")
            cursor.execute("DELETE FROM slskd_search_logs")
            return {"success": True}
    except Exception as e:
        logger.error(f"[purge_all] {e}")
        return {"success": False, "error": str(e)}


def get_imported(limit: int = 50) -> List[Dict[str, Any]]:
    try:
        with db_cursor() as (conn, cursor):
            cursor.execute(
                """SELECT *
                   FROM download_queue
                   WHERE status = 'imported'
                   ORDER BY updated_at DESC
                   LIMIT %s""",
                (limit,)
            )
            rows = cursor.fetchall()
            return [dict(zip([c[0] for c in cursor.description], r)) for r in rows]
    except Exception as e:
        logger.error(f"[get_imported] {e}")
        return []


def cleanup() -> dict:
    try:
        with db_cursor(commit=True) as (conn, cursor):
            cursor.execute("""
                DELETE FROM download_queue
                WHERE status = 'failed'
                  AND updated_at < NOW() - INTERVAL '7 days'
            """)
            return {"success": True, "deleted": cursor.rowcount}
    except Exception as e:
        logger.error(f"[cleanup] {e}")
        return {"success": False, "error": str(e)}


def cleanup_copied_sources() -> dict:
    deleted_paths: list[str] = []
    scanned_count = 0
    deleted_count = 0

    def _delete_if_exists(path: str) -> bool:
        if os.path.exists(path):
            try:
                os.remove(path)
                return True
            except Exception:
                return False
        return False

    try:
        with db_cursor(commit=True) as (conn, cursor):
            cursor.execute("""
                SELECT id, file_path, found_filename
                FROM download_queue
                WHERE status = 'imported'
                  AND (file_path IS NOT NULL OR found_filename IS NOT NULL)
            """)
            items = cursor.fetchall()

            for row in items:
                scanned_count += 1
                queue_id = row_get(row, "id", 0)
                file_path = row_get(row, "file_path", 1)
                found_filename = row_get(row, "found_filename", 2)
                downloads_root = os.path.abspath(resolve_downloads_dir())
                deleted = False

                if file_path and _delete_if_exists(file_path):
                    deleted = True
                    deleted_paths.append(file_path)

                if not deleted and found_filename and os.path.isdir(downloads_root):
                    for root, _dirs, files in os.walk(downloads_root):
                        if found_filename in files:
                            candidate = os.path.join(root, found_filename)
                            if _delete_if_exists(candidate):
                                deleted = True
                                deleted_paths.append(candidate)
                                break

                if deleted:
                    deleted_count += 1
                    cursor.execute(
                        "UPDATE download_queue SET updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                        (queue_id,)
                    )

            return {
                "success": True,
                "scanned": scanned_count,
                "deleted": deleted_count,
                "deleted_paths": deleted_paths,
            }
    except Exception as e:
        logger.error(f"[cleanup_copied_sources] {e}")
        return {"success": False, "error": str(e)}


def cleanup_orphaned(data: dict) -> dict:
    try:
        with db_cursor() as (conn, cursor):
            downloads_root = os.path.abspath(resolve_downloads_dir())
            perform_cleanup = bool(data.get("cleanup", False))
            filter_artist = (data.get("artist") or "").strip().lower()

            cursor.execute("""
                SELECT id, file_path, found_filename, artist, title
                FROM download_queue
                WHERE status IN ('imported', 'completed')
            """)
            items = cursor.fetchall()
            orphaned_files = []
            deleted_files = []

            for row in items:
                queue_id = row_get(row, "id", 0)
                file_path = row_get(row, "file_path", 1)
                found_filename = row_get(row, "found_filename", 2)
                artist = (row_get(row, "artist", 3) or "").lower()
                title = row_get(row, "title", 4)

                if filter_artist and filter_artist not in artist:
                    continue

                actual_file = None
                if file_path and os.path.exists(file_path):
                    actual_file = file_path
                if not actual_file and found_filename:
                    for root, _dirs, files in os.walk(downloads_root):
                        if found_filename in files:
                            actual_file = os.path.join(root, found_filename)
                            break

                if actual_file:
                    orphaned_files.append({
                        "queue_id": queue_id,
                        "file_path": actual_file,
                        "artist": artist,
                        "title": title,
                    })
                    if perform_cleanup:
                        try:
                            os.remove(actual_file)
                            deleted_files.append(actual_file)
                        except Exception:
                            pass

            return {
                "success": True,
                "orphaned_count": len(orphaned_files),
                "deleted_count": len(deleted_files),
                "orphaned_files": orphaned_files,
                "deleted_files": deleted_files,
            }
    except Exception as e:
        logger.error(f"[cleanup_orphaned] {e}")
        return {"success": False, "error": str(e)}


def count_pending_by_release(release_mbid: str) -> int:
    with db_cursor() as (_, cursor):
        cursor.execute("""
            SELECT COUNT(*)
            FROM download_queue
            WHERE (release_mbid = %s OR release_id = %s)
              AND status NOT IN (
                  'completed', 'imported', 'in_collection',
                  'removed', 'cancelled', 'deleted'
              )
        """, (release_mbid, release_mbid))
        return cursor.fetchone()[0] or 0


def verify_and_prune(data: dict) -> dict:
    try:
        with db_cursor(commit=True) as (conn, cursor):
            dry_run = bool(data.get("dry_run", True))
            filter_artist = (data.get("filter_artist") or "").strip().lower()

            cursor.execute("""
                SELECT id, file_path, found_filename, artist, title, status
                FROM download_queue
            """)
            items = cursor.fetchall()
            checked_count = 0
            missing_items = []
            pruned_count = 0
            downloads_root = os.path.abspath(resolve_downloads_dir())

            for row in items:
                queue_id = row_get(row, "id", 0)
                file_path = row_get(row, "file_path", 1)
                found_filename = row_get(row, "found_filename", 2)
                artist = (row_get(row, "artist", 3) or "").lower()
                title = row_get(row, "title", 4)

                if filter_artist and filter_artist not in artist:
                    continue

                checked_count += 1
                exists = file_path and os.path.exists(file_path)

                if not exists and found_filename:
                    for root, _dirs, files in os.walk(downloads_root):
                        if found_filename in files:
                            exists = True
                            break

                if not exists:
                    missing_items.append({
                        "id": queue_id,
                        "artist": artist,
                        "title": title,
                        "file_path": file_path,
                    })
                    if not dry_run:
                        cursor.execute("DELETE FROM download_queue WHERE id=%s", (queue_id,))
                        pruned_count += 1

            return {
                "success": True,
                "checked_count": checked_count,
                "missing_count": len(missing_items),
                "pruned_count": pruned_count,
                "missing_items": missing_items,
                "dry_run": dry_run,
            }
    except Exception as e:
        logger.error(f"[verify_and_prune] {e}")
        return {"success": False, "error": str(e)}


# =============================================================================
# DISCOVERY / AUTO-DISCOVER HELPERS
# =============================================================================

def find_existing_discovered_file(*, file_path: str, filename: str, rel_path: str) -> Optional[Dict[str, Any]]:
    try:
        with db_cursor() as (_, cursor):
            cursor.execute(
                """
                SELECT * FROM download_queue
                WHERE file_path = %s
                   OR found_filename = %s
                   OR found_filename = %s
                   OR found_filename = %s
                LIMIT 1
                """,
                (file_path, filename, rel_path, file_path),
            )
            row = cursor.fetchone()
            return dict(zip([c[0] for c in cursor.description], row)) if row else None
    except Exception as e:
        logger.error(f"[find_existing_discovered_file] {e}")
        return None


def find_duplicate_queue_item(*, artist: str, title: str, album: str | None) -> Optional[Dict[str, Any]]:
    try:
        with db_cursor() as (_, cursor):
            cursor.execute(
                """
                SELECT * FROM download_queue
                WHERE LOWER(artist) = LOWER(%s)
                  AND LOWER(title) = LOWER(%s)
                  AND LOWER(COALESCE(album, '')) = LOWER(COALESCE(%s, ''))
                  AND status NOT IN ('removed', 'cancelled')
                LIMIT 1
                """,
                (artist, title, album),
            )
            row = cursor.fetchone()
            return dict(zip([c[0] for c in cursor.description], row)) if row else None
    except Exception as e:
        logger.error(f"[find_duplicate_queue_item] {e}")
        return None


def insert_discovered_file(
    *,
    artist: str,
    title: str,
    album: str,
    album_artist: str | None,
    track_number: str | None,
    disc_number: str | None,
    year: str | None,
    duration: int | None,
    file_path: str,
    filename: str,
    import_group: str,
) -> Dict[str, Any]:
    from db.repositories.queue import insert_queue_item
    return insert_queue_item(
        artist=artist,
        title=title,
        album=album,
        album_artist=album_artist or artist,
        track_number=track_number,
        disc_number=disc_number,
        year=year,
        duration=duration,
        file_path=file_path,
        found_filename=filename,
        source="discovered",
        status="unmatched",
        import_group=import_group,
        import_type="album",
    )


def delete_duplicate_queue_entries(*, keep_id: int, artist: str, title: str, album: str | None) -> int:
    try:
        with db_cursor(commit=True) as (_, cursor):
            cursor.execute(
                """
                DELETE FROM download_queue
                WHERE id <> %s
                  AND LOWER(artist) = LOWER(%s)
                  AND LOWER(title) = LOWER(%s)
                  AND LOWER(COALESCE(album, '')) = LOWER(COALESCE(%s, ''))
                  AND status NOT IN ('removed', 'cancelled', 'deleted', 'imported', 'in_collection')
                """,
                (keep_id, artist, title, album),
            )
            return int(cursor.rowcount or 0)
    except Exception as e:
        logger.error(f"[delete_duplicate_queue_entries] {e}")
        return 0


# =============================================================================
# FOLDER OPERATIONS
# =============================================================================

def get_queue_items_by_folder(folder_path: str) -> List[Dict[str, Any]]:
    try:
        with db_cursor() as (conn, cursor):
            cursor.execute(
                """
                SELECT id, artist, album, title, status, track_number,
                       found_filename, album_artist, release_mbid, release_id, import_group
                FROM download_queue
                WHERE import_group = %s
                ORDER BY
                    CASE WHEN TRIM(COALESCE(track_number, '')) ~ '^\d+$'
                        THEN TRIM(track_number)::integer ELSE 9999 END,
                    id
                """,
                (folder_path,),
            )
            return [dict(zip([c[0] for c in cursor.description], r)) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"[get_queue_items_by_folder] {e}")
        return []


def slskd_eligibility_diagnostics() -> dict:
    try:
        with db_cursor() as (_, cursor):
            cursor.execute("""
                SELECT status, COUNT(*) as count
                FROM download_queue
                GROUP BY status
                ORDER BY count DESC
            """)
            rows = cursor.fetchall()
            return {
                "success": True,
                "status_counts": {r[0]: r[1] for r in rows},
            }
    except Exception as e:
        logger.error(f"[slskd_eligibility_diagnostics] {e}")
        return {"success": False, "error": str(e)}


def delete_folder(data: dict) -> dict:
    try:
        group_id = (data.get("group_id") or data.get("folder") or "").strip()
        if not group_id:
            return {"success": False, "error": "No group_id provided"}

        with db_cursor(commit=True) as (_, cursor):
            cursor.execute("DELETE FROM download_queue WHERE import_group = %s", (group_id,))
            return {"success": True, "deleted": cursor.rowcount}
    except Exception as e:
        logger.error(f"[delete_folder] {e}")
        return {"success": False, "error": str(e)}


def remove_group(data: dict) -> dict:
    try:
        group_id = (data.get("group_id") or data.get("folder") or "").strip()
        if not group_id:
            return {"success": False, "error": "No group_id provided"}

        with db_cursor(commit=True) as (_, cursor):
            cursor.execute(
                "UPDATE download_queue SET import_group = NULL WHERE import_group = %s",
                (group_id,),
            )
            return {"success": True, "updated": cursor.rowcount}
    except Exception as e:
        logger.error(f"[remove_group] {e}")
        return {"success": False, "error": str(e)}


def reset_moving(queue_ids: list[int] | None, stale_minutes: int) -> dict:
    try:
        with db_cursor(commit=True) as (_, cursor):
            if queue_ids:
                placeholders = ", ".join(["%s"] * len(queue_ids))
                cursor.execute(
                    f"""
                    UPDATE download_queue
                    SET status = 'completed', updated_at = CURRENT_TIMESTAMP
                    WHERE id IN ({placeholders}) AND status = 'moving'
                    """,
                    queue_ids,
                )
            else:
                cursor.execute(
                    """
                    UPDATE download_queue
                    SET status = 'completed', updated_at = CURRENT_TIMESTAMP
                    WHERE status = 'moving'
                      AND updated_at < NOW() - MAKE_INTERVAL(mins := %s)
                    """,
                    (stale_minutes,),
                )
            return {"success": True, "updated": cursor.rowcount}
    except Exception as e:
        logger.error(f"[reset_moving] {e}")
        return {"success": False, "error": str(e)}


# =============================================================================
# MATCHING HELPERS
# =============================================================================

def apply_release_mbid(queue_ids: list, mbid: str, artist: str, album: str) -> int:
    try:
        with db_cursor(commit=True) as (_, cursor):
            placeholders = ", ".join(["%s"] * len(queue_ids))
            cursor.execute(
                f"""
                UPDATE download_queue
                SET release_mbid = %s, release_id = %s,
                    release_source = 'musicbrainz',
                    album_artist = CASE WHEN NULLIF(TRIM(album_artist), '') IS NULL THEN %s ELSE album_artist END,
                    album = CASE WHEN NULLIF(TRIM(album), '') IS NULL THEN %s ELSE album END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id IN ({placeholders})
                """,
                (mbid, mbid, artist, album, *queue_ids),
            )
            return cursor.rowcount
    except Exception as e:
        logger.error(f"[apply_release_mbid] {e}")
        return 0


def mark_in_collection(
    *,
    queue_id: int,
    matched_file_path: str,
    collection_track_id: int | None = None,
    found_filename: str | None = None,
    file_path: str | None = None,
) -> Optional[Dict[str, Any]]:
    """Mark a queue item as already present in the collection."""
    try:
        with db_cursor(commit=True) as (_, cursor):
            cursor.execute(
                """
                UPDATE download_queue
                SET status = 'in_collection',
                    matched_file_path = %s,
                    collection_track_id = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                RETURNING *
                """,
                (matched_file_path, collection_track_id, queue_id),
            )
            row = cursor.fetchone()
            return dict(zip([c[0] for c in cursor.description], row)) if row else None
    except Exception as e:
        logger.error(f"[mark_in_collection] {e}")
        return None


def get_active_queue_signatures() -> set[str]:
    """Return a set of normalized (artist::title) signatures for the active queue."""
    try:
        with db_cursor() as (_, cursor):
            cursor.execute("""
                SELECT LOWER(COALESCE(NULLIF(artist, ''), 'unknown'))
                       || '::' ||
                       LOWER(COALESCE(NULLIF(title, ''), 'unknown'))
                FROM download_queue
                WHERE status IN ('queued', 'searching', 'downloading', 'pending_match')
            """)
            return {row[0] for row in cursor.fetchall() or []}
    except Exception as e:
        logger.error(f"[get_active_queue_signatures] {e}")
        return set()
