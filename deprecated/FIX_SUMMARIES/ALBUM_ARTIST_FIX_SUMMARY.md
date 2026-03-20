# Fix Summary: Album Artist Display Issue

## Problem Statement
Pull request #249 introduced changes to handle `album_artist` fields, but a subsequent issue was discovered where track artists with featuring artists (e.g., "A Killer's Confession feat. JMANN") were incorrectly showing up as album artists in the UI, even though in Navidrome the actual album artist is just "A Killer's Confession".

## Root Cause Analysis

### Investigation
The issue was traced to how `album_artist` is populated during Navidrome import. In three key import functions:
- `navidrome_import.py::scan_artist_to_db()`
- `start.py` (main scan function)
- `scan_helpers.py::scan_artist_to_db()`

The code was using:
```python
"album_artist": t.get("albumArtist", "")
```

This retrieves the `albumArtist` field from the track object (`t`) returned by Navidrome's API.

### The Problem
According to the Subsonic API specification:
- **Album objects** should have an `artist` field containing the album artist
- **Track objects** have an optional `albumArtist` field for compilations where the track artist differs from the album artist

However, Navidrome appears to populate the track's `albumArtist` field with the track's `artist` value (which includes featuring artists like "feat. JMANN") rather than the actual album artist from the album metadata.

### Example
For an album by "A Killer's Confession" with a track featuring another artist:
- **Correct album artist**: "A Killer's Confession" (from album metadata)
- **Track artist**: "A Killer's Confession feat. JMANN" (track-specific)
- **Track's albumArtist field**: "A Killer's Confession feat. JMANN" ❌ (incorrect - contains track artist)

Using `t.get("albumArtist")` resulted in storing "A Killer's Confession feat. JMANN" as the album artist for all tracks in the album.

## Solution

### Implementation
Changed all three import paths to use album-level data instead of track-level data:

```python
# Get the album artist from the album object or fall back to the artist we're importing for
# The track's albumArtist field can be incorrect (e.g., containing track artist with feat.)
# Priority: album.artist > artist_name (function parameter) > track.albumArtist (as last resort)
album_artist_value = alb.get("artist", artist_name)
```

Then use this value when creating track data:
```python
"album_artist": album_artist_value,
```

### Logic Flow
1. **First choice**: Use `alb.get("artist")` - the artist field from the album object
2. **Fallback**: Use `artist_name` - the function parameter representing the artist we're importing
3. **Deprecated**: No longer using `t.get("albumArtist")` from track object

### Why This Works
- The album object comes from Navidrome's `getArtist.view` endpoint, which returns albums for a specific artist
- Each album has an `artist` field that contains the correct album artist
- The `artist_name` parameter is the artist we're currently importing, which is also the correct album artist for all albums in that import cycle
- This ensures all tracks in an album get the same album artist, based on album-level metadata

## Files Changed

### Core Changes
1. **navidrome_import.py** (lines 264-269, 338)
   - Added `album_artist_value` calculation before track loop
   - Updated `album_artist` field to use `album_artist_value`

2. **start.py** (lines 521-528, 603)
   - Added `album_artist_value` calculation before track loop
   - Updated `album_artist` field to use `album_artist_value`

3. **scan_helpers.py** (lines 157-163, 224)
   - Added `album_artist_value` calculation before track loop
   - Updated `album_artist` field to use `album_artist_value`

### Test Added
4. **test_album_artist_fix.py** (new file)
   - Unit tests verifying the fix
   - Tests correct album artist assignment
   - Tests fallback behavior
   - Tests compilation album handling

## Testing

### Unit Tests
Created comprehensive unit tests that verify:
- ✅ Album artist correctly uses `album.artist` field
- ✅ Falls back to `artist_name` parameter when `album.artist` is missing
- ✅ Works correctly for compilations (Various Artists)
- ✅ Fixes issue where track artists with 'feat.' were showing as album artists

All tests pass successfully.

### Code Quality
- ✅ **Code Review**: No issues found
- ✅ **Security Scan (CodeQL)**: No alerts found
- ✅ **Syntax Check**: All files compile successfully

## Impact

### Positive Impact
- Album artists now display correctly in the UI
- Tracks with featuring artists no longer incorrectly show as separate album artists
- Compilations (Various Artists) continue to work correctly
- All tracks in an album now consistently have the same album artist

### Backward Compatibility
- The change is backward compatible
- Uses fallback logic to handle cases where album object doesn't have `artist` field
- Existing database records are not modified; change affects future imports

### Data Consistency
After this fix, new imports will:
- Store the correct album artist for all tracks
- Group albums correctly under their album artist
- Show featuring artists only in the track artist field, not as album artists

## User Benefit
Users will now see:
- **Artist List**: Shows album artists only (e.g., "A Killer's Confession")
- **Artist Page**: Displays all albums by that album artist
- **Album Page**: Shows consistent album artist across all tracks
- **Track Artist**: Still shows featuring artists where applicable (e.g., "A Killer's Confession feat. JMANN")

This matches the expected behavior from Navidrome's own UI.

## Recommendation
After deploying this fix, users should:
1. Re-import their library to update existing tracks with correct album artists
2. Use the "Force Re-scan" option in the web UI for affected artists
3. The SQL queries in `app.py` using `COALESCE(NULLIF(album_artist, ''), artist)` will continue to work correctly
