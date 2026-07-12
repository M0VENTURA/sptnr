"""Standard Flask API response helpers.

Provides consistent JSON response formatting for all API routes.
Prefer importing from this module instead of generic helpers.

Key Functions:
    - api_ok(): Return a success response with optional message and data.
    - api_fail(): Return an error response with message and status code.
    - api_success(): Return a success response with timestamp.

Response Format:
    Success: {"success": true, "data": ..., "message": ...}
    Error:   {"success": false, "error": "..."}

Architecture:
    Route/web concern. Separated from generic helpers to maintain clear
    dependency boundaries between infrastructure and web layers.
"""

from __future__ import annotations

from datetime import datetime
from flask import jsonify


def api_ok(message: str | None = None, status: int = 200, **kwargs):
    body = {"success": True}
    if message is not None:
        body["message"] = message
    body.update(kwargs)
    return jsonify(body), status


def api_fail(message: str, status: int = 400, **kwargs):
    body = {"success": False, "error": message}
    body.update(kwargs)
    return jsonify(body), status


def api_success(data=None, message=None, status=200, **kwargs):
    response = {"success": True, "data": data, "timestamp": datetime.now().isoformat()}
    if message:
        response["message"] = message
    response.update(kwargs)
    return jsonify(response), status


def api_error(code, message, status=400, details=None, **kwargs):
    error_obj = {"code": code, "message": message, "timestamp": datetime.now().isoformat()}
    if details is not None:
        error_obj["details"] = details
    response = {"success": False, "error": error_obj}
    response.update(kwargs)
    return jsonify(response), status
