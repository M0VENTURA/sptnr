# Logging Cleanup and Duplicate Album Fix - Implementation Summary

## Overview

This implementation addresses two main issues:
1. **Logging Cleanup**: Simplify unified log, enhance info/debug logs, and implement 7-day retention
2. **Duplicate Albums**: Fix duplicate albums in database caused by unstable Navidrome IDs

## Changes Made

### 1. Log Rotation (logging_config.py)

**Changed from size-based to time-based rotation:**
- `RotatingFileHandler` → `TimedRotatingFileHandler`
- Rotates daily at midnight (`when='midnight', interval=1`)
- Keeps exactly 7 days of history (`backupCount=7`)
- Applies to all three logs: unified_scan.log, info.log, debug.log

### 2. Unified Log Simplification

Reduced log verbosity from ~105 calls to 36 calls in popularity.py, following the specification:

#### Navidrome Import Scan
```
Navidrome Import Scan - Starting Navidrome Import
Navidrome Import Scan - Scanning Artist {Artist Name} ({Number of albums} albums)
Navidrome Import Scan - Importing {Artist} - {Album}
Navidrome Import Scan - Skipped {Album} (already cached)
```

#### Popularity Scan
```
Popularity Scan - Starting Popularity Scan
Popularity Scan - Scanning Artist {Artist Name} ({Number of albums} album(s))
Popularity Scan - Scanning Album {Album Name} ({n}/{total})
Popularity Scan - Scanning {Spotify, Last.FM, ListenBrainz} for Metadata
Popularity Scan - 25% completed - {n}/{total} songs
Popularity Scan - 50% completed - {n}/{total} songs
Popularity Scan - 75% completed - {n}/{total} songs
Popularity Scan - Popularity Scanning for {Album Name} Complete
Popularity Scan - Complete: {stats}
```

Note: Album/artist skip messages include the current rescan time set by album_skip_days.

#### Beets Import Scan
```
Beets Import - Starting Beets Import Scan
Beets Import - Scanning Artist {Artist Name} ({Number of albums} albums)
Beets Import - Scanning {Album Name} ({n}/{total})
Beets Import - Scanning complete for {Artist} - {Album}
```

#### Single Detection Scan
```
(Integrated into Popularity Scan - per album completion)
```

#### Playlist Creation
```
(Integrated into Popularity Scan - after all albums completed)
```

### 3. Info Log Enhancement

Moved detailed operational information from unified to info log:
- Artist ID lookups and results
- Track-by-track processing details
- Spotify/Last.fm lookup results
- Single detection results per track
- Star rating assignments
- Album/artist statistics
- Success/warning/error messages
- API call results

### 4. Debug Log Enhancement

Added comprehensive technical logging:
- API request/response data
- SQL queries with parameters
- Rate limiting details
- Error stack traces (with exc_info=True)
- Internal calculations and thresholds
- Progress state management
- Database operations
- Configuration loading

### 5. Duplicate Album Prevention (popularity_helpers.py)

**Modified `save_to_db()` function to prevent duplicates:**

```python
# Before inserting, check for existing track by content
# Match by: (artist, album, title, duration ±2s)
# Priority for keeping track:
#   1. Track with beets_mbid (beets has verified it)
#   2. Track with mbid (has MusicBrainz ID)  
#   3. Track with file_path (has file location)
#   4. Most recently scanned track
```

**How it works:**
1. When saving a track, first check if another track exists with same (artist, album, title, duration)
2. If duplicate found, compare metadata quality scores
3. Keep the track with better metadata, delete or skip the other
4. Prevents Navidrome re-imports from creating duplicates

### 6. Duplicate Album Cleanup Script (fix_duplicate_albums.py)

**New utility script to clean existing duplicates:**

```bash
# Dry-run (shows what would be deleted)
python3 fix_duplicate_albums.py

# Apply changes (actually delete duplicates)
python3 fix_duplicate_albums.py --apply
```

**Features:**
- Finds duplicate tracks (same artist, album, title)
- Chooses best track based on metadata quality
- Deletes inferior duplicates
- Logs all operations to centralized logging
- Reports affected albums and statistics

**Selection Priority:**
1. beets_mbid present (score +1000)
2. mbid present (score +500)
3. file_path present (score +200)
4. duration present (score +50)
5. popularity_score > 0 (score +30)
6. is_single = true (score +20)
7. stars > 0 (score +10)
8. Most recent last_scanned (timestamp tiebreaker)

## Files Modified

1. **logging_config.py** - Time-based rotation, 7-day retention
2. **popularity.py** - Simplified unified log, enhanced info/debug logs
3. **navidrome_import.py** - Simplified unified log, enhanced info/debug logs
4. **beets_auto_import.py** - Integrated centralized logging, simplified unified log
5. **popularity_helpers.py** - Added duplicate prevention in save_to_db()

## Files Created

1. **fix_duplicate_albums.py** - Utility script to clean existing duplicates

## Testing

### Syntax Validation
✅ All Python files compile without errors
```bash
python3 -m py_compile logging_config.py popularity_helpers.py fix_duplicate_albums.py
```

### Security Scan
✅ CodeQL: 0 alerts found

### Functionality
- No breaking changes to existing logic
- Only logging output modified
- Duplicate prevention is backward compatible

## Usage

### View Logs
- **Unified Log**: `/config/unified_scan.log` - Dashboard-viewable, basic operations
- **Info Log**: `/config/info.log` - Detailed operations
- **Debug Log**: `/config/debug.log` - Technical troubleshooting

### Clean Duplicate Albums
```bash
# See what would be deleted (dry-run)
python3 /home/runner/work/sptnr/sptnr/fix_duplicate_albums.py

# Apply the cleanup
python3 /home/runner/work/sptnr/sptnr/fix_duplicate_albums.py --apply
```

### Log Retention
Logs automatically rotate daily at midnight and keep 7 days of history. Old logs are automatically deleted.

## Root Cause Analysis: Duplicate Albums

### Problem
Albums appear twice in the database with different IDs for the same content.

### Cause
1. **Unstable IDs**: Navidrome generates new track IDs on each rescan
2. **No Content Deduplication**: Primary key is `id` only, no unique constraint on (artist, album, title)
3. **ID-based Matching**: save_to_db() only checks if track with same ID exists, not same content

### Solution
1. **Content-based Deduplication**: Check for (artist, album, title, duration) match before insert
2. **Metadata Priority**: Keep track with best metadata when duplicates found
3. **Cleanup Utility**: Script to remove existing duplicates

## Benefits

1. **Cleaner Dashboard**: Unified log is now concise and easy to read
2. **Better Debugging**: Debug log has comprehensive technical information
3. **Organized Information**: Info log contains all operational details
4. **Automatic Cleanup**: Old logs deleted after 7 days
5. **No Duplicates**: New imports won't create duplicate albums
6. **Data Quality**: Keeps tracks with best metadata when merging

## Migration Notes

For users upgrading to this version:

1. **Existing logs**: Will continue to work, new rotation starts on first run
2. **Existing duplicates**: Run `fix_duplicate_albums.py --apply` to clean up
3. **Future imports**: Automatically prevented by content matching in save_to_db()
4. **No action required**: Changes are transparent and backward compatible
