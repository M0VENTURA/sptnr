"""Album art enrichment service.

Replaces helper-style album_art_manager responsibilities with a service that
composes API clients/enrichment and repository/file-tag layers without raw HTTP leakage.
"""

from __future__ import annotations

import logging
import os

import httpx

from quart import Response

from api_clients.coverartarchive import get_release_group_front_image_bytes
from api_clients.discogs_http import DiscogsHttpClient
from api_clients.musicbrainz_http import MusicBrainzHttpClient
from sqlalchemy import text
from db.engine import db_session
from db.utils import get_db_connection, row_get
from db.repositories.metadata import fetch_album_art_blob
from helpers.normalization_service import (
    normalize_artist,
    normalize_album,
)



logger = logging.getLogger(__name__)

# --- Existing Core Functions ---

def save_album_art_to_db(artist_name: str, album_name: str, image_data: bytes, source: str = "unknown", mime_type: str = "image/jpeg") -> bool:
    if not image_data:
        return False
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO album_art
            (artist_name, album_name, image_data, image_mime_type, source, downloaded_at)
            VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (artist_name, album_name)
            DO UPDATE SET
                image_data = EXCLUDED.image_data,
                image_mime_type = EXCLUDED.image_mime_type,
                source = EXCLUDED.source,
                downloaded_at = EXCLUDED.downloaded_at
            """,
            (artist_name, album_name, image_data, mime_type, source),
        )
        conn.commit()
        return True
    except Exception as exc:
        logger.debug("Failed to save album art to database: %s", exc)
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            conn.close()

def fetch_album_art_from_musicbrainz(artist_name: str, album_name: str) -> bytes | None:
    """Fetch cover art cleanly via MusicBrainz release-group and Cover Art Archive client."""
    try:
        musicbrainz = MusicBrainzHttpClient()
        release_groups = musicbrainz.search_release_groups(f'release:"{album_name}" AND artist:"{artist_name}"', limit=1)
        if not release_groups:
            return None
        release_group_mbid = release_groups[0].get("id")
        if not release_group_mbid:
            return None
        return get_release_group_front_image_bytes(release_group_mbid)
    except Exception as exc:
        logger.debug("Failed to fetch album art from MusicBrainz/CAA: %s", exc)
        return None
def fetch_album_art_from_itunes(
    artist_name: str,
    album_name: str,
) -> bytes | None:
    """
    Fetch album art from the iTunes / Apple Music API.

    Strategy:
        1. Search iTunes for album
        2. Normalize artist/album names for comparison
        3. Prefer exact normalized match
        4. Fall back to first result
        5. Download highest resolution artwork available
    """

    if not artist_name or not album_name:
        return None

    try:
        headers = {
            "User-Agent": "Popularr/1.0"
        }

        search_url = (
            "https://itunes.apple.com/search"
        )

        params = {
            "term": f"{artist_name} {album_name}",
            "entity": "album",
            "limit": 5,
        }

        response = httpx.get(
            search_url,
            params=params,
            headers=headers,
            timeout=5,
        )

        response.raise_for_status()

        data = response.json()

        results = (
            data.get("results")
            or []
        )

        if not results:
            logger.debug(
                "iTunes: No results for %s - %s",
                artist_name,
                album_name,
            )
            return None

        wanted_artist = normalize_artist(
            artist_name
        )

        wanted_album = normalize_album(
            album_name
        )

        # ---------------------------------------------------------
        # Preferred match
        # ---------------------------------------------------------

        for result in results:

            result_artist = normalize_artist(
                result.get(
                    "artistName",
                    "",
                )
            )

            result_album = normalize_album(
                result.get(
                    "collectionName",
                    "",
                )
            )

            if (
                result_artist == wanted_artist
                and result_album == wanted_album
            ):

                artwork_url = (
                    result.get(
                        "artworkUrl100",
                        "",
                    )
                )

                if not artwork_url:
                    continue

                artwork_url = artwork_url.replace(
                    "100x100",
                    "1000x1000",
                )

                logger.info(
                    "iTunes: Found exact match for %s - %s",
                    artist_name,
                    album_name,
                )

                art_response = httpx.get(
                    artwork_url,
                    headers=headers,
                    timeout=5,
                )

                if art_response.status_code == 200:

                    logger.info(
                        "iTunes: Downloaded cover art for %s - %s",
                        artist_name,
                        album_name,
                    )

                    return art_response.content

        # ---------------------------------------------------------
        # Fallback to first result
        # ---------------------------------------------------------

        artwork_url = (
            results[0].get(
                "artworkUrl100",
                "",
            )
        )

        if artwork_url:

            artwork_url = artwork_url.replace(
                "100x100",
                "1000x1000",
            )

            logger.debug(
                "iTunes: Using fallback result for %s - %s",
                artist_name,
                album_name,
            )

            art_response = httpx.get(
                artwork_url,
                headers=headers,
                timeout=5,
            )

            if art_response.status_code == 200:

                logger.info(
                    "iTunes: Downloaded fallback cover art for %s - %s",
                    artist_name,
                    album_name,
                )

                return art_response.content

    except Exception as exc:
        logger.debug(
            "Failed to fetch album art from iTunes: %s",
            exc,
        )

    return None

def fetch_album_art_from_discogs(artist_name: str, album_name: str, token: str) -> bytes | None:
    """Fetch cover art cleanly using the low-level DiscogsHttpClient."""
    if not token:
        return None
    try:
        # 1. Use the HTTP client directly for raw endpoints
        discogs = DiscogsHttpClient(token=token)
        
        # 2. Use the search_database method from the HTTP client
        # This returns a list of results as per your discogs_http.py
        results = discogs.search_database({"q": f"{artist_name} {album_name}", "type": "release", "per_page": 1})
        
        if not results or not results[0].get("cover_url"):
            return None
        
        # 3. Use the session directly to fetch the image bytes
        # The http client's session is pre-configured with the correct User-Agent/Auth
        img_url = results[0]["cover_url"]
        resp = discogs.session.get(img_url, timeout=5)
        
        if resp.status_code == 200:
            return resp.content
            
    except Exception as exc:
        logger.debug("Failed to fetch album art from Discogs: %s", exc)
    return None

def fetch_album_art_from_audiodb(artist_name: str, album_name: str) -> bytes | None:
    """Fetch album art from TheAudioDB as an additional fallback source.

    AudioDB has good coverage for popular artists and fast response times.
    Rate limit: 30 requests per minute (no API key required for read-only).

    Args:
        artist_name: Artist name.
        album_name: Album name.

    Returns:
        Raw image bytes, or None if not found.
    """
    try:
        url = "https://theaudiodb.com/api/v1/json/2/searchalbum.php"
        resp = httpx.get(url, params={"s": artist_name, "a": album_name}, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        albums = data.get("album", [])
        if not albums:
            return None
        art_url = albums[0].get("strAlbumThumb") or albums[0].get("strAlbumCDart")
        if not art_url:
            return None
        img_resp = httpx.get(art_url, timeout=10)
        if img_resp.status_code == 200:
            logger.debug("Fetched album art from AudioDB for %s — %s", artist_name, album_name)
            return img_resp.content
        return None
    except Exception as exc:
        logger.debug("Failed to fetch album art from AudioDB: %s", exc)
        return None


def apply_album_art_to_tracks(artist_name: str, album_name: str, image_data: bytes, mime_type: str = "image/jpeg") -> int:
    if not image_data:
        return 0
    conn = None
    updated = 0
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, file_path FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s AND album = %s
            """,
            (artist_name, album_name),
        )
        rows = cursor.fetchall() or []
    except Exception as exc:
        logger.debug("Failed to query tracks for album-art apply: %s", exc)
        rows = []
    finally:
        if conn:
            conn.close()

    # Lazy import to break circular dependency:
    # album_art_service → tag_file_service → metadata.__init__ → album_service → album_art_service
    from services.metadata.tag_file_service import write_tags_to_file

    for row in rows:
        file_path = str(row_get(row, "file_path", 1) or "").strip()
        if not file_path or not os.path.exists(file_path):
            continue
        if write_tags_to_file(file_path, {"cover_art_data": image_data, "cover_art_mime": mime_type}):
            updated += 1
    return updated

