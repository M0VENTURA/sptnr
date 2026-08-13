"""Download management routes.

Handles:
- Download folder display and matching.
- Queue item management (add, cancel, retry).
- MusicBrainz matching workflows.
- Download orchestration triggers.
"""

from __future__ import annotations

from datetime import datetime

from quart import Blueprint, request, jsonify
import logging

from helpers.config_helpers import get_config

from services.queue.queue_orchestrator import (
    process_next_batch,
)

from services.downloads import (
    cancel_folder,
    scan_downloads,
    get_scan_progress,
    verify_moved_files,
    discover_files,
    match_folder,
    auto_match_folder,
    apply_musicbrainz_match,
    get_release_status,
    get_release_tracks,
    check_folder_duplicates,
    process_album_existing,
    start_scheduler,
    stop_scheduler,
    scheduler_status,
)

from services.downloads.download_organize_service import (
    organize_folder,
    organize_track,
    merge_folders,
)

from services.downloads.download_folder_service import (
    cancel_folder_downloads,
    get_folder_groups_with_musicbrainz,
    get_folder_details,
)

from db.repositories.queue_admin import clear_queue as _clear_queue
from db.repositories.queue import (
    get_queue_status_counts,
    update_queue_item,
)

# scan_downloads_for_queue_import/grouped not yet migrated; using scan_downloads from services.downloads above
downloads_bp = Blueprint("downloads", __name__)

# =============================================================================
# ✅ FOLDER APIs
# =============================================================================

@downloads_bp.route("/api/downloads/folder-groups")
def api_get_folder_groups():
    return jsonify(get_folder_groups_with_musicbrainz())


@downloads_bp.route("/api/downloads/grouped-folders")
def api_get_grouped_folders():
    """Alias used by monitor JS — returns same data as folder-groups."""
    return jsonify(get_folder_groups_with_musicbrainz())


@downloads_bp.route("/api/downloads/unmatched-folders")
def api_get_unmatched_folders():
    """Folders under the downloads dir not tracked as MusicBrainz releases."""
    from services.downloads.download_folder_service import get_unmatched_folders
    return jsonify(get_unmatched_folders())


@downloads_bp.route("/api/downloads/folder/match", methods=["POST"])
async def api_match_folder_to_release():
    """Copy an unmatched folder into the library as a MusicBrainz release."""
    payload = (await request.get_json(silent=True)) or {}
    folder_path = (payload.get("folder_path") or "").strip()
    mb_id = (payload.get("mb_id") or payload.get("mbid") or "").strip()
    if not folder_path or not mb_id:
        return jsonify({"success": False, "error": "folder_path and mb_id (release/release-group URL or ID) are required"}), 400
    from services.downloads.download_folder_service import match_folder_to_release
    return jsonify(match_folder_to_release(folder_path, mb_id))


@downloads_bp.route("/api/downloads/folder/delete", methods=["POST"])
async def api_delete_download_folder():
    """Delete a folder under the downloads directory (safety-railed)."""
    payload = (await request.get_json(silent=True)) or {}
    folder_path = (payload.get("folder_path") or "").strip()
    if not folder_path:
        return jsonify({"success": False, "error": "folder_path required"}), 400
    from services.downloads.download_folder_service import delete_download_folder
    return jsonify(delete_download_folder(folder_path))
    return jsonify(get_folder_groups_with_musicbrainz())


@downloads_bp.route("/api/downloads/folder-status")
def api_get_folder_status():
    """Return folder status summary (stub for now)."""
    return jsonify({"success": True, "scanning": False, "folders": [], "total": 0})


@downloads_bp.route("/api/downloads/folder-duplicates")
def api_get_folder_duplicates():
    """Return folder duplicate info (stub for now)."""
    return jsonify({"success": True, "duplicates": [], "total": 0})


@downloads_bp.route("/api/downloads/folder/<path:folder_path>")
def api_get_folder_details(folder_path):
    return jsonify(get_folder_details(folder_path))


@downloads_bp.route("/api/downloads/folder/<path:folder_path>/cancel", methods=["POST"])
def api_cancel_folder(folder_path):
    return jsonify(cancel_folder_downloads(folder_path))


# =============================================================================
# ✅ SCAN / DISCOVERY
# =============================================================================

@downloads_bp.route("/api/downloads/scan")
def api_downloads_scan():
    from helpers.metadata_reader import read_mp3_metadata
    return jsonify(scan_downloads(read_mp3_metadata))


@downloads_bp.route("/api/downloads/discover", methods=["POST"])
def api_downloads_discover():
    return jsonify(discover_files())


@downloads_bp.route("/api/downloads/scan-progress")
def api_downloads_scan_progress():
    return jsonify(get_scan_progress())


@downloads_bp.route("/api/downloads/verify-moved-files")
def api_downloads_verify_moved():
    minutes = int(request.args.get("minutes_old", 30))
    return jsonify(verify_moved_files(minutes))


