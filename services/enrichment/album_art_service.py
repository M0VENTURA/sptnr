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
from db.utils import row_get
from db.repositories.metadata import fetch_album_art_blob
from helpers.normalization_service import (
    normalize_artist,
    normalize_album,
)



logger = logging.getLogger(__name__)

# Negative-result cache for the Navidrome art lookup: albums genuinely
# missing from Navidrome shouldn't re-run a search3 call on every art
# request (an artist page can load dozens of covers at once).
_navidrome_art_miss_cache: dict[tuple[str, str], float] = {}
_NAVIDROME_MISS_TTL_SECONDS = 6 * 3600

# --- Existing Core Functions ---

def save_album_art_to_db(artist_name: str, album_name: str, image_data: bytes, source: str = "unknown", mime_type: str = "image/jpeg") -> bool:
    if not image_data:
        return False
    try:
        with db_session() as session:
            session.execute(
                text("""
                    INSERT INTO album_art
                    (artist_name, album_name, image_data, image_mime_type, source, downloaded_at)
                    VALUES (:artist_name, :album_name, :image_data, :mime_type, :source, CURRENT_TIMESTAMP)
                    ON CONFLICT (artist_name, album_name)
                    DO UPDATE SET
                        image_data = EXCLUDED.image_data,
                        image_mime_type = EXCLUDED.image_mime_type,
                        source = EXCLUDED.source,
                        downloaded_at = EXCLUDED.downloaded_at
                """),
                {
                    "artist_name": artist_name,
                    "album_name": album_name,
                    "image_data": image_data,
                    "mime_type": mime_type,
                    "source": source,
                },
            )
        return True
    except Exception as exc:
        logger.debug("Failed to save album art to database: %s", exc)
        return False

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

