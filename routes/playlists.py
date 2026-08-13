"""Playlist management routes.

Handles:
- Playlist file creation (.nsp).
- Navidrome playlist listing.
- Track search for playlist editing.
- Download session initiation for batch imports.
"""

from __future__ import annotations

import asyncio
import logging

from quart import Blueprint, render_template, redirect, url_for, session, request, jsonify

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


def _playlist_sessions_file() -> str:
    """State-dir path for the lightweight playlist download sessions file."""
    try:
        from helpers.config_helpers import get_state_directory
        return _os.path.join(get_state_directory(), "playlist_sessions.json")
    except Exception:
        return _os.environ.get("PLAYLIST_SESSIONS_FILE", "/data/playlist_sessions.json")


def _load_sessions() -> list[dict]:
    """Load all playlist download sessions from the JSON file."""
    path = _playlist_sessions_file()
    if not _os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as _fh:
            return _json.load(_fh)
    except Exception:
        return []


def _save_sessions(sessions: list[dict]) -> None:
    """Persist playlist download sessions to the JSON file."""
    path = _playlist_sessions_file()
    _os.makedirs(_os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as _fh:
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
    # The standalone manager page was folded into the /playlists hub.
    return redirect(url_for("playlists.playlists_index"))


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


# =============================================================================
# API ROUTES
# =============================================================================

# --------------------------------------------------
# IMPORT (JSON → DB MATCHING)
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
# CSV IMPORT (Exportify → parse → queue-ready)
# --------------------------------------------------

@playlists_bp.route("/api/playlist/import/csv", methods=["POST"])
async def api_playlist_import_csv():
    """Import a playlist from an Exportify-exported CSV file (multipart).

    Form fields:
        file                 – CSV file (required)
        playlist_name        – import name (required)
        playlist_description – optional description
        target_user          – optional Navidrome user
        skip_matching        – "true" returns the parsed tracks only; the
                               Downloads page and /playlist/import/csv page
                               use this, then batch-queue the tracks as one
                               album group (import_group = playlist name,
                               album_artist = caller-chosen, default
                               "Various Artists")

    Exportify columns parsed: Track Name, Artist Name(s), Album Name,
    Album Artist, ISRC, Spotify URI, Duration (ms), Release Date, Genres,
    Record Label, Popularity, Explicit.  Semicolon-joined multi-artist
    fields are reduced to the primary artist so queue matching stays clean.
    """
    try:
        import csv
        import io
        import re

        form = await request.form
        files = await request.files

        uploaded_file = files.get("file")
        if uploaded_file is None or not uploaded_file.filename:
            return jsonify({"error": "No file uploaded"}), 400
        if not uploaded_file.filename.lower().endswith(".csv"):
            return jsonify({"error": "Only .csv files are supported"}), 400

        playlist_name = str(form.get("playlist_name") or "").strip()
        playlist_description = str(form.get("playlist_description") or "").strip()
        target_user = str(form.get("target_user") or "").strip()
        if not playlist_name:
            return jsonify({"error": "playlist_name is required"}), 400

        # Strip the UTF-8 BOM if present, then normalise header names.
        content = uploaded_file.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))
        normalised = {(h or "").lower().strip(): h for h in (reader.fieldnames or [])}

        def col(aliases):
            """Return the actual header name for the first matching alias."""
            for alias in aliases:
                if alias in normalised:
                    return normalised[alias]
            return None

        title_col = col(["track name", "name", "title"])
        artist_col = col(["artist name(s)", "artist names", "artist name", "artist", "artists"])
        album_col = col(["album name", "album"])
        album_artist_col = col(["album artist", "album_artist", "albumartist"])
        isrc_col = col(["isrc"])
        uri_col = col(["spotify uri", "track uri", "uri"])
        duration_col = col(["duration (ms)", "duration_ms", "duration"])
        date_col = col(["release date", "release_date", "date"])
        genres_col = col(["genres", "genre"])
        label_col = col(["record label", "label"])
        popularity_col = col(["popularity"])
        explicit_col = col(["explicit"])

        if not title_col or not artist_col:
            return jsonify({
                "error": "CSV must contain at least 'Track Name' and 'Artist Name(s)' columns. "
                         "Please export using Exportify (exportify.net)."
            }), 400

        def _extract_year(date_str):
            """First 4-digit year found in a date string (or None)."""
            if not date_str:
                return None
            m = re.search(r"\b(\d{4})\b", str(date_str))
            return m.group(1) if m else None

        tracks_from_csv = []
        for row in reader:
            title = (row.get(title_col) or "").strip()
            artist = (row.get(artist_col) or "").strip()
            if not title and not artist:
                continue  # skip blank rows

            # Exportify joins multi-artist tracks with semicolons — keep the
            # primary artist so matching and Soulseek searches stay clean.
            if ";" in artist:
                artist = artist.split(";")[0].strip()
            raw_album_artist = (row.get(album_artist_col) or "").strip() if album_artist_col else ""
            album_artist = raw_album_artist or artist

            duration_s = None
            if duration_col:
                raw_dur = (row.get(duration_col) or "").strip()
                try:
                    duration_s = int(round(float(raw_dur) / 1000)) if raw_dur else None
                except (TypeError, ValueError):
                    duration_s = None

            tracks_from_csv.append({
                "title": title,
                "artist": artist,
                "album_artist": album_artist,
                "album": (row.get(album_col) or "").strip() if album_col else "",
                "isrc": (row.get(isrc_col) or "").strip() if isrc_col else "",
                "spotify_id": (row.get(uri_col) or "").strip().split(":")[-1] if uri_col else "",
                "duration_s": duration_s,
                "year": _extract_year(row.get(date_col)) if date_col else None,
                "genres": (row.get(genres_col) or "").strip() if genres_col else "",
                "record_label": (row.get(label_col) or "").strip() if label_col else "",
                "popularity": (row.get(popularity_col) or "").strip() if popularity_col else "",
                "explicit": (row.get(explicit_col) or "").strip().upper() if explicit_col else "",
            })

        if not tracks_from_csv:
            return jsonify({"error": "No tracks found in CSV"}), 400

        skip_matching = str(form.get("skip_matching") or "").lower() in ("true", "1", "yes")
        if skip_matching:
            logging.info(
                "CSV parse-only (skip_matching) for '%s': %d tracks parsed",
                playlist_name, len(tracks_from_csv),
            )
            return jsonify({
                "success": True,
                "all_tracks": tracks_from_csv,
                "total": len(tracks_from_csv),
            })

        # Library matching: ISRC first, then normalised artist+title.
        from db.repositories.tracks import find_library_track
        from sqlalchemy import text as _text
        from db.engine import db_session

        matched_tracks = []
        missing_tracks = []
        match_stats = {"isrc": 0, "fuzzy": 0, "strict": 0, "unmatched": 0}

        for track in tracks_from_csv:
            library_track = None
            strategy = "fuzzy"
            if track.get("isrc"):
                with db_session() as session:
                    row = session.execute(
                        _text(
                            "SELECT * FROM tracks "
                            "WHERE isrc = :isrc AND file_path IS NOT NULL AND file_path <> '' LIMIT 1"
                        ),
                        {"isrc": track["isrc"]},
                    ).fetchone()
                    if row:
                        library_track = dict(row._mapping)
                        strategy = "isrc"
            if library_track is None:
                library_track = find_library_track(
                    artist=track["artist"],
                    title=track["title"],
                    album=track.get("album") or None,
                    strict_album=False,
                )
                if library_track:
                    strategy = "strict"

            if library_track:
                matched_tracks.append({
                    "id": library_track.get("id"),
                    "title": library_track.get("title"),
                    "artist": library_track.get("artist"),
                    "album": library_track.get("album"),
                    "stars": library_track.get("stars"),
                    "file_path": library_track.get("file_path"),
                    "duration_s": library_track.get("duration"),
                    "confidence": 1.0,
                    "strategy": strategy,
                })
                match_stats[strategy] += 1
            else:
                missing_tracks.append({
                    "title": track["title"],
                    "artist": track["artist"],
                    "album_artist": track.get("album_artist", ""),
                    "album": track["album"],
                    "spotify_id": track["spotify_id"],
                    "isrc": track["isrc"],
                    "best_score": 0.0,
                    "duration_s": track.get("duration_s"),
                    "year": track.get("year"),
                    "genres": track.get("genres"),
                    "record_label": track.get("record_label"),
                    "popularity": track.get("popularity"),
                    "explicit": track.get("explicit"),
                })
                match_stats["unmatched"] += 1

        from helpers.config_helpers import get_config
        slskd_enabled = bool(get_config().get("slskd", {}).get("enabled", False))

        logging.info(
            "CSV playlist import '%s': matched %d/%d tracks (ISRC=%d, strict=%d)",
            playlist_name, len(matched_tracks), len(tracks_from_csv),
            match_stats["isrc"], match_stats["strict"],
        )
        return jsonify({
            "success": True,
            "playlist_name": playlist_name,
            "playlist_description": playlist_description,
            "target_user": target_user,
            "matched_tracks": matched_tracks,
            "missing_tracks": missing_tracks,
            "slskd_enabled": slskd_enabled,
            "message": f"Matched {len(matched_tracks)}/{len(tracks_from_csv)} tracks",
            "match_stats": match_stats,
        })
    except UnicodeDecodeError:
        return jsonify({"error": "Could not decode CSV file. Please save it as UTF-8."}), 400
    except Exception as exc:
        logging.error("CSV playlist import error: %s", exc, exc_info=True)
        return jsonify({"error": f"An error occurred while processing the CSV file: {exc}"}), 500


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

        # ``format`` selects the file type: "m3u" writes a regular .m3u into
        # the Playlists folder (needs file paths), "nsp" writes a smart-
        # playlist JSON (legacy default).
        file_format = str(data.get("format") or "nsp").lower()
        if file_format == "m3u":
            from services.playlists.playlist_service import create_m3u_file
            m3u_tracks = []
            for t in tracks:
                if not (t.get("file_path") or "").strip():
                    continue
                m3u_tracks.append({
                    "file_path": t["file_path"],
                    "title": t.get("title") or "",
                    "artist": t.get("artist") or "",
                    "duration": t.get("duration_s") or t.get("duration") or 0,
                })
            file_path = create_m3u_file(name, m3u_tracks)
            if not file_path:
                return jsonify({"error": "No tracks with file paths could be written"}), 400
            return jsonify({
                "success": True,
                "file_path": file_path,
                "track_count": len(m3u_tracks),
            }), 201

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
    try:
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
            is_m3u = entry.get("kind") == "m3u"
            merged.append({
                "id": f"file::{entry['file_name']}",
                "source": "file",
                "type": "regular" if is_m3u else "smart",
                "name": entry["name"],
                "file_name": entry["file_name"],
                "file_path": entry["file_path"],
                "comment": entry["comment"],
                "track_count": entry["track_count"],
                "rule_based": entry["rule_based"],
                "kind": entry.get("kind", "nsp"),
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
    except Exception as exc:
        # Never let an exception escape to an HTML error page — the frontend
        # expects JSON and would choke on a rendered template.
        logging.error("Failed to list playlists: %s", exc, exc_info=True)
        return jsonify({"error": f"Failed to list playlists: {exc}", "playlists": []}), 500


@playlists_bp.route("/api/playlists/generate/recommendations", methods=["POST"])
async def api_playlists_generate():
    """Generate a recommendations playlist from Last.fm / ListenBrainz.

    Candidates are grounded in the library's top artists, matched against the
    local tracks table, written to an ``{name}.m3u`` when matches exist, and
    missing tracks are queued to Soulseek via ``queue_add``.  The work runs in
    a worker thread (``asyncio.to_thread``) so the per-source API throttling
    (Last.fm ~3 req/s, ListenBrainz 1 req/s) never blocks the event loop.
    """
    try:
        data = (await request.get_json(silent=True)) or {}
        source = str(data.get("source") or "both").strip().lower()
        name = str(data.get("name") or "Recommended Mix").strip()
        limit = max(1, min(int(data.get("limit") or 12), 25))
        if source not in ("lastfm", "listenbrainz", "both"):
            return jsonify({"error": "source must be 'lastfm', 'listenbrainz' or 'both'"}), 400

        from services.playlists.generator_service import generate_recommendations
        result = await asyncio.to_thread(generate_recommendations, source, name, limit)
        return jsonify(result)
    except Exception as exc:
        logging.error("Playlist generation failed: %s", exc, exc_info=True)
        return jsonify({"error": f"Playlist generation failed: {exc}"}), 500


@playlists_bp.route("/api/playlists/tracks", methods=["POST"])
async def api_playlists_tracks():
    """Return the track list of one playlist.

    ``source`` selects the backend: ``file`` reads the .nsp JSON directly
    (falling back to a Navidrome name lookup for rule-based files), while
    ``navidrome`` loads the playlist from the Subsonic API.
    """
    try:
        data = (await request.get_json(silent=True)) or {}
        source = str(data.get("source") or "")
        playlist_id = str(data.get("id") or "")

        from routes.navidrome import get_navidrome_client
        from services.playlists.playlist_service import read_nsp_playlist

        if source == "file":
            file_path = str(data.get("file_path") or "")
            if not file_path or not _os.path.exists(file_path):
                return jsonify({"error": "Playlist file not found"}), 404

            # Generated .m3u playlists are plain text — parse directly.
            if str(file_path).lower().endswith((".m3u", ".m3u8")):
                from services.playlists.playlist_service import read_m3u_file
                tracks = read_m3u_file(file_path)
                return jsonify({
                    "playlist": {
                        "id": playlist_id,
                        "source": "file",
                        "type": "regular",
                        "name": _os.path.splitext(_os.path.basename(file_path))[0],
                        "file_name": _os.path.basename(file_path),
                        "file_path": file_path,
                        "comment": "Generated playlist",
                        "rule_based": False,
                    },
                    "tracks": tracks,
                })

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
    except Exception as exc:
        logging.error("Failed to load playlist tracks: %s", exc, exc_info=True)
        return jsonify({"error": f"Failed to load playlist tracks: {exc}", "tracks": []}), 500


@playlists_bp.route("/api/playlists/rename", methods=["POST"])
async def api_playlists_rename():
    """Rename a playlist.

    File-backed smart playlists also rename the .nsp file on disk
    (``file_name``); Navidrome playlists are renamed via the Subsonic API.
    """
    try:
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
            # Generated .m3u playlists are renamed as plain text files.
            if str(file_path).lower().endswith((".m3u", ".m3u8")):
                from services.playlists.playlist_service import sanitize_playlist_name
                new_file_name = str(data.get("file_name") or name or "").strip()
                base = sanitize_playlist_name(new_file_name or name)
                new_path = _os.path.join(_os.path.dirname(file_path), f"{base}.m3u")
                if new_path != file_path:
                    _os.rename(file_path, new_path)
                return jsonify({"success": True, "file_path": new_path})
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
    except Exception as exc:
        logging.error("Failed to rename playlist: %s", exc, exc_info=True)
        return jsonify({"error": f"Rename failed: {exc}"}), 500


@playlists_bp.route("/api/playlists/delete", methods=["POST"])
async def api_playlists_delete():
    """Delete a playlist.

    ``source == "file"`` removes the .nsp/.m3u file from the Playlists
    directory (path-validated so arbitrary files can never be deleted);
    ``source == "navidrome"`` deletes the playlist via the Subsonic API.
    """
    try:
        data = (await request.get_json(silent=True)) or {}
        source = str(data.get("source") or "")

        if source == "file":
            from services.playlists.playlist_service import _playlists_dir
            file_path = str(data.get("file_path") or "")
            if not file_path:
                return jsonify({"error": "Missing playlist file path"}), 400
            abs_path = _os.path.abspath(file_path)
            root = _os.path.abspath(_playlists_dir())
            if not abs_path.startswith(root + _os.sep) or not _os.path.isfile(abs_path):
                return jsonify({"error": "Invalid playlist file path"}), 400
            _os.remove(abs_path)
            return jsonify({"success": True, "file_path": abs_path})

        if source == "navidrome":
            from routes.navidrome import get_navidrome_client
            client = get_navidrome_client()
            if not client:
                return jsonify({"error": "Navidrome not configured"}), 400
            playlist_id = str(data.get("id") or "")
            if not playlist_id:
                return jsonify({"error": "Missing playlist id"}), 400
            if client.delete_playlist(playlist_id):
                return jsonify({"success": True})
            return jsonify({"error": "Navidrome rejected the delete"}), 500

        return jsonify({"error": "Unknown playlist source"}), 400
    except Exception as exc:
        logging.error("Failed to delete playlist: %s", exc, exc_info=True)
        return jsonify({"error": f"Delete failed: {exc}"}), 500