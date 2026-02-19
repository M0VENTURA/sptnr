# Star Rating Logging Fix - Implementation Summary

## Problem Statement

Based on GitHub issue logs showing duplicate star ratings for "Stark" album after scanning "This Is the End of Control", two issues were identified:

1. **Wrong album data showing after second scan**: Star ratings and track listings for "Stark" (album 1/2) were being logged again after scanning "This Is the End of Control" (album 2/2)
2. **Missing detection source and z-score**: "Rest of Album" tracks were not showing detection source and z-score information, unlike other track categories

## Root Cause Analysis

### Issue 1: Variable Shadowing Bug

**Location**: `popularity.py` line 3505

**Problem**: The code iterates through all tracks for an artist (lines 3482-3542) to calculate standout ratings. Inside this loop, line 3505 reassigns the `album` variable:

```python
for track in artist_tracks:  # artist_tracks contains ALL tracks for the artist
    track_id = row_get(track, 'id')
    track_title = row_get(track, 'title')
    album = row_get(track, 'album', '')  # ❌ OVERWRITES the outer loop variable!
```

This **shadows** the `album` variable from the outer album loop at line 2643:
```python
for album, album_tracks in albums.items():  # album is the loop variable
```

After the inner loop completes, `album` contains the album name from the **last track** in `artist_tracks`, not the current album being processed.

**Impact**: When star rating logging runs at line 4224, it uses this incorrect `album` variable:
```python
log_unified(f"Star Ratings - Album '{album}' by {artist}: {dist_str}")
```

The subsequent database query at lines 4231-4237 fetches tracks using the wrong album name:
```python
cursor.execute("""
    SELECT id, title, stars, is_single, single_confidence, single_sources,
           is_standout_track, artist_z_score
    FROM tracks 
    WHERE artist = ? AND album = ?  # Uses wrong album variable!
    ORDER BY stars DESC, popularity_score DESC
""", (artist, album))
```

### Issue 2: Discarded Reason String

**Location**: `popularity.py` line 4358

**Problem**: The code builds a `reason_str` containing detection source and z-score info (lines 4285-4311), stores it in tuples for each track category (lines 4321-4328), but then **discards** it for `rest_of_album` tracks:

```python
for title, stars, _ in rest_of_album:  # ❌ underscore ignores reason
    log_unified(f"Single Detection Scan - {stars:<5} {artist} - {title}")
```

Meanwhile, other categories include the reason:
```python
for title, stars, reason in detected_singles:
    log_unified(f"Single Detection Scan - {stars:<5} {artist} - {title}{reason}")
```

## Solution

### Fix 1: Rename Shadowing Variable

Changed line 3505 from:
```python
album = row_get(track, 'album', '')
```

To:
```python
track_album = row_get(track, 'album', '')
```

And updated all references in lines 3516-3533 to use `track_album`, `track_album_mean`, `track_album_stdev`, and `sorted_track_album_scores`.

This prevents the outer `album` variable from being overwritten.

### Fix 2: Include Reason String

Changed line 4358 from:
```python
for title, stars, _ in rest_of_album:
    log_unified(f"Single Detection Scan - {stars:<5} {artist} - {title}")
```

To:
```python
for title, stars, reason in rest_of_album:
    log_unified(f"Single Detection Scan - {stars:<5} {artist} - {title}{reason}")
```

This ensures all tracks display their detection method and z-score consistently.

## Files Modified

- `popularity.py`:
  - Lines 3505, 3516-3521, 3533: Renamed `album` to `track_album` in nested loop
  - Lines 4358-4359: Include `reason` string in rest_of_album logging

## Testing

Two test scripts were created:

1. **test_album_variable_scoping.py**: Verifies that star ratings log correct album data when scanning multiple albums for the same artist
2. **test_zscore_display.py**: Verifies that z-score and detection source information appears in logs for all track categories

## Verification

- ✅ **Code Review**: Passed with no issues
- ✅ **Security Scan**: No vulnerabilities detected
- ✅ **Manual Review**: Changes are minimal and surgical, directly addressing the reported issues

## Impact

### Before Fix
```
2026-02-19 17:21:36.546 [INFO] Single Detection - 75% completed - 9/12 tracks
2026-02-19 17:21:47.512 [INFO] Star Ratings - Album 'Stark' by Cherri Bomb: 4★: 2, 3★: 2, 2★: 1
2026-02-19 17:21:47.513 [INFO] Single Detection Scan - ===== Stark - All Tracks =====
2026-02-19 17:21:47.513 [INFO] Single Detection Scan - ★★★★  Cherri Bomb - The Pretender
2026-02-19 17:21:47.513 [INFO] Single Detection Scan - ★★★★  Cherri Bomb - Let It Go
```
*Wrong: After scanning "This Is the End of Control", logs show "Stark" album again*
*Missing: No detection source or z-score shown for tracks*

### After Fix
```
2026-02-19 17:21:36.546 [INFO] Single Detection - 75% completed - 9/12 tracks
2026-02-19 17:21:47.512 [INFO] Star Ratings - Album 'This Is the End of Control' by Cherri Bomb: ...
2026-02-19 17:21:47.513 [INFO] Single Detection Scan - ===== This Is the End of Control - All Tracks =====
2026-02-19 17:21:47.513 [INFO] Single Detection Scan - ★★★★  Cherri Bomb - Track Name (Spotify; MusicBrainz; z-score: 2.15)
```
*Correct: Shows the actual album that was just scanned*
*Complete: Detection source and z-score displayed for all tracks*

## Memory to Store

Variable shadowing in nested loops is a common Python pitfall. When iterating through collections, avoid reusing variable names from outer loops. Use descriptive names like `track_album` instead of generic names like `album` to prevent shadowing.
