"""Artist metadata service — consolidated from the old monolithic app.py.

This module owns artist-level metadata routes that don't fit neatly into
the existing correction/scan services. Functions are module-level so routes
can ``import metadata_service as metadata``.

TODO: Many functions are still stubs — port the full implementations from the
old app.py when those features are needed.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from sqlalchemy import text
from db.engine import db_session
from db.utils import get_db_connection
from services.enrichment.artist_bio_service import get_artist_biography

logger = logging.getLogger(__name__)


def cleanup_false_positive_missing(artist: str) -> tuple[dict, int]:
    """Remove false-positive missing releases for an artist."""
    if not artist:
        return {"success": False, "error": "artist required"}, 400
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM missing_releases WHERE LOWER(artist) = LOWER(%s) AND title IN "
            "(SELECT DISTINCT album FROM tracks WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(%s))",
            (artist, artist),
        )
        conn.commit()
        removed = cursor.rowcount or 0
        return {"success": True, "removed_count": removed, "artist": artist}, 200
    except Exception as exc:
        return {"success": False, "error": str(exc)}, 500
    finally:
        conn.close()


def get_artist_bio(artist: str) -> tuple[dict, int]:
    """Get artist biography from DB cache or Wikidata fallback."""
    if not artist:
        return {"success": False, "error": "name required"}, 400
    try:
        conn = get_db_connection()
    except Exception as exc:
        return {"success": False, "error": f"DB connection failed: {exc}"}, 500
    bio = ""
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT bio FROM artists WHERE name = %s", (artist,))
        row = cursor.fetchone()
        # Rows are RealDictRow (dict-like); never index by position.
        bio = str(row.get("bio") or "").strip() if row else ""
    except Exception:
        bio = ""
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if bio:
        return {"success": True, "bio": bio, "source": "database"}, 200
    try:
        bio = get_artist_biography(artist) or ""
    except Exception:
        bio = ""
    return {"success": True, "bio": bio, "source": "wikidata" if bio else "none"}, 200


def get_singles_count(artist: str) -> tuple[dict, int]:
    """Get count of singles for an artist."""
    if not artist:
        return {"success": False, "error": "name required"}, 400
    try:
        conn = get_db_connection()
    except Exception as exc:
        return {"success": False, "error": f"DB connection failed: {exc}"}, 500
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s "
            "AND COALESCE(is_single, FALSE) = TRUE "
            "AND LOWER(COALESCE(single_confidence, '')) = 'high'",
            (artist,),
        )
        row = cursor.fetchone()
        # Rows are RealDictRow (dict-like); never index by position.
        count = int(row.get("count") or 0) if row else 0
        return {"success": True, "count": count}, 200
    except Exception as exc:
        return {"success": False, "error": str(exc)}, 500
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_covered_by(artist: str) -> tuple[dict, int]:
    """Get covers of this artist's songs in the library."""
    if not artist:
        return {"success": False, "error": "artist required"}, 400
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, title, artist, album FROM tracks "
            "WHERE original_cover_artist ILIKE %s AND LOWER(COALESCE(album_artist, artist)) != LOWER(%s)",
            (artist, artist),
        )
        rows = cursor.fetchall()
        covers = []
        for r in rows:
            covers.append({
                "id": r[0] if not hasattr(r, "get") else r.get("id"),
                "title": r[1] if not hasattr(r, "get") else r.get("title"),
                "artist": r[2] if not hasattr(r, "get") else r.get("artist"),
                "album": r[3] if not hasattr(r, "get") else r.get("album"),
            })
        return {"success": True, "covers": covers, "total": len(covers)}, 200
    finally:
        conn.close()


def artist_favourite(request) -> tuple[dict, int]:
    """Check/add/remove artist favourite via bookmarks table."""
    from quart import jsonify
    artist = (request.args.get("artist") or (request.json or {}).get("artist") or "").strip()
    if not artist:
        return {"success": False, "error": "artist required"}, 400
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        if request.method == "GET":
            cursor.execute(
                "SELECT 1 FROM bookmarks WHERE type = 'artist_favourite' AND LOWER(name) = LOWER(%s) LIMIT 1",
                (artist,),
            )
            return {"success": True, "is_favourite": cursor.fetchone() is not None}, 200
        elif request.method == "POST":
            cursor.execute(
                "INSERT INTO bookmarks (type, name) VALUES ('artist_favourite', %s) ON CONFLICT DO NOTHING",
                (artist,),
            )
            conn.commit()
            return {"success": True, "is_favourite": True}, 200
        elif request.method == "DELETE":
            cursor.execute(
                "DELETE FROM bookmarks WHERE type = 'artist_favourite' AND LOWER(name) = LOWER(%s)", (artist,),
            )
            conn.commit()
            return {"success": True, "is_favourite": False}, 200
        return {"success": False, "error": "Unsupported method"}, 405
    finally:
        conn.close()


