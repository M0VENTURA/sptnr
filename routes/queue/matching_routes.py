"""
Queue matching routes.
"""

from __future__ import annotations


from flask import Blueprint, request
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

queue_matching_bp = Blueprint("queue_matching", __name__)

# -----------------------------------------------------------------------------
# Move to music (single track)
# -----------------------------------------------------------------------------

@queue_matching_bp.route("/api/queue/move-to-music/<int:queue_id>", methods=["POST"])
def api_queue_move_to_music(queue_id: int):
    payload = request.get_json(silent=True) or {}
    return _json_response(organize_track(queue_id, payload))


# -----------------------------------------------------------------------------
# Organize
# -----------------------------------------------------------------------------

@queue_matching_bp.route("/api/queue/<int:queue_id>/organize", methods=["POST"])
def api_queue_organize(queue_id: int):
    payload = request.get_json(silent=True) or {}
    return _json_response(organize_track(queue_id, payload))


@queue_matching_bp.route("/api/queue/organize-group", methods=["POST"])
def api_queue_organize_group():
    payload = request.get_json(silent=True) or {}
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
def api_match_folder():
    payload = request.get_json(silent=True) or {}
    folder_path = payload.get("folder_path")
    if not folder_path:
        return _json_response({
            "success": False,
            "error": "folder_path is required"
        })

    return _json_response(match_folder(folder_path, payload))


@queue_matching_bp.route("/api/queue/auto-match-folder", methods=["POST"])
def api_auto_match_folder():
    payload = request.get_json(silent=True) or {}
    folder_path = payload.get("folder_path")
    if not folder_path:
        return _json_response({
            "success": False,
            "error": "folder_path is required"
        })

    return _json_response(auto_match_folder(folder_path, payload))