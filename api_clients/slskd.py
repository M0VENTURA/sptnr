"""Soulseek (slskd) API client for search and download operations."""
import logging
import time
import traceback
from typing import Optional
from dataclasses import dataclass
from . import session

logger = logging.getLogger(__name__)

# Maximum age in milliseconds before an "InProgress" search is considered
# stuck and eligible for cancellation in clear_stale_searches().
_STUCK_SEARCH_TIMEOUT_MS = 3 * 60 * 1000  # 3 minutes

# Soulseek search states that are terminal AND carry no results (the search
# was cancelled or failed before collecting any peer responses).  Used by
# get_search_results() to skip a redundant /responses HTTP call.
# slskd serialises C# flag enums as comma-separated strings (e.g.
# "Completed, TimedOut", "Completed, Cancelled").  We also keep the plain
# legacy strings for backward compatibility.
_EMPTY_TERMINAL_STATES = frozenset({
    "Completed, Cancelled", "Completed, Errored",
    "Cancelled", "Errored",
})


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
    has_free_upload_slot: bool = True

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
    
    def __init__(self, web_url: str, api_key: str = "", http_session=None, enabled: bool = True, default_timeout: Optional[int] = 15):
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
    
    def start_search(self, query: str, timeout: Optional[int] = None, max_attempts: int = 5) -> Optional[str]:
        """
        Start a new search on Soulseek.
        
        Args:
            query: Search query (e.g., "artist title")
            timeout: Request timeout (uses default_timeout if not specified)
            max_attempts: Maximum number of attempts when slskd returns 429 (slot busy).
                          Use a lower value (e.g. 3) for interactive/manual searches so the
                          caller doesn't wait too long before surfacing an error.
            
        Returns:
            Search ID or None on failure
        """
        if not self.enabled:
            return None
        
        timeout = timeout or self.default_timeout
        
        try:
            url = f"{self.base_url}/searches"
            # slskd API uses searchText as the field name.
            # filterResponses=False disables the user's configured UI search
            # filter (e.g. minbitrate:320, minfilesinfolder:8) for automated
            # queue searches.  Without this, slskd silently drops results that
            # don't meet the UI filter — e.g. valid 256kbps files or peers
            # sharing fewer files than the configured threshold — before we
            # even see them.  Our own scoring logic handles quality filtering.
            data = {"searchText": query, "filterResponses": False}

            # slskd enforces a single concurrent search operation; gracefully
            # wait/retry when the API returns HTTP 429 for that condition.
            for attempt in range(1, max_attempts + 1):
                resp = self.session.post(url, json=data, headers=self.headers, timeout=timeout)

                if resp.status_code in [200, 201]:
                    search_response = resp.json()
                    # Handle both possible response formats
                    search_id = search_response.get("id") or search_response.get("searchId")
                    if search_id:
                        logger.debug(f"Slskd search started: {search_id} for query '{query}'")
                    else:
                        logger.warning(f"Slskd search response missing ID: {search_response}")
                    return search_id

                body_preview = (resp.text or "")[:200]
                body_lc = body_preview.lower()
                retryable_429 = (
                    resp.status_code == 429
                    and (
                        "only one concurrent operation" in body_lc
                        or "wait until the previous request completes" in body_lc
                    )
                )

                if retryable_429 and attempt < max_attempts:
                    retry_after_header = (resp.headers.get("Retry-After") or "").strip()
                    wait_seconds = 0.8
                    if retry_after_header:
                        try:
                            wait_seconds = max(0.2, float(retry_after_header))
                        except Exception:
                            wait_seconds = 0.8
                    else:
                        wait_seconds = min(2.0, 0.4 * attempt)

                    logger.info(
                        f"Slskd search slot busy (attempt {attempt}/{max_attempts}) for '{query}'; "
                        f"waiting {wait_seconds:.1f}s before retry"
                    )
                    time.sleep(wait_seconds)
                    continue

                logger.warning(f"Slskd search start failed: {resp.status_code} - {body_preview}")
                return None

            logger.warning(f"Slskd search start exhausted retries for query '{query}'")
            return None
        except Exception as e:
            logger.error(f"Slskd search failed for query '{query}': {e}")
            return None
    
    def get_search_results(self, search_id: str, timeout: Optional[int] = None) -> tuple[list[SearchResponse], str, bool]:
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
            
            # Terminal states that carry no search results (the search was
            # cancelled or failed before collecting any peers).  Skip the
            # second HTTP call to /responses — it would return an empty list
            # anyway, and saving the round-trip matters in busy queues.
            if state in _EMPTY_TERMINAL_STATES:
                logger.debug(f"Slskd search {search_id} is in terminal no-result state ({state}); skipping responses fetch")
                return [], state, True
            
            # Get the actual responses from the responses endpoint
            responses_url = f"{self.base_url}/searches/{search_id}/responses"
            resp = self.session.get(responses_url, headers=self.headers, timeout=timeout)
            
            if resp.status_code != 200:
                logger.debug(f"Slskd responses endpoint returned {resp.status_code}")
                _active_states = {"None", "Queued", "Requested", "InProgress", "Initializing"}
                return [], state, state not in _active_states

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
                            files=files,
                            has_free_upload_slot=raw_resp.get("hasFreeUploadSlot", raw_resp.get("HasFreeUploadSlot", True)),
                        )
                        responses.append(sr)
                    else:
                        responses.append(raw_resp)
                except Exception as e:
                    logger.warning(f"Failed to parse slskd response {idx}: {e}")

            _active_states = {"None", "Queued", "Requested", "InProgress", "Initializing"}
            is_complete = state not in _active_states
            logger.info(f"Slskd search {search_id}: state={state}, peers={len(responses)}, is_complete={is_complete}")
            
            return responses, state, is_complete
        except Exception as e:
            logger.error(f"Slskd get results failed for search {search_id}: {e}")
            return [], "Error", True
    
    def download_file(self, username: str, filename: str, size: int = 0, timeout: Optional[int] = None) -> bool:
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

    @staticmethod
    def _extract_queue_position(entry: dict | None) -> Optional[int]:
        """Extract queue position from transfer payloads across slskd versions."""
        if not isinstance(entry, dict):
            return None
        raw = (
            entry.get("queuePosition")
            or entry.get("queue_position")
            or entry.get("position")
            or entry.get("queueIndex")
            or entry.get("queueLength")
        )
        if raw is None:
            return None
        try:
            return int(raw)
        except Exception:
            return None

    def enqueue_download_with_tracking(
        self,
        username: str,
        filename: str,
        size: int = 0,
        timeout: Optional[int] = None,
        lookup_attempts: int = 3,
        lookup_delay_seconds: float = 0.35,
    ) -> dict:
        """Enqueue a download and best-effort resolve its transfer identity/state.

        Returns a dict with:
          - success: bool
          - transfer_id: str
          - username: str
          - state: str
          - queue_position: Optional[int]
        """
        success = self.download_file(username=username, filename=filename, size=size, timeout=timeout)
        result = {
            "success": bool(success),
            "transfer_id": "",
            "username": username,
            "state": "Requested" if success else "",
            "queue_position": None,
        }
        if not success:
            return result

        timeout = timeout or self.default_timeout
        for attempt in range(max(1, int(lookup_attempts))):
            transfer = self.find_download(username=username, filename=filename, timeout=timeout)
            if transfer:
                result["transfer_id"] = str(transfer.get("id") or "")
                result["username"] = str(transfer.get("username") or username or "")
                result["state"] = self._state_text(
                    transfer.get("state") or transfer.get("transferState") or transfer.get("status") or "Requested"
                )
                result["queue_position"] = self._extract_queue_position(transfer)
                break
            if attempt < max(1, int(lookup_attempts)) - 1:
                time.sleep(max(0.05, float(lookup_delay_seconds)))

        return result

    def download_files(self, files: list[dict], timeout: Optional[int] = None) -> list[dict]:
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
                        "has_free_upload_slot": getattr(resp, 'has_free_upload_slot', True),
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
        timeout: Optional[int] = None
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
    
    # slskd transfer state constants (from slskd/src/web/src/lib/transfers.js)
    # Active states
    STATE_REQUESTED = "Requested"
    STATE_QUEUED_REMOTELY = "Queued, Remotely"
    STATE_QUEUED_LOCALLY = "Queued, Locally"
    STATE_INITIALIZING = "Initializing"
    STATE_IN_PROGRESS = "InProgress"
    # Terminal states
    STATE_SUCCEEDED = "Completed, Succeeded"
    STATE_CANCELLED = "Completed, Cancelled"
    STATE_TIMED_OUT = "Completed, TimedOut"
    STATE_ERRORED = "Completed, Errored"
    STATE_REJECTED = "Completed, Rejected"

    # Sets for quick membership tests (include common state-string variants across slskd versions)
    ACTIVE_STATES = frozenset([
        STATE_REQUESTED,
        STATE_QUEUED_REMOTELY,
        STATE_QUEUED_LOCALLY,
        STATE_INITIALIZING,
        STATE_IN_PROGRESS,
        "Queued",
        "In Progress",
        "Downloading",
    ])
    FAILED_STATES = frozenset([
        STATE_CANCELLED,
        STATE_TIMED_OUT,
        STATE_ERRORED,
        STATE_REJECTED,
        "Cancelled",
        "TimedOut",
        "Errored",
        "Failed",
        "Rejected",
        "Error",
    ])

    @staticmethod
    def _state_text(raw_state) -> str:
        """Normalize transfer state values from different slskd response variants."""
        if raw_state is None:
            return ""
        if isinstance(raw_state, dict):
            # Some wrappers can return a typed object-like dict.
            raw_state = raw_state.get("state") or raw_state.get("name") or raw_state.get("value")
        return str(raw_state).strip()

    @classmethod
    def _is_success_state(cls, raw_state) -> bool:
        """Return True when transfer state indicates successful completion."""
        state = cls._state_text(raw_state)
        state_lower = state.lower()
        if not state_lower:
            return False
        if state == cls.STATE_SUCCEEDED:
            return True
        return (
            "succeed" in state_lower
            or state_lower in {"completed", "complete", "succeeded"}
        )

    def _iter_transfer_files(self, user_entry: dict):
        """Yield transfer file dicts for both nested and flat transfer payload variants."""
        if not isinstance(user_entry, dict):
            return

        # Canonical slskd response shape: [{username, directories:[{files:[...]}]}]
        directories = user_entry.get("directories")
        if isinstance(directories, list):
            for directory in directories:
                if not isinstance(directory, dict):
                    continue
                files = directory.get("files") or directory.get("downloads") or []
                if not isinstance(files, list):
                    continue
                for f in files:
                    if isinstance(f, dict):
                        yield f

        # Alternate shape where user object directly contains files/downloads
        direct_files = user_entry.get("files") or user_entry.get("downloads")
        if isinstance(direct_files, list):
            for f in direct_files:
                if isinstance(f, dict):
                    yield f

    def _parse_transfers_response(self, raw: list | dict) -> list[dict]:
        """
        Parse slskd GET /transfers/downloads response into a flat list of file dicts.

        The API returns a nested structure:
          [ { username, directories: [ { directory, files: [ { id, state, filename,
              size, bytesTransferred, averageSpeed, localFilePath, ... } ] } ] } ]

        Each returned dict includes a top-level 'username' and 'id' so callers can
        use the correct cancel endpoint (DELETE /transfers/downloads/{username}/{id}).
        """
        flat = []

        # Some API wrappers return {downloads:[...]} or {transfers:[...]}.
        if isinstance(raw, dict):
            raw = raw.get("downloads") or raw.get("transfers") or raw.get("items") or []

        for user_entry in (raw or []):
            if not isinstance(user_entry, dict):
                continue
            username = user_entry.get("username", "Unknown")

            # Flat per-transfer object shape: [{username, filename, state, ...}]
            if user_entry.get("filename") and not user_entry.get("directories"):
                size = int(user_entry.get("size", 0) or 0)
                bytes_transferred = int(user_entry.get("bytesTransferred", 0) or 0)
                progress = min(100, round((bytes_transferred / size) * 100, 2)) if size else int(user_entry.get("percentComplete", 0) or 0)
                flat.append({
                    "id": user_entry.get("id") or user_entry.get("remoteToken") or user_entry.get("token") or "",
                    "username": username,
                    "filename": user_entry.get("filename") or user_entry.get("fileName") or user_entry.get("path") or "",
                    "size": size,
                    "bytesTransferred": bytes_transferred,
                    "progress": progress,
                    "state": self._state_text(user_entry.get("state") or user_entry.get("transferState") or user_entry.get("status")),
                    "averageSpeed": int(user_entry.get("averageSpeed", 0) or 0),
                    "queuePosition": self._extract_queue_position(user_entry),
                    "localFilePath": (
                        user_entry.get("localFilePath")
                        or user_entry.get("localPath")
                        or user_entry.get("downloadedFilePath")
                        or user_entry.get("path")
                        or ""
                    ),
                })
                continue

            for f in self._iter_transfer_files(user_entry):
                size = int(f.get("size", 0) or 0)
                bytes_transferred = int(f.get("bytesTransferred", 0) or 0)
                percent_complete = int(f.get("percentComplete", 0) or 0)
                progress = min(100, round((bytes_transferred / size) * 100, 2)) if size else percent_complete
                flat.append({
                    "id": f.get("id") or f.get("remoteToken") or f.get("token") or "",
                    "username": username,
                    "filename": f.get("filename") or f.get("fileName") or f.get("name") or f.get("path") or "",
                    "size": size,
                    "bytesTransferred": bytes_transferred,
                    "progress": progress,
                    "state": self._state_text(f.get("state") or f.get("transferState") or f.get("status")),
                    "averageSpeed": int(f.get("averageSpeed", 0) or 0),
                    "queuePosition": self._extract_queue_position(f),
                    "localFilePath": (
                        f.get("localFilePath")
                        or f.get("localPath")
                        or f.get("downloadedFilePath")
                        or f.get("path")
                        or ""
                    ),
                })
        return flat

    def get_active_downloads(self, timeout: Optional[int] = None) -> list[dict]:
        """
        Get list of all downloads (active and completed) from slskd.

        Returns a flat list of file dicts. Each dict includes 'id', 'username',
        'filename', 'state', 'progress', 'localFilePath', etc. so callers can
        distinguish active vs. completed transfers and cancel by the correct ID.
        """
        if not self.enabled:
            return []

        timeout = timeout or self.default_timeout

        try:
            url = f"{self.base_url}/transfers/downloads"
            resp = self.session.get(url, headers=self.headers, timeout=timeout)

            if resp.status_code != 200:
                logger.warning(f"Slskd downloads endpoint failed: {resp.status_code}")
                return []

            downloads = self._parse_transfers_response(resp.json())
            logger.debug(f"Slskd: {len(downloads)} download entries parsed")
            return downloads
        except Exception as e:
            logger.error(f"Slskd get active downloads failed: {e}")
            return []

    def get_events(self, timeout: Optional[int] = None) -> list[dict]:
        """Fetch recent slskd events (optional event-driven sync surface)."""
        if not self.enabled:
            return []

        timeout = timeout or self.default_timeout
        try:
            url = f"{self.base_url}/events"
            resp = self.session.get(url, headers=self.headers, timeout=timeout)
            if resp.status_code != 200:
                logger.debug(f"Slskd events endpoint failed: {resp.status_code}")
                return []
            payload = resp.json() or []
            if isinstance(payload, dict):
                payload = payload.get("events") or payload.get("items") or []
            return payload if isinstance(payload, list) else []
        except Exception as e:
            logger.debug(f"Slskd get events failed: {e}")
            return []

    def get_server_state(self, timeout: Optional[int] = None) -> dict:
        """Fetch slskd server state, with backward-compatible fallbacks.

        Modern slskd exposes ``GET /api/v0/server``.  Older versions nest the
        state inside ``GET /api/v0/application``.  This method tries the
        canonical endpoint first and falls back to the legacy one on 404.

        Returns:
            Parsed JSON dict (may be empty on error).
        """
        if not self.enabled:
            return {}

        timeout = timeout or self.default_timeout
        try:
            url = f"{self.base_url}/server"
            resp = self.session.get(url, headers=self.headers, timeout=timeout)
            if resp.status_code == 200:
                return resp.json() or {}
            if resp.status_code == 404:
                # Fallback for older slskd versions
                app_url = f"{self.base_url}/application"
                app_resp = self.session.get(app_url, headers=self.headers, timeout=timeout)
                if app_resp.status_code == 200:
                    app_data = app_resp.json() or {}
                    return app_data.get("server") or {}
            logger.debug(f"Slskd get_server_state failed: {resp.status_code}")
        except Exception as e:
            logger.debug(f"Slskd get_server_state error: {e}")
        return {}

    def is_connected(self, timeout: Optional[int] = None) -> bool:
        """Return True when slskd reports it is connected to the Soulseek network.

        Handles the C# enum-flags string that slskd returns in its ``state``
        field (e.g. ``"Connected, LoggedIn"``) as well as the modern
        ``isConnected`` boolean field.
        """
        if not self.enabled:
            return False

        state = self.get_server_state(timeout=timeout)
        # Modern slskd versions expose an explicit boolean
        if "isConnected" in state:
            return bool(state["isConnected"])

        raw = str(state.get("state") or state.get("status") or "").strip().lower()
        if not raw:
            return False

        # C# enum flags are comma-separated, e.g. "Connected, LoggedIn"
        flags = {f.strip() for f in raw.split(",") if f.strip()}
        return "connected" in flags

    def get_completed_transfers(self, timeout: Optional[int] = None) -> list[dict]:
        """
        Return only transfers in state 'Completed, Succeeded', each including
        'localFilePath' — the on-disk path where slskd saved the file.

        This is the preferred way to detect completed downloads without a
        filesystem walk.
        """
        return [
            t for t in self.get_active_downloads(timeout=timeout)
            if self._is_success_state(t.get("state"))
        ]
    
    def list_searches(self, timeout: Optional[int] = None) -> list[dict]:
        """
        Return all searches known to slskd.

        Returns:
            List of search dicts from slskd (each has at least 'id' and 'state').
            Returns an empty list on any error.
        """
        if not self.enabled:
            return []

        timeout = timeout or self.default_timeout

        try:
            url = f"{self.base_url}/searches"
            resp = self.session.get(url, headers=self.headers, timeout=timeout)
            if resp.status_code == 200:
                return resp.json() or []
            logger.warning(f"Slskd list searches failed: {resp.status_code} - {resp.text[:200]}")
            return []
        except Exception as e:
            logger.error(f"Slskd list searches error: {e}")
            return []

    def cancel_search(self, search_id: str, timeout: Optional[int] = None) -> bool:
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

            body_preview = (resp.text or "")[:400]
            # slskd sometimes throws DbUpdateConcurrencyException when its
            # internal finalization modifies the search row at the same time
            # we call DELETE. The row is already gone or updated — treat that
            # as success rather than a hard failure.
            if resp.status_code in [409, 500] and "concurrency" in body_preview.lower():
                logger.debug(
                    f"Slskd search {search_id} cancel hit concurrency conflict; "
                    f"treating as already removed"
                )
                return True

            logger.warning(f"Slskd search cancel failed: {resp.status_code} - {body_preview[:200]}")
            return False
        except Exception as e:
            logger.error(f"Slskd cancel search failed for {search_id}: {e}")
            return False
    
    def clear_stale_searches(self, budget_seconds: float = 8) -> None:
        """Cancel any terminal-state (or long-running stuck) searches in slskd.

        slskd enforces a single concurrent search slot; a stale completed /
        timed-out search that was never cleaned up will cause subsequent
        ``start_search()`` calls to receive HTTP 429 ("only one concurrent
        operation"), making every new search appear to time out.

        Also cancels searches that have been in an active state for longer
        than ``_STUCK_SEARCH_TIMEOUT_MS`` to clear truly stuck searches.

        ``budget_seconds`` caps the total wall-clock time so that a large
        backlog of entries cannot cause the caller to exceed a request timeout.
        """
        # slskd serialises C# SearchStates flag enums as "Completed, <suffix>".
        # The exact strings are taken from slskd's SearchStatusIcon.jsx (as of 3/26/25).
        # We also keep plain "Completed" / "Succeeded" and the legacy simple
        # strings so searches from older slskd versions (or searches that finish
        # with only the Completed flag) are still cleaned up.
        _TERMINAL_STATES = {
            "Completed, TimedOut",
            "Completed, ResponseLimitReached",
            "Completed, FileLimitReached",
            "Completed, Cancelled",
            "Completed, Errored",
            "Completed",
            "Succeeded",
            "Cancelled",
            "Errored",
            "TimedOut",
        }
        _ACTIVE_STATES = {"None", "Queued", "Requested", "InProgress", "Initializing"}

        deadline = time.monotonic() + budget_seconds
        try:
            time_left = deadline - time.monotonic()
            existing = self.list_searches(timeout=min(4, max(1, int(time_left))))
            for s in existing:
                if time.monotonic() >= deadline:
                    logger.warning("[SLSKD] Stale search cleanup budget exhausted")
                    break
                sid = s.get("id") or s.get("searchId") or s.get("Id")
                state = str(s.get("state") or s.get("State") or "")
                if not sid:
                    continue

                should_cancel = state in _TERMINAL_STATES
                if not should_cancel and state in _ACTIVE_STATES:
                    # Also cancel searches that have been running too long.
                    # slskd does not expose elapsedMilliseconds; compute from startedAt.
                    started_at = s.get("startedAt") or s.get("StartedAt") or s.get("started_at")
                    elapsed_ms = 0
                    if started_at:
                        try:
                            from datetime import datetime, timezone
                            if isinstance(started_at, str):
                                started_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                            else:
                                started_dt = started_at
                            if started_dt.tzinfo is None:
                                started_dt = started_dt.replace(tzinfo=timezone.utc)
                            elapsed_ms = int((datetime.now(timezone.utc) - started_dt).total_seconds() * 1000)
                        except Exception:
                            elapsed_ms = 0
                    if elapsed_ms > _STUCK_SEARCH_TIMEOUT_MS:
                        should_cancel = True
                        logger.info(
                            f"[SLSKD] Cancelling stuck active search {sid} "
                            f"(state={state}, elapsed={elapsed_ms}ms)"
                        )

                if should_cancel:
                    # Strict budget check before each HTTP call so a large backlog
                    # of stale searches cannot exceed the caller's timeout.
                    time_left = deadline - time.monotonic()
                    if time_left <= 0:
                        logger.warning("[SLSKD] Stale search cleanup budget exhausted")
                        break
                    cancel_timeout = max(0.5, min(2, time_left))
                    # For active searches use PUT (calls slskd TryCancel) so the
                    # underlying Soulseek operation is stopped before we delete the
                    # record.  For terminal searches DELETE is sufficient.
                    if state in _ACTIVE_STATES:
                        time_left = deadline - time.monotonic()
                        if time_left <= 0:
                            logger.warning("[SLSKD] Stale search cleanup budget exhausted")
                            break
                        try:
                            put_url = f"{self.base_url}/searches/{sid}"
                            self.session.put(put_url, headers=self.headers, timeout=cancel_timeout)
                        except Exception:
                            pass
                    time_left = deadline - time.monotonic()
                    if time_left <= 0:
                        logger.warning("[SLSKD] Stale search cleanup budget exhausted")
                        break
                    self.cancel_search(sid, timeout=cancel_timeout)
                    if state in _TERMINAL_STATES:
                        logger.info(
                            f"[SLSKD] Cleared stale search {sid} (state={state})"
                        )
        except Exception as cleanup_err:
            logger.warning(f"[SLSKD] Could not clear stale searches: {cleanup_err}")

    def cancel_download(self, username: str, transfer_id: str, remove: bool = True, timeout: Optional[int] = None) -> bool:
        """
        Cancel (and optionally remove) a specific download.

        Args:
            username: Peer username the transfer belongs to
            transfer_id: Transfer ID returned by the transfers API
            remove: If True, also remove the transfer record from slskd's list
            timeout: Request timeout (uses default_timeout if not specified)

        Returns:
            True if cancelled successfully
        """
        if not self.enabled:
            return False

        timeout = timeout or self.default_timeout

        try:
            url = (
                f"{self.base_url}/transfers/downloads"
                f"/{username}/{transfer_id}?remove={str(remove).lower()}"
            )
            resp = self.session.delete(url, headers=self.headers, timeout=timeout)

            if resp.status_code in [200, 204]:
                logger.info(f"Slskd download {transfer_id} (user={username}) cancelled successfully")
                return True
            else:
                logger.warning(f"Slskd download cancel failed: {resp.status_code} - {resp.text[:200]}")
                return False
        except Exception as e:
            logger.error(f"Slskd cancel download failed for {username}/{transfer_id}: {e}")
            return False
    
    def get_transfer(self, username: str, transfer_id: str, timeout: Optional[int] = None) -> Optional[dict]:
        """
        Get details of a specific download transfer.

        Args:
            username: Peer username the transfer belongs to
            transfer_id: Transfer ID returned by the transfers API
            timeout: Request timeout (uses default_timeout if not specified)

        Returns:
            Transfer details dict (flat, with 'username' injected) or None
        """
        if not self.enabled:
            return None

        timeout = timeout or self.default_timeout

        try:
            url = f"{self.base_url}/transfers/downloads/{username}/{transfer_id}"
            resp = self.session.get(url, headers=self.headers, timeout=timeout)

            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict):
                    data.setdefault("username", username)
                return data
            else:
                logger.debug(f"Slskd get transfer {transfer_id} failed: {resp.status_code}")
                return None
        except Exception as e:
            logger.error(f"Slskd get transfer failed for {username}/{transfer_id}: {e}")
            return None

    def find_download(self, username: str = "", filename: str = "", timeout: Optional[int] = None) -> Optional[dict]:
        """Resolve a transfer entry from the flat downloads list.

        Matching prefers exact normalized filename, then basename. When multiple
        transfers match the same basename, prefer terminal failed states so retry
        code clears the stale errored entry before re-queuing.
        """
        if not self.enabled:
            return None

        target_name = str(filename or "").strip().replace('\\', '/').lower()
        target_base = target_name.rsplit('/', 1)[-1] if target_name else ""
        target_user = str(username or "").strip()

        matches = []
        for transfer in self.get_active_downloads(timeout=timeout):
            transfer_user = str(transfer.get("username") or "").strip()
            if target_user and transfer_user != target_user:
                continue
            transfer_name = str(transfer.get("filename") or "").strip().replace('\\', '/').lower()
            transfer_base = transfer_name.rsplit('/', 1)[-1] if transfer_name else ""
            if target_name and transfer_name == target_name:
                return transfer
            if target_base and transfer_base == target_base:
                matches.append(transfer)

        if not matches:
            return None

        for transfer in matches:
            state = self._state_text(transfer.get("state") or transfer.get("transferState") or transfer.get("status"))
            if state in self.FAILED_STATES:
                return transfer

        return matches[0]

    def remove_download_by_filename(self, username: str = "", filename: str = "", timeout: Optional[int] = None) -> bool:
        """Resolve a transfer by filename and remove it from slskd."""
        transfer = self.find_download(username=username, filename=filename, timeout=timeout)
        if not transfer:
            return False

        transfer_id = str(transfer.get("id") or "")
        transfer_user = str(transfer.get("username") or username or "")
        if not transfer_id or not transfer_user:
            return False
        return self.cancel_download(transfer_user, transfer_id, remove=True, timeout=timeout)

    def clear_completed_downloads(self, timeout: Optional[int] = None) -> bool:
        """
        Remove all completed (terminal-state) download entries from slskd's list.

        Uses: DELETE /transfers/downloads/all/completed

        Returns:
            True if the request succeeded
        """
        if not self.enabled:
            return False

        timeout = timeout or self.default_timeout

        try:
            url = f"{self.base_url}/transfers/downloads/all/completed"
            resp = self.session.delete(url, headers=self.headers, timeout=timeout)

            if resp.status_code in [200, 204]:
                logger.info("Slskd: cleared all completed download entries")
                return True
            else:
                logger.warning(f"Slskd clear completed failed: {resp.status_code} - {resp.text[:200]}")
                return False
        except Exception as e:
            logger.error(f"Slskd clear completed downloads failed: {e}")
            return False


# Backward-compatible module functions
_slskd_client = None

def _get_slskd_client(web_url: str, api_key: str = "", enabled: bool = True):
    """Get or create singleton slskd client."""
    global _slskd_client
    if _slskd_client is None:
        _slskd_client = SlskdClient(web_url, api_key, enabled=enabled)
    return _slskd_client
