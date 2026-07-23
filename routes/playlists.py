"""Playlist management routes.

Handles:
- Playlist file creation (.nsp).
- Spotify playlist import.
- Navidrome playlist listing.
- Track search for playlist editing.
- Download session initiation for batch imports.
"""

from __future__ import annotations

from quart import Blueprint, render_template, redirect, url_for, session, request, jsonify
import logging

from helpers.config_helpers import get_config

# ✅ Service imports
from services.playlists import (
    create_playlist_file,
    list_playlists,
    load_playlist,
    search_songs_in_db,
    start_download_session,
    match_playlist_tracks,
)


        
from services.playlists.playlist_matching_service import (
    match_playlist_tracks
)    

from services.playlists.playlist_external_import_service import (
    import_playlist_from_url
)

# -----------------------------------------------------------------------------
# Blueprint
# -----------------------------------------------------------------------------

playlists_bp = Blueprint("playlists", __name__)



# =============================================================================
# PLAYLIST DOWNLOAD SESSIONS (lightweight JSON-backed)
# =============================================================================

import json as _json
import os as _os
import uuid as _uuid
from datetime import datetime as _datetime

_PLAYLIST_SESSIONS_FILE = _os.environ.get(
    "PLAYLIST_SESSIONS_FILE",
    "/data/playlist_sessions.json",
)


def _load_sessions() -> list[dict]:
    """Load all playlist download sessions from the JSON file."""
    if not _os.path.exists(_PLAYLIST_SESSIONS_FILE):
        return []
    try:
        with open(_PLAYLIST_SESSIONS_FILE, "r", encoding="utf-8") as _fh:
            return _json.load(_fh)
    except Exception:
        return []


def _save_sessions(sessions: list[dict]) -> None:
    """Persist playlist download sessions to the JSON file."""
    _os.makedirs(_os.path.dirname(_PLAYLIST_SESSIONS_FILE), exist_ok=True)
    with open(_PLAYLIST_SESSIONS_FILE, "w", encoding="utf-8") as _fh:
        _json.dump(sessions, _fh, indent=2, default=str)


# ---------------------------------------------------------------------------
# GET /api/playlist-downloads
# ---------------------------------------------------------------------------

@playlists_bp.route("/api/playlist-downloads", methods=["GET"])
def api_playlist_downloads_list():
    """Return all active playlist download sessions."""
    sessions = _load_sessions()
    return jsonify({"sessions": sessions})


# ---------------------------------------------------------------------------
# POST /api/playlist-downloads/create
# ---------------------------------------------------------------------------

@playlists_bp.route("/api/playlist-downloads/create", methods=["POST"])
async def api_playlist_downloads_create():
    """Create a new playlist download session."""
    data = (await request.get_json(silent=True)) or {}
    session_name = str(data.get("session_name", "Unnamed Session")).strip()
    total_tracks = data.get("total_tracks")
    try:
        total_tracks = int(total_tracks) if total_tracks is not None else 0
    except (TypeError, ValueError):
        total_tracks = 0

    new_session = {
        "id": str(_uuid.uuid4()),
        "session_name": session_name or "Unnamed Session",
        "status": "active",
        "completed_tracks": 0,
        "total_tracks": total_tracks,
        "priority_queue": bool(data.get("priority_queue", False)),
        "created_at": _datetime.now().isoformat(),
    }

    sessions = _load_sessions()
    sessions.append(new_session)
    _save_sessions(sessions)

    return jsonify({"success": True, "session_id": new_session["id"]})

# =============================================================================
# UI ROUTES
# =============================================================================

@playlists_bp.route("/playlist-manager")
async def playlist_manager():
    cfg = get_config()
    return await render_template(
        "playlists/manager.html",
        navidrome_users=cfg.get("navidrome_users", [])
    )


@playlists_bp.route("/playlist/import")
async def playlist_importer():
    cfg = get_config()
    return await render_template(
        "playlists/importer.html",
        navidrome_users=cfg.get("navidrome_users", [])
    )


@playlists_bp.route("/playlist/import/csv")
async def playlist_importer_csv():
    return await render_template("playlists/importer_csv.html")


