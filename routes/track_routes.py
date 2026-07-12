"""Track API routes — migrated from the old monolithic app.py."""

from __future__ import annotations

import logging
import os
from typing import Any

from flask import Blueprint, jsonify, request, Response, send_file

from sqlalchemy import text

from db.engine import db_session
from helpers.config_helpers import get_config
from helpers.response_helpers import _ok, _fail

logger = logging.getLogger(__name__)

track_bp = Blueprint("track_api", __name__, url_prefix="/api/track")


def _coerce_optional_int(value: Any, allow_prefix: bool = False) -> int | None:
    """Return an int for numeric input, otherwise None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    candidate = text
    if allow_prefix and "/" in text:
        candidate = text.split("/", 1)[0].strip()
    if not candidate:
        return None
    signless = candidate[1:] if candidate.startswith("-") else candidate
    if not signless.isdigit():
        return None
    try:
        return int(candidate)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# GET /api/track/<track_id> — single track metadata
# ---------------------------------------------------------------------------

@track_bp.route("/<track_id>", methods=["GET"])
def api_get_track(track_id):
    """Get track metadata by ID."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tracks WHERE CAST(id AS TEXT) = %s", (track_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return jsonify({"error": "Track not found"}), 404
        return jsonify({"success": True, "track": dict(row)})
    except Exception as exc:
        logger.error("Error fetching track %s: %s", track_id, exc)
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# GET /api/track/<track_id>/audio — stream audio file
# ---------------------------------------------------------------------------

@track_bp.route("/<track_id>/audio")
def api_track_audio(track_id):
    """Stream an audio file for in-browser playback."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT file_path FROM tracks WHERE CAST(id AS TEXT) = %s", (track_id,))
        row = cursor.fetchone()
        conn.close()
        if not row or not row.get("file_path"):
            return Response("", status=404)
        file_path = row["file_path"]
        if "__queued_for_download__" in file_path:
            return Response("", status=404)
        resolved = os.path.realpath(file_path)
        if not os.path.isfile(resolved):
            return Response("", status=404)
        cfg = get_config()
        music_folder = os.path.realpath(
            cfg.get("navidrome", {}).get("music_folder", "")
            or os.environ.get("MUSIC_FOLDER", "")
            or os.environ.get("MUSIC_DIR", "/music")
        )
        if not resolved.startswith(music_folder + os.sep) and resolved != music_folder:
            return Response("", status=403)
        ext = os.path.splitext(resolved)[1].lower()
        mime_map = {
            ".mp3": "audio/mpeg", ".flac": "audio/flac", ".ogg": "audio/ogg",
            ".opus": "audio/ogg; codecs=opus", ".m4a": "audio/mp4",
            ".aac": "audio/aac", ".wav": "audio/wav",
        }
        return send_file(resolved, mimetype=mime_map.get(ext, "application/octet-stream"), conditional=True)
    except Exception as exc:
        logger.error("Error streaming track %s: %s", track_id, exc)
        return Response("", status=500)


# ---------------------------------------------------------------------------
# POST /api/track/<track_id>/rename-file
# ---------------------------------------------------------------------------

@track_bp.route("/<track_id>/rename-file", methods=["POST"])
def api_track_rename_file(track_id):
    """Rename/move a single track's file using the configured naming format."""
    try:
        from services.infrastructure.filesystem_service import get_import_destination_path
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT file_path, artist, album, title, track_number FROM tracks WHERE CAST(id AS TEXT) = %s", (track_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return jsonify({"success": False, "error": "Track not found"}), 404
        metadata = dict(row)
        src = metadata.get("file_path", "")
        if not src or not os.path.exists(src):
            return jsonify({"success": False, "error": "File not found"}), 404
        dest = get_import_destination_path(src, "", {})
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.rename(src, dest)
        conn2 = get_db_connection()
        try:
            c2 = conn2.cursor()
            c2.execute("UPDATE tracks SET file_path = %s WHERE CAST(id AS TEXT) = %s", (dest, track_id))
            conn2.commit()
        finally:
            conn2.close()
        return jsonify({"success": True, "old_path": src, "new_path": dest})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/track/<track_id>/toggle-manual-single
# ---------------------------------------------------------------------------

@track_bp.route("/<track_id>/toggle-manual-single", methods=["POST"])
def api_toggle_manual_single(track_id):
    """Toggle single_manual_override flag for a track."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT single_manual_override FROM tracks WHERE CAST(id AS TEXT) = %s", (track_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Track not found"}), 404
        current = bool(row.get("single_manual_override", False)) if hasattr(row, "get") else bool(row[0] if row else False)
        new_val = 0 if current else 1
        cursor.execute("UPDATE tracks SET single_manual_override = %s WHERE CAST(id AS TEXT) = %s", (new_val, track_id))
        conn.commit()
        conn.close()
        return jsonify({"success": True, "single_manual_override": bool(new_val)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/track/favourite
# ---------------------------------------------------------------------------

@track_bp.route("/favourite", methods=["GET", "POST", "DELETE"])
def api_track_favourite():
    """Check, add, or remove a track from favourites."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        if request.method == "GET":
            track_id = request.args.get("track_id", "").strip()
            if not track_id:
                return jsonify({"error": "track_id required"}), 400
            cursor.execute(
                "SELECT 1 FROM bookmarks WHERE type = 'track_favourite' AND LOWER(name) = LOWER(%s) LIMIT 1",
                (track_id,),
            )
            return jsonify({"success": True, "is_favourite": cursor.fetchone() is not None}), 200
        elif request.method == "POST":
            data = request.json or {}
            track_id = str(data.get("track_id") or "").strip()
            if not track_id:
                return jsonify({"error": "track_id required"}), 400
            cursor.execute(
                "INSERT INTO bookmarks (type, name) VALUES ('track_favourite', %s) ON CONFLICT DO NOTHING",
                (track_id,),
            )
            conn.commit()
            return jsonify({"success": True, "is_favourite": True}), 200
        elif request.method == "DELETE":
            track_id = request.args.get("track_id", "").strip()
            if not track_id:
                return jsonify({"error": "track_id required"}), 400
            cursor.execute(
                "DELETE FROM bookmarks WHERE type = 'track_favourite' AND LOWER(name) = LOWER(%s)", (track_id,),
            )
            conn.commit()
            return jsonify({"success": True, "is_favourite": False}), 200
        return jsonify({"error": "Unsupported method"}), 405
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# POST /api/track/update-metadata
# ---------------------------------------------------------------------------

@track_bp.route("/update-metadata", methods=["POST"])
def api_track_update_metadata():
    """Update track metadata comprehensively."""
    try:
        data = request.json or {}
        track_id = str(data.get("track_id") or "").strip()
        if not track_id:
            return jsonify({"error": "track_id required"}), 400
        conn = get_db_connection()
        cursor = conn.cursor()
        allowed_fields = {
            "title", "artist", "album", "album_artist", "composer", "writer", "arranger",
            "mixer", "producer", "work", "genres", "stars", "is_single", "single_confidence",
            "year", "track_number", "disc_number", "comment", "mbid", "isrc", "bpm",
            "bitrate", "sample_rate", "is_cover", "alternate_take", "is_compilation",
            "is_live", "is_acoustic", "is_remix",
        }
        updates = {}
        for field in allowed_fields:
            if field in data:
                updates[field] = data[field]
        if not updates:
            return jsonify({"error": "No fields to update"}), 400
        set_clause = ", ".join(f"{k} = %s" for k in updates)
        params = list(updates.values()) + [track_id]
        cursor.execute(f"UPDATE tracks SET {set_clause} WHERE CAST(id AS TEXT) = %s", params)
        conn.commit()
        conn.close()
        return jsonify({"success": True, "updated": list(updates.keys())})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# GET /api/track/genre-recommendations
# ---------------------------------------------------------------------------

@track_bp.route("/genre-recommendations", methods=["GET"])
def track_genre_recommendations():
    """Get genre recommendations for a track from various sources."""
    track_id = request.args.get("track_id", "").strip()
    if not track_id:
        return jsonify({"error": "track_id required"}), 400
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT spotify_genres, lastfm_tags, musicbrainz_genres, discogs_genres FROM tracks WHERE CAST(id AS TEXT) = %s",
            (track_id,),
        )
        row = cursor.fetchone()
        conn.close()
        if not row:
            return jsonify({"error": "Track not found"}), 404
        genres = {}
        for key in ["spotify_genres", "lastfm_tags", "musicbrainz_genres", "discogs_genres"]:
            val = row.get(key) if hasattr(row, "get") else None
            if val:
                if isinstance(val, str):
                    try:
                        import json
                        parsed = json.loads(val) if val.startswith("[") else [val]
                    except json.JSONDecodeError:
                        parsed = [g.strip() for g in val.replace("\\", ",").split(",") if g.strip()]
                elif isinstance(val, list):
                    parsed = val
                else:
                    parsed = []
                genres[key] = parsed if isinstance(parsed, list) else [str(parsed)]
        return jsonify({"success": True, "genres": genres})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/track/<track_id>/rescan-single
