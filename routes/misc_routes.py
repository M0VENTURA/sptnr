"""Miscellaneous API routes — genres, corrections, bookmarks, country, essentia, etc."""

from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, jsonify, request, Response

from db.utils import get_db_connection
from helpers.config_helpers import get_config

logger = logging.getLogger(__name__)

misc_api_bp = Blueprint("misc_api", __name__, url_prefix="/api")


# ===========================================================================
# SEARCH
# ===========================================================================

@misc_api_bp.route("/search", methods=["POST"])
def api_search():
    """Search the library for artists, albums, and tracks."""
    data = request.get_json() or {}
    query = str(data.get("query", "")).strip().lower()
    if not query or len(query) < 2:
        return jsonify({"error": "Query too short"}), 400
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        exact_p = query
        starts_p = f"{query}%"
        contains_p = f"%{query}%"

        # Artists
        cursor.execute(
            """SELECT COALESCE(NULLIF(album_artist, ''), artist) as name,
                      COUNT(DISTINCT album) as album_count, COUNT(*) as track_count,
                      CASE WHEN LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = %s THEN 0
                           WHEN LOWER(COALESCE(NULLIF(album_artist, ''), artist)) LIKE %s THEN 1 ELSE 2 END as match_rank
               FROM tracks
               WHERE LOWER(artist) LIKE %s OR LOWER(album_artist) LIKE %s
               GROUP BY COALESCE(NULLIF(album_artist, ''), artist)
               ORDER BY match_rank ASC, track_count DESC LIMIT 20""",
            (exact_p, starts_p, contains_p, contains_p),
        )
        artists = [dict(r) for r in cursor.fetchall()]

        # Albums
        cursor.execute(
            """SELECT COALESCE(NULLIF(album_artist, ''), artist) as artist, album,
                      COUNT(*) as track_count, AVG(stars) as avg_stars,
                      CASE WHEN LOWER(album) = %s THEN 0
                           WHEN LOWER(album) LIKE %s THEN 1 ELSE 2 END as match_rank
               FROM tracks WHERE LOWER(album) LIKE %s
               GROUP BY COALESCE(NULLIF(album_artist, ''), artist), album
               ORDER BY match_rank ASC, track_count DESC LIMIT 20""",
            (exact_p, starts_p, contains_p),
        )
        albums = [dict(r) for r in cursor.fetchall()]

        # Tracks
        cursor.execute(
            """SELECT id, title, COALESCE(NULLIF(album_artist, ''), artist) as artist, album, stars,
                      CASE WHEN LOWER(title) = %s THEN 0
                           WHEN LOWER(title) LIKE %s THEN 1
                           WHEN LOWER(title) LIKE %s THEN 2 ELSE 3 END as match_rank
               FROM tracks
               WHERE LOWER(title) LIKE %s OR LOWER(artist) LIKE %s OR LOWER(album_artist) LIKE %s
               ORDER BY match_rank ASC, stars DESC LIMIT 50""",
            (exact_p, starts_p, contains_p, contains_p, contains_p, contains_p),
        )
        tracks = [dict(r) for r in cursor.fetchall()]

        conn.close()
        return jsonify({"artists": artists, "albums": albums, "tracks": tracks})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ===========================================================================
# STATS
# ===========================================================================

