"""Soulseek (slskd) API client for search and download operations."""
import logging
import time
from typing import Optional
from dataclasses import dataclass
from . import session

logger = logging.getLogger(__name__)


@dataclass
class SearchFile:
    """Represents a file from a Soulseek search result."""
    filename: str
    size: int
    bitrate: int
    sample_rate: int
    length: int
    code: str
    
    def __post_init__(self):
        """Ensure numeric fields are integers."""
        self.size = int(self.size or 0)
        self.bitrate = int(self.bitrate or 0)
        self.sample_rate = int(self.sample_rate or 0)
        self.length = int(self.length or 0)
    
    @property
    def size_mb(self) -> float:
        """Size in megabytes."""
        return self.size / (1024 * 1024) if self.size else 0
    
    @property
    def duration_seconds(self) -> int:
        """Track duration in seconds."""
        return self.length
    
    @property
    def duration_formatted(self) -> str:
        """Format duration as MM:SS."""
        if not self.length:
            return "0:00"
        minutes = self.length // 60
        seconds = self.length % 60
        return f"{minutes}:{seconds:02d}"
    
    def matches_quality(self, min_bitrate: int = 320, min_sample_rate: int = 44100) -> bool:
        """Check if file meets quality requirements."""
        return self.bitrate >= min_bitrate and self.sample_rate >= min_sample_rate


@dataclass
class SearchResponse:
    """Represents a response from a single peer."""
    username: str
    files: list[SearchFile]
    
    def __post_init__(self):
        """Parse raw file dicts into SearchFile objects."""
        if not self.files:
            self.files = []
        elif isinstance(self.files[0], dict):
            self.files = [
                SearchFile(
                    filename=f.get("filename", ""),
                    size=f.get("size", 0),
                    bitrate=f.get("bitRate", 0),
                    sample_rate=f.get("sampleRate", 0),
                    length=f.get("length", 0),
                    code=f.get("code", ""),
                )
                for f in self.files
            ]


