# MusicBrainz Release Download - Remaining Phases Analysis

**Date Created:** March 5, 2026
**Status:** Review & Planning for Phases 4-7

---

## Executive Summary

**Completed Work (Phases 1-3):**
- ✅ Database schema with `musicbrainz_releases` and `musicbrainz_release_tracks` tables
- ✅ MusicBrainzReleaseManager class with core functionality
- ✅ API endpoints for starting releases and getting status
- ✅ Dynamic monitoring folder naming: `YEAR - ARTIST - ALBUM` (e.g., `2026 - Angus McSix - Angus McSix and the All Seeing Astral Eye`)
- ✅ Track-by-track queue item creation with "downloading" status on artist/album pages
- ✅ User-Agent header consistency fixed

**Remaining Work (Phases 4-7):**
- ⏳ **Phase 4:** Queue display integration in downloads_monitor.html
- ⏳ **Phase 5:** File matching and movement logic to monitoring folders
- ⏳ **Phase 6:** Auto-finalization when all tracks found
- ⏳ **Phase 7:** Release cleanup and database sync

---

## Phase 4: Queue Display Integration (Folder Groups)

### Current State
- API endpoint `GET /api/musicbrainz/releases/active` exists and returns releases with progress
- downloads_monitor.html already has **"Folder Groups Section"** (line ~160) for organized folder-based monitoring
- This existing system is perfect for integrating MusicBrainz releases as a special folder type

### ✅ Implementation Complete

**Files Created/Modified:**

- ✅ `musicbrainz_folder_integration.py` - Helper functions for folder operations
- ✅ API endpoint: `GET /api/downloads/folder-groups` - Get all releases with discovered files
- ✅ API endpoint: `GET /api/downloads/folder/<path>` - Get folder details and all files
- ✅ API endpoint: `POST /api/musicbrainz/release/<id>/retry-match` - Retry file matching
- ✅ API endpoint: `POST /api/downloads/folder/<path>/cancel` - Cancel folder and remove from queue
- ✅ `static/js/musicbrainz-folder-groups.js` - Frontend with display, filtering, auto-refresh
- ✅ `templates/downloads_monitor.html` - Integrated into folder groups section

### Display Behavior

**MusicBrainz Releases shown as GREEN folder entries with:**

- Green background and left border (4px green line)
- "Release" badge (green) to distinguish from regular folders
- Disc icon (💿) instead of folder icon
- Progress bar in green
- Actual discovered files listed (e.g., "Track_01_Downloaded.mp3")
- Status labels: "Ready to Finalize" (complete), "In Progress", "Waiting"
- Action buttons: View, Retry Matching, Cancel

**Progress Display:**

- Shows "X of Y tracks discovered" below each folder
- Files displayed as they're discovered
- Auto-refresh every 5 seconds
- Pauses refresh when tab is not visible (saves bandwidth)

**Filtering:**

- "All" - Shows both releases and regular folders
- "Releases" - Shows only MusicBrainz releases
- "Folders" - Shows only regular folder groups

### Architecture Design

**Integration Strategy:**
- MusicBrainz releases appear as **green folder entries** in the existing "Downloads Organized by Folder" section
- Initial display: Shows MusicBrainz release metadata (artist, album, year, track count)
- As files are matched to tracks: Transition to show actual discovered files instead of track listing
- Monitoring folder name shows progress: "2026 - Artist - Album [9/12]"

### Required Implementation

#### 4a. API Endpoint for Folder Groups

Create new endpoint to get combined view:

```python
@app.route("/api/downloads/folder-groups", methods=["GET"])
def api_get_folder_groups():
    """Get organized folder groups including MusicBrainz releases"""
    try:
        from musicbrainz_release_manager import get_manager
        
        # Get MusicBrainz releases with folder info
        manager = get_manager()
        mb_releases = manager.get_active_releases()
        
        # Convert to folder group format
        folder_groups = []
        for release in mb_releases:
            folder_groups.append({
                "type": "musicbrainz",  # Mark as MB release (green)
                "name": release["monitoring_folder"],
                "display_name": f"{release['release_title']} ({release['artist']} - {release['release_year']})",
                "release_id": release["release_id"],
                "total_tracks": release["total_tracks"],
                "discovered_count": release["discovered_count"],
                "progress_percent": release["progress_percent"],
                "status": "active",
                "files": get_files_in_folder(release["monitoring_folder"]),  # Get actual files, not just track list
                "metadata": {
                    "artist": release["artist"],
                    "album": release["release_title"],
                    "year": release["release_year"],
                    "source": "musicbrainz"
                }
            })
        
        # Get other folder groups (legacy folder grouping from processAlbums)
        # These would be merged here if needed
        
        return jsonify({
            "success": True,
            "count": len(folder_groups),
            "folder_groups": folder_groups
        })
    except Exception as e:
        logging.error(f"Error getting folder groups: {e}")
        return jsonify({"error": str(e)}), 500
```

