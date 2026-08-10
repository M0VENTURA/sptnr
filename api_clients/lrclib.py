"""LRCLIB lyrics API client (https://lrclib.net).

Open-source, no API key required.  ``GET /api/get`` returns both plain and
synchronised (LRC) lyrics for a track when the query matches.

Usage::

    from api_clients.lrclib import fetch_lyrics
    result = fetch_lyrics("Uncontrolled", "Future Palace", "Distortion", 242)
    # -> {"plain": "...", "synced": "...", "source": "lrclib", "track_name": ...}
"""

from __future__ import annotations

import logging
from typing import Any

from api_clients.http_utils import create_retry_client

logger = logging.getLogger(__name__)

LRCLIB_API = "https://lrclib.net/api/get"


def fetch_lyrics(
    track_name: str,
    artist_name: str,
    album_name: str | None = None,
    duration: float | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Fetch plain + synced lyrics from LRCLIB for a track.

    Returns ``{}`` when the query does not match any track (LRCLIB returns
    404 for unknown tracks) or the network call fails.  The response is
    normalised to ``{"plain", "synced", "source", "track_name", "artist_name",
    "album_name", "duration"}``.
    """
    params: dict[str, str] = {
        "track_name": str(track_name or "").strip(),
        "artist_name": str(artist_name or "").strip(),
    }
    if album_name:
        params["album_name"] = str(album_name).strip()
    if duration:
        params["duration"] = str(int(duration or 0))
    try:
        client = create_retry_client(timeout=timeout)
        resp = client.get(LRCLIB_API, params=params)
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.debug("[LRCLIB] Request failed for %s - %s: %s", artist_name, track_name, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        "plain": str(data.get("plainLyrics") or "").strip(),
        "synced": str(data.get("syncedLyrics") or "").strip(),
        "source": "lrclib",
        "track_name": str(data.get("trackName") or track_name),
        "artist_name": str(data.get("artistName") or artist_name),
        "album_name": str(data.get("albumName") or album_name or ""),
        "duration": data.get("duration"),
    }
