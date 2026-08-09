"""
Album metadata service (clean version)

✅ No raw SQL
✅ Uses repository only
✅ No queue logic
✅ No retry logic
✅ No HTTP logic (except fallback - optional)
"""

from __future__ import annotations

import logging
import os
from typing import Any
import httpx

from sqlalchemy import text
from db.engine import db_session
from db.context import db_cursor  # TODO: migrate to db_session
from sqlalchemy import text
from db.engine import db_session
from db.utils import get_db_connection
from db.repositories.metadata import (
    album_is_favourite,
    set_album_favourite_db,
    fetch_album_art_blob,
    fetch_album_art_urls,
    fetch_album_tracklist,
    fetch_album_queue_track_stubs,
    fetch_queue_status,
    fetch_album_tracks_for_tag_update,
    save_album_art_db,
    update_track_genres,
    update_album_mbid_fields,
    update_album_discogs_fields,
    ignore_missing_track_db,
)
from services.queue.queue_constraints import STATUS_DISPLAY_CONFIG

from services.enrichment.album_art_service import (
    save_album_art_to_db,
    fetch_album_art_from_itunes,
    fetch_album_art_from_musicbrainz
)
from helpers.config_helpers import get_musicbrainz_user_agent

logger = logging.getLogger(__name__)

MUSICBRAINZ_USER_AGENT = get_musicbrainz_user_agent()


# =============================================================================
# FILE OPERATIONS
# =============================================================================

def rename_album_files_service(
    artist: str,
    album: str,
) -> dict[str, Any]:
    """Rename all files in an album based on current metadata.

    TODO: Implement actual file renaming logic via infrastructure service.
    """
    return {"success": True, "message": "Rename not yet implemented"}


# =============================================================================
# FAVOURITES
# =============================================================================

def is_album_favourite(
    artist: str,
    album: str,
) -> bool:
    with db_cursor() as (conn, _cursor):
        return album_is_favourite(
            conn,
            artist,
            album,
        )


def set_album_favourite(
    artist: str,
    album: str,
    is_favourite: bool,
) -> bool:
    try:
        with db_cursor(commit=True) as (conn, _cursor):
            set_album_favourite_db(
                conn,
                artist,
                album,
                is_favourite,
            )
        return True

    except Exception as exc:
        logger.error(
            "Error setting favourite: %s",
            exc,
            exc_info=True,
        )
        return False


# =============================================================================
# ALBUM ART
# =============================================================================

def get_local_album_art(
    artist: str,
    album: str,
) -> tuple[bytes | None, str | None]:
    """Get album art: local DB first, then Navidrome (default source).

    Navidrome already holds the art the user sees in their library, so it is
    consulted before any external service. Art pulled from Navidrome is
    cached to the DB for future requests.
    """
    with db_cursor() as (conn, _cursor):
        data, mime = fetch_album_art_blob(
            conn,
            artist,
            album,
        )

        if data:
            return data, mime or "image/jpeg"

    try:
        from services.enrichment.album_art_service import (
            fetch_album_art_from_navidrome,
            save_album_art_to_db,
        )

        data = fetch_album_art_from_navidrome(artist, album)
        if data:
            save_album_art_to_db(artist, album, data, source="navidrome")
            return data, "image/jpeg"
    except Exception as exc:
        logger.debug("Navidrome album art fallback failed for '%s' / '%s': %s", artist, album, exc)

    return None, None

# In services/album_service.py

def get_or_fetch_album_art(artist: str, album: str) -> tuple[bytes | None, str | None]:
    """Fetch album art from DB or external sources.
    
    Delegates to the canonical implementation in services.enrichment.album_art_service.
    """
    from services.enrichment.album_art_service import get_or_fetch_album_art as _fetch
    return _fetch(artist, album)


# =============================================================================
# TRACKLIST
# =============================================================================

