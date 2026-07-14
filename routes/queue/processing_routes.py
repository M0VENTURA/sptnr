"""
Queue processing routes.
"""

from __future__ import annotations

from quart import Blueprint, request
from routes.utils import json_response as _json_response

from services.downloads.download_processing_service import (
    queue_add,
    queue_add_batch,
    queue_clear,
    queue_delete,
    queue_purge_all,
    queue_requeue,
    queue_requeue_all_unmatched,
    queue_retry_all_failed,
    queue_status,
    queue_update,
    queue_imported,
)
from db.utils import get_db_connection, row_get

queue_processing_bp = Blueprint("queue_processing", __name__)


@queue_processing_bp.route("/api/queue/add", methods=["POST"])
def api_queue_add():
    payload = request.get_json(silent=True) or {}
    return _json_response(queue_add(payload))


@queue_processing_bp.route("/api/queue/add-batch", methods=["POST"])
def api_queue_add_batch():
    payload = request.get_json(silent=True) or {}
    return _json_response(queue_add_batch(payload))


@queue_processing_bp.route("/api/queue/status", methods=["GET"])
def api_queue_status():
    return _json_response(queue_status(request.args))


@queue_processing_bp.route("/api/queue/imported", methods=["GET"])
def api_queue_imported():
    return _json_response(queue_imported(request.args))


@queue_processing_bp.route("/api/queue/<int:queue_id>/update", methods=["POST"])
def api_queue_update(queue_id: int):
    payload = request.get_json(silent=True) or {}
    return _json_response(queue_update(queue_id, payload))


@queue_processing_bp.route("/api/queue/<int:queue_id>/send", methods=["POST"])
def api_queue_send_to_download(queue_id: int):
    return _json_response(queue_requeue(queue_id))


@queue_processing_bp.route("/api/queue/<int:queue_id>/requeue", methods=["POST"])
def api_queue_requeue_item(queue_id: int):
    return _json_response(queue_requeue(queue_id))


@queue_processing_bp.route("/api/queue/<int:queue_id>/delete", methods=["DELETE"])
def api_queue_delete(queue_id: int):
    delete_download_file = request.args.get("delete_download_file", "0").lower() in {"1", "true", "yes"}
    return _json_response(queue_delete(queue_id, delete_download_file=delete_download_file))


@queue_processing_bp.route("/api/queue/clear", methods=["POST", "DELETE"])
def api_queue_clear():
    payload = request.get_json(silent=True) or {}
    return _json_response(queue_clear(payload))


@queue_processing_bp.route("/api/queue/purge-all", methods=["POST", "DELETE"])
def api_queue_purge_all():
    return _json_response(queue_purge_all())


@queue_processing_bp.route("/api/queue/requeue-all-unmatched", methods=["POST"])
def api_queue_requeue_all_unmatched():
    return _json_response(queue_requeue_all_unmatched())


@queue_processing_bp.route("/api/queue/prefetch-release-tracks", methods=["POST"])
def api_queue_prefetch_release_tracks():
    """Prefetch release tracks for an upcoming album release (stub)."""
    return _json_response({"success": True, "tracks": []})


@queue_processing_bp.route("/api/queue/retry-all-failed", methods=["POST"])
def api_queue_retry_all_failed():
    return _json_response(queue_retry_all_failed())


# ── SSE: real-time queue progress stream ──────────────────────────────────

import asyncio
import json
import time

