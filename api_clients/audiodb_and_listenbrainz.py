"""AudioDB and ListenBrainz API client module."""
import logging
import math
import json
from datetime import datetime
from . import session

logger = logging.getLogger(__name__)


class ListenBrainzUserClient:
    """
    ListenBrainz API wrapper for user-specific operations.
    Requires user authentication token for love/feedback operations.
    """
    
    def __init__(self, user_token: str, http_session=None):
        """
        Initialize ListenBrainz user client.
        
        Args:
            user_token: User's ListenBrainz API token
            http_session: Optional requests.Session (uses shared if not provided)
        """
        self.token = user_token
        self.session = http_session or session
        self.base_url = "https://api.listenbrainz.org/1"
        self.headers = {"Authorization": f"Token {user_token}"}
    
    def love_track(self, mbid: str) -> bool:
        """
        Mark a track as loved on ListenBrainz.
        
        Args:
            mbid: MusicBrainz recording ID
            
        Returns:
            True if successful
        """
        try:
            url = f"{self.base_url}/feedback/recording-feedback"
            payload = {
                "recording_mbid": mbid,
                "score": 1  # 1 = love
            }
            res = self.session.post(url, json=payload, headers=self.headers, timeout=(5, 10))  # (connect_timeout, read_timeout)
            res.raise_for_status()
            logger.info(f"Marked {mbid} as loved on ListenBrainz")
            return True
        except Exception as e:
            logger.error(f"Failed to love track {mbid} on ListenBrainz: {e}")
            return False
    
    def unlove_track(self, mbid: str) -> bool:
        """
        Remove love status from a track on ListenBrainz.
        
        Args:
            mbid: MusicBrainz recording ID
            
        Returns:
            True if successful
        """
        try:
            url = f"{self.base_url}/feedback/recording-feedback"
            payload = {
                "recording_mbid": mbid,
                "score": 0  # 0 = remove feedback
            }
            res = self.session.post(url, json=payload, headers=self.headers, timeout=(5, 10))  # (connect_timeout, read_timeout)
            res.raise_for_status()
            logger.info(f"Removed love from {mbid} on ListenBrainz")
            return True
        except Exception as e:
            logger.error(f"Failed to unlove track {mbid} on ListenBrainz: {e}")
            return False
    
    def get_loved_tracks(self, limit: int = 100, offset: int = 0) -> list:
        """
        Get tracks the user has loved on ListenBrainz.
        
        Args:
            limit: Number of results per page
            offset: Pagination offset
            
        Returns:
            List of dicts with 'recording_mbid' and 'score'
        """
        try:
            url = f"{self.base_url}/feedback/user/{{username}}/get-feedback"
            # Note: Need to get username first or use a different endpoint
            # For now, return empty list - this needs username from token validation
            logger.warning("get_loved_tracks not fully implemented - needs username")
            return []
        except Exception as e:
            logger.error(f"Failed to get loved tracks from ListenBrainz: {e}")
            return []
    
    def get_recording_tags(self, mbid: str) -> list:
        """
        Get genre tags for a recording from ListenBrainz.
        Does not require authentication.
        
        Args:
            mbid: MusicBrainz recording ID
            
        Returns:
            List of dicts with 'tag' and 'count'
        """
        try:
            url = f"{self.base_url}/metadata/recording/{mbid}/tags"
            res = self.session.get(url, timeout=(5, 10))  # (connect_timeout, read_timeout)
            res.raise_for_status()
            data = res.json()
            tags = data.get("tag", {}).get("recording", [])
            # Sort by count descending
            sorted_tags = sorted(tags, key=lambda x: x.get("count", 0), reverse=True)
            logger.debug(f"Got {len(sorted_tags)} tags for recording {mbid}")
            return sorted_tags
        except Exception as e:
            logger.debug(f"Failed to get tags for recording {mbid}: {e}")
            return []
    
    def get_artist_tags(self, mbid: str) -> list:
        """
        Get genre tags for an artist from ListenBrainz.
        
        Args:
            mbid: MusicBrainz artist ID
            
        Returns:
            List of dicts with 'tag' and 'count'
        """
        try:
            url = f"{self.base_url}/metadata/artist/{mbid}/tags"
            res = self.session.get(url, timeout=(5, 10))  # (connect_timeout, read_timeout)
            res.raise_for_status()
            data = res.json()
            tags = data.get("tag", {}).get("artist", [])
            sorted_tags = sorted(tags, key=lambda x: x.get("count", 0), reverse=True)
            logger.debug(f"Got {len(sorted_tags)} tags for artist {mbid}")
            return sorted_tags
        except Exception as e:
            logger.debug(f"Failed to get tags for artist {mbid}: {e}")
            return []