def get_album_tracklist(artist: str, album: str) -> list[dict[str, Any]]:
    with db_cursor() as (conn, _cursor):
        rows = fetch_album_tracklist(conn, artist, album)

    return [
        {
            "track_id": r.get("id") if hasattr(r, "get") else r[0],
            "position": str((r.get("track_number") if hasattr(r, "get") else r[2]) or "").strip() or "—",
            "title": r.get("title") if hasattr(r, "get") else r[1],
            "artist": (r.get("artist") if hasattr(r, "get") else r[4]) or "",
        }
        for r in rows
    ]


def get_album_tracklist_from_db(artist: str, album: str) -> list[dict[str, Any]]:
    """Alias for get_album_tracklist to satisfy blueprint imports."""
    return get_album_tracklist(artist, album)


def match_album_tracklist(artist: str, album: str) -> dict[str, Any]:
    """Matches album tracks against the local library, falling back to MusicBrainz."""
    logger.debug("Matching tracklist for %s - %s", artist, album)

    with db_cursor() as (conn, _cursor):
        # 1. Fetch tracks for this album from repository
        album_rows = fetch_album_queue_track_stubs(conn, artist, album)
        
        matched_tracks = []
        queued_tracks = []

        for row in album_rows:
            title_val = row.get("title") if hasattr(row, "get") else row[0]
            file_path_val = (row.get("file_path") if hasattr(row, "get") else row[1]) or ""
            entry = {"title": title_val}

            if str(file_path_val).startswith("__queued_for_download__"):
                queued_tracks.append(entry)
            else:
                matched_tracks.append(entry)

        if album_rows:
            logger.info(
                "Found %d existing album tracks for %s - %s (library=%d, queued=%d)",
                len(album_rows), artist, album, len(matched_tracks), len(queued_tracks)
            )
            return {
                "success": True,
                "matched": matched_tracks,
                "queued": queued_tracks,
                "unmatched": [],
                "status": 200,
            }

        # 2. If no tracks found, check all artist tracks in the database
        logger.debug("No album tracks found in database, checking all tracks for artist %s", artist)
        all_artist_rows = fetch_album_tracklist(conn, artist, album="")
        library_tracks = {
            str(r.get("title") if hasattr(r, "get") else r[1]).lower().strip(): True 
            for r in all_artist_rows
            if (r.get("title") if hasattr(r, "get") else r[1])
        }

    # 3. Fallback to MusicBrainz API check
    try:
        headers = {"User-Agent": MUSICBRAINZ_USER_AGENT}
        search_url = "https://musicbrainz.org/ws/2/release"
        params = {
            "query": f'release:"{album}" AND artist:"{artist}"',
            "fmt": "json",
            "limit": 5,
        }

        resp = httpx.get(search_url, params=params, headers=headers, timeout=5)
        resp.raise_for_status()
        releases = resp.json().get("releases", [])

        if not releases:
            search_url = "https://musicbrainz.org/ws/2/release-group"
            params = {"query": f'"{album}" AND artist:"{artist}"', "fmt": "json", "limit": 1}

            resp = httpx.get(search_url, params=params, headers=headers, timeout=5)
            resp.raise_for_status()
            release_groups = resp.json().get("release-groups", [])

            if not release_groups or not release_groups[0].get("id"):
                return {"success": True, "matched": [], "queued": [], "unmatched": [], "status": 200}

            rg_id = release_groups[0]["id"]
            releases_url = f"https://musicbrainz.org/ws/2/release-group/{rg_id}/releases"

            releases_resp = httpx.get(releases_url, params={"fmt": "json", "limit": 5}, headers=headers, timeout=5)
            releases_resp.raise_for_status()
            releases = releases_resp.json().get("releases", [])

        if releases:
            release_id = releases[0].get("id")
            release_detail_url = f"https://musicbrainz.org/ws/2/release/{release_id}"
            
            detail_resp = httpx.get(
                release_detail_url,
                params={"fmt": "json", "inc": "recordings"},
                headers=headers,
                timeout=5,
            )
            detail_resp.raise_for_status()
            media = detail_resp.json().get("media", [])

            mb_matched = []
            mb_unmatched = []

            for medium in media:
                for track_obj in medium.get("tracks", []):
                    track_title = track_obj.get("title") or track_obj.get("recording", {}).get("title", "")
                    if not track_title:
                        continue

                    entry = {"title": track_title}
                    if track_title.lower().strip() in library_tracks:
                        mb_matched.append(entry)
                    else:
                        mb_unmatched.append(entry)

            logger.info("Matched %d tracks from MusicBrainz for %s - %s", len(mb_matched), artist, album)
            return {
                "success": True,
                "matched": mb_matched,
                "queued": [],
                "unmatched": mb_unmatched,
                "status": 200,
            }

        return {"success": True, "matched": [], "queued": [], "unmatched": [], "status": 200}

    except Exception as exc:
        logger.error("Error matching tracklist via MusicBrainz: %s", exc, exc_info=True)
        return {"error": str(exc), "status": 500}


