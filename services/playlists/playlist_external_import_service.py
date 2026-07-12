"""External playlist import service.

Handles importing playlists from external streaming sources into the
local download queue. Currently supports Spotify.

Key Functions:
    - extract_spotify_playlist_id(): Parse a Spotify playlist URL or URI
      to extract the playlist ID.
    - get_spotify_access_token(): Obtain an anonymous Spotify access token
      for API calls (no user authentication required).
    - fetch_spotify_playlist(): Retrieve full playlist track listing from
      Spotify using the Web API.

Architecture:
    Uses anonymous Spotify tokens (no OAuth flow) for basic playlist
    reading. Designed for extension to support additional sources.
"""

import logging
import requests
from urllib.parse import urlparse


def extract_spotify_playlist_id(url: str):
    if "open.spotify.com/playlist/" in url:
        return url.split("open.spotify.com/playlist/")[1].split("?")[0].split("/")[0]
    elif url.startswith("spotify:playlist:"):
        return url.split("spotify:playlist:")[1].split("?")[0]
    return None


def get_spotify_access_token():
    try:
        resp = requests.get(
            "https://open.spotify.com/get_access_token",
            params={"reason": "transport", "productType": "web_player"},
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
            },
            timeout=(5, 10),
        )
        if resp.status_code == 200:
            return resp.json().get("accessToken")
    except Exception:
        pass
    return None


def fetch_spotify_playlist(playlist_id: str):
    token = get_spotify_access_token()

    if not token:
        raise Exception("Could not get Spotify token")

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # Playlist metadata
    name = playlist_id
    try:
        meta = requests.get(
            f"https://api.spotify.com/v1/playlists/{playlist_id}",
            headers=headers,
            params={"fields": "name"},
            timeout=(5, 15),
        )
        if meta.status_code == 200:
            name = meta.json().get("name") or name
    except Exception:
        pass

    # Tracks
    tracks = []
    next_url = f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks"
    params = {"limit": 100}

    while next_url:
        resp = requests.get(next_url, headers=headers, params=params, timeout=(5, 30))
        resp.raise_for_status()

        data = resp.json()

        for item in data.get("items", []):
            track = item.get("track") or {}

            if track.get("id"):
                artist = ", ".join(a.get("name", "") for a in track.get("artists", []))

                tracks.append({
                    "track_number": len(tracks) + 1,
                    "artist": artist,
                    "title": track.get("name", ""),
                    "album": (track.get("album") or {}).get("name", ""),
                    "duration_ms": track.get("duration_ms") or 0,
                })

        next_url = data.get("next")
        params = None

    # Album artist detection
    artists = [t["artist"] for t in tracks if t["artist"]]
    primary = set(a.split(",")[0].strip() for a in artists)

    album_artist = list(primary)[0] if len(primary) == 1 else "Various Artists"
    is_compilation = album_artist == "Various Artists"

    return {
        "playlist_name": name,
        "tracks": tracks,
        "album_artist": album_artist,
        "is_compilation": is_compilation,
        "service": "spotify"
    }


def import_playlist_from_url(url: str):
    spotify_id = extract_spotify_playlist_id(url)

    if spotify_id:
        return fetch_spotify_playlist(spotify_id)

    parsed = urlparse(url)
    if parsed.hostname and "music.apple.com" in parsed.hostname:
        raise Exception("Apple Music not supported yet")

    raise Exception("Unsupported playlist URL")
