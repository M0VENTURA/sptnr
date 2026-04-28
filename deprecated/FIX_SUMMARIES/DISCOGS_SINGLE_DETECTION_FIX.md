# Discogs Single Detection Fix - Summary

## Problem Statement
The Discogs `is_single()` method was failing to detect known singles for tracks like "Viva la Vida", "Lost!", "Violet Hill", and "Strawberry Swing" by Coldplay. While other sources (Spotify, MusicBrainz) correctly identified these as singles, and Discogs Video found their music videos, the Discogs single detection itself was returning False.

This resulted in log output like:
```
★★★★★ Coldplay - Viva la Vida (Spotify, MusicBrainz, Discogs Video)
★★★★★ Coldplay - Lost! (Spotify, MusicBrainz, Discogs Video)
★★★★★ Coldplay - Violet Hill (Spotify, MusicBrainz, Discogs Video)
★★★★★ Coldplay - Strawberry Swing (Spotify, MusicBrainz, Discogs Video, Version_count)
```

Notice "Discogs" is missing from the sources, even though "Discogs Video" is present.

## Root Cause Analysis
The issue was in the search strategy in `api_clients/discogs.py`:

### Before (Incorrect Order):
1. **First search**: WITHOUT format filter → returns ALL release types (albums, singles, EPs, compilations)
2. **Result processing**: Albums appear first in results and get filtered out
3. **Fallback search**: Only executed if first search returned NO results
4. **Outcome**: Since first search DID return results (albums), it never tried the filtered search

### The Problem:
- Search for "Coldplay Viva la Vida" returns the full album "Viva la Vida or Death and All His Friends" as the top result
- Code filters out albums (line 481-482: `if "album" in names or "album" in descs: continue`)
- Code only checks first 10 results (line 464: `for r in results[:10]`)
- Single release may not be in first 10 results if albums appear first
- Fallback to format filter never happens because first search returned results

## Solution
Reversed the search order to prioritize singles:

### After (Correct Order):
1. **First search**: WITH "Single, EP" format filter → prioritizes actual single releases
2. **Result processing**: Singles/EPs appear first
3. **Fallback search**: Executed if filtered search returns NO results (for non-standard releases)
4. **Outcome**: Finds actual single releases instead of album appearances

## Code Changes

### File: `api_clients/discogs.py`

**Before:**
```python
# Try search without format filter first
params = {
    "q": f"{artist} {title}", 
    "type": "release", 
    "per_page": 15
}
results = make_discogs_search_request(params)

# If no results without filter, try with format filter as fallback
if not results:
    _throttle_discogs()
    fallback_params = {**params, "format": "Single, EP"}
    results = make_discogs_search_request(fallback_params)
```

**After:**
```python
# Try search WITH format filter first (to prioritize singles/EPs over albums)
params = {
    "q": f"{artist} {title}", 
    "type": "release", 
    "per_page": 15,
    "format": "Single, EP"  # Prioritize singles and EPs
}
results = make_discogs_search_request(params)

# If no results with filter, try without filter as fallback (for non-standard releases)
if not results:
    _throttle_discogs()
    fallback_params = {
        "q": params["q"],
        "type": params["type"],
        "per_page": params["per_page"]
    }
    results = make_discogs_search_request(fallback_params)
```

## Testing

### New Tests (`test_discogs_single_priority.py`)
1. **test_search_uses_format_filter_first**: Verifies format filter is applied in first search
2. **test_fallback_to_unfiltered_search**: Verifies fallback works when filtered search returns nothing
3. **test_single_found_in_first_search**: Verifies singles are correctly detected

### Existing Tests
All existing tests continue to pass:
- `test_fix_detection_issues.py`: All 13 tests pass
- `test_discogs_integration.py`: All tests pass
- Other Discogs-related tests: All pass

## Expected Outcome
After this fix, the log output should now show:
```
★★★★★ Coldplay - Viva la Vida (Spotify, MusicBrainz, Discogs, Discogs Video)
★★★★★ Coldplay - Lost! (Spotify, MusicBrainz, Discogs, Discogs Video)
★★★★★ Coldplay - Violet Hill (Spotify, MusicBrainz, Discogs, Discogs Video)
★★★★★ Coldplay - Strawberry Swing (Spotify, MusicBrainz, Discogs, Discogs Video, Version_count)
```

Notice "Discogs" is now included in the sources.

## Impact
- **Positive**: Discogs single detection now works correctly for known singles
- **No Breaking Changes**: Fallback to unfiltered search ensures non-standard releases still work
- **No Performance Impact**: Same number of API calls (1 search + individual release fetches)
- **Security**: No security vulnerabilities (CodeQL scan passed)

## Related Files
- `api_clients/discogs.py`: Main fix
- `test_discogs_single_priority.py`: New tests
- `test_fix_detection_issues.py`: Existing tests (all still pass)