@queue_processing_bp.route("/api/queue/stream", methods=["GET"])
async def api_queue_stream():
    """Server-Sent Events endpoint for live queue status updates.

    Frontend connects via:
        const es = new EventSource("/api/queue/stream");
        es.onmessage = (e) => { const data = JSON.parse(e.data); ... };

    Events are emitted every ~2 seconds with current queue counts.
    Connection closes automatically when the client disconnects.
    """
    async def event_generator():
        last_payload = None
        while True:
            try:
                from db.repositories.queue import get_queue_status_counts
                counts = get_queue_status_counts()
                payload = json.dumps(counts)

                # Only send if data changed (reduces bandwidth)
                if payload != last_payload:
                    yield f"data: {payload}\n\n"
                    last_payload = payload
            except Exception:
                yield f"data: {json.dumps({'error': 'failed to read queue status'})}\n\n"

            await asyncio.sleep(2)

    from quart import Response
    return Response(
        event_generator(),
        content_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# -----------------------------------------------------------------------------
# Copy from local music path
# -----------------------------------------------------------------------------

@queue_processing_bp.route("/api/queue/<int:queue_id>/copy-from-local", methods=["POST"])
def api_queue_copy_from_local(queue_id: int):
    """Copy a file from its existing /music location to /downloads so it can be
    re-tagged and moved to the correct album folder."""
    import logging as _logging
    import os
    import threading as _threading
    import shutil as _shutil

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM download_queue WHERE id = %s", (queue_id,))
        row = cursor.fetchone()
        conn.close()
        conn = None

        if not row:
            return _json_response({"error": "Queue item not found"}), 404

        item = row_get(row, None) if hasattr(row, "keys") else {
            col.name: row[idx] for idx, col in enumerate(cursor.description or [])
        } if cursor.description else {}

        source_path = (item.get("source_music_path") or "").strip() if isinstance(item, dict) else ""
        if not source_path:
            return _json_response({"success": False, "error": "No source music path stored"}), 400
        if not os.path.isfile(source_path):
            return _json_response({"success": False, "error": f"Source file not found: {source_path}"}), 400

        downloads_dir = os.environ.get("DOWNLOADS_DIR", "/downloads")
        dest_filename = os.path.basename(source_path)
        dest_path = os.path.join(downloads_dir, dest_filename)

        if os.path.exists(dest_path):
            base, ext = os.path.splitext(dest_filename)
            dest_path = os.path.join(downloads_dir, f"{base}_local_copy{ext}")

        def _background_copy():
            try:
                _shutil.copy2(source_path, dest_path)
                _logging.getLogger(__name__).info(
                    "[COPY_FROM_LOCAL] Queue %s: copied '%s' -> '%s'", queue_id, source_path, dest_path
                )
                from services.downloads.download_processing_service import queue_update as _qu
                _qu(queue_id, status="matched", file_path=dest_path)
            except Exception as bg_err:
                _logging.getLogger(__name__).error(
                    "[COPY_FROM_LOCAL] Background copy failed for queue %s: %s", queue_id, bg_err
                )
                try:
                    from services.downloads.download_processing_service import queue_update as _qu2
                    _qu2(queue_id, status="failed", failure_reason=str(bg_err)[:200])
                except Exception:
                    pass

        _threading.Thread(target=_background_copy, daemon=True, name=f"copy-from-local-{queue_id}").start()

        return _json_response({
            "success": True,
            "message": "File copy started — will be tagged and moved automatically.",
            "dest_path": dest_path,
        })

    except Exception as exc:
        _logging.getLogger(__name__).error("[COPY_FROM_LOCAL] Error for queue %s: %s", queue_id, exc)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        return _json_response({"success": False, "error": str(exc)}), 500


# -----------------------------------------------------------------------------
# Migrate existing queue items
# -----------------------------------------------------------------------------

@queue_processing_bp.route("/api/queue/migrate-existing", methods=["POST"])
def api_queue_migrate_existing():
    """Backfill legacy queue rows into current grouped/source conventions.

    Note: this endpoint requires the migration service module at
    ``services.queue.migration_service`` to be implemented.  If the module
    does not exist yet the endpoint returns a clear error message so the
    frontend can display it to the user.
    """
    import logging as _logging
    try:
        from services.queue.migration_service import migrate_existing_queue_items_to_grouped_setup as _migrate
    except ImportError:
        return _json_response({
            "success": False,
            "error": "Migration service not available — see services/queue/migration_service.py",
        }), 501

    try:
        payload = request.get_json(silent=True) or {}
        limit = request.values.get("limit") or payload.get("limit")
        if limit is not None:
            try:
                limit = int(limit)
                if limit <= 0:
                    return _json_response({"error": "limit must be a positive integer"}), 400
            except (TypeError, ValueError):
                return _json_response({"error": "limit must be an integer"}), 400
        result = _migrate(limit=limit)
        status_code = 200 if result.get("success") else 500
        return _json_response((result, status_code))
    except Exception as exc:
        _logging.getLogger(__name__).error("Error migrating existing queue rows: %s", exc, exc_info=True)
        return _json_response({"success": False, "error": str(exc)}), 500


# -----------------------------------------------------------------------------
# Update album MBID
# -----------------------------------------------------------------------------

@queue_processing_bp.route("/api/queue/update-album-mbid", methods=["POST"])
def api_queue_update_album_mbid():
    """Update all queue items for an album with a new MusicBrainz release ID."""
    import logging as _logging
    conn = None
    try:
        data = request.get_json(silent=True) or {}
        old_artist = (data.get("old_artist") or "").strip()
        old_album = (data.get("old_album") or "").strip()
        new_mbid = (data.get("new_mbid") or "").strip()
        new_artist = (data.get("new_artist") or "").strip()
        new_album = (data.get("new_album") or "").strip()

        if not all([old_artist, old_album, new_mbid]):
            return _json_response({"error": "Missing required fields (old_artist, old_album, new_mbid)"}), 400

        conn = get_db_connection()
        cursor = conn.cursor()
        mbid_import_group = f"mbid_{new_mbid}"

        cursor.execute("""
            UPDATE download_queue
            SET release_mbid = %s, release_id = %s, release_source = 'musicbrainz',
                album = %s, album_artist = %s, import_group = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(%s)
              AND LOWER(album) = LOWER(%s)
        """, (new_mbid, new_mbid, new_album, new_artist, mbid_import_group, old_artist, old_album))

        updated_count = cursor.rowcount or 0

        # Merge queue rows already under this MBID into the same import_group
        cursor.execute("""
            UPDATE download_queue
            SET import_group = %s, release_source = 'musicbrainz',
                album_artist = COALESCE(NULLIF(album_artist, ''), %s),
                updated_at = CURRENT_TIMESTAMP
            WHERE COALESCE(NULLIF(release_mbid, ''), NULLIF(release_id, '')) = %s
        """, (mbid_import_group, new_artist, new_mbid))
        merged_count = cursor.rowcount or 0

        conn.commit()
        conn.close()
        conn = None

        return _json_response({
            "success": True,
            "message": f"Updated {updated_count} queue items with new MBID",
            "updated_count": updated_count,
            "merged_count": merged_count,
            "release_mbid": new_mbid,
        })

    except Exception as exc:
        _logging.getLogger(__name__).error("Error updating album MBID: %s", exc, exc_info=True)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        return _json_response({"success": False, "error": str(exc)}), 500


# -----------------------------------------------------------------------------
# Missing tracks
# -----------------------------------------------------------------------------

@queue_processing_bp.route("/api/queue/missing-tracks", methods=["GET"])
def api_queue_missing_tracks():
    """Return MusicBrainz release tracks not currently present in queue rows."""
    import logging as _logging
    import re as _re

    conn = None
    try:
        release_mbid = (request.args.get("release_mbid") or "").strip()
        queue_ids_raw = (request.args.get("queue_ids") or "").strip()

        if not release_mbid:
            return _json_response({"success": False, "error": "release_mbid is required"}), 400

        queue_ids = []
        if queue_ids_raw:
            for value in queue_ids_raw.split(","):
                v = value.strip()
                if not v:
                    continue
                try:
                    qid = int(v)
                    if qid > 0:
                        queue_ids.append(qid)
                except Exception:
                    continue
            queue_ids = list(dict.fromkeys(queue_ids))

        # Fetch release tracks from download matching service
        from services.downloads.download_matching_service import get_release_tracks as _rel_tracks
        release_meta = _rel_tracks(release_id=release_mbid, source="musicbrainz") or {}
        release_tracks = release_meta.get("tracks") or []

        if not release_tracks:
            return _json_response({
                "success": True, "release_mbid": release_mbid,
                "total_release_tracks": 0, "missing_tracks": [],
            })

        conn = get_db_connection()
        cursor = conn.cursor()

        if queue_ids:
            ids_placeholders = ", ".join("%s" for _ in queue_ids)
            cursor.execute(f"""
                SELECT id, title, track_number, disc_number, recording_mbid
                FROM download_queue
                WHERE id IN ({ids_placeholders})
                  AND status NOT IN ('removed', 'cancelled', 'deleted')
            """, tuple(queue_ids))
        else:
            cursor.execute("""
                SELECT id, title, track_number, disc_number, recording_mbid
                FROM download_queue
                WHERE COALESCE(NULLIF(release_mbid, ''), NULLIF(release_id, '')) = %s
                  AND status NOT IN ('removed', 'cancelled', 'deleted')
            """, (release_mbid,))

        queue_rows = cursor.fetchall() or []
        conn.close()
        conn = None

        def _norm_text(value):
            return _re.sub(r"\s+", " ", _re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())).strip()

        existing_recording_mbids = set()
        existing_track_keys = set()

        for row in queue_rows:
            recording_mbid = (row_get(row, "recording_mbid", 4) or "").strip()
            if recording_mbid:
                existing_recording_mbids.add(recording_mbid.lower())
            disc = (row_get(row, "disc_number", 3) or "").strip()
            track = (row_get(row, "track_number", 2) or "").strip()
            title = _norm_text(row_get(row, "title", 1))
            key = f"{disc}:{track}:{title}" if (disc or track or title) else ""
            if key:
                existing_track_keys.add(key)

        missing_tracks = []
        for track in release_tracks:
            recording_mbid = (track.get("recording_mbid") or "").strip()
            disc_number = track.get("disc_number")
            track_number = track.get("track_number")
            title = (track.get("title") or "").strip()

            disc = str(disc_number or "").strip()
            track_str = str(track_number or "").strip()
            title_norm = _norm_text(title)
            track_key = f"{disc}:{track_str}:{title_norm}" if (disc or track_str or title_norm) else ""

            if recording_mbid and recording_mbid.lower() in existing_recording_mbids:
                continue
            if track_key and track_key in existing_track_keys:
                continue

            missing_tracks.append({
                "disc_number": disc_number,
                "track_number": track_number,
                "title": title,
                "artist": track.get("artist") or release_meta.get("artist") or "",
                "duration": track.get("duration") or 0,
                "recording_mbid": recording_mbid,
            })

        return _json_response({
            "success": True,
            "release_mbid": release_mbid,
            "release_title": release_meta.get("release_title") or "",
            "release_artist": release_meta.get("artist") or "",
            "total_release_tracks": len(release_tracks),
            "missing_tracks": missing_tracks,
        })

    except Exception as exc:
        _logging.getLogger(__name__).error("Error fetching missing tracks: %s", exc)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        return _json_response({"success": False, "error": str(exc)}), 500


