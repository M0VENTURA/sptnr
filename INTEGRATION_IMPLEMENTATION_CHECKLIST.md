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

**Goal:** Button to transfer all queued tracks at once (auto-ready when all matched)

### Key Distinction

**Auto-Ready Trigger (Automatic):**
- When ALL songs in THIS specific import are matched/organized
- Happens automatically - no user action needed
- Updates status to "ready_to_transfer"

**Transfer Action (Manual):**
- User must click "Transfer to Library" button
- button only appears when ready_to_transfer is true
- User has choice to wait or transfer now

### Tasks

- [ ] **Task 4.1:** Create auto-ready logic
  - File: `download_queue_manager.py` or new helper function
  - When file matched to queue item
  - Check: Are ALL items in this import_group matched?
  - Action: Update all to 'ready_to_transfer' status automatically
  
- [ ] **Task 4.2:** Create transfer endpoint in `app.py`
  - Path: `POST /api/queue/release/<release_id>/transfer`
  - Action: Transfer all ready tracks to music library
  - Note: This is MANUAL - user clicks button
  
- [ ] **Task 4.3:** Pre-check before transfer
  - Verify all queued tracks are "ready_to_transfer" status
  - Get folder path from database
  - Verify source folder exists
  - Important: Only check tracks in THIS import, not full album
  
- [ ] **Task 4.4:** Call finalizer & cleanup
  - Reuse existing `musicbrainz_finalizer.py` logic
  - Pass: source_folder, album_artist, year, album_name
  - Delete monitoring folder IMMEDIATELY after transfer success
  - Don't keep for verification

- [ ] **Task 4.5:** Post-transfer
  - Verify all files moved successfully
  - Update `musicbrainz_releases.status` to "completed"
  - Return success/error response

- [ ] **Task 4.6:** UI components
  - Show "Ready to Transfer" badge when auto-ready triggers
  - Show button only when badge is visible
  - Button text: "Transfer to Library"
  - Call endpoint on click
  - Show loading state
  - Refresh page on success

- [ ] **Task 4.7:** Test
  - Manually set all tracks to matched status
  - Verify auto-ready triggers (no user action)
  - Verify button appears
  - Click transfer button
  - Verify files in music library
  - Verify folder deleted
  - Verify naming correct

### Files to Modify
- `app.py` (1 new route, ~40 lines)
- `download_queue_manager.py` (auto-ready logic, ~20 lines)
- `templates/downloads.html` (add button)
- `static/js/downloads.js` (button click handler)

### Sample Auto-Ready Logic
```python
def check_and_mark_import_ready(import_group_id, conn):
    """If all tracks in import are organized, mark all as ready_to_transfer """
    cursor = conn.cursor()
    
    # Count queued tracks in this import
    cursor.execute("""
        SELECT COUNT(*) FROM download_queue 
        WHERE import_group = ? AND status != 'organized'
    """, (import_group_id,))
    
    not_ready = cursor.fetchone()[0]
    
    if not_ready == 0:
        # All are organized - mark as ready
        cursor.execute("""
            UPDATE download_queue 
            SET status = 'ready_to_transfer'
            WHERE import_group = ? AND status = 'organized'
        """, (import_group_id,))
        conn.commit()
        return True
    return False
```

### Database Changes
- None required

---

## Phase 4b: File Naming Configuration (Hours: 2-3)

**Goal:** Allow users to configure default file naming format in config.html

### Tasks

- [ ] **Task 4b.1:** Add config option to `config.yaml`
  ```yaml
  file_naming:
    format: "track_number. artist - title"  # or other pattern
    enabled: true
  ```

- [ ] **Task 4b.2:** Update `config.html` template
  - Find/create Settings or Config section
  - Add field for file naming pattern
  - Default: `TRACK#. ARTIST - SONG.mp3`
  - Examples of other formats:
    - `ARTIST - TRACK# - SONG`
    - `TRACK# - SONG`
    - `SONG - ARTIST`
  
- [ ] **Task 4b.3:** Create pattern builder/validator
  - File: `helpers/file_naming.py` (new file)
  - Function: `generate_filename(track_number, artist, title, pattern)`
  - Validates pattern before saving
  - Handles special characters
  - Returns formatted filename

- [ ] **Task 4b.4:** Integration point
  - File: `musicbrainz_finalizer.py`
  - Use pattern from config instead of hardcoded format
  - Call `generate_filename()` for each track
  
- [ ] **Task 4b.5:** Test
  - Change pattern in config
  - Transfer album
  - Verify files use new naming

### Files to Modify
- `config.yaml` (add section)
- `templates/config.html` (add UI field)
- Create `helpers/file_naming.py` (~50 lines)
- `musicbrainz_finalizer.py` (update ~5 lines)
- `app.py` (config save endpoint)

### Sample Config UI
```html
<div class="config-section">
  <h3>File Naming</h3>
  <label>
    Default file naming format:
    <input type="text" name="file_naming_format" 
           value="{{ config.file_naming.format }}"
           placeholder="TRACK#. ARTIST - SONG">
  </label>
  <p class="help-text">
    Available variables: {track_number}, {artist}, {title}
    <br/>Example: {artist} - {track_number} - {title}.mp3
  </p>
</div>
```

### Database Changes
- None - stored in config.yaml

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
- [ ] Multiple releases create separate folders

### Phase 2 Tests
- [ ] API endpoint returns all active releases
- [ ] Track counts accurate per status
- [ ] Supports multiple releases simultaneously
- [ ] Query performance acceptable

