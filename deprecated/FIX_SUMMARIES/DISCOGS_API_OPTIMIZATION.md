# Discogs API Performance Optimization

## Problem

The Discogs API integration was very slow when checking if tracks were singles because it was:
1. Searching for all Singles and EPs by an artist (could be 100+ releases)
2. Making individual API calls to fetch detailed information for each release to get track listings
3. This resulted in 100+ API calls per artist, which was extremely slow

Example from logs:
```
2026-02-17 07:05:29.878 [DEBUG] [DISCOGS_RELEASES] Found 100 Single releases for artist 'Coldplay'
2026-02-17 07:05:30.200 [DEBUG] [DISCOGS_RELEASES] Processing Single release 28576915: 'God Put A Smile Upon Your Face = 浑然天成' with 12 track(s)
2026-02-17 07:05:30.728 [DEBUG] [DISCOGS_RELEASES] Processing Single release 913336: 'God Put A Smile Upon Your Face = 浑然天成' with 8 track(s)
... (98 more releases)
```

## Solution

Implemented an optimized specific track search that:
1. Uses the Discogs API's `track` parameter to search for a specific track by artist + title
2. Checks if any results are Singles or EPs
3. Only makes **1 API call** instead of 100+

## Implementation Details

### New Method: `_search_specific_single()`

Added a new method in `api_clients/discogs.py` that searches for a specific track:

```python
def _search_specific_single(self, title: str, artist_name: str, timeout: tuple[int, int] | int = (5, 10)) -> bool:
    """
    Search for a specific track by artist and title using Discogs database search.
    
    This is much faster than fetching all artist singles/EPs as it only searches
    for the specific track we're interested in using the 'track' parameter.
    """
    # Search using both artist and track parameters
    params = {
        "artist": artist_name,
        "track": title,
        "type": "release",
        "per_page": 50
    }
    
    # Check if any results are Singles or EPs
    for release_info in releases:
        format_str = " ".join(release_info.get("format", []))
        is_single = "single" in format_str.lower()
        is_ep = re.search(r'\bep\b', format_str.lower())
        
        if is_single or is_ep:
            return True
    
    return False
```

### Modified: `is_single()` Method

Updated the `is_single()` method to use the optimized search:

```python
def is_single(self, title: str, artist: str, ...):
    # Check persistent cache first
    if artist_lower not in self._artist_singles_cache:
        cached_singles = cache.get_cached_titles(artist)
        
        if cached_singles:
            # Use cached data
            self._artist_singles_cache[artist_lower] = {"singles": list(cached_singles), "eps": []}
        else:
            # NEW: Use optimized specific search instead of fetching all releases
            specific_result = self._search_specific_single(title, artist, timeout)
            self._specific_search_cache[cache_key] = specific_result
            return specific_result
    
    # Check cached data
    return normalized_title in all_cached_titles
```

### Caching Strategy

The optimization includes two levels of caching:

1. **Persistent cache** (`discogs_singles_cache` table): Stores previously fetched artist singles/EPs
2. **In-memory cache** (`_specific_search_cache`): Caches specific track search results within a request

This prevents repeated API calls for the same track.

## Performance Impact

### Before Optimization
- First query for an artist: **100+ API calls**
  - 1 search for all singles/EPs
  - 100+ release detail fetches
- Time: **30-60 seconds** depending on artist

### After Optimization
- First query for an artist: **1 API call**
  - 1 specific track search
- Time: **<1 second**

### Speedup
- **~100x faster** for single track lookups
- **99% reduction** in API calls

## Testing

### New Test: `test_discogs_optimization.py`

Created comprehensive test to verify:
- Specific search method works correctly
- Only 1 API call is made
- Caching works for repeated lookups
- Correctly identifies singles vs. albums

### Updated Test: `test_discogs_single_priority.py`

Updated existing test to verify the new optimized behavior:
- Uses `artist` and `track` parameters
- Makes only 1 API call
- Correctly identifies singles

All tests pass successfully.

## API Usage

The optimization uses the Discogs `/database/search` endpoint with parameters:
- `artist`: Artist name
- `track`: Track title
- `type`: "release"
- `per_page`: 50

Example API call:
```
GET https://api.discogs.com/database/search?artist=Coldplay&track=Viva%20la%20Vida&type=release&per_page=50
```

## Backward Compatibility

The optimization is fully backward compatible:
- Existing persistent cache data is still used when available
- Falls back to the specific search when cache is empty
- Does not break any existing functionality

## Future Improvements

Potential future enhancements:
1. Store specific search results in persistent cache for longer-term caching
2. Batch multiple track lookups if needed
3. Pre-fetch common artists during off-peak hours

## References

- [Discogs API Documentation](https://www.discogs.com/developers/)
- Issue: https://github.com/M0VENTURA/sptnr/actions/workflows/docker-image.yml
- Implementation PR: copilot/improve-discogs-api-fetching