@playlists_bp.route("/playlists/browse")
async def playlists_browse():
    cfg = get_config()
    nav_users = cfg.get("navidrome_users", [])
    return await render_template("playlists/browse.html", navidrome_users=nav_users)


@playlists_bp.route("/playlists/create/<playlist_type>")
async def playlists_create(playlist_type):
    cfg = get_config()
    return await render_template(
        "playlists/create.html",
        playlist_type=playlist_type,
        navidrome_users=cfg.get("navidrome_users", [])
    )


@playlists_bp.route("/playlists/import")
async def playlists_import():
    cfg = get_config()
    return await render_template(
        "playlists/import.html",
        navidrome_users=cfg.get("navidrome_users", [])
    )


# =============================================================================
# API ROUTES
# =============================================================================

# --------------------------------------------------
# IMPORT FROM URL (Spotify / Apple Music)
# --------------------------------------------------

@playlists_bp.route("/api/import_playlist_url", methods=["POST"])
def api_import_playlist_url():
    try:
        data = request.get_json() or {}
        url = (data.get("url") or "").strip()

        if not url:
            return jsonify({"success": False, "error": "url is required"}), 400

        result = import_playlist_from_url(url)

        return jsonify({
            "success": True,
            **result
        })

    except Exception as e:
        logging.error(f"[import_playlist_url] Error: {e}", exc_info=True)

        return jsonify({
            "success": False,
            "error": str(e)
        }), 400


# --------------------------------------------------
# IMPORT (Spotify JSON → DB MATCHING)
# --------------------------------------------------