# ---------------------------------------------------------------------------

@track_bp.route("/<track_id>/rescan-single", methods=["POST"])
def api_rescan_single_track(track_id):
    """Force a fresh single detection scan for one track."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE tracks SET single_detection_last_updated = NULL WHERE CAST(id AS TEXT) = %s", (track_id,))
        conn.commit()
        conn.close()
        return jsonify({"success": True, "message": "Single detection cleared for re-scan"})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/track/<track_id>/apply-mb-release
# ---------------------------------------------------------------------------

@track_bp.route("/<track_id>/apply-mb-release", methods=["POST"])
def api_track_apply_mb_release(track_id):
    """Apply a chosen MusicBrainz release MBID to a track."""
    try:
        data = request.json or {}
        release_mbid = str(data.get("release_mbid") or "").strip()
        if not release_mbid:
            return jsonify({"error": "release_mbid required"}), 400
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE tracks SET musicbrainz_album_mbid = %s WHERE CAST(id AS TEXT) = %s",
            (release_mbid, track_id),
        )
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# GET /api/track/<track_id>/mb-releases
# ---------------------------------------------------------------------------

@track_bp.route("/<track_id>/mb-releases", methods=["GET"])
def api_track_mb_releases(track_id):
    """Fetch all MusicBrainz releases containing this track's recording."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT artist, title, mbid FROM tracks WHERE CAST(id AS TEXT) = %s", (track_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return jsonify({"error": "Track not found"}), 404
        artist = row.get("artist") if hasattr(row, "get") else row[1]
        title = row.get("title") if hasattr(row, "get") else row[2]
        import requests
        from urllib.parse import quote
        headers = {"User-Agent": "Popularr/1.0", "Accept": "application/json"}
        resp = requests.get(
            f"https://musicbrainz.org/ws/2/recording/?query=artist:{quote(artist)}+AND+recording:{quote(title)}&fmt=json&limit=10",
            headers=headers, timeout=10,
        )
        data = resp.json()
        return jsonify({"success": True, "recordings": data.get("recordings", [])})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/track/match-missing