class ListenBrainzClient:
    """ListenBrainz API wrapper for listening stats."""
    
    def __init__(self, http_session=None, enabled: bool = True):
        """
        Initialize ListenBrainz client.
        
        Args:
            http_session: Optional requests.Session (uses shared if not provided)
            enabled: Whether ListenBrainz is enabled
        """
        self.session = http_session or session
        self.enabled = enabled
        self.base_url = "https://api.listenbrainz.org/1"
    
    def get_listen_count(self, mbid: str = "", artist: str = "", title: str = "") -> int:
        """
        Fetch ListenBrainz listen count using MBID or fallback search.
        
        Args:
            mbid: MusicBrainz recording ID (preferred)
            artist: Artist name (for fallback)
            title: Track title (for fallback)
            
        Returns:
            Listen count
        """
        if not self.enabled:
            return 0
        
        # If no MBID provided, try to get one from MusicBrainz
        if not mbid and artist and title:
            try:
                from api_clients.musicbrainz import get_suggested_mbid
                mbid, confidence = get_suggested_mbid(title, artist, limit=1)
                if mbid and confidence >= 0.75:
                    logger.debug(f"Got MBID from MusicBrainz for '{title}': {mbid} (confidence: {confidence})")
                else:
                    if confidence < 0.75:
                        logger.debug(f"MusicBrainz confidence too low for '{title}': {confidence} (need >= 0.75)")
                    mbid = ""
            except Exception as e:
                logger.debug(f"MusicBrainz MBID lookup failed for '{title}': {e}")
        
        # Primary: stats by MBID (if available)
        if mbid:
            try:
                url = f"{self.base_url}/stats/recording/{mbid}"
                res = self.session.get(url, timeout=(5, 10))  # (connect_timeout, read_timeout)
                res.raise_for_status()
                data = res.json()
                payload = data.get("payload", {})
                count = int(payload.get("total_listen_count", 0))
                if count > 0:
                    logger.debug(f"ListenBrainz count for '{title}' (MBID {mbid}): {count}")
                    return count
                else:
                    logger.debug(f"ListenBrainz no listens found for MBID {mbid} ('{title}')")
                    return 0
            except Exception as e:
                logger.debug(f"ListenBrainz MBID lookup failed for {mbid} ('{title}'): {e}")
        
        # Fallback: Without a reliable search endpoint, return 0
        # The /1/recording/search endpoint is not available or deprecated
        # ListenBrainz primarily works with MBIDs, not artist/title search
        if not mbid:
            logger.debug(f"ListenBrainz: No MBID available for '{artist} - {title}'")
        return 0


class AudioDbClient:
    """TheAudioDB API wrapper for artist genres."""
    
    def __init__(self, api_key: str, http_session=None, enabled: bool = True):
        """
        Initialize AudioDB client.
        
        Args:
            api_key: TheAudioDB API key
            http_session: Optional requests.Session (uses shared if not provided)
            enabled: Whether AudioDB is enabled
        """
        self.api_key = api_key
        self.session = http_session or session
        self.enabled = enabled
        self.base_url = "https://theaudiodb.com/api/v1/json"
    
    def get_artist_genres(self, artist: str) -> list[str]:
        """
        Fetch genres from TheAudioDB for an artist.
        
        Args:
            artist: Artist name
            
        Returns:
            List of genre strings
        """
        if not self.enabled or not self.api_key:
            return []
        
        try:
            url = f"{self.base_url}/{self.api_key}/search.php"
            res = self.session.get(url, params={"s": artist}, timeout=(5, 10))  # (connect_timeout, read_timeout)
            res.raise_for_status()
            
            data = res.json().get("artists", [])
            if data and data[0].get("strGenre"):
                return [data[0]["strGenre"]]
            return []
        except Exception as e:
            logger.warning(f"AudioDB lookup failed for '{artist}': {e}")
            return []


def score_by_age(playcount: int | float, release_str: str) -> tuple[float, int]:
    """
    Apply age decay to score based on release date.
    
    Args:
        playcount: Number of plays
        release_str: Release date as string ("%Y-%m-%d")
        
    Returns:
        Tuple of (decayed_score, days_since_release)
    """
    try:
        release_date = datetime.strptime(release_str, "%Y-%m-%d")
        days_since = max((datetime.now() - release_date).days, 30)
        capped_days = min(days_since, 5 * 365)
        decay = 1 / math.log2(capped_days + 2)
        return playcount * decay, days_since
    except Exception:
        return 0, 9999


# Backward-compatible module functions
_listenbrainz_client = None
_audiodb_client = None

def _get_listenbrainz_client(enabled: bool = True):
    """Get or create singleton ListenBrainz client."""
    global _listenbrainz_client
    if _listenbrainz_client is None:
        _listenbrainz_client = ListenBrainzClient(enabled=enabled)
    return _listenbrainz_client

def _get_audiodb_client(api_key: str, enabled: bool = True):
    """Get or create singleton AudioDB client."""
    global _audiodb_client
    if _audiodb_client is None:
        _audiodb_client = AudioDbClient(api_key, enabled=enabled)
    return _audiodb_client

def get_listenbrainz_score(mbid: str, artist: str = "", title: str = "", enabled: bool = True) -> int:
    """Backward-compatible wrapper."""
    client = _get_listenbrainz_client(enabled)
    return client.get_listen_count(mbid, artist, title)

def get_audiodb_genres(artist: str, api_key: str = "", enabled: bool = True) -> list[str]:
    """Backward-compatible wrapper."""
    client = _get_audiodb_client(api_key, enabled)
    return client.get_artist_genres(artist)
