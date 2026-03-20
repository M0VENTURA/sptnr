# MusicBrainz Release Download - Implementation Progress

## Completed (Phase 1-3)

### Database Schema ✅
- `musicbrainz_releases` - Track active release downloads with monitoring folders
- `musicbrainz_release_tracks` - Track individual track status and files
- `download_queue` columns - Added `release_id`, `track_number`, `is_final_file`, `mb_release_download_id`

### Core Manager Class ✅
- `MusicBrainzReleaseManager` in `musicbrainz_release_manager.py`
- Methods implemented:
  - `fetch_release_from_musicbrainz()` - Get release details from MB API
  - `create_monitoring_folder()` - Create folder: `YEAR - ARTIST - ALBUM`
  - `create_release_entry()` - Database tracking of releases
  - `add_release_tracks_to_queue()` - Add each track to download queue
  - `start_release_download()` - Main entry point
  - `get_active_releases()` - List all active downloads
  - `get_release_tracks()` - Get track status for a release

### API Endpoints ✅
- `POST /api/musicbrainz/release/<release_id>/start` - Start downloading a release
- `GET /api/musicbrainz/releases/active` - List active releases with progress
- `GET /api/musicbrainz/release/<release_id>` - Get release details and tracks
- `GET /api/queue/release/<release_id>` - Get queue items for a release

## Next Steps (Phase 4-7)

### Phase 4: Queue Display & Download Management
**Files to modify:**
- `downloads_monitor.html` - Add section to show active MusicBrainz releases
- Integrate release track items into active queue view
- Show progress: "Discovering 3 of 12 tracks"

**Key features:**
- Display as "Artist - Title" in queue
- Show track number and release context
- Status badges: queued, searching, downloading, discovered

### Phase 5: File Matching & Movement
**New file:** `musicbrainz_file_matcher.py`

**Functionality needed:**
- Monitor `/downloads/` for new files
- Match files to release tracks using:
  - Filename similarity (fuzzy matching)
  - ID3 tags (artist, title, track number)
- Move matching files to monitoring folder:
  - `/downloads/Music/2026 - Artist - Album/filename.mp3`
- Update track status to `organized`
- Track progress: "Organized 3 of 12 tracks"

**Logic:**
```python
def match_file_to_release(filepath, release_id):
    # Try tag matching first
    # Fallback to filename similarity
    # Update queue item and track tables
    # Move to monitoring folder
    # Update release progress counts
```

### Phase 6: Download Queue Integration
**Modify:** `downloads_queue.py` (if exists) or equivalent

**Features needed:**
- Process queue items with `release_id != null`
- Initiate slskd searches for track artist/title
- Handle retries per track
- Track completion automatically via file discovery

### Phase 7: Release Finalization
**New endpoint & logic:**

**Trigger:** When `organized_count == total_tracks`

**Process:**
1. Get all files from monitoring folder
2. For each file:
   - Parse ID3 tags or filename for track number
   - Generate final filename: `01. Artist - Title.mp3`
   - Create final directory: `/music/ARTIST/YEAR - ALBUM/`
   - Move and rename file
3. Delete monitoring folder
4. Update release status to `finalized`

**Logic:**
```python
def finalize_release(release_id):
    # Get all files in monitoring folder
    # Parse track info from ID3/filename
    # Create final directory structure
    # Move/rename files
    # Cleanup monitoring folder
    # Update database
```

## Data Flow

```
User Search MB
    ↓
SELECT Release → Download Release
    ↓
fetch_release_from_musicbrainz()
    ↓
create_monitoring_folder()
create_release_entry()  
add_release_tracks_to_queue()
    ↓
Each track as queue item:
  - status: 'queued'
  - release_id links to release
  - Display in Active Queue
    ↓
Queue processor initiates searches:
  - status: 'searching'
  - Update when file found: 'downloading'
    ↓
File discovery process:
  - Match file to track
  - Move to monitoring folder
  - status: 'organized'
  - Update release organized_count
    ↓
Release complete check:
  - If all tracks organized
  - Finalize: rename, move to /music/
  - Cleanup monitoring folder
  - status: 'finalized'
```

## Frontend Integration Points

### 1. MusicBrainz Search Results (downloads.html line 315)
Currently shows individual release downloads. Should add:
- "Download Full Release" button
- Show how many tracks will be added to queue
- Highlight monitoring folder that will be created

### 2. Active Queue Display (downloads_monitor.html line ~500)
Should show section:
- Active Releases
  - Release name
  - Artist
  - Progress: "3 of 12 tracks discovered"
  - List of tracks with status badges
  - Release-level actions: pause, cancel finalize

### 3. Settings
Consider adding:
- Auto-finalize when complete
- Keep monitoring folder after finalization (archive)
- Folder naming conventions

## Testing Checklist

- [ ] Start a release download
- [ ] Verify queue items created
- [ ] Check monitoring folder created
- [ ] File matching logic works
- [ ] Files move to monitoring folder
- [ ] Progress tracked correctly
- [ ] Release finalizes and moves to /music/
- [ ] Original monitoring folder deleted
- [ ] Concurrent releases handled
- [ ] Retry logic for failed tracks

## Migration Notes

Existing download system (`managed_downloads`) is kept separate:
- Used for playlist sessions
- Integrates with search result selection
- New release system runs parallel
- Can migrate existing downloads to new system if needed

## Performance Considerations

- Use database queries with indexes on `release_id`, `status`
- Batch file operations for performance
- Consider limiting concurrent releases
- Cache MusicBrainz responses
- Async file operations to avoid blocking

## Future Enhancements

1. Smart duplicate detection (same release, different sources)
2. Artist preferences (preferred version, remaster)
3. Playlist generation after finalization
4. Release metadata fetching (cover art, release notes)
5. A/B release comparison (different versions)
6. Scheduled automatic downloads (weekly new releases)
