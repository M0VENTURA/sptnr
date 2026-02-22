# Discogs Punctuation Fix

## Problem

When matching songs with Discogs, the system struggled with tracks containing punctuation, particularly apostrophes. For example:
- "Janie's Got A Gun" by Aerosmith
- "Don't Stop Believin'" by Journey
- "What's Love Got to Do with It" by Tina Turner
- "You've Got A Friend" by James Taylor

## Root Cause

The Discogs API search was using the **original track title** (with punctuation) in the `track` parameter:

```python
params = {
    "artist": "Aerosmith",
    "track": "Janie's Got A Gun",  # Original with apostrophe
    "format": "Single"
}
```

This caused issues because:
1. Discogs may store titles with different apostrophe styles (straight `'` vs. curly `'` quotes)
2. Some entries might have no apostrophes at all
3. The API `track` parameter requires exact or near-exact matching

## Solution

The fix was to use the **normalized track title** (with punctuation removed) for the API search query:

```python
# Before: Using original title
results_single = self._discogs_search_with_format(artist, title, "Single", timeout)

# After: Using normalized title
results_single = self._discogs_search_with_format(artist, normalized_title, "Single", timeout)
```

Now the API receives:
```python
params = {
    "artist": "Aerosmith",
    "track": "janies got a gun",  # Normalized without apostrophe
    "format": "Single"
}
```

### Normalization Process

The `normalize_track_title()` function from `discogs_singles_cache.py`:
1. Converts curly quotes to straight quotes
2. Converts to lowercase
3. Removes ALL punctuation (including apostrophes, periods, commas, etc.)
4. Collapses whitespace

Examples:
- `"Janie's Got A Gun"` → `"janies got a gun"`
- `"Don't Stop Believin'"` → `"dont stop believin"`
- `"M.M.I.X."` → `"mmix"`

## Code Changes

### `api_clients/discogs.py`

Modified `_search_discogs_for_single()` method (line ~674):

```python
def _search_discogs_for_single(self, artist: str, title: str, normalized_title: str, timeout):
    """
    Search Discogs for a track as a Single or EP.
    
    Note: We use normalized_title for the API search to handle punctuation issues.
    For example, "Janie's Got A Gun" is normalized to "janies got a gun" which
    matches better in Discogs regardless of how they store apostrophes.
    """
    # Try searching with format=Single first (most common)
    # Use normalized_title to avoid issues with punctuation like apostrophes
    log_debug(f"[DISCOGS_SINGLE] Searching: artist='{artist}' track='{title}' (normalized: '{normalized_title}') format=Single")
    
    results_single = self._discogs_search_with_format(artist, normalized_title, "Single", timeout)
    # ... rest of method
```

The key change is passing `normalized_title` instead of `title` to `_discogs_search_with_format()`.

## Testing

Added comprehensive test coverage:

### `test_discogs_punctuation_fix.py`
Tests normalization and API calls with various punctuation:
- Verifies `normalize_track_title()` works correctly
- Mocks Discogs API and verifies normalized titles are sent
- Tests multiple apostrophe scenarios

### `test_discogs_apostrophe_real_scenario.py`
Demonstrates the real-world problem:
- Shows how API returns no results with original apostrophe
- Proves normalization solves the issue
- Tests the complete fix end-to-end

Both test files pass successfully, and all existing Discogs tests continue to pass.

## Impact

This fix ensures that:
1. Tracks with apostrophes can be matched in Discogs
2. Different apostrophe styles (straight, curly) are handled uniformly
3. Searches are more resilient to punctuation variations in the Discogs database
4. No regression in existing functionality

## Related Code

- **Normalization function**: `discogs_singles_cache.py:normalize_track_title()`
- **Search method**: `api_clients/discogs.py:_search_discogs_for_single()`
- **API call**: `api_clients/discogs.py:_discogs_search_with_format()`
- **Result matching**: `api_clients/discogs.py:_check_search_results()`

## Future Considerations

This fix maintains consistency with the existing normalization strategy used for result comparison. Any future changes to track matching should preserve this approach to ensure punctuation continues to be handled correctly.
