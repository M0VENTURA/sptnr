"""
Queue cleanup routes.

Thin route layer only.
Cleanup logic must live in services.
"""

from __future__ import annotations


from flask import Blueprint, request
from routes.utils import json_response as _json_response


from services.queue.queue_cleanup_service import (
    queue_cleanup,
    queue_reset_moving,
    queue_cleanup_copied_sources,
    queue_cleanup_orphaned,
    queue_verify_and_prune,
    queue_delete_folder,
    queue_remove_group,
)

queue_cleanup_bp = Blueprint("queue_cleanup", __name__)



@queue_cleanup_bp.route("/api/queue/cleanup", methods=["POST"])
def api_queue_cleanup():
    return _json_response(queue_cleanup())


@queue_cleanup_bp.route("/api/queue/reset-moving", methods=["POST"])
def api_queue_reset_moving():
    payload = request.get_json(silent=True) or {}
    return _json_response(queue_reset_moving(payload))


@queue_cleanup_bp.route("/api/queue/cleanup-copied", methods=["POST"])
def api_queue_cleanup_copied_sources():
    return _json_response(queue_cleanup_copied_sources())


@queue_cleanup_bp.route("/api/queue/cleanup-orphaned", methods=["POST"])
def api_queue_cleanup_orphaned():
    payload = request.get_json(silent=True) or {}
    return _json_response(queue_cleanup_orphaned(payload))


@queue_cleanup_bp.route("/api/queue/verify-and-prune", methods=["POST"])
def api_queue_verify_and_prune():
    payload = request.get_json(silent=True) or {}
    return _json_response(queue_verify_and_prune(payload))


@queue_cleanup_bp.route("/api/queue/folder/delete", methods=["POST"])
def api_queue_delete_folder():
    payload = request.get_json(silent=True) or {}
    return _json_response(queue_delete_folder(payload))


@queue_cleanup_bp.route("/api/queue/group/remove", methods=["POST"])
def api_queue_remove_group():
    payload = request.get_json(silent=True) or {}
    return _json_response(queue_remove_group(payload))