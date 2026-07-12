"""Scan resume and fallback helpers.

Provides optional DB-based resume support as a fallback when file-based
checkpoints are unavailable.

Architecture:
    File-based checkpoints (``scan_state.py``) are the primary resume
    mechanism. This module provides an optional DB-based fallback for
    audit visibility or environments where file state is unreliable.

Key Functions:
    - get_resume_artist_from_db(): Return the most recently scanned artist
      from the scan history table.
"""

from __future__ import annotations

from db.utils import get_db_connection, row_get


def get_resume_artist_from_db() -> str | None:
    """
    Return most recently scanned artist from DB history.

    Used ONLY as a fallback if file checkpoint is missing.
    """
    conn = get_db_connection()

    try:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT artist_name
            FROM scan_history
            ORDER BY scanned_at DESC
            LIMIT 1
        """)

        row = cursor.fetchone()
        return row_get(row, "artist_name", 0)

    except Exception:
        return None

    finally:
        conn.close()