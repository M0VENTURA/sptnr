"""
Standardized API response helpers for Flask endpoints.

Provides consistent response formats for success and error cases across all API endpoints.
All responses follow a standard structure for predictable frontend parsing.

Usage:
    from helpers.api_response import api_success, api_error
    
    @app.route("/api/example", methods=["GET"])
    def example_endpoint():
        try:
            result = some_operation()
            return api_success({"data": result}, status=200)
        except ValueError as e:
            return api_error("validation_error", str(e), status=400)
        except Exception as e:
            return api_error("internal_error", str(e), status=500)
"""

from flask import jsonify
from datetime import datetime


def api_success(data=None, message=None, status=200, **kwargs):
    """
    Return a standardized success response.
    
    Args:
        data: Response payload (dict, list, or any JSON-serializable type)
        message: Optional success message
        status: HTTP status code (default: 200)
        **kwargs: Additional fields to include in response (e.g., count=10)
    
    Returns:
        Flask JSON response with status code
    
    Example:
        return api_success(
            {"user": "john", "email": "john@example.com"},
            message="User created successfully",
            status=201
        )
        
    Response structure:
        {
            "success": true,
            "data": {...},
            "message": "...",
            "timestamp": "2026-03-04T12:00:00.000000",
            // additional fields from **kwargs
        }
    """
    response = {
        "success": True,
        "data": data,
        "timestamp": datetime.now().isoformat()
    }
    
    if message:
        response["message"] = message
    
    # Add any additional fields
    response.update(kwargs)
    
    return jsonify(response), status


def api_error(code, message, status=400, details=None, **kwargs):
    """
    Return a standardized error response.
    
    Args:
        code: Machine-readable error code (e.g., "validation_error", "not_found")
        message: Human-readable error message
        status: HTTP status code (default: 400)
        details: Optional error details (dict, list, or string)
        **kwargs: Additional fields to include in response (e.g., field="email")
    
    Returns:
        Flask JSON response with status code
    
    Example:
        return api_error(
            "validation_error",
            "Email is required",
            status=400,
            field="email"
        )
        
    Response structure:
        {
            "success": false,
            "error": {
                "code": "...",
                "message": "...",
                "details": {...},
                "timestamp": "..."
            },
            // additional fields from **kwargs
        }
    """
    error_obj = {
        "code": code,
        "message": message,
        "timestamp": datetime.now().isoformat()
    }
    
    if details is not None:
        error_obj["details"] = details
    
    response = {
        "success": False,
        "error": error_obj
    }
    
    # Add any additional fields
    response.update(kwargs)
    
    return jsonify(response), status


def api_paginated(items, page=1, per_page=50, total=None, message=None, status=200):
    """
    Return a standardized paginated response.
    
    Args:
        items: List of items for current page
        page: Current page number (1-based)
        per_page: Items per page
        total: Total number of items (if unknown, omit)
        message: Optional message
        status: HTTP status code (default: 200)
    
    Returns:
        Flask JSON response with pagination metadata
    
    Example:
        users = get_users(page=1, per_page=50)
        return api_paginated(
            users["items"],
            page=1,
            per_page=50,
            total=users["total"]
        )
        
    Response structure:
        {
            "success": true,
            "data": [...],
            "pagination": {
                "page": 1,
                "per_page": 50,
                "total": 1000,
                "pages": 20
            },
            "message": "...",
            "timestamp": "..."
        }
    """
    pagination = {
        "page": page,
        "per_page": per_page
    }
    
    if total is not None:
        pagination["total"] = total
        pagination["pages"] = (total + per_page - 1) // per_page  # Ceiling division
    
    response = {
        "success": True,
        "data": items,
        "pagination": pagination,
        "timestamp": datetime.now().isoformat()
    }
    
    if message:
        response["message"] = message
    
    return jsonify(response), status


def api_created(data, message="Created successfully", location=None, status=201):
    """
    Return a standardized creation response (201 Created).
    
    Args:
        data: Created resource data
        message: Optional message
        location: Optional Location header value (for REST convention)
        status: HTTP status code (default: 201)
    
    Returns:
        Flask JSON response with 201 status
    
    Example:
        playlist = create_playlist({"name": "My Playlist"})
        return api_created(
            playlist,
            location=f"/api/playlists/{playlist['id']}"
        )
    """
    response = {
        "success": True,
        "data": data,
        "timestamp": datetime.now().isoformat()
    }
    
    if message:
        response["message"] = message
    
    # Note: Caller should set Location header themselves if needed
    # response_obj.headers["Location"] = location
    
    return jsonify(response), status


# ---------------------------------------------------------------------------
# Flat helpers — backward-compatible with existing JS consumers
# ---------------------------------------------------------------------------
# The existing `api_success` / `api_error` helpers above use a nested structure
# that is incompatible with legacy JS consumers that read `data.error` as a
# plain string.  These flat helpers match the shape the frontend already expects:
#
#   Success:  {"success": true,  "message": "...", ...extra_fields}
#   Failure:  {"success": false, "error": "...",   ...extra_fields}
#
# Use `api_ok` / `api_fail` for all new routes (and when migrating existing
# routes) to keep a consistent, frontend-safe shape.


def api_ok(message: str | None = None, status: int = 200, **kwargs):
    """Return ``{"success": true, "message": ..., ...kwargs}`` with *status*.

    Args:
        message: Optional human-readable success message.
        status:  HTTP status code (default 200).
        **kwargs: Any extra fields to merge into the top-level response dict
                  (e.g. ``queue_id=42``, ``requeued=5``).

    Example::

        return api_ok("Item re-queued", queue_id=queue_id)
        # → {"success": true, "message": "Item re-queued", "queue_id": 42}
    """
    body: dict = {"success": True}
    if message is not None:
        body["message"] = message
    body.update(kwargs)
    return jsonify(body), status


def api_fail(message: str, status: int = 400, **kwargs):
    """Return ``{"success": false, "error": message, ...kwargs}`` with *status*.

    Args:
        message: Human-readable error message (exposed as ``data.error`` in JS).
        status:  HTTP status code (default 400).
        **kwargs: Any extra fields to merge into the top-level response dict.

    Example::

        return api_fail("Queue item not found", status=404)
        # → {"success": false, "error": "Queue item not found"}, 404
    """
    body: dict = {"success": False, "error": message}
    body.update(kwargs)
    return jsonify(body), status


# Common HTTP error status codes with default messages
HTTP_ERROR_CODES = {
    400: ("bad_request", "Bad request"),
    401: ("unauthorized", "Unauthorized"),
    403: ("forbidden", "Forbidden"),
    404: ("not_found", "Not found"),
    409: ("conflict", "Conflict"),
    422: ("unprocessable_entity", "Unprocessable entity"),
    429: ("rate_limited", "Rate limit exceeded"),
    500: ("internal_server_error", "Internal server error"),
    503: ("service_unavailable", "Service unavailable"),
}


def api_http_error(status, message=None, **kwargs):
    """
    Return a standardized HTTP error response with auto-selected code.
    
    Args:
        status: HTTP status code
        message: Custom error message (uses default if not provided)
        **kwargs: Additional fields
    
    Returns:
        Flask JSON response
    
    Example:
        if not found:
            return api_http_error(404, "Playlist not found")
    """
    default_code, default_message = HTTP_ERROR_CODES.get(status, ("error", "Error"))
    return api_error(
        default_code,
        message or default_message,
        status=status,
        **kwargs
    )
