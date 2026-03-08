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
    
    def get_recommendations(self, username: str, recommendation_type: str = "raw") -> list:
        """
        Get personalized recommendations from ListenBrainz.
        
        Args:
            username: ListenBrainz username
            recommendation_type: Type of recommendations ('raw', 'top_discoveries_for_year', etc.)
            
        Returns:
            List of recommended recordings with metadata
        """
        try:
            url = f"{self.base_url}/user/{username}/recommendations/recording/{recommendation_type}"
            res = self.session.get(url, headers=self.headers, timeout=(5, 30))
            res.raise_for_status()
            data = res.json()
            # The response structure varies by type, but generally has a 'payload' with 'mbids' or 'recordings'
            payload = data.get("payload", {})
            recordings = payload.get("recordings", [])
            logger.info(f"Got {len(recordings)} recommendations of type '{recommendation_type}' for {username}")
            return recordings
        except Exception as e:
            logger.error(f"Failed to get recommendations for {username}: {e}")
            return []
    
    def get_weekly_jams(self, username: str) -> list:
        """
        Get Weekly Jams recommendations (current week).
        
        Args:
            username: ListenBrainz username
            
        Returns:
            List of recommended tracks
        """
        return self.get_recommendations(username, "raw")
    
    def get_weekly_exploration(self, username: str) -> list:
        """
        Get Weekly Exploration recommendations (discovery mode).
        
        Args:
            username: ListenBrainz username
            
        Returns:
            List of recommended tracks for exploration
        """
        try:
            # Weekly exploration uses a different endpoint
            url = f"{self.base_url}/user/{username}/recommendations/exploration/weekly"
            res = self.session.get(url, headers=self.headers, timeout=(5, 30))
            res.raise_for_status()
            data = res.json()
            payload = data.get("payload", {})
            playlists = payload.get("playlists", [])
            # Flatten all tracks from playlists
            tracks = []
            for playlist in playlists:
                tracks.extend(playlist.get("recordings", []))
            logger.info(f"Got {len(tracks)} exploration tracks for {username}")
            return tracks
        except Exception as e:
            logger.error(f"Failed to get weekly exploration for {username}: {e}")
            return []
    
    def get_last_week_jams(self, username: str) -> list:
        """
        Get last week's jams (previous week recommendations).
        
        Args:
            username: ListenBrainz username
            
        Returns:
            List of recommended tracks from last week
            
        Note:
            Currently returns current week's data as ListenBrainz API does not 
            provide a direct endpoint for archived weekly recommendations.
            This is a placeholder implementation.
        """
        logger.warning("get_last_week_jams: Currently using current week's data - archived recommendations not available")
        return self.get_recommendations(username, "raw")
    
    def get_last_week_exploration(self, username: str) -> list:
        """
        Get last week's exploration tracks.
        
        Args:
            username: ListenBrainz username
            
        Returns:
            List of exploration tracks from last week
            
        Note:
            Currently returns current week's data as ListenBrainz API does not
            provide a direct endpoint for archived weekly exploration.
            This is a placeholder implementation.
        """
        logger.warning("get_last_week_exploration: Currently using current week's data - archived recommendations not available")
        return self.get_weekly_exploration(username)
    
    def get_username_from_token(self) -> str:
        """
        Validate token and get the associated username.
        
        Returns:
            Username associated with the token, or empty string if invalid
        """
        try:
            url = f"{self.base_url}/validate-token"
            res = self.session.get(url, headers=self.headers, timeout=(5, 10))
            res.raise_for_status()
            data = res.json()
            username = data.get("user_name", "")
            logger.info(f"Token validated for user: {username}")
            return username
        except Exception as e:
            logger.error(f"Failed to validate token: {e}")
            return ""
    
    def get_similar_artists(self, mbid: str, limit: int = 10) -> list:
        """
        Fetch similar artists from ListenBrainz for a given artist MBID.
        
        Uses the relationships endpoint to find acoustically/culturally similar artists.
        This will be used to find artists with similar listener bases,
        enabling artist-contextual popularity weighting.
        
        Args:
            mbid: MusicBrainz Artist ID
            limit: Maximum number of results to return (1-100)
            
        Returns:
            List of dicts with 'name', 'mbid', and optional 'score'
            Example: [{'name': 'Similar Artist 1', 'mbid': 'xxx'}, {...}]
        """
        if not mbid:
            return []
        
        limit = max(1, min(100, limit))
        
        try:
            # ListenBrainz API endpoint for artist relationships
            # This includes similar artists, collaborators, etc.
            url = f"{self.base_url}/artist/{mbid}/relationships?inc=artist"
            
            res = self.session.get(url, headers=self.headers, timeout=(5, 10))
            res.raise_for_status()
            data = res.json()
            
            relationships = data.get("relationships", [])
            similar_artists = []
            
            # Extract artist relationships (similar-to, collaboration, etc.)
            for rel in relationships:
                if rel.get("type") in ["similar-to", "collaboration", "performing in"]:
                    target = rel.get("artist") or rel.get("target")
                    if target and isinstance(target, dict):
                        name = target.get("name", "")
                        artist_mbid = target.get("id", "")
                        if name and len(similar_artists) < limit:
                            similar_artists.append({
                                "name": name,
                                "mbid": artist_mbid
                            })
            
            logger.debug(f"Fetched {len(similar_artists)} similar artists for MBID {mbid} from ListenBrainz")
            return similar_artists
            
        except Exception as e:
            logger.debug(f"Failed to fetch similar artists for MBID {mbid} from ListenBrainz: {e}")
            return []


