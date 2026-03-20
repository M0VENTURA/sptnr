# Database Cleanup Behavior

## Overview
This document describes how the sptnr database handles removed songs, albums, and artists from Navidrome or MP3 files.

## Current Behavior

### Automatic Deletion: **NOT IMPLEMENTED**
The database does **NOT** automatically delete records when songs, albums, or artists are removed from:
- Navidrome
- MP3 files in your music library
- The file system

This is an intentional design decision to preserve metadata and prevent data loss.

### Why Automatic Deletion is NOT Enabled

1. **Data Preservation**: Ratings, popularity scores, single detection, and other metadata are valuable and should not be lost if files are temporarily moved or Navidrome rescans
2. **Navidrome ID Changes**: Navidrome IDs can change during rescans, but the content-based matching (artist, album, title, duration) prevents duplicates
3. **Manual Control**: Users should explicitly decide when to remove old metadata
4. **Historical Data**: Keeping old records allows tracking of removed content and reverting deletions

### Manual Cleanup Options

#### Option 1: API Endpoint
Use the cleanup API endpoint to remove duplicates:

```bash
curl -X POST http://localhost:5000/api/database/cleanup-duplicates
```

This endpoint:
- Finds duplicate tracks based on content matching (artist, album, title, duration)
- Removes duplicates while preserving the best metadata (prioritizes beets_mbid > mbid > file_path > most recent)
- Does NOT remove tracks that are simply missing from Navidrome

#### Option 2: Python Script
Run the duplicate cleanup script directly:

```bash
python fix_duplicate_albums.py
```

By default, this runs in **dry-run mode** (shows what would be deleted without actually deleting).

To actually delete duplicates:
```bash
# Edit fix_duplicate_albums.py and change dry_run=False
# OR implement a command-line flag
```

#### Option 3: Manual SQL
For advanced users who want to remove specific orphaned records:

```sql
-- Find tracks with no file_path (potentially removed from disk)
SELECT * FROM tracks WHERE file_path IS NULL OR file_path = '';

-- Delete them (USE WITH CAUTION)
DELETE FROM tracks WHERE file_path IS NULL OR file_path = '';

-- Find artists with no tracks
SELECT a.artist_name, a.album_count, a.track_count 
FROM artist_stats a
WHERE a.artist_name NOT IN (SELECT DISTINCT COALESCE(album_artist, artist) FROM tracks);

-- Delete orphaned artist_stats
DELETE FROM artist_stats 
WHERE artist_name NOT IN (SELECT DISTINCT COALESCE(album_artist, artist) FROM tracks);
```

## Confirming Removed Items

### Songs Removed from MP3 Files or Navidrome

When a song is removed:
1. The track record remains in the `tracks` table
2. If the file had a `file_path`, it will still be stored
3. On next Navidrome import:
   - If the song is truly gone from Navidrome, it won't be updated
   - The `last_scanned` timestamp will become outdated
   - The track will still appear in the database

**To find potentially removed tracks:**
```sql
-- Tracks not scanned in the last 30 days (adjust as needed)
SELECT artist, album, title, last_scanned 
FROM tracks 
WHERE last_scanned < datetime('now', '-30 days')
ORDER BY last_scanned ASC;
```

### Albums Removed

Albums don't have separate records—they're aggregated from track data.

**To find potentially removed albums:**
```sql
-- Albums not scanned recently
SELECT artist, album, MAX(last_scanned) as last_scan, COUNT(*) as track_count
FROM tracks 
GROUP BY artist, album
HAVING MAX(last_scanned) < datetime('now', '-30 days')
ORDER BY last_scan ASC;
```

### Artists Removed

Artists are tracked in two places:
1. `tracks` table (aggregated by `album_artist` or `artist`)
2. `artist_stats` table (separate artist-level statistics)

**To find orphaned artists in artist_stats:**
```sql
-- Artists in artist_stats but not in tracks
SELECT artist_name, album_count, track_count, last_updated
FROM artist_stats
WHERE artist_name NOT IN (
    SELECT DISTINCT COALESCE(album_artist, artist) FROM tracks
);
```

## Recommendations

### For Regular Maintenance
1. Run the duplicate cleanup monthly:
   ```bash
   curl -X POST http://localhost:5000/api/database/cleanup-duplicates
   ```

2. Check for orphaned artists quarterly:
   ```sql
   DELETE FROM artist_stats 
   WHERE artist_name NOT IN (SELECT DISTINCT COALESCE(album_artist, artist) FROM tracks);
   ```

### For Major Library Reorganization
If you've significantly reorganized your library (moved/deleted many files):

1. Back up the database:
   ```bash
   cp sptnr.db sptnr.db.backup
   ```

2. Re-import from Navidrome to update all records:
   - This will update `last_scanned` timestamps for current tracks
   - Old tracks will have outdated timestamps

3. Identify tracks not updated in the last day (these were likely removed):
   ```sql
   SELECT COUNT(*) as potentially_removed
   FROM tracks 
   WHERE last_scanned < datetime('now', '-1 day');
   ```

4. Optionally delete them:
   ```sql
   DELETE FROM tracks WHERE last_scanned < datetime('now', '-1 day');
   ```

## Future Enhancement Possibility

A future enhancement could add an automatic cleanup mode that:
- Tracks which Navidrome IDs were seen during the last import
- Marks records not seen as "potentially removed"
- After N days without seeing a record, optionally deletes it
- Provides a UI to review "pending deletion" records

This feature is not currently implemented.

## Summary

✅ **Confirmed**: Database does NOT automatically delete removed items  
✅ **Confirmed**: Manual cleanup options exist via API, script, or SQL  
✅ **Confirmed**: Orphaned records will accumulate without manual maintenance  
✅ **Recommended**: Periodic cleanup of duplicates and orphaned artist_stats records
