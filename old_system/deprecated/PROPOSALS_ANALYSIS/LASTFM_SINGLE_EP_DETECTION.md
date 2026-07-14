# Last.fm Single Detection Enhancement

## Summary

This PR adjusts the Last.fm single detection to allow up to 6 songs if the song name is the same as the release name, which covers singles released as EPs.

## Problem Statement

Previously, the Last.fm single detection only considered albums with 1-3 tracks as potential singles. However, many singles are released as EPs with 4-6 tracks, where one of the tracks is the actual single (title track) and matches the album name.

## Solution

### 1. New Method: `has_title_track()`
Added to `api_clients/lastfm.py`:
- Checks if an album has a track with the same name as the album
- Uses case-insensitive comparison
- Handles both single track and multi-track responses from Last.fm API
- Returns `True` if a title track is found, `False` otherwise

### 2. Enhanced Detection Logic
Updated `single_detection_enhanced.py`:
- Keeps existing 1-3 track limit for regular singles
- Adds new detection for 4-6 track albums that have a title track
- Only calls `has_title_track()` API when track count is 4-6 (optimization)
- Provides clear debug logging for both cases

### 3. Test Coverage
Created `test_lastfm_title_track.py`:
- Tests single track matching album name
- Tests multiple tracks with one matching album name
- Tests no matching tracks
- Tests case-insensitive matching
- All 4 tests passing

## Implementation Details

### Last.fm API Integration
The `has_title_track()` method:
1. Calls Last.fm's `album.getInfo` API
2. Extracts the track list from the response
3. Normalizes album and track names (lowercase, strip whitespace)
4. Compares each track name with the album name
5. Returns `True` on first match, `False` if no match found

### Detection Flow
```
If Last.fm returns track count:
  - 1-3 tracks: Single confirmed (existing logic)
  - 4-6 tracks with title track: Single EP confirmed (new logic)
  - Otherwise: Not a single
```

## Code Quality

- ✅ Code review completed - all feedback addressed
- ✅ Security scan passed - no vulnerabilities found
- ✅ All tests passing (4/4)
- ✅ Minimal changes - only touched necessary files
- ✅ Backward compatible - existing detection still works

## Files Changed

1. `api_clients/lastfm.py` - Added `has_title_track()` method
2. `single_detection_enhanced.py` - Enhanced detection logic
3. `test_lastfm_title_track.py` - Test coverage (new file)

## Example Use Cases

This change will now detect as singles:
- **"Shape of You"** by Ed Sheeran - Released as a 6-track EP with title track
- **"Bad Guy"** by Billie Eilish - Released as a 5-track EP with title track
- Any similar single releases where the main single is released alongside B-sides or remixes

## Security Considerations

- No new security vulnerabilities introduced
- Proper error handling for all API calls
- No sensitive data exposure
- Uses existing Last.fm API authentication

## Testing

Run tests with:
```bash
python test_lastfm_title_track.py
```

Expected output: 4/4 tests passing