class ListenBrainzClient:
    """ListenBrainz API wrapper for listening stats.
    
    NOTE: ListenBrainz does NOT provide a public API endpoint for global listen counts.
    Global listen statistics are only available via:
    - PostgreSQL data dumps (processed locally)
    - Big Data infrastructure (Spark pipelines) 
    
    User-specific listen counts ARE available if authenticated with a user token.
    """
    
    def __init__(self, http_session=None, enabled: bool = True, user_token: str = ""):
        """
        Initialize ListenBrainz client.
        
        Args:
            http_session: Optional requests.Session (uses shared if not provided)
            enabled: Whether ListenBrainz is enabled
            user_token: User authentication token for personal stats (optional)
        """
        self.session = http_session or session
        self.enabled = enabled
        self.user_token = user_token
        self.base_url = "https://api.listenbrainz.org/1"
        self.headers = {}
        if user_token:
            self.headers["Authorization"] = f"Token {user_token}"
    
    def get_listen_count(self, mbid: str = "", artist: str = "", title: str = "") -> int:
        """
        Fetch ListenBrainz listen count.
        
        IMPORTANT: ListenBrainz does NOT provide a public API for global listen counts.
        This always returns 0. Global stats require:
        - Processing PostgreSQL data dumps locally
        - Access to ListenBrainz Big Data infrastructure
        
        User-specific listen stats could be implemented if user provides token.
        
        Args:
            mbid: MusicBrainz recording ID
            artist: Artist name
            title: Track title
            
        Returns:
            Always returns 0 (global endpoint not available)
        """
        if not self.enabled:
            logger.debug("ListenBrainz is disabled")
            return 0
        
        # Log why we can't fetch global stats
        logger.debug(
            f"ListenBrainz global listen count for '{title}' cannot be fetched via API. "
            f"ListenBrainz does not provide a public endpoint for global listen counts. "
            f"Global statistics are only available via data dumps or their Big Data infrastructure."
        )
        
        # Could implement user-specific stats here if token provided
        if self.user_token and mbid:
            return self._get_user_listen_count(mbid)
        
        return 0
    
    def _get_user_listen_count(self, mbid: str) -> int:
        """
        Get user's personal listen count for a recording (requires auth token).
        
        Args:
            mbid: MusicBrainz recording ID
            
        Returns:
            User's listen count for this recording, or 0
        """
        if not self.user_token or not mbid:
            return 0
        
        try:
            # This endpoint requires authentication - would fetch user's personal stats
            # Implementation would go here if we wanted to support it
            logger.debug(f"User-specific listen stats not yet implemented for {mbid}")
            return 0
        except Exception as e:
            logger.debug(f"Failed to fetch user listen count: {e}")
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