def fetch_album_art_from_navidrome(artist_name: str, album_name: str) -> bytes | None:
    """Pull album art straight from Navidrome (Subsonic ``getCoverArt``).

    Navidrome already holds the art the user sees in their library, so it is
    the preferred default source: external services are only consulted when
    Navidrome returns no art.

    Args:
        artist_name: Artist name.
        album_name: Album name.

    Returns:
        Raw image bytes, or None if not found / not configured.
    """
    try:
        from helpers.config_helpers import get_config
        from api_clients.navidrome import NavidromeClient

        cfg = get_config()
        users = cfg.get("navidrome_users") or []
        if not users:
            return None
        first = users[0]
        base_url = str(first.get("base_url") or "").rstrip("/")
        username = str(first.get("user") or "")
        password = str(first.get("pass") or "")
        if not all([base_url, username, password]):
            return None

        import time as _time

        cache_key = (normalize_artist(artist_name), normalize_album(album_name))
        last_miss = _navidrome_art_miss_cache.get(cache_key)
        if last_miss and (_time.time() - last_miss) < _NAVIDROME_MISS_TTL_SECONDS:
            return None

        client = NavidromeClient(
            base_url=base_url,
            username=username,
            password=password,
        )

        wanted_artist = normalize_artist(artist_name)
        wanted_album = normalize_album(album_name)

        # Find the album id via search3 (auth lives in the query string).
        album_id = None
        try:
            result = client.search(
                f"{artist_name} {album_name}",
                artist_count=0,
                album_count=10,
                song_count=0,
            )
            for candidate in result.get("albums") or []:
                candidate_artist = normalize_artist(candidate.get("artist") or "")
                candidate_album = normalize_album(
                    candidate.get("name") or candidate.get("album") or ""
                )
                if (
                    candidate_artist == wanted_artist
                    and candidate_album == wanted_album
                ):
                    album_id = candidate.get("id")
                    break
        except Exception as exc:
            logger.debug("Failed to locate album in Navidrome for %s — %s: %s", artist_name, album_name, exc)

        if not album_id:
            _navidrome_art_miss_cache[cache_key] = _time.time()
            return None

        data = client.get_cover_art_bytes(album_id, size=600)
        if data:
            logger.debug("Fetched album art from Navidrome for %s — %s", artist_name, album_name)
            return data
        _navidrome_art_miss_cache[cache_key] = _time.time()
        return None
    except Exception as exc:
        logger.debug("Failed to fetch album art from Navidrome: %s", exc)
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
    rows = []
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT id, file_path FROM tracks
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist_name AND album = :album_name
                """),
                {"artist_name": artist_name, "album_name": album_name},
            )
            rows = result.fetchall() or []
    except Exception as exc:
        logger.debug("Failed to query tracks for album-art apply: %s", exc)
        rows = []

    # Lazy import to break circular dependency:
    # album_art_service → tag_file_service → metadata.__init__ → album_service → album_art_service
    from services.metadata.tag_file_service import write_tags_to_file

    updated = 0
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
    """Orchestrates DB retrieval, API fetching, and DB caching.

    Order: local DB → Navidrome (default source, the art the user already
    sees in their library) → MusicBrainz → Discogs → AudioDB. External
    lookups only run when Navidrome has no art.
    """
    # 1. DB (the repository opens its own session)
    data, mime = fetch_album_art_blob(artist=artist, album=album)
    if data:
        return data, mime

    # 2. Navidrome (default — no external calls needed)
    data = fetch_album_art_from_navidrome(artist, album)
    if data:
        save_album_art_to_db(artist, album, data, source="navidrome")
        return data, "image/jpeg"

    # 3. MusicBrainz
    data = fetch_album_art_from_musicbrainz(artist, album)
    if data:
        save_album_art_to_db(artist, album, data, source="musicbrainz")
        return data, "image/jpeg"

    # 4. Discogs
    data = fetch_album_art_from_discogs(artist, album, token=discogs_token)
    if data:
        save_album_art_to_db(artist, album, data, source="discogs")
        return data, "image/jpeg"

    # 5. AudioDB fallback
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
    """Search for album art from the specified external source.

    Returns ``{"images": [{"url": <data-url>, "title": ..., "artist": ..., "source": ...}]}``
    so the front-end modal can render thumbnails immediately (no second
    download round-trip needed).

    ``applemusic`` is accepted as an alias for ``itunes``.
    """
    import base64 as _b64

    sources = {
        "musicbrainz": ("MusicBrainz", lambda: fetch_album_art_from_musicbrainz(artist, album)),
        "discogs": ("Discogs", lambda: fetch_album_art_from_discogs(artist, album, token="")),
        "applemusic": ("Apple Music", lambda: fetch_album_art_from_itunes(artist, album)),
        "itunes": ("Apple Music", lambda: fetch_album_art_from_itunes(artist, album)),
        "audiodb": ("AudioDB", lambda: fetch_album_art_from_audiodb(artist, album)),
    }
    entry = sources.get(source)
    if not entry:
        return {"success": False, "error": f"Unknown source: {source}"}, 400

    label, fn = entry
    data = fn()
    if not data:
        return (
            {
                "success": False,
                "error": "No album art found",
                "images": [],
            },
            404,
        )

    data_url = f"data:image/jpeg;base64,{_b64.b64encode(data).decode('ascii')}"
    return (
        {
            "success": True,
            "images": [
                {
                    "url": data_url,
                    "title": album,
                    "artist": artist,
                    "source": label,
                }
            ],
        },
        200,
    )


def set_album_art_from_url(artist: str, album: str, image_url: str) -> dict:
    """Download image from URL and save to database.

    Also accepts ``data:image/...;base64,...`` URLs (used by the album-art
    search modal, which renders matched art as inline data URLs).
    """
    try:
        if str(image_url or "").startswith("data:"):
            header, _, b64_payload = image_url.partition(",")
            mime_type = header[5:].split(";")[0] or "image/jpeg"
            import base64 as _b64
            try:
                image_data = _b64.b64decode(b64_payload)
            except Exception:
                return {"success": False, "error": "Invalid data URL payload"}, 400
        else:
            resp = httpx.get(image_url, timeout=10)
            if resp.status_code != 200:
                return {"success": False, "error": "Failed to download image"}
            image_data = resp.content
            mime_type = resp.headers.get("content-type", "image/jpeg")

        saved = save_album_art_to_db(artist, album, image_data, source="url", mime_type=mime_type)
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
    "fetch_album_art_from_navidrome",
    "get_or_fetch_album_art",
    "download_and_save_album_art",
    "get_album_art_placeholder_svg",
]