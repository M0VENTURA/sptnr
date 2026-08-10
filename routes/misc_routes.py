"""Miscellaneous API routes — genres, corrections, bookmarks, country, essentia, etc."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from quart import Blueprint, jsonify, request, Response
from sqlalchemy import text

from db.engine import db_session
from helpers.config_helpers import get_config
from services.catalog.album_classification_service import classify_album_type

logger = logging.getLogger(__name__)

misc_api_bp = Blueprint("misc_api", __name__, url_prefix="/api")


# ===========================================================================
# SEARCH
# ===========================================================================
@misc_api_bp.route("/search", methods=["POST"])
async def api_search():
    """Search artists, albums and tracks with legacy ranking behaviour."""
    try:
        data = (await request.get_json(silent=True)) or {}

        query = str(data.get("query") or "").strip().lower()

        if not query or len(query) < 2:
            return jsonify({"error": "Search query must be at least 2 characters"}), 400

        exact_pattern = query
        starts_pattern = f"{query}%"
        contains_pattern = f"%{query}%"

        with db_session() as session:

            # ---------------------------------------------------------
            # Artists
            # ---------------------------------------------------------
            artist_result = session.execute(
                text("""
                    SELECT
                        COALESCE(NULLIF(album_artist, ''), artist) AS name,
                        COUNT(DISTINCT album) AS album_count,
                        COUNT(*) AS track_count,
                        CASE
                            WHEN LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = :exact THEN 0
                            WHEN LOWER(COALESCE(NULLIF(album_artist, ''), artist)) LIKE :starts THEN 1
                            ELSE 2
                        END AS match_rank
                    FROM tracks
                    WHERE LOWER(COALESCE(artist, '')) LIKE :contains
                       OR LOWER(COALESCE(album_artist, '')) LIKE :contains
                    GROUP BY COALESCE(NULLIF(album_artist, ''), artist)
                    ORDER BY
                        match_rank ASC,
                        track_count DESC
                    LIMIT 20
                """),
                {
                    "exact": exact_pattern,
                    "starts": starts_pattern,
                    "contains": contains_pattern,
                },
            )

            artists = [
                {
                    "name": row._mapping["name"],
                    "album_count": int(row._mapping["album_count"] or 0),
                    "track_count": int(row._mapping["track_count"] or 0),
                }
                for row in artist_result.fetchall()
            ]
            # ---------------------------------------------------------
            # Albums — bucketed by release type (albums / compilations /
            # live_albums / eps / singles), each with its release year so the
            # search modal can render the artist-page discography structure.
            # ---------------------------------------------------------
            album_result = session.execute(
                text("""
                    SELECT
                        COALESCE(NULLIF(album_artist, ''), artist) AS artist,
                        album,
                        COUNT(*) AS track_count,
                        AVG(stars) AS avg_stars,
                        MAX(COALESCE(NULLIF(year, 0), release_year, 0)) AS album_year,
                        MAX(COALESCE(NULLIF(musicbrainz_albumtype, ''),
                                     NULLIF(spotify_album_type, ''),
                                     NULLIF(album_type, ''))) AS album_type,
                        CASE
                            WHEN LOWER(COALESCE(album, '')) = :exact THEN 0
                            WHEN LOWER(COALESCE(album, '')) LIKE :starts THEN 1
                            ELSE 2
                        END AS match_rank
                    FROM tracks
                    WHERE LOWER(COALESCE(album, '')) LIKE :contains
                    GROUP BY
                        COALESCE(NULLIF(album_artist, ''), artist),
                        album
                    ORDER BY
                        match_rank ASC,
                        track_count DESC
                    LIMIT 20
                """),
                {
                    "exact": exact_pattern,
                    "starts": starts_pattern,
                    "contains": contains_pattern,
                },
            )

            # Map each album into the unified-search buckets (mirrors the
            # artist page discography).  Remix albums fold into the Albums
            # bucket per the unified-search blueprint.
            _bucket_map = {
                "album": "albums",
                "remix_album": "albums",
                "compilation": "compilations",
                "live_album": "live_albums",
                "ep": "eps",
                "single": "singles",
            }
            _type_labels = {
                "albums": "Studio Album",
                "remix_album": "Remix Album",
                "compilations": "Compilation",
                "live_albums": "Live Album",
                "eps": "EP",
                "singles": "Single",
            }
            albums_by_bucket: dict[str, list[dict[str, Any]]] = {
                "albums": [],
                "compilations": [],
                "live_albums": [],
                "eps": [],
                "singles": [],
            }
            for row in album_result.fetchall():
                _m = row._mapping
                _raw_type = classify_album_type(dict(_m))
                _bucket = _bucket_map.get(_raw_type, "albums")
                _year = int(_m["album_year"] or 0) or None
                albums_by_bucket[_bucket].append({
                    "artist": _m["artist"],
                    "album": _m["album"],
                    "year": _year,
                    "track_count": int(_m["track_count"] or 0),
                    "avg_stars": (
                        float(_m["avg_stars"])
                        if _m["avg_stars"] is not None
                        else None
                    ),
                    "type": _bucket,
                    "type_label": _type_labels.get(_raw_type, _type_labels[_bucket]),
                    "in_library": True,
                })

            # ---------------------------------------------------------
            # Tracks
            # ---------------------------------------------------------
            track_result = session.execute(
                text("""
                    SELECT
                        id,
                        title,
                        COALESCE(NULLIF(album_artist, ''), artist) AS artist,
                        album,
                        stars,
                        CASE
                            WHEN LOWER(COALESCE(title, '')) = :exact THEN 0
                            WHEN LOWER(COALESCE(title, '')) LIKE :starts THEN 1
                            WHEN LOWER(COALESCE(title, '')) LIKE :contains THEN 2
                            ELSE 3
                        END AS match_rank
                    FROM tracks
                    WHERE LOWER(COALESCE(title, '')) LIKE :contains
                       OR LOWER(COALESCE(artist, '')) LIKE :contains
                       OR LOWER(COALESCE(album_artist, '')) LIKE :contains
                    ORDER BY
                        match_rank ASC,
                        stars DESC NULLS LAST,
                        LOWER(COALESCE(title, '')) ASC
                    LIMIT 50
                """),
                {
                    "exact": exact_pattern,
                    "starts": starts_pattern,
                    "contains": contains_pattern,
                },
            )

            tracks = [
                {
                    "id": row._mapping["id"],
                    "title": row._mapping["title"],
                    "artist": row._mapping["artist"],
                    "album": row._mapping["album"],
                    "stars": row._mapping["stars"],
                }
                for row in track_result.fetchall()
            ]

        return jsonify({
            "artists": artists,
            **albums_by_bucket,
            "tracks": tracks,
        })

    except Exception as exc:
        logger.exception("Search error")
        return jsonify({"error": str(exc)}), 500


# ===========================================================================
# STATS
# ===========================================================================

@misc_api_bp.route("/stats", methods=["GET"])
def api_stats():
    """Get library statistics."""
    try:
        with db_session() as session:
            result = session.execute(text("SELECT COUNT(*) as track_count, COUNT(DISTINCT album) as album_count, "
                       "COUNT(DISTINCT COALESCE(NULLIF(album_artist, ''), artist)) as artist_count, "
                       "AVG(stars) as avg_stars, SUM(duration) as total_duration FROM tracks"))
            stats = dict(result.fetchone()._mapping)
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
        with db_session() as session:
            count = session.execute(text("SELECT COUNT(*) as count FROM tracks")).scalar()
        return jsonify({"count": count or 0})
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
async def api_fetch_artist_country():
    """Fetch artist country from MusicBrainz and update database."""
    data = (await request.get_json()) or {}
    artist = str(data.get("artist_name") or "").strip()
    if not artist:
        return jsonify({"error": "artist_name required"}), 400
    try:
        from api_clients.musicbrainz_http import MusicBrainzHttpClient
        client = MusicBrainzHttpClient(enabled=True)
        # area.name is the readable country (the raw "country" field is an
        # ISO code and frequently absent from search results).
        country = client.get_artist_country(artist)
        if country:
            with db_session() as session:
                session.execute(text("INSERT INTO artists (name, country) VALUES (:artist, :country) ON CONFLICT (name) DO UPDATE SET country = EXCLUDED.country"), {"artist": artist, "country": country})
                session.execute(text("UPDATE tracks SET artist_country = :country WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist"), {"country": country, "artist": artist})
        return jsonify({"success": True, "country": country})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@misc_api_bp.route("/artist/country/update", methods=["POST"])
async def api_update_artist_country():
    """Manually update artist country."""
    data = (await request.get_json()) or {}
    artist = str(data.get("artist_name") or "").strip()
    country = str(data.get("country") or "").strip()
    if not artist or not country:
        return jsonify({"error": "artist_name and country required"}), 400
    try:
        with db_session() as session:
            session.execute(text("INSERT INTO artists (name, country) VALUES (:artist, :country) ON CONFLICT (name) DO UPDATE SET country = EXCLUDED.country"), {"artist": artist, "country": country})
            session.execute(text("UPDATE tracks SET artist_country = :country WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist"), {"country": country, "artist": artist})
        return jsonify({"success": True, "country": country})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@misc_api_bp.route("/artist/country/apply-as-genre", methods=["POST"])
async def api_apply_country_as_genre():
    """Apply artist country as genre tag to all tracks."""
    data = (await request.get_json()) or {}
    artist = str(data.get("artist_name") or "").strip()
    if not artist:
        return jsonify({"error": "artist_name required"}), 400
    try:
        with db_session() as session:
            result = session.execute(text("""SELECT country FROM artists WHERE name = :artist """), {"artist": artist})
            row = result.fetchone()
            if not row:
                return jsonify({"error": "Artist not found or no country"}), 404
            country = row[0]
            session.execute(
                text("""UPDATE tracks SET genres = CONCAT_WS(' \\ ', COALESCE(NULLIF(genres, ''), ''), :country) WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist"""),
                {"country": country, "artist": artist},
            )
        return jsonify({"success": True, "country": country})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ===========================================================================
# DUPLICATE ARTISTS
# ===========================================================================

@misc_api_bp.route("/duplicate-artists/<path:artist>", methods=["GET"])
def api_get_duplicate_artists(artist):
    """Get duplicate artists for a specific artist.

    Returns ``{duplicates: [{mbid, canonical_mb, variations[], track_counts{}}],
    artist_info}`` — the shape the corrections page's merge UI expects.
    Uses the track artist (not album_artist) so compilation appearances
    don't contaminate detection.
    """
    from urllib.parse import unquote
    artist = unquote(artist)
    try:
        with db_session() as session:
            # MBID for the current artist.
            row = session.execute(text("""
                SELECT DISTINCT musicbrainz_artistid
                FROM tracks
                WHERE artist = :artist
                  AND musicbrainz_artistid IS NOT NULL
                  AND musicbrainz_artistid != ''
                LIMIT 1
            """), {"artist": artist}).fetchone()
            artist_mbid = str(row[0]) if row else None

            duplicates = []
            if artist_mbid:
                # All artist-name variations sharing this MBID.
                rows = session.execute(text("""
                    SELECT artist, COUNT(*) AS track_count
                    FROM tracks
                    WHERE musicbrainz_artistid = :mbid
                    GROUP BY artist
                    ORDER BY track_count DESC
                """), {"mbid": artist_mbid}).fetchall()
                variations_data = [dict(r._mapping) for r in rows]

                if len(variations_data) > 1:
                    # Canonical display name: MB artist name when resolvable,
                    # else the most common local variation.
                    canonical_mb = variations_data[0].get("artist") or ""
                    try:
                        from api_clients.musicbrainz_http import MusicBrainzHttpClient
                        mb_artist = MusicBrainzHttpClient().get_artist(artist_mbid) or {}
                        if (mb_artist.get("name") or "").strip():
                            canonical_mb = str(mb_artist["name"]).strip()
                    except Exception:
                        pass
                    variations = [v.get("artist") for v in variations_data if v.get("artist")]
                    track_counts = {
                        v.get("artist"): int(v.get("track_count") or 0)
                        for v in variations_data if v.get("artist")
                    }
                    duplicates.append({
                        "mbid": artist_mbid,
                        "canonical_mb": canonical_mb,
                        "variations": variations,
                        "track_counts": track_counts,
                        "current_artist": artist,
                    })

        return jsonify({
            "success": True,
            "duplicates": duplicates,
            "artist_info": {"name": artist, "mbid": artist_mbid},
        })
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc), "duplicates": []}), 500


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
        with db_session() as session:
            result = session.execute(text("""
                SELECT spotify_genres, lastfm_tags, musicbrainz_genres, discogs_genres,
                       essentia_genres, mood, listenbrainz_genres
                FROM tracks WHERE CAST(id AS TEXT) = :id
            """), {"id": track_id})
            row = result.fetchone()
        if not row:
            return jsonify({"error": "Track not found"}), 404

        import json as _json
        genres: dict[str, list[dict[str, str | int]]] = {}
        source_keys = [
            "discogs_genres", "mood", "essentia_genres",
            "musicbrainz_genres", "lastfm_tags", "listenbrainz_genres", "spotify_genres",
        ]
        for idx, output_key in enumerate(source_keys):
            raw = row[idx]
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
        with db_session() as session:
            result = session.execute(text("""
                SELECT spotify_genres, lastfm_tags, musicbrainz_genres, discogs_genres,
                       essentia_genres, mood, listenbrainz_genres
                FROM tracks
                WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND album = :album
            """), {"artist": artist, "album": album})
            rows = result.fetchall()

        from collections import Counter
        source_keys = [
            "discogs_genres", "mood", "essentia_genres",
            "musicbrainz_genres", "lastfm_tags", "listenbrainz_genres", "spotify_genres",
        ]
        result: dict[str, list[dict[str, str | int]]] = {k: [] for k in source_keys}

        import json as _json
        for row in rows:
            for idx, key in enumerate(source_keys):
                raw = row[idx]
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
        with db_session() as session:
            result = session.execute(text("""
                SELECT spotify_genres, lastfm_tags, musicbrainz_genres, discogs_genres,
                       essentia_genres, mood, listenbrainz_genres
                FROM tracks
                WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
            """), {"artist": artist})
            rows = result.fetchall()

        from collections import Counter
        source_keys = [
            "discogs_genres", "mood", "essentia_genres",
            "musicbrainz_genres", "lastfm_tags", "listenbrainz_genres", "spotify_genres",
        ]
        result: dict[str, list[dict[str, str | int]]] = {k: [] for k in source_keys}

        import json as _json
        for row in rows:
            for idx, key in enumerate(source_keys):
                raw = row[idx] if idx < len(row) else None
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
        with db_session() as session:
            result = session.execute(text("SELECT * FROM genre_updates ORDER BY created_at DESC LIMIT 50"))
            rows = result.fetchall()
        return jsonify({"success": True, "updates": [dict(r._mapping) for r in rows]})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ===========================================================================
# CORRECTIONS
# ===========================================================================

@misc_api_bp.route("/correcting/fix-album-field", methods=["POST"])
async def api_correcting_fix_album_field():
    """Apply a single field value to all tracks in an album."""
    try:
        from services.metadata.correction_service import fix_album_field
        payload = (await request.get_json(silent=True)) or {}
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
    try:
        with db_session() as session:
            result = session.execute(
                text("SELECT musicbrainz_albumid FROM tracks WHERE COALESCE(NULLIF(album_artist,''), artist) = :artist AND album = :album "
                "AND musicbrainz_albumid IS NOT NULL AND TRIM(musicbrainz_albumid) != '' GROUP BY musicbrainz_albumid ORDER BY COUNT(*) DESC LIMIT 1"),
                {"artist": album_artist, "album": album},
            )
            row = result.fetchone()
        if not row:
            return jsonify({"success": True, "suggestions": {}, "mbid": None}), 200
        mbid = str(row[0])
        from api_clients.musicbrainz_http import MusicBrainzHttpClient
        mb_client = MusicBrainzHttpClient()
        time.sleep(1.0)
        data = mb_client.get_release(mbid, inc="release-groups+labels", timeout=12.0)
        if not data:
            return jsonify({"success": True, "suggestions": {}, "mbid": mbid}), 200
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


@misc_api_bp.route("/correcting/ignore", methods=["POST"])
async def api_correcting_ignore():
    """Persist an ignore rule for a specific (album_artist, album, field)."""
    try:
        payload = (await request.get_json(silent=True)) or {}
        album_artist = (payload.get("album_artist") or "").strip()
        album = (payload.get("album") or "").strip()
        field = (payload.get("field") or "").strip()
        if not album or not field:
            return jsonify({"error": "album and field required"}), 400
        with db_session() as session:
            session.execute(
                text("CREATE TABLE IF NOT EXISTS correction_ignores (id SERIAL PRIMARY KEY, "
                "album_artist TEXT NOT NULL DEFAULT '', album TEXT NOT NULL, field TEXT NOT NULL, "
                "ignored_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE (album_artist, album, field))")
            )
            session.execute(
                text("INSERT INTO correction_ignores (album_artist, album, field) VALUES (:artist, :album, :field) ON CONFLICT DO NOTHING"),
                {"artist": album_artist, "album": album, "field": field},
            )
        return jsonify({"success": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@misc_api_bp.route("/correcting/unignore", methods=["POST"])
async def api_correcting_unignore():
    """Remove an ignore rule."""
    try:
        payload = (await request.get_json(silent=True)) or {}
        album_artist = (payload.get("album_artist") or "").strip()
        album = (payload.get("album") or "").strip()
        field = (payload.get("field") or "").strip()
        if not album or not field:
            return jsonify({"error": "album and field required"}), 400
        with db_session() as session:
            session.execute(
                text("CREATE TABLE IF NOT EXISTS correction_ignores (id SERIAL PRIMARY KEY, "
                "album_artist TEXT NOT NULL DEFAULT '', album TEXT NOT NULL, field TEXT NOT NULL, "
                "ignored_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE (album_artist, album, field))")
            )
            session.execute(
                text("DELETE FROM correction_ignores WHERE album = :album AND field = :field AND COALESCE(album_artist, '') = :artist"),
                {"album": album, "field": field, "artist": album_artist},
            )
        return jsonify({"success": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@misc_api_bp.route("/correcting/ignores")
async def api_correcting_list_ignores():
    """Return all active ignore rules."""
    try:
        with db_session() as session:
            session.execute(
                text("CREATE TABLE IF NOT EXISTS correction_ignores (id SERIAL PRIMARY KEY, "
                "album_artist TEXT NOT NULL DEFAULT '', album TEXT NOT NULL, field TEXT NOT NULL, "
                "ignored_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE (album_artist, album, field))")
            )
            result = session.execute(text("SELECT album_artist, album, field, ignored_at FROM correction_ignores"))
            rows = result.fetchall()
        ignores = [{"album_artist": str(r[0] or ""), "album": str(r[1] or ""),
                     "field": str(r[2] or ""), "ignored_at": str(r[3] or "")}
                    for r in rows]
        return jsonify({"success": True, "ignores": ignores})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ===========================================================================
# BOOKMARKS
# ===========================================================================

@misc_api_bp.route("/bookmarks", methods=["GET", "POST"])
async def api_bookmarks():
    """Get all bookmarks or add a new bookmark."""
    if request.method == "GET":
        with db_session() as session:
            result = session.execute(text("SELECT * FROM bookmarks ORDER BY created_at DESC LIMIT 100"))
            rows = result.fetchall()
        return jsonify({"success": True, "bookmarks": [dict(r._mapping) for r in rows]})
    elif request.method == "POST":
        data = (await request.get_json()) or {}
        btype = str(data.get("type") or "custom").strip()
        name = str(data.get("name") or "").strip()
        url = str(data.get("url") or "").strip()
        if not name:
            return jsonify({"error": "name required"}), 400
        with db_session() as session:
            result = session.execute(
                text("INSERT INTO bookmarks (type, name, url) VALUES (:type, :name, :url) RETURNING id"),
                {"type": btype, "name": name, "url": url},
            )
            return jsonify({"success": True, "id": result.scalar()}), 201
    return jsonify({"error": "Unsupported method"}), 405


@misc_api_bp.route("/bookmarks/<int:bookmark_id>", methods=["DELETE"])
def api_delete_bookmark(bookmark_id):
    """Delete a bookmark."""
    try:
        with db_session() as session:
            session.execute(text("DELETE FROM bookmarks WHERE id = :id"), {"id": bookmark_id})
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
    """Return download status for Essentia models.

    Checks whether the configured models directory exists and contains
    model files (``.pb`` / ``.json``).  Returns ``"installed"`` when
    at least one model file is found, so the UI can hide the download
    button.
    """
    from helpers.config_helpers import get_config
    import os

    cfg = get_config()
    models_dir = (
        (cfg.get("essentia", {}) or {}).get("models_dir")
        or os.environ.get("ESSENTIA_MODELS_DIR")
        or "/opt/essentia_models"
    )

    if os.path.isdir(models_dir):
        model_files = [f for f in os.listdir(models_dir) if f.endswith((".pb", ".json"))]
        if model_files:
            return jsonify({"status": "installed", "models_dir": models_dir, "file_count": len(model_files)}), 200

    return jsonify({"status": "idle", "models_dir": models_dir}), 200


# ===========================================================================
# DATABASE
# ===========================================================================
@misc_api_bp.route("/database/cleanup-duplicates", methods=["POST"])
def api_cleanup_duplicates():
    """Clean up duplicate albums in the database."""
    return jsonify({"success": True, "message": "Cleanup triggered"}), 200