class SlskdClient:
    """Soulseek (slskd) API wrapper for search and downloads."""
    
    def __init__(self, web_url: str, api_key: str = "", http_session=None, enabled: bool = True):
        """
        Initialize slskd client.
        
        Args:
            web_url: slskd web URL (e.g., "http://localhost:5030")
            api_key: slskd API key (optional)
            http_session: Optional requests.Session (uses shared if not provided)
            enabled: Whether slskd is enabled
        """
        self.web_url = web_url.rstrip("/")
        self.api_key = api_key
        self.session = http_session or session
        self.enabled = enabled
        self.base_url = f"{self.web_url}/api/v0"
        self.headers = {"X-API-Key": api_key} if api_key else {}
    
    def start_search(self, query: str, timeout: int = 10) -> Optional[str]:
        """
        Start a new search on Soulseek.
        
        Args:
            query: Search query (e.g., "artist title")
            timeout: Request timeout
            
        Returns:
            Search ID or None on failure
        """
        if not self.enabled:
            return None
        
        try:
            url = f"{self.base_url}/searches"
            data = {"searchText": query}
            resp = self.session.post(url, json=data, headers=self.headers, timeout=timeout)
            
            if resp.status_code not in [200, 201]:
                logger.warning(f"Slskd search start failed: {resp.status_code}")
                return None
            
            search_response = resp.json()
            search_id = search_response.get("id")
            if search_id:
                logger.debug(f"Slskd search started: {search_id} for query '{query}'")
            return search_id
        except Exception as e:
            logger.error(f"Slskd search failed for query '{query}': {e}")
            return None
    
    def get_search_results(self, search_id: str, timeout: int = 10) -> tuple[list[SearchResponse], str, bool]:
        """
        Poll for search results from Soulseek.
        
        Args:
            search_id: Search ID from start_search()
            timeout: Request timeout
            
        Returns:
            Tuple of (responses, state, is_complete)
            - responses: List of SearchResponse objects
            - state: Current search state ('Searching', 'Completed', 'Cancelled', etc.)
            - is_complete: True if search is done
        """
        if not self.enabled:
            return [], "Error", True
        
        try:
            url = f"{self.base_url}/searches/{search_id}"
            resp = self.session.get(url, headers=self.headers, timeout=timeout)
            
            if resp.status_code != 200:
                logger.warning(f"Slskd status failed: {resp.status_code}")
                return [], "Error", True
            
            search_data = resp.json()
            if not search_data:
                return [], "Searching", False
            
            state = search_data.get("state", "Searching")
            raw_responses = search_data.get("responses", []) or []
            
            # Parse responses into SearchResponse objects
            responses = []
            for raw_resp in raw_responses:
                try:
                    sr = SearchResponse(
                        username=raw_resp.get("username", "Unknown"),
                        files=raw_resp.get("files", [])
                    )
                    responses.append(sr)
                except Exception as e:
                    logger.debug(f"Failed to parse slskd response: {e}")
            
            is_complete = state in ["Completed", "Cancelled"]
            logger.debug(f"Slskd search {search_id}: state={state}, peers={len(responses)}, is_complete={is_complete}")
            
            return responses, state, is_complete
        except Exception as e:
            logger.error(f"Slskd get results failed for search {search_id}: {e}")
            return [], "Error", True
    
    def download_file(self, username: str, file_code: str, timeout: int = 10) -> bool:
        """
        Enqueue a file for download from a peer.
        
        Args:
            username: Peer username
            file_code: File code from SearchFile
            timeout: Request timeout
            
        Returns:
            True if enqueued successfully
        """
        if not self.enabled:
            return False
        
        try:
            url = f"{self.base_url}/transfers/downloads/{username}"
            data = {"fileId": file_code}
            resp = self.session.post(url, json=data, headers=self.headers, timeout=timeout)
            
            if resp.status_code in [200, 201]:
                logger.info(f"Download enqueued from {username} (file={file_code})")
                return True
            else:
                logger.warning(f"Slskd download failed: {resp.status_code}")
                return False
        except Exception as e:
            logger.error(f"Slskd download failed: {e}")
            return False
    
    def filter_results_by_quality(
        self,
        responses: list[SearchResponse],
        min_bitrate: int = 320,
        min_sample_rate: int = 44100,
        max_results: int = 10
    ) -> list[dict]:
        """
        Filter search results by quality metrics and return top matches.
        
        Args:
            responses: List of SearchResponse objects from get_search_results()
            min_bitrate: Minimum bitrate in kbps
            min_sample_rate: Minimum sample rate in Hz
            max_results: Maximum number of files to return
            
        Returns:
            List of file dicts sorted by quality (best first)
        """
        qualified = []
        
        for resp in responses:
            for file in resp.files:
                if file.matches_quality(min_bitrate, min_sample_rate):
                    qualified.append({
                        "username": resp.username,
                        "filename": file.filename,
                        "size_mb": file.size_mb,
                        "bitrate": file.bitrate,
                        "sample_rate": file.sample_rate,
                        "duration": file.duration_formatted,
                        "length_seconds": file.length,
                        "file_code": file.code,
                    })
        
        # Sort by bitrate (descending), then sample rate (descending)
        qualified.sort(key=lambda x: (-x["bitrate"], -x["sample_rate"]))
        
        return qualified[:max_results]
    
    def search_and_filter(
        self,
        query: str,
        min_bitrate: int = 320,
        wait_seconds: int = 5,
        poll_interval: float = 1.0,
        timeout: int = 10
    ) -> list[dict]:
        """
        Execute a complete search workflow: start → poll → filter → return results.
        
        Args:
            query: Search query
            min_bitrate: Minimum bitrate requirement
            wait_seconds: Time to wait for results
            poll_interval: Time between polls
            timeout: Request timeout
            
        Returns:
            List of qualified file results
        """
        if not self.enabled:
            return []
        
        # Start search
        search_id = self.start_search(query, timeout)
        if not search_id:
            return []
        
        # Poll for results
        start_time = time.time()
        while (time.time() - start_time) < wait_seconds:
            responses, state, is_complete = self.get_search_results(search_id, timeout)
            
            if responses:
                # Filter and return immediately if we have qualified results
                qualified = self.filter_results_by_quality(responses, min_bitrate=min_bitrate)
                if qualified:
                    logger.info(f"Slskd search found {len(qualified)} qualified files for '{query}'")
                    return qualified
            
            if is_complete:
                break
            
            time.sleep(poll_interval)
        
        # Final attempt
        responses, _, _ = self.get_search_results(search_id, timeout)
        qualified = self.filter_results_by_quality(responses, min_bitrate=min_bitrate)
        
        if qualified:
            logger.info(f"Slskd search found {len(qualified)} qualified files for '{query}' (final)")
        else:
            logger.info(f"Slskd search completed for '{query}' but no qualified results")
        
        return qualified


# Backward-compatible module functions
_slskd_client = None

def _get_slskd_client(web_url: str, api_key: str = "", enabled: bool = True):
    """Get or create singleton slskd client."""
    global _slskd_client
    if _slskd_client is None:
        _slskd_client = SlskdClient(web_url, api_key, enabled=enabled)
    return _slskd_client
