# Track Star Rating Logging Fix Summary

## Issues Fixed

### Issue 1: Track Star Ratings Not Logged for Albums Without Singles/Standouts
**Problem:** Track star ratings were not being output to the unified_scan.log for albums that had no singles, standout tracks, or close matches. This was because the condition at line 4351 required at least one special category to exist before logging the "Rest of Album" tracks.

**Root Cause:** The condition `if rest_of_album and (detected_singles or standout_tracks or possible_singles)` prevented any tracks from being logged if all three special categories were empty.

**Fix:** Modified the logic to:
1. Always log `rest_of_album` tracks if they exist
2. Show "All Tracks" header when there are no special categories
3. Show "Rest of Album" header when special categories exist

**Code Change (popularity.py line 4351-4354):**
```python
# Before:
if rest_of_album and (detected_singles or standout_tracks or possible_singles):
    log_unified(f"Single Detection Scan - ===== {album} - Rest of Album =====")
    for title, stars, _ in rest_of_album:
        log_unified(f"Single Detection Scan - {stars:<5} {artist} - {title}")

# After:
if rest_of_album:
    # If there are special tracks, label as "Rest of Album"
    # Otherwise, label as "All Tracks"
    if detected_singles or standout_tracks or possible_singles:
        log_unified(f"Single Detection Scan - ===== {album} - Rest of Album =====")
    else:
        log_unified(f"Single Detection Scan - ===== {album} - All Tracks =====")
    for title, stars, _ in rest_of_album:
        log_unified(f"Single Detection Scan - {stars:<5} {artist} - {title}")
```

### Issue 2: Incorrect Album Display After Artist Scan Completion
**Problem:** After an artist scan completed, the UI could display stale artist information because the progress file was not cleared.

**Root Cause:** When the scan completed, the final progress state (lines 4443-4449) wrote `is_running: false` but did not include `current_artist: None`. This meant any UI code that cached the previous `current_artist` value would continue to show it even after the scan finished.

**Fix:** Added `current_artist: None` to the final progress state to explicitly clear the current artist information when the scan completes.

**Code Change (popularity.py line 4448):**
```python
# Before:
progress_data = {
    "is_running": False,
    "scan_type": "popularity_scan",
    "processed_artists": total_artists,
    "total_artists": total_artists,
    "percent_complete": 100
}

# After:
progress_data = {
    "is_running": False,
    "scan_type": "popularity_scan",
    "processed_artists": total_artists,
    "total_artists": total_artists,
    "percent_complete": 100,
    "current_artist": None  # Clear current artist when scan completes
}
```

## Testing

Created `test_track_star_rating_logging.py` to validate both fixes:

1. **Track Logging Test:** Verifies that tracks are logged to unified_scan.log even when there are no singles/standouts
2. **Progress Cleanup Test:** Verifies that `current_artist` is cleared when the scan completes

Both tests pass successfully ✓

## Impact

### User-Visible Changes
1. **Unified Log:** All tracks with star ratings now appear in unified_scan.log, even for albums without singles
2. **UI Progress:** Artist/album information is properly cleared when scans complete, preventing stale data from being displayed

### Expected Log Format
For albums with NO singles/standouts/close matches:
```
Single Detection Scan - ===== Album Name - All Tracks =====
Single Detection Scan - ★★★   Artist Name - Track 1
Single Detection Scan - ★★    Artist Name - Track 2
Single Detection Scan - ★★★★  Artist Name - Track 3
```

For albums WITH special categories:
```
Single Detection Scan - ===== Album Name - Detected Singles =====
Single Detection Scan - ★★★★★ Artist Name - Single Track (MusicBrainz; z-score: 2.14)

Single Detection Scan - ===== Album Name - Rest of Album =====
Single Detection Scan - ★★★   Artist Name - Track 1
Single Detection Scan - ★★    Artist Name - Track 2
```

## Files Modified
- `popularity.py`: Lines 4351-4354 (track logging), Line 4448 (progress cleanup)
- `test_track_star_rating_logging.py`: New test file

## Verification Steps
1. Run popularity scan on an artist with albums that have no singles/standouts
2. Check unified_scan.log to verify all tracks are logged with star ratings
3. Monitor the UI during and after scan completion to verify artist/album information is displayed correctly