def get_artist_image(artist: str):
    """Get artist image URL from database."""
    if not artist:
        return {"success": False, "error": "name required"}, 400
    try:
        conn = get_db_connection()
    except Exception:
        return {"success": False, "error": "DB connection failed", "image_url": ""}, 200
    try:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT image_url FROM artists WHERE name = %s", (artist,))
            row = cursor.fetchone()
            # Rows are RealDictRow (dict-like); never index by position.
            url = str(row.get("image_url") or "").strip() if row else ""
        except Exception:
            url = ""
        if url and url.startswith(("http://", "https://")):
            return {"success": True, "image_url": url}, 200
        return {"success": False, "error": "No image", "image_url": ""}, 200
    except Exception as exc:
        return {"success": False, "error": str(exc), "image_url": ""}, 200
    finally:
        try:
            conn.close()
        except Exception:
            pass


def search_images(artist: str, source: str) -> tuple[dict, int]:
    """Search for artist images from external sources."""
    return {"success": False, "error": "Not yet implemented", "images": []}, 501


def set_image(payload: dict) -> tuple[dict, int]:
    """Set custom artist image URL."""
    artist = (payload.get("artist") or "").strip()
    url = (payload.get("image_url") or "").strip()
    if not artist or not url:
        return {"success": False, "error": "artist and image_url required"}, 400
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO artists (name, image_url) VALUES (%s, %s) "
            "ON CONFLICT (name) DO UPDATE SET image_url = EXCLUDED.image_url",
            (artist, url),
        )
        conn.commit()
        return {"success": True}, 200
    finally:
        conn.close()


def update_ids(payload: dict) -> tuple[dict, int]:
    """Update artist ID columns (MusicBrainz, Last.fm, Discogs) on all of an artist's tracks."""
    artist = str(payload.get("artist") or "").strip()
    if not artist:
        return {"success": False, "error": "Missing artist name"}, 400

    updates: list[str] = []
    params: list[str] = []
    for column, key in (
        ("musicbrainz_artistid", "musicbrainz_artist_id"),
        ("lastfm_artist_mbid", "lastfm_artist_mbid"),
        ("discogs_artist_id", "discogs_artist_id"),
        ("spotify_artist_id", "spotify_artist_id"),
    ):
        value = str(payload.get(key) or "").strip()
        if value:
            updates.append(f"{column} = %s")
            params.append(value)

    if not updates:
        return {"success": False, "error": "No IDs provided"}, 400

    params.append(artist)
    try:
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE tracks SET {', '.join(updates)} "
                "WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s",
                tuple(params),
            )
            rows_updated = cursor.rowcount or 0
            conn.commit()
        finally:
            conn.close()
        return {
            "success": True,
            "message": f"Artist IDs updated for {artist}",
            "rows_updated": rows_updated,
            "updated": {
                "musicbrainz_artist_id": str(payload.get("musicbrainz_artist_id") or "").strip() or None,
                "lastfm_artist_mbid": str(payload.get("lastfm_artist_mbid") or "").strip() or None,
                "discogs_artist_id": str(payload.get("discogs_artist_id") or "").strip() or None,
            },
        }, 200
    except Exception as exc:
        logger.error("update_ids failed for %s: %s", artist, exc, exc_info=True)
        return {"success": False, "error": str(exc)}, 500