# ---------------------------------------------------------------------------

@track_bp.route("/match-missing", methods=["POST"])
def api_track_match_missing():
    """Match a MusicBrainz 'missing' track to an existing track."""
    try:
        data = request.json or {}
        track_id = str(data.get("track_id") or "").strip()
        mb_title = str(data.get("mb_title") or "").strip()
        if not track_id or not mb_title:
            return jsonify({"error": "track_id and mb_title required"}), 400
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE tracks SET title = %s WHERE CAST(id AS TEXT) = %s", (mb_title, track_id))
        conn.commit()
        conn.close()
        return jsonify({"success": True, "updated_title": mb_title})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/track/ignore-mb-field
# ---------------------------------------------------------------------------

@track_bp.route("/ignore-mb-field", methods=["POST"])
def api_track_ignore_mb_field():
    """Permanently ignore a specific MusicBrainz diff field for a track."""
    try:
        data = request.json or {}
        track_id = str(data.get("track_id") or "").strip()
        field = str(data.get("field") or "").strip()
        if not track_id or not field:
            return jsonify({"error": "track_id and field required"}), 400
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT mb_ignored_fields FROM tracks WHERE CAST(id AS TEXT) = %s", (track_id,))
        row = cursor.fetchone()
        import json as _json
        ignored = []
        if row:
            raw = row.get("mb_ignored_fields") if hasattr(row, "get") else row[0]
            if raw:
                try:
                    ignored = _json.loads(raw) if isinstance(raw, str) else list(raw) if isinstance(raw, (list, tuple)) else []
                except (TypeError, _json.JSONDecodeError):
                    ignored = []
        if field not in ignored:
            ignored.append(field)
        cursor.execute("UPDATE tracks SET mb_ignored_fields = %s WHERE CAST(id AS TEXT) = %s",
                       (_json.dumps(ignored), track_id))
        conn.commit()
        conn.close()
        return jsonify({"success": True, "ignored_fields": ignored})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
