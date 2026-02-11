# Comprehensive Metadata Timeout Fix

## Problem Statement

From the GitHub Actions CI logs, comprehensive metadata fetching was timing out:

```
2026-02-11 17:54:46,199 [DEBUG] sptnr_Timeout error: Comprehensive metadata fetch timed out after 30s
```

## Root Cause

The `fetch_and_store_track_metadata()` function in `spotify_metadata_fetcher.py` was making **4 sequential Spotify API calls**:

1. Track metadata (`get_track_metadata`) - ~30.5s max
2. Audio features (`get_audio_features`) - ~30.5s max
3. Artist metadata (`get_artist_metadata`) - ~30.5s max
4. Album metadata (`get_album_metadata`) - ~30.5s max

**Total worst case**: ~122 seconds (4 × 30.5s)

However, the timeout was set to only **30 seconds** (`API_CALL_TIMEOUT`), causing frequent timeouts.

### Why Each Call Takes 30.5s

Per `api_clients/__init__.py`, the `timeout_safe_session` uses:
- 1 retry maximum
- 0.5s backoff delay
- 15s per attempt (5s connect + 10s read timeout)

Calculation:
- First attempt: 15s
- Retry delay: 0.5s
- Second attempt: 15s
- **Total**: ~30.5s per API call

## Solution Implemented

### 1. Parallel API Calls

Modified `spotify_metadata_fetcher.py` to fetch independent API calls in parallel:

```python
# Fetch track metadata first (needed to get album_id and artist_id)
track_meta = self.client.get_track_metadata(track_id)

# Then fetch the other 3 calls in parallel using ThreadPoolExecutor
with ThreadPoolExecutor(max_workers=3) as executor:
    futures = {}
    futures['audio'] = executor.submit(self.client.get_audio_features, track_id)
    if artist_id:
        futures['artist'] = executor.submit(self.client.get_artist_metadata, artist_id)
    if album_id:
        futures['album'] = executor.submit(self.client.get_album_metadata, album_id)
    
    # Collect results with timeout
    for key, future in futures.items():
        result = future.result(timeout=35)
        # ... store results
```

**Time reduction**:
- Before: ~122s (4 sequential calls)
- After: ~61s (1 initial + max of 3 parallel calls)

### 2. Dedicated Timeout Configuration

Added a new timeout constant specifically for comprehensive metadata fetching:

```python
# In popularity.py
COMPREHENSIVE_METADATA_TIMEOUT = int(os.environ.get("COMPREHENSIVE_METADATA_TIMEOUT", "90"))
```

This gives the function enough time to complete even in worst-case scenarios:
- Track metadata: ~30.5s
- Max of (audio, artist, album): ~30.5s
- **Total**: ~61s (well within 90s timeout)

Updated the call site to use this new timeout:

```python
metadata_fetched = _run_with_timeout(
    fetch_comprehensive_metadata,
    COMPREHENSIVE_METADATA_TIMEOUT,  # Was: API_CALL_TIMEOUT (30s)
    f"Comprehensive metadata fetch timed out after {COMPREHENSIVE_METADATA_TIMEOUT}s",
    db_track_id=track_id,
    spotify_track_id=spotify_track_id,
    force_refresh=force
)
```

## Benefits

1. **Eliminates Timeouts**: 90s timeout is sufficient for parallel API calls
2. **Faster Execution**: ~50% reduction in metadata fetch time (122s → 61s)
3. **Better Resource Usage**: Parallel execution maximizes throughput
4. **Configurable**: Can adjust timeout via environment variable if needed

## Configuration

The timeout can be adjusted via environment variable:

```bash
# Default is 90s
export COMPREHENSIVE_METADATA_TIMEOUT=120

# Or in docker-compose.yml:
environment:
  - COMPREHENSIVE_METADATA_TIMEOUT=120
```

## Testing

Created `test_comprehensive_metadata_timeout.py` to verify:

1. **Parallel execution**: Confirms 3 API calls complete in ~10s instead of ~40s
2. **Timeout configuration**: Verifies `COMPREHENSIVE_METADATA_TIMEOUT` is properly set

Test results:
```
✅ PASS: Parallel execution detected (10.0s < 25s)
✅ PASS: Timeout is 90s (>= 90s minimum)
```

## Files Modified

1. **spotify_metadata_fetcher.py**
   - Added `ThreadPoolExecutor` import
   - Refactored `fetch_and_store_track_metadata()` to use parallel API calls
   - Added individual call timeout (35s) for safety

2. **popularity.py**
   - Added `COMPREHENSIVE_METADATA_TIMEOUT` constant (90s default)
   - Updated comprehensive metadata call to use new timeout

3. **documentation/SPOTIFY_METADATA_FEATURES.md**
   - Updated performance optimization section
   - Documented parallel fetching and timeout management

4. **test_comprehensive_metadata_timeout.py** (new)
   - Test for parallel execution
   - Test for timeout configuration

## Backward Compatibility

✅ All changes maintain backward compatibility:
- No API changes
- Environment variable is optional (uses sensible default)
- Parallel execution is transparent to callers
- Error handling remains the same

## Performance Impact

Expected improvements:
- ✅ **50% faster metadata fetching** (122s → 61s worst case)
- ✅ **Zero timeout failures** (90s timeout vs 61s worst case)
- ✅ **Better API utilization** (parallel requests)
- ✅ **Reduced scan time** for large libraries

## Conclusion

This fix addresses the timeout errors reported in the CI logs by:
1. Making independent API calls in parallel (2x speedup)
2. Using an appropriate timeout (90s) for the multi-step operation

The changes are minimal, surgical, and maintain full backward compatibility while significantly improving performance.
