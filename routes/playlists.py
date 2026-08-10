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


@playlists_bp.route("/playlists")
async def playlists_index():
    """Rebuilt Playlists page: all smart + regular playlists in one view."""
    return await render_template("playlists/index.html")


@playlists_bp.route("/playlists/browse")
async def playlists_browse():
    return redirect(url_for("playlists.playlists_index"))


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
async def api_import_playlist_url():
    try:
        data = (await request.get_json(silent=True)) or {}
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
async def api_playlist_import():
    try:
        data = (await request.get_json(silent=True)) or {}
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
async def api_playlist_create():
    try:
        data = (await request.get_json(silent=True)) or {}

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
async def api_playlist_load():
    try:
        data = (await request.get_json(silent=True)) or {}
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
async def api_playlist_search():
    try:
        data = (await request.get_json(silent=True)) or {}
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
async def api_playlist_session():
    try:
        data = (await request.get_json(silent=True)) or {}
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
async def api_recommended_playlists_create():
    """Create a Navidrome playlist from a recommendation category/type."""
    data = (await request.get_json(silent=True)) or {}
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


# =============================================================================
# PLAYLIST INDEX (rebuilt /playlists page)
# =============================================================================

@playlists_bp.route("/api/playlists/all")
def api_playlists_all():
    """Return every playlist in the system.

    Smart playlists come from .nsp files in the Playlists directory (with
    their file name/path); anything else is a Navidrome smart/regular
    playlist fetched from the Subsonic API.  .nsp files win over same-named
    Navidrome entries so the file-backed version (which we can rename) is
    shown.
    """
    from routes.navidrome import get_navidrome_client
    from services.playlists.playlist_service import list_nsp_playlists

    file_playlists = list_nsp_playlists()
    file_names = {entry["name"].strip().lower(): entry for entry in file_playlists}

    client = get_navidrome_client()
    nav_playlists = []
    if client:
        try:
            nav_playlists = client.fetch_all_playlists() or []
        except Exception as exc:
            logging.error("Failed to fetch Navidrome playlists: %s", exc, exc_info=True)

    merged = []
    for entry in file_playlists:
        merged.append({
            "id": f"file::{entry['file_name']}",
            "source": "file",
            "type": "smart",
            "name": entry["name"],
            "file_name": entry["file_name"],
            "file_path": entry["file_path"],
            "comment": entry["comment"],
            "track_count": entry["track_count"],
            "rule_based": entry["rule_based"],
        })

    for nav in nav_playlists:
        name = str(nav.get("name") or "").strip()
        if not name or name.strip().lower() in file_names:
            continue  # already shown as a file-backed smart playlist
        nav_type = nav.get("type")
        merged.append({
            "id": str(nav.get("id") or ""),
            "source": "navidrome",
            "type": nav_type if nav_type in ("smart", "regular") else "regular",
            "name": name,
            "file_name": None,
            "file_path": None,
            "comment": str(nav.get("comment") or ""),
            "track_count": int(nav.get("songCount") or 0) or len(nav.get("entry") or []),
            "rule_based": bool(nav.get("criteria")),
        })

    merged.sort(key=lambda item: (item["type"] != "smart", item["name"].lower()))
    return jsonify({"playlists": merged, "navidrome_configured": bool(client)})


@playlists_bp.route("/api/playlists/tracks", methods=["POST"])
async def api_playlists_tracks():
    """Return the track list of one playlist.

    ``source`` selects the backend: ``file`` reads the .nsp JSON directly
    (falling back to a Navidrome name lookup for rule-based files), while
    ``navidrome`` loads the playlist from the Subsonic API.
    """
    data = (await request.get_json(silent=True)) or {}
    source = str(data.get("source") or "")
    playlist_id = str(data.get("id") or "")

    from routes.navidrome import get_navidrome_client
    from services.playlists.playlist_service import read_nsp_playlist

    if source == "file":
        file_path = str(data.get("file_path") or "")
        if not file_path or not _os.path.exists(file_path):
            return jsonify({"error": "Playlist file not found"}), 404

        playlist = read_nsp_playlist(file_path)
        if not playlist:
            return jsonify({"error": "Could not read playlist file"}), 500

        tracks = playlist.get("_tracks") or []
        if not tracks:
            # Rule-based or trackIds-only: let Navidrome resolve the entries.
            client = get_navidrome_client()
            if client:
                try:
                    found = client.find_playlist_by_name(playlist.get("name") or "")
                    if found:
                        detail = client.fetch_playlist(found.get("id"))
                        tracks = detail.get("tracks") or []
                except Exception as exc:
                    logging.error("Failed to resolve rule-based playlist via Navidrome: %s", exc, exc_info=True)

        return jsonify({
            "playlist": {
                "id": playlist_id,
                "source": "file",
                "type": "smart",
                "name": playlist.get("name"),
                "file_name": playlist.get("_file_name"),
                "file_path": playlist.get("_file_path"),
                "comment": playlist.get("comment") or "",
                "rule_based": bool(playlist.get("rules")),
            },
            "tracks": tracks,
        })

    if source == "navidrome":
        client = get_navidrome_client()
        if not client:
            return jsonify({"error": "Navidrome not configured"}), 400
        playlist = client.fetch_playlist(playlist_id)
        if not playlist:
            return jsonify({"error": "Playlist not found"}), 404
        tracks = []
        for entry in playlist.get("tracks") or []:
            if not isinstance(entry, dict):
                continue
            tracks.append({
                "id": str(entry.get("id") or ""),
                "title": str(entry.get("title") or entry.get("name") or ""),
                "artist": str(entry.get("artist") or ""),
                "album": str(entry.get("album") or ""),
                "duration": entry.get("duration"),
                "rating": entry.get("userRating") or entry.get("rating"),
            })
        return jsonify({
            "playlist": {
                "id": playlist_id,
                "source": "navidrome",
                "type": playlist.get("type") if playlist.get("type") in ("smart", "regular") else "regular",
                "name": playlist.get("name"),
                "file_name": None,
                "file_path": None,
                "comment": playlist.get("comment") or "",
                "rule_based": False,
            },
            "tracks": tracks,
        })

    return jsonify({"error": "Unknown playlist source"}), 400


@playlists_bp.route("/api/playlists/rename", methods=["POST"])
async def api_playlists_rename():
    """Rename a playlist.

    File-backed smart playlists also rename the .nsp file on disk
    (``file_name``); Navidrome playlists are renamed via the Subsonic API.
    """
    data = (await request.get_json(silent=True)) or {}
    source = str(data.get("source") or "")
    name = str(data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Playlist name is required"}), 400

    if source == "file":
        from services.playlists.playlist_service import rename_nsp_playlist
        file_path = str(data.get("file_path") or "")
        if not file_path or not _os.path.exists(file_path):
            return jsonify({"error": "Playlist file not found"}), 404
        try:
            result = rename_nsp_playlist(file_path, name, data.get("file_name"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            logging.error("Failed to rename playlist file: %s", exc, exc_info=True)
            return jsonify({"error": f"Rename failed: {exc}"}), 500
        return jsonify({"success": True, **result})

    if source == "navidrome":
        from routes.navidrome import get_navidrome_client
        client = get_navidrome_client()
        if not client:
            return jsonify({"error": "Navidrome not configured"}), 400
        playlist_id = str(data.get("id") or "")
        if not playlist_id:
            return jsonify({"error": "Missing playlist id"}), 400
        if client.rename_playlist(playlist_id, name):
            return jsonify({"success": True})
        return jsonify({"error": "Navidrome rejected the rename"}), 500

    return jsonify({"error": "Unknown playlist source"}), 400