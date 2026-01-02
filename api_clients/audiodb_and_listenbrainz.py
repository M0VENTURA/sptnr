"""AudioDB and ListenBrainz API client module."""
import logging
import math
from datetime import datetime
from . import session

logger = logging.getLogger(__name__)


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
        
        if not mbid:
            # Fallback: search by artist/title
            try:
                url = f"{self.base_url}/recording/search"
                params = {"artist_name": artist, "recording_name": title, "limit": 1}
                res = self.session.get(url, params=params, timeout=10)
                res.raise_for_status()
                hits = res.json().get("recordings", [])
                if hits:
                    return int(hits[0].get("listen_count", 0))
            except Exception as e:
                logger.debug(f"ListenBrainz fallback search failed for '{title}': {e}")
            return 0
        
        # Primary: stats by MBID
        try:
            url = f"{self.base_url}/stats/recording/{mbid}/listen-count"
            res = self.session.get(url, timeout=10)
            res.raise_for_status()
            data = res.json()
            payload = data.get("payload", {})
            return int(payload.get("count", 0))
        except Exception as e:
            logger.warning(f"ListenBrainz fetch failed for MBID {mbid}: {e}")
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
            res = self.session.get(url, params={"s": artist}, timeout=10)
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