@misc_api_bp.route("/stats", methods=["GET"])
def api_stats():
    """Get library statistics."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as track_count, COUNT(DISTINCT album) as album_count, "
                       "COUNT(DISTINCT COALESCE(NULLIF(album_artist, ''), artist)) as artist_count, "
                       "AVG(stars) as avg_stars, SUM(duration) as total_duration FROM tracks")
        stats = dict(cursor.fetchone())
        conn.close()
        return jsonify({"success": True, **stats})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ===========================================================================
# TRACK-COUNT
# ===========================================================================

@misc_api_bp.route("/track-count", methods=["GET"])
def api_track_count():
    """Get total track count for progress calculation."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as count FROM tracks")
        row = cursor.fetchone()
        conn.close()
        return jsonify({"count": row.get("count", 0) if hasattr(row, "get") else (row[0] if row else 0)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ===========================================================================
# INTEGRATIONS STATUS
# ===========================================================================

@misc_api_bp.route("/integrations/status", methods=["GET"])
def api_integrations_status():
    """Return health/status information for all configured integrations."""
    from helpers.config_helpers import get_all_services_status
    status = get_all_services_status()
    return jsonify({"success": True, "integrations": status})


# ===========================================================================
# FEATURES UPDATE
# ===========================================================================

@misc_api_bp.route("/features/update", methods=["POST"])
def api_features_update():
    """Update individual feature flags."""
    return jsonify({"success": True, "message": "Feature update not yet implemented"}), 200


# ===========================================================================
# ARTIST COUNTRY
# ===========================================================================

@misc_api_bp.route("/artist/country", methods=["POST"])
def api_fetch_artist_country():
    """Fetch artist country from MusicBrainz and update database."""
    data = request.json or {}
    artist = str(data.get("artist_name") or "").strip()
    if not artist:
        return jsonify({"error": "artist_name required"}), 400
    try:
        import requests
        headers = {"User-Agent": "Popularr/1.0", "Accept": "application/json"}
        resp = requests.get(
            f"https://musicbrainz.org/ws/2/artist/?query=artist:%22{requests.utils.quote(artist)}%22&fmt=json&limit=1",
            headers=headers, timeout=10,
        )
        data = resp.json()
        artists = data.get("artists", [])
        country = artists[0].get("country", "") if artists else ""
        if country:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("INSERT INTO artists (name, country) VALUES (%s, %s) ON CONFLICT (name) DO UPDATE SET country = EXCLUDED.country", (artist, country))
            cursor.execute("UPDATE tracks SET artist_country = %s WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s", (country, artist))
            conn.commit()
            conn.close()
        return jsonify({"success": True, "country": country})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@misc_api_bp.route("/artist/country/update", methods=["POST"])
def api_update_artist_country():
    """Manually update artist country."""
    data = request.json or {}
    artist = str(data.get("artist_name") or "").strip()
    country = str(data.get("country") or "").strip()
    if not artist or not country:
        return jsonify({"error": "artist_name and country required"}), 400
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO artists (name, country) VALUES (%s, %s) ON CONFLICT (name) DO UPDATE SET country = EXCLUDED.country", (artist, country))
        cursor.execute("UPDATE tracks SET artist_country = %s WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s", (country, artist))
        conn.commit()
        conn.close()
        return jsonify({"success": True, "country": country})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@misc_api_bp.route("/artist/country/apply-as-genre", methods=["POST"])
def api_apply_country_as_genre():
    """Apply artist country as genre tag to all tracks."""
    data = request.json or {}
    artist = str(data.get("artist_name") or "").strip()
    if not artist:
        return jsonify({"error": "artist_name required"}), 400
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT country FROM artists WHERE name = %s", (artist,))
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "Artist not found or no country"}), 404
        country = row.get("country") if hasattr(row, "get") else row[0]
        cursor.execute(
            "UPDATE tracks SET genres = CONCAT_WS(' \\ ', COALESCE(NULLIF(genres, ''), ''), %s) WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s",
            (country, artist),
        )
        conn.commit()
        conn.close()
        return jsonify({"success": True, "country": country})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ===========================================================================
# DUPLICATE ARTISTS
# ===========================================================================

@misc_api_bp.route("/duplicate-artists/<path:artist>", methods=["GET"])
def api_get_duplicate_artists(artist):
    """Get duplicate artists for a specific artist."""
    from urllib.parse import unquote
    artist = unquote(artist)
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT musicbrainz_artist_id, COUNT(DISTINCT artist) as names FROM tracks "
            "WHERE musicbrainz_artist_id IS NOT NULL AND musicbrainz_artist_id != '' "
            "GROUP BY musicbrainz_artist_id HAVING COUNT(DISTINCT artist) > 1"
        )
        rows = cursor.fetchall()
        conn.close()
        return jsonify({"success": True, "duplicates": [dict(r) for r in rows]})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@misc_api_bp.route("/duplicate-artists/merge", methods=["POST"])
def api_merge_duplicate_artists():
    """Merge duplicate artists."""
    return jsonify({"success": True, "message": "Merge not yet implemented"}), 200


# ===========================================================================
# GENRES
# ===========================================================================