@playlists_bp.route("/api/playlist/import", methods=["POST"])
def api_playlist_import():
    try:
        data = request.get_json() or {}
        spotify_tracks = data.get("tracks", [])

        if not spotify_tracks:
            return jsonify({"error": "No tracks provided"}), 400

        from db.repositories.tracks import find_library_track

        def _match_track(track, cursor, **kwargs):
            """Match a single playlist track against the library."""
            library_track = find_library_track(
                artist=track.get("artist", ""),
                title=track.get("title", ""),
                album=track.get("album", ""),
                strict_album=False,
            )
            if library_track:
                return library_track, 1.0, "db_match"
            return None, 0.0, "unmatched"

        matched, missing, stats = match_playlist_tracks(
            spotify_tracks,
            _match_track
        )

        return jsonify({
            "success": True,
            "matched_tracks": matched,
            "missing_tracks": missing,
            "match_stats": stats
        })

    except Exception as e:
        logging.error(f"Playlist import error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# --------------------------------------------------
# CREATE (NSP FILE)
# --------------------------------------------------

@playlists_bp.route("/api/playlist/create", methods=["POST"])
def api_playlist_create():
    try:
        data = request.get_json() or {}

        name = data.get("playlist_name")
        description = data.get("playlist_description", "")
        tracks = data.get("matched_tracks", [])

        if not name or not tracks:
            return jsonify({"error": "Missing data"}), 400

        track_ids = [t["id"] for t in tracks if t.get("id")]

        file_path = create_playlist_file(name, description, track_ids)

        return jsonify({
            "success": True,
            "file_path": file_path,
            "track_count": len(track_ids)
        }), 201

    except Exception as e:
        logging.error(f"Playlist create error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# --------------------------------------------------
# NAVIDROME LIST
# --------------------------------------------------

@playlists_bp.route("/api/playlist/list")
def api_playlist_list():
    try:
        cfg = get_config()
        nav = cfg.get("navidrome", {})

        playlists = list_playlists(
            nav.get("base_url"),
            nav.get("user"),
            nav.get("pass")
        )

        return jsonify({"playlists": playlists})

    except Exception as e:
        logging.error(f"List playlists error: {e}")
        return jsonify({"error": str(e)}), 500


# --------------------------------------------------
# NAVIDROME LOAD
# --------------------------------------------------

@playlists_bp.route("/api/playlist/load", methods=["POST"])
def api_playlist_load():
    try:
        data = request.get_json() or {}
        playlist_id = data.get("playlist_id")

        if not playlist_id:
            return jsonify({"error": "Missing playlist_id"}), 400

        cfg = get_config()
        nav = cfg.get("navidrome", {})

        playlist = load_playlist(
            nav.get("base_url"),
            nav.get("user"),
            nav.get("pass"),
            playlist_id
        )

        return jsonify({"success": True, "playlist": playlist})

    except Exception as e:
        logging.error(f"Load playlist error: {e}")
        return jsonify({"error": str(e)}), 500


# --------------------------------------------------
# SEARCH SONGS
# --------------------------------------------------

@playlists_bp.route("/api/playlist/search-songs", methods=["POST"])
def api_playlist_search():
    try:
        data = request.get_json() or {}
        query = data.get("query", "").strip()

        if not query:
            return jsonify({"error": "Query required"}), 400

        results = search_songs_in_db(query)

        return jsonify({"songs": results})

    except Exception as e:
        logging.error(f"Search error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# --------------------------------------------------
# DOWNLOAD SESSION
# --------------------------------------------------

@playlists_bp.route("/api/playlist/session", methods=["POST"])
def api_playlist_session():
    try:
        data = request.get_json() or {}
        tracks = data.get("tracks", [])
        playlist_name = data.get("playlist_name", "Imported Playlist")

        if not tracks:
            return jsonify({"error": "No tracks"}), 400

        session = start_download_session(
            name=playlist_name,
            user="default",
            total=len(tracks),
            priority=False,
        )

        return jsonify({
            "success": True,
            "session": session,
        })

    except Exception as e:
        logging.error(f"Session error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# --------------------------------------------------
# RECOMMENDED PLAYLISTS
# --------------------------------------------------

@playlists_bp.route("/api/recommended-playlists", methods=["GET"])
def api_recommended_playlists():
    """Get recommended playlists from Last.fm / ListenBrainz.

    Returns empty recommendations gracefully when APIs are unavailable.
    """
    try:
        cfg = get_config()
        lastfm_cfg = cfg.get("api_integrations", {}).get("lastfm", {})
        api_key = lastfm_cfg.get("api_key", "")

        from services.playlists.recommendation_service import PlaylistRecommender
        from db.engine import db_session

        lf_client = None
        if api_key:
            from api_clients.lastfm import LastFmClient
            lf_client = LastFmClient(api_key)

        recommender = PlaylistRecommender(lastfm_client=lf_client, db_connection=db_session)
        recommendations = recommender.get_recommendations()

        return jsonify({"success": True, "recommendations": recommendations})
    except Exception as exc:
        logging.error("Failed to fetch recommended playlists: %s", exc, exc_info=True)
        return jsonify({"success": True, "recommendations": {}})


@playlists_bp.route("/api/recommended-playlists/create", methods=["POST"])
def api_recommended_playlists_create():
    """Create a Navidrome playlist from a recommendation category/type."""
    data = request.get_json(silent=True) or {}
    category = data.get("category", "")
    playlist_type = data.get("type", "")

    if not category or not playlist_type:
        return jsonify({"success": False, "error": "category and type required"}), 400

    try:
        cfg = get_config()
        lastfm_cfg = cfg.get("api_integrations", {}).get("lastfm", {})
        api_key = lastfm_cfg.get("api_key", "")

        from services.playlists.recommendation_service import PlaylistRecommender
        from db.engine import db_session

        lf_client = None
        if api_key:
            from api_clients.lastfm import LastFmClient
            lf_client = LastFmClient(api_key)

        recommender = PlaylistRecommender(lastfm_client=lf_client, db_connection=db_session)

        # Re-fetch recommendations and find matching playlist
        recs = recommender.get_recommendations()
        category_recs = recs.get(category, []) if isinstance(recs, dict) else []
        target = None
        for p in category_recs:
            if isinstance(p, dict) and p.get("type") == playlist_type:
                target = p
                break

        if not target:
            return jsonify({"success": False, "error": f"No playlist found for {category}/{playlist_type}"}), 404

        track_ids = target.get("track_ids", [])
        playlist_name = target.get("name", f"{category} - {playlist_type}")

        from services.playlists import create_playlist_file
        file_path = create_playlist_file(playlist_name, target.get("description", ""), track_ids)

        return jsonify({
            "success": True,
            "playlist": {
                "name": playlist_name,
                "file_path": file_path,
                "track_count": len(track_ids),
            }
        })
    except Exception as exc:
        logging.error("Failed to create recommended playlist: %s", exc, exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500