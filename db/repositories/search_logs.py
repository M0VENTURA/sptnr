"""Soulseek search log repository.

Provides DB queries for ``slskd_search_logs`` table.
Logs are written during Soulseek searches for debugging and
auditing purposes.

Responsibilities:
- ``get_slskd_search_logs`` – Retrieve recent search logs.
- ``log_slskd_search`` – Record a new search event.
- ``get_search_stats`` – Aggregate search metrics.
"""

from __future__ import annotations
import json
import logging
from sqlalchemy import text
from db.engine import db_session

logger = logging.getLogger(__name__)

# Note: If you don't have a shared lock file, 
# consider moving this to a dedicated sync utility.
_slskd_search_logs_lock = None 

def get_slskd_search_logs(limit: int = 50) -> list[dict]:
    """Get recent Soulseek search logs from database."""
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT created_at AS timestamp, search_type, query, queue_id,
                           artist, title, album, result_count, duration_seconds,
                           notes, selected_result, results
                    FROM slskd_search_logs
                    ORDER BY created_at DESC
                    LIMIT :limit
                """),
                {"limit": limit},
            )
            logs = []
            for row in result.fetchall():
                log = dict(row._mapping)
                # Handle ISO format and JSON parsing
                if log.get('timestamp'):
                    from datetime import datetime as dt
                    if isinstance(log['timestamp'], dt):
                        log['timestamp'] = log['timestamp'].isoformat()
                for field in ('selected_result', 'results'):
                    if isinstance(log.get(field), str):
                        log[field] = json.loads(log[field])
                logs.append(log)
            return logs
    except Exception as db_err:
        logger.error(f"[SEARCH_LOG] Database read error: {db_err}")
        return []


def clear_slskd_search_logs() -> None:
    """Clear all Soulseek search logs."""
    try:
        with db_session() as session:
            session.execute(text("TRUNCATE TABLE slskd_search_logs"))
    except Exception as db_err:
        logger.error(f"[SEARCH_LOG] Could not truncate table: {db_err}")


def log_slskd_search(
    *,
    search_type: str,
    query: str,
    queue_id: int | None = None,
    artist: str = "",
    title: str = "",
    album: str = "",
    result_count: int = 0,
    duration_seconds: float | None = None,
    notes: str | None = None,
    selected_result: dict | None = None,
    results: list | None = None,
) -> None:
    """Record a Soulseek search event for diagnostics/auditing.

    Mirrors the legacy ``download_queue_manager.log_slskd_search_event``:
    every automatic/manual search is written to ``slskd_search_logs`` so the
    UI diagnostics view and ``log_service`` can report what happened.
    """
    try:
        import json as _json
        with db_session() as session:
            session.execute(
                text("""
                    INSERT INTO slskd_search_logs
                        (search_type, query, queue_id, artist, title, album,
                         result_count, duration_seconds, notes, selected_result, results)
                    VALUES
                        (:search_type, :query, :queue_id, :artist, :title, :album,
                         :result_count, :duration_seconds, :notes,
                         :selected_result::jsonb, :results::jsonb)
                """),
                {
                    "search_type": search_type,
                    "query": query,
                    "queue_id": queue_id,
                    "artist": artist,
                    "title": title,
                    "album": album,
                    "result_count": int(result_count or 0),
                    "duration_seconds": duration_seconds,
                    "notes": notes,
                    "selected_result": _json.dumps(selected_result) if selected_result else None,
                    "results": _json.dumps(results) if results else None,
                },
            )
    except Exception as db_err:
        logger.debug("[SEARCH_LOG] Could not record search event: %s", db_err)