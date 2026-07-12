"""Artist scan service.

Responsible for external release comparison and scan orchestration.
DB persistence is delegated to repositories; network calls should eventually move
to api_clients/musicbrainz*.py if not already there.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import requests

from sqlalchemy import text
from db.engine import db_session
from db.utils import get_db_connection
from db.repositories.metadata import (
    fetch_artist_albums,
    fetch_artist_mbid,
    fetch_all_distinct_artists,
)
from helpers.normalization_service import normalize_title_for_lookup
from helpers.config_helpers import get_musicbrainz_user_agent

logger = logging.getLogger(__name__)
MUSICBRAINZ_USER_AGENT = get_musicbrainz_user_agent()


def _normalize_release_title(title: str) -> str:
    return normalize_title_for_lookup(title or "")


def _fetch_musicbrainz_releases(artist: str, artist_mbid: str | None = None) -> list[dict[str, Any]]:
    """Compatibility network helper.

    Prefer replacing this with your api_clients.musicbrainz client later.
    """
    headers = {"User-Agent": MUSICBRAINZ_USER_AGENT}
    if artist_mbid:
        url = f"https://musicbrainz.org/ws/2/release-group"
        params = {"fmt": "json", "artist": artist_mbid, "type": "album|ep|single"}
    else:
        url = "https://musicbrainz.org/ws/2/release-group"
        params = {"fmt": "json", "query": f'artist:"{artist}"', "type": "album|ep|single"}
    response = requests.get(url, headers=headers, params=params, timeout=10)
    response.raise_for_status()
    return response.json().get("release-groups", []) or []


def get_missing_releases(artist: str):
    conn = get_db_connection()
    try:
        existing_albums = fetch_artist_albums(conn, artist)
        artist_mbid = fetch_artist_mbid(conn, artist)
    finally:
        conn.close()

    if not artist_mbid:
        return {"artist": artist, "missing": [], "existing_albums": existing_albums}, 200

    mb_releases = _fetch_musicbrainz_releases(artist, artist_mbid=artist_mbid)
    existing_norm = {_normalize_release_title(a) for a in existing_albums}

    missing = []
    for release in mb_releases:
        title = release.get("title", "")
        if not title:
            continue
        if _normalize_release_title(title) in existing_norm:
            continue
        missing.append({"title": title, "id": release.get("id"), "cover_art_url": release.get("cover_art_url")})

    return {"artist": artist, "missing": missing, "existing_albums": existing_albums}, 200


def start_missing_release_scan():
    def run():
        try:
            conn = get_db_connection()
            artists = fetch_all_distinct_artists(conn)
            conn.close()
            for artist in artists:
                try:
                    _fetch_musicbrainz_releases(artist)
                except Exception as exc:
                    logger.warning("Missing release scan failed for %s: %s", artist, exc)
        except Exception as exc:
            logger.error("Missing release scan failed: %s", exc, exc_info=True)

    threading.Thread(target=run, daemon=True, name="missing-release-scan").start()
    return {"success": True}, 200


def import_release(artist: str, release_id: str, title: str):
    """Import a MusicBrainz release as placeholder track records.

    This keeps the old public function but delegates DB save to existing save_to_db
    as a compatibility bridge.
    """
    headers = {"User-Agent": MUSICBRAINZ_USER_AGENT}
    response = requests.get(
        f"https://musicbrainz.org/ws/2/release/{release_id}",
        params={"fmt": "json", "inc": "recordings"},
        headers=headers,
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    media = data.get("media", [])
    if not media:
        return {"error": "No media found"}, 400

    try:
        from db.repositories.popularity_repository import save_to_db
    except Exception:
        from db.repositories.popularity_repository import save_to_db  # compatibility fallback

    count = 0
    for disc in media:
        for i, track in enumerate(disc.get("tracks", []), start=1):
            recording = track.get("recording", {})
            save_to_db({"title": recording.get("title"), "artist": artist, "album": title, "track_number": i})
            count += 1
    return {"success": True, "tracks_imported": count}, 200


def scan_all_missing_releases() -> tuple[dict, int]:
    """Scan all artists for missing releases in the background."""
    start_missing_release_scan()
    return {"success": True, "message": "Missing releases scan started"}, 200


def add_artist(artist: str) -> tuple[dict, int]:
    """Add an artist to the database by creating a placeholder record."""
    if not artist:
        return {"success": False, "error": "artist required"}, 400
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO artists (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
            (artist,),
        )
        conn.commit()
        return {"success": True, "message": f"Artist '{artist}' added"}, 200
    except Exception as exc:
        return {"success": False, "error": str(exc)}, 500
    finally:
        conn.close()