def lookup_ids(payload: dict) -> tuple[dict, int]:
    """Look up MusicBrainz/Discogs artist IDs and persist them for the artist."""
    artist = str(payload.get("artist") or "").strip()
    if not artist:
        return {"success": False, "error": "Missing artist name"}, 400

    import difflib

    musicbrainz_id = ""
    discogs_id = ""

    # MusicBrainz lookup (rate-limited client with the proper User-Agent).
    try:
        from api_clients.musicbrainz_http import MusicBrainzHttpClient, escape_lucene_special_chars
        mb = MusicBrainzHttpClient()
        results = mb.search_artists(f'artist:"{escape_lucene_special_chars(artist)}"', limit=5)
        if results:
            best = max(
                results,
                key=lambda item: difflib.SequenceMatcher(
                    None, artist.lower(), str(item.get("name") or "").lower()
                ).ratio(),
            )
            musicbrainz_id = str(best.get("id") or "").strip()
    except Exception as exc:
        logger.debug("MusicBrainz artist lookup failed for %s: %s", artist, exc)

    # Discogs lookup (only when a token is configured).
    try:
        from helpers.config_helpers import get_config
        from api_clients.discogs_http import DiscogsHttpClient
        cfg = get_config() or {}
        token = str((cfg.get("api_integrations", {}).get("discogs", {}) or {}).get("token") or "").strip()
        if token:
            dc = DiscogsHttpClient(token=token)
            results = dc.search_database({"q": artist, "type": "artist", "per_page": 5})
            if results:
                best = max(
                    results,
                    key=lambda item: difflib.SequenceMatcher(
                        None, artist.lower(), str(item.get("title") or "").lower()
                    ).ratio(),
                )
                discogs_id = str(best.get("id") or "").strip()
    except Exception as exc:
        logger.debug("Discogs artist lookup failed for %s: %s", artist, exc)

    if not musicbrainz_id and not discogs_id:
        return {"success": False, "error": "No IDs found from external lookup"}, 404

    updates: list[str] = []
    params: list[str] = []
    if musicbrainz_id:
        updates.extend(["musicbrainz_artistid = %s", "lastfm_artist_mbid = %s"])
        params.extend([musicbrainz_id, musicbrainz_id])
    if discogs_id:
        updates.append("discogs_artist_id = %s")
        params.append(discogs_id)
    params.append(artist)

    try:
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE tracks SET {', '.join(updates)} "
                "WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s",
                tuple(params),
            )
            rows_updated = cursor.rowcount or 0
            conn.commit()
        finally:
            conn.close()
        return {
            "success": True,
            "artist": artist,
            "musicbrainz_artist_id": musicbrainz_id or None,
            "discogs_artist_id": discogs_id or None,
            "rows_updated": rows_updated,
        }, 200
    except Exception as exc:
        logger.error("lookup_ids failed for %s: %s", artist, exc, exc_info=True)
        return {"success": False, "error": str(exc)}, 500


def get_similar_artists(artist: str, args) -> tuple[dict, int]:
    """Get similar artists from DB cache (Last.fm / ListenBrainz)."""
    if not artist:
        return {"success": False, "error": "artist required"}, 400
    sources = {"lastfm": [], "listenbrainz": []}
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT similar_artists_lastfm, similar_artists_listenbrainz, similar_artists_last_updated "
            "FROM artists WHERE name = %s",
            (artist,),
        )
        row = cursor.fetchone()
        if row:
            import json
            # Rows are RealDictRow (dict-like); never index by position.
            lf_raw = str(row.get("similar_artists_lastfm") or "") if row else ""
            lb_raw = str(row.get("similar_artists_listenbrainz") or "") if row else ""
            if lf_raw:
                try:
                    sources["lastfm"] = json.loads(lf_raw) if isinstance(json.loads(lf_raw), list) else []
                except Exception:
                    pass
            if lb_raw:
                try:
                    sources["listenbrainz"] = json.loads(lb_raw) if isinstance(json.loads(lb_raw), list) else []
                except Exception:
                    pass
        return {"success": True, "similar_artists": sources}, 200
    except Exception as exc:
        return {"success": False, "error": str(exc)}, 500
    finally:
        conn.close()


def get_compilations(artist: str) -> tuple[dict, int]:
    """Get compilation appearances for an artist."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT album, album_artist, COUNT(*) as count FROM tracks "
            "WHERE LOWER(artist) = LOWER(%s) AND LOWER(COALESCE(NULLIF(album_artist, ''), artist)) != LOWER(%s) "
            "GROUP BY album, album_artist ORDER BY album",
            (artist, artist),
        )
        rows = cursor.fetchall()
        compilations = []
        for r in rows:
            compilations.append({
                "album": r[0] if not hasattr(r, "get") else r.get("album"),
                "album_artist": r[1] if not hasattr(r, "get") else r.get("album_artist"),
                "track_count": int(r[2] if not hasattr(r, "get") else r.get("count", 0)),
            })
        return {"success": True, "compilations": compilations}, 200
    finally:
        conn.close()


def get_main_tracks(artist: str) -> tuple[dict, int]:
    """Get main tracks for an artist."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, title, album, stars FROM tracks "
            "WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s ORDER BY stars DESC NULLS LAST LIMIT 50",
            (artist,),
        )
        rows = cursor.fetchall()
        tracks = []
        for r in rows:
            tracks.append({
                "id": r[0] if not hasattr(r, "get") else r.get("id"),
                "title": r[1] if not hasattr(r, "get") else r.get("title"),
                "album": r[2] if not hasattr(r, "get") else r.get("album"),
                "stars": float(r[3] or 0) if not hasattr(r, "get") else float(r.get("stars", 0) or 0),
            })
        return {"success": True, "tracks": tracks}, 200
    finally:
        conn.close()


