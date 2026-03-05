# Active Queue & Downloads Folder Integration Guide

## Current State Overview

Your system already has comprehensive infrastructure for this workflow:

### Existing Components

1. **Download Queue Manager** (`download_queue_manager.py`)
   - Tracks items in `download_queue` table
   - Monitors for file completion
   - Updates statuses: `queued` → `searching` → `downloading` → `organized`

2. **MusicBrainz Release Manager** (`musicbrainz_release_manager.py`)
   - Creates release tracking entries
   - Adds individual tracks to queue
   - Links queue items to releases via `musicbrainz_release_tracks` table

3. **File Matching & Finalization**
   - `musicbrainz_file_matcher.py` - Discovers downloaded files
   - `musicbrainz_finalizer.py` - Moves files to permanent library
   - `folder_matching_enhancements.py` - Tracks folder organization progress

4. **Database Schema**
   - `musicbrainz_releases` - Release metadata & status
   - `musicbrainz_release_tracks` - Per-track download status
   - `download_queue` - Individual download items
   - `folder_album_matches` - Folder organization tracking

5. **Frontend Pages**
   - `/downloads` - Main downloads page
   - `/downloads/monitor` - Active queue monitor
   - `/downloads/discover/<category>` - Browse by category

## Your Requirements vs. Current Implementation

### ✅ Already Implemented

1. **Add to Queue from MusicBrainz**
   - `POST /api/musicbrainz/download` - Accepts release_id, creates tracking
   - Adds all queued tracks (user-selected subset) to `download_queue`
   - Creates `musicbrainz_release_tracks` entries

2. **Show Queue While Downloading**
   - Active queue visible at `/downloads/monitor`
   - Status tracking: queued, searching, downloading
   - Integration with Soulseek via slskd

3. **Update Album/Artist Status**
   - Partially implemented in `musicbrainz_releases`
   - Database entries created when download initiated
   - Shows "downloading" status

4. **Ready to Transfer Status**
   - `organized` status exists in `musicbrainz_release_tracks`
   - Set when files matched to queue items

5. **Finalization & Library Transfer**
   - Complete implementation in `musicbrainz_finalizer.py`
   - Moves to: `/music/ALBUM_ARTIST/RELEASE_YEAR - ALBUM/TRACK_NUMBER. ARTIST - TITLE`

### 🔄 Needs Enhancement

1. **Folder Creation Timing**
   - Currently created during finalization (Phase 5-6)
   - Your requirement: Create at queue time (Phase 1)
   - Current: `/downloads/Music/YEAR - ARTIST - ALBUM/`
   - Change to: Create immediately when tracks added to queue

2. **Auto Ready-to-Transfer Trigger**
   - When ALL tracks in that specific queue import are matched
   - Important: Based on import count, not full album count
   - If 2/12 songs queued, waits for those 2 only
   - Trigger is automatic when matched and AUTOMATICALLY TRANSFERS
   - No manual step required - transfer happens automatically

3. **Automatic Transfer on Ready**
   - Once auto-ready trigger fires
   - Immediately move files from /downloads/ to /music/
   - Delete monitoring folder
   - Update database status
   - Album appears in library immediately

4. **File Naming Configuration**
   - Need config.html option to change default format
   - Default: `TRACK#. ARTIST - SONG.mp3`
   - Allow user customization per preferences

5. **Immediate Folder Cleanup**
   - Delete monitoring folder immediately after transfer
   - Don't keep for verification

## Recommended Implementation Plan

### Phase 1: Folder Creation on Queue (Week 1)

**File:** `musicbrainz_release_manager.py` - Method: `start_release_download()`

```python
def start_release_download(self, release_id, release_title, artist, album_artist=None, year=None):
    """Create monitoring folder immediately when release download initiated"""
    
    # Current: Folder created during organization
    # Change to: Create folder here
    
    folder_name = f"{year or 'Unknown'} - {artist} - {release_title}"
    folder_path = os.path.join(self.downloads_dir, folder_name)
    
    os.makedirs(folder_path, exist_ok=True)
    
    # Update musicbrainz_releases table
    # monitoring_folder_path = folder_path (set immediately, not on finalization)
```

