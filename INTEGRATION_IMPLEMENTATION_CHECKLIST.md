# Queue & Downloads Integration - Implementation Checklist

## Quick Summary

Your system **already has 80% of the infrastructure** for this workflow. The main gaps are:

1. **Folder creation timing** - Create at queue time, not finalization time
2. **Status display UI** - Show progress by album with track counts
3. **Batch transfer UI** - "Transfer to Library" button for complete albums
4. **Artist/Album badges** - Show download status on browse pages

## Phase 1: Folder Creation (Hours: 2-3)

**Goal:** Folder created immediately when release added to queue

### Tasks

- [ ] **Task 1.1:** Open `musicbrainz_release_manager.py`
  - Find `start_release_download()` method
  - Move folder creation from finalization to initialization
  - Current folder pattern: `/downloads/Music/YEAR - ARTIST - ALBUM/` ✓
  
- [ ] **Task 1.2:** Verify database
  - Check `musicbrainz_releases.monitoring_folder_path` is populated
  - This field should be set when release download starts
  
- [ ] **Task 1.3:** Test
  - Add MusicBrainz release via API
  - Verify folder appears immediately in file system
  - Verify database records folder path

### Files to Modify
- `musicbrainz_release_manager.py` (1 method)

### Database Changes
- None required - use existing schema

---

## Phase 2: Status API Endpoint (Hours: 3-4)

**Goal:** API endpoint showing all releases with track progress

### Tasks

- [ ] **Task 2.1:** Create new endpoint in `app.py`
  - Path: `GET /api/queue/releases/status`
  - Returns: List of all active releases with:
    - Total tracks
    - Count by status (queued, downloading, ready_to_transfer, completed)
    - Folder path
    - Progress percentage

- [ ] **Task 2.2:** Query builder
  - Count tracks by status per release
  - Use SQL GROUP BY on `musicbrainz_release_tracks.status`
  - Join with `musicbrainz_releases` for metadata

- [ ] **Task 2.3:** Status mapping for UI
  - `queued` → "Queued"
  - `downloading` → "Downloading"
  - `organized` or `discovered` → "Ready to Transfer"
  - `finalized` → "Completed"

- [ ] **Task 2.4:** Test
  - Call endpoint with active release
  - Verify accurate track counts
  - Test with multiple releases

### Files to Modify
- `app.py` (1 new route, ~30 lines)

### SQL Query Template
```sql
SELECT 
    mr.release_id,
    mr.release_title,
    mr.artist,
    mr.release_year,
    mr.monitoring_folder_path,
    COUNT(*) as total_tracks,
    SUM(CASE WHEN mrt.status = 'queued' THEN 1 ELSE 0 END) as queued_count,
    SUM(CASE WHEN mrt.status = 'downloading' THEN 1 ELSE 0 END) as downloading_count,
    SUM(CASE WHEN mrt.status IN ('organized', 'discovered') THEN 1 ELSE 0 END) as ready_count,
    SUM(CASE WHEN mrt.status = 'finalized' THEN 1 ELSE 0 END) as completed_count
FROM musicbrainz_releases mr
LEFT JOIN musicbrainz_release_tracks mrt ON mr.release_id = mrt.release_id
WHERE mr.status != 'completed'
GROUP BY mr.release_id
```

### Database Changes
- None required

---

## Phase 3: Enhanced Monitor UI (Hours: 4-5)

**Goal:** Display releases with progress bars and track lists

### Tasks

- [ ] **Task 3.1:** Find monitor page template
  - File: `templates/downloads.html` or similar
  - Or create new:  `templates/downloads_monitor.html`
  
- [ ] **Task 3.2:** Update layout
  - Remove/refactor current flat track list
  - Add album grouping sections
  - Add progress bars per album
  - Show status breakdown (n queued, n downloading, n ready)

- [ ] **Task 3.3:** Track detail display
  - Show track number
  - Show track title
  - Show current status with icon
  - Show progress % if downloading

- [ ] **Task 3.4:** API integration
  - Call `GET /api/queue/releases/status` on page load
  - Refresh every 5-10 seconds
  - Render using JavaScript/template

- [ ] **Task 3.5:** Test
  - Load page with active release
  - Verify progress updates in real-time
  - Test with multiple releases

### Files to Modify
- `templates/downloads.html` (major update)
- `static/js/downloads.js` (if separate file, or inline)
- `app.py` - ensure `/downloads/monitor` calls correct template