# =============================================================================
# ✅ GROUPING / MATCHING
# =============================================================================

@downloads_bp.route("/api/downloads/folder/<path:folder_path>/match-musicbrainz", methods=["POST"])
async def api_match_folder(folder_path):
    return jsonify(match_folder(folder_path, await request.get_json()))


@downloads_bp.route("/api/downloads/folder/<path:folder_path>/auto-match", methods=["POST"])
async def api_auto_match_folder(folder_path):
    return jsonify(auto_match_folder(folder_path, await request.get_json()))


@downloads_bp.route("/api/downloads/release/<source>/<release_id>/tracks")
def api_release_tracks(source, release_id):
    return jsonify(get_release_tracks(release_id=release_id, source=source))


@downloads_bp.route("/api/downloads/folder/<path:folder_path>/duplicates", methods=["POST"])
async def api_check_duplicates(folder_path):
    return jsonify(check_folder_duplicates(folder_path, await request.get_json()))
from services.downloads.match_orchestrator import apply_mbid_match_batch


@downloads_bp.route("/api/queue/apply-mbid-match-batch", methods=["POST"])
async def api_queue_apply_mbid_match_batch():
    try:
        data = (await request.get_json(force=True, silent=True)) or {}

        queue_ids = [
            int(x)
            for x in (data.get("queue_ids") or [])
            if str(x).isdigit()
        ]

        new_mbid = (data.get("new_mbid") or "").strip()
        new_artist = (data.get("new_artist") or "").strip()
        new_album = (data.get("new_album") or "").strip()

        result = apply_mbid_match_batch(
            queue_ids=queue_ids,
            new_mbid=new_mbid,
            new_artist=new_artist,
            new_album=new_album,
        )

        if not result.get("success"):
            return jsonify(result), 400

        return jsonify(result)

    except Exception as e:
        logging.error(f"[API] apply MBID batch failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

# =============================================================================
# ✅ ORGANIZE / MOVE / MERGE
# =============================================================================

@downloads_bp.route("/api/downloads/folder/<path:folder_path>/organize", methods=["POST"])
async def api_organize_folder(folder_path):
    return jsonify(organize_folder(folder_path, await request.get_json()))



@downloads_bp.route("/api/downloads/track/<int:track_index>/move", methods=["POST"])
async def api_move_track(track_index):
    return jsonify(organize_track(track_index, await request.get_json()))



@downloads_bp.route("/api/downloads/merge-folders", methods=["POST"])
async def api_merge_folders():
    return jsonify(merge_folders(await request.get_json()))


# =============================================================================
# ✅ PROCESSING
# =============================================================================


@downloads_bp.route("/api/downloads/process", methods=["POST"])
def api_process():
    return jsonify(process_next_batch())



@downloads_bp.route("/api/downloads/process-one", methods=["POST"])
async def api_process_one():
    return jsonify(process_single_file(await request.get_json()))


@downloads_bp.route("/api/downloads/process-retry", methods=["POST"])
def api_process_retry():
    return jsonify(process_retry_queue())


@downloads_bp.route("/api/downloads/process-albums", methods=["POST"])
def api_process_albums():
    return jsonify(process_albums())


@downloads_bp.route("/api/downloads/albums/use-existing", methods=["POST"])
async def api_use_existing():
    return jsonify(process_album_existing(await request.get_json()))


@downloads_bp.route("/api/downloads/albums/apply-match", methods=["POST"])
async def api_apply_match():
    return jsonify(apply_musicbrainz_match(await request.get_json()))


@downloads_bp.route("/api/downloads/release-tracks", methods=["POST"])
async def api_release_status():
    return jsonify(get_release_status(await request.get_json()))


# =============================================================================
# ✅ QUEUE
# =============================================================================

@downloads_bp.route("/api/downloads/queue")
def api_queue():
    limit = request.args.get("limit", 100, type=int)
    offset = request.args.get("offset", 0, type=int)
    try:
        from db.repositories.queue import get_active_queue
        status_counts = get_queue_status_counts()
        # Return the actual queue rows (queued/searching/downloading/failed),
        # paginated — this endpoint previously stubbed ``queue`` to [].
        items = get_active_queue(limit=max(1, min(limit + offset, 500)))
        if offset:
            items = items[offset:offset + limit]
        else:
            items = items[:limit]
        from db.repositories.queue import get_completed_queue
        completed = get_completed_queue(limit=min(limit, 50))
        return jsonify({
            "success": True,
            "queue": items,
            "completed": completed,
            "status_counts": status_counts or {},
            "total": sum(status_counts.values()) if status_counts else 0,
            "limit": limit,
            "offset": offset,
        })
    except Exception as exc:
        return jsonify({
            "success": False,
            "error": str(exc),
            "queue": [],
            "status_counts": {},
            "total": 0,
        })


@downloads_bp.route("/api/downloads/queue-upcoming", methods=["POST"])
async def api_queue_upcoming():
    """Queue an upcoming release (by ``upcoming_release_id``) for download.

    Only releases whose date has passed (``release_date <= today``) can be
    queued.  Inserts an album-typed item into ``download_queue`` (deduped by
    artist/title) and marks the upcoming row ``status = 'queued'``.
    """
    payload = await request.get_json(silent=True) or {}
    release_id = payload.get("upcoming_release_id") or payload.get("id")
    try:
        release_id = int(release_id)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "upcoming_release_id is required"}), 400

    try:
        from db.engine import db_session
        from db.repositories.queue import insert_queue_item
        from sqlalchemy import text

        with db_session() as session:
            row = session.execute(
                text("SELECT * FROM upcoming_releases WHERE id = :id"),
                {"id": release_id},
            ).fetchone()
        if row is None:
            return jsonify({"success": False, "error": "release not found"}), 404

        release = dict(row._mapping)
        artist = (release.get("artist_name") or "").strip()
        album = (release.get("album_name") or "").strip()
        rel_date = (release.get("release_date") or "").strip()
        if not artist or not album:
            return jsonify({"success": False, "error": "release has no artist/album"}), 400

        today = datetime.now().date().isoformat()
        if not rel_date or rel_date > today:
            return jsonify({
                "success": False,
                "error": "release not out yet" if rel_date else "release date unknown",
                "release_date": rel_date,
            }), 400

        year = int(rel_date[:4]) if len(rel_date) >= 4 and rel_date[:4].isdigit() else None
        release_mbid = (release.get("release_group_mbid") or "").strip() or None

        # Prefer the full MusicBrainz pipeline: resolve the release-group to a
        # concrete release and queue one download_queue row PER TRACK (the
        # same flow the MusicBrainz modal uses).  Falls back to a single
        # album-typed item when no MBID is matched yet or MB data can't be
        # fetched.
        if release_mbid:
            try:
                from services.downloads.download_pipeline_service import start_release_download
                result = start_release_download(
                    release_mbid,
                    album,
                    artist,
                    method="slskd",
                    create_folder_group=False,
                )
                if result.get("success"):
                    queued_tracks = int(result.get("queue_items_created") or 0)
                    with db_session() as session:
                        session.execute(
                            text("UPDATE upcoming_releases SET status = 'queued' WHERE id = :id"),
                            {"id": release_id},
                        )
                    try:
                        from services.queue.queue_signal import signal_new_item
                        signal_new_item()
                    except Exception:
                        pass
                    return jsonify({
                        "success": True,
                        "total_tracks": queued_tracks,
                        "queued_tracks": queued_tracks,
                        "artist": artist,
                        "album": album,
                        "release_group_mbid": release_mbid,
                    })
                logger.warning(
                    "[QUEUE_UPCOMING] MB pipeline failed for %s (%s): %s — falling back to single item",
                    release_id, album, result.get("error"),
                )
            except Exception as exc:
                logger.warning(
                    "[QUEUE_UPCOMING] MB pipeline error for %s (%s): %s — falling back to single item",
                    release_id, album, exc,
                )

        queued = insert_queue_item(
            artist=artist,
            title=album,
            album=album,
            source="soulseek",
            priority=5,
            year=year,
            release_mbid=release_mbid,
            import_type="album",
        )

        with db_session() as session:
            session.execute(
                text("UPDATE upcoming_releases SET status = 'queued' WHERE id = :id"),
                {"id": release_id},
            )

        return jsonify({
            "success": True,
            "already_queued": bool(queued.get("already_queued")),
            "queue_id": queued.get("id"),
            "artist": artist,
            "album": album,
        })
    except Exception as exc:
        logging.error("Failed to queue upcoming release %s: %s", release_id, exc, exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500


@downloads_bp.route("/api/downloads/clear-queue", methods=["POST"])
def api_clear_queue():
    return jsonify(_clear_queue())


@downloads_bp.route("/api/downloads/retry-queue")
def api_retry_queue():
    return jsonify(get_queue_status_counts())


@downloads_bp.route("/api/downloads/queue/grouped")
def api_grouped_queue():
    return jsonify(get_queue_status_counts())


@downloads_bp.route("/api/downloads/queue/batch-group", methods=["POST"])
async def api_batch_group():
    """Group selected queue items into a named album group.

    Port of the legacy batch-group endpoint: sets ``album`` on every item
    and, when a group artist is supplied, only overwrites mismatched artists
    when ``force_artist_override`` is set (prevents cross-artist corruption).
    """
    data = (await request.get_json(silent=True)) or {}
    item_ids = data.get("item_ids") or []
    group_name = str(data.get("group_name") or "").strip()
    group_artist = str(data.get("group_artist") or "").strip()
    force_artist_override = bool(data.get("force_artist_override", False))

    if not isinstance(item_ids, list) or not item_ids or not group_name:
        return jsonify({"success": False, "error": "item_ids and group_name are required"}), 400

    empty_artists = {"", "unknown", "various", "various artists"}

    from db.engine import db_session
    from sqlalchemy import text

    updated = artist_updated = artist_skipped = 0
    conflicts = []
    try:
        with db_session() as session:
            for item_id in item_ids:
                row = session.execute(
                    text("SELECT id, artist, title FROM download_queue WHERE id = :id"),
                    {"id": item_id},
                ).fetchone()
                if row is None:
                    continue

                item_artist = str(row[1] or "")
                item_title = str(row[2] or "")

                if group_artist:
                    current_norm = item_artist.strip().lower()
                    new_norm = group_artist.strip().lower()
                    should_update = (
                        force_artist_override
                        or current_norm in empty_artists
                        or current_norm == new_norm
                    )
                    if should_update:
                        session.execute(
                            text("""
                                UPDATE download_queue
                                SET album = :album, artist = :artist,
                                    updated_at = CURRENT_TIMESTAMP
                                WHERE id = :id
                            """),
                            {"album": group_name, "artist": group_artist, "id": item_id},
                        )
                        artist_updated += 1
                    else:
                        session.execute(
                            text("""
                                UPDATE download_queue
                                SET album = :album, updated_at = CURRENT_TIMESTAMP
                                WHERE id = :id
                            """),
                            {"album": group_name, "id": item_id},
                        )
                        artist_skipped += 1
                        conflicts.append({
                            "id": item_id,
                            "title": item_title,
                            "artist": item_artist,
                        })
                else:
                    session.execute(
                        text("""
                            UPDATE download_queue
                            SET album = :album, updated_at = CURRENT_TIMESTAMP
                            WHERE id = :id
                        """),
                        {"album": group_name, "id": item_id},
                    )
                updated += 1
    except Exception as exc:
        logging.error(f"[batch-group] {exc}")
        return jsonify({"success": False, "error": str(exc)}), 500

    return jsonify({
        "success": True,
        "updated": updated,
        "total": len(item_ids),
        "artist_updated": artist_updated,
        "artist_skipped": artist_skipped,
        "artist_conflicts": conflicts[:10],
        "message": f"Grouped {updated} items into '{group_name}'",
    })


@downloads_bp.route("/api/downloads/queue/<int:queue_id>", methods=["POST"])
async def api_manage_queue(queue_id):
    _data = (await request.get_json()) or {}
    return jsonify(update_queue_item(queue_id, **_data) or {"success": False})


# =============================================================================
# ✅ SCHEDULER
# =============================================================================

@downloads_bp.route("/api/downloads/scheduler/start", methods=["POST"])
def api_scheduler_start():
    return jsonify(start_scheduler())


@downloads_bp.route("/api/downloads/scheduler/stop", methods=["POST"])
def api_scheduler_stop():
    return jsonify(stop_scheduler())


@downloads_bp.route("/api/downloads/scheduler/status")
def api_scheduler_status():
    return jsonify(scheduler_status())

@downloads_bp.route("/api/downloads/pipeline/run", methods=["POST"])
def api_run_pipeline():
    from services.downloads.slskd_service import SlskdService
    from api_clients.slskd_http import SlskdHttpClient

    cfg = get_config()
    slskd_cfg = cfg.get("slskd", {})
    web_url = slskd_cfg.get("web_url", "http://localhost:5030")
    api_key = slskd_cfg.get("api_key", "")
    client = SlskdHttpClient(web_url=web_url, api_key=api_key)
    slskd = SlskdService(client)

    from services.downloads.download_pipeline_service import run_pipeline
    result = run_pipeline(slskd)

    return jsonify(result)

@downloads_bp.route("/api/downloads/pipeline/sync", methods=["POST"])
def api_pipeline_sync():
    from services.downloads.slskd_service import SlskdService
    from api_clients.slskd_http import SlskdHttpClient

    cfg = get_config()
    slskd_cfg = cfg.get("slskd", {})
    web_url = slskd_cfg.get("web_url", "http://localhost:5030")
    api_key = slskd_cfg.get("api_key", "")
    client = SlskdHttpClient(web_url=web_url, api_key=api_key)
    slskd = SlskdService(client)

    from services.downloads.download_pipeline_service import sync_transfers
    return jsonify(sync_transfers(slskd))


def process_single_file(data=None):
    from services.downloads.download_processing_service import (
        process_single_file as _impl,
    )
    return _impl(data or {})

def process_retry_queue():
    return process_next_batch()

def process_albums():
    from services.downloads.download_processing_service import (
        process_albums as _impl,
    )
    return _impl()
