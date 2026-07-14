"""Scan history query and recording service.

Provides read/write access to the ``scan_history`` table for WebUI dashboard
display and for recording scan events.

Key Functions:
    - record_scan(): Insert a scan event (start, complete, or failure).
    - get_recent_album_scans(): Fetch recent scan records with timestamps.

Architecture:
    Query and mutation layer for scan_history.  Pipeline code should call
    ``record_scan()`` at start and completion so the dashboard always has
    up-to-date data.
"""

from datetime import datetime
from sqlalchemy import text
from db.engine import db_session


def record_scan(
    scan_type: str,
    status: str,
    message: str = "",
    artist: str | None = None,
    album: str | None = None,
    started_at: datetime | None = None,
) -> None:
    """Record a scan event in the scan_history table.

    Args:
        scan_type: Type of scan (e.g. 'full', 'artist', 'popularity', 'navidrome').
        status: 'started', 'completed', or 'failed'.
        message: Human-readable description or error message.
        artist: Optional artist name this scan was for.
        album: Optional album name this scan was for.
        started_at: When the scan started.  If omitted, defaults to now.
    """
    try:
        with db_session() as session:
            if status == "started":
                session.execute(
                    text("""
                        INSERT INTO scan_history
                            (scan_type, status, message, artist, album, started_at)
                        VALUES (:scan_type, 'started', :message, :artist, :album, :started_at)
                    """),
                    {
                        "scan_type": scan_type,
                        "message": message,
                        "artist": artist,
                        "album": album,
                        "started_at": started_at or datetime.utcnow(),
                    },
                )
            else:
                # Update the most recent 'started' row for this scan_type/artist
                session.execute(
                    text("""
                        UPDATE scan_history
                        SET status = :status,
                            message = :message,
                            completed_at = :completed_at,
                            duration_seconds = EXTRACT(EPOCH FROM (:completed_at - started_at))
                        WHERE id = (
                            SELECT id FROM scan_history
                            WHERE scan_type = :scan_type
                              AND (artist IS NOT DISTINCT FROM :artist)
                              AND status = 'started'
                            ORDER BY started_at DESC
                            LIMIT 1
                        )
                    """),
                    {
                        "scan_type": scan_type,
                        "status": status,
                        "message": message,
                        "artist": artist,
                        "completed_at": datetime.utcnow(),
                    },
                )
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Failed to record scan history")


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