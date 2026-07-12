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
    download_log,
)

logs_bp = Blueprint(
    "logs",
    __name__,
)


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
        lines    - number of lines to return (1-2000)
        verbose  - include verbose/debug entries (0 or 1)
    """

    try:
        lines = int(request.args.get("lines", 400))
        lines = max(1, min(lines, 2000))
    except (TypeError, ValueError):
        lines = 400

    verbose = request.args.get("verbose", "0") == "1"

    # Now calling with just lines and verbose, as service resolves paths internally
    result = get_unified_log(
        lines=lines,
        verbose=verbose,
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
def api_download_log(log_type: str):
    """
    Download a log file.
    """

    result = download_log(log_type)

    # If result is a Flask Response (e.g. from _build_download_response), 
    # return it directly.
    if isinstance(result, Response):
        return result

    # Otherwise, handle the (payload, status) tuple
    if isinstance(result, tuple) and len(result) == 2:
        payload, status = result
        return jsonify(payload), status

    return jsonify(result)