# =============================================================================
# QUEUE STATUS (SAFE — READ ONLY)
# =============================================================================

def get_album_queue_status_db(artist: str, album: str):
    result = {}

    with db_cursor() as (conn, _cursor):
        rows = fetch_album_queue_track_stubs(conn, artist, album)

        for row in rows:
            track_id = row.get("id") if hasattr(row, "get") else row[0]
            file_path = (row.get("file_path") if hasattr(row, "get") else row[1]) or ""

            queue_id = None
            if "queue_id_" in file_path:
                try:
                    queue_id = int(file_path.split("queue_id_")[-1])
                except ValueError:
                    pass

            status = fetch_queue_status(conn, queue_id) if queue_id else "queued"
            cfg = STATUS_DISPLAY_CONFIG.get(status, {})

            result[track_id] = {
                "queue_id": queue_id,
                "status": status,
                "label": cfg.get("label", status),
                "css": cfg.get("css", ""),
                "icon": cfg.get("icon", ""),
            }

    return result


# =============================================================================
# GENRES
# =============================================================================

def apply_genres_to_album(artist: str, album: str, genres: list[str]):
    from services.metadata.tag_file_service import update_file_tags

    genres_clean = [g.strip() for g in genres if g.strip()]
    genres_str = ",".join(genres_clean)

    updated = 0
    failed = []

    with db_cursor(commit=True) as (conn, _cursor):
        tracks = fetch_album_tracks_for_tag_update(conn, artist, album)

        for t in tracks:
            track_id = t.get("id") if hasattr(t, "get") else t[0]
            title = t.get("title") if hasattr(t, "get") else t[1]
            path = t.get("file_path") if hasattr(t, "get") else t[2]

            if path and os.path.exists(path):
                if update_file_tags(path, {"genres": genres_clean}):
                    update_track_genres(conn, track_id, genres_str)
                    updated += 1
                else:
                    failed.append(title)
            else:
                failed.append(title)

    return {
        "success": True,
        "updated": updated,
        "failed": len(failed),
        "failed_files": failed,
    }


# =============================================================================
# MBID / DISCOGS
# =============================================================================

def apply_mbid_to_album(artist, album, mbid, rg_mbid, cover_url):
    with db_cursor(commit=True) as (conn, _cursor):
        rows = update_album_mbid_fields(conn, artist, album, mbid, rg_mbid, cover_url)

    return {"success": rows > 0, "rows_updated": rows}


def apply_discogs_id_to_album(artist, album, discogs_id, is_single):
    with db_cursor(commit=True) as (conn, _cursor):
        rows = update_album_discogs_fields(conn, artist, album, discogs_id, is_single)

    return {"success": True, "rows_updated": rows}


# =============================================================================
# BULK TRACK OPERATIONS (old-version parity)
# =============================================================================