# -----------------------------------------------------------------------------
# Import missing tracks
# -----------------------------------------------------------------------------

@queue_processing_bp.route("/api/queue/import-missing-tracks", methods=["POST"])
async def api_queue_import_missing_tracks():
    """Match selected missing MusicBrainz release tracks to existing queue rows."""
    import logging as _logging
    import re as _re
    from difflib import SequenceMatcher

    conn = None
    try:
        payload = (await request.get_json(silent=True)) or {}
        release_mbid = (payload.get("release_mbid") or "").strip()
        selected_keys = payload.get("selected_keys") or []
        queue_ids_raw = payload.get("queue_ids") or []

        if not release_mbid:
            return _json_response({"success": False, "error": "release_mbid is required"}), 400
        if not selected_keys:
            return _json_response({"success": False, "error": "selected_keys is required"}), 400

        selected_keys = list(dict.fromkeys(str(k).strip() for k in selected_keys if str(k).strip()))
        queue_ids = list(dict.fromkeys(int(v) for v in queue_ids_raw if str(v).strip().isdigit()))

        from services.downloads.download_matching_service import get_release_tracks as _rel_tracks
        release_meta = _rel_tracks(release_id=release_mbid, source="musicbrainz") or {}
        release_tracks = release_meta.get("tracks") or []
        if not release_tracks:
            return _json_response({"success": False, "error": "No MusicBrainz track metadata available"}), 400

        conn = get_db_connection()
        cursor = conn.cursor()

        # Get context from the first queue ID
        ref_artist = ref_album = ref_import_group = ""
        if queue_ids:
            ids_ph = ", ".join("%s" for _ in queue_ids)
            cursor.execute(f"""
                SELECT artist, album, album_artist, import_group
                FROM download_queue WHERE id IN ({ids_ph}) ORDER BY id LIMIT 1
            """, tuple(queue_ids))
            ref = cursor.fetchone()
            if ref:
                ref_artist = row_get(ref, "artist", 0) or ""
                ref_album = row_get(ref, "album", 1) or ""
                ref_import_group = row_get(ref, "import_group", 3) or ""

        release_title = release_meta.get("release_title") or ref_album
        release_artist = release_meta.get("artist") or ""
        import_group = ref_import_group or f"mbid_{release_mbid}"

        # Fetch candidate queue rows
        cursor.execute("""
            SELECT id, title, track_number, disc_number, recording_mbid
            FROM download_queue
            WHERE (LOWER(COALESCE(NULLIF(album, ''), '')) = LOWER(%s)
                   OR (import_group IS NOT NULL AND import_group = %s))
              AND COALESCE(NULLIF(release_mbid, ''), NULLIF(release_id, '')) IS DISTINCT FROM %s
              AND status NOT IN ('removed', 'cancelled', 'deleted', 'imported', 'in_collection')
        """, (release_title, import_group, release_mbid))
        candidate_rows = cursor.fetchall() or []

        available = {}
        for row in candidate_rows:
            row_id = row_get(row, "id", 0)
            if row_id:
                title_norm = _re.sub(r"\s+", " ", _re.sub(r"[^a-z0-9]+", " ", str(row_get(row, "title", 1) or "").lower())).strip()
                available[row_id] = {
                    "id": row_id,
                    "title_norm": title_norm,
                    "track_number": str(row_get(row, "track_number", 2) or "").strip(),
                    "disc_number": str(row_get(row, "disc_number", 3) or "").strip(),
                    "recording_mbid": str(row_get(row, "recording_mbid", 4) or "").strip(),
                }

        def _norm_text(value):
            return _re.sub(r"\s+", " ", _re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())).strip()

        matched_count = no_match_count = failed_count = 0
        matched_items = []
        no_match_items = []
        failed_items = []

        for track in release_tracks:
            title = (track.get("title") or "").strip()
            if not title:
                continue
            recording_mbid = (track.get("recording_mbid") or "").strip()
            disc_number = track.get("disc_number")
            track_number = track.get("track_number")
            key = (recording_mbid or
                   f"{disc_number or ''}:{track_number or ''}:{_norm_text(title)}")
            if key not in selected_keys:
                continue

            title_norm = _norm_text(title)
            best_id = None
            best_score = -1.0

            for row_id, cand in available.items():
                score = 0.0
                if recording_mbid and cand["recording_mbid"] == recording_mbid:
                    score = 1.0
                elif title_norm and cand["title_norm"]:
                    score = SequenceMatcher(None, title_norm, cand["title_norm"]).ratio()
                    if track_number and cand["track_number"] and str(track_number).strip() == cand["track_number"]:
                        score = min(1.0, score + 0.15)
                if score > best_score:
                    best_score = score
                    best_id = row_id

            if best_id is None or best_score < 0.6:
                no_match_count += 1
                no_match_items.append({"title": title})
                continue

            try:
                updates = {
                    "release_mbid": release_mbid,
                    "release_id": release_mbid,
                    "release_source": "musicbrainz",
                    "import_group": import_group,
                    "album_artist": release_artist or None,
                    "album": release_title or None,
                }
                if recording_mbid:
                    updates["recording_mbid"] = recording_mbid
                if title:
                    updates["title"] = title
                if track_number:
                    updates["track_number"] = str(track_number)
                if disc_number:
                    updates["disc_number"] = str(disc_number)

                set_clause = ", ".join(f"{col} = %s" for col in updates)
                params = list(updates.values()) + [best_id]
                cursor.execute(
                    f"UPDATE download_queue SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                    params,
                )
                conn.commit()
                del available[best_id]
                matched_count += 1
                matched_items.append({"queue_id": best_id, "title": title, "match_score": round(best_score, 3)})
            except Exception as upd_err:
                _logging.getLogger(__name__).warning("Failed to update queue row %s: %s", best_id, upd_err)
                try:
                    conn.rollback()
                except Exception:
                    pass
                failed_count += 1
                failed_items.append({"title": title})

        conn.close()
        conn = None

        return _json_response({
            "success": True,
            "release_mbid": release_mbid,
            "matched_count": matched_count,
            "no_match_count": no_match_count,
            "failed_count": failed_count,
            "matched_items": matched_items,
            "no_match_items": no_match_items,
            "failed_items": failed_items,
        })

    except Exception as exc:
        _logging.getLogger(__name__).error("Error matching missing tracks: %s", exc)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        return _json_response({"success": False, "error": str(exc)}), 500