def get_recording_popularity_batch(recording_mbids: list[str], user_agent: str = "") -> dict[str, dict]:
    """
    Fetch ListenBrainz popularity for multiple recordings in a single batch request.
    
    IMPORTANT: ListenBrainz and MusicBrainz share the same MetaBrainz infrastructure.
    Both APIs enforce 1 request per second rate limit. This function shares rate limiting
    with MusicBrainz API calls via api_rate_limiter.check_musicbrainz_limit().
    
    Args:
        recording_mbids: List of MusicBrainz recording IDs (up to 100 per request)
        user_agent: User-Agent header (should match MusicBrainz user agent for consistency)
                   Falls back to app's user agent if not provided
    
    Returns:
        Dict mapping recording_mbid → {'total_listen_count': int, 'total_user_count': int}
        Returns null values for not-found recordings
        Example: {
            "13dd61c7-ce73-4e97-9f0c-9f0e53144411": {"total_listen_count": 1000, "total_user_count": 50},
            "22ad712e-ce73-9f0c-4e97-9f0c-4e97": {"total_listen_count": null, "total_user_count": null}
        }
    """
    try:
        # Import here to avoid circular dependency
        from helpers.api_rate_limiter import get_rate_limiter
        from api_clients.musicbrainz import _USER_AGENT as MB_USER_AGENT
        
        # Use provided user agent or fall back to MusicBrainz user agent
        ua = user_agent or MB_USER_AGENT or "sptnr/2.0"
        
        # Check rate limit before making request
        rate_limiter = get_rate_limiter()
        can_proceed, reason = rate_limiter.check_musicbrainz_limit()
        
        if not can_proceed:
            logger.debug(f"[LB_POPULARITY] Rate limit check failed: {reason}")
            # Return empty dict on rate limit
            return {mbid: {"total_listen_count": None, "total_user_count": None} for mbid in recording_mbids}
        
        # Limit batch size to ListenBrainz API max (typically around 100)
        if len(recording_mbids) > 100:
            logger.warning(f"[LB_POPULARITY] Batch size {len(recording_mbids)} exceeds 100, truncating")
            recording_mbids = recording_mbids[:100]
        
        # Prepare request
        url = "https://api.listenbrainz.org/1/popularity/recording"
        payload = {"recording_mbids": recording_mbids}
        headers = {"User-Agent": ua}
        
        logger.debug(f"[LB_POPULARITY] Fetching popularity for {len(recording_mbids)} recordings")
        
        # Make request
        res = session.post(url, json=payload, headers=headers, timeout=(5, 15))
        res.raise_for_status()
        
        # Record the API request for rate limiting
        rate_limiter.record_musicbrainz_request()
        logger.debug(f"[LB_POPULARITY] Request successful, rate limit recorded")
        
        # Parse response - should be a list maintaining order of input MBIDs
        data = res.json()
        
        if not isinstance(data, list):
            logger.error(f"[LB_POPULARITY] Unexpected response format: {type(data)}")
            return {mbid: {"total_listen_count": None, "total_user_count": None} for mbid in recording_mbids}
        
        # Convert list response to dict mapping MBID → popularity data
        result = {}
        for i, mbid in enumerate(recording_mbids):
            if i < len(data):
                item = data[i]
                result[mbid] = {
                    "total_listen_count": item.get("total_listen_count"),
                    "total_user_count": item.get("total_user_count")
                }
            else:
                result[mbid] = {"total_listen_count": None, "total_user_count": None}
        
        logger.debug(f"[LB_POPULARITY] Batch complete: {len(result)} recordings processed")
        return result
        
    except Exception as e:
        logger.debug(f"[LB_POPULARITY] Error fetching popularity: {e}")
        return {mbid: {"total_listen_count": None, "total_user_count": None} for mbid in recording_mbids}


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
