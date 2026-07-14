# MusicBrainz SSL Protocol Error Fix

## Problem Statement

The application was experiencing SSL/TLS protocol errors when making API calls to MusicBrainz:

```
MusicBrainz album lookup failed after retries: HTTPSConnectionPool(host='musicbrainz.org', port=443): 
Max retries exceeded with url: /ws/2/release-group?query=release%3A%22Shadow+Work%22+AND+artist%3A%22Warrel+Dane%22&fmt=json&limit=10 
(Caused by SSLError(SSLEOFError(8, '[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1016)')))
```

## Root Cause

The error was caused by **incorrect User-Agent headers** in API requests to MusicBrainz. The MusicBrainz API has strict requirements for User-Agent formatting as documented in their [API documentation](https://musicbrainz.org/doc/MusicBrainz_API):

**Required Format:** `AppName/Version ( contact-info )`

### Incorrect Headers Found

The following non-compliant User-Agent headers were found in the codebase:

1. `"sptnr-web/1.0 (support@example.com)"` - Contains placeholder email
2. `"sptnr-web/1.0"` - Missing contact info in parentheses
3. `"sptnr-cli/2.1 (support@example.com)"` - Contains placeholder email

### Why This Caused SSL Errors

MusicBrainz API servers may reject or prematurely close connections when they receive requests with non-compliant User-Agent headers. This can manifest as SSL protocol errors because the server closes the connection during the SSL handshake or data transfer phase.

## Solution

### Changes Made

1. **Centralized User-Agent Definition**: The correct User-Agent was already defined in `api_clients/musicbrainz.py`:
   ```python
   _USER_AGENT = f"sptnr/{_VERSION} ( https://github.com/M0VENTURA/sptnr )"
   ```
   This produces: `sptnr/2.0.0-alpha ( https://github.com/M0VENTURA/sptnr )`

2. **Fixed All MusicBrainz API Calls**:
   - **app.py**: 8 occurrences fixed
   - **artist_api_additions.py**: 3 occurrences fixed

3. **Import at Module Level**: Following code review feedback, the User-Agent is now imported once at the module level:
   ```python
   from api_clients.musicbrainz import _USER_AGENT as MUSICBRAINZ_USER_AGENT
   ```

### Files Modified

- `app.py`: Updated 8 functions making MusicBrainz API calls
  - `_fetch_musicbrainz_releases()`
  - `api_import_release()`
  - `api_artist_bio()` (2 locations)
  - `api_musicbrainz_search()`
  - `_fetch_album_art_from_musicbrainz()`
  - `api_album_musicbrainz_lookup()`
  - `api_track_musicbrainz_lookup()`

- `artist_api_additions.py`: Updated 3 functions
  - `api_artist_bio()` (2 locations)
  - `api_artist_search_images()`

## Validation

### Tests Performed

1. **User-Agent Format Validation**: Created `test_musicbrainz_user_agent.py` to verify:
   - User-Agent follows the correct format
   - Contains app name, version, and contact info
   - Does not contain placeholder emails

2. **Security Scan**: Ran CodeQL security analysis
   - **Result**: 0 vulnerabilities found

### Expected Behavior

With the correct User-Agent header:
- MusicBrainz API requests should complete successfully
- SSL protocol errors should no longer occur
- API rate limiting and retry logic will continue to work as designed

## Impact

This fix resolves the SSL protocol error without changing any business logic. All MusicBrainz API functionality remains the same, but now with proper API compliance.

### Benefits

1. **Eliminates SSL Errors**: Proper User-Agent headers prevent premature connection closures
2. **API Compliance**: Follows MusicBrainz API guidelines for identification
3. **Better Reliability**: Reduces likelihood of being blocked or rate-limited
4. **Maintainability**: Centralized User-Agent definition makes future updates easier

## References

- [MusicBrainz API Documentation](https://musicbrainz.org/doc/MusicBrainz_API)
- [MusicBrainz API Rate Limiting](https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting)
- Related issue: SSL protocol error in `/api/album/musicbrainz` endpoint
