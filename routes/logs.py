"""
Log viewing and download routes.
"""

from __future__ import annotations

from quart import (
    Blueprint,
    jsonify,
    request,
    Response,
)

from services.log_service import (
    get_unified_log,
    get_log_file_content,
    download_log,
)

logs_bp = Blueprint(
    "logs",
    __name__,
)


# =============================================================================
# LOG FILE CONTENT (any file in the log dir)
# =============================================================================

@logs_bp.route("/api/log-file", methods=["GET"])
def api_log_file():
    """Return the tail of an arbitrary log file from the log directory.

    Query params:
        name  - log filename (e.g. ``info.log``, ``debug.log``)
        lines - number of lines (1-2000, default 500), or ``all`` for the
                full file history (used by the /logs page)
    """
    name = request.args.get("name", "").strip()
    lines = request.args.get("lines", "500")
    result = get_log_file_content(name, lines)
    if isinstance(result, tuple) and len(result) == 2:
        payload, status = result
        return jsonify(payload), status
    return jsonify(result)


# =============================================================================
# UNIFIED LOG
# =============================================================================

@logs_bp.route(
    "/api/unified-log",
    methods=["GET"],
)
def api_unified_log():
    """
    Return unified log content.

    Query parameters:
        lines     - number of lines to return (1-2000)
        verbose   - include verbose/debug entries (0 or 1)
        last_hour - only return lines within the last hour (0 or 1)
    """

    try:
        lines = int(request.args.get("lines", 400))
        lines = max(1, min(lines, 2000))
    except (TypeError, ValueError):
        lines = 400

    verbose = request.args.get("verbose", "0") == "1"
    last_hour = request.args.get("last_hour", "0") == "1"

    # Now calling with just lines and verbose, as service resolves paths internally
    result = get_unified_log(
        lines=lines,
        verbose=verbose,
        last_hour=last_hour,
    )

    if isinstance(result, tuple) and len(result) == 2:
        payload, status = result
        return jsonify(payload), status

    return jsonify(result)


# =============================================================================
# DOWNLOAD LOG FILE
# =============================================================================

@logs_bp.route(
    "/api/download-log/<log_type>",
    methods=["GET"],
)
async def api_download_log(log_type: str):
    """
    Download a log file.
    """

    result = await download_log(log_type)

    # If result is a Flask Response (e.g. from _build_download_response), 
    # return it directly.
    if isinstance(result, Response):
        return result

    # Otherwise, handle the (payload, status) tuple
    if isinstance(result, tuple) and len(result) == 2:
        payload, status = result
        return jsonify(payload), status

    return jsonify(result)