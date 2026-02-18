"""Soulseek (slskd) API client for search and download operations."""
import logging
import time
import traceback
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
        elif self.files and isinstance(self.files[0], dict):
            self.files = [
                SearchFile(
                    filename=f.get("filename", ""),
                    size=f.get("size", 0),
                    bitrate=f.get("bitRate", 0),
                    sample_rate=f.get("sampleRate", 0),
                    length=f.get("length", 0),
                )
                for f in self.files
            ]


class SlskdClient:
    """Soulseek (slskd) API wrapper for search and downloads."""
    
    def __init__(self, web_url: str, api_key: str = "", http_session=None, enabled: bool = True, default_timeout: int = 10):
        """
        Initialize slskd client.
        
        Args:
            web_url: slskd web URL (e.g., "http://localhost:5030")
            api_key: slskd API key (optional)
            http_session: Optional requests.Session (uses shared if not provided)
            enabled: Whether slskd is enabled
            default_timeout: Default timeout for API requests in seconds
        """
        self.web_url = web_url.rstrip("/")
        self.api_key = api_key
        self.session = http_session or session
        self.enabled = enabled
        self.default_timeout = default_timeout
        self.base_url = f"{self.web_url}/api/v0"
        self.headers = {"X-API-Key": api_key} if api_key else {}
    
    def start_search(self, query: str, timeout: int = None) -> Optional[str]:
        """
        Start a new search on Soulseek.
        
        Args:
            query: Search query (e.g., "artist title")
            timeout: Request timeout (uses default_timeout if not specified)
            
        Returns:
            Search ID or None on failure
        """
        if not self.enabled:
            return None
        
        timeout = timeout or self.default_timeout
        
        try:
            url = f"{self.base_url}/searches"
            # slskd API uses searchText as the field name
            data = {"searchText": query}
            resp = self.session.post(url, json=data, headers=self.headers, timeout=timeout)
            
            if resp.status_code not in [200, 201]:
                logger.warning(f"Slskd search start failed: {resp.status_code} - {resp.text[:200]}")
                return None
            
            search_response = resp.json()
            # Handle both possible response formats
            search_id = search_response.get("id") or search_response.get("searchId")
            if search_id:
                logger.debug(f"Slskd search started: {search_id} for query '{query}'")
            else:
                logger.warning(f"Slskd search response missing ID: {search_response}")
            return search_id
        except Exception as e:
            logger.error(f"Slskd search failed for query '{query}': {e}")
            return None
    
    def get_search_results(self, search_id: str, timeout: int = None) -> tuple[list[SearchResponse], str, bool]:
        """
        Poll for search results from Soulseek.
        
        Args:
            search_id: Search ID from start_search()
            timeout: Request timeout (uses default_timeout if not specified)
            
        Returns:
            Tuple of (responses, state, is_complete)
            - responses: List of SearchResponse objects
            - state: Current search state ('Searching', 'Completed', 'Cancelled', etc.)
            - is_complete: True if search is done
        """
        if not self.enabled:
            return [], "Error", True
        
        timeout = timeout or self.default_timeout
        
        try:
            # First, get the search state
            state_url = f"{self.base_url}/searches/{search_id}"
            state_resp = self.session.get(state_url, headers=self.headers, timeout=timeout)
            
            if state_resp.status_code != 200:
                logger.warning(f"Slskd status failed: {state_resp.status_code} - {state_resp.text[:200]}")
                return [], "Error", True
            
            state_data = state_resp.json()
            state = state_data.get("state", "InProgress")
            logger.debug(f"Slskd search {search_id} state: {state}")
            
            # Get the actual responses from the responses endpoint
            responses_url = f"{self.base_url}/searches/{search_id}/responses"
            resp = self.session.get(responses_url, headers=self.headers, timeout=timeout)
            
            if resp.status_code != 200:
                logger.debug(f"Slskd responses endpoint returned {resp.status_code}")
                return [], state, state not in ["InProgress", "Requested"]
            
            raw_responses = resp.json() or []
            
            # Debug: log response structure and count
            if raw_responses:
                logger.debug(f"Slskd got {len(raw_responses)} raw responses, response type: {type(raw_responses)}")
                if isinstance(raw_responses, list) and raw_responses:
                    first_item = raw_responses[0]
                    logger.debug(f"First response type: {type(first_item)}, keys: {first_item.keys() if isinstance(first_item, dict) else 'N/A'}")
            else:
                logger.debug(f"Slskd responses list is empty or None")
            
            # Parse responses into SearchResponse objects
            responses = []
            for idx, raw_resp in enumerate(raw_responses):
                try:
                    # Handle both dict and pre-parsed object formats
                    if isinstance(raw_resp, dict):
                        username = raw_resp.get("username", "Unknown")
                        files = raw_resp.get("files", [])
                        logger.debug(f"Response {idx}: username={username}, files={len(files)}")
                        sr = SearchResponse(
                            username=username,
                            files=files
                        )
                        responses.append(sr)
                    else:
                        responses.append(raw_resp)
                except Exception as e:
                    logger.warning(f"Failed to parse slskd response {idx}: {e}")
            
            is_complete = state not in ["InProgress", "Requested"]
            logger.info(f"Slskd search {search_id}: state={state}, peers={len(responses)}, is_complete={is_complete}")
            
            return responses, state, is_complete
        except Exception as e:
            logger.error(f"Slskd get results failed for search {search_id}: {e}")
            return [], "Error", True
    
    def download_file(self, username: str, filename: str, size: int = 0, timeout: int = None) -> bool:
        """
        Enqueue a file for download from a peer.
        
        Args:
            username: Peer username
            filename: Full file path from search results
            size: File size in bytes
            timeout: Request timeout (uses default_timeout if not specified)
            
        Returns:
            True if enqueued successfully
        """
        if not self.enabled:
            logger.warning("Slskd download_file called but client is not enabled")
            return False
        
        timeout = timeout or self.default_timeout
        
        try:
            # slskd API expects POST with files array containing filename and size
            url = f"{self.base_url}/transfers/downloads/{username}"
            data = [{"filename": filename, "size": size}]
            
            logger.info(f"Enqueuing download: username={username}, file={filename[:80]}, size={size}")
            logger.debug(f"POST {url} with data: {data}")
            
            resp = self.session.post(url, json=data, headers=self.headers, timeout=timeout)
            
            logger.debug(f"Slskd download response: status={resp.status_code}, body={resp.text[:500]}")
            
            if resp.status_code in [200, 201, 204]:
                logger.info(f"✓ Download successfully enqueued in slskd from {username}")
                return True
            else:
                logger.error(f"✗ Slskd download failed: HTTP {resp.status_code}")
                logger.error(f"Response body: {resp.text[:500]}")
                return False
        except Exception as e:
            logger.error(f"✗ Slskd download exception for {username}/{filename[:50]}: {type(e).__name__}: {e}")
            logger.error(traceback.format_exc())
            return False

    def download_files(self, files: list[dict], timeout: int = None) -> list[dict]:
        """
        Enqueue multiple files (potentially across users) for download.

        Args:
            files: List of {username, filename, size} dicts
            timeout: Request timeout (uses default_timeout if not specified)

        Returns:
            List of result dicts per user with status and error (if any)
        """
        if not self.enabled:
            return []
        
        timeout = timeout or self.default_timeout

        # Group by username because slskd expects per-user batches
        grouped: dict[str, list[dict]] = {}
        for entry in files or []:
            username = entry.get("username")
            filename = entry.get("filename")
            if not username or not filename:
                logger.warning("Skipping slskd download entry missing username or filename")
                continue
            grouped.setdefault(username, []).append({
                "filename": filename,
                "size": int(entry.get("size") or 0)
            })

        results = []
        for username, payload in grouped.items():
            try:
                url = f"{self.base_url}/transfers/downloads/{username}"
                resp = self.session.post(url, json=payload, headers=self.headers, timeout=timeout)

                success = resp.status_code in [200, 201, 204]
                if success:
                    logger.info(f"Download enqueued from {username} ({len(payload)} files)")
                else:
                    logger.warning(f"Slskd batch download failed: {resp.status_code} - {resp.text[:200]}")

                results.append({
                    "username": username,
                    "requested": len(payload),
                    "status": resp.status_code,
                    "success": success,
                    "error": None if success else resp.text[:200]
                })
            except Exception as e:
                logger.error(f"Slskd batch download failed for {username}: {e}")
                results.append({
                    "username": username,
                    "requested": len(payload),
                    "status": None,
                    "success": False,
                    "error": str(e)
                })

        return results
    
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
        timeout: int = None
    ) -> list[dict]:
        """
        Execute a complete search workflow: start → poll → filter → return results.
        
        Args:
            query: Search query
            min_bitrate: Minimum bitrate requirement
            wait_seconds: Time to wait for results
            poll_interval: Time between polls
            timeout: Request timeout (uses default_timeout if not specified)
            
        Returns:
            List of qualified file results
        """
        if not self.enabled:
            return []
        
        timeout = timeout or self.default_timeout
        
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
    
    def get_active_downloads(self, timeout: int = None) -> list[dict]:
        """
        Get list of active downloads from slskd.
        
        Args:
            timeout: Request timeout (uses default_timeout if not specified)
            
        Returns:
            List of download dicts with progress information
        """
        if not self.enabled:
            return []
        
        timeout = timeout or self.default_timeout
        
        try:
            # Query the transfers/downloads endpoint
            url = f"{self.base_url}/transfers/downloads"
            resp = self.session.get(url, headers=self.headers, timeout=timeout)
            
            if resp.status_code != 200:
                logger.warning(f"Slskd downloads endpoint failed: {resp.status_code}")
                return []
            
            raw_downloads = resp.json() or []
            downloads = []
            
            for download in raw_downloads:
                try:
                    # Extract key fields from slskd response
                    username = download.get("username", "Unknown")
                    filename = download.get("filename", "Unknown")
                    size = int(download.get("size", 0))
                    bytes_transferred = int(download.get("bytesTransferred", 0))
                    state = download.get("state", "Unknown")
                    
                    # Calculate progress percentage (0-100)
                    progress = 0
                    if size > 0:
                        progress = min(100, round((bytes_transferred / size) * 100, 2))
                    
                    # Calculate average speed (bytes/sec)
                    # slskd may provide averageSpeed or we can estimate it
                    average_speed = int(download.get("averageSpeed", 0))
                    
                    downloads.append({
                        "username": username,
                        "filename": filename,
                        "size": size,
                        "bytesTransferred": bytes_transferred,
                        "progress": progress,
                        "state": state,
                        "averageSpeed": average_speed,
                    })
                except Exception as e:
                    logger.warning(f"Failed to parse slskd download entry: {e}")
            
            logger.debug(f"Slskd found {len(downloads)} active downloads")
            return downloads
        except Exception as e:
            logger.error(f"Slskd get active downloads failed: {e}")
            return []
    
    def cancel_search(self, search_id: str, timeout: int = None) -> bool:
        """
        Cancel an active search.
        
        Args:
            search_id: Search ID from start_search()
            timeout: Request timeout (uses default_timeout if not specified)
            
        Returns:
            True if cancelled successfully
        """
        if not self.enabled:
            return False
        
        timeout = timeout or self.default_timeout
        
        try:
            url = f"{self.base_url}/searches/{search_id}"
            resp = self.session.delete(url, headers=self.headers, timeout=timeout)
            
            if resp.status_code in [200, 204]:
                logger.info(f"Slskd search {search_id} cancelled successfully")
                return True
            else:
                logger.warning(f"Slskd search cancel failed: {resp.status_code} - {resp.text[:200]}")
                return False
        except Exception as e:
            logger.error(f"Slskd cancel search failed for {search_id}: {e}")
            return False
    
    def cancel_download(self, transfer_id: str, timeout: int = None) -> bool:
        """
        Cancel a specific download by transfer ID.
        
        Args:
            transfer_id: Transfer/download ID
            timeout: Request timeout (uses default_timeout if not specified)
            
        Returns:
            True if cancelled successfully
        """
        if not self.enabled:
            return False
        
        timeout = timeout or self.default_timeout
        
        try:
            url = f"{self.base_url}/transfers/{transfer_id}"
            resp = self.session.delete(url, headers=self.headers, timeout=timeout)
            
            if resp.status_code in [200, 204]:
                logger.info(f"Slskd download {transfer_id} cancelled successfully")
                return True
            else:
                logger.warning(f"Slskd download cancel failed: {resp.status_code} - {resp.text[:200]}")
                return False
        except Exception as e:
            logger.error(f"Slskd cancel download failed for {transfer_id}: {e}")
            return False
    
    def get_transfer(self, transfer_id: str, timeout: int = None) -> Optional[dict]:
        """
        Get details of a specific transfer by ID.
        
        Args:
            transfer_id: Transfer/download ID
            timeout: Request timeout (uses default_timeout if not specified)
            
        Returns:
            Transfer details dict or None if not found
        """
        if not self.enabled:
            return None
        
        timeout = timeout or self.default_timeout
        
        try:
            url = f"{self.base_url}/transfers/{transfer_id}"
            resp = self.session.get(url, headers=self.headers, timeout=timeout)
            
            if resp.status_code == 200:
                return resp.json()
            else:
                logger.debug(f"Slskd get transfer {transfer_id} failed: {resp.status_code}")
                return None
        except Exception as e:
            logger.error(f"Slskd get transfer failed for {transfer_id}: {e}")
            return None


# Backward-compatible module functions
_slskd_client = None

def _get_slskd_client(web_url: str, api_key: str = "", enabled: bool = True):
    """Get or create singleton slskd client."""
    global _slskd_client
    if _slskd_client is None:
        _slskd_client = SlskdClient(web_url, api_key, enabled=enabled)
    return _slskd_client