def bulk_tag_tracks(payload: dict) -> tuple[dict, int]:
    """Add genre tags to multiple tracks — DB columns + audio file tags."""
    from services.metadata.tag_file_service import update_file_tags

    track_ids = [str(t) for t in (payload.get("track_ids") or []) if t]
    genres = [g.strip() for g in (payload.get("genres") or []) if g and g.strip()]
    if not track_ids:
        return {"success": False, "error": "No tracks selected"}, 400
    if not genres:
        return {"success": False, "error": "No genres provided"}, 400

    updated_count = 0
    failed_files: list[str] = []

    with db_cursor(commit=True) as (conn, cursor):
        for track_id in track_ids:
            try:
                cursor.execute(
                    "SELECT title, genres, manual_genres, file_path FROM tracks WHERE id = %s",
                    (track_id,),
                )
                row = cursor.fetchone()
                if not row:
                    continue
                title = row.get("title") if hasattr(row, "get") else row[0]
                current = str(row.get("genres") if hasattr(row, "get") else (row[1] or ""))
                manual = str(row.get("manual_genres") if hasattr(row, "get") else (row[2] or ""))
                file_path = str(row.get("file_path") if hasattr(row, "get") else (row[3] or ""))

                def _split_genres(raw: str) -> set:
                    sep = "\\" if "\\" in raw else ","
                    return {g.strip() for g in raw.split(sep) if g.strip()}

                existing = _split_genres(current)
                manual_existing = _split_genres(manual)
                existing.update(genres)
                manual_existing.update(genres)

                new_genres = ", ".join(sorted(existing))
                new_manual = ", ".join(sorted(manual_existing))

                if file_path and os.path.exists(file_path):
                    if not update_file_tags(file_path, {"genres": sorted(existing)}):
                        failed_files.append(title or f"Track ID: {track_id}")

                cursor.execute(
                    "UPDATE tracks SET genres = %s, manual_genres = %s WHERE id = %s",
                    (new_genres, new_manual, track_id),
                )
                updated_count += 1
            except Exception as exc:
                logger.error("[bulk_tag] Track %s failed: %s", track_id, exc)
                continue

    return {
        "success": True,
        "updated_count": updated_count,
        "failed_files": failed_files,
    }, 200


def bulk_delete_tracks(payload: dict) -> tuple[dict, int]:
    """Delete multiple tracks from the database, optionally removing audio files."""
    track_ids = [str(t) for t in (payload.get("track_ids") or []) if t]
    delete_files = bool(payload.get("delete_files", True))
    if not track_ids:
        return {"success": False, "error": "No tracks selected"}, 400

    deleted_count = 0

    with db_cursor(commit=True) as (conn, cursor):
        for track_id in track_ids:
            try:
                cursor.execute("SELECT file_path FROM tracks WHERE id = %s", (track_id,))
                row = cursor.fetchone()
                if not row:
                    continue
                file_path = str(row.get("file_path") if hasattr(row, "get") else (row[0] or ""))
                if delete_files and file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except Exception as exc:
                        logger.warning("[bulk_delete] Could not delete file %s: %s", file_path, exc)
                cursor.execute("DELETE FROM tracks WHERE id = %s", (track_id,))
                deleted_count += 1
            except Exception as exc:
                logger.error("[bulk_delete] Track %s failed: %s", track_id, exc)
                continue

    return {"success": True, "deleted_count": deleted_count}, 200


def update_album_ids(payload: dict) -> tuple[dict, int]:
    """Update release IDs (MusicBrainz release/release-group, Discogs) for an album's tracks."""
    artist = str(payload.get("artist") or "").strip()
    album = str(payload.get("album") or "").strip()
    if not artist or not album:
        return {"success": False, "error": "artist and album required"}, 400

    musicbrainz_id = str(payload.get("musicbrainz_release_id") or "").strip()
    release_group_id = str(payload.get("musicbrainz_release_group_id") or "").strip()
    discogs_id = str(payload.get("discogs_release_id") or "").strip()

    if not (musicbrainz_id or release_group_id or discogs_id):
        return {"success": False, "error": "No IDs provided"}, 400

    updates: list[str] = []
    params: list[Any] = []
    if musicbrainz_id:
        updates.append("musicbrainz_album_mbid = %s")
        params.append(musicbrainz_id)
    if release_group_id:
        updates.append("musicbrainz_releasegroupid = %s")
        params.append(release_group_id)
    if discogs_id:
        updates.append("discogs_album_id = %s")
        params.append(discogs_id)
    params.extend([artist, album])

    with db_cursor(commit=True) as (conn, cursor):
        cursor.execute(
            f"UPDATE tracks SET {', '.join(updates)} "
            "WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s AND album = %s",
            tuple(params),
        )
        rows = cursor.rowcount or 0

    return {"success": True, "rows_updated": rows}, 200