### Sample HTML Structure
```html
<div class="release-container" data-release-id="{{ release.release_id }}">
  <div class="release-header">
    <h3 class="release-title">{{ release.release_title }}</h3>
    <p class="release-artist">{{ release.artist }} ({{ release.release_year }})</p>
  </div>
  
  <div class="progress-section">
    <div class="progress-bar">
      <div class="progress-fill" style="width: {{ progress }}%"></div>
    </div>
    <div class="progress-text">
      {{ ready_count }}/{{ total_tracks }} Ready to Transfer
    </div>
  </div>
  
  <div class="status-counts">
    <span class="queued">{{ queued_count }} Queued</span>
    <span class="downloading">{{ downloading_count }} Downloading</span>
    <span class="ready">{{ ready_count }} Ready</span>
  </div>
  
  <ul class="track-list">
    {% for track in release.tracks %}
    <li class="track-item status-{{ track.status }}">
      <span class="track-number">{{ track.track_number }}</span>
      <span class="track-title">{{ track.track_title }}</span>
      <span class="track-status">{{ track.status_label }}</span>
    </li>
    {% endfor %}
  </ul>
</div>
```

### Database Changes
- None required

---

## Phase 4: Batch Transfer Endpoint (Hours: 3-4)

**Goal:** Button to transfer all "Ready to Transfer" tracks at once

### Tasks

- [ ] **Task 4.1:** Create endpoint in `app.py`
  - Path: `POST /api/queue/release/<release_id>/transfer`
  - Action: Transfer all ready tracks to music library
  
- [ ] **Task 4.2:** Pre-check
  - Verify all tracks are "ready_to_transfer" status
  - Get folder path from database
  - Verify source folder exists

- [ ] **Task 4.3:** Call finalizer
  - Reuse existing `musicbrainz_finalizer.py` logic
  - OR call `organize_folder_to_music()` directly
  - Pass: source_folder, album_artist, year, album_name

- [ ] **Task 4.4:** Post-transfer
  - Verify all files moved successfully
  - Update `musicbrainz_releases.status` to "completed"
  - Return success/error response

- [ ] **Task 4.5:** UI button
  - Show in monitor only when all tracks ready
  - Button text: "Transfer to Library"
  - Call endpoint on click
  - Show loading state
  - Refresh page on success

- [ ] **Task 4.6:** Test
  - Manually set all tracks to ready status
  - Click transfer button
  - Verify files in music library
  - Verify naming correct

### Files to Modify
- `app.py` (1 new route, ~40 lines)
- `templates/downloads.html` (add button)
- `static/js/downloads.js` (button click handler)

### Sample Endpoint Code
```python
@app.route("/api/queue/release/<release_id>/transfer", methods=["POST"])
def transfer_release_to_library(release_id):
    conn = get_db()
    cursor = conn.cursor()
    
    # Check all tracks are ready
    # Get release metadata
    # Get source folder path
    # Call finalizer
    # Update status to 'completed'
    # Return response
```

### Database Changes
- None required

---

## Phase 5: Update Artist/Album Pages (Hours: 4-5)

**Goal:** Show download status on `/artists` and `/albums` pages

### Tasks

- [ ] **Task 5.1:** Find artist page route
  - File: `app.py` - route `/artist/<artist_name>`
  - Check database for any MusicBrainz releases for this artist
  
- [ ] **Task 5.2:** Find album page route
  - File: `app.py` - route `/album/<album>/<artist>`
  - Check for active MusicBrainz releases
  
- [ ] **Task 5.3:** Add badge data
  - Query: Any active `musicbrainz_releases` matching artist
  - Count: Total tracks being downloaded
  - Count: Tracks ready to transfer
  - Render as: "⬇️ Downloading (4/12)" badge
  
- [ ] **Task 5.4:** Update templates
  - File: `templates/artist.html`
  - File: `templates/album.html`
  - Add badge section near title
  - Style to match existing design
  
- [ ] **Task 5.5:** Link to downloads
  - Badge should link to `/downloads/monitor`
  - Filter to show only this release (optional)
  
- [ ] **Task 5.6:** Test
  - Add release to queue
  - Browse artist page - verify badge
  - Browse album page - verify badge
  - Click badge - goes to monitor

### Files to Modify
- `app.py` (2 routes, add ~15 lines each)
- `templates/artist.html` (add badge section)
- `templates/album.html` (add badge section)

### SQL Query
```sql
SELECT 
    SUM(total_tracks) as total,
    SUM(ready_count) as ready
FROM release_status_summary
WHERE artist = ?
    AND status = 'active'
```

### Sample Badge HTML
```html
{% if download_status %}
<div class="download-status-badge">
  <span class="badge-icon">⬇️</span>
  <span class="badge-text">
    {{ download_status.downloading|length }} Downloading
  </span>
  <span class="badge-ready">
    {{ download_status.ready|length }} Ready
  </span>
</div>
{% endif %}
```

### Database Changes
- None required

---

## Phase 6: Navidrome Import Sync (Hours: 5-6)

**Goal:** After transfer, update Navidrome library automatically

### Tasks

