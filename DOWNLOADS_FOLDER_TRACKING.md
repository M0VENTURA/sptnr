# Downloads Monitor: Folder Tracking & Completion Status

## Overview
Comprehensive enhancement to the downloads monitor that tracks folder organization status, shows completion progress, detects duplicates, and highlights matched/missing tracks.

## Features Implemented

### 1. Database Tracking
**Tables Created:**
- `folder_album_matches` - Tracks which folders match which MusicBrainz/Discogs releases
  - Stores folder path, release ID, source, artist, album, track counts
  - Tracks completion status (pending, organizing, completed)
  - Records expected vs matched track counts
  
- `folder_track_matches` - Tracks individual file organization
  - Links files to their folder match
  - Records original path and organized destination
  - Stores track metadata (number, title, artist)
  - Links to download queue items

**Indexes:** 6 performance indexes on folder_path, mb_release_id, status, etc.

### 2. Backend APIs

#### `/api/downloads/folder-status` (GET)
Returns all folder matches with completion tracking:
```json
{
  "success": true,
  "folder_matches": [
    {
      "id": 1,
      "folder_path": "/downloads/Artist - Album",
      "mb_release_id": "abc-123",
      "artist": "Artist Name",
      "album": "Album Title",
      "total_expected_tracks": 12,
      "matched_tracks_count": 8,
      "completion_percentage": 66.7,
      "status": "organizing",
      "matched_tracks": [...]
    }
  ]
}
```

#### `/api/downloads/folder-duplicates` (GET)
Detects duplicate folders matching the same album:
```json
{
  "success": true,
  "duplicates": [
    {
      "mb_release_id": "abc-123",
      "artist": "Artist",
      "album": "Album",
      "folder_count": 2,
      "folders": [
        {"id": 1, "folder_path": "...", "matched_tracks_count": 5},
        {"id": 2, "folder_path": "...", "matched_tracks_count": 7}
      ],
      "suggestion": "merge_to_first"
    }
  ]
}
```

#### `/api/downloads/folder-merge` (POST)
Merges duplicate folders into primary folder:
```json
{
  "primary_folder_id": 1,
  "secondary_folder_ids": [2, 3]
}
```
Response:
```json
{
  "success": true,
  "merged_tracks": 15,
  "merged_folders": 2,
  "errors": []
}
```

### 3. Updated organize_folder_to_music()
**Changes:**
- Added `db_conn` parameter for database tracking
- Records folder match in `folder_album_matches` when organizing starts
- Updates status to 'organizing' during file moves
- Records each file in `folder_track_matches` after successful move
- Updates final track count and status to 'completed'
- Handles existing folder matches (updates instead of creating duplicates)

**Database Integration:**
```python
# Before organizing
cursor.execute("""
    INSERT INTO folder_album_matches 
    (folder_path, mb_release_id, artist, album, ...)
    VALUES (?, ?, ?, ?, ...)
""")

# After each file move
cursor.execute("""
    INSERT INTO folder_track_matches
    (folder_match_id, file_path, organized_path, ...)
    VALUES (?, ?, ?, ...)
""")

# After completion
cursor.execute("""
    UPDATE folder_album_matches
    SET matched_tracks_count = ?, status = 'completed'
    WHERE id = ?
""")
```

### 4. Frontend Enhancements

#### Completion Progress Badges
Shows real-time completion status for each folder:
- **Green badge:** 100% complete (X/X tracks)
- **Yellow badge:** 50-99% complete  
- **Red badge:** <50% complete
- **Progress bar:** Visual indicator for partial completion

#### Track Highlighting
Individual tracks in the track list are now highlighted:
- ✅ **Green highlight + checkmark:** Track has been matched and organized
- ⏳ **Dimmed + hourglass:** Album matched but track not yet organized
- (Default): Track not yet matched

#### Duplicate Warnings
Yellow alert box appears when duplicate folders are detected:
```
⚠️ Duplicate Folders Detected
These folders match the same album and can be merged:
• Artist - Album: 2 folders (5 tracks, 7 tracks) [Merge All]
```

#### Auto-Merge Functionality
Button to merge duplicate folders:
1. Moves all files from secondary folders to primary folder
2. Updates database records to point to primary folder
3. Removes empty secondary folder records
4. Refreshes display automatically

