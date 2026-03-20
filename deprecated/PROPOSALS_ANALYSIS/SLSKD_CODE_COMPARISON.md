# Soulseek (slskd) API Implementation Comparison

## Overview

This document compares the current SPTNR implementation of the slskd API client (`api_clients/slskd.py`) with the official slskd API documentation and best practices.

**Reference**: https://github.com/slskd/slskd  
**API Version**: v0 (REST API)  
**Implementation Date**: 2026-02-18

---

## Executive Summary

✅ **Overall Assessment**: The current implementation is **excellent** and follows slskd API best practices.

### Key Findings:
- ✅ Correct API endpoint structure (`/api/v0/*`)
- ✅ Proper authentication using `X-API-Key` header
- ✅ Comprehensive error handling and logging
- ✅ Correct data structures for search and download operations
- ✅ Quality filtering with bitrate/sample rate checks
- ✅ Batch download support with username grouping
- ⚠️ Minor: Could benefit from transfer status monitoring
- ⚠️ Minor: Could add search cancellation support

---

## API Endpoint Comparison

### 1. Search Endpoints

#### ✅ Start Search - `POST /api/v0/searches`

**Official API**:
```json
POST /api/v0/searches
Body: {"searchText": "artist album track"}
Response: {"id": "search-id", "state": "InProgress", ...}
```

**SPTNR Implementation** (`slskd.py:95-129`):
```python
def start_search(self, query: str, timeout: int = 10) -> Optional[str]:
    url = f"{self.base_url}/searches"
    data = {"searchText": query}
    resp = self.session.post(url, json=data, headers=self.headers, timeout=timeout)
    # Returns search_id
```

**Status**: ✅ **Correct** - Uses proper field name `searchText` and returns search ID.

---

#### ✅ Get Search Results - `GET /api/v0/searches/{id}`

**Official API**:
```json
GET /api/v0/searches/{id}
Response: {"id": "...", "state": "InProgress|Completed|...", ...}
```

**SPTNR Implementation** (`slskd.py:131-205`):
```python
def get_search_results(self, search_id: str, timeout: int = 10):
    # Gets state from /searches/{id}
    state_url = f"{self.base_url}/searches/{search_id}"
    state_resp = self.session.get(state_url, headers=self.headers, timeout=timeout)
    state = state_data.get("state", "InProgress")
    
    # Gets results from /searches/{id}/responses
    responses_url = f"{self.base_url}/searches/{search_id}/responses"
    resp = self.session.get(responses_url, headers=self.headers, timeout=timeout)
```

**Status**: ✅ **Correct** - Properly uses both endpoints:
- `/searches/{id}` for state
- `/searches/{id}/responses` for actual results

This is the **recommended approach** per slskd documentation.

---

### 2. Download Endpoints

#### ✅ Download Files - `POST /api/v0/transfers/downloads/{username}`

**Official API**:
```json
POST /api/v0/transfers/downloads/{username}
Body: [{"filename": "/path/file.mp3", "size": 12345}, ...]
Response: 201 Created (or 200 OK)
```

**SPTNR Implementation** (`slskd.py:207-246`):
```python
def download_file(self, username: str, filename: str, size: int = 0):
    url = f"{self.base_url}/transfers/downloads/{username}"
    data = [{"filename": filename, "size": size}]
    resp = self.session.post(url, json=data, headers=self.headers, timeout=timeout)
    return resp.status_code in [200, 201, 204]
```

**Status**: ✅ **Correct** - Uses proper endpoint and payload format.

**Batch Downloads** (`slskd.py:248-304`):
```python
def download_files(self, files: list[dict], timeout: int = 10):
    # Groups files by username (required by slskd API)
    grouped: dict[str, list[dict]] = {}
    for entry in files:
        username = entry.get("username")
        grouped.setdefault(username, []).append({
            "filename": filename,
            "size": int(entry.get("size") or 0)
        })
    
    # Sends per-user batch requests
    for username, payload in grouped.items():
        url = f"{self.base_url}/transfers/downloads/{username}"
        resp = self.session.post(url, json=payload, headers=self.headers, timeout=timeout)
```

**Status**: ✅ **Excellent** - Correctly groups files by username before sending. This is the **proper way** to batch downloads with slskd, as the API expects per-user arrays.

---

#### ✅ Get Active Downloads - `GET /api/v0/transfers/downloads`

**Official API**:
```json
GET /api/v0/transfers/downloads
Response: [
  {
    "username": "user1",
    "filename": "/path/file.mp3",
    "size": 12345,
    "bytesTransferred": 5000,
    "state": "InProgress|Completed|...",
    "averageSpeed": 1234
  },
  ...
]
```

**SPTNR Implementation** (`slskd.py:402-461`):
```python
def get_active_downloads(self, timeout: int = 10):
    url = f"{self.base_url}/transfers/downloads"
    resp = self.session.get(url, headers=self.headers, timeout=timeout)
    # Parses and calculates progress percentage
    for download in raw_downloads:
        progress = (bytes_transferred / size) * 100 if size > 0 else 0
        downloads.append({
            "username": username,
            "filename": filename,
            "progress": progress,
            ...
        })
```