- [ ] **Task 6.1:** Find Navidrome config
  - Check `app.py` for existing Navidrome integration
  - Get API key and base URL
  
- [ ] **Task 6.2:** Create sync function
  - After files moved to music library
  - Call Navidrome rescan endpoint
  - Wait for indexing to complete
  
- [ ] **Task 6.3:** Verify import
  - Query Navidrome API for album
  - Confirm tracks appear in system
  
- [ ] **Task 6.4:** Update database
  - Set `musicbrainz_releases.status = 'completed'`
  - Set `musicbrainz_releases.finalized_at = now()`
  - Remove from active downloads view

- [ ] **Task 6.5:** Test
  - Transfer album to library
  - Verify appears in Navidrome within 30 seconds
  - Verify removed from active downloads

### Files to Modify
- `app.py` (update transfer endpoint, ~10 lines)
- Create `helpers/navidrome_sync.py` (new file, ~50 lines)

### Navidrome API Calls
```python
# Trigger scan
POST /navidrome/api/startScan

# Poll for completion
GET /navidrome/api/getScanStatus

# Verify album exists
GET /navidrome/api/albums?query=album_name
```

### Database Changes
- None required

---

## Testing Checklist

### Phase 1 Tests
- [ ] Folder created in `/downloads/Music/` when release queued
- [ ] Folder path stored in `musicbrainz_releases` table
- [ ] Folder visible in file explorer

### Phase 2 Tests
- [ ] API endpoint returns all active releases
- [ ] Track counts accurate per status
- [ ] Supports multiple releases simultaneously

### Phase 3 Tests
- [ ] Monitor page renders with progress bars
- [ ] Track list displays correctly
- [ ] Progress updates in real-time (refresh 5s)
- [ ] Multiple releases display correctly

### Phase 4 Tests
- [ ] Transfer button appears only when all ready
- [ ] Button disabled during transfer
- [ ] Files actually move to music library
- [ ] Files renamed correctly
- [ ] Release marked as completed after transfer

### Phase 5 Tests
- [ ] Artist page shows download badge
- [ ] Album page shows download badge
- [ ] Badge disappears after transfer
- [ ] Badge counts accurate
- [ ] Clicking badge links to monitor

### Phase 6 Tests
- [ ] After transfer, waits for Navidrome sync
- [ ] Album appears in Navidrome
- [ ] Release removed from active downloads
- [ ] Status updated to "completed"

---

## Effort Estimate

| Phase | Hours | Priority |
|-------|-------|----------|
| 1: Folder Creation | 2-3 | HIGH |
| 2: Status API | 3-4 | HIGH |
| 3: Monitor UI | 4-5 | HIGH |
| 4: Transfer Function | 3-4 | HIGH |
| 5: Artist/Album Badges | 4-5 | MEDIUM |
| 6: Navidrome Sync | 5-6 | MEDIUM |
| **Testing** | **5-8** | **HIGH** |
| **Documentation** | **2-3** | **MEDIUM** |
| **Total** | **28-38 hours** | — |

**Recommended Approach:**
- Phases 1-4: Complete in one sprint (2 weeks)
- Phases 5-6: Second sprint (1 week)
- Incremental testing throughout

---

## Success Criteria (MVP)

✅ Folder created immediately when release queued
✅ Active queue shows albums with progress
✅ Track statuses visible and updating
✅ "Ready to Transfer" status appears when all tracks found
✅ Transfer button visible and functional for complete albums
✅ Files successfully move to music library
✅ Correct naming applied: `TRACK#. ARTIST - SONG.mp3`
✅ Artist/album pages show "Downloading" badge

**Nice to Have:**
- Navidrome auto-sync
- Automatic folder cleanup
- Duplicate detection/merge

---

## Key Code Locations

- **Music library path:** `MUSIC_LIBRARY_DIR` in `musicbrainz_release_manager.py`
- **Finalization logic:** `musicbrainz_finalizer.py` - reuse directly
- **Queue management:** `download_queue_manager.py`
- **Web routes:** `app.py` - search for `@app.route`
- **Templates:** `templates/downloads.html` or similar
- **Database:** `sqlite3` via `database_abstraction.py`

---

## Questions to Clarify

1. **Should "Ready to Transfer" auto-trigger after download completes?**
   - Or wait for user confirmation?
   - Recommend: Auto-ready, user must click transfer button

2. **When transfer completes, close folder immediately?**
   - Or keep visible for verification (1 hour)?
   - Recommend: Keep visible for 1 hour, then move to "Completed" tab

3. **In artist/album views, link to specific release or just filter?**
   - Recommend: Filter to show only this release when user clicks badge

4. **Naming format for files - confirm:**
   - `01. Artist Name - Song Title.mp3`
   - Directory: `/music/Album Artist/Release Year - Album Name/`
   - Is this correct?