#### 4b. Update Folder Groups Display Function

Modify existing `loadFolderGroups()` or create `loadFolderGroupsWithMB()`:

```javascript
async function loadFolderGroupsWithMB() {
  try {
    // Replace or augment existing folder groups with MusicBrainz releases
    const response = await fetch('/api/downloads/folder-groups');
    const data = await response.json();
    
    if (!data.success || data.count === 0) {
      document.getElementById('folderGroupsSection').style.display = 'none';
      return;
    }
    
    document.getElementById('folderGroupsSection').style.display = 'block';
    document.getElementById('folderGroupsBadge').textContent = data.count;
    
    const html = data.folder_groups.map((group) => {
      const isMusicBrainz = group.type === 'musicbrainz';
      const badgeColor = isMusicBrainz ? 'bg-success' : 'bg-secondary';  // Green for MB, gray for others
      const icon = isMusicBrainz ? 'bi-disc' : 'bi-folder';
      
      // Initial state: Show track list (from MusicBrainz)
      // Or actual files if files exist in folder
      const displayList = group.files.length > 0 
        ? group.files.map(f => `<small>📁 ${f.name}</small>`).join('<br>')
        : `<small class="text-muted">Waiting for ${group.total_tracks} tracks...</small>`;
      
      return `
        <div class="list-group-item" style="background-color: ${isMusicBrainz ? '#f0fff4' : ''};">
          <div class="d-flex justify-content-between align-items-start">
            <div style="flex: 1;">
              <div class="d-flex align-items-center gap-2 mb-1">
                <i class="bi ${icon}"></i>
                <h6 class="mb-0">${group.display_name}</h6>
                <span class="badge ${badgeColor}" style="font-size: 0.75rem;">
                  ${isMusicBrainz ? 'Release' : 'Folder'}
                </span>
              </div>
              
              <!-- Progress Bar -->
              <div class="progress mb-2" style="height: 20px;">
                <div class="progress-bar ${isMusicBrainz ? 'bg-success' : 'bg-info'}" 
                     style="width: ${group.progress_percent}%">
                  <small>${group.progress_percent}%</small>
                </div>
              </div>
              
              <!-- Track/File Listing -->
              <div style="font-size: 0.9rem; max-height: 150px; overflow-y: auto;">
                ${displayList}
              </div>
              
              <!-- Stats -->
              <small class="text-muted mt-2 d-block">
                ${group.discovered_count} of ${group.total_tracks} tracks discovered
              </small>
            </div>
            
            <!-- Actions -->
            <div class="btn-group btn-group-sm ms-2">
              <button class="btn btn-outline-info" onclick="viewFolderContents('${group.name}')" title="View folder">
                <i class="bi bi-folder-open"></i>
              </button>
              <button class="btn btn-outline-warning" onclick="retryMatching('${group.release_id}')" title="Retry matching">
                <i class="bi bi-arrow-repeat"></i>
              </button>
              <button class="btn btn-outline-danger" onclick="cancelFolder('${group.name}')" title="Cancel">
                <i class="bi bi-x"></i>
              </button>
            </div>
          </div>
        </div>
      `;
    }).join('');
    
    document.getElementById('folderGroupsList').innerHTML = 
      `<div class="list-group list-group-flush">${html}</div>`;
    
  } catch (error) {
    console.error('Error loading folder groups:', error);
  }
}
```

#### 4c. Update Status Card with MusicBrainz Count

Modify the status cards to show MusicBrainz releases specifically:

```javascript
async function updateStatusCards() {
  const mbResponse = await fetch('/api/musicbrainz/releases/active');
  const mbData = mbResponse.json();
  
  // Add MusicBrainz count to status
  const mbCount = mbData.count || 0;
  document.getElementById('queueActiveCount').textContent = mbCount + ' MB Releases';
}
```

#### 4d. Auto-Refresh Integration

```javascript
// Refresh every 5 seconds when releases are active
setInterval(async () => {
  await loadFolderGroupsWithMB();
  await updateStatusCards();
}, 5000);

// Initial load
loadFolderGroupsWithMB();
updateStatusCards();
```

### Expected Display

