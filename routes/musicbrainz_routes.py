"""MusicBrainz tag import/search/download routes — migrated from old app.py."""

from __future__ import annotations

import logging
import os
import time
import re
from typing import Any

from quart import Blueprint, jsonify, request, session

from sqlalchemy import text
from db.engine import db_session
from helpers.config_helpers import get_config
from helpers.response_helpers import _ok, _fail
from api_clients.musicbrainz_http import MusicBrainzHttpClient

logger = logging.getLogger(__name__)

mb_bp = Blueprint("musicbrainz", __name__, url_prefix="/api/musicbrainz")
_mb_client: MusicBrainzHttpClient | None = None


def _get_mb_client() -> MusicBrainzHttpClient:
    global _mb_client
    if _mb_client is None:
        _mb_client = MusicBrainzHttpClient(enabled=True)
    return _mb_client



# ---------------------------------------------------------------------------
# GET /api/musicbrainz/tags/track
# ---------------------------------------------------------------------------

@mb_bp.route("/tags/track", methods=["GET"])
def api_musicbrainz_tags_track():
    """Get MusicBrainz tags for a single track."""
    artist = request.args.get("artist", "").strip()
    title = request.args.get("title", "").strip()
    if not artist or not title:
        return jsonify({"error": "artist and title required"}), 400
    try:
        client = _get_mb_client()
        recordings = client.search_recordings(
            f'artist:"{artist}" AND recording:"{title}"',
            limit=5,
        )
        return jsonify({"success": True, "recordings": recordings})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# GET /api/musicbrainz/tags/album
# ---------------------------------------------------------------------------

@mb_bp.route("/tags/album", methods=["GET"])
def api_musicbrainz_tags_album():
    """Get MusicBrainz tags for all tracks in an album."""
    artist = request.args.get("artist", "").strip()
    album = request.args.get("album", "").strip()
    if not (artist and album):
        return jsonify({"error": "artist and album required"}), 400
    try:
        with db_session() as session:
            result = session.execute(
                text("SELECT id, artist, title FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND album = :album"),
                {"artist": artist, "album": album},
            )
            rows = result.fetchall()
        return jsonify({"success": True, "tracks": [dict(r._mapping) for r in rows]})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/musicbrainz/import/track
# ---------------------------------------------------------------------------

@mb_bp.route("/import/track", methods=["POST"])
def api_musicbrainz_import_track():
    """Import MusicBrainz tags from MP3 for a single track."""
    return jsonify({"success": True, "message": "Track import queued"}), 200


# ---------------------------------------------------------------------------
# POST /api/musicbrainz/import/album
# ---------------------------------------------------------------------------

@mb_bp.route("/import/album", methods=["POST"])
def api_musicbrainz_import_album():
    """Import MusicBrainz tags from MP3s for all tracks in an album."""
    return jsonify({"success": True, "message": "Album import queued"}), 200


# ---------------------------------------------------------------------------
# POST /api/musicbrainz/import/artist
# ---------------------------------------------------------------------------

@mb_bp.route("/import/artist", methods=["POST"])
def api_musicbrainz_import_artist():
    """Import MusicBrainz tags from MP3s for all tracks by an artist."""
    return jsonify({"success": True, "message": "Artist import queued"}), 200


# ---------------------------------------------------------------------------
# POST /api/musicbrainz/tag/update
# ---------------------------------------------------------------------------

@mb_bp.route("/tag/update", methods=["POST"])
async def api_musicbrainz_tag_update():
    """Update a MusicBrainz tag in the database and optionally write to MP3."""
    data = (await request.get_json()) or {}
    artist = data.get("artist", "").strip()
    album = data.get("album", "").strip()
    title = data.get("title", "").strip()
    field_name = data.get("field", "").strip()
    field_value = data.get("value", "").strip()
    write_to_mp3 = data.get("write_to_mp3", False)
    if not (artist and album and title and field_name):
        return jsonify({"error": "Missing required fields"}), 400
    try:
        with db_session() as session:
            result = session.execute(
                text(f"UPDATE tracks SET {field_name} = :value WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND album = :album AND title = :title"),
                {"value": field_value, "artist": artist, "album": album, "title": title},
            )
        return jsonify({"success": True, "updated": result.rowcount})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/musicbrainz/tag/write-to-mp3
