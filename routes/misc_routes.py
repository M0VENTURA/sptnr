"""Miscellaneous API routes — genres, corrections, bookmarks, country, essentia, etc."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from datetime import datetime
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
# ALGORITHM SANDBOX — read-only metrics for client-side weight simulation
# ===========================================================================
# The sandbox page fetches a flattened, lightweight track payload, then runs
# the linear weight arithmetic locally so sliders stay frame-rate friendly.
# Per-album median/MAD of the current raw blend are precomputed here so the
# browser can mirror the album-relative re-map (50 + z*16.7 below the median,
# logistic above) without shipping the whole library's distributions.

_SANDBOX_MAX_TRACKS = 30000
# Default blend used to compute the album reference distribution server-side;
# mirrors the live popularity weights (config popularity.weights.*).
_SANDBOX_REF_WEIGHTS = {"lf": 0.55, "lb": 0.35, "age": 0.10}


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


@misc_api_bp.route("/sandbox/metrics")
def api_sandbox_metrics():
    """Flattened track metrics for the Algorithm Sandbox.

    Query params: ``scope`` = ``global`` (default, capped at 30k tracks) |
    ``recent`` (last-scanned within 14 days) | ``artist`` (requires ``artist``).
    Each row carries the per-source raw scores, the stored final score + live
    stars, the confirmed-single flag, and the album's median/MAD of the raw
    blend (computed with the default weights) so the client can mirror the
    album-relative re-map.  Nothing is written.
    """
    try:
        scope = str(request.args.get("scope", "global") or "global").strip().lower()
        artist = str(request.args.get("artist", "") or "").strip()

        where = "COALESCE(final_score, 0) > 0"
        params: dict[str, Any] = {}
        if scope == "recent":
            where += " AND last_scanned >= (NOW() - INTERVAL '14 days')"
        elif scope == "artist":
            if not artist:
                return jsonify({"error": "artist parameter required for scope=artist"}), 400
            where += " AND LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)"
            params["artist"] = artist
        elif scope != "global":
            return jsonify({"error": "scope must be global, recent or artist"}), 400

        with db_session() as session:
            rows = session.execute(
                text(
                    "SELECT CAST(id AS TEXT) AS id, title, artist, album, "
                    "COALESCE(lastfm_score, 0) AS lf, COALESCE(listenbrainz_score, 0) AS lb, "
                    "COALESCE(age_score, 0) AS age, COALESCE(final_score, 0) AS score, "
                    "COALESCE(stars, 0) AS stars, COALESCE(is_single, 0) AS single "
                    f"FROM tracks WHERE {where} ORDER BY artist, album, track_number "
                    f"LIMIT {_SANDBOX_MAX_TRACKS}"
                ),
                params,
            ).fetchall()

        tracks = [dict(r._mapping) for r in rows or []]

        # Per-album reference distribution: raw blend with the DEFAULT weights,
        # then median + MAD of the album's valid (>0) blends.
        by_album: dict[str, list[float]] = {}
        for t in tracks:
            raw = (t.get("lf") or 0) * _SANDBOX_REF_WEIGHTS["lf"] \
                + (t.get("lb") or 0) * _SANDBOX_REF_WEIGHTS["lb"] \
                + (t.get("age") or 0) * _SANDBOX_REF_WEIGHTS["age"]
            t["raw"] = round(raw, 2)
            if raw > 0:
                by_album.setdefault(str(t.get("album") or ""), []).append(raw)
        for t in tracks:
            ref = by_album.get(str(t.get("album") or "")) or []
            if len(ref) >= 3:
                med = _median(ref)
                mad = _median([abs(v - med) for v in ref])
                t["a_med"] = round(med, 2)
                t["a_mad"] = round(mad, 2)
            else:
                t["a_med"] = None
                t["a_mad"] = None

        return jsonify({
            "tracks": tracks,
            "count": len(tracks),
            "truncated": len(tracks) >= _SANDBOX_MAX_TRACKS,
            "ref_weights": _SANDBOX_REF_WEIGHTS,
        })
    except Exception as exc:
        logger.error("[sandbox] metrics fetch failed: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


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
                    WITH variants AS (
                        SELECT
                            COALESCE(NULLIF(album_artist, ''), artist) AS variant,
                            LOWER(COALESCE(NULLIF(album_artist, ''), artist)) AS name_key,
                            COUNT(*) AS cnt,
                            COUNT(DISTINCT album) AS album_count
                        FROM tracks
                        WHERE LOWER(COALESCE(artist, '')) LIKE :contains
                           OR LOWER(COALESCE(album_artist, '')) LIKE :contains
                        GROUP BY COALESCE(NULLIF(album_artist, ''), artist)
                    ),
                    ranked AS (
                        SELECT
                            variant, name_key, cnt, album_count,
                            ROW_NUMBER() OVER (
                                PARTITION BY name_key
                                ORDER BY cnt DESC,
                                         (initcap(variant) = variant) DESC,
                                         LENGTH(variant) DESC
                            ) AS rn
                        FROM variants
                    )
                    SELECT
                        variant AS name,
                        SUM(cnt) AS track_count,
                        SUM(album_count) AS album_count,
                        CASE
                            WHEN name_key = :exact THEN 0
                            WHEN name_key LIKE :starts THEN 1
                            ELSE 2
                        END AS match_rank
                    FROM ranked
                    WHERE rn = 1
                    GROUP BY name_key, variant
                    ORDER BY
                        match_rank ASC,
                        SUM(cnt) DESC
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
                    WITH variants AS (
                        SELECT
                            COALESCE(NULLIF(album_artist, ''), artist) AS variant,
                            LOWER(COALESCE(NULLIF(album_artist, ''), artist)) AS name_key,
                            album,
                            COUNT(*) AS track_count,
                            AVG(stars) AS avg_stars,
                            SUM(duration) AS album_duration,
                            -- year is stored as TEXT; extract the leading 4-digit
                            -- year (tolerates junk like "1990-05-01" or "TBA")
                            MAX(COALESCE(
                                NULLIF(SUBSTRING(year FROM '^[0-9]{4}'), '')::INTEGER,
                                release_year,
                                0
                            )) AS album_year,
                            MAX(COALESCE(NULLIF(musicbrainz_albumtype, ''),
                                         NULLIF(spotify_album_type, ''))) AS album_type,
                            CASE
                                WHEN LOWER(COALESCE(album, '')) = :exact THEN 0
                                WHEN LOWER(COALESCE(album, '')) LIKE :starts THEN 1
                                ELSE 2
                            END AS match_rank
                        FROM tracks
                        WHERE LOWER(COALESCE(album, '')) LIKE :contains
                           OR LOWER(COALESCE(album_artist, '')) LIKE :contains
                           OR LOWER(COALESCE(artist, '')) LIKE :contains
                        GROUP BY
                            COALESCE(NULLIF(album_artist, ''), artist),
                            album
                    ),
                    ranked AS (
                        SELECT
                            variant, name_key, album, track_count, avg_stars,
                            album_duration, album_year, album_type, match_rank,
                            ROW_NUMBER() OVER (
                                PARTITION BY name_key, album
                                ORDER BY track_count DESC,
                                         (initcap(variant) = variant) DESC,
                                         LENGTH(variant) DESC
                            ) AS rn
                        FROM variants
                    )
                    SELECT
                        variant AS artist,
                        album,
                        track_count,
                        avg_stars,
                        album_duration,
                        album_year,
                        album_type,
                        match_rank
                    FROM ranked
                    WHERE rn = 1
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
                    "duration_total": (
                        float(_m["album_duration"])
                        if _m["album_duration"] is not None
                        else None
                    ),
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
async def api_features_update():
    """Update individual feature flags in config.yaml.

    Whitelist-only (a malicious/accidental payload cannot write arbitrary
    keys): only the known boolean feature flags are accepted.  Changes are
    deep-merged into the existing YAML via ``save_partial_config`` so the
    in-memory config cache is refreshed immediately.
    """
    try:
        data = (await request.get_json(silent=True)) or {}
    except Exception:
        data = {}
    if not data:
        return jsonify({"success": False, "error": "No JSON data provided"}), 400

    allowed_bool_keys = {
        "perpetual", "force", "launch_on_startup", "startup_scan_restart",
        "sync_ratings_to_all_users",
    }
    updates = {k: bool(v) for k, v in data.items() if k in allowed_bool_keys}
    if not updates:
        return jsonify({"success": False, "error": "No supported feature flags in payload"}), 400

    try:
        from helpers.config_helpers import save_partial_config
        ok = save_partial_config({"features": updates})
        if not ok:
            return jsonify({"success": False, "error": "Failed to write config.yaml"}), 500
        logger.info("[FEATURES] Updated feature flags: %s", updates)
        return jsonify({"success": True, **updates})
    except Exception as exc:
        logger.error("[FEATURES] Error updating feature flags: %s", exc, exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500


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
async def api_merge_duplicate_artists():
    """Merge duplicate artist variants into a canonical name.

    Rewrites ``artist`` / ``album_artist`` on every matching track to the
    canonical spelling (case-insensitive variants included), then best-effort
    syncs the corrected names into file tags.  Physical file renames are NOT
    performed — the library folder structure is left untouched (run the File
    Organization rename action afterwards to reorganize paths).

    Request JSON:
        - new_artist: Canonical artist name (required)
        - source_artists: list of variant names to merge (optional)
        - mbid: MusicBrainz artist ID (optional, accepted for parity)
        - dry_run: preview without executing (optional)
    """
    try:
        payload = (await request.get_json(silent=True)) or {}
        new_artist = str(payload.get("new_artist") or "").strip()
        source_artists = payload.get("source_artists") or []
        dry_run = bool(payload.get("dry_run", False))

        if not new_artist:
            return jsonify({"success": False, "error": "new_artist is required"}), 400

        if isinstance(source_artists, str):
            source_artists = [source_artists]
        sources = [str(s).strip() for s in (source_artists or []) if str(s).strip()]
        sources = [s for s in sources if s.lower() != new_artist.lower()]
        # Also sweep case/whitespace variants of the canonical name itself
        # (e.g. "babymetal" / "BABYMETAL" → "Babymetal").
        sources.append(new_artist.lower())
        sources.append(new_artist.upper())
        sources.append(new_artist.title())
        sources = list(dict.fromkeys(s for s in sources if s != new_artist))

        if dry_run:
            from db.repositories.scan_repository import normalize_existing_artist_rows
            estimated = normalize_existing_artist_rows(
                canonical_artist_name=new_artist, aliases=sources,
            )
            return jsonify({
                "success": True, "dry_run": True,
                "updated_db": int(estimated or 0), "updated_files": 0,
                "moved_files": 0, "errors": [],
                "message": f"[DRY RUN] Would merge {len(sources)} variant(s) into '{new_artist}': {int(estimated or 0)} tracks",
            })

        errors: list[str] = []
        updated_db = 0
        try:
            from db.repositories.scan_repository import normalize_existing_artist_rows
            updated_db = int(normalize_existing_artist_rows(
                canonical_artist_name=new_artist, aliases=sources,
            ) or 0)
        except Exception as exc:
            errors.append(f"DB normalize failed: {exc}")

        # Best-effort tag sync for tracks whose DB name changed.
        updated_files = 0
        if updated_db and not errors:
            try:
                from db.engine import db_session as _db_session
                from services.metadata.tag_file_service import update_file_metadata
                import os as _os
                with _db_session() as session:
                    rows = session.execute(
                        text("SELECT id, file_path FROM tracks "
                             "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist "
                             "AND file_path IS NOT NULL AND TRIM(file_path) != ''"),
                        {"artist": new_artist},
                    ).fetchall() or []
                for row in rows:
                    fp = str(row[1] or "")
                    if not fp or not _os.path.isfile(fp):
                        continue
                    try:
                        if update_file_metadata(fp, {"artist": new_artist, "album_artist": new_artist}):
                            updated_files += 1
                    except Exception:
                        pass
            except Exception as exc:
                errors.append(f"Tag sync failed: {exc}")

        return jsonify({
            "success": True, "dry_run": False,
            "updated_db": updated_db, "updated_files": updated_files,
            "moved_files": 0, "errors": errors,
            "message": (f"Merged {len(sources)} variant(s) into '{new_artist}': "
                        f"{updated_db} tracks updated, {updated_files} file tags synced"),
        })
    except Exception as exc:
        logger.error("[MERGE] Duplicate artist merge failed: %s", exc, exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500


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
async def api_remove_genres():
    """Remove specific genres from artist or album's tracks (DB only).

    Mirrors the legacy endpoint: updates the ``genres`` and ``manual_genres``
    columns (comma-joined in the current schema), logs a genre-update audit
    row, and triggers a Navidrome rescan in the background so the library
    reflects the change.
    """
    try:
        import threading
        payload = (await request.get_json(silent=True)) or {}
        artist_name = str(payload.get("artist_name") or "").strip()
        album_name = str(payload.get("album_name") or "").strip()
        genres_to_remove = payload.get("genres") or []

        if not artist_name and not album_name:
            return jsonify({"error": "artist_name or album_name required"}), 400
        if not genres_to_remove or not isinstance(genres_to_remove, list):
            return jsonify({"error": "genres must be a non-empty list"}), 400

        remove_lower = {str(g).strip().lower() for g in genres_to_remove if str(g).strip()}
        if not remove_lower:
            return jsonify({"error": "genres must be a non-empty list"}), 400

        def _strip_genres(raw: str | None) -> str:
            if not raw:
                return ""
            cleaned = [
                g for g in (str(g).strip() for g in raw.replace("\\", ",").split(","))
                if g and g.lower() not in remove_lower
            ]
            return ", ".join(dict.fromkeys(cleaned))  # dedupe, preserve order

        affected = 0
        with db_session() as session:
            if album_name:
                rows = session.execute(
                    text("SELECT id, genres, manual_genres FROM tracks "
                         "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND album = :album"),
                    {"artist": artist_name, "album": album_name},
                ).fetchall() or []
            else:
                rows = session.execute(
                    text("SELECT id, genres, manual_genres FROM tracks "
                         "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist"),
                    {"artist": artist_name},
                ).fetchall() or []

            for row in rows:
                track_id, genres_raw, manual_raw = row[0], row[1] or "", row[2] or ""
                new_genres = _strip_genres(genres_raw)
                new_manual = _strip_genres(manual_raw)
                if new_genres == (genres_raw or "") and new_manual == (manual_raw or ""):
                    continue
                session.execute(
                    text("UPDATE tracks SET genres = :genres, manual_genres = :manual WHERE id = :id"),
                    {"genres": new_genres, "manual": new_manual, "id": track_id},
                )
                affected += 1

        if affected:
            try:
                from db.repositories.genres import log_genre_update
                log_genre_update(
                    artist_name=artist_name, album_name=album_name, track_id=None,
                    genres_before="", genres_after="",
                    action_type="remove_from_album" if album_name else "remove_from_artist",
                    affected_count=affected,
                    change_summary=f"Removed genres from {affected} tracks: {', '.join(sorted(remove_lower))}",
                )
            except Exception as exc:
                logger.debug("[GENRES] Audit log failed: %s", exc)

        def _trigger_scan():
            try:
                from helpers.config_helpers import get_config
                from api_clients.navidrome import NavidromeClient
                cfg = get_config() or {}
                users = cfg.get("navidrome_users") or []
                if not users and cfg.get("navidrome"):
                    users = [cfg["navidrome"]]
                for u in users:
                    if u.get("base_url") and u.get("user") and u.get("pass"):
                        client = NavidromeClient(u["base_url"], u["user"], u["pass"])
                        client.start_scan()
                        break
            except Exception as exc:
                logger.debug("[GENRES] Navidrome scan trigger failed: %s", exc)

        threading.Thread(target=_trigger_scan, daemon=True).start()
        return jsonify({
            "success": True,
            "affected_tracks": affected,
            "message": f"Removed genres from {affected} track(s)",
            "scan_triggered": True,
        }), 200
    except Exception as exc:
        logger.error("[GENRES] Genre removal failed: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


@misc_api_bp.route("/tags/track/<track_id>", methods=["GET", "POST"])
async def api_track_tags(track_id):
    """Get or update a single track's metadata tags.

    GET returns the track's editable tag fields.  POST accepts either the
    generic shape ``{tags: {field: value}, sync_to_file: bool}`` or the
    album-page quick-add shape ``{genre: "Metal", action: "add",
    sync_to_file: true}`` which merges the genre into the track's
    ``genres`` + ``manual_genres`` columns without overwriting.
    """
    if request.method == "GET":
        try:
            from db.repositories.tag_repository import get_track_tags
            tags = get_track_tags(track_id)
            if not tags:
                return jsonify({"error": "Track not found"}), 404
            return jsonify({"success": True, "track_id": track_id, "tags": tags})
        except Exception as exc:
            logger.error("[TAGS] Error getting track tags: %s", exc, exc_info=True)
            return jsonify({"error": str(exc)}), 500

    try:
        from db.repositories.tag_repository import update_track_tags
        from db.engine import db_session
        from sqlalchemy import text as _text

        data = (await request.get_json(silent=True)) or {}
        sync_to_file = bool(data.get("sync_to_file", False))
        tag_updates = data.get("tags") if isinstance(data.get("tags"), dict) else {}

        # Quick-add shape: merge a single genre (case-insensitive, no overwrite).
        genre = str(data.get("genre") or "").strip()
        if genre:
            with db_session() as session:
                row = session.execute(
                    _text("SELECT genres, manual_genres FROM tracks WHERE id = :id"),
                    {"id": track_id},
                ).fetchone()
                if not row:
                    return jsonify({"error": "Track not found"}), 404

                def _merge(raw):
                    existing = [g.strip() for g in (raw or "").replace("\\", ",").split(",") if g.strip()]
                    if genre.lower() not in {x.lower() for x in existing}:
                        existing.append(genre)
                    return ", ".join(existing)

                merged_genres = _merge(row[0])
                merged_manual = _merge(row[1])
                session.execute(
                    _text("UPDATE tracks SET genres = :genres, manual_genres = :manual WHERE id = :id"),
                    {"genres": merged_genres, "manual": merged_manual, "id": track_id},
                )
            tag_updates = {"genres": merged_genres, "manual_genres": merged_manual}

        if not tag_updates:
            return jsonify({"error": "No tags to update"}), 400

        try:
            ok = update_track_tags(track_id, tag_updates)
            if not ok:
                return jsonify({"error": "Failed to update tags in database"}), 500
        except Exception as exc:
            logger.error("[TAGS] Database update failed for %s: %s", track_id, exc)
            return jsonify({"error": f"Database error: {exc}"}), 500

        file_synced = False
        if sync_to_file:
            try:
                from services.metadata.tag_file_service import sync_track_tags_to_file
                file_synced = bool(sync_track_tags_to_file(track_id))
            except Exception as exc:
                logger.debug("[TAGS] File sync failed for %s: %s", track_id, exc)

        return jsonify({
            "success": True,
            "track_id": track_id,
            "file_synced": file_synced,
            "message": f"Updated {len(tag_updates)} field(s) for track {track_id}",
        })
    except Exception as exc:
        logger.error("[TAGS] Unexpected error updating track tags: %s", exc, exc_info=True)
        return jsonify({"error": f"Unexpected error: {exc}"}), 500


@misc_api_bp.route("/genres/apply", methods=["POST"])
async def api_apply_genres():
    """Apply genres to an artist's tracks (merge into genres + manual_genres).

    Called by the artist genre-management UI.  Merges the submitted genres
    into every track by the artist without overwriting existing values.
    """
    try:
        import threading
        payload = (await request.get_json(silent=True)) or {}
        artist_name = str(payload.get("artist_name") or "").strip()
        genres = payload.get("genres") or []
        if not artist_name:
            return jsonify({"error": "artist_name required"}), 400
        clean = [str(g).strip() for g in genres if str(g).strip()]
        if not clean:
            return jsonify({"error": "genres must be a non-empty list"}), 400

        def _merge_genres(raw: str | None) -> str:
            existing = [g.strip() for g in (raw or "").replace("\\", ",").split(",") if g.strip()]
            merged = existing[:]
            for g in clean:
                if g.lower() not in {x.lower() for x in merged}:
                    merged.append(g)
            return ", ".join(merged)

        affected = 0
        with db_session() as session:
            rows = session.execute(
                text("SELECT id, genres, manual_genres FROM tracks "
                     "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist"),
                {"artist": artist_name},
            ).fetchall() or []
            for row in rows:
                track_id, genres_raw, manual_raw = row[0], row[1] or "", row[2] or ""
                new_genres = _merge_genres(genres_raw)
                new_manual = _merge_genres(manual_raw)
                if new_genres == genres_raw and new_manual == manual_raw:
                    continue
                session.execute(
                    text("UPDATE tracks SET genres = :genres, manual_genres = :manual WHERE id = :id"),
                    {"genres": new_genres, "manual": new_manual, "id": track_id},
                )
                affected += 1

        if affected:
            try:
                from db.repositories.genres import log_genre_update
                log_genre_update(
                    artist_name=artist_name, track_id=None,
                    action_type="apply_to_artist", affected_count=affected,
                    change_summary=f"Applied genres to {affected} tracks: {', '.join(clean)}",
                )
            except Exception as exc:
                logger.debug("[GENRES] Audit log failed: %s", exc)

        def _trigger_scan():
            try:
                from helpers.config_helpers import get_config
                from api_clients.navidrome import NavidromeClient
                cfg = get_config() or {}
                users = cfg.get("navidrome_users") or []
                if not users and cfg.get("navidrome"):
                    users = [cfg["navidrome"]]
                for u in users:
                    if u.get("base_url") and u.get("user") and u.get("pass"):
                        NavidromeClient(u["base_url"], u["user"], u["pass"]).start_scan()
                        break
            except Exception as exc:
                logger.debug("[GENRES] Navidrome scan trigger failed: %s", exc)

        threading.Thread(target=_trigger_scan, daemon=True).start()
        return jsonify({"success": True, "affected_tracks": affected,
                        "message": f"Applied genres to {affected} track(s)"}), 200
    except Exception as exc:
        logger.error("[GENRES] Genre apply failed: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


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
        from services.infrastructure.api_rate_limiter import get_rate_limiter
        mb_client = MusicBrainzHttpClient()
        # Use the shared MusicBrainz rate budget so manual lookups don't
        # collide with scan traffic (raw sleep bypassed it).
        get_rate_limiter().throttle_musicbrainz()
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
        artist_name = str(data.get("artist") or data.get("artist_name") or "").strip()
        album_name = str(data.get("album") or data.get("album_name") or "").strip()
        title = str(data.get("title") or "").strip()
        if not name:
            return jsonify({"error": "name required"}), 400
        with db_session() as session:
            result = session.execute(
                text("""
                    INSERT INTO bookmarks (type, name, url, artist_name, album_name, title)
                    VALUES (:type, :name, :url, :artist_name, :album_name, :title)
                    RETURNING id
                """),
                {
                    "type": btype, "name": name, "url": url,
                    "artist_name": artist_name, "album_name": album_name, "title": title,
                },
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

# ML model files downloaded from the official Essentia model zoo — the same
# set the bundled Docker image ships with (mood + Discogs-400 genre models).
_ESSENTIA_MODEL_URLS = [
    "https://essentia.upf.edu/models/music-style-classification/discogs-effnet/discogs-effnet-bs64-1.pb",
    "https://essentia.upf.edu/models/classification-heads/genre_discogs400/genre_discogs400-discogs-effnet-1.pb",
    "https://essentia.upf.edu/models/classification-heads/genre_discogs400/genre_discogs400-discogs-effnet-1.json",
    "https://essentia.upf.edu/models/classification-heads/mtg_jamendo_moodtheme/mtg_jamendo_moodtheme-discogs-effnet-1.pb",
    "https://essentia.upf.edu/models/classification-heads/mtg_jamendo_moodtheme/mtg_jamendo_moodtheme-discogs-effnet-1.json",
]
_ESSENTIA_REPO_URL = "https://github.com/WB2024/Essentia-to-Metadata.git"
_ESSENTIA_SCRIPT_DEFAULT = "/opt/Essentia-to-Metadata/tag_music.py"
_ESSENTIA_MODELS_DEFAULT = "/opt/essentia_models"

_essentia_download_thread = None
_essentia_download_lock = threading.Lock()


def _essentia_progress_file() -> str:
    """Path of the Essentia download progress file (state dir)."""
    from helpers.config_helpers import get_state_directory
    return os.path.join(get_state_directory(), "essentia_download_progress.json")


def _write_essentia_progress(is_running: bool, **extra) -> None:
    """Persist the Essentia download progress payload (best-effort)."""
    try:
        payload = {
            "is_running": is_running,
            "scan_type": "essentia_download",
            "last_updated": datetime.now().isoformat(),
        }
        payload.update(extra)
        path = _essentia_progress_file()
        _dir = os.path.dirname(path) or "."
        os.makedirs(_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except Exception as exc:
        logger.debug("Failed writing essentia download progress: %s", exc)


def _do_essentia_download() -> None:
    """Clone/update the Essentia-to-Metadata script and download the ML models.

    Runs on a background thread.  Steps:
      1. git-clone (or git-pull) the WB2024/Essentia-to-Metadata repo into the
         directory derived from ``essentia.script_path`` (or the Docker default).
      2. Stream-download the 5 model files from essentia.upf.edu into
         ``essentia.models_dir`` (or /opt/essentia_models).

    Progress is written to ``essentia_download_progress.json`` so
    ``/api/essentia/download-status`` can serve it to the config page and the
    setup wizard.
    """
    try:
        cfg = get_config() or {}
        essentia_cfg = cfg.get("essentia", {}) if isinstance(cfg, dict) else {}
        script_path = str(essentia_cfg.get("script_path") or "").strip()
        models_dir_cfg = str(essentia_cfg.get("models_dir") or "").strip()

        clone_dir = os.path.dirname(script_path) if script_path else os.path.dirname(_ESSENTIA_SCRIPT_DEFAULT)
        target_models_dir = models_dir_cfg or _ESSENTIA_MODELS_DEFAULT
        total_files = len(_ESSENTIA_MODEL_URLS)

        # ── Step 1: clone or update the script repository ────────────────
        _write_essentia_progress(
            True, status="cloning_script",
            current_step=f"Cloning Essentia-to-Metadata to {clone_dir}…",
            files_done=0, files_total=total_files,
        )
        parent_dir = os.path.dirname(clone_dir)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        if os.path.isdir(os.path.join(clone_dir, ".git")):
            # Repo already present — pull latest changes.
            result = subprocess.run(
                ["git", "-C", clone_dir, "pull", "--ff-only"],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                logger.warning("Essentia git pull returned non-zero (non-fatal): %s", result.stderr.strip())
        else:
            result = subprocess.run(
                ["git", "clone", "--depth=1", _ESSENTIA_REPO_URL, clone_dir],
                capture_output=True, text=True, timeout=180,
            )
            if result.returncode != 0:
                raise RuntimeError(f"git clone failed: {result.stderr.strip()[:300]}")

        # ── Step 2: download the model files ─────────────────────────────
        os.makedirs(target_models_dir, exist_ok=True)
        for idx, url in enumerate(_ESSENTIA_MODEL_URLS):
            filename = os.path.basename(url)
            dest_path = os.path.join(target_models_dir, filename)
            _write_essentia_progress(
                True, status="downloading_models",
                current_step=f"Downloading {filename} ({idx + 1}/{total_files})…",
                files_done=idx, files_total=total_files,
            )
            if os.path.isfile(dest_path):
                logger.info("Essentia model already present, skipping: %s", dest_path)
                continue
            tmp_path = dest_path + ".tmp"
            try:
                with httpx.stream("GET", url, timeout=300, follow_redirects=True) as resp:
                    resp.raise_for_status()
                    with open(tmp_path, "wb") as fh:
                        for chunk in resp.iter_bytes(chunk_size=1 << 20):
                            if chunk:
                                fh.write(chunk)
                os.replace(tmp_path, dest_path)
            except Exception:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                raise

        _write_essentia_progress(
            False, status="complete", current_step="Download complete",
            files_done=total_files, files_total=total_files,
            models_dir=target_models_dir, script_dir=clone_dir,
        )
        from helpers.logging_config import log_unified
        log_unified(
            f"Essentia Scan - Models and script downloaded successfully"
            f" (models: {target_models_dir}, script: {clone_dir})"
        )
    except Exception as exc:
        logger.error("Essentia model download failed: %s", exc, exc_info=True)
        _write_essentia_progress(False, status="error", current_step=f"Error: {exc}", error=str(exc))


@misc_api_bp.route("/essentia/download-models", methods=["POST"])
def api_essentia_download_models():
    """Download the Essentia-to-Metadata script and ML model files (background).

    Starts a background thread that:
      1. git-clones (or git-pulls) https://github.com/WB2024/Essentia-to-Metadata
         into the directory derived from the configured ``script_path``.
      2. Downloads the 5 Essentia ML model files from essentia.upf.edu into
         the configured ``models_dir``.

    Returns JSON ``{"status": "started"}`` immediately, or 409
    ``{"status": "already_running"}`` if a download is already in progress.
    Poll ``/api/essentia/download-status`` for progress.
    """
    global _essentia_download_thread
    with _essentia_download_lock:
        if _essentia_download_thread is not None and _essentia_download_thread.is_alive():
            return jsonify({"status": "already_running", "message": "Download already in progress"}), 409
        _essentia_download_thread = threading.Thread(
            target=_do_essentia_download, daemon=True, name="essentia-download"
        )
        _essentia_download_thread.start()
    return jsonify({"status": "started", "message": "Essentia download started"}), 202


@misc_api_bp.route("/essentia/download-status")
def api_essentia_download_status():
    """Return download status for Essentia models/script.

    While a download runs, mirrors the background thread's progress file
    (``cloning_script`` / ``downloading_models`` / ``error``).  Once idle, an
    ``installed`` result is reported when the configured models directory
    contains model files (``.pb`` / ``.json``) so the UI can hide the
    download button; ``complete`` is reported only when the models dir is
    unexpectedly empty after a successful run.
    """
    progress = None
    try:
        with open(_essentia_progress_file(), "r", encoding="utf-8") as fh:
            progress = json.load(fh)
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.debug("Essentia progress read failed: %s", exc)

    if isinstance(progress, dict) and progress.get("is_running"):
        return jsonify(progress)
    if isinstance(progress, dict) and progress.get("status") == "error":
        return jsonify(progress), 500

    cfg = get_config()
    models_dir = (
        (cfg.get("essentia", {}) or {}).get("models_dir")
        or os.environ.get("ESSENTIA_MODELS_DIR")
        or _ESSENTIA_MODELS_DEFAULT
    )
    if os.path.isdir(models_dir):
        model_files = [f for f in os.listdir(models_dir) if f.endswith((".pb", ".json"))]
        if model_files:
            return jsonify({"status": "installed", "models_dir": models_dir, "file_count": len(model_files)}), 200

    if isinstance(progress, dict) and progress.get("status") == "complete":
        return jsonify(progress)

    return jsonify({"status": "idle", "models_dir": models_dir}), 200


# ===========================================================================
# DATABASE
# ===========================================================================
@misc_api_bp.route("/database/cleanup-duplicates", methods=["POST"])
def api_cleanup_duplicates():
    """Remove duplicate track rows that share the same file path.

    Runs the same dedupe used by scan sanitisation: tracks pointing at the
    same audio file are grouped, the row with an MBID / duration / freshest
    scan is kept, and the rest are deleted.  Accepts ``{artist: name}`` to
    scope to one artist (optional — defaults to the whole library).
    """
    try:
        payload = request.get_json(silent=True) or {}
        artist = str(payload.get("artist") or "").strip()

        from db.repositories.scan_repository import sanitize_artist_file_paths_and_duplicates

        if artist:
            summary = sanitize_artist_file_paths_and_duplicates(artist)
            return jsonify({
                "success": True,
                "artist": artist,
                "stats": summary,
                "message": f"Removed {summary['duplicates_removed']} duplicate track(s) for '{artist}' "
                           f"({summary['path_updates']} path(s) normalized)",
            })

        # Whole-library pass: one artist at a time.
        total = {"path_updates": 0, "duplicates_removed": 0}
        artists: list[str] = []
        with db_session() as session:
            rows = session.execute(
                text("SELECT DISTINCT COALESCE(NULLIF(album_artist, ''), artist) FROM tracks "
                     "WHERE file_path IS NOT NULL AND TRIM(file_path) != ''")
            ).fetchall() or []
            artists = [str(r[0]) for r in rows if r[0]]
        for artist_name in artists:
            try:
                s = sanitize_artist_file_paths_and_duplicates(artist_name)
                total["path_updates"] += int(s.get("path_updates") or 0)
                total["duplicates_removed"] += int(s.get("duplicates_removed") or 0)
            except Exception as exc:
                logger.debug("[CLEANUP] Skip %s: %s", artist_name, exc)
        return jsonify({
            "success": True,
            "stats": total,
            "message": f"Removed {total['duplicates_removed']} duplicate track(s) across {len(artists)} artist(s)",
        })
    except Exception as exc:
        logger.error("[CLEANUP] Duplicate cleanup failed: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500
