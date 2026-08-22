"""API v1 — Artist endpoints."""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote

import structlog
from quart import jsonify
from sqlalchemy import text

from db.engine import db_session
from helpers.response_helpers import _fail, _ok
from . import api_v1_bp

logger = structlog.get_logger(__name__)


@api_v1_bp.route("/artists/<path:name>")
async def get_artist(name: str) -> Any:
    """Get artist details with album list."""
    name = unquote(name)
    try:
        with db_session() as session:
            result = session.execute(
                text("""SELECT MIN(album) as album, COUNT(*) as track_count, AVG(stars) as avg_stars,
                    MIN(year) as album_year
                    FROM tracks WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:name)
                    GROUP BY LOWER(TRIM(COALESCE(album, '')))
                    ORDER BY (MIN(year) IS NULL), MIN(year) DESC NULLS LAST"""),
                {"name": name},
            )
            albums = [dict(r._mapping) for r in result.fetchall()]

            result = session.execute(
                text("""SELECT COUNT(*) as track_count, COUNT(DISTINCT album) as album_count,
                    AVG(stars) as avg_stars
                    FROM tracks WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:name)"""),
                {"name": name},
            )
            row = result.fetchone()
            stats = dict(row._mapping) if row else {}
            
            payload, status = _ok(artist=name, albums=albums, stats=stats)
            return jsonify(payload), status
            
    except Exception as exc:
        logger.error("Failed to get artist details", artist=name, error=str(exc))
        payload, status = _fail(str(exc), 500)
        return jsonify(payload), status
