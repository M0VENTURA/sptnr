"""Scan history query service.

Read-only access to the ``scan_history`` table for WebUI dashboard display.

Key Functions:
    - get_recent_album_scans(): Fetch recent scan records with timestamps.
    - Per-artist scan history queries.

Architecture:
    Pure query layer with no mutations. Results are returned as dicts with
    column names mapped from the database schema.
"""

from sqlalchemy import text
from db.engine import db_session


def get_recent_album_scans(limit: int = 50):
    with db_session() as session:
        result = session.execute(text("""
            SELECT *
            FROM scan_history
            ORDER BY started_at DESC
            LIMIT :limit
        """), {"limit": limit})

        cols = list(result.keys())

        return [
            dict(zip(cols, row))
            for row in result.fetchall() or []
        ]