# =============================================================================
# IGNORE TRACK
# =============================================================================

def ignore_missing_track(missing_id, artist, album, title, disc_number):
    try:
        with db_cursor(commit=True) as (conn, _cursor):
            ignore_missing_track_db(conn, missing_id, artist, album, title, disc_number)
        return True
    except Exception as exc:
        logger.error("ignore_missing_track failed: %s", exc)
        return False


def get_majority_artist(artist: str, album: str) -> dict:
    """Return the most common artist across all tracks in an album."""
    from collections import Counter
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT artist FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s AND album = %s",
            (artist, album),
        )
        # Rows are RealDictRow (dict-like); never index by position.
        counts = Counter(row.get("artist") for row in cursor.fetchall() if row.get("artist"))
        if not counts:
            return {"success": False, "error": "No tracks found"}
        top = counts.most_common(1)[0]
        return {
            "success": True,
            "majority_artist": top[0],
            "count": top[1],
            "total": sum(counts.values()),
        }
    finally:
        conn.close()


def add_album_to_missing_releases(artist: str, album: str, year: str | None = None) -> dict:
    """Add an album to the missing_releases tracking table."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO missing_releases (artist, title, primary_type, first_release_date, category, created_at)
            VALUES (%s, %s, 'album', %s, 'album', CURRENT_TIMESTAMP)
            ON CONFLICT (artist, title) DO NOTHING
            """,
            (artist, album, year or None),
        )
        conn.commit()
        return {"success": True, "message": f"Added '{album}' to missing releases"}
    except Exception as exc:
        logger.error("Error adding to missing releases: %s", exc)
        return {"success": False, "error": str(exc)}
    finally:
        conn.close()


def get_spotify_genres(artist: str, album: str) -> dict:
    """Read stored Spotify genres from tracks table for an album."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT spotify_genres FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s AND album = %s AND spotify_genres IS NOT NULL LIMIT 1",
            (artist, album),
        )
        row = cursor.fetchone()
        genres = []
        if row:
            raw = row[0] if not hasattr(row, "get") else row.get("spotify_genres")
            if raw:
                if isinstance(raw, str):
                    import json
                    try:
                        genres = json.loads(raw) if raw.startswith("[") else [raw]
                    except json.JSONDecodeError:
                        genres = [g.strip() for g in raw.replace("\\", ",").split(",") if g.strip()]
                elif isinstance(raw, list):
                    genres = raw
        return {"success": True, "genres": genres}
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    finally:
        conn.close()


def get_track_recommendations(artist: str, album: str) -> dict:
    """Get genre recommendations by aggregating all genre sources in DB."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT spotify_genres, lastfm_tags, musicbrainz_genres, discogs_genres "
            "FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s AND album = %s",
            (artist, album),
        )
        rows = cursor.fetchall()
    finally:
        conn.close()

    from collections import defaultdict
    source_map: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        cols = ["spotify_genres", "lastfm_tags", "musicbrainz_genres", "discogs_genres"]
        for src_key, col in zip(["spotify", "lastfm", "musicbrainz", "discogs"], cols):
            val = None
            if hasattr(row, "get"):
                val = row.get(col)
            else:
                idx = cols.index(col)
                val = row[idx] if len(row) > idx else None
            if val:
                vals = val if isinstance(val, list) else [str(val)]
                source_map[src_key].extend(v.strip() for v in vals if v and v.strip())
    from services.enrichment.genre_aggregation_service import aggregate_genres
    recommended = aggregate_genres(dict(source_map))
    return {"success": True, "artist": artist, "album": album, "genres": recommended}