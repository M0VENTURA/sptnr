"""MusicBrainz API client module."""
import logging
import difflib
from . import session

logger = logging.getLogger(__name__)


class MusicBrainzClient:
    """MusicBrainz API wrapper for single detection and metadata."""
    
    def __init__(self, http_session=None, enabled: bool = True):
        """
        Initialize MusicBrainz client.
        
        Args:
            http_session: Optional requests.Session (uses shared if not provided)
            enabled: Whether MusicBrainz is enabled
        """
        self.session = http_session or session
        self.enabled = enabled
        self.base_url = "https://musicbrainz.org/ws/2/"
        self.headers = {"User-Agent": "sptnr-cli/2.1 (support@example.com)"}
    
    def is_single(self, title: str, artist: str) -> bool:
        """
        Query MusicBrainz release-group by title+artist and check primary-type=Single.
        
        Args:
            title: Track title
            artist: Artist name
            
        Returns:
            True if release-group type is Single
        """
        if not self.enabled:
            return False
        
        try:
            # Use simpler query format without extra quotes
            query = f'{title} AND artist:{artist} AND primarytype:Single'
            res = self.session.get(
                f"{self.base_url}release-group/",
                params={
                    "query": query,
                    "fmt": "json",
                    "limit": 5
                },
                headers=self.headers,
                timeout=15
            )
            res.raise_for_status()
            rgs = res.json().get("release-groups", [])
            return any((rg.get("primary-type") or "").lower() == "single" for rg in rgs)
        except Exception as e:
            logger.debug(f"MusicBrainz single check failed for '{title}': {e}")
            return False
    
    def get_genres(self, title: str, artist: str) -> list[str]:
        """
        Fetch tags/genres from MusicBrainz with explicit includes on recordings.
        
        Strategy:
          1) Search recording with inc=tags+artist-credits+releases
          2) Use recording-level tags if present
          3) If no recording tags, try tags on first associated release
          
        Args:
            title: Track title
            artist: Artist name
            
        Returns:
            List of genre/tag names
        """
        if not self.enabled:
            return []
        
        try:
            # Step 1: search recording with richer includes
            query = f'{title} AND artist:{artist}'
            rec_params = {
                "query": query,
                "fmt": "json",
                "limit": 3,
                "inc": "tags+artist-credits+releases",
            }
            r = self.session.get(f"{self.base_url}recording/", params=rec_params, headers=self.headers, timeout=10)
            r.raise_for_status()
            recs = r.json().get("recordings", []) or []
            if not recs:
                return []
            
            # Prefer the top match
            rec = recs[0]
            
            # 2) use recording-level tags if present
            tags = rec.get("tags") or []
            tag_names = [t.get("name", "") for t in tags if t.get("name")]
            if tag_names:
                return tag_names
            
            # 3) fallback: pull tags from the first release if any
            releases = rec.get("releases") or []
            if releases:
                rel_id = releases[0].get("id")
                if rel_id:
                    rel_params = {"fmt": "json", "inc": "tags"}
                    rr = self.session.get(f"{self.base_url}release/{rel_id}", params=rel_params, headers=self.headers, timeout=15)
                    rr.raise_for_status()
                    rel_tags = rr.json().get("tags", []) or []
                    return [t.get("name", "") for t in rel_tags if t.get("name")]
            return []
        except Exception as e:
            logger.warning(f"MusicBrainz genres lookup failed for '{title}' by '{artist}': {e}")
            return []
    
    def get_suggested_mbid(self, title: str, artist: str, limit: int = 5) -> tuple[str, float]:
        """
        Search MusicBrainz recordings and compute (mbid, confidence).
        
        Confidence:
          - Title similarity (SequenceMatcher)
          - +0.15 bonus if associated release-group primary-type == 'Single'
          
        Args:
            title: Track title
            artist: Artist name
            limit: Number of results to check
            
        Returns:
            Tuple of (mbid, confidence_score)
        """
        if not self.enabled:
            return "", 0.0
        
        try:
            # 1) Find recordings (with releases included for second hop)
            query = f'{title} AND artist:{artist}'
            rec_params = {
                "query": query,
                "fmt": "json",
                "limit": limit,
                "inc": "releases+artist-credits",
            }
            r = self.session.get(f"{self.base_url}recording/", params=rec_params, headers=self.headers, timeout=15)
            r.raise_for_status()
            recordings = r.json().get("recordings", []) or []
            if not recordings:
                return "", 0.0
            
            best_mbid = ""
            best_score = 0.0
            nav_title = (title or "").lower()
            
            for rec in recordings:
                rec_mbid = rec.get("id", "")
                rec_title = (rec.get("title") or "").lower()
                title_sim = difflib.SequenceMatcher(None, nav_title, rec_title).ratio()
                
                # Default: no bonus
                single_bonus = 0.0
                
                # 2) If we have at least one release, second hop to get primary-type reliably
                releases = rec.get("releases") or []
                if releases:
                    rel_id = releases[0].get("id")
                    if rel_id:
                        rel_params = {"fmt": "json", "inc": "release-groups"}
                        rr = self.session.get(f"{self.base_url}release/{rel_id}", params=rel_params, headers=self.headers, timeout=15)
                        if rr.ok:
                            rel_json = rr.json()
                            rg = rel_json.get("release-group") or {}
                            primary_type = (rg.get("primary-type") or "").lower()
                            if primary_type == "single":
                                single_bonus = 0.15
                
                confidence = min(1.0, title_sim + single_bonus)
                if confidence > best_score:
                    best_score = confidence
                    best_mbid = rec_mbid
            
            return best_mbid, round(best_score, 3)
        except Exception as e:
            logger.debug(f"MusicBrainz suggested MBID lookup failed for '{title}' by '{artist}': {e}")
            return "", 0.0


# Backward-compatible module functions
_musicbrainz_client = None

def _get_musicbrainz_client(enabled: bool = True):
    """Get or create singleton MusicBrainz client."""
    global _musicbrainz_client
    if _musicbrainz_client is None:
        _musicbrainz_client = MusicBrainzClient(enabled=enabled)
    return _musicbrainz_client

def is_musicbrainz_single(title: str, artist: str, enabled: bool = True) -> bool:
    """Backward-compatible wrapper."""
    client = _get_musicbrainz_client(enabled)
    return client.is_single(title, artist)

def get_musicbrainz_genres(title: str, artist: str, enabled: bool = True) -> list[str]:
    """Backward-compatible wrapper."""
    client = _get_musicbrainz_client(enabled)
    return client.get_genres(title, artist)

def get_suggested_mbid(title: str, artist: str, limit: int = 5, enabled: bool = True) -> tuple[str, float]:
    """Backward-compatible wrapper."""
    client = _get_musicbrainz_client(enabled)
    return client.get_suggested_mbid(title, artist, limit)