**Status**: ✅ **Correct** - Properly fetches and parses download status.

---

## Data Structure Comparison

### SearchFile Class

**SPTNR Implementation** (`slskd.py:12-50`):
```python
@dataclass
class SearchFile:
    filename: str
    size: int
    bitrate: int
    sample_rate: int
    length: int
    
    @property
    def size_mb(self) -> float:
        return self.size / (1024 * 1024)
    
    @property
    def duration_formatted(self) -> str:
        minutes = self.length // 60
        seconds = self.length % 60
        return f"{minutes}:{seconds:02d}"
```

**Status**: ✅ **Good Design** - Uses Python dataclass with:
- Type hints for all fields
- Computed properties for convenience
- Proper field validation in `__post_init__`

### SearchResponse Class

**SPTNR Implementation** (`slskd.py:52-73`):
```python
@dataclass
class SearchResponse:
    username: str
    files: list[SearchFile]
    
    def __post_init__(self):
        # Converts raw dicts to SearchFile objects
        if self.files and isinstance(self.files[0], dict):
            self.files = [SearchFile(...) for f in self.files]
```

**Status**: ✅ **Excellent** - Handles both dict and object formats gracefully.

---

## Feature Comparison

| Feature | slskd API | SPTNR Implementation | Status |
|---------|-----------|----------------------|--------|
| Start search | ✅ | ✅ | Implemented |
| Poll search state | ✅ | ✅ | Implemented |
| Get search responses | ✅ | ✅ | Implemented |
| Download single file | ✅ | ✅ | Implemented |
| Batch download | ✅ | ✅ | Implemented (with grouping) |
| Monitor downloads | ✅ | ✅ | Implemented |
| Cancel download | ✅ | ❌ | **Not implemented** |
| Cancel search | ✅ | ❌ | **Not implemented** |
| Get transfer by ID | ✅ | ❌ | **Not implemented** |
| Upload management | ✅ | ❌ | Not needed for SPTNR |
| Chat/messaging | ✅ | ❌ | Not needed for SPTNR |
| Browse shares | ✅ | ❌ | Not needed for SPTNR |

---

## Queue Processor Comparison

### Current Implementation (`queue_processor.py`)

**Workflow**:
1. Get queued items from database
2. Start search with `client.start_search()`
3. Poll up to 15 times with 1-second intervals
4. Download first file from first user
5. Monitor `/downloads` folder for completion
6. Update database status

**Status**: ✅ **Solid Implementation** with:
- Proper retry logic with exponential backoff
- Comprehensive logging with ✓/✗ symbols
- NULL-safe SQL queries (fixed in QUEUE_PROCESSOR_FIX.md)
- Automatic file matching and organization

---

## Best Practices Comparison

### ✅ Authentication
- **slskd**: Supports `X-API-Key` header or basic auth
- **SPTNR**: Uses `X-API-Key` header ✅

### ✅ Error Handling
- **Best Practice**: Check HTTP status codes and handle errors gracefully
- **SPTNR**: Comprehensive try/catch blocks with logging ✅

### ✅ Polling Strategy
- **Best Practice**: Poll search results with reasonable intervals
- **SPTNR**: 1-second intervals for up to 15 seconds ✅

### ✅ Batch Operations
- **Best Practice**: Group files by username for batch downloads
- **SPTNR**: Correctly groups files before sending ✅

### ⚠️ Timeouts
- **Best Practice**: Use reasonable timeouts for all API calls
- **SPTNR**: Uses 10-second default timeout ⚠️ (Could be configurable)

### ⚠️ Rate Limiting
- **Best Practice**: Respect API rate limits
- **SPTNR**: No explicit rate limiting ⚠️ (But uses 30-second processing interval)

---

## Recommendations

### High Priority: None
The current implementation is production-ready and follows best practices.

### Medium Priority

#### 1. Add Transfer Management
```python
def get_transfer(self, transfer_id: str, timeout: int = 10) -> Optional[dict]:
    """Get details of a specific transfer by ID."""
    url = f"{self.base_url}/transfers/{transfer_id}"
    resp = self.session.get(url, headers=self.headers, timeout=timeout)
    if resp.status_code == 200:
        return resp.json()
    return None

def cancel_download(self, transfer_id: str, timeout: int = 10) -> bool:
    """Cancel a specific download by transfer ID."""
    url = f"{self.base_url}/transfers/{transfer_id}"
    resp = self.session.delete(url, headers=self.headers, timeout=timeout)
    return resp.status_code in [200, 204]
```

**Benefit**: Allow users to cancel stuck or unwanted downloads.

#### 2. Add Search Cancellation
```python
def cancel_search(self, search_id: str, timeout: int = 10) -> bool:
    """Cancel an active search."""
    url = f"{self.base_url}/searches/{search_id}"
    resp = self.session.delete(url, headers=self.headers, timeout=timeout)
    return resp.status_code in [200, 204]
```

