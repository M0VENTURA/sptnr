# Singles Detection Timeout and Essential Playlist Fix Summary

## Problem Statement

From the user's logs, two issues were identified:

1. **Timeout Issue**: MusicBrainz and Discogs API calls were timing out during singles detection
   ```
   2026-01-16 18:55:18,054 [INFO]    ⏱ MusicBrainz single check timed out for Lycanthrope: MusicBrainz single detection timed out after 30s
   2026-01-16 18:55:48,054 [INFO]    ⏱ Discogs single check timed out for Lycanthrope: Discogs single detection timed out after 30s
   ```

2. **Essential Playlist Issue**: Log indicated playlists were created for every artist, even when they didn't meet the requirements
   ```
   2026-01-16 19:04:20,365 [INFO]    ✓ Essential playlist created for artist: +44 (12 total tracks)
   ```
   - +44 has only 12 tracks (needs 100 for Case B) and 0 confirmed five-star singles (needs 10 for Case A)
   - Yet the log says a playlist was created

## Root Causes

### Issue 1: API Timeout
The timeout issue had two root causes:

1. **Excessive Retry Count**: The standard `session` uses 3 retries with exponential backoff
   - MusicBrainz: 3 retries × ~15s per attempt = up to 45s total
   - Discogs: 3 retries + rate limiting delays = up to 60s total
   - These exceeded the 30s timeout configured in `API_CALL_TIMEOUT`

2. **Retry Override**: MusicBrainzClient was overriding the session's retry configuration
   - Even when passed `timeout_safe_session` (1 retry), it would override it back to 3 retries
   - This happened in `_setup_retry_strategy()` which mounted a new adapter with hardcoded 3 retries

### Issue 2: Misleading Logging
The essential playlist logging issue was simpler:

- Lines 1110 and 1137 in `popularity.py` logged unconditionally
- They logged "Creating essential playlist" and "✓ Essential playlist created" for EVERY artist
- The actual `create_or_update_playlist_for_artist()` function already had proper conditional logging

## Solutions Implemented

### Solution 1: Timeout-Safe API Clients

Created dedicated timeout-safe client instances for use within the popularity scanner:

```python
# New timeout-safe client factory functions
def _get_timeout_safe_musicbrainz_client():
    """Get or create timeout-safe MusicBrainz client for use in popularity scanner."""
    global _timeout_safe_mb_client
    if _timeout_safe_mb_client is None and HAVE_MUSICBRAINZ:
        _timeout_safe_mb_client = MusicBrainzClient(http_session=timeout_safe_session, enabled=True)
    return _timeout_safe_mb_client

def _get_timeout_safe_discogs_client(token: str):
    """Get or create timeout-safe Discogs client for use in popularity scanner."""
    global _timeout_safe_discogs_clients
    if not HAVE_DISCOGS:
        return None
    if token not in _timeout_safe_discogs_clients:
        _timeout_safe_discogs_clients[token] = DiscogsClient(token, http_session=timeout_safe_session, enabled=True)
    return _timeout_safe_discogs_clients.get(token)
```

Updated all single detection calls to use these clients:

```python
# MusicBrainz single detection
mb_client = _get_timeout_safe_musicbrainz_client()
if mb_client:
    result = _run_with_timeout(
        mb_client.is_single,
        API_CALL_TIMEOUT,
        f"MusicBrainz single detection timed out after {API_CALL_TIMEOUT}s",
        title, artist
    )

# Discogs single detection
discogs_client = _get_timeout_safe_discogs_client(discogs_token)
if discogs_client:
    result = _run_with_timeout(
        lambda: discogs_client.is_single(title, artist, album_context=None),
        API_CALL_TIMEOUT,
        f"Discogs single detection timed out after {API_CALL_TIMEOUT}s"
    )

# Discogs video detection
discogs_client = _get_timeout_safe_discogs_client(discogs_token)
if discogs_client:
    result = _run_with_timeout(
        lambda: discogs_client.has_official_video(title, artist),
        API_CALL_TIMEOUT,
        f"Discogs video detection timed out after {API_CALL_TIMEOUT}s"
    )
```

Fixed MusicBrainzClient to respect pre-configured sessions:

```python
def __init__(self, http_session=None, enabled: bool = True):
    # Track if a custom session was provided (don't override its retry config)
    custom_session_provided = http_session is not None
    self.session = http_session or session
    self.enabled = enabled
    self.base_url = "https://musicbrainz.org/ws/2/"
    self.headers = {"User-Agent": "sptnr-cli/2.1 (support@example.com)"}
    # Only setup retry strategy if using default session (not a pre-configured one)
    if not custom_session_provided:
        self._setup_retry_strategy()
    self._load_cache()
```

### Solution 2: Accurate Playlist Logging

Removed the misleading unconditional logs and let the function handle its own logging:

```python
# BEFORE (lines 1109-1139):
log_unified(f'Creating essential playlist for artist: {artist}')
# ... fetch tracks ...
create_or_update_playlist_for_artist(artist, tracks_list)
log_unified(f'   ✓ Essential playlist created for artist: {artist} ({len(all_artist_tracks)} total tracks)')

# AFTER (simplified):
# Just call the function - it handles its own logging
create_or_update_playlist_for_artist(artist, tracks_list)
```

The function itself already logs appropriately:
- Case A (10+ five-star): `"Essential playlist created for '{artist}' (5★ essentials)"`
- Case B (100+ tracks): `"Essential playlist created for '{artist}' (top 10% by rating)"`
- Neither case met: `"No Essential playlist created for '{artist}' (total={total}, five★={count})"`

## Results

### Timeout Improvements

With timeout-safe sessions (1 retry), API call durations are now:
- First attempt: ~15s (5s connect + 10s read)
- Backoff delay: 0.2s
- Second attempt: ~15s
- **Total maximum: ~30s** (within the 30s timeout window)

Previously with 3 retries:
- First attempt: ~15s
- Backoff delay: 0.3s → 0.6s → 1.2s
- Retries: 15s × 3 = 45s
- **Total maximum: ~60s** (exceeded 30s timeout)

### Logging Improvements

Before:
```
2026-01-16 19:04:20,362 [INFO] Creating essential playlist for artist: +44
2026-01-16 19:04:20,365 [INFO]    ✓ Essential playlist created for artist: +44 (12 total tracks)
```

After (with 12 tracks, no 5-star singles):
```
2026-01-16 XX:XX:XX,XXX [INFO] No Essential playlist created for '+44' (total=12, five★=0)
```

After (with 120 tracks, 1 five-star single):
```
2026-01-16 XX:XX:XX,XXX [INFO] Essential playlist created for 'Artist' (top 10% by rating)
```

## Testing

### Test Coverage

1. **test_timeout_fix.py** - Validates timeout-safe client implementation
   - ✅ MusicBrainz client uses timeout_safe_session (1 retry)
   - ✅ Discogs client uses timeout_safe_session (1 retry)
   - ✅ Session retry configurations are correct (3 vs 1)
   - ✅ Regular API functions remain available (backward compatible)

2. **test_essential_playlist_fix.py** - Validates playlist creation logic
   - ✅ Case A: 10+ five-star tracks creates 5★ essentials playlist
   - ✅ Case B: 100+ total tracks creates top 10% playlist
   - ✅ No playlist created when requirements not met
   - ✅ Chiodos scenario (100+ tracks, 1 five-star) creates top 10% playlist

### Security Scan

CodeQL analysis: **0 alerts found** ✅

## Files Modified

1. **popularity.py**
   - Added timeout-safe client factory functions
   - Updated MusicBrainz/Discogs single detection calls
   - Removed misleading playlist creation logs

2. **api_clients/musicbrainz.py**
   - Modified `__init__` to skip retry setup when custom session is provided

3. **test_timeout_fix.py** (new)
   - Comprehensive test for timeout-safe client implementation

## Backward Compatibility

All changes maintain backward compatibility:
- Regular API functions (e.g., `is_musicbrainz_single()`) remain available
- Standard session with 3 retries is unchanged for non-timeout-sensitive code
- Only popularity scanner uses timeout-safe clients
- Existing code using API clients directly is unaffected

## Performance Impact

Expected improvements:
- **Reduced timeout failures**: API calls will complete within 30s window
- **Faster failure recovery**: 1 retry instead of 3 means quicker fallback when APIs are down
- **Less thread pool exhaustion**: Fewer long-running background tasks in `_timeout_executor`
- **Clearer logs**: No misleading messages about playlist creation

## Conclusion

These changes address both issues reported in the problem statement:
1. Singles detection timeouts are fixed by using timeout-safe sessions with 1 retry
2. Essential playlist logging is now accurate and only reports actual playlist creation

The fixes are minimal, surgical, and maintain full backward compatibility with existing code.