```
┌─ Downloads Organized by Folder [2] ────────────────────────────┐
│                                                                 │
│ 💿 2026 - Angus McSix - Album Name [Release]                   │
│ ▓▓▓▓▓▓▓▓░░░░░░░░  75%                                           │
│ 📁 Track_01_Downloaded.mp3                                      │
│ 📁 Track_03_Downloaded.flac                                     │
│ 📁 Track_05_Downloaded.mp3                                      │
│ ... 6 more tracks waiting                                       │
│ 9 of 12 tracks discovered              👁 🔄 ✕                │
│                                                                 │
│ 🎵 Song Name (Other Artist - Album) [Folder]                   │
│ ▓░░░░░░░░░░░░  10%                                              │
│ 📁 downloaded_song.mp3                                          │
│ 1 of 1 track found                     👁 🔄 ✕                │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**Key Visual Differences:**
- Green background for MusicBrainz releases (distinguishes them)
- "Release" badge (green) vs "Folder" badge (gray)
- Disc icon (💿) for MB releases vs folder icon (🎵)
- Progress bar color: green for MB, blue for folders
- Shows **actual files** that matched, not just track names
- Shows progress: "9 of 12 tracks discovered"

### Improvements & Features

1. **Smart Display Transition:**
   - Initial: Shows "Waiting for 12 tracks..."
   - As files match: Shows actual file names as they're discovered
   - Complete: Shows all 12 files plus "Ready to finalize"

2. **Expandable File List:** Click to expand/collapse full list

3. **One-Click Finalize:** When complete, single button to move to /music/

4. **Match Confidence:** Show "High (95%)" for well-matched files vs "Low (72%)" for uncertain

5. **Retry Matching:** If file didn't match automatically, user can force retry

6. **Unified Interface:** Regular folders and MB releases side-by-side, same treatment

---

## Phase 5: File Matching & Movement Logic

### Current State
- ✅ Files discovered in `/downloads/Music` automatically
- ✅ Files matched to release tracks using multi-strategy algorithm
- ✅ Matched files moved to monitoring folders
- ✅ Database updated with discovered status and file paths
- ✅ Background task integrated into queue processor (runs every 30 seconds)

### ✅ Implementation Complete

**File Created:**
- ✅ `musicbrainz_file_matcher.py` - Complete file matching system

**API Endpoint:**
- ✅ `POST /api/musicbrainz/check-files` - Trigger file matching (called automatically)

**Integration:**
- ✅ `queue_processor.py` - Added `maybe_check_musicbrainz_files()` to main loop
- ✅ Runs automatically every 30 seconds alongside auto-discovery

### Matching Algorithm

**Strategy Priority (Confidence Scoring):**

1. **ISRC Code Match** (100% confidence)
   - Read ISRC from ID3 tags
   - Compare against MusicBrainz release track ISRC
   - Perfect accuracy if available

2. **ID3 Tag Matching** (95-99% confidence)
   - Extract artist + title from ID3 tags
   - Exact match: 99%
   - Fuzzy match (SequenceMatcher): Title 70%, Artist 30% weighted = 95%+
   - Threshold: > 85% similarity

3. **Filename Similarity** (80-90% confidence)
   - Parse filename (remove extension)
   - Compare against "track_title track_artist"
   - SequenceMatcher ratio >= 80%

**Confidence Threshold:** 75% minimum to accept match and move file

### Key Features

**File Discovery:**
- Scans `/downloads/Music` recursively
- Skips files already in monitoring folders
- Skips non-audio formats (only .mp3, .flac, .m4a, .ogg, .wav, .aac, .wma)
- Skips corrupted files (< 50KB = too small)

**Metadata Extraction:**
- Uses Mutagen library (ID3v2.4 compatible)
- Handles multiple tag formats (TIT2, TPE1, TSRC for ID3)
- Fallback: Parse from filename if ID3 tags not available
- Graceful error handling

**File Movement:**
- Source: `/downloads/Music/Song Name.mp3`
- Destination: `/downloads/Music/YEAR - Artist - Album/Song Name.mp3`
- Preserves original filename (no renaming yet)
- Handles collisions: versioned filenames if exists
- Atomic file operations with error recovery

**Database Updates:**
```python
UPDATE musicbrainz_release_tracks
SET status = 'discovered',
    found_filename = 'Song Name.mp3',
    file_path = '/downloads/Music/2026 - Artist - Album/Song Name.mp3'
WHERE release_id = ? AND track_number = ?

