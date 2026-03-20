# MusicBrainz Download Queue Integration Fix

## Problem Statement

When users downloaded albums from MusicBrainz via Soulseek, they saw:

1. **Multiple separate queue entries** (e.g., "Angus McSix - Angus McSix and the All-Seeing Astral" appeared 5 times)
2. **Albums not being sent to Soulseek** for actual downloading

### Root Cause

The `/api/musicbrainz/download` endpoint was a **simplified download system** that:
- Created a single entry in `managed_downloads` table
- Searched for the album as ONE file
- Never fetched MusicBrainz release data
- Never added tracks to the `download_queue`
- Never integrated with the queue processor

Result: Albums were treated as simple searches without proper track tracking.

## Solution Implemented

### Modified `/api/musicbrainz/download` Endpoint

The endpoint now:

1. **Fetches MusicBrainz Release Data**
   ```python
   from musicbrainz_release_manager import get_manager
   manager = get_manager()
   mb_data = manager.fetch_release_from_musicbrainz(release_id)
   ```

2. **Extracts All Tracks**
   - Iterates through all tracks in all media/formats
   - Gets: title, artist, track number, duration, ISRC

3. **Adds Tracks to Download Queue**
   ```sql
   INSERT INTO download_queue
   (artist, album, title, search_query, source, status,
    release_id, track_number, created_at, updated_at)
   VALUES (...)
   ```
   - Each track gets its own download_queue entry
   - Linked by `release_id` for grouping
   - Status: 'queued' for processing

4. **Creates Single Managed Download Entry**
   ```sql
   INSERT INTO managed_downloads
   (release_id, release_title, artist, method, status, download_query, ...)
   ```
   - ONE entry per album
   - Stores album-level metadata
   - Links to all queued tracks

5. **Initiates Album Search**
   - Searches for entire album in Soulseek
   - User selects the album file from results
   - Downloads complete album for quick installation

### Enhanced `/api/musicbrainz/downloads` API

Now returns track statistics:
```json
{
  "downloads": [
    {
      "id": 1,
      "release_title": "Album Name",
      "artist": "Artist Name",
      "method": "slskd",
      "status": "downloading",
      "total_tracks": 5,
      "completed_tracks": 2,
      "downloading_tracks": 2,
      "failed_tracks": 1
    }
  ]
}
```

### Updated Downloads Display

Shows album-level summary with track progress:

```
Album Name | Artist | Soulseek | Downloading | 12:34 | [Select] [Retry]
Tracks: 2/5 completed
```

## Workflow After Fix

### Step 1: User Initiates Download
```
User clicks "Download via Soulseek" on album
    ↓
/api/musicbrainz/download receives request
```

### Step 2: Backend Processing
```
1. Fetch MusicBrainz release data
2. Extract all tracks (metadata)
3. Add each track to download_queue
4. Create single managed_downloads entry
5. Search for album file in Soulseek
```

### Step 3: User Selects File
```
User sees Soulseek search results
    ↓
Selects album file
    ↓
Backend initiates download
```

### Step 4: Queue Processing
```
queue_processor.py runs independently
    ↓
Processes each track in download_queue
    ↓
Searches for individual tracks in Soulseek
    ↓
Downloads files
    ↓
post_download_processor organizes files
```

### Step 5: Display Updates
```
Downloads page shows:
- Single album row
- Track progress: "2/5 completed"
- Overall status: "Downloading"
```

## Database Integration

### migration_downloads Table
- `release_id` - Links to MusicBrainz release
- `release_title` - Album name
- `artist` - Album artist
- `status` - Overall download state
- `total_tracks` - Derived from download_queue count

### download_queue Table
- Each track gets ONE row
- `release_id` - Links to album
- `track_number` - Order in album
- `status` - Per-track state (queued, searching, found, downloading, completed, failed)
- `search_query` - Individual track search

### Grouping Logic
```sql
-- Get album progress
SELECT 
    COUNT(*) as total_tracks,
    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed_tracks,
    SUM(CASE WHEN status IN ('downloading', 'searching') THEN 1 ELSE 0 END) as downloading_tracks,
    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed_tracks
FROM download_queue
WHERE release_id = ?
```

## Fallback Behavior

If MusicBrainz data unavailable:
- `_simple_mb_download()` function handles fallback
- Creates managed_downloads entry
- Searches for album as before
- No track-level precision but still functional

## Benefits

✅ **Single Entry Per Album** - No more duplicate queue entries
✅ **Proper Track Tracking** - All tracks monitored in download_queue
✅ **Queue Integration** - Seamless with queue_processor.py
✅ **Unified Progress** - Album-level and track-level stats
✅ **Better User Experience** - Clear what's happening
✅ **PostgreSQL Compatible** - Uses placeholder patterns
✅ **Graceful Degradation** - Works even if MB data missing

## Code Changes

### app.py (160 lines added)
- `api_musicbrainz_download()` - Complete rewrite (lines 9160-9298)
- `_simple_mb_download()` - New fallback function
- `api_musicbrainz_downloads()` - Enhanced with track stats

### templates/downloads.html
- Added track progress display in table rows
- Shows "Tracks: X/Y completed" inline

## Testing Checklist

- [ ] Download album via Soulseek
- [ ] Verify only ONE entry in managed_downloads
- [ ] Verify all tracks added to download_queue
- [ ] Verify downloads page shows "Tracks: X/Y completed"
- [ ] Wait for queue_processor to process tracks
- [ ] Verify tracks downloaded to Soulseek folder
- [ ] Verify post_download_processor organized files
- [ ] Test with different album sizes (1 track, 5 tracks, 20+ tracks)
- [ ] Test fallback with invalid release_id

## Migration Notes

No database migrations needed - uses existing tables:
- `managed_downloads` (already has release_id)
- `download_queue` (already has release_id, track_number)

Fully backward compatible with existing downloads.

## Future Enhancements

1. **Cache MusicBrainz Responses** - Avoid repeated API calls for same release
2. **Batch Track Searching** - Search for multiple tracks without downloading album file
3. **Intelligent File Extraction** - Auto-extract tracks from downloaded album file
4. **Track Matching** - Match downloaded file to tracks by duration/metadata
5. **Resume Downloads** - Restart failed albums from where they stopped

## Related Documentation

- `MUSICBRAINZ_IMPLEMENTATION_PROGRESS.md` - Overall MB implementation status
- `QUEUE_AND_DOWNLOADS_INTEGRATION_GUIDE.md` - Download queue architecture
- `musicbrainz_release_manager.py` - Release manager implementation
- `queue_processor.py` - Background queue processor
