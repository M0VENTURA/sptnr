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
        bio = str(row[0] or "").strip() if row else ""
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
            "SELECT COUNT(*) FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s AND COALESCE(is_single, FALSE) = TRUE",
            (artist,),
        )
        row = cursor.fetchone()
        count = int(row[0] or 0) if row else 0
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
        # Wrap column reference to handle missing columns gracefully
        try:
            cursor.execute("SELECT image_url FROM artists WHERE name = %s", (artist,))
            row = cursor.fetchone()
            url = str(row[0] or "").strip() if row else ""
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
    return {"success": False, "error": "Not yet implemented"}, 501


def lookup_ids(payload: dict) -> tuple[dict, int]:
    return {"success": False, "error": "Not yet implemented"}, 501


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
            lf_raw = str(row[0] or "") if len(row) > 0 else ""
            lb_raw = str(row[1] or "") if len(row) > 1 else ""
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
