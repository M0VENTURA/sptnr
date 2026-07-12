"""Playlist management routes.

Handles:
- Playlist file creation (.nsp).
- Spotify playlist import.
- Navidrome playlist listing.
- Track search for playlist editing.
- Download session initiation for batch imports.
"""

from __future__ import annotations

from flask import Blueprint, render_template, redirect, url_for, session, request, jsonify
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
# UI ROUTES
# =============================================================================

@playlists_bp.route("/playlist-manager")
def playlist_manager():
    cfg = get_config()
    return render_template(
        "playlists/manager.html",
        navidrome_users=cfg.get("navidrome_users", [])
    )


@playlists_bp.route("/playlist/import")
def playlist_importer():
    cfg = get_config()
    return render_template(
        "playlists/importer.html",
        navidrome_users=cfg.get("navidrome_users", [])
    )


@playlists_bp.route("/playlist/import/csv")
def playlist_importer_csv():
    return render_template("playlists/importer_csv.html")


@playlists_bp.route("/playlists/browse")
def playlists_browse():
    cfg = get_config()
    nav_users = cfg.get("navidrome_users", [])
    return render_template("playlists/browse.html", navidrome_users=nav_users)


@playlists_bp.route("/playlists/create/<playlist_type>")
def playlists_create(playlist_type):
    cfg = get_config()
    return render_template(
        "playlists/create.html",
        playlist_type=playlist_type,
        navidrome_users=cfg.get("navidrome_users", [])
    )


@playlists_bp.route("/playlists/import")
def playlists_import():
    cfg = get_config()
    return render_template(
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