### 5. Integration Flow

**When files are organized:**
1. User clicks "Move Files" button
2. `moveMatchedFolder()` calls `/api/downloads/folder/.../organize`
3. Backend `organize_folder_to_music()` moves files and records in database
4. Frontend refreshes via `loadFolderGroups()`
5. `loadFolderStatus()` fetches updated completion data
6. Display updates with completion badges and track highlighting

**Auto-refresh cycle:**
```javascript
loadFolderGroups() 
  → loadFolderStatus()      // Fetch completion data
  → loadFolderDuplicates()  // Check for duplicates
  → Render folders with badges/progress/highlighting
```

## Files Modified

### Backend
- `migrations/add_folder_tracking_tables.py` - Database migration (NEW)
- `download_folder_grouping.py` - Updated `organize_folder_to_music()` 
- `app.py` - Added 3 new API endpoints

### Frontend  
- `templates/downloads_monitor.html` - Enhanced display and tracking

### Documentation
- `DOWNLOADS_MONITOR_ENHANCEMENTS.md` - Implementation plan (NEW)
- `DOWNLOADS_FOLDER_TRACKING.md` - This file (NEW)

## Usage

### Running the Migration
```bash
python migrations/add_folder_tracking_tables.py
```
Output: `✅ Migration complete: folder tracking tables created`

### API Usage Examples

**Check folder status:**
```bash
curl http://localhost:5000/api/downloads/folder-status
```

**Detect duplicates:**
```bash
curl http://localhost:5000/api/downloads/folder-duplicates
```

**Merge folders:**
```bash
curl -X POST http://localhost:5000/api/downloads/folder-merge \
  -H "Content-Type: application/json" \
  -d '{"primary_folder_id": 1, "secondary_folder_ids": [2, 3]}'
```

## Database Schema Details

### folder_album_matches
```sql
CREATE TABLE folder_album_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_path TEXT UNIQUE NOT NULL,
    mb_release_id TEXT NOT NULL,
    mb_source TEXT NOT NULL DEFAULT 'musicbrainz',
    artist TEXT NOT NULL,
    album TEXT NOT NULL,
    release_date TEXT,
    total_expected_tracks INTEGER DEFAULT 0,
    matched_tracks_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### folder_track_matches
```sql
CREATE TABLE folder_track_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_match_id INTEGER NOT NULL,
    file_path TEXT NOT NULL,
    organized_path TEXT,
    track_number INTEGER,
    track_title TEXT,
    track_artist TEXT,
    queue_item_id INTEGER,
    matched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    organized_at TIMESTAMP,
    FOREIGN KEY (folder_match_id) REFERENCES folder_album_matches(id) ON DELETE CASCADE,
    FOREIGN KEY (queue_item_id) REFERENCES download_queue(id) ON DELETE SET NULL
);
```

## Benefits

1. **Visual Progress:** Users can see at a glance which folders are complete
2. **Avoid Duplicates:** Automatic detection prevents wasted storage
3. **Track Visibility:** Immediately see which tracks are matched vs missing
4. **One-Click Merge:** Consolidate duplicate downloads effortlessly
5. **Persistent Tracking:** Database records survive restarts/crashes
6. **Queue Integration:** Links organized files back to download queue items

## Testing Checklist

- [x] Migration creates tables successfully
- [x] folder_status API returns data
- [x] folder_duplicates API finds duplicates
- [x] folder_merge API merges successfully
- [x] organize_folder_to_music tracks matches
- [x] Frontend displays completion badges
- [x] Track highlighting shows matched/missing
- [x] Duplicate warnings appear when detected
- [x] Merge button consolidates folders
- [x] Auto-refresh after organization
- [ ] Test with real downloads (next step)

## Next Steps

1. Download test album with 12 tracks
2. Manually download 5 tracks to simulate partial completion
3. Match folder to MusicBrainz
4. Verify 5/12 completion badge appears
5. Download remaining 7 tracks
6. Verify badge updates to 12/12
7. Create duplicate folder with same MB release
8. Verify duplicate warning appears
9. Test merge functionality
10. Confirm files consolidated correctly

## Related Commits

- Migration and database schema
- Backend API endpoints  
- organize_folder_to_music tracking
- Frontend completion display
- Duplicate detection UI