UPDATE musicbrainz_releases
SET discovered_count = (COUNT of discovered tracks)
WHERE release_id = ?
```

### Logging & Monitoring

**Log Prefix:** `[FILE_MATCHER]` for easy tracking

**Log Examples:**
```
[FILE_MATCHER] Starting file discovery and matching...
[FILE_MATCHER] Found 42 unmatched files
[FILE_MATCHER] ISRC match: song.mp3 -> Track 3
[FILE_MATCHER] ID3_exact match: song.mp3 -> Track 5 (99%)
[FILE_MATCHER] filename match: song.mp3 -> Track 7 (87%)
[FILE_MATCHER] Matched {matched}/{total} files
[FILE_MATCHER] Moved song.mp3 -> 2026 - Artist - Album/song.mp3
[FILE_MATCHER] Updated database for track 3 (confidence: 99%)
```

### Architecture Design

**New File:** `musicbrainz_file_matcher.py`

#### 5a. Core File Matching Algorithm

```python
class MusicBrainzFileMatcher:
    """Match files to release tracks and move to monitoring folder"""
    
    def monitor_and_match(self, release_id):
        """
        Main loop:
        1. Scan /downloads/Music for files
        2. Match to release tracks
        3. Move to monitoring folder
        4. Update database
        """
        pass
    
    def find_files_in_downloads(self):
        """Find all unorganized music files in /downloads/Music"""
        # Scan for .mp3, .flac, .m4a, .ogg, .wav
        # Ignore files already in monitoring folders
        pass
    
    def match_file_to_track(self, filepath, release_id):
        """
        Match using multiple strategies:
        
        Strategy 1: ID3 Tag Matching (Best Match)
        - Read ID3 tags from file
        - Extract artist, title, track number
        - Compare to release tracks
        - Confidence: High if exact match
        
        Strategy 2: Filename Similarity (Fallback)
        - Parse filename (remove extensions, cleanup)
        - Use fuzzy matching (difflib.SequenceMatcher)
        - Match against release track titles
        - Confidence: Medium if similarity > 80%
        
        Strategy 3: ISRC Matching (If available)
        - Read ISRC from ID3
        - Compare to release track ISRC (from MusicBrainz)
        - Confidence: Very high if ISRC matches
        
        Returns: (track_number, confidence_score)
        """
        pass
    
    def move_to_monitoring_folder(self, filepath, release_id, track_number):
        """
        Move file to monitoring folder with preservation:
        - Source: /downloads/Music/Song Name.mp3
        - Dest: /downloads/Music/2026 - Artist - Album/Song Name.mp3
        - Preserve original filename (don't rename yet)
        """
        pass
```

#### 5b. Integration with Queue Processor

Modify the existing queue processor or create new endpoint:

```python
@app.route("/api/musicbrainz/check-files", methods=["POST"])
def api_check_files():
    """Background task to discover and match files to releases"""
    # Called periodically (every 30 seconds)
    # Scan for new files
    # Match to active releases
    # Move to monitoring folders
    # Update release discovered_count
```

#### 5c. Database Updates

Update `musicbrainz_release_tracks` table when file is found:

```python
cursor.execute("""
    UPDATE musicbrainz_release_tracks
    SET status = 'discovered', 
        found_filename = ?,
        file_path = ?,
        updated_at = CURRENT_TIMESTAMP
    WHERE release_id = ? AND track_number = ?
""", (filename, full_path, release_id, track_number))

# Update release progress
cursor.execute("""
    UPDATE musicbrainz_releases
    SET discovered_count = (
        SELECT COUNT(*) FROM musicbrainz_release_tracks 
        WHERE release_id = ? AND status = 'discovered'
    ),
    updated_at = CURRENT_TIMESTAMP
    WHERE release_id = ?
""", (release_id, release_id))
```

### Matching Algorithm Details

**ID3 Tag Priority Order:**
1. Artist + Title exact match (100%)
2. Artist + Title fuzzy match > 85%
3. Album artist + Title
4. ISRC code match
5. Filename similarity > 80%

**Confidence Scoring:**
```
Score = (tag_match_score * 0.7) + (filename_score * 0.3)

