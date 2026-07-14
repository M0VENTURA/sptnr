# Standout Track 5-Star Assignment Fix

## Summary

Fixed issue where tracks marked as `is_standout_track` (high Last.fm scrobbles) were not receiving 5 stars during popularity scan.

## Problem

The `is_standout_track` flag was being set correctly based on Last.fm playcount statistics during the popularity scan:
- Tracks with z-score >= 2.0 (2+ standard deviations above artist mean)
- AND listeners >= 1000

However, these standout tracks were not receiving 5 stars because:
1. The SELECT queries for star assignment didn't include the `is_standout_track` field
2. The star assignment logic never checked this flag

## Solution

### Changes Made

1. **Added `is_standout_track` to database queries** (popularity.py lines 3003, 3012)
   ```python
   # Before:
   "SELECT id, title, popularity_score, is_single, single_confidence, single_sources, lastfm_track_playcount FROM tracks WHERE..."
   
   # After:
   "SELECT id, title, popularity_score, is_single, single_confidence, single_sources, lastfm_track_playcount, is_standout_track FROM tracks WHERE..."
   ```

2. **Extract `is_standout_track` in star rating loop** (popularity.py line 3103)
   ```python
   is_standout_track = track_row["is_standout_track"] if track_row["is_standout_track"] is not None else 0
   ```

3. **Added 5-star check for standout tracks** (popularity.py lines 3194-3197)
   ```python
   # Standout tracks (high Last.fm scrobbles) get 5 stars
   elif is_standout_track:
       stars = 5
       log_info(f"5-star assignment: {title} (standout track - high Last.fm scrobbles)")
       log_debug(f"Standout track - track_id: {track_id}")
   ```

### Testing

Created `test_standout_track_stars.py` which:
- Creates test database with tracks marked as standout
- Verifies the SELECT query includes `is_standout_track`
- Confirms standout tracks receive 5 stars
- Test passes successfully ✅

## Impact

This fix ensures that:
- Songs that weren't detected as singles
- BUT are among the highest scrobbled tracks on Last.fm for an artist (z-score >= 2.0, listeners >= 1000)
- Now correctly receive 5 stars and are logged as "Standout Tracks"

## Code Review & Security

- ✅ Code review: No issues found
- ✅ CodeQL security scan: No vulnerabilities detected

## Related Code

The standout track detection logic (lines 2708-2767 in popularity.py) was already working correctly:
```python
# Mark as standout if z-score >= 2.0 AND listeners >= 1000
is_standout = 1 if (track_zscore >= 2.0 and listeners >= 1000) else 0

if is_standout:
    cursor.execute("""
        UPDATE tracks SET is_standout_track = ?, artist_z_score = ?
        WHERE id = ?
    """, (is_standout, track_zscore, track_id))
```

The fix simply ensures this flag is now properly checked during star assignment.