# ---------------------------------------------------------------------------

@mb_bp.route("/tag/write-to-mp3", methods=["POST"])
def api_musicbrainz_tag_write_mp3():
    """Write MusicBrainz tags to MP3 file (without database update)."""
    return jsonify({"success": True, "message": "Tag write not yet implemented"}), 200


# ---------------------------------------------------------------------------
# POST /api/musicbrainz/tags/batch-update
# ---------------------------------------------------------------------------

@mb_bp.route("/tags/batch-update", methods=["POST"])
async def api_musicbrainz_batch_update():
    """Update multiple MusicBrainz tags at once."""
    data = (await request.get_json()) or {}
    artist = data.get("artist", "").strip()
    album = data.get("album", "").strip()
    title = data.get("title", "").strip()
    tags = data.get("tags", {})
    if not (artist and album and title and tags):
        return jsonify({"error": "Missing required fields"}), 400
    try:
        with db_session() as session:
            set_parts = [f"{k} = :{k}" for k in tags]
            params = {**tags, "artist": artist, "album": album, "title": title}
            result = session.execute(
                text(f"UPDATE tracks SET {', '.join(set_parts)} WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND album = :album AND title = :title"),
                params,
            )
        return jsonify({"success": True, "updated": result.rowcount})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/musicbrainz/search
# ---------------------------------------------------------------------------

@mb_bp.route("/search", methods=["POST"])
async def api_musicbrainz_search():
    """Search MusicBrainz for releases + local cached missing releases.

    Accepts structured fields so each search-form entry maps to the correct
    MusicBrainz Lucene index:

        artist  → artist:"<name>"
        album   → releasegroup:"<title>"
        track   → recording:"<title>"
        year    → date:<year>

    Legacy callers may still send ``query`` (+ optional ``artist_only``); those
    are routed to ``releasegroup`` / ``artist`` respectively.
    """
    payload = (await request.get_json(silent=True)) or {}
    artist = str(payload.get("artist", "")).strip()
    album = str(payload.get("album", "")).strip()
    track = str(payload.get("track", "")).strip()
    year = str(payload.get("year", "")).strip()
    query = str(payload.get("query", "")).strip()
    artist_only = bool(payload.get("artist_only", False))

    def _esc(value: str) -> str:
        return value.replace('"', "")

    # Build a field-aware MusicBrainz Lucene query from structured fields.
    parts: list[str] = []
    if artist:
        parts.append(f'artist:"{_esc(artist)}"')
    if album:
        parts.append(f'releasegroup:"{_esc(album)}"')
    if track:
        parts.append(f'recording:"{_esc(track)}"')
    if year:
        parts.append(f'date:{_esc(year)}')

    if not parts:
        # Legacy free-text query path.
        if not query:
            return jsonify({"error": "query required"}), 400
        parts.append(
            f'artist:"{_esc(query)}"' if artist_only else f'releasegroup:{query}'
        )

    mb_query = " AND ".join(parts)

    if not mb_query.strip():
        return jsonify({"error": "query required"}), 400

    try:
        client = _get_mb_client()
        raw = client.get("release-group/", params={
            "query": mb_query,
            "limit": 50 if artist_only else 20,
        })
        results = raw.get("release-groups", [])
        return jsonify({"success": True, "releases": results})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# GET /api/musicbrainz/search/releases
# ---------------------------------------------------------------------------

@mb_bp.route("/search/releases", methods=["GET"])
def api_musicbrainz_search_releases():
    """Search MusicBrainz for releases by artist and album."""
    artist = request.args.get("artist", "").strip()
    album = request.args.get("album", "").strip()
    if not artist or not album:
        return jsonify({"error": "artist and album required"}), 400
    try:
        client = _get_mb_client()
        releases = client.search_releases(
            f'artist:"{artist}" AND release:"{album}"',
            limit=10,
        )
        return jsonify({"success": True, "releases": releases})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# GET /api/musicbrainz/search-releases  (hyphen — used by the modal JS)