**Benefit**: Free up resources when searches are no longer needed.

### Low Priority

#### 3. Configurable Timeouts
```python
class SlskdClient:
    def __init__(self, web_url: str, api_key: str = "", 
                 default_timeout: int = 10, enabled: bool = True):
        self.default_timeout = default_timeout
```

**Benefit**: Allow users to adjust timeouts based on network conditions.

#### 4. Rate Limiting
```python
from time import sleep
from datetime import datetime, timedelta

class SlskdClient:
    def __init__(self, ...):
        self._last_request_time = None
        self._min_request_interval = 0.1  # 100ms between requests
    
    def _rate_limit(self):
        if self._last_request_time:
            elapsed = (datetime.now() - self._last_request_time).total_seconds()
            if elapsed < self._min_request_interval:
                sleep(self._min_request_interval - elapsed)
        self._last_request_time = datetime.now()
```

**Benefit**: Protect slskd from being overwhelmed by rapid requests.

---

## Testing Recommendations

### Unit Tests (Not Currently Present)

```python
def test_start_search():
    client = SlskdClient("http://localhost:5030", "test-key")
    search_id = client.start_search("test query")
    assert search_id is not None
    assert isinstance(search_id, str)

def test_download_file():
    client = SlskdClient("http://localhost:5030", "test-key")
    result = client.download_file("testuser", "/path/file.mp3", 12345)
    assert result is True

def test_batch_download_grouping():
    client = SlskdClient("http://localhost:5030", "test-key")
    files = [
        {"username": "user1", "filename": "/file1.mp3", "size": 1000},
        {"username": "user1", "filename": "/file2.mp3", "size": 2000},
        {"username": "user2", "filename": "/file3.mp3", "size": 3000},
    ]
    # Should group into 2 API calls (one per user)
    results = client.download_files(files)
    assert len(results) == 2
```

### Integration Tests

```python
def test_search_workflow():
    """Test complete search → download workflow"""
    client = SlskdClient("http://localhost:5030", "real-api-key")
    
    # Start search
    search_id = client.start_search("test query")
    assert search_id
    
    # Poll for results
    responses, state, is_complete = client.get_search_results(search_id)
    assert state in ["InProgress", "Completed"]
    
    # Download if results found
    if responses:
        best_file = responses[0].files[0]
        success = client.download_file(
            responses[0].username,
            best_file.filename,
            best_file.size
        )
        assert success
```

---

## Comparison with Official Python Package

The official `slskd-api` Python package ([PyPI](https://pypi.org/project/slskd-api/)) provides similar functionality:

```python
from slskd_api import SlskdClient as OfficialClient

# Official package structure
client = OfficialClient(host="localhost", port=5030, api_key="key")
client.searches.post(search_text="query")
client.transfers.downloads.username("user").post(files=[...])
```

**SPTNR vs Official Package**:
- **SPTNR**: Custom implementation, more control, fewer dependencies ✅
- **Official**: More features (chat, browse, etc.), maintained by slskd team

**Recommendation**: **Keep current custom implementation** because:
1. SPTNR only needs search/download features
2. Custom code is simpler and easier to maintain
3. No need for extra dependencies
4. Current implementation is already correct

---

## Security Considerations

### ✅ API Key Storage
- **Current**: API key stored in `config.yml` ✅
- **Recommendation**: Ensure config file has restricted permissions (600)

### ✅ HTTPS Support
- **Current**: Supports HTTPS via `web_url` configuration ✅
- **Recommendation**: Document HTTPS setup for production deployments

### ✅ Input Validation
- **Current**: Validates search queries and file paths ✅
- **Recommendation**: Add size limits for search queries (prevent abuse)

---

## Conclusion

**The current SPTNR implementation of the slskd API client is excellent and follows best practices.** It correctly uses:

1. ✅ Proper API endpoints and HTTP methods
2. ✅ Correct authentication with `X-API-Key`
3. ✅ Appropriate data structures (dataclasses)
4. ✅ Comprehensive error handling
5. ✅ Batch download optimization with username grouping
6. ✅ Quality filtering based on bitrate/sample rate
7. ✅ Detailed logging for debugging

**No critical changes are needed.** The implementation is production-ready and handles the search/download workflow correctly.

### Optional Improvements (Medium Priority):
1. Add transfer/download cancellation support
2. Add search cancellation support
3. Make timeouts configurable
4. Add basic rate limiting

### Long-term Enhancements:
1. Add unit tests for API client
2. Add integration tests with mock slskd server
3. Document HTTPS/reverse proxy setup
4. Add API client usage examples

---

## References

- slskd GitHub: https://github.com/slskd/slskd
- slskd Docker Hub: https://hub.docker.com/r/slskd/slskd
- slskd API Python Package: https://pypi.org/project/slskd-api/
- slskd API Documentation: https://slskd-api.readthedocs.io/
- SPTNR Queue Processor Fix: QUEUE_PROCESSOR_FIX.md
- SPTNR Download Queue System: DOWNLOAD_QUEUE_SYSTEM.md
