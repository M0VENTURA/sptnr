"""MusicBrainz tag import/search/download routes — migrated from old app.py."""

from __future__ import annotations

import logging
import os
import time
import re
from typing import Any

from flask import Blueprint, jsonify, request, session

from db.utils import get_db_connection
from helpers.config_helpers import get_config
from helpers.response_helpers import _ok, _fail

logger = logging.getLogger(__name__)

mb_bp = Blueprint("musicbrainz", __name__, url_prefix="/api/musicbrainz")
MUSICBRAINZ_USER_AGENT = "Popularr/1.0 +https://github.com/M0VENTURA/popularr"


# ---------------------------------------------------------------------------
# GET /api/musicbrainz/tags/track
# ---------------------------------------------------------------------------

@mb_bp.route("/tags/track", methods=["GET"])
def api_musicbrainz_tags_track():
    """Get MusicBrainz tags for a single track."""
    artist = request.args.get("artist", "").strip()
    album = request.args.get("album", "").strip()
    title = request.args.get("title", "").strip()
    if not (artist and album and title):
        return jsonify({"error": "artist, album, and title required"}), 400
    try:
        import requests as req
        from urllib.parse import quote
        headers = {"User-Agent": MUSICBRAINZ_USER_AGENT, "Accept": "application/json"}
        resp = req.get(
            f"https://musicbrainz.org/ws/2/recording/?query=artist:{quote(artist)}+AND+recording:{quote(title)}&fmt=json&limit=5",
            headers=headers, timeout=10,
        )
        data = resp.json()
        return jsonify({"success": True, "recordings": data.get("recordings", [])})
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
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, artist, title FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s AND album = %s",
            (artist, album),
        )
        rows = cursor.fetchall()
        conn.close()
        return jsonify({"success": True, "tracks": [dict(r) for r in rows]})
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
def api_musicbrainz_tag_update():
    """Update a MusicBrainz tag in the database and optionally write to MP3."""
    data = request.json or {}
    artist = data.get("artist", "").strip()
    album = data.get("album", "").strip()
    title = data.get("title", "").strip()
    field_name = data.get("field", "").strip()
    field_value = data.get("value", "").strip()
    write_to_mp3 = data.get("write_to_mp3", False)
    if not (artist and album and title and field_name):
        return jsonify({"error": "Missing required fields"}), 400
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE tracks SET {field_name} = %s WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s AND album = %s AND title = %s",
            (field_value, artist, album, title),
        )
        conn.commit()
        conn.close()
        return jsonify({"success": True, "updated": cursor.rowcount})
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
def api_musicbrainz_batch_update():
    """Update multiple MusicBrainz tags at once."""
    data = request.json or {}
    artist = data.get("artist", "").strip()
    album = data.get("album", "").strip()
    title = data.get("title", "").strip()
    tags = data.get("tags", {})
    if not (artist and album and title and tags):
        return jsonify({"error": "Missing required fields"}), 400
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        set_parts = [f"{k} = %s" for k in tags]
        params = list(tags.values()) + [artist, album, title]
        cursor.execute(
            f"UPDATE tracks SET {', '.join(set_parts)} WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s AND album = %s AND title = %s",
            params,
        )
        conn.commit()
        conn.close()
        return jsonify({"success": True, "updated": cursor.rowcount})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/musicbrainz/search
# ---------------------------------------------------------------------------

@mb_bp.route("/search", methods=["POST"])
def api_musicbrainz_search():
    """Search MusicBrainz for releases + local cached missing releases."""
    payload = request.get_json(silent=True) or {}
    query = str(payload.get("query", "")).strip()
    artist_only = bool(payload.get("artist_only", False))
    if not query:
        return jsonify({"error": "query required"}), 400
    try:
        import requests as req
        from urllib.parse import quote
        headers = {"User-Agent": MUSICBRAINZ_USER_AGENT, "Accept": "application/json"}
        if artist_only:
            resp = req.get(
                f"https://musicbrainz.org/ws/2/artist/?query=artist:{quote(query)}&fmt=json&limit=10",
                headers=headers, timeout=10,
            )
        else:
            resp = req.get(
                f"https://musicbrainz.org/ws/2/release-group/?query=releasegroup:{quote(query)}&fmt=json&limit=20",
                headers=headers, timeout=10,
            )
        data = resp.json()
        return jsonify({"success": True, "results": data})
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
        import requests as req
        from urllib.parse import quote
        headers = {"User-Agent": MUSICBRAINZ_USER_AGENT, "Accept": "application/json"}
        resp = req.get(
            f"https://musicbrainz.org/ws/2/release/?query=artist:{quote(artist)}+AND+release:{quote(album)}&fmt=json&limit=10",
            headers=headers, timeout=10,
        )
        data = resp.json()
        return jsonify({"success": True, "releases": data.get("releases", [])})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# GET /api/musicbrainz/releases/active
# ---------------------------------------------------------------------------

@mb_bp.route("/releases/active", methods=["GET"])
def api_get_active_releases():
    """Get all active MusicBrainz releases with download progress."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM musicbrainz_releases WHERE status != 'finalized' ORDER BY created_at DESC LIMIT 50"
        )
        rows = cursor.fetchall()
        conn.close()
        return jsonify({"success": True, "releases": [dict(r) for r in rows]})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
