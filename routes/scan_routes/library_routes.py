"""Library sync API routes."""
from __future__ import annotations
from flask import Blueprint
from services.library.library_sync_service import get_library_sync_state, request_library_sync
from services.web.api_response import api_ok

library_bp = Blueprint("library", __name__, url_prefix="/api/library")


@library_bp.route("/sync", methods=["POST"])
def trigger_library_sync():
    return api_ok(**request_library_sync())


@library_bp.route("/status", methods=["GET"])
def library_status():
    return api_ok(**get_library_sync_state())

