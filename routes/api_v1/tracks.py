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
        with db_session() as session:
            result = session.execute(
                text("""SELECT spotify_genres, lastfm_tags, musicbrainz_genres,
                    discogs_genres, essentia_genres, mood, listenbrainz_genres
                    FROM tracks WHERE CAST(id AS TEXT) = :id"""),
                {"id": track_id},
            )
            row = result.fetchone()
            if not row:
                return jsonify(_fail("Track not found", 404))

            import json as _json
            source_keys = ["discogs_genres", "mood", "essentia_genres",
                          "musicbrainz_genres", "lastfm_tags", "listenbrainz_genres", "spotify_genres"]
            genres = {}
            for idx, key in enumerate(source_keys):
                raw = row[idx]
                if not raw:
                    genres[key] = []
                    continue
                try:
                    parsed = _json.loads(raw) if isinstance(raw, str) else raw
                except Exception:
                    parsed = [g.strip() for g in str(raw).split(",") if g.strip()]
                if isinstance(parsed, list):
                    genres[key] = parsed
                else:
                    genres[key] = []
            return jsonify(_ok(genres=genres))
    except Exception as exc:
        return jsonify(_fail(str(exc), 500))