### Phase 3 Tests
- [ ] Monitor page renders with progress bars
- [ ] Track list displays correctly
- [ ] Progress updates in real-time (refresh 5s)
- [ ] Multiple releases display correctly
- [ ] Status labels match expected values

### Phase 4 Tests
- [ ] Auto-ready triggers when all tracks matched
  - [ ] With partial import (2/12 songs)
  - [ ] With full album import
  - [ ] Status updates without user action
- [ ] Transfer button appears only when ready
- [ ] Button disabled during transfer
- [ ] Files actually move to music library
- [ ] Files renamed correctly with default format
- [ ] Monitoring folder deleted immediately after transfer
- [ ] Transfer report shows success/failures
- [ ] Release marked as completed after transfer

### Phase 4b Tests
- [ ] Config option appears in settings
- [ ] Saving new format updates config.yaml
- [ ] Format validation prevents bad patterns
- [ ] Transfer uses new format
- [ ] Files renamed with custom format
- [ ] Pre-configured format suggestions available

### Phase 5 Tests
- [ ] Artist page shows download badge
- [ ] Album page shows download badge
- [ ] Badge disappears after transfer completes
- [ ] Badge counts accurate
- [ ] Clicking badge links to monitor
- [ ] Multiple downloads show correct counts

### End-to-End Integration Tests
- [ ] Queue partial album (5/10 songs)
  - [ ] Folder created immediately
  - [ ] Auto-ready triggers after 5 matched
  - [ ] Transfer moves only 5 songs
  - [ ] Remaining 5 songs can be queued separately
- [ ] Queue full album
  - [ ] All 10 songs added to queue
  - [ ] Auto-ready when all 10 matched
  - [ ] Transfer moves all to library
- [ ] File naming scenarios
  - [ ] Default format works
  - [ ] Custom format applied
  - [ ] Special characters handled
- [ ] Status persistence
  - [ ] Refresh page maintains progress
  - [ ] Database reflects correct status
  - [ ] Page load shows correct state

---

## Effort Estimate

| Phase | Hours | Priority |
|-------|-------|----------|
| 1: Folder Creation | 2-3 | HIGH |
| 2: Status API | 3-4 | HIGH |
| 3: Monitor UI | 4-5 | HIGH |
| 4: Auto-Ready + Transfer | 3-4 | HIGH |
| 4b: File Naming Config | 2-3 | MEDIUM |
| 5: Artist/Album Badges | 4-5 | MEDIUM |
| **Testing** | **5-8** | **HIGH** |
| **Documentation** | **2-3** | **MEDIUM** |
| **Total** | **25-35 hours** | — |

**Recommended Approach:**
- Phases 1-4: Complete in one sprint (2 weeks)
- Phase 4b + 5: Second sprint (1 week)
- Incremental testing throughout

---

## Success Criteria (MVP)

✅ Folder created immediately when release queued
✅ Active queue shows albums with progress
✅ Track statuses visible and updating in real-time
✅ Auto-ready trigger: "Ready to Transfer" appears when all queued songs matched
✅ Transfer button visible only when ready
✅ User clicks button to manually transfer
✅ Files successfully move to music library
✅ Correct naming applied with configurable format
✅ Monitoring folder deleted immediately after transfer
✅ Artist/album pages show "Downloading" badge
✅ File naming format configurable in config.html

**Nice to Have:**
- Duplicate folder detection/merge
- Partial album transfers (user selects subset)
- Retry failed transfers
- Download speed monitoring

---

## Key Code Locations

- **Music library path:** `MUSIC_LIBRARY_DIR` in `musicbrainz_release_manager.py`
- **Finalization logic:** `musicbrainz_finalizer.py` - reuse directly
- **Queue management:** `download_queue_manager.py`
- **Web routes:** `app.py` - search for `@app.route`
- **Templates:** `templates/downloads.html` or similar
- **Database:** `sqlite3` via `database_abstraction.py`

---

## Key Implementation Notes

### Auto-Ready vs Manual Transfer

- **Auto-Ready (Automatic, no user action):**
  - Triggered when ALL items in import_group are 'organized'
  - Update status to 'ready_to_transfer' automatically
  - "Ready to Transfer" badge appears automatically

- **Transfer (Manual, user clicks button):**
  - User must explicitly click "Transfer to Library"
  - NOT automatic - gives user control
  - Transfers only when user is ready
  - Import count-aware: if 2/12 queued, waits only for those 2

### File Naming Configuration

- **Default Format:** `TRACK#. ARTIST - SONG.mp3`
- **Configurable in:** `config.html` Settings section
- **Stored in:** `config.yaml` under `file_naming.format`
- **Pattern Variables:** {track_number}, {artist}, {title}
- **Applied During:** Transfer phase in finalizer

### Folder Management

- **Creation:** Immediate when tracks added to queue
- **During Download:** Physical folder at `/downloads/Music/YEAR - ARTIST - ALBUM/`
- **After Transfer:** Deleted immediately (no verification period)
- **Location Tracking:** Stored in `musicbrainz_releases.monitoring_folder_path`

### Import-Aware Logic

- **Import Group:** Field in download_queue to group related tracks
- **Behavior:** Only waits for songs in THIS import, not full album
- **Example:**
  - Album has 12 tracks total
  - User queues songs 1-5 + song 8 (6 total)
  - Auto-ready triggers when those 6 are all matched
  - Song 9 remaining on album doesn't matter

### Navidrome Integration

- **Automatic:** Happens in background when files in `/music/` folder
- **No Additional Code Needed:** Navidrome auto-scans
- **Timing:** Album appears in Navidrome shortly after transfer
- **Status Update:** Just mark release as 'completed' in database


