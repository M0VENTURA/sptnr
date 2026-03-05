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

## Phase 4: Queue Display Integration

### Current State
- API endpoint `GET /api/musicbrainz/releases/active` exists and returns releases with progress
- downloads_monitor.html has sections for Active Queue, Failed Queue, but **NO dedicated MusicBrainz releases section**
- Infrastructure to display releases exists, but no UI yet

### Required Implementation

#### 4a. UI Section in downloads_monitor.html

**Add new section after "Queue Status Cards" (around line 170):**

```html
<!-- Active MusicBrainz Releases Section -->
<div class="card mb-4" id="musicbrainzReleasesSection">
  <div class="card-header d-flex justify-content-between align-items-center">
    <h5 class="card-title mb-0">
      <i class="bi bi-disc"></i> MusicBrainz Releases
    </h5>
    <span class="badge bg-info" id="mbReleasesBadge">0</span>
  </div>
  <div class="card-body p-0">
    <div id="mbReleasesEmpty" class="alert alert-info alert-sm mb-0">
      <i class="bi bi-info-circle"></i> No active MusicBrainz releases.
    </div>
    <div id="mbReleasesList" style="display:none">
      <!-- Release cards rendered here -->
    </div>
  </div>
</div>
```

#### 4b. JavaScript Function to Load & Display Releases

Create new function `loadMusicBrainzReleases()`:

```javascript
async function loadMusicBrainzReleases() {
  try {
    const response = await fetch('/api/musicbrainz/releases/active');
    const data = await response.json();
    
    if (!data.success || data.count === 0) {
      document.getElementById('mbReleasesEmpty').style.display = 'block';
      document.getElementById('mbReleasesList').style.display = 'none';
      document.getElementById('mbReleasesBadge').textContent = '0';
      return;
    }
    
    document.getElementById('mbReleasesEmpty').style.display = 'none';
    document.getElementById('mbReleasesList').style.display = 'block';
    document.getElementById('mbReleasesBadge').textContent = data.count;
    
    const html = data.releases.map((release) => `
      <div class="list-group-item">
        <div class="d-flex justify-content-between align-items-start">
          <div>
            <h6 class="mb-1">${release.release_title}</h6>
            <p class="text-muted small mb-2">${release.artist} (${release.release_year})</p>
            <div class="progress" style="height: 20px;">
              <div class="progress-bar" style="width: ${release.progress_percent}%">
                ${release.progress_percent}%
              </div>
            </div>
            <small class="text-muted">${release.discovered_count} of ${release.total_tracks} tracks discovered</small>
          </div>
          <div>
            <button class="btn btn-sm btn-outline-info" onclick="showReleaseDetails('${release.release_id}')">
              <i class="bi bi-eye"></i>
            </button>
            <button class="btn btn-sm btn-outline-danger" onclick="cancelRelease('${release.release_id}')">
              <i class="bi bi-x"></i>
            </button>
          </div>
        </div>
      </div>
    `).join('');
    
    document.getElementById('mbReleasesList').innerHTML = 
      `<div class="list-group list-group-flush">${html}</div>`;
    
  } catch (error) {
    console.error('Error loading MusicBrainz releases:', error);
  }
}
```

#### 4c. Bind to Queue Status Refresh

Update `loadQueueStatus()` to also call `loadMusicBrainzReleases()` at the end.

### Expected Display

```
┌─ MusicBrainz Releases [3] ──────────────────────────┐
│                                                      │
│ Album Name (Artist - 2026)                    👁 ✕  │
│ ▓▓▓▓▓░░░░░░░░░░░░░░░  75%                          │
│ 9 of 12 tracks discovered                          │
│                                                      │
│ Another Album (Other Artist - 2025)           👁 ✕  │
│ ▓▓░░░░░░░░░░░░░░░░░░  17%                          │
│ 2 of 12 tracks discovered                          │
│                                                      │
└──────────────────────────────────────────────────────┘
```

### Improvements to Consider

1. **Expandable Track List:** Click release to expand and show individual tracks with status badges
2. **Auto-Refresh:** Refresh every 5 seconds when releases are active
3. **Color Coding:** Status badges for each track (queued, searching, downloading, discovered)
4. **Cancel/Pause Buttons:** Allow user to cancel incomplete releases
5. **Estimated Time:** Show time remaining based on current download speed

---

## Phase 5: File Matching & Movement Logic

### Current State
- Monitoring folders created but no file discovery logic yet
- Files downloaded by slskd go to `/downloads/` but not matched to release tracks
- No movement to monitoring folder yet

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
- Release stored in database with status tracking
- discovered_count field exists but not auto-finalized

### Implementation

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