def get_artist_members_cached(artist: str) -> list[dict]:
    """Fetch artist members from DB cache, or MusicBrainz API if stale/missing."""
    import json
    from datetime import datetime, timezone, timedelta
    from api_clients.musicbrainz_http import MusicBrainzHttpClient

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT members, members_last_updated FROM artists WHERE name = %s",
            (artist,),
        )
        row = cursor.fetchone()
        now = datetime.now(timezone.utc)

        if row:
            # Rows are RealDictRow (dict-like); never index by position.
            members_raw = str(row.get("members") or "") if row else ""
            updated_raw = str(row.get("members_last_updated") or "") if row else ""
            if members_raw and updated_raw:
                try:
                    updated = datetime.fromisoformat(updated_raw.replace("Z", "+00:00"))
                    if updated.tzinfo is None:
                        updated = updated.replace(tzinfo=timezone.utc)
                    # Cache is valid for 7 days
                    if (now - updated) < timedelta(days=7):
                        return json.loads(members_raw)
                except Exception:
                    pass

        # Cache miss — fetch from MusicBrainz
        # First resolve the artist MBID
        mb = MusicBrainzHttpClient()
        results = mb.search_artists(artist, limit=5)
        if not results:
            return []

        # Prefer groups/orchestras
        preferred = next(
            (a for a in results if (a.get("type") or "").lower() in {"group", "orchestra", "choir"}),
            results[0],
        )
        artist_mbid = preferred.get("id")
        if not artist_mbid:
            return []

        members = mb.get_artist_members(artist_mbid)
        members_json = json.dumps(members)

        # Cache in artists table
        cursor.execute(
            "INSERT INTO artists (id, name, members, members_last_updated) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (name) DO UPDATE SET members = EXCLUDED.members, members_last_updated = EXCLUDED.members_last_updated",
            (artist, artist, members_json, now.isoformat()),
        )
        conn.commit()
        return members
    except Exception:
        return []
    finally:
        conn.close()


def get_stats(artist: str) -> tuple[dict, int]:
    """Get statistics for an artist."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) as tracks, COUNT(DISTINCT album) as albums, "
            "AVG(stars) as avg_stars, SUM(CASE WHEN stars = 5 THEN 1 ELSE 0 END) as five_star "
            "FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s",
            (artist,),
        )
        row = cursor.fetchone()
        if not row:
            return {"success": False, "error": "Artist not found"}, 404
        stats = {
            "track_count": int(row[0] if not hasattr(row, "get") else row.get("tracks", 0)),
            "album_count": int(row[1] if not hasattr(row, "get") else row.get("albums", 0)),
            "avg_stars": float(row[2] or 0) if not hasattr(row, "get") else float(row.get("avg_stars", 0) or 0),
            "five_star_count": int(row[3] or 0) if not hasattr(row, "get") else int(row.get("five_star", 0) or 0),
        }
        return {"success": True, **stats}, 200
    finally:
        conn.close()


def apply_genres(payload: dict) -> tuple[dict, int]:
    """Apply genres to all tracks by an artist."""
    from services.metadata.tag_file_service import write_tags_to_file
    artist = (payload.get("artist") or "").strip()
    genres = payload.get("genres", [])
    if not artist or not genres:
        return {"success": False, "error": "artist and genres required"}, 400
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, file_path FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s",
            (artist,),
        )
        rows = cursor.fetchall()
        genre_str = " \\ ".join(genres) if isinstance(genres, list) else str(genres)
        updated = 0
        for r in rows:
            track_id = r[0] if not hasattr(r, "get") else r.get("id")
            fp = r[1] if not hasattr(r, "get") else r.get("file_path")
            cursor.execute("UPDATE tracks SET genres = %s WHERE id = %s", (genre_str, track_id))
            updated += 1
            if fp and fp.strip():
                try:
                    write_tags_to_file(str(fp), {"genre": genres})
                except Exception:
                    pass
        conn.commit()
        return {"success": True, "updated": updated}, 200
    finally:
        conn.close()


def genre_recommendations(artist: str) -> tuple[dict, int]:
    """Get genre recommendations for an artist from MusicBrainz."""
    if not artist:
        return {"success": False, "error": "artist required"}, 400
    try:
        from urllib.parse import quote
        headers = {"User-Agent": "Popularr/1.0", "Accept": "application/json"}
        resp = httpx.get(
            f"https://musicbrainz.org/ws/2/artist/?query=artist:{quote(artist)}&fmt=json&limit=1",
            headers=headers, timeout=10,
        )
        data = resp.json()
        artists = data.get("artists", [])
        if not artists:
            return {"success": True, "genres": []}, 200
        tags = artists[0].get("tags", [])
        genres = [{"name": t.get("name"), "count": t.get("count")} for t in tags]
        return {"success": True, "genres": genres}, 200
    except Exception as exc:
        return {"success": False, "error": str(exc)}, 500


def genre_management(payload: dict) -> tuple[dict, int]:
    """Handle genre management save operations."""
    return {"success": False, "error": "Not yet implemented"}, 501