Where:
- tag_match_score: 1.0 (exact) or 0.0-0.99 (fuzzy)
- filename_score: calculated from SequenceMatcher ratio
```

**Validation Before Moving:**
- File must be >= 2 seconds (avoid corrupted files)
- Confidence >= 75%
- File doesn't already exist in destination
- Disk space available in monitoring folder

### Improvements to Consider

1. **Duplicate Detection:** Track files that match multiple tracks
2. **Manual Override:** UI to manually assign files to tracks
3. **Quarantine:** Move low-confidence matches to review folder
4. **Resume:** Remember which files were already processed

---

## Phase 6: Auto-Finalization

### Current State
- ✅ Release stored in database with status tracking  
- ✅ discovered_count field exists and updated automatically
- ✅ Auto-finalization implemented and integrated into background loop

### ✅ Implementation Complete

**File Created:**
- ✅ `musicbrainz_finalizer.py` - Complete finalization system

**API Endpoints:**
- ✅ `POST /api/musicbrainz/check-finalization` - Automatic finalization trigger
- ✅ `POST /api/musicbrainz/release/<id>/finalize` - Manual finalization for testing
- ✅ `GET /api/musicbrainz/release/<id>/finalization-progress` - Check progress

**Integration:**
- ✅ `queue_processor.py` - Added `maybe_finalize_musicbrainz_releases()` to main loop
- ✅ Runs automatically every 60 seconds
- ✅ Logs with `[FINALIZER]` prefix for easy debugging

### Finalization Algorithm

**Detection Trigger:**
```sql
WHERE status = 'active'
AND discovered_count >= total_tracks
```

When a release has all tracks discovered, the finalizer:

1. **Retrieves Release Info**
   - Release title, artist, year from database
   - Monitoring folder path
   - Total track count

2. **Gets All Files from Monitoring Folder**
   - Lists files in `/downloads/Music/2026 - Artist - Album/`
   - Matches to tracks in database via found_filename

3. **Creates Final Directory Structure**
   - Path: `/music/ARTIST/YEAR - ALBUM/`
   - Creates artist and album subdirectories
   - Handles path length limits (200 char max)

4. **Moves and Renames Files**
   - Source: `/downloads/Music/2026 - Artist - Album/Song.mp3`
   - Destination: `/music/Artist/2026 - Album/01. Artist - Song.mp3`
   - Filename format: `NN. Artist - Title.ext` (track number padded to 2 digits)
   - Preserves file extension
   - Handles collisions: overwrites if destination exists

5. **Updates Database**
   - Sets track status: `discovered` → `finalized`
   - Sets track file_path: Full path in /music/
   - Sets release status: `active` → `finalized`
   - Sets finalized_at: Current timestamp

6. **Cleanup**
   - Removes empty monitoring folder (silently fails if not empty)
   - Logs cleanup status

### Key Features

**Smart Detection:**
- Checks every 60 seconds (configurable)
- Scans for releases with discovered_count >= total_tracks
- Processes one release at a time to avoid bottlenecks

**Robust File Handling:**
- Matches files to tracks using database found_filename field
- Handles missing track metadata (uses "00. filename" fallback)
- Collision handling: overwrites duplicates
- Atomic file operations with error recovery

**Database Integrity:**
- Transactional updates (all-or-nothing)
- Sets status='finalized' and records finalized_at timestamp
- Updates track file_path for library integration

**Error Resilience:**
- Graceful handling of missing folders
- Continues even if optional cleanup fails
- Logs all errors with [FINALIZER] prefix
- Never crashes the main processor loop

### Logging

**Prefix:** `[FINALIZER]`

**Log Examples:**
```
[FINALIZER] Checking for releases ready to finalize...
[FINALIZER] Found 3 releases ready for finalization
[FINALIZER] Finalizing release 12345abc...
[FINALIZER] Created final directory: /music/Artist/2026 - Album
[FINALIZER] Moved Song.mp3 → 01. Artist - Song.mp3
[FINALIZER] Moved Song2.mp3 → 02. Artist - Song2.mp3
[FINALIZER] Moved 3/3 files to final location
[FINALIZER] Removed empty monitoring folder: 2026 - Artist - Album
[FINALIZER] Successfully finalized release 12345abc
[FINALIZER] Finalized 3/3 releases
```

### API Usage Examples

**Check Finalization Automatically:**
```bash
# Called by queue processor every 60 seconds (automatic)
curl -X POST http://localhost:5000/api/musicbrainz/check-finalization
```

**Manual Finalization (for testing):**
```bash
curl -X POST http://localhost:5000/api/musicbrainz/release/12345abc/finalize
```

**Check Progress Toward Finalization:**
```bash
curl http://localhost:5000/api/musicbrainz/release/12345abc/finalization-progress
```

**Response Example:**
```json
{
  "success": true,
  "progress": {
    "release_id": "12345abc",
    "title": "Album Name",
    "artist": "Artist",
    "year": 2026,
    "total_tracks": 12,
    "discovered_count": 10,
    "status": "active",
    "ready_to_finalize": false,
    "finalized_at": null
  }
}
```

### Architecture Flow

```
┌─ Queue Processor Loop (Every 60s) ─────────────────────┐
│                                                         │
└─→ Check MusicBrainz Release Finalization               │
    ├─ Find releases with discovered_count >= total     │
    ├─ Create final directory: /music/ARTIST/YEAR-ALBUM │
    ├─ Move files from monitoring folder                │
    │  ├─ Rename: NN. Artist - Title.ext               │
    │  ├─ Update file_path in database                 │
    │  └─ Update track status to finalized             │
    ├─ Cleanup empty monitoring folder                 │
    └─ Update release status: active → finalized       │
