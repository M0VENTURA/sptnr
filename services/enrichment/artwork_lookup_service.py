"""Artwork lookup service.

Responsible for:
- selecting best artwork source
- applying fallback logic

Does NOT:
- download images
- store images
"""

from __future__ import annotations

from api_clients.applemusic import AppleMusicClient
from api_clients.audiodb import AudioDbClient
from api_clients.coverartarchive import get_release_image_from_caa


from helpers.config_helpers import get_audiodb_config


def get_best_album_artwork(
    *,
    artist: str,
    album: str,
    release_mbid: str = "",
    audiodb_key: str | None = None,
    apple_enabled: bool = True,
    audiodb_enabled: bool = True,
) -> str:
    """Return best album artwork URL using ordered fallback."""
    if audiodb_key is None:
        audiodb_key = get_audiodb_config().get("api_key", "195003")

    # 1. MusicBrainz Cover Art Archive (best source if we have MBID)
    if release_mbid:
        caa_url = get_release_image_from_caa(release_mbid)
        if caa_url:
            return caa_url

    # 2. Apple Music
    apple = AppleMusicClient(enabled=apple_enabled)
    apple_url = apple.get_album_artwork(album, artist)
    if apple_url:
        return apple_url

    # 3. AudioDB fallback
    audiodb = AudioDbClient(api_key=audiodb_key, enabled=audiodb_enabled)
    audiodb_url = audiodb.get_album_artwork(artist, album)

    return audiodb_url or ""


def get_best_artist_artwork(
    *,
    artist: str,
    audiodb_key: str | None = None,
    apple_enabled: bool = True,
    audiodb_enabled: bool = True,
) -> str:
    """Return best artist artwork URL using ordered fallback."""
    if audiodb_key is None:
        audiodb_key = get_audiodb_config().get("api_key", "195003")

    apple = AppleMusicClient(enabled=apple_enabled)
    apple_url = apple.get_artist_artwork(artist)
    if apple_url:
        return apple_url

    audiodb = AudioDbClient(api_key=audiodb_key, enabled=audiodb_enabled)
    audiodb_url = audiodb.get_artist_fanart(artist)

    return audiodb_url or ""