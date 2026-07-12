"""Log file reading service.

Provides Flask ``Response`` objects for streaming log files to the
WebUI dashboard. Supports:
- Reading last N lines from log files.
- Format detection for line-based vs. stream output.
- Security-constrained path resolution.
"""

from __future__ import annotations

import os
import re
import glob
import logging
from datetime import datetime
from quart import Response, send_file
from helpers.logging_config import resolve_log_dir

logger = logging.getLogger(__name__)

# =============================================================================
# ✅ HELPERS
# =============================================================================

def _read_last_lines(path, max_lines, chunk_size=65536, max_bytes=4 * 1024 * 1024):
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        file_size = fh.tell()
        data = b""
        cursor = file_size
        while cursor > 0 and data.count(b"\n") < max_lines + 200 and len(data) < max_bytes:
            read_size = min(chunk_size, cursor)
            cursor -= read_size
            fh.seek(cursor)
            data = fh.read(read_size) + data
    return data.decode("utf-8", errors="ignore").splitlines()[-max_lines:]

def _resolve_log_path(log_type: str) -> str | None:
    log_dir = resolve_log_dir()
    mapping = {
        "unified": "unified_scan.log",
        "info": "info.log",
        "debug": "debug.log",
    }
    base_name = mapping.get(log_type)
    if not base_name:
        return None
        
    full_path = os.path.join(log_dir, base_name)
    # Check for rotated files too
    for p in [full_path] + glob.glob(full_path + "*"):
        if os.path.exists(p):
            return p
    return None

# =============================================================================
# ✅ LOG ACCESS
# =============================================================================

def get_unified_log(lines: int, verbose: bool, path_candidates: list[str] | None = None):
    path_candidates = path_candidates or [] # Handle default
    log_path = next((p for p in path_candidates if p and os.path.exists(p)), _resolve_log_path("unified"))

    if not log_path:
        return {"error": "Unified log not found", "lines": []}, 404

    try:
        log_lines = _read_last_lines(log_path, lines)
        if not verbose:
            http_pattern = re.compile(r'"(GET|POST|PUT|DELETE|PATCH) /api/')
            skip_pattern = re.compile(r'\[api_unified_log\]|Checking match|Found \d+', re.I)
            log_lines = [l for l in log_lines if not http_pattern.search(l) and not skip_pattern.search(l)]
        return {"lines": log_lines[-lines:]}
    except Exception as e:
        logger.error("[LOG] unified read error: %s", e)
        return {"error": str(e), "lines": []}, 500

def download_log(log_type: str):
    if log_type == "search": return _generate_search_log()
    if log_type == "queue": return _generate_queue_log()

    log_path = _resolve_log_path(log_type)
    if not log_path:
        return {"error": f"Log not found: {log_type}"}, 404

    return _build_download_response(log_path, log_type)

def _generate_search_log():
    from db.repositories.search_logs import get_slskd_search_logs
    logs = get_slskd_search_logs(limit=200)
    lines = [f"[{e.get('timestamp')}] {e.get('query')} → {e.get('result_count')} results" for e in logs]
    return Response("\n".join(lines) or "No search logs", mimetype="text/plain")

def _generate_queue_log():
    from services.queue.queue_diagnostics_service import get_queue_events
    events = get_queue_events(limit=500)
    lines = [f"[{e.get('timestamp')}] {e.get('message')}" for e in events]
    return Response("\n".join(lines) or "No queue logs", mimetype="text/plain")



def _build_download_response(path, log_type):
    filename = f"{log_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    return send_file(path, as_attachment=True, download_name=filename, mimetype="text/plain")