```

### Configuration

**Finalization Check Interval:**
```python
# In queue_processor.py
maybe_finalize_musicbrainz_releases(now_ts, last_run_ts, interval_seconds=60)
```

**Tuneable Parameters:**
- Check interval: 60 seconds (every minute)
- Path length limit: 200 characters (artist/album)
- Extension preservation: Maintain original audio format
- Overwrite behavior: Replace existing files

### Data Flow Example

**Before Finalization:**
```
/downloads/Music/
└─ 2026 - Radiohead - A Moon Shaped Pool/
   ├─ track_01.flac
   ├─ track_02.flac
   └─ track_03.flac

Database:
musicbrainz_releases:
  - status: active
  - discovered_count: 3
  - total_tracks: 3

musicbrainz_release_tracks:
  - track_01: status=discovered, file_path=/downloads/.../track_01.flac
  - track_02: status=discovered, file_path=/downloads/.../track_02.flac
  - track_03: status=discovered, file_path=/downloads/.../track_03.flac
```

**After Finalization:**
```
/music/
└─ Radiohead/
   └─ 2026 - A Moon Shaped Pool/
      ├─ 01. Radiohead - Burn the Witch.flac
      ├─ 02. Radiohead - Daydreaming.flac
      └─ 03. Radiohead - Decks Dark.flac

Database:
musicbrainz_releases:
  - status: finalized
  - finalized_at: 2026-03-05 12:34:56
  - discovered_count: 3

musicbrainz_release_tracks:
  - track_01: status=finalized, file_path=/music/Radiohead/2026-.../01...flac
  - track_02: status=finalized, file_path=/music/Radiohead/2026-.../02...flac
  - track_03: status=finalized, file_path=/music/Radiohead/2026-.../03...flac
```

### Integration with Other Phases

**Phase 5 → Phase 6:**
- Phase 5 discovers files and moves to monitoring folders
- Phase 6 moves from monitoring folders to final location
- Both use discovered_count as shared progress metric

**Phase 6 → Downstream:**
- Files now in /music/ library directory
- Ready for tag scanning (popularity.py)
- Ready for playlist matching (playlist_matcher.py)
- Database tracks file_path updated to final location

### Edge Cases & Error Handling

**Scenario 1: Database Track Not Found**
- Problem: File exists but no database match
- Solution: Use "00. filename" naming
- Result: File still moved to final location

**Scenario 2: Destination File Exists**
- Problem: File already at /music/Artist/Album/
- Solution: Overwrite existing file
- Result: Newer version replaces old one

**Scenario 3: Directory Creation Fails**
- Problem: Permission denied or invalid path
- Solution: Log error and return false
- Result: Release stays in 'active' status, retry next cycle

**Scenario 4: Monitoring Folder Not Empty**
- Problem: Other files in monitoring folder
- Solution: Silently skip cleanup attempt
- Result: Folder left in place for manual review

**Scenario 5: Database Connection Lost**
- Problem: Can't update database
- Solution: Revert file operations if possible
- Result: Log error, retry on next cycle

### Performance Characteristics

**Throughput:**
- **Typical:** 3-5 releases finalized per 60-second interval
- **File moves:** ~100-500ms per file (depends on size/destination)
- **Database:** ~50ms per transaction

**Resource Usage:**
- **Memory:** Minimal (file listing only)
- **CPU:** Low (I/O bound operation)
- **Disk I/O:** Only during file moves
- **Database:** One update per track + one per release

### Testing Checklist

- [ ] Create test release with 3 tracks
- [ ] Manually download all 3 files
- [ ] Watch queue processor logs for [FINALIZER] messages  
- [ ] Verify files moved to /music/Artist/YEAR - Album/
- [ ] Verify files renamed with track numbers
- [ ] Check database: status changed to 'finalized'
- [ ] Check finalized_at timestamp is set
- [ ] Verify original monitoring folder is removed
- [ ] Check /downloads/Music for cleanup

### Future Enhancement Ideas

1. **Batch Finalization:** Process multiple releases in parallel
2. **Selective Finalization:** Option to keep monitoring folder
3. **Backup Before Move:** Copy to backup location first
4. **Validation Step:** Verify all files after move
5. **Notification:** Alert user when finalization complete
6. **Soft Links:** Link to finalized files from monitoring folder


#### 6a. Finalization Trigger

```python
def check_and_finalize_releases():
    """
    Background task (runs every minute):
    - Check all active releases
    - If discovered_count == total_tracks
    - Finalize the release
    """
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT id, release_id, release_title, artist, 
               monitoring_folder_path, total_tracks, discovered_count
        FROM musicbrainz_releases
        WHERE status = 'active'
    """)
    
    for row in cursor.fetchall():
        release_id = row['id']
        if row['discovered_count'] >= row['total_tracks']:
            finalize_release(release_id)
```

#### 6b. Finalization Process

```python
def finalize_release(release_id):
    """
    1. Get all files from monitoring folder
    2. Rename with track numbers
    3. Create final directory: /music/ARTIST/YEAR - ALBUM/
    4. Move files to final location
    5. Cleanup monitoring folder
    6. Update database
    """
    
    # Step 1: Get release info and files
    cursor.execute("""
        SELECT release_title, artist, release_year, monitoring_folder_path
        FROM musicbrainz_releases WHERE id = ?
    """, (release_id,))
    release = cursor.fetchone()
    
    monitoring_folder = Path(release['monitoring_folder_path'])
    files = list(monitoring_folder.glob('*'))
    
    # Step 2: Create final directory
    final_dir = Path(f"/music/{release['artist']}/{release['release_year']} - {release['release_title']}")
    final_dir.mkdir(parents=True, exist_ok=True)
    
    # Step 3: Move and rename files
    for file in files:
        # Get track number from database
        cursor.execute("""
            SELECT track_number FROM musicbrainz_release_tracks
            WHERE found_filename = ? OR file_path LIKE ?
        """, (file.name, f"%{file.name}%"))
        track_row = cursor.fetchone()
        
        if not track_row:
            # Couldn't match track number, use discovery order
            continue
        
        track_number = track_row['track_number']
        
        # Format: "01. Artist - Title.ext"
        extension = file.suffix
        new_name = f"{track_number:02d}. {release['artist']} - {file.stem}{extension}"
        destination = final_dir / new_name
        
        # Move file
        shutil.move(str(file), str(destination))
        
        # Update database
        cursor.execute("""
            UPDATE musicbrainz_release_tracks
            SET status = 'finalized', file_path = ?
            WHERE release_id = ? AND track_number = ?
        """, (str(destination), release_id, track_number))
    
    # Step 4: Cleanup
    try:
        monitoring_folder.rmdir()
    except:
        # Files remaining in folder
        pass
    
    # Step 5: Update release status
    cursor.execute("""
        UPDATE musicbrainz_releases
        SET status = 'finalized', finalized_at = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (release_id,))
    
    conn.commit()
