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

def _scheduler_noise_filter() -> re.Pattern:
    """Scheduler bookkeeping that must never surface in the dashboard log."""
    return re.compile(
        r'APScheduler|job store|Added job|registered .* every .* min|'
        r'Scheduler started|Scheduler shutdown|Scheduler paused|Scheduler resumed',
        re.I,
    )


def _filter_lines_last_hour(lines: list[str]) -> list[str]:
    """Keep only lines whose leading timestamp falls within the last hour.

    The dashboard's scanning panel should only show the last hour of activity
    (the /logs page reads the file directly and shows the full history).  Lines
    without a parseable ``YYYY-MM-DD HH:MM:SS`` prefix are kept as-is.
    """
    if not lines:
        return lines
    from datetime import datetime as _dt, timezone, timedelta

    cutoff = _dt.now(timezone.utc) - timedelta(hours=1)
    _TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})")
    kept = []
    for line in lines:
        match = _TS_RE.match(line)
        if not match:
            kept.append(line)
            continue
        try:
            text = match.group(1).replace(" ", "T")
            ts = _dt.fromisoformat(text)
            if ts.tzinfo is None:
                # Log timestamps are written in the host's local time — treat
                # naive stamps as local time rather than UTC.
                ts = ts.astimezone()
            else:
                ts = ts.astimezone(timezone.utc)
            if ts >= cutoff:
                kept.append(line)
        except Exception:
            kept.append(line)
    return kept


def get_unified_log(lines: int, verbose: bool, path_candidates: list[str] | None = None, last_hour: bool = False):
    path_candidates = path_candidates or [] # Handle default
    log_path = next((p for p in path_candidates if p and os.path.exists(p)), _resolve_log_path("unified"))

    if not log_path or not os.path.exists(log_path):
        # The file may not exist yet on a fresh volume — the dashboard must
        # render an empty panel, not a 404/500 it silently swallows.
        return {"lines": [], "message": "unified_scan.log not found yet — it appears after the first log write."}

    try:
        log_lines = _read_last_lines(log_path, lines)
        if not verbose:
            # Dashboard mode: drop scheduler bookkeeping only. The unified
            # log is the activity feed (scans, queue, imports); narrowing it
            # to scan-pattern lines made the panel blank whenever the last
            # hour held only queue activity (and right after boot).
            noise_pattern = _scheduler_noise_filter()
            log_lines = [l for l in log_lines if not noise_pattern.search(l)]
        if last_hour:
            # Dashboard scanning panel: only surface the last hour of activity.
            log_lines = _filter_lines_last_hour(log_lines)
        if not log_lines:
            # Old-system parity: when the unified log has nothing (fresh
            # boot, queue-only runs), surface info.log's tail so the panel
            # is never blank.
            info_path = _resolve_log_path("info")
            if info_path and os.path.exists(info_path):
                info_lines = _read_last_lines(info_path, lines)
                if not verbose:
                    info_lines = [l for l in info_lines if not _scheduler_noise_filter().search(l)]
                if last_hour:
                    info_lines = _filter_lines_last_hour(info_lines)
                log_lines = info_lines
        return {"lines": log_lines[-lines:]}
    except Exception as e:
        logger.error("[LOG] unified read error: %s", e)
        return {"lines": [], "message": f"unified log read failed: {e}"}

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


def _filter_last_hour(events: list[dict], keys: tuple[str, ...] = ("timestamp", "created_at")) -> list[dict]:
    """Keep only events whose timestamp is within the last 60 minutes.

    The UI labels these exports "Download Last Hour" — enforce that window so
    the file does not silently contain the entire history.
    """
    if not events:
        return events

    from datetime import timezone, timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    kept = []
    for event in events:
        raw = None
        for key in keys:
            raw = event.get(key)
            if raw:
                break
        if not raw:
            continue
        try:
            text = str(raw).replace("Z", "+00:00")
            ts = datetime.fromisoformat(text)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            else:
                ts = ts.astimezone(timezone.utc)
            if ts >= cutoff:
                kept.append(event)
        except Exception:
            # Unparseable timestamp — keep the row (better than dropping data).
            kept.append(event)
    return kept


async def _generate_search_log():
    from db.repositories.search_logs import get_slskd_search_logs
    logs = get_slskd_search_logs(limit=200)
    logs = _filter_last_hour(logs, keys=("timestamp", "created_at"))
    lines = [f"[{e.get('timestamp')}] {e.get('query')} → {e.get('result_count')} results" for e in logs]
    return Response("\n".join(lines) or "No search logs", mimetype="text/plain")


async def _generate_queue_log():
    from services.queue.queue_diagnostics_service import get_queue_events, _tail_queue_log
    events = get_queue_events(limit=500)
    if not events:
        events = _tail_queue_log(limit=500)
    events = _filter_last_hour(events)
    lines = [f"[{e.get('timestamp') or e.get('created_at')}] {e.get('message')}" for e in events]
    return Response("\n".join(lines) or "No queue logs", mimetype="text/plain")



async def _build_download_response(path, log_type):
    filename = f"{log_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    return await send_file(path, as_attachment=True, download_name=filename, mimetype="text/plain")