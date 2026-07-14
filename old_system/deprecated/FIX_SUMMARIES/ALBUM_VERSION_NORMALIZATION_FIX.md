# Album Version Normalization Fix

**Date:** 2026-02-10  
**PR:** #257  
**Status:** ✅ COMPLETE

## Problem

Albums with version suffixes like "(2021 version)", "(Deluxe Edition)", or "(Remastered)" were not getting proper song ratings. The issue was that when searching Spotify for track popularity, the album name was being passed with the version suffix, which prevented accurate matching.

### Example from Issue

```
2026-02-11 00:58:29,472 [INFO] Popularity Scan - Popularity Scanning for Helix (2021 version) Complete
2026-02-11 01:02:24,792 [INFO] Single Detection Scan - Singles Detected in Amaranthe - Helix (2021 version)
2026-02-11 01:02:24,792 [INFO] Single Detection Scan - ★★★★  Amaranthe - Inferno (Discogs)
2026-02-11 01:02:24,794 [INFO] Single Detection Scan - ★★★★  Amaranthe - 365 (Discogs)
```

**Expected:** Songs from "Helix (2021 version)" should match with "Helix" on Spotify and get proper 5-star ratings.

**Actual:** Songs were not getting 5-star ratings because the album version suffix prevented matching.

## Root Cause

The Spotify search API call in `popularity.py` was passing the album name directly from the database, including version suffixes like "(2021 version)". This made it harder for Spotify to match the tracks because Spotify might have the album as just "Helix" without the version suffix.

## Solution

Created a new `normalize_album()` function in `matching_utils.py` that removes common version/edition suffixes from album names before searching. This normalization is applied when calling Spotify's search API.

### Implementation Details

#### 1. New Function: `normalize_album()` in `matching_utils.py`

Removes the following patterns:
- **Year-based suffixes**: `(2021 version)`, `(2020 remaster)`, `(2019 edition)`
- **Edition keywords**: `deluxe`, `expanded`, `reissue`, `anniversary`, `special edition`, etc.
- **Remaster indicators**: `remaster`, `remastered`
- **Bonus content**: `bonus tracks`, `bonus edition`
- **Anniversary editions**: `(10th Anniversary Edition)`, etc.

**Key Features:**
- Only removes suffixes in parentheses or after dashes (to avoid false positives)
- Preserves album names where the keyword is part of the actual name (e.g., "The Deluxe" remains "The Deluxe")
- Uses the same `normalize_string()` base function for consistency

#### 2. Updated Spotify Search in `popularity.py`

```python
# Before
spotify_search_results = _run_with_timeout(
    search_spotify_track,
    API_CALL_TIMEOUT,
    f"Spotify track search timed out after {API_CALL_TIMEOUT}s",
    title, artist, album
)

# After
normalized_album = normalize_album(album) if album else None
spotify_search_results = _run_with_timeout(
    search_spotify_track,
    API_CALL_TIMEOUT,
    f"Spotify track search timed out after {API_CALL_TIMEOUT}s",
    title, artist, normalized_album
)
```

#### 3. Updated Track Similarity Calculation

The `calculate_track_similarity()` function in `matching_utils.py` now uses `normalize_album()` instead of `normalize_string()` for album comparisons, ensuring consistent normalization across the codebase.

## Test Coverage

Created comprehensive test suite in `test_album_normalization.py`:

### Normalization Tests (26 test cases)
- ✅ Year-based version suffixes
- ✅ Deluxe/Special/Limited editions
- ✅ Remastered/Reissue variations
- ✅ Anniversary editions (including "10th Anniversary")
- ✅ Bonus content indicators
- ✅ Edge cases (album names with keywords, multiple parentheses)

### Matching Tests (6 test cases)
- ✅ Albums with version differences match correctly
- ✅ Different albums don't match

**All tests pass:** 32/32 ✅

## Verification

### Existing Tests
- ✅ `test_spotify_version_matching.py` - 6/6 suites pass
- ✅ `test_strict_spotify_matching.py` - 26/26 tests pass
- ✅ No regressions in existing functionality

## Examples

### Before and After Normalization

| Original Album Name | Normalized |
|-------------------|------------|
| `Helix (2021 version)` | `helix` |
| `Album Name (Deluxe Edition)` | `album name` |
| `Greatest Hits - Remastered` | `greatest hits` |
| `Dark Side of the Moon (2011 Remaster)` | `dark side of the moon` |
| `Album Name (10th Anniversary Edition)` | `album name` |
| `Simple Album` | `simple album` |

## Impact

This fix ensures that:
1. Albums with version suffixes can match their base versions on Spotify
2. Songs get proper popularity ratings (including 5-star ratings for popular tracks)
3. Single detection works correctly regardless of album version suffix
4. The system is more robust when dealing with different album versions

## Files Modified

1. **`matching_utils.py`**
   - Added `normalize_album()` function
   - Updated `calculate_track_similarity()` to use `normalize_album()` for album comparisons

2. **`popularity.py`**
   - Added import for `normalize_album`
   - Updated Spotify search call to normalize album name before searching
   - Added logging to show both original and normalized album names

3. **`test_album_normalization.py`** (new)
   - Comprehensive test suite for album normalization
   - Tests for matching with version differences

## Related Work

This fix builds on the existing normalization infrastructure:
- `normalize_title()` - For track titles (preserves Roman numerals and punctuation)
- `normalize_artist()` - For artist names (handles collaborations)
- `normalize_string()` - Base normalization (removes accents, special characters)

## Code Quality

- ✅ **DRY principle**: Reuses existing `normalize_string()` function
- ✅ **Consistency**: Follows same patterns as other normalization functions
- ✅ **Maintainability**: Clear documentation and test coverage
- ✅ **No breaking changes**: All existing tests pass
- ✅ **Edge cases handled**: Album names where keywords are part of the name

## Future Considerations

1. Monitor for edge cases where normalization might be too aggressive
2. Consider adding configuration option to disable/customize normalization if needed
3. May want to apply similar normalization to other metadata fields if issues arise

## Status

**Ready for review ✅**
