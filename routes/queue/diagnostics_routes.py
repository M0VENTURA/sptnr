"""
Queue diagnostics routes.
"""

from __future__ import annotations


from flask import Blueprint, request
from routes.utils import json_response as _json_response


from services.queue.queue_diagnostics_service import (
    queue_processor_status,
    queue_processor_restart,
    queue_search_events,
    queue_events,
    queue_check_collection_batch,
    queue_slskd_eligibility_diagnostics,
)

queue_diagnostics_bp = Blueprint("queue_diagnostics", __name__)



@queue_diagnostics_bp.route("/api/queue-processor/status", methods=["GET"])
def api_queue_processor_status():
    return _json_response(queue_processor_status())


@queue_diagnostics_bp.route("/api/queue-processor/restart", methods=["POST"])
def api_queue_processor_restart():
    return _json_response(queue_processor_restart())


@queue_diagnostics_bp.route("/api/queue/events", methods=["GET"])
def api_queue_events():
    return _json_response(queue_events(request.args))


@queue_diagnostics_bp.route("/api/queue/search-events", methods=["GET"])
def api_queue_search_events():
    return _json_response(queue_search_events(request.args))


@queue_diagnostics_bp.route("/api/queue/check-collection-batch", methods=["POST"])
def api_queue_check_collection_batch():
    payload = request.get_json(silent=True) or {}
    return _json_response(queue_check_collection_batch(payload))


@queue_diagnostics_bp.route("/api/queue/diagnostics/slskd-eligibility", methods=["GET"])
def api_queue_slskd_eligibility_diagnostics():
    return _json_response(queue_slskd_eligibility_diagnostics())