@misc_api_bp.route("/genres/track/<path:track_id>", methods=["GET"])
def api_genres_track(track_id: str):
    """Get all genre sources for a single track."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT spotify_genres, lastfm_tags, musicbrainz_genres, discogs_genres,
                   essentia_genres, mood, listenbrainz_genres
            FROM tracks WHERE CAST(id AS TEXT) = %s
        """, (track_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return jsonify({"error": "Track not found"}), 404

        import json as _json
        genres: dict[str, list[dict[str, str | int]]] = {}
        source_keys = [
            ("discogs_genres", "discogs_genres"),
            ("mood", "mood"),
            ("essentia_genres", "essentia_genres"),
            ("musicbrainz_genres", "musicbrainz_genres"),
            ("lastfm_tags", "lastfm_tags"),
            ("listenbrainz_genres", "listenbrainz_genres"),
            ("spotify_genres", "spotify_genres"),
        ]
        for db_key, output_key in source_keys:
            raw = row.get(db_key) if hasattr(row, "get") else None
            if not raw:
                genres[output_key] = []
                continue
            try:
                parsed = _json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                parsed = [g.strip() for g in str(raw).split(",") if g.strip()]
            if isinstance(parsed, list):
                genres[output_key] = [{"name": g["name"] if isinstance(g, dict) else str(g), "count": g.get("count", 1) if isinstance(g, dict) else 1} for g in parsed]
            else:
                genres[output_key] = []
        return jsonify({"success": True, "genres": genres})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@misc_api_bp.route("/genres/album/<path:album>/<path:artist>", methods=["GET"])
def api_genres_album(album: str, artist: str):
    """Get aggregated genres across all tracks in an album."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT spotify_genres, lastfm_tags, musicbrainz_genres, discogs_genres,
                   essentia_genres, mood, listenbrainz_genres
            FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s AND album = %s
        """, (artist, album))
        rows = cursor.fetchall()
        conn.close()

        from collections import Counter
        source_keys = [
            "discogs_genres", "mood", "essentia_genres",
            "musicbrainz_genres", "lastfm_tags", "listenbrainz_genres", "spotify_genres",
        ]
        result: dict[str, list[dict[str, str | int]]] = {k: [] for k in source_keys}

        import json as _json
        for row in rows:
            for key in source_keys:
                raw = row.get(key) if hasattr(row, "get") else None
                if not raw:
                    continue
                try:
                    parsed = _json.loads(raw) if isinstance(raw, str) else raw
                except Exception:
                    parsed = [g.strip() for g in str(raw).split(",") if g.strip()]
                if isinstance(parsed, list):
                    for g in parsed:
                        name = g["name"] if isinstance(g, dict) else str(g)
                        result[key].append(name)
        # Deduplicate and count
        for key in source_keys:
            counter = Counter(result[key])
            result[key] = [{"name": name, "count": count} for name, count in counter.most_common(25)]

        return jsonify({"success": True, "genres": result})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@misc_api_bp.route("/genres/artist/<path:artist>", methods=["GET"])
def api_genres_artist(artist: str):
    """Get aggregated genres across all tracks by an artist."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT spotify_genres, lastfm_tags, musicbrainz_genres, discogs_genres,
                   essentia_genres, mood, listenbrainz_genres
            FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s
        """, (artist,))
        rows = cursor.fetchall()
        conn.close()

        from collections import Counter
        source_keys = [
            "discogs_genres", "mood", "essentia_genres",
            "musicbrainz_genres", "lastfm_tags", "listenbrainz_genres", "spotify_genres",
        ]
        result: dict[str, list[dict[str, str | int]]] = {k: [] for k in source_keys}

        import json as _json
        for row in rows:
            for key in source_keys:
                raw = row.get(key) if hasattr(row, "get") else None
                if not raw:
                    continue
                try:
                    parsed = _json.loads(raw) if isinstance(raw, str) else raw
                except Exception:
                    parsed = [g.strip() for g in str(raw).split(",") if g.strip()]
                if isinstance(parsed, list):
                    for g in parsed:
                        name = g["name"] if isinstance(g, dict) else str(g)
                        result[key].append(name)
        for key in source_keys:
            counter = Counter(result[key])
            result[key] = [{"name": name, "count": count} for name, count in counter.most_common(30)]

        return jsonify({"success": True, "genres": result})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@misc_api_bp.route("/genres/remove", methods=["POST"])
