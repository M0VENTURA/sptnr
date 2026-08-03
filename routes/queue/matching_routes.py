"""
Queue matching routes.
"""

from __future__ import annotations


from quart import Blueprint, request
from routes.utils import json_response as _json_response


from services.downloads.download_organize_service import (
    organize_track,
)

from services.downloads.download_matching_service import (
    match_folder,
    auto_match_folder,
)
from services.queue.queue_processing_service import organize_group_sync
from services.tasks.queue_tasks import start_organize_group
from db.utils import get_db_connection, row_get

queue_matching_bp = Blueprint("queue_matching", __name__)

# -----------------------------------------------------------------------------
# Move to music (single track)
# -----------------------------------------------------------------------------

@queue_matching_bp.route("/api/queue/move-to-music/<int:queue_id>", methods=["POST"])
async def api_queue_move_to_music(queue_id: int):
    payload = (await request.get_json(silent=True)) or {}
    return _json_response(organize_track(queue_id, payload))


# -----------------------------------------------------------------------------
# Organize
# -----------------------------------------------------------------------------

@queue_matching_bp.route("/api/queue/<int:queue_id>/organize", methods=["POST"])
async def api_queue_organize(queue_id: int):
    payload = (await request.get_json(silent=True)) or {}
    return _json_response(organize_track(queue_id, payload))


@queue_matching_bp.route("/api/queue/organize-group", methods=["POST"])
async def api_queue_organize_group():
    payload = (await request.get_json(silent=True)) or {}
    group_id = payload.get("group_id") or payload.get("import_group")

    if not group_id:
        return _json_response({
            "success": False,
            "error": "group_id is required"
        })

    async_requested = str(request.args.get("async", "0")).strip().lower() in {"1", "true", "yes", "on"}

    if async_requested:
        task_id = start_organize_group(group_id, payload.get("metadata") or {})
        return _json_response(({
            "success": True,
            "accepted": True,
            "task_id": task_id,
            "status": "running",
            "message": "Organization started in background",
        }, 202))

    return _json_response(organize_group_sync(group_id, payload.get("metadata") or {}))

# -----------------------------------------------------------------------------
# Matching (optional but correct)
# -----------------------------------------------------------------------------

@queue_matching_bp.route("/api/queue/match-folder", methods=["POST"])
async def api_match_folder():
    payload = (await request.get_json(silent=True)) or {}
    folder_path = payload.get("folder_path")
    if not folder_path:
        return _json_response({
            "success": False,
            "error": "folder_path is required"
        })

    return _json_response(match_folder(folder_path, payload))


@queue_matching_bp.route("/api/queue/auto-match-folder", methods=["POST"])
async def api_auto_match_folder():
    payload = (await request.get_json(silent=True)) or {}
    folder_path = payload.get("folder_path")
    if not folder_path:
        return _json_response({
            "success": False,
            "error": "folder_path is required"
        })

    return _json_response(auto_match_folder(folder_path, payload))


# -----------------------------------------------------------------------------
# Matched releases (for queue item match modal)
# -----------------------------------------------------------------------------

