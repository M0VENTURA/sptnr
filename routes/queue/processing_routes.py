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
