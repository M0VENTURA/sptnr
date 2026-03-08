"""
Cover Art Archive client for fetching artist and album artwork.

Documentation: https://coverartarchive.org/
"""

import logging
from . import session

logger = logging.getLogger(__name__)

# Cover Art Archive base URLs
CAA_ARTIST_URL = "https://coverartarchive.org/artist/{mbid}"
CAA_RELEASE_URL = "https://coverartarchive.org/release/{mbid}"


def get_artist_image_from_caa(mbid: str) -> str:
    """
    Fetch artist image URL from Cover Art Archive.
    
    Args:
        mbid: MusicBrainz artist ID
        
    Returns:
        URL to artist front image, or empty string if not found
    """
    if not mbid:
        return ""
    
    params_front = {"type": "front"}
    
    try:
        # Try to get the front image
        res = session.get(
            CAA_ARTIST_URL.format(mbid=mbid),
            params=params_front,
            timeout=(5, 10),
            allow_redirects=True
        )
        
        if res.status_code == 200:
            # CAA returns a redirect to the actual image
            return res.url
        else:
            logger.debug(f"No front image found on CAA for artist {mbid}")
            return ""
    except Exception as e:
        logger.debug(f"Failed to fetch artist image from CAA for '{mbid}': {e}")
        return ""


def get_release_image_from_caa(mbid: str) -> str:
    """
    Fetch release/album image URL from Cover Art Archive.
    
    Args:
        mbid: MusicBrainz release ID
        
    Returns:
        URL to release front image, or empty string if not found
    """
    if not mbid:
        return ""
    
    params_front = {"type": "front"}
    
    try:
        # Try to get the front image
        res = session.get(
            CAA_RELEASE_URL.format(mbid=mbid),
            params=params_front,
            timeout=(5, 10),
            allow_redirects=True
        )
        
        if res.status_code == 200:
            # CAA returns a redirect to the actual image
            return res.url
        else:
            logger.debug(f"No front image found on CAA for release {mbid}")
            return ""
    except Exception as e:
        logger.debug(f"Failed to fetch release image from CAA for '{mbid}': {e}")
        return ""