**Benefits:**
- Physical folder exists from moment user adds to queue
- Clear indication album is downloading
- Users can watch folder fill in real-time

### Phase 2: Enhanced Status Tracking (Week 1)

**File:** `download_queue_manager.py` & `app.py`

Add new API endpoint for UI display:

```
GET /api/queue/releases/status
Returns:
{
  "releases": [
    {
      "release_id": "abc-123",
      "title": "Album Name",
      "artist": "Artist Name",
      "total_tracks": 12,
      "status_breakdown": {
        "queued": 0,
        "downloading": 8,
        "ready_to_transfer": 4,
        "completed": 0
      },
      "completion_percentage": 66.7,
      "folder_path": "/downloads/Music/2024 - Artist - Album"
    }
  ]
}
```

### Phase 3: UI Integration (Week 2)

**Update `/downloads/monitor` page**

Current layout:
```
Active Queue
├── [All Items Listed]
└── Status: queued/searching/downloading
```

New layout:
```
Active Queue - By Release
├── Album: [Title]
│   ├── [Progress Bar] 4/12 tracks ready to transfer
│   ├── Folder: /downloads/Music/...
│   ├── Track List:
│   │   ├── 01. Song 1 [downloading 45%]
│   │   ├── 02. Song 2 [ready to transfer]
│   │   └── ...
│   └── [Transfer to Library] button (enabled when all ready)
├── Album: [Title 2]
│   └── [Progress Bar] 12/12 tracks ready to transfer
│       [Transfer to Library] button
```

### Phase 4: Automatic Transfer Logic (Week 2)

**File:** `app.py` - New endpoint for monitoring, trigger function

```python
@app.route("/api/queue/release/<release_id>/auto-transfer", methods=["POST"])
def auto_transfer_release(release_id):
    """
    Automatically triggered when all tracks in import are matched
    
    Process (automatic, no user action):
    1. Verify all queued tracks in 'organized' status
    2. Move from /downloads/Music/Album/ to /music/...
    3. Rename files with configured format
    4. Delete monitoring folder immediately
    5. Update release status to 'completed'
    
    NOTE: This is AUTOMATIC trigger - happens when ready
    Called by file matcher when last track is found
    """
```

This calls existing `musicbrainz_finalizer.py` logic but triggered automatically.

### Phase 6: Status Sync with Navidrome (Week 3)

**File:** Create `navidrome_sync.py`

After transfer & import:
1. Request Navidrome rescan
2. Verify album appears in library
3. Update `musicbrainz_releases` status to `completed` + import date
4. Remove from active downloads view

## Implementation Details

### Key Files to Modify

1. **`musicbrainz_release_manager.py`**
   - Create folder on `start_release_download()`
   - Add `monitoring_folder_path` to initial insert

2. **`download_queue_manager.py`**
   - Add `ready_to_transfer` status mapping
   - Update file discovery to set proper status

3. **`app.py`**
   - New endpoints:
     - `/api/queue/releases/status` (GET)
     - `/api/queue/release/<release_id>/transfer` (POST)
     - Update `/artists` and `/albums` routes

4. **Templates** (`downloads.html`, `artist.html`, `album.html`)
   - Add progress indicators
   - Show download status
   - Add transfer buttons

### Database Changes

Minimal changes - mostly use existing schema:

```sql
-- Add to download_queue table (if not exists)
ALTER TABLE download_queue ADD COLUMN IF NOT EXISTS ready_to_transfer_at TIMESTAMP;

-- Alias existing statuses for clarity
-- 'organized' → 'ready_to_transfer'
-- 'finalized' → 'completed'
```

Or update your mapping in Python:

```python
STATUS_MAPPING = {
    'queued': 'Queued',
    'searching': 'Searching',
    'downloading': 'Downloading',
    'discovered': 'Found',
    'organized': 'Ready to Transfer',  # Key status
    'finalized': 'Completed'
}
```

## Frontend Structure

### `/downloads/monitor` Enhanced

