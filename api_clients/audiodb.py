#!/usr/bin/env python3
"""
The AudioDB API Client - Fetch artist artwork and metadata.

The AudioDB (https://www.theaudiodb.com) provides:
- Artist fanart and artwork
- Artist biography
- Album artwork
- Rate limited: 30 requests per minute (free tier)

Free API Key: 195003
"""

import requests
import logging
from typing import Optional, Dict, Any
from helpers import create_retry_session

logger = logging.getLogger(__name__)

# The AudioDB API endpoints
AUDIODB_API_BASE = "https://www.theaudiodb.com/api/v1/json"
DEFAULT_API_KEY = "195003"  # Free API key from The AudioDB


def get_artist_fanart(
    artist_name: str,
    api_key: str = DEFAULT_API_KEY,
    enabled: bool = True
) -> Optional[str]:
    """
    Fetch the best available artist fanart/image from The AudioDB.
    
    The AudioDB provides several image options:
    - strArtistFanart (large fanart)
    - strArtistBanner (banner image)
    - strArtistLogo (logo image)
    - strArtistThumb (thumbnail)
    
    We prioritize fanart > banner > logo > thumb
    
    Args:
        artist_name: Name of the artist
        api_key: The AudioDB API key (default: free key)
        enabled: Whether The AudioDB integration is enabled
        
    Returns:
        URL to artist fanart/image, or None if not found or disabled
    """
    if not enabled or not api_key:
        return None
    
    try:
        # The AudioDB search endpoint
        url = f"{AUDIODB_API_BASE}/{api_key}/search.php"
        params = {"s": artist_name}
        
        logger.debug(f"[AUDIODB] Fetching artist data for: {artist_name}")
        
        session = create_retry_session(user_agent="sptnr/1.0", retries=3, backoff=1.0)
        response = session.get(url, params=params, timeout=5)
        response.raise_for_status()
        
        data = response.json()
        artists = data.get("artists")
        
        if not artists or len(artists) == 0:
            logger.debug(f"[AUDIODB] No artist found for: {artist_name}")
            return None
        
        # Get the first (best) match
        artist = artists[0]
        
        # Priority order: fanart > banner > logo > thumb
        fanart_url = (
            artist.get("strArtistFanart") or
            artist.get("strArtistBanner") or
            artist.get("strArtistLogo") or
            artist.get("strArtistThumb")
        )
        
        if fanart_url:
            logger.info(f"[AUDIODB] Found artist fanart for {artist_name}: {fanart_url[:80]}...")
            return fanart_url
        else:
            logger.debug(f"[AUDIODB] No artwork found for artist: {artist_name}")
            return None
            
    except requests.exceptions.Timeout:
        logger.debug(f"[AUDIODB] Timeout fetching artist data for: {artist_name}")
        return None
    except requests.exceptions.RequestException as e:
        logger.debug(f"[AUDIODB] Error fetching artist data for {artist_name}: {e}")
        return None
    except Exception as e:
        logger.debug(f"[AUDIODB] Unexpected error fetching artist fanart: {e}")
        return None


def get_artist_biography(
    artist_name: str,
    api_key: str = DEFAULT_API_KEY,
    enabled: bool = True
) -> Optional[str]:
    """
    Fetch artist biography from The AudioDB.
    
    Args:
        artist_name: Name of the artist
        api_key: The AudioDB API key
        enabled: Whether The AudioDB integration is enabled
        
    Returns:
        Artist biography text, or None if not found or disabled
    """
    if not enabled or not api_key:
        return None
    
    try:
        url = f"{AUDIODB_API_BASE}/{api_key}/search.php"
        params = {"s": artist_name}
        
        logger.debug(f"[AUDIODB] Fetching artist biography for: {artist_name}")
        
        session = create_retry_session(user_agent="sptnr/1.0", retries=3, backoff=1.0)
        response = session.get(url, params=params, timeout=5)
        response.raise_for_status()
        
        data = response.json()
        artists = data.get("artists")
        
        if not artists or len(artists) == 0:
            logger.debug(f"[AUDIODB] No artist found for: {artist_name}")
            return None
        
        artist = artists[0]
        biography = artist.get("strBiographyEN") or artist.get("strBiography")
        
        if biography:
            logger.info(f"[AUDIODB] Found artist biography for {artist_name} ({len(biography)} chars)")
            return biography
        else:
            logger.debug(f"[AUDIODB] No biography found for artist: {artist_name}")
            return None
            
    except Exception as e:
        logger.debug(f"[AUDIODB] Error fetching artist biography: {e}")
        return None


def get_album_artwork(
    artist_name: str,
    album_name: str,
    api_key: str = DEFAULT_API_KEY,
    enabled: bool = True
) -> Optional[str]:
    """
    Fetch album artwork from The AudioDB.
    
    Args:
        artist_name: Name of the artist
        album_name: Name of the album
        api_key: The AudioDB API key
        enabled: Whether The AudioDB integration is enabled
        
    Returns:
        URL to album artwork, or None if not found or disabled
    """
    if not enabled or not api_key:
        return None
    
    try:
        # The AudioDB album search endpoint
        url = f"{AUDIODB_API_BASE}/{api_key}/searchalbum.php"
        params = {"s": artist_name, "a": album_name}
        
        logger.debug(f"[AUDIODB] Fetching album artwork for: {artist_name} - {album_name}")
        
        session = create_retry_session(user_agent="sptnr/1.0", retries=3, backoff=1.0)
        response = session.get(url, params=params, timeout=5)
        response.raise_for_status()
        
        data = response.json()
        albums = data.get("album")
        
        if not albums or len(albums) == 0:
            logger.debug(f"[AUDIODB] No album found for: {artist_name} - {album_name}")
            return None
        
        # Get the first (best) match
        album = albums[0]
        artwork_url = album.get("strAlbumThumb") or album.get("strAlbumCDart")
        
        if artwork_url:
            logger.info(f"[AUDIODB] Found album artwork for {artist_name} - {album_name}: {artwork_url[:80]}...")
            return artwork_url
        else:
            logger.debug(f"[AUDIODB] No artwork found for album: {artist_name} - {album_name}")
            return None
            
    except Exception as e:
        logger.debug(f"[AUDIODB] Error fetching album artwork: {e}")
        return None
