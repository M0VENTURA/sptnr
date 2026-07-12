"""API v1 — Track endpoints."""

from __future__ import annotations

from quart import jsonify
from sqlalchemy import text

from db.engine import db_session
from helpers.response_helpers import _ok, _fail
from . import api_v1_bp


@api_v1_bp.route("/tracks/<track_id>")
async def get_track(track_id: str):
    """Get track metadata."""
    try:
        with db_session() as session:
            result = session.execute(
                text("SELECT * FROM tracks WHERE CAST(id AS TEXT) = :id"),
                {"id": track_id},
            )
            row = result.fetchone()
            if not row:
                return jsonify(_fail("Track not found", 404))
            return jsonify(_ok(track=dict(row._mapping)))
    except Exception as exc:
        return jsonify(_fail(str(exc), 500))


@api_v1_bp.route("/tracks/<track_id>/genres")
async def get_track_genres(track_id: str):
    """Get all genre sources for a track."""
    try:
        from services.enrichment.genre_tag_aggregator import get_track_genre_sources

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
                return jsonify(_fail("Track not found", 404))

            keys = ["spotify_genres", "lastfm_tags", "musicbrainz_genres",
                    "discogs_genres", "essentia_genres", "mood",
                    "listenbrainz_genres", "navidrome_genres", "manual_genres"]
            track_dict = dict(zip(keys, [row[i] for i in range(len(keys))]))
            sources = get_track_genre_sources(track_dict)

            # Flatten to simple name lists for the API response.
            genres = {
                source: [t["name"] for t in tags]
                for source, tags in sources.items()
            }
            return jsonify(_ok(genres=genres))
    except Exception as exc:
        return jsonify(_fail(str(exc), 500))