```

### Considerations

1. **Track Number Parsing:** ID3 tags have `track_number` field
2. **Artist Name Consistency:** Use canonical artist name from database
3. **Collision Handling:** What if final file already exists?
4. **Atomic Operations:** Ensure all-or-nothing during move
5. **Logging:** Track which files moved where for audit trail

---

## Phase 7: Release Cleanup

### Current State
- Monitoring folders created but never cleaned
- Abandoned releases could accumulate

### Implementation

#### 7a. Cleanup Conditions

Release cleanup should occur when:
1. Release is finalized ✓ (handled in Phase 6)
2. Release is cancelled by user
3. Release is stalled (no progress for 7+ days)
4. User manually requests cleanup

#### 7b. Stalled Release Detection

```python
def find_stalled_releases():
    """Find releases with no progress for 7 days"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT id, release_id, release_title, artist, 
               discovered_count, total_tracks, updated_at
        FROM musicbrainz_releases
        WHERE status = 'active'
        AND (
            julianday('now') - julianday(updated_at) > 7
            OR (
                discovered_count > 0 
                AND julianday('now') - julianday(updated_at) > 14
            )
        )
    """)
    
    return cursor.fetchall()
```

#### 7c. Cleanup Process

```python
def cleanup_release(release_id, reason='user_requested'):
    """
    1. Get monitoring folder path
    2. Backup folder (optional)
    3. Delete monitoring folder
    4. Mark all tracks as cancelled
    5. Update release status to 'cancelled'
    6. Remove from download queue items
    """
    
    cursor.execute("""
        SELECT monitoring_folder_path FROM musicbrainz_releases 
        WHERE id = ?
    """, (release_id,))
    release = cursor.fetchone()
    
    if not release:
        return False
    
    folder = Path(release['monitoring_folder_path'])
    
    # Backup (optional)
    if reason == 'stalled':
        backup_path = folder.parent / f"{folder.name}_backup_{datetime.now().isoformat()}"
        shutil.move(str(folder), str(backup_path))
    else:
        shutil.rmtree(str(folder), ignore_errors=True)
    
    # Update database
    cursor.execute("""
        UPDATE musicbrainz_releases
        SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (release_id,))
    
    cursor.execute("""
        DELETE FROM download_queue
        WHERE mb_release_download_id = ?
    """, (release_id,))
    
    return True
```

---

## Recommended Improvements

### 1. **Parallel File Processing**
Current plan processes files sequentially. Consider:
- Background thread pool for file matching
- Async file I/O operations
- Prevents UI blocking during large file discoveries

### 2. **Better Progress Tracking**
Current: Just count discovered vs total
Suggested additions:
- Total files matched
- Failed/unmatched files
- Confidence score distribution
- Estimated time to completion based on current speed

### 3. **Duplicate Release Handling**
What if user starts same release twice?
- Check if release_id already exists in `musicbrainz_releases`
- Either:
  - Merge queue items (add new tracks only)
  - Return error (release already active)
  - Update existing release with new method parameter

### 4. **ID3 Tag Standardization**
Before files enter monitoring folder, consider:
- Write standardized ID3 tags
- Ensure track_number is set correctly
- Preserve artist/album/title consistency

### 5. **File Organization Rules**
Make final file naming configurable:
- Current: `01. Artist - Title.ext`
- Alternative: `01 - Title.ext`
- Alternative: `Title.ext`
- Custom pattern support via settings

### 6. **Monitoring Folder Permissions**
Ensure proper permissions for file operations:
- `/downloads/Music/` must be writable
- `/music/` must be writable
- Handle permission errors gracefully

### 7. **Database Audit Trail**
Add columns for better tracking:
- `error_message` (if matching failed)
- `error_count` (retry attempts)
- `last_attempt_at` (timestamp)
- `matched_confidence` (0.0-1.0)

### 8. **API for Manual Operations**
```python
# Manual track assignment
POST /api/musicbrainz/release/{id}/assign-file
  {
    "track_number": 3,
    "filepath": "/downloads/Music/some_file.mp3"
  }

# Cancel single track
POST /api/musicbrainz/release/{id}/cancel-track
  { "track_number": 5 }

# Retry failed matches
POST /api/musicbrainz/release/{id}/retry-matching
```

### 9. **Notification System**
When release is complete:
- Add notification to dashboard
- Log to scan log
- Optional email notification
- Webhook support for external systems

### 10. **Testing Checklist**
Before deploying Phases 4-7:
- [ ] File matching works on mixed file types (.mp3, .flac, .m4a)
- [ ] Monitoring folder created with correct name format
- [ ] Files moved to correct location
- [ ] ID3 tags updated correctly
- [ ] Release marked finalized when complete
- [ ] Stalled releases detected and cleaned up
- [ ] UI refreshes properly during active download
- [ ] Duplicate release handling works
- [ ] Permission errors handled gracefully
- [ ] Database remains consistent

---

## Implementation Order Recommendation

1. **Phase 4 (UI)** - Implement first, easiest, helps visualize progress
2. **Phase 5 (File Matching)** - Most critical, enables rest of system
3. **Phase 6 (Auto-finalization)** - Depends on Phase 5
4. **Phase 7 (Cleanup)** - Can be deferred, but important for maintenance

**Estimated Timeline:**
- Phase 4: 2-3 days
- Phase 5: 3-5 days (most complex)
- Phase 6: 1-2 days
- Phase 7: 1 day
- Testing & Polish: 2-3 days

**Total: 2-3 weeks**

---

## Database Schema Validation

Current `musicbrainz_releases` table has these fields:
- ✅ `id`, `release_id`, `release_title`, `artist`, `release_year`
- ✅ `total_tracks`, `discovered_count`, `organized_count`, `finalized_count`
- ✅ `monitoring_folder_path`, `final_folder_path`
- ✅ `status` (active, finalizing, finalized, cancelled)
- ✅ `method` (slskd, qbittorrent)
- ✅ `created_at`, `updated_at`, `finalized_at`

**These are sufficient.** No additional columns needed.

Current `musicbrainz_release_tracks` table:
- ✅ `id`, `release_id`, `queue_id`
- ✅ `track_number`, `track_title`, `track_artist`, `duration`, `isrc`
- ✅ `found_filename`, `file_path`
- ✅ `status` (queued, searching, downloading, discovered, organized, finalized)
- ✅ `created_at`, `updated_at`

**Suggested additions:**
- `matched_confidence` (REAL 0.0-1.0)
- `match_source` (TEXT: id3, filename, isrc)
- `error_message` (TEXT)
- `retry_count` (INTEGER)

---

## Conclusion

The foundation is solid (Phases 1-3). Phases 4-7 are well-documented and straightforward to implement. The key complexity is in Phase 5 (file matching), but the algorithm is sound.

**Next Step:** Start with Phase 4 UI, then move to Phase 5 file matching implementation.
