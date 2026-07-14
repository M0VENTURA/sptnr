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

from sqlalchemy import text

from db.engine import db_session


def get_resume_artist_from_db() -> str | None:
    """
    Return most recently scanned artist from DB history.

    Used ONLY as a fallback if file checkpoint is missing.
    """
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT artist_name
                    FROM scan_history
                    ORDER BY scanned_at DESC
                    LIMIT 1
                """)
            )
            row = result.fetchone()
            return str(row[0]) if row else None
    except Exception:
        return None