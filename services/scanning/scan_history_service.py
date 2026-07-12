"""Scan history query service.

Read-only access to the ``scan_history`` table for WebUI dashboard display.

Key Functions:
    - get_recent_album_scans(): Fetch recent scan records with timestamps.
    - Per-artist scan history queries.

Architecture:
    Pure query layer with no mutations. Results are returned as dicts with
    column names mapped from the database schema.
"""

from db.utils import get_db_connection


def get_recent_album_scans(limit: int = 50):
    conn = get_db_connection()
    try:
        cur = conn.cursor()

        cur.execute("""
            SELECT *
            FROM scan_history
            ORDER BY started_at DESC
            LIMIT %s
        """, (limit,))

        cols = [c[0] for c in cur.description]

        return [
            dict(zip(cols, row))
            for row in cur.fetchall() or []
        ]

    finally:
        conn.close()