@queue_matching_bp.route("/api/queue/matched-releases", methods=["GET"])
def api_queue_matched_releases():
    """Return all unique releases currently in the download queue."""
    import logging as _logging

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        artist_filter = (request.args.get("artist") or "").strip()
        album_filter = (request.args.get("album") or "").strip()
        try:
            requested_limit = int(request.args.get("limit", 80))
        except Exception:
            requested_limit = 80
        limit = max(10, min(requested_limit, 250))

        active_statuses = (
            "queued", "searching", "downloading",
            "matched", "completed",
            "unmatched", "queried",
            "discovered", "pending_match",
            "possible_duplicate", "duplicate",
        )
        status_placeholders = ", ".join("%s" for _ in active_statuses)

        cursor.execute(f"""
            SELECT
                COALESCE(NULLIF(release_mbid, ''), NULLIF(release_id, '')) AS mbid,
                MAX(COALESCE(NULLIF(album_artist, ''), artist)) AS release_artist,
                MAX(COALESCE(NULLIF(album, ''), '')) AS release_album,
                MAX(COALESCE(NULLIF(CAST(release_year AS TEXT), ''), NULLIF(CAST(year AS TEXT), ''))) AS resolved_year,
                COUNT(*) AS track_count,
                MAX(CASE WHEN %s <> '' AND LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(%s) THEN 1 ELSE 0 END) AS artist_match,
                MAX(CASE WHEN %s <> '' AND LOWER(COALESCE(NULLIF(album, ''), '')) = LOWER(%s) THEN 1 ELSE 0 END) AS album_match
            FROM download_queue
            WHERE COALESCE(NULLIF(release_mbid, ''), NULLIF(release_id, '')) IS NOT NULL
              AND status IN ({status_placeholders})
            GROUP BY COALESCE(NULLIF(release_mbid, ''), NULLIF(release_id, ''))
            ORDER BY artist_match DESC, album_match DESC, track_count DESC,
                     LOWER(MAX(COALESCE(NULLIF(album_artist, ''), artist))),
                     LOWER(MAX(COALESCE(NULLIF(album, ''), '')))
            LIMIT %s
        """, (artist_filter, artist_filter, album_filter, album_filter, *active_statuses, limit))

        releases = []
        for row in cursor.fetchall() or []:
            releases.append({
                "mbid": row_get(row, "mbid", 0) or "",
                "artist": row_get(row, "release_artist", 1) or "",
                "album": row_get(row, "release_album", 2) or "",
                "year": row_get(row, "resolved_year", 3) or "",
                "track_count": int(row_get(row, "track_count", 4) or 0),
            })

        conn.close()
        conn = None
        return _json_response({"success": True, "releases": releases})

    except Exception as exc:
        _logging.getLogger(__name__).error("Error fetching matched releases: %s", exc)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        return _json_response({"success": False, "error": str(exc)}), 500


# -----------------------------------------------------------------------------
# Reset match (per queue item)
# -----------------------------------------------------------------------------

@queue_matching_bp.route("/api/queue/<int:queue_id>/reset-match", methods=["POST"])
def api_queue_reset_match(queue_id: int):
    """Reset a queue item's match so it can be rematched manually."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE download_queue
            SET release_mbid = NULL, release_id = NULL, release_source = NULL,
                mb_release_group_id = NULL, mb_match_status = NULL, mb_match_score = NULL,
                mb_match_candidates = NULL, mb_matched_title = NULL, mb_matched_artist = NULL,
                mb_matched_year = NULL, mb_last_match_at = NULL,
                status = 'unmatched',
                file_path = NULL, matched_file_path = NULL, music_file_path = NULL,
                found_filename = NULL, source_music_path = NULL,
                in_collection = 0, collection_track_id = NULL, collection_matched_at = NULL,
                failure_reason = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (queue_id,))
        updated = cursor.rowcount > 0
        conn.commit()
        conn.close()
        conn = None

        if not updated:
            return _json_response({"error": "Queue item not found"}), 404
        return _json_response({"success": True, "message": "Queue item match reset", "queue_id": queue_id})

    except Exception as exc:
        if conn is not None:
            try:
                conn.rollback()
                conn.close()
            except Exception:
                pass
        return _json_response({"success": False, "error": str(exc)}), 500
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# -----------------------------------------------------------------------------
# Apply MBID match (per queue item)
# -----------------------------------------------------------------------------

@queue_matching_bp.route("/api/queue/<int:queue_id>/apply-mbid-match", methods=["POST"])
async def api_queue_apply_mbid_match(queue_id: int):
    """Apply a MusicBrainz release match to a single queue item."""
    import logging as _logging

    try:
        payload = (await request.get_json(silent=True)) or {}
        new_mbid = (payload.get("new_mbid") or "").strip()
        new_artist = (payload.get("new_artist") or "").strip()
        new_album = (payload.get("new_album") or "").strip()

        if not new_mbid:
            return _json_response({"success": False, "error": "new_mbid is required"}), 400

        conn = get_db_connection()
        cursor = conn.cursor()
        import_group = f"mbid_{new_mbid}"

        cursor.execute("""
            UPDATE download_queue
            SET release_mbid = %s, release_id = %s, release_source = 'musicbrainz',
                album = COALESCE(%s, album), album_artist = COALESCE(%s, album_artist),
                import_group = %s, status = 'matched', updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (new_mbid, new_mbid, new_album, new_artist, import_group, queue_id))

        updated = cursor.rowcount > 0
        conn.commit()
        conn.close()

        if not updated:
            return _json_response({"error": "Queue item not found"}), 404
        return _json_response({"success": True, "message": "MBID match applied", "queue_id": queue_id})

    except Exception as exc:
        _logging.getLogger(__name__).error("Error applying MBID match: %s", exc)
        return _json_response({"success": False, "error": str(exc)}), 500