"""Cover Art Archive client."""

from __future__ import annotations

import logging

from api_clients import session
from api_clients.musicbrainz_http import USER_AGENT

logger = logging.getLogger(__name__)

CAA_ARTIST_URL = "https://coverartarchive.org/artist/{mbid}"
CAA_RELEASE_URL = "https://coverartarchive.org/release/{mbid}"
CAA_RELEASE_GROUP_URL = "https://coverartarchive.org/release-group/{mbid}/front-{size}"
CAA_RELEASE_FRONT_URL = "https://coverartarchive.org/release/{mbid}/front-{size}"


def _get_front_image(url_template: str, mbid: str) -> str:
    """Fetch front image URL, or empty string when unavailable."""
    if not mbid:
        return ""
    try:
        response = session.get(
            url_template.format(mbid=mbid),
            params={"type": "front"},
            headers={"User-Agent": USER_AGENT},
        )
        return str(response.url) if response.status_code == 200 else ""
    except Exception as exc:
        logger.debug("Failed to fetch CAA front image for %s: %s", mbid, exc)
        return ""


def get_artist_image_from_caa(mbid: str) -> str:
    return _get_front_image(CAA_ARTIST_URL, mbid)


def get_release_image_from_caa(mbid: str) -> str:
    return _get_front_image(CAA_RELEASE_URL, mbid)


def get_release_group_front_image_bytes(release_group_mbid: str, size: str = "500") -> bytes | None:
    """Fetch binary image bytes for a release group from Cover Art Archive."""
    if not release_group_mbid:
        return None
    try:
        url = CAA_RELEASE_GROUP_URL.format(mbid=release_group_mbid, size=size)
        # Using the shared session to maintain connection pooling benefits
        response = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=10.0)
        return response.content if response.status_code == 200 else None
    except Exception as exc:
        logger.debug("CAA release-group binary image fetch failed for %s: %s", release_group_mbid, exc)
        return None


def get_release_front_image_bytes(release_mbid: str, size: str = "500") -> bytes | None:
    """Fetch binary image bytes for a release from Cover Art Archive."""
    if not release_mbid:
        return None
    try:
        url = CAA_RELEASE_FRONT_URL.format(mbid=release_mbid, size=size)
        response = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=10.0)
        return response.content if response.status_code == 200 else None
    except Exception as exc:
        logger.debug("CAA release binary image fetch failed for %s: %s", release_mbid, exc)
        return None