```html
<div class="release-group">
  <div class="release-header">
    <h3>{{ release.title }}</h3>
    <span class="artist">{{ release.artist }}</span>
  </div>
  
  <div class="progress-container">
    <div class="progress-bar">
      <div style="width: 66.7%">8/12 tracks ready</div>
    </div>
    <div class="status-breakdown">
      <span class="queued">0 Queued</span>
      <span class="downloading">4 Downloading</span>
      <span class="ready">8 Ready</span>
    </div>
  </div>
  
  <div class="folder-info">
    <small>📁 {{ release.folder_path }}</small>
  </div>
  
  <div class="track-list">
    {% for track in release.tracks %}
    <div class="track-item status-{{ track.status }}">
      <span class="number">{{ track.number }}</span>
      <span class="title">{{ track.title }}</span>
      <span class="status">{{ track.status_display }}</span>
    </div>
    {% endfor %}
  </div>
  
  {% if release.all_ready %}
  <button class="btn-transfer">Transfer to Library</button>
  {% endif %}
</div>
```

### `/artists` and `/albums` Badges

```html
<!-- On artist/album cards -->
<div class="album-card">
  <h4>Album Title</h4>
  {% if downloading %}
  <span class="badge badge-downloading">
    ⬇️ Downloading (4/12)
  </span>
  {% endif %}
  {% if ready_to_transfer %}
  <span class="badge badge-ready">
    ✓ Ready (8/12)
  </span>
  {% endif %}
</div>
```

## Testing Strategy

1. **Phase 1 Test**
   - Add MusicBrainz release
   - Verify folder created immediately
   - Check database entries

2. **Phase 2 Test**
   - Call `/api/queue/releases/status`
   - Verify accurate track counts per status

3. **Phase 3 Test**
   - Browse `/downloads/monitor`
   - Verify visual layout and data

4. **Phase 4 Test**
   - Manually move files to ready state
   - Call transfer endpoint
   - Verify files in music library

5. **Phase 5 Test**
   - Browse `/artists` and `/albums`
   - Verify badges show correct counts

6. **Integration Test**
   - Full end-to-end: Queue → Download → Transfer → Library

## API Reference

### GET `/api/queue/releases/status`
Returns all active releases with status breakdown.

### GET `/api/queue/release/<release_id>`
Returns all tracks in release with status.

### POST `/api/queue/release/<release_id>/transfer`
Transfers all ready-to-transfer tracks to library.

### POST `/api/slskd/download` (Existing)
Initiates Soulseek download for album.

### GET `/api/musicbrainz/downloads` (Existing)
Lists all MusicBrainz downloads.

## Database Views (Optional)

For performance, create views:

```sql
CREATE VIEW release_status_summary AS
SELECT 
    mr.release_id,
    mr.release_title as title,
    mr.artist,
    mr.release_year as year,
    mr.monitoring_folder_path,
    COUNT(mrt.id) as total_tracks,
    SUM(CASE WHEN mrt.status = 'queued' THEN 1 ELSE 0 END) as queued_count,
    SUM(CASE WHEN mrt.status = 'downloading' THEN 1 ELSE 0 END) as downloading_count,
    SUM(CASE WHEN mrt.status IN ('organized', 'discovered') THEN 1 ELSE 0 END) as ready_count,
    SUM(CASE WHEN mrt.status = 'finalized' THEN 1 ELSE 0 END) as completed_count
FROM musicbrainz_releases mr
LEFT JOIN musicbrainz_release_tracks mrt ON mr.release_id = mrt.release_id
WHERE mr.status != 'completed'
GROUP BY mr.release_id;
```

## Success Criteria

✅ User adds MusicBrainz album to queue
✅ Folder created immediately in `/downloads/Music/Artist - Album/`
✅ Queue shows album with track progress
✅ Each track shows current status
✅ Once all tracks downloaded, "Ready to Transfer" shows
✅ User clicks "Transfer to Library"
✅ Files renamed and moved to music library with proper naming
✅ Artists/Albums views updated with download badge
✅ Album appears in Navidrome after import

## Timeline

- **Week 1:** Phases 1-2 (Folder creation, Status API)
- **Week 2:** Phases 3-4 (UI, Batch transfer)
- **Week 3:** Phase 5-6 (Views, Navidrome sync)

**Total Effort:** ~40-50 development hours

## Notes

- Existing infrastructure handles ~80% already
- Main work is UI integration and Navidrome sync
- Database changes minimal - mostly status mapping
- Can use existing finalizer code directly
- Recommend incremental rollout - test each phase