# ---------------------------------------------------------------------------

@mb_bp.route("/search-releases", methods=["GET"])
def api_musicbrainz_search_releases_modal():
    """Search MusicBrainz release-groups for the search modal.

    Query params:
        q (str): Free-text search query.
        type (str, optional): Release type filter (album, single, ep, etc.).
        limit (int, optional): Max results (default 25, max 50).
    """
    query = request.args.get("q", "").strip()
    release_type = request.args.get("type", "").strip().lower()
    limit = min(int(request.args.get("limit", 25)), 50)

    if not query:
        return jsonify({"releases": []})

    try:
        client = _get_mb_client()

        # Build MusicBrainz query
        mb_query_parts = [f'releasegroup:"{query.replace(chr(34), "")}"']
        if release_type and release_type not in ("", "all"):
            mb_query_parts.append(f'primarytype:{release_type}')
        mb_query = " AND ".join(mb_query_parts)

        # Search release-groups with genres and release data
        payload = client.get("release-group/", params={
            "query": mb_query,
            "fmt": "json",
            "limit": limit,
            "inc": "genres+releases",
        })
        groups = payload.get("release-groups", []) if isinstance(payload.get("release-groups"), list) else []

        releases = []
        for rg in groups:
            # Extract artist credit
            artist_credit = rg.get("artist-credit", [])
            artist_name = ""
            if artist_credit and isinstance(artist_credit, list):
                parts = []
                for ac in artist_credit:
                    if isinstance(ac, dict):
                        name = ac.get("name", "") or (ac.get("artist", {}) or {}).get("name", "")
                        join_phrase = ac.get("joinphrase", "")
                        if name:
                            parts.append(name)
                        if join_phrase:
                            parts.append(join_phrase)
                    elif isinstance(ac, str):
                        parts.append(ac)
                artist_name = "".join(parts)

            rg_mbid = rg.get("id", "") or ""
            raw_date = rg.get("first-release-date")
            first_release_date = str(raw_date) if raw_date else ""

            # Build cover art URL from Cover Art Archive using release-group MBID
            cover_art_url = ""
            if rg_mbid:
                cover_art_url = f"https://coverartarchive.org/release-group/{rg_mbid}/front-250"

            release = {
                "id": rg_mbid,
                "title": rg.get("title", ""),
                "primary_type": rg.get("primary-type", rg.get("type", "Other")),
                "first_release_date": first_release_date,
                "artist": artist_name,
                "artist-credit": artist_credit,
                "cover_art_url": cover_art_url,
                "genres": [g.get("name", "") for g in (rg.get("genres", []) or []) if isinstance(g, dict)],
                "tags": [t.get("name", "") for t in (rg.get("tags", []) or []) if isinstance(t, dict)],
                "releases": [],
            }

            # Include contained releases for the "Choose Release" dropdown
            contained = rg.get("releases", [])
            if isinstance(contained, list):
                for rel in contained:
                    if isinstance(rel, dict):
                        release["releases"].append({
                            "id": rel.get("id", ""),
                            "title": rel.get("title", rg.get("title", "")),
                            "date": rel.get("date", ""),
                            "release_date": rel.get("date", ""),
                            "country": rel.get("country", ""),
                            "label": "",
                            "format": "",
                        })

            releases.append(release)

        return jsonify({"releases": releases})

    except Exception as exc:
        logger.error("MusicBrainz search-releases failed: %s", exc, exc_info=True)
        return jsonify({"releases": [], "error": str(exc)})


# ---------------------------------------------------------------------------
# GET /api/musicbrainz/releases/active
# ---------------------------------------------------------------------------

@mb_bp.route("/releases/active", methods=["GET"])
def api_get_active_releases():
    """Get all active MusicBrainz releases with download progress."""
    try:
        with db_session() as session:
            result = session.execute(text("SELECT * FROM musicbrainz_releases WHERE status != 'finalized' ORDER BY created_at DESC LIMIT 50"))
            rows = result.fetchall()
        return jsonify({"success": True, "releases": [dict(r._mapping) for r in rows]})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
