"""Download management routes.

Handles:
- Download folder display and matching.
- Queue item management (add, cancel, retry).
- MusicBrainz matching workflows.
- Download orchestration triggers.
"""

from __future__ import annotations

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
    return jsonify(get_queue_status_counts())


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
def api_batch_group():
    return jsonify({"success": False, "error": "Not implemented"})


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
    return {"success": False, "message": "Not implemented"}

def process_retry_queue():
    return process_next_batch()

def process_albums():
    return {"success": False, "message": "Not implemented"}
