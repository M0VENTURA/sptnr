"""API v1 — Track endpoints."""

from __future__ import annotations

from typing import Any

import structlog
from quart import jsonify
from sqlalchemy import text

from db.engine import db_session
from helpers.response_helpers import _fail, _ok
from services.enrichment.genre_tag_aggregator import get_track_genre_sources

from . import api_v1_bp

logger = structlog.get_logger(__name__)


@api_v1_bp.route("/tracks/<track_id>")
async def get_track(track_id: str) -> Any:
    """Get track metadata."""
    try:
        with db_session() as session:
            result = session.execute(
                text("SELECT * FROM tracks WHERE CAST(id AS TEXT) = :id"),
                {"id": track_id},
            )
            row = result.fetchone()
            if not row:
                payload, status = _fail("Track not found", 404)
                return jsonify(payload), status
                
            payload, status = _ok(track=dict(row._mapping))
            return jsonify(payload), status
    except Exception as exc:
        logger.error("Failed to get track metadata", track_id=track_id, error=str(exc))
        payload, status = _fail(str(exc), 500)
        return jsonify(payload), status


@api_v1_bp.route("/tracks/<track_id>/genres")
async def get_track_genres(track_id: str) -> Any:
    """Get all genre sources for a track."""
    try:
        with db_session() as session:
            result = session.execute(
                text("""SELECT spotify_genres, lastfm_tags, musicbrainz_genres,
                    discogs_genres, essentia_genres, mood, listenbrainz_genres,
                    navidrome_genres, manual_genres
                    FROM tracks WHERE CAST(id AS TEXT) = :id"""),
                {"id": track_id},
            )
            row = result.fetchone()
            if not row:
                payload, status = _fail("Track not found", 404)
                return jsonify(payload), status

            track_dict = dict(row._mapping)
            sources = get_track_genre_sources(track_dict)

            # Flatten to simple name lists for the API response.
            genres = {
                source: [t["name"] for t in tags]
                for source, tags in sources.items()
            }
            
            payload, status = _ok(genres=genres)
            return jsonify(payload), status
            
    except Exception as exc:
        logger.error("Failed to get track genres", track_id=track_id, error=str(exc))
        payload, status = _fail(str(exc), 500)
        return jsonify(payload), status