# --- New Orchestration & Utility ---

def get_album_art_placeholder_svg(size: int = 300) -> Response:
    """Generate an SVG placeholder."""
    try:
        size = int(size)
        size = max(10, min(1000, size))
    except (ValueError, TypeError):
        size = 300
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" role="img">
        <rect fill="#2a2a2a" width="{size}" height="{size}"/>
        <text x="50%" y="50%" text-anchor="middle" dy=".3em" fill="#666" font-size="16">No Album Art</text>
    </svg>'''
    return Response(svg, mimetype='image/svg+xml')

def get_or_fetch_album_art(artist: str, album: str, discogs_token: str = "") -> tuple[bytes | None, str | None]:
    """Orchestrates DB retrieval, API fetching, and DB caching."""
    # 1. DB
    conn = get_db_connection()
    try:
        data, mime = fetch_album_art_blob(conn, artist, album)
        if data:
            return data, mime
    finally:
        conn.close()

    # 2. MusicBrainz
    data = fetch_album_art_from_musicbrainz(artist, album)
    if data:
        save_album_art_to_db(artist, album, data, source="musicbrainz")
        return data, "image/jpeg"

    # 3. Discogs
    data = fetch_album_art_from_discogs(artist, album, token=discogs_token)
    if data:
        save_album_art_to_db(artist, album, data, source="discogs")
        return data, "image/jpeg"

    # AudioDB fallback (inserted before Discogs)
    data = fetch_album_art_from_audiodb(artist, album)
    if data:
        save_album_art_to_db(artist, album, data, source="audiodb")
        return data, "image/jpeg"

    return None, None


def download_and_save_album_art(artist: str, album: str, image_data: bytes, source: str = "unknown") -> bool:
    """Download and save album art to the database and embed into local files.

    Args:
        artist: Artist name.
        album: Album name.
        image_data: Raw image bytes.
        source: Source label for the DB record.

    Returns:
        True if at least one track was updated with the art.
    """
    if not image_data:
        return False
    save_album_art_to_db(artist, album, image_data, source=source)
    count = apply_album_art_to_tracks(artist, album, image_data)
    return count > 0


def search_album_art_external(artist: str, album: str, source: str = "musicbrainz") -> tuple[dict, int]:
    """Search for album art from the specified external source."""
    sources = {
        "musicbrainz": lambda: fetch_album_art_from_musicbrainz(artist, album),
        "discogs": lambda: fetch_album_art_from_discogs(artist, album, token=""),
        "itunes": lambda: fetch_album_art_from_itunes(artist, album),
        "audiodb": lambda: fetch_album_art_from_audiodb(artist, album),
    }
    fn = sources.get(source)
    if not fn:
        return {"success": False, "error": f"Unknown source: {source}"}, 400
    data = fn()
    if data:
        return {"success": True, "data": list(data), "source": source}, 200
    return {"success": False, "error": "No album art found"}, 404


def set_album_art_from_url(artist: str, album: str, image_url: str) -> dict:
    """Download image from URL and save to database."""
    try:
        resp = httpx.get(image_url, timeout=10)
        if resp.status_code != 200:
            return {"success": False, "error": "Failed to download image"}
        mime = resp.headers.get("content-type", "image/jpeg")
        saved = save_album_art_to_db(artist, album, resp.content, source="url", mime_type=mime)
        if saved:
            return {"success": True, "message": "Album art saved from URL"}
        return {"success": False, "error": "Failed to save album art"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def set_album_art_from_upload(artist: str, album: str, image_data: bytes, mime_type: str) -> dict:
    """Save uploaded image data to database."""
    saved = save_album_art_to_db(artist, album, image_data, source="upload", mime_type=mime_type)
    if saved:
        return {"success": True, "message": "Album art saved from upload"}
    return {"success": False, "error": "Failed to save album art"}


__all__ = [
    "fetch_album_art_blob",
    "save_album_art_to_db",
    "apply_album_art_to_tracks",
    "fetch_album_art_from_musicbrainz",
    "fetch_album_art_from_discogs",
    "fetch_album_art_from_audiodb",
    "get_or_fetch_album_art",
    "download_and_save_album_art",
    "get_album_art_placeholder_svg",
]