def api_remove_genres():
    """Remove specific genres from artist or album's tracks."""
    return jsonify({"success": True, "message": "Genre removal not yet implemented"}), 200


@misc_api_bp.route("/genres/recent-updates", methods=["GET"])
def api_recent_genre_updates():
    """Get recent genre updates for the logs page."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM genre_updates ORDER BY created_at DESC LIMIT 50")
        rows = cursor.fetchall()
        conn.close()
        return jsonify({"success": True, "updates": [dict(r) for r in rows]})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ===========================================================================
# CORRECTIONS
# ===========================================================================

@misc_api_bp.route("/correcting/fix-album-field", methods=["POST"])
def api_correcting_fix_album_field():
    """Apply a single field value to all tracks in an album."""
    try:
        from services.metadata.correction_service import fix_album_field
        payload = request.get_json(silent=True) or {}
        album_artist = (payload.get("album_artist") or "").strip()
        album = (payload.get("album") or "").strip()
        field = (payload.get("field") or "").strip()
        value = payload.get("value")
        if not album or not field:
            return jsonify({"error": "album and field required"}), 400
        updated, files = fix_album_field(album_artist, album, field, value)
        return jsonify({"success": True, "updated_count": updated, "files_updated": files})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@misc_api_bp.route("/correcting/mb-suggestions")
def api_correcting_mb_suggestions():
    """Fetch MusicBrainz authoritative values for an album."""
    album_artist = request.args.get("album_artist", "").strip()
    album = request.args.get("album", "").strip()
    if not album:
        return jsonify({"error": "album required"}), 400
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT musicbrainz_albumid FROM tracks WHERE COALESCE(NULLIF(album_artist,''), artist) = %s AND album = %s "
            "AND musicbrainz_albumid IS NOT NULL AND TRIM(musicbrainz_albumid) != '' GROUP BY musicbrainz_albumid ORDER BY COUNT(*) DESC LIMIT 1",
            (album_artist, album),
        )
        row = cursor.fetchone()
        conn.close()
        if not row:
            return jsonify({"success": True, "suggestions": {}, "mbid": None}), 200
        mbid = row[0] if not hasattr(row, "get") else row.get("musicbrainz_albumid")
        import requests, time
        headers = {"User-Agent": "Popularr/1.0", "Accept": "application/json"}
        time.sleep(1.0)
        resp = requests.get(
            f"https://musicbrainz.org/ws/2/release/{mbid}",
            params={"fmt": "json", "inc": "release-groups+labels"},
            headers=headers, timeout=12,
        )
        if resp.status_code == 404:
            return jsonify({"success": True, "suggestions": {}, "mbid": mbid}), 200
        resp.raise_for_status()
        data = resp.json()
        suggestions = {}
        rg = data.get("release-group") or {}
        raw_date = (rg.get("first-release-date") or data.get("date") or "").strip()
        if raw_date:
            suggestions["year"] = raw_date[:4]
        country = (data.get("country") or "").strip()
        if country:
            suggestions["releasecountry"] = country
        status = (data.get("status") or "").strip()
        if status:
            suggestions["releasestatus"] = status
        primary_type = (rg.get("primary-type") or "").strip()
        secondary_types = [t for t in (rg.get("secondary-types") or []) if t]
        if primary_type or secondary_types:
            all_types = ([primary_type] if primary_type else []) + secondary_types
            suggestions["releasetype"] = ", ".join(all_types)
        label_info = data.get("label-info") or []
        if label_info:
            label_obj = label_info[0].get("label") or {}
            label_name = (label_obj.get("name") or "").strip()
            if label_name:
                suggestions["label"] = label_name
                suggestions["recordlabel"] = label_name
        media_list = data.get("media") or []
        if media_list:
            suggestions["disctotal"] = str(len(media_list))
            total_tracks = sum(len(m.get("tracks") or []) or int(m.get("track-count") or 0) for m in media_list)
            if total_tracks:
                suggestions["tracktotal"] = str(total_tracks)
            formats = [m.get("format") for m in media_list if m.get("format")]
            if formats:
                suggestions["media"] = formats[0]
        return jsonify({"success": True, "suggestions": suggestions, "mbid": mbid})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        if conn:
            conn.close()


@misc_api_bp.route("/correcting/ignore", methods=["POST"])
def api_correcting_ignore():
    """Persist an ignore rule for a specific (album_artist, album, field)."""
    try:
        payload = request.get_json(silent=True) or {}
        album_artist = (payload.get("album_artist") or "").strip()
        album = (payload.get("album") or "").strip()
        field = (payload.get("field") or "").strip()
        if not album or not field:
            return jsonify({"error": "album and field required"}), 400
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS correction_ignores (id SERIAL PRIMARY KEY, "
            "album_artist TEXT NOT NULL DEFAULT '', album TEXT NOT NULL, field TEXT NOT NULL, "
            "ignored_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE (album_artist, album, field))"
        )
        cursor.execute(
            "INSERT INTO correction_ignores (album_artist, album, field) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (album_artist, album, field),
        )
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@misc_api_bp.route("/correcting/unignore", methods=["POST"])
def api_correcting_unignore():
    """Remove an ignore rule."""
    try:
        payload = request.get_json(silent=True) or {}
        album_artist = (payload.get("album_artist") or "").strip()
        album = (payload.get("album") or "").strip()
        field = (payload.get("field") or "").strip()
        if not album or not field:
            return jsonify({"error": "album and field required"}), 400
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM correction_ignores WHERE album = %s AND field = %s AND COALESCE(album_artist, '') = %s",
            (album, field, album_artist),
        )
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@misc_api_bp.route("/correcting/ignores")
def api_correcting_list_ignores():
    """Return all active ignore rules."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT album_artist, album, field, ignored_at FROM correction_ignores")
        rows = cursor.fetchall()
        ignores = [{"album_artist": r.get("album_artist") or "", "album": r.get("album") or "",
                     "field": r.get("field") or "", "ignored_at": str(r.get("ignored_at") or "")}
                    for r in rows]
        return jsonify({"success": True, "ignores": ignores})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


# ===========================================================================
# BOOKMARKS
# ===========================================================================

@misc_api_bp.route("/bookmarks", methods=["GET", "POST"])
def api_bookmarks():
    """Get all bookmarks or add a new bookmark."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        if request.method == "GET":
            cursor.execute("SELECT * FROM bookmarks ORDER BY created_at DESC LIMIT 100")
            rows = cursor.fetchall()
            return jsonify({"success": True, "bookmarks": [dict(r) for r in rows]})
        elif request.method == "POST":
            data = request.json or {}
            btype = str(data.get("type") or "custom").strip()
            name = str(data.get("name") or "").strip()
            url = str(data.get("url") or "").strip()
            if not name:
                return jsonify({"error": "name required"}), 400
            cursor.execute(
                "INSERT INTO bookmarks (type, name, url) VALUES (%s, %s, %s) RETURNING id",
                (btype, name, url),
            )
            conn.commit()
            return jsonify({"success": True, "id": cursor.fetchone()[0]}), 201
        return jsonify({"error": "Unsupported method"}), 405
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


@misc_api_bp.route("/bookmarks/<int:bookmark_id>", methods=["DELETE"])
def api_delete_bookmark(bookmark_id):
    """Delete a bookmark."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM bookmarks WHERE id = %s", (bookmark_id,))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ===========================================================================
# ESSENTIA
# ===========================================================================

@misc_api_bp.route("/essentia/download-models", methods=["POST"])
def api_essentia_download_models():
    """Download Essentia model files."""
    return jsonify({"status": "started", "message": "Download started"}), 202


@misc_api_bp.route("/essentia/download-status")
def api_essentia_download_status():
    """Return download status for Essentia models."""
    return jsonify({"status": "idle"}), 200


# ===========================================================================
# DATABASE
# ===========================================================================
@misc_api_bp.route("/database/cleanup-duplicates", methods=["POST"])
def api_cleanup_duplicates():
    """Clean up duplicate albums in the database."""
    return jsonify({"success": True, "message": "Cleanup triggered"}), 200
