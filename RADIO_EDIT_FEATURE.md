# Radio Edit Medium Confidence Feature

## Summary

Added support for detecting "Radio Edit" versions in Spotify search results as a medium confidence indicator for single detection.

## Problem Statement

When Spotify search results contain a track with "Radio Edit" in the title (e.g., "Giving In - Radio Edit"), it was being rejected due to title mismatch. The user requested that such results should contribute to medium confidence for single detection, as radio edits are typically only created for singles.

## Implementation

### Changes Made

1. **Modified `single_detection_enhanced.py`**:
   - Added `radio_edit_found` flag to track when a Radio Edit is detected in Spotify results
   - Added radio edit detection logic in the Spotify checking section (lines 1042-1050)
   - Added `radio_edit_found` as a parameter to `determine_final_status` function
   - Added radio edit as a medium confidence source (lines 684-685)
   - Updated source tracking to include 'radio_edit' when detected (lines 1076-1079)

### Detection Logic

The implementation detects radio edit versions using the following approach:

1. **Pattern Detection**: Searches for variations like:
   - "Song Name - Radio Edit"
   - "Song Name (Radio Edit)"
   - Case-insensitive matching

2. **Base Title Verification**: 
   - Extracts the base title by removing the radio edit suffix
   - Normalizes both the base title and the search term
   - Only marks as found if the normalized titles match

3. **Medium Confidence**: 
   - Radio edit detection adds to medium confidence count
   - Works alongside other medium confidence sources (Spotify, MusicBrainz, Discogs video, Last.fm)

### Example

**Before**:
```
[SPOTIFY] Checking result: 'Giving In - Radio Edit' (type: album, album: 'Insomniac's Dream')
[SPOTIFY] Title mismatch: 'Giving In - Radio Edit' != 'Giving In'
[SPOTIFY] ✗ NOT confirmed - No single/EP matches found
```

**After**:
```
[SPOTIFY] Checking result: 'Giving In - Radio Edit' (type: album, album: 'Insomniac's Dream')
[SPOTIFY] ✓ Radio Edit found: 'Giving In - Radio Edit' (medium confidence indicator)
[SPOTIFY] Radio Edit detected for Giving In (medium confidence)
```

## Testing

Created comprehensive tests to validate the feature:

1. **`test_radio_edit_detection.py`**:
   - Tests that radio_edit_found contributes to medium confidence
   - Tests combination with other confidence sources
   - All tests passing ✓

2. **`test_radio_edit_integration.py`**:
   - Tests radio edit pattern detection
   - Tests base title extraction from radio edit titles
   - Tests normalized title matching
   - All tests passing ✓

3. **Existing Tests**:
   - All existing tests in `test_enhanced_single_detection.py` still pass
   - No regressions introduced

## Benefits

- More accurate single detection for tracks that have radio edit versions
- Provides medium confidence even when Discogs/MusicBrainz don't confirm
- Works alongside existing confidence sources
- Minimal code changes with clear, focused implementation
