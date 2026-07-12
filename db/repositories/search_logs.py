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
import psycopg2.extras
from db.utils import get_db_connection

logger = logging.getLogger(__name__)

# Note: If you don't have a shared lock file, 
# consider moving this to a dedicated sync utility.
_slskd_search_logs_lock = None 

def get_slskd_search_logs(limit: int = 50) -> list[dict]:
    """Get recent Soulseek search logs from database."""
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT created_at AS timestamp, search_type, query, queue_id,
                       artist, title, album, result_count, duration_seconds,
                       notes, selected_result, results
                FROM slskd_search_logs
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cursor.fetchall()
            logs = []
            for row in rows:
                log = dict(row)
                # Handle ISO format and JSON parsing
                if log.get('timestamp'):
                    log['timestamp'] = log['timestamp'].isoformat()
                for field in ('selected_result', 'results'):
                    if isinstance(log.get(field), str):
                        log[field] = json.loads(log[field])
                logs.append(log)
            return logs
    except Exception as db_err:
        logger.error(f"[SEARCH_LOG] Database read error: {db_err}")
        return []
    finally:
        conn.close()

def clear_slskd_search_logs() -> None:
    """Clear all Soulseek search logs."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("TRUNCATE TABLE slskd_search_logs")
            conn.commit()
    except Exception as db_err:
        logger.error(f"[SEARCH_LOG] Could not truncate table: {db_err}")
    finally:
        conn.close()