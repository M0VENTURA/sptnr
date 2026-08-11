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


def _read_all_lines(path):
    """Read an entire log file (used by the /logs page's full-history view)."""
    with open(path, "rb") as fh:
        data = fh.read()
    return data.decode("utf-8", errors="ignore").splitlines()


def _count_lines(path: str) -> int:
    """Count newline-terminated lines without loading the file into memory.

    A 24MB access.log would otherwise be read fully just to report how many
    lines exist for the "showing last N of M" banner.
    """
    count = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)  # 1 MiB
            if not chunk:
                break
            count += chunk.count(b"\n")
    return count


def stream_append_log(name: str, start_offset: int, pending: str):
    """Return ``(new_lines, new_offset, new_pending)`` for SSE log streaming.

    Reads everything appended to ``name`` since ``start_offset``, splits it
    into complete lines (a trailing partial line is carried in ``pending``),
    and applies the same unified_scan.log filters the page view uses.
    Handles rotation/truncation by resetting the offset.
    """
    log_path = resolve_log_file_path(name)
    if not log_path or not os.path.exists(log_path):
        return [], start_offset, pending
    try:
        size = os.path.getsize(log_path)
    except OSError:
        return [], start_offset, pending
    if size < start_offset:
        start_offset = 0
        pending = ""
    if size == start_offset:
        return [], start_offset, pending
    try:
        with open(log_path, "rb") as fh:
            fh.seek(start_offset)
            pending += fh.read().decode("utf-8", errors="ignore")
    except OSError:
        return [], start_offset, pending
    new_offset = size
    pieces = pending.split("\n")
    new_pending = pieces.pop()  # may be a partial line
    lines = [p for p in pieces if p]
    if name == "unified_scan.log":
        noise_pattern = _scheduler_noise_filter()
        scan_pattern = _scan_activity_filter()
        lines = [l for l in lines if not noise_pattern.search(l) and scan_pattern.search(l)]
    return lines, new_offset, new_pending


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


_LOG_SOURCE_FILES = {
    "scanner": "unified_scan.log",
    "soulseek": "search.log",
    "navidrome": "info.log",
    "system": "error.log",
}


def _filter_lines_since(lines: list[str], hours: float) -> list[str]:
    """Keep only lines whose leading timestamp falls within the last *hours*.

    Lines without a parseable ``YYYY-MM-DD HH:MM:SS`` prefix (multi-line
    tracebacks, continuation lines) are kept when the log already has content.
    """
    if not lines or hours <= 0:
        return lines
    from datetime import datetime as _dt, timezone, timedelta

    cutoff = _dt.now(timezone.utc) - timedelta(hours=hours)
    _TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})")
    kept: list[str] = []
    for line in lines:
        match = _TS_RE.match(line)
        if not match:
            if kept:
                kept.append(line)  # traceback continuation
            continue
        try:
            text = match.group(1).replace(" ", "T")
            ts = _dt.fromisoformat(text)
            if ts.tzinfo is None:
                ts = ts.astimezone()  # naive stamps are host-local time
            else:
                ts = ts.astimezone(timezone.utc)
            if ts >= cutoff:
                kept.append(line)
        except Exception:
            if kept:
                kept.append(line)
    return kept


def export_log_lines_since(source: str, hours: int = 1) -> str:
    """Return the last *hours* of log lines for a named source as text.

    Source mapping: scanner → unified_scan.log, soulseek → search.log,
    navidrome → info.log, system → error.log.
    """
    name = _LOG_SOURCE_FILES.get(str(source or "").strip().lower(), "unified_scan.log")
    path = resolve_log_file_path(name)
    if not path or not os.path.exists(path):
        return f"No log entries found for '{source}' in the last {hours} hour(s)."
    try:
        with open(path, "rb") as fh:
            lines = fh.read().decode("utf-8", errors="ignore").splitlines()
    except OSError as exc:
        return f"Could not read log file: {exc}"
    filtered = _filter_lines_since(lines, hours)
    if not filtered:
        return f"No log entries found for '{source}' in the last {hours} hour(s)."
    return "\n".join(filtered) + "\n"


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
    """Bookkeeping that must never surface in the dashboard log.

    Scheduler registrations plus the downloads watcher's periodic
    ``[SCAN] Discovered N audio files`` line (the queue worker's
    maintenance cycle scans the downloads folder every ~30s — the dashboard
    panel is for popularity/singles scanning activity, not watcher churn).
    """
    return re.compile(
        r'APScheduler|job store|Added job|registered .* every .* min|'
        r'Scheduler started|Scheduler shutdown|Scheduler paused|Scheduler resumed|'
        r'\[SCAN\] Discovered \d+ audio files',
        re.I,
    )


def _scan_activity_filter() -> re.Pattern:
    """Dashboard scanning panel: ONLY popularity/singles scan activity.

    Queue/download/retry/slskd lines belong to the Queue Activity and
    Soulseek Search logs, not the dashboard's scanning panel.
    """
    return re.compile(
        r'\[POPULARITY\]|\[TRACK_STAGE\]|\[TRACK\]|\[TRACK_RESULT\]|'
        r'\[ALBUM_STAGE\]|\[FINALISE_STAGE\]|\[LOAD_STAGE\]|'
        r'\[scan_runner\]|\[LIBRARY_SYNC\]|'
        r'\[SINGLE\]|Navidrome Import|Artist scan|'
        r'popularity scan|Popularity |popularity_scan|'
        r'Full library scan|Boot scan|Scan complete|Scan failed|'
        r'Scan stopped|single detection|Singles Detection|SCAN RESULTS|'
        r'SINGLE CONF|Distribution:|Navidrome: synced|star ratings|★',
        re.I,
    )


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
            # Dashboard mode: only popularity/singles scanning activity.
            # Queue/download/retry/slskd lines are visible in the Queue
            # Activity and Soulseek Search logs instead.
            noise_pattern = _scheduler_noise_filter()
            scan_pattern = _scan_activity_filter()
            log_lines = [
                l for l in log_lines
                if not noise_pattern.search(l) and scan_pattern.search(l)
            ]
        if last_hour:
            # Dashboard scanning panel: only surface the last hour of activity.
            log_lines = _filter_lines_last_hour(log_lines)
        if not log_lines:
            # Old-system parity: when the unified log has nothing (fresh
            # boot), surface info.log's tail — filtered the same way so the
            # panel only ever shows scan activity.
            info_path = _resolve_log_path("info")
            if info_path and os.path.exists(info_path):
                info_lines = _read_last_lines(info_path, lines)
                if not verbose:
                    info_lines = [
                        l for l in info_lines
                        if not _scheduler_noise_filter().search(l)
                        and _scan_activity_filter().search(l)
                    ]
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


def get_log_file_content(name: str, lines: int | str = 500):
    """Return the tail of an arbitrary log file from the log directory.

    ``unified_scan.log`` is filtered to scan activity only, so the /logs page
    shows the same scanning feed as the dashboard's Scanning Log panel (which
    reads it via ``/api/unified-log``).  Other files are returned raw.

    ``lines`` accepts a count (1-2000) or ``"all"`` / ``0`` / ``-1`` to return
    the file's FULL history (no tail truncation).

    Returns ``{"lines": [...]}`` or an error tuple.
    """
    all_lines = str(lines).strip().lower() in ("all", "0", "-1")
    if not all_lines:
        try:
            lines = max(1, min(int(lines), 2000))
        except (TypeError, ValueError):
            lines = 500

    log_path = resolve_log_file_path(name)
    if not log_path:
        return {"error": f"Log not found: {name}", "lines": []}, 404

    try:
        if all_lines:
            log_lines = _read_all_lines(log_path)
        else:
            log_lines = _read_last_lines(log_path, lines)
        if name == "unified_scan.log":
            noise_pattern = _scheduler_noise_filter()
            scan_pattern = _scan_activity_filter()
            log_lines = [
                l for l in log_lines
                if not noise_pattern.search(l) and scan_pattern.search(l)
            ]
        # Truncation metadata for the "showing last N of M lines" banner.
        total_lines = _count_lines(log_path)
        visible = log_lines if all_lines else log_lines[-lines:]
        return {
            "lines": visible,
            "total_lines": total_lines,
            "truncated": bool(not all_lines and total_lines > len(visible)),
        }
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