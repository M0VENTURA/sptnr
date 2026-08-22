"""Repository helpers for queue admin/management operations.

Contains:
- Clear, purge, and bulk cleanup
- Filesystem verification and pruning
- Discovery file management
- Duplicate detection and merging
- Folder-level operations
- Diagnostics
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Set, Tuple

import structlog
from sqlalchemy import text

from db.engine import db_session
from services.infrastructure.filesystem_service import resolve_downloads_dir

logger = structlog.get_logger(__name__)


# =============================================================================
# BULK / CLEANUP
# =============================================================================

def clear_queue(filters: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        with db_session() as session:
            if filters and "status" in filters:
                result = session.execute(
                    text("DELETE FROM download_queue WHERE status = :status"),
                    {"status": filters["status"]},
                )
                return {"success": True, "queue_items_deleted": result.rowcount}
            session.execute(text("DELETE FROM download_queue"))
            session.execute(text("DELETE FROM musicbrainz_releases"))
            return {"success": True}
    except Exception as e:
        logger.error("Clear queue failed", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


def purge_all() -> dict[str, Any]:
    """Hard purge the download pipeline."""
    try:
        import shutil as _shutil

        downloads_dir = resolve_downloads_dir()
        downloads_abs = os.path.abspath(downloads_dir or "")
        drive_root = os.path.splitdrive(downloads_abs)[1]
        
        if not downloads_abs or drive_root in (os.sep, "", "\\"):
            logger.error("Unsafe downloads path for purge", path=downloads_abs or downloads_dir)
            return {"success": False, "error": f"Unsafe downloads path for purge: {downloads_abs or downloads_dir}"}

        queue_deleted = 0
        with db_session() as session:
            result = session.execute(text("DELETE FROM download_queue"))
            queue_deleted = result.rowcount or 0
            session.execute(text("DELETE FROM slskd_search_logs"))
            
            for tbl in ("folder_track_matches", "folder_album_matches", "musicbrainz_releases"):
                try:
                    session.execute(text(f"DELETE FROM {tbl}"))
                except Exception as exc:
                    logger.debug("Table purge skipped", table=tbl, error=str(exc))

        deleted_files = 0
        deleted_dirs = 0
        if os.path.isdir(downloads_abs):
            for child_name in os.listdir(downloads_abs):
                child_path = os.path.join(downloads_abs, child_name)
                try:
                    if os.path.isdir(child_path):
                        for _root, _dirs, files in os.walk(child_path):
                            deleted_files += len(files)
                        _shutil.rmtree(child_path)
                        deleted_dirs += 1
                    else:
                        os.remove(child_path)
                        deleted_files += 1
                except Exception as fs_err:
                    logger.warning("Could not remove path", path=child_path, error=str(fs_err))

        logger.info(
            "Purged download pipeline",
            queue_deleted=queue_deleted,
            files_deleted=deleted_files,
            dirs_deleted=deleted_dirs,
            path=downloads_abs,
        )
        return {
            "success": True,
            "queue_items_deleted": queue_deleted,
            "deleted_files": deleted_files,
            "deleted_dirs": deleted_dirs,
            "downloads_dir": downloads_abs,
        }
    except Exception as e:
        logger.error("Purge all failed", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


def get_imported(limit: int = 50) -> list[dict[str, Any]]:
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT * FROM download_queue
                    WHERE status = 'imported'
                    ORDER BY updated_at DESC
                    LIMIT :limit
                """),
                {"limit": limit},
            )
            rows = result.fetchall()
            return [dict(r._mapping) for r in rows]
    except Exception as e:
        logger.error("Failed to get imported items", error=str(e))
        return []


def cleanup() -> dict[str, Any]:
    try:
        with db_session() as session:
            result = session.execute(text("""
                DELETE FROM download_queue
                WHERE status = 'failed'
                  AND updated_at < NOW() - INTERVAL '7 days'
            """))
            return {"success": True, "deleted": result.rowcount}
    except Exception as e:
        logger.error("Queue cleanup failed", error=str(e))
        return {"success": False, "error": str(e)}


def cleanup_copied_sources() -> dict[str, Any]:
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
        with db_session() as session:
            result = session.execute(text("""
                SELECT id, file_path, found_filename
                FROM download_queue
                WHERE status = 'imported'
                  AND (file_path IS NOT NULL OR found_filename IS NOT NULL)
            """))
            items = result.fetchall()

            for row in items:
                scanned_count += 1
                mapping = getattr(row, "_mapping", None)
                queue_id = mapping.get("id") if mapping else row[0]
                file_path = mapping.get("file_path") if mapping else row[1]
                found_filename = mapping.get("found_filename") if mapping else row[2]
                
                downloads_root = os.path.abspath(resolve_downloads_dir())
                deleted = False

                if file_path and _delete_if_exists(file_path):
                    deleted = True
                    deleted_paths.append(file_path)

                if not deleted and found_filename and os.path.isdir(downloads_root):
                    found_base = os.path.basename(str(found_filename).replace("\\", "/"))
                    for root, _dirs, files in os.walk(downloads_root):
                        if found_base and found_base in files:
                            candidate = os.path.join(root, found_base)
                            if _delete_if_exists(candidate):
                                deleted = True
                                deleted_paths.append(candidate)
                                break

                if deleted:
                    deleted_count += 1
                    session.execute(
                        text("UPDATE download_queue SET updated_at = CURRENT_TIMESTAMP WHERE id = :id"),
                        {"id": queue_id},
                    )

            return {
                "success": True,
                "scanned": scanned_count,
                "deleted": deleted_count,
                "deleted_paths": deleted_paths,
            }
    except Exception as e:
        logger.error("Cleanup copied sources failed", error=str(e))
        return {"success": False, "error": str(e)}


def cleanup_orphaned(data: dict[str, Any]) -> dict[str, Any]:
    try:
        with db_session() as session:
            downloads_root = os.path.abspath(resolve_downloads_dir())
            perform_cleanup = bool(data.get("cleanup", False))
            filter_artist = (data.get("artist") or "").strip().lower()

            result = session.execute(text("""
                SELECT id, file_path, found_filename, artist, title
                FROM download_queue
                WHERE status IN ('imported', 'completed')
            """))
            items = result.fetchall()
            orphaned_files = []
            deleted_files = []

            for row in items:
                mapping = getattr(row, "_mapping", None)
                queue_id = mapping.get("id") if mapping else row[0]
                file_path = mapping.get("file_path") if mapping else row[1]
                found_filename = mapping.get("found_filename") if mapping else row[2]
                artist = (mapping.get("artist") if mapping else row[3] or "").lower()
                title = mapping.get("title") if mapping else row[4]

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
        logger.error("Cleanup orphaned failed", error=str(e))
        return {"success": False, "error": str(e)}


def count_pending_by_release(release_mbid: str) -> int:
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT COUNT(*)
                    FROM download_queue
                    WHERE (release_mbid = :mbid OR release_id = :mbid)
                      AND status NOT IN (
                          'completed', 'imported', 'in_collection',
                          'removed', 'cancelled', 'deleted'
                      )
                """),
                {"mbid": release_mbid},
            )
            return result.scalar() or 0
    except Exception as exc:
        logger.error("Count pending by release failed", release_mbid=release_mbid, error=str(exc))
        return 0


def verify_and_prune(data: dict[str, Any]) -> dict[str, Any]:
    try:
        with db_session() as session:
            dry_run = bool(data.get("dry_run", True))
            filter_artist = (data.get("filter_artist") or "").strip().lower()

            result = session.execute(text("""
                SELECT id, file_path, found_filename, artist, title, status
                FROM download_queue
            """))
            items = result.fetchall()
            checked_count = 0
            missing_items = []
            pruned_count = 0
            downloads_root = os.path.abspath(resolve_downloads_dir())

            for row in items:
                mapping = getattr(row, "_mapping", None)
                queue_id = mapping.get("id") if mapping else row[0]
                file_path = mapping.get("file_path") if mapping else row[1]
                found_filename = mapping.get("found_filename") if mapping else row[2]
                artist = (mapping.get("artist") if mapping else row[3] or "").lower()
                title = mapping.get("title") if mapping else row[4]

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
                        session.execute(text("DELETE FROM download_queue WHERE id = :id"), {"id": queue_id})
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
        logger.error("Verify and prune failed", error=str(e))
        return {"success": False, "error": str(e)}


# =============================================================================
# DISCOVERY / AUTO-DISCOVER HELPERS
# =============================================================================

def find_existing_discovered_file(*, file_path: str, filename: str, rel_path: str) -> dict[str, Any] | None:
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT * FROM download_queue
                    WHERE file_path = :file_path
                       OR found_filename = :filename
                       OR found_filename = :rel_path
                       OR found_filename = :file_path2
                    LIMIT 1
                """),
                {"file_path": file_path, "filename": filename, "rel_path": rel_path, "file_path2": file_path},
            )
            row = result.fetchone()
            return dict(row._mapping) if row else None
    except Exception as e:
        logger.error("Find existing discovered file failed", path=file_path, error=str(e))
        return None


def find_duplicate_queue_item(*, artist: str, title: str, album: str | None) -> dict[str, Any] | None:
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT * FROM download_queue
                    WHERE LOWER(artist) = LOWER(:artist)
                      AND LOWER(title) = LOWER(:title)
                      AND LOWER(COALESCE(album, '')) = LOWER(COALESCE(:album, ''))
                      AND status NOT IN ('removed', 'cancelled')
                    LIMIT 1
                """),
                {"artist": artist, "title": title, "album": album},
            )
            row = result.fetchone()
            return dict(row._mapping) if row else None
    except Exception as e:
        logger.error("Find duplicate queue item failed", artist=artist, title=title, error=str(e))
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
) -> dict[str, Any]:
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
        with db_session() as session:
            result = session.execute(
                text("""
                    DELETE FROM download_queue
                    WHERE id <> :keep_id
                      AND LOWER(artist) = LOWER(:artist)
                      AND LOWER(title) = LOWER(:title)
                      AND LOWER(COALESCE(album, '')) = LOWER(COALESCE(:album, ''))
                      AND status NOT IN ('removed', 'cancelled', 'deleted', 'imported', 'in_collection')
                """),
                {"keep_id": keep_id, "artist": artist, "title": title, "album": album},
            )
            return int(result.rowcount or 0)
    except Exception as e:
        logger.error("Delete duplicate queue entries failed", keep_id=keep_id, error=str(e))
        return 0


# =============================================================================
# FOLDER OPERATIONS
# =============================================================================

def get_queue_items_by_folder(folder_path: str) -> list[dict[str, Any]]:
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT id, artist, album, title, status, track_number,
                           found_filename, album_artist, release_mbid, release_id, import_group
                    FROM download_queue
                    WHERE import_group = :folder_path
                    ORDER BY
                        CASE WHEN TRIM(COALESCE(track_number, '')) ~ '^\\d+$'
                            THEN TRIM(track_number)::integer ELSE 9999 END,
                        id
                """),
                {"folder_path": folder_path},
            )
            return [dict(r._mapping) for r in result.fetchall()]
    except Exception as e:
        logger.error("Get queue items by folder failed", folder=folder_path, error=str(e))
        return []


def slskd_eligibility_diagnostics() -> dict[str, Any]:
    try:
        with db_session() as session:
            result = session.execute(text("""
                SELECT status, COUNT(*) as count
                FROM download_queue
                GROUP BY status
                ORDER BY count DESC
            """))
            rows = result.fetchall()
            return {
                "success": True,
                "status_counts": {r[0]: r[1] for r in rows},
            }
    except Exception as e:
        logger.error("Slskd eligibility diagnostics failed", error=str(e))
        return {"success": False, "error": str(e)}


def delete_folder(data: dict[str, Any]) -> dict[str, Any]:
    try:
        group_id = (data.get("group_id") or data.get("folder") or "").strip()
        if not group_id:
            return {"success": False, "error": "No group_id provided"}

        with db_session() as session:
            result = session.execute(
                text("DELETE FROM download_queue WHERE import_group = :group_id"),
                {"group_id": group_id},
            )
            return {"success": True, "deleted": result.rowcount}
    except Exception as e:
        logger.error("Delete folder failed", group_id=data.get("group_id"), error=str(e))
        return {"success": False, "error": str(e)}


def remove_group(data: dict[str, Any]) -> dict[str, Any]:
    try:
        group_id = (data.get("group_id") or data.get("folder") or "").strip()
        if not group_id:
            return {"success": False, "error": "No group_id provided"}

        with db_session() as session:
            result = session.execute(
                text("UPDATE download_queue SET import_group = NULL WHERE import_group = :group_id"),
                {"group_id": group_id},
            )
            return {"success": True, "updated": result.rowcount}
    except Exception as e:
        logger.error("Remove group failed", group_id=data.get("group_id"), error=str(e))
        return {"success": False, "error": str(e)}


def reset_moving(queue_ids: list[int] | None, stale_minutes: int) -> dict[str, Any]:
    try:
        with db_session() as session:
            if queue_ids:
                placeholders = ", ".join([f":id_{i}" for i in range(len(queue_ids))])
                params: dict[str, Any] = {f"id_{i}": qid for i, qid in enumerate(queue_ids)}
                result = session.execute(
                    text(f"""
                        UPDATE download_queue
                        SET status = 'completed', updated_at = CURRENT_TIMESTAMP
                        WHERE id IN ({placeholders}) AND status = 'moving'
                    """),
                    params,
                )
            else:
                result = session.execute(
                    text("""
                        UPDATE download_queue
                        SET status = 'completed', updated_at = CURRENT_TIMESTAMP
                        WHERE status = 'moving'
                          AND updated_at < NOW() - make_interval(mins => :stale_minutes)
                    """),
                    {"stale_minutes": stale_minutes},
                )
            return {"success": True, "updated": result.rowcount}
    except Exception as e:
        logger.error("Reset moving failed", error=str(e))
        return {"success": False, "error": str(e)}


# =============================================================================
# MATCHING HELPERS
# =============================================================================

def apply_release_mbid(queue_ids: list[int], mbid: str, artist: str, album: str) -> int:
    try:
        with db_session() as session:
            placeholders = ", ".join([f":id_{i}" for i in range(len(queue_ids))])
            params: dict[str, Any] = {f"id_{i}": qid for i, qid in enumerate(queue_ids)}
            params["mbid"] = mbid
            params["artist"] = artist
            params["album"] = album
            result = session.execute(
                text(f"""
                    UPDATE download_queue
                    SET release_mbid = :mbid, release_id = :mbid,
                        release_source = 'musicbrainz',
                        album_artist = CASE WHEN NULLIF(TRIM(album_artist), '') IS NULL THEN :artist ELSE album_artist END,
                        album = CASE WHEN NULLIF(TRIM(album), '') IS NULL THEN :album ELSE album END,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id IN ({placeholders})
                """),
                params,
            )
            return result.rowcount
    except Exception as e:
        logger.error("Apply release MBID failed", error=str(e))
        return 0


def mark_in_collection(
    *,
    queue_id: int,
    matched_file_path: str,
    collection_track_id: int | None = None,
    found_filename: str | None = None,
    file_path: str | None = None,
) -> dict[str, Any] | None:
    """Mark a queue item as already present in the collection."""
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    UPDATE download_queue
                    SET status = 'in_collection',
                        matched_file_path = :matched_file_path,
                        collection_track_id = :collection_track_id,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :queue_id
                    RETURNING *
                """),
                {"matched_file_path": matched_file_path, "collection_track_id": collection_track_id, "queue_id": queue_id},
            )
            row = result.fetchone()
            return dict(row._mapping) if row else None
    except Exception as e:
        logger.error("Mark in collection failed", queue_id=queue_id, error=str(e))
        return None


def get_active_queue_signatures() -> set[str]:
    """Return a set of normalized (artist::title) signatures for the active queue."""
    try:
        with db_session() as session:
            result = session.execute(text("""
                SELECT LOWER(COALESCE(NULLIF(artist, ''), 'unknown'))
                       || '::' ||
                       LOWER(COALESCE(NULLIF(title, ''), 'unknown'))
                FROM download_queue
                WHERE status IN ('queued', 'searching', 'downloading', 'pending_match')
            """))
            return {row[0] for row in result.fetchall() or []}
    except Exception as e:
        logger.error("Get active queue signatures failed", error=str(e))
        return set()
