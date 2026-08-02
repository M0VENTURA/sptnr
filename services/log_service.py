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
            # Dashboard mode: keep only scan-related lines.
            # Full log is available on the /logs page.
            scan_pattern = re.compile(
                r'\[POPULARITY\]|\[TRACK_STAGE\]|\[ALBUM_STAGE\]|\[FINALISE_STAGE\]|'
                r'\[LOAD_STAGE\]|\[scan_runner\]|\[LIBRARY_SYNC\]|'
                r'\[SINGLE\]|Navidrome Import|Artist scan|'
                r'popularity scan|Popularity |popularity_scan|'
                r'Full library scan|Boot scan|Scan complete|Scan failed|'
                r'Scan stopped|single detection|star ratings',
                re.I,
            )
            log_lines = [l for l in log_lines if scan_pattern.search(l)]
        return {"lines": log_lines[-lines:]}
    except Exception as e:
        logger.error("[LOG] unified read error: %s", e)
        return {"error": str(e), "lines": []}, 500

def resolve_log_file_path(name: str) -> str | None:
    """Resolve a log filename to an absolute path, constrained to the log dir.

    Only plain ``.log`` filenames (no path separators, no ``..``) are accepted
    so the /logs page can never read arbitrary files on the host.
    """
    name = (name or "").strip()
    if not name:
        return None
    if "/" in name or "\\" in name or name in (".", ".."):
        return None
    if not name.endswith(".log"):
        return None

    log_dir = resolve_log_dir()
    full = os.path.realpath(os.path.join(log_dir, name))
    if not full.startswith(os.path.realpath(log_dir) + os.sep):
        return None
    if not os.path.isfile(full):
        return None
    return full


def get_log_file_content(name: str, lines: int = 500):
    """Return the tail of an arbitrary log file from the log directory.

    Returns ``{"lines": [...]}`` or an error tuple.
    """
    try:
        lines = max(1, min(int(lines), 2000))
    except (TypeError, ValueError):
        lines = 500

    log_path = resolve_log_file_path(name)
    if not log_path:
        return {"error": f"Log not found: {name}", "lines": []}, 404

    try:
        log_lines = _read_last_lines(log_path, lines)
        return {"lines": log_lines[-lines:]}
    except Exception as exc:
        logger.error("[LOG] read error for %s: %s", name, exc)
        return {"error": str(exc), "lines": []}, 500


async def download_log(log_type: str):
    if log_type == "search": return await _generate_search_log()
    if log_type == "queue": return await _generate_queue_log()

    log_path = _resolve_log_path(log_type)
    if not log_path:
        return {"error": f"Log not found: {log_type}"}, 404

    return await _build_download_response(log_path, log_type)

async def _generate_search_log():
    from db.repositories.search_logs import get_slskd_search_logs
    logs = get_slskd_search_logs(limit=200)
    lines = [f"[{e.get('timestamp')}] {e.get('query')} → {e.get('result_count')} results" for e in logs]
    return Response("\n".join(lines) or "No search logs", mimetype="text/plain")

async def _generate_queue_log():
    from services.queue.queue_diagnostics_service import get_queue_events, _tail_queue_log
    events = get_queue_events(limit=500)
    if not events:
        events = _tail_queue_log(limit=500)
    lines = [f"[{e.get('timestamp') or e.get('created_at')}] {e.get('message')}" for e in events]
    return Response("\n".join(lines) or "No queue logs", mimetype="text/plain")



async def _build_download_response(path, log_type):
    filename = f"{log_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    return await send_file(path, as_attachment=True, download_name=filename, mimetype="text/plain")