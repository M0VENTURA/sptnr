"""API v1 — Artist endpoints."""

from __future__ import annotations

from quart import jsonify
from sqlalchemy import text

from db.engine import db_session
from helpers.response_helpers import _ok, _fail
from . import api_v1_bp


@api_v1_bp.route("/artists/<path:name>")
async def get_artist(name: str):
    """Get artist details with album list."""
    from urllib.parse import unquote
    name = unquote(name)
    try:
        with db_session() as session:
            result = session.execute(
                text("""SELECT album, COUNT(*) as track_count, AVG(stars) as avg_stars,
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
            stats = dict(result.fetchone()._mapping) if result.fetchone() else {}
            return jsonify(_ok(artist=name, albums=albums, stats=stats))
    except Exception as exc:
        return jsonify(_fail(str(exc), 500))
