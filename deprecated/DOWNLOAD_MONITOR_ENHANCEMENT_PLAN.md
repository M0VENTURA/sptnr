# Download Monitor Enhancement Plan

## Overview
This document outlines a comprehensive enhancement to the Download Monitor system, implementing advanced MusicBrainz integration, intelligent duplicate detection, file matching, and automated organization workflows.

## Current System Status

### Existing Database Schema (`download_queue` table)
```sql
- id, artist, album, title, search_query
- source, source_id, status, priority
- found_filename, file_path, metadata (JSON)
- retry_count, max_retries, failure_reason, last_failure_time
- retry_delay_minutes, next_retry_at
- imported_at, created_at, updated_at
```

### Existing add_to_queue Parameters (Already Supported!)
✅ `track_number` - Track position from MusicBrainz  
✅ `album_artist` - Album artist metadata  
✅ `year` - Release year  
✅ `release_id` - MusicBrainz release ID  
✅ `release_source` - 'musicbrainz' or 'discogs'

**Good news**: MusicBrainz parameters already exist in the `add_to_queue()` function but aren't being stored in dedicated database columns yet.

### Current Status Values
- `queued` - Default status for new downloads
- `searching` - Appears to be in use
- `downloading` - Active downloads
- `completed` - Not explicitly seen but referenced
- `failed` - Retry logic exists

## Enhancement Plan

### Phase 1: Database Schema Enhancements

#### New Columns for `download_queue`
```sql
-- MusicBrainz Metadata
duration INTEGER,              -- Track duration in seconds
track_number INTEGER,          -- Track position (already in add_to_queue!)
disc_number INTEGER,           -- Disc number for multi-disc albums
release_mbid TEXT,            -- MusicBrainz release ID (already in add_to_queue as release_id!)
recording_mbid TEXT,          -- MusicBrainz recording ID
release_year INTEGER,         -- Release year (already in add_to_queue as year!)
album_artist TEXT,            -- Album artist (already in add_to_queue!)

-- File Path Tracking
matched_file_path TEXT,       -- Path when matched with downloads folder file
matched_at TIMESTAMP,         -- When file was matched

-- Duplicate Detection  
is_duplicate BOOLEAN DEFAULT 0,
duplicate_of_id INTEGER,      -- References parent queue item
duplicate_detected_at TIMESTAMP,

-- Collection Matching
in_collection BOOLEAN DEFAULT 0,
collection_track_id INTEGER,  -- FK to tracks table
collection_matched_at TIMESTAMP,

-- Auto-cleanup
auto_delete_at TIMESTAMP      -- For 24-hour duplicate cleanup
```

#### Migration Strategy
1. Add new columns with `ALTER TABLE` in check_db.py
2. Update indexes for new columns
3. Backfill `release_mbid`, `release_year`, `track_number`, `album_artist` from existing `metadata` JSON where available

---

### Phase 2: Enhanced Status Workflow

#### New Status Values
1. **queued** - Auto send to SLSK every 10 minutes ✅
2. **searching** - SLSK search initiated ✅
3. **downloading** - Currently downloading from SLSK ✅
4. **matched** - File found in /downloads and matched to queue item 🆕
5. **completed** - File moved to /Music with tags applied 🆕
6. **duplicate** - Song already in queue for same album 🆕
7. **unmatched** - File in /downloads but no queue match 🆕
8. **in_collection** - Already exists in Navidrome collection 🆕

#### Status Transition Rules
```
queued → searching (SLSK search starts)
searching → downloading (file found) OR queued (no match)
downloading → matched (download complete)
matched → completed (moved to /Music)
duplicate → auto-delete after 24 hours
```

---

### Phase 3: Duplicate Detection Logic

#### When Adding to Queue (`add_to_queue()`)
```python
# Check for existing entry with same artist + album + title
cursor.execute("""
    SELECT id, status FROM download_queue
    WHERE artist = ? AND album = ? AND title = ?
    AND status NOT IN ('completed', 'deleted')
    LIMIT 1
""", (artist, album, title))

existing = cursor.fetchone()
if existing:
    # Mark new entry as duplicate
    is_duplicate = True
    duplicate_of_id = existing['id']
    auto_delete_at = datetime.now() + timedelta(hours=24)
    status = 'duplicate'
```

#### Duplicate Behavior
- **Don't send to SLSK** - Skip in queue processor
- **Show alert badge** - Visual indicator in UI
- **Auto-delete after 24 hours** - Cleanup job removes stale duplicates
- **Delete when parent album completes** - Remove duplicate when all tracks in album are `completed` or `in_collection`

---

### Phase 4: File Matching Enhancements

#### Current Matching Logic
Located in `download_queue_manager.py`:
- `_metadata_matches_queue_item()` - Fuzzy matching with threshold 0.68
- `is_match()` - Filename-based matching

#### Enhanced Matching Flow
```python
def match_downloaded_file_to_queue(file_path, file_metadata):
    """
    Match discovered file against queue items.
    Priority matching: artist name + song title
    """
    artist = file_metadata.get('artist', '').strip()
    title = file_metadata.get('title', '').strip()
    
    # Query queue for matches
    cursor.execute("""
        SELECT id, artist, title, album, status, release_mbid
        FROM download_queue
        WHERE status IN ('queued', 'searching', 'downloading')
        ORDER BY 
            -- Prioritize exact artist + title matches
            CASE WHEN LOWER(artist) = LOWER(?) AND LOWER(title) = LOWER(?) THEN 1
            ELSE 2 END,
            created_at ASC
    """, (artist, title))
    
    for queue_item in cursor.fetchall():
        if fuzzy_match(artist, queue_item['artist'], 0.85) and \
           fuzzy_match(title, queue_item['title'], 0.85):
            # MATCH FOUND
            update_queue_match(queue_item['id'], file_path)
            return queen_item
    
    # NO MATCH - Add as unmatched
    return None
```

#### Update Queue on Match
```python
def update_queue_match(queue_id, file_path):
    cursor.execute("""
        UPDATE download_queue
        SET status = 'matched',
            matched_file_path = ?,
            matched_at = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (file_path, queue_id))
```

---

### Phase 5: Unmatched File Workflow

#### When No Queue Match Found
```python
def handle_unmatched_file(file_path, file_metadata):
    """
    File found in /downloads but doesn't match queue.
    Add as 'unmatched' and auto-search MusicBrainz.
    """
    artist = file_metadata.get('artist', '')
    title = file_metadata.get('title', '')
    album = file_metadata.get('album', '')
    
    # Add to queue with unmatched status
    queue_id = add_to_queue(
        artist=artist,
        title=title,
        album=album,
        source='local',
        status='unmatched',
        matched_file_path=file_path
    )
    
    # Trigger MusicBrainz auto-search
    search_and_update_musicbrainz(queue_id, artist, title, album)
```

#### MusicBrainz Auto-Search for Unmatched
```python
def search_and_update_musicbrainz(queue_id, artist, title, album):
    """
    Search MusicBrainz for album match.
    If found, auto-fill remaining tracks as 'queried'.
    """
    # Use existing MusicBrainz search from folder_matching_enhancements.py
    from folder_matching_enhancements import search_musicbrainz_releases
    
    releases = search_musicbrainz_releases(artist, album)
    
    if not releases:
        logger.info(f"No MusicBrainz match for unmatched file (queue_id={queue_id})")
        return
    
    # Take best match (first result)
    release = releases[0]
    release_mbid = release['id']
    
    # Update original queue item with MBID
    cursor.execute("""
        UPDATE download_queue
        SET release_mbid = ?,
            release_year = ?,
            album_artist = ?
        WHERE id = ?
    """, (release_mbid, release.get('year'), release.get('artist'), queue_id))
    
    # Fetch full tracklist
    from folder_matching_enhancements import get_musicbrainz_release_tracks
    tracks = get_musicbrainz_release_tracks(release_mbid)
    
    # Add remaining tracks as 'queried'
    for track in tracks:
        add_to_queue(
            artist=track['artist'],
            title=track['title'],
            album=album,
            status='queried',  # New status
            track_number=track['position'],
            release_mbid=release_mbid,
            recording_mbid=track.get('recording_id'),
            duration=track.get('duration')
        )
```

#### Queried Status Behavior
- **Don't send to SLSK automatically**
- **Show "Send" button in UI** - User can manually queue
- **Clicking "Send"** - Changes status to `queued`, enters normal download flow

---

### Phase 6: Collection Matching

#### Check Against Navidrome Collection
```python
def check_collection_match(queue_item):
    """
    Check if track already exists in Navidrome collection.
    Match criteria: artist name + title + release MBID
    """
    cursor.execute("""
        SELECT id, file_path
        FROM tracks
        WHERE LOWER(artist) = LOWER(?)
        AND LOWER(title) = LOWER(?)
        AND (release_group_mbid = ? OR suggested_mbid = ?)
        LIMIT 1
    """, (queue_item['artist'], queue_item['title'], 
          queue_item['release_mbid'], queue_item['release_mbid']))
    
    track = cursor.fetchone()
    if track:
        # Mark as in_collection
        cursor.execute("""
            UPDATE download_queue
            SET status = 'in_collection',
                in_collection = 1,
                collection_track_id = ?,
                collection_matched_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (track['id'], queue_item['id']))
        return True
    return False
```

#### When to Check Collection
1. **On queue add** - Immediate check when item added
2. **During file scan** - Check discovered files
3. **After Navidrome sync** - Re-check queue items against updated library

#### In Collection Behavior
- **Don't send to SLSK** - Skip in queue processor
- **Auto-delete album when complete** - When all tracks in album are `completed` or `in_collection`, remove entire album group

---

### Phase 7: MBID Display & Manual Search

#### UI Changes - Album Header
```html
<div class="album-header">
  <h5>
    <span class="artist-name">{{ artist }}</span> - 
    <span class="album-name">{{ album }}</span>
    
    <!-- NEW: MBID Display -->
    {% if release_mbid %}
    <span class="badge bg-info ms-2" title="MusicBrainz Release ID">
      MBID: {{ release_mbid[:8] }}...
    </span>
    <button class="btn btn-sm btn-outline-primary ms-1" 
            onclick="showMBIDSearchModal('{{ artist }}', '{{ album }}', '{{ release_mbid }}')">
      <i class="bi bi-search"></i> Better Match
    </button>
    {% endif %}
  </h5>
</div>
```

#### MBID Search Modal
```html
<div class="modal fade" id="mbidSearchModal" tabindex="-1">
  <div class="modal-dialog modal-lg">
    <div class="modal-content">
      <div class="modal-header">
        <h5>Search MusicBrainz for Better Match</h5>
        <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body">
        <!-- Pre-filled search fields -->
        <div class="mb-3">
          <label>Artist</label>
          <input type="text" id="mbSearchArtist" class="form-control" value="...">
        </div>
        <div class="mb-3">
          <label>Album</label>
          <input type="text" id="mbSearchAlbum" class="form-control" value="...">
        </div>
        <button class="btn btn-primary" onclick="searchMusicBrainz()">
          <i class="bi bi-search"></i> Search
        </button>
        
        <!-- Search results -->
        <div id="mbSearchResults" class="mt-3"></div>
      </div>
    </div>
  </div>
</div>
```

#### Search Results & Selection
```javascript
function searchMusicBrainz() {
  const artist = document.getElementById('mbSearchArtist').value;
  const album = document.getElementById('mbSearchAlbum').value;
  
  fetch(`/api/musicbrainz/search?artist=${encodeURIComponent(artist)}&album=${encodeURIComponent(album)}`)
    .then(r => r.json())
    .then(releases => {
      let html = '<div class="list-group">';
      releases.forEach(release => {
        html += `
          <div class="list-group-item">
            <div class="d-flex justify-content-between align-items-center">
              <div>
                <strong>${release.artist}</strong> - ${release.title}
                <br><small class="text-muted">${release.year || 'Unknown'} · ${release.country || ''} · ${release.tracks} tracks</small>
                <br><small class="text-muted">MBID: ${release.id}</small>
              </div>
              <button class="btn btn-sm btn-success" 
                      onclick="selectMusicBrainzRelease('${release.id}', '${escapeHtml(release.artist)}', '${escapeHtml(release.title)}')">
                Select
              </button>
            </div>
          </div>`;
      });
      html += '</div>';
      document.getElementById('mbSearchResults').innerHTML = html;
    });
}

function selectMusicBrainzRelease(mbid, artist, album) {
  // Update all queue items for this album
  fetch('/api/queue/update-album-mbid', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      old_artist: currentAlbumArtist,
      old_album: currentAlbumName,
      new_mbid: mbid,
      new_artist: artist,
      new_album: album
    })
  }).then(() => {
    alert('Album updated with new MusicBrainz match!');
    location.reload();
  });
}
```

---

### Phase 8: Move to Music with Tag Management

#### UI - Matched Track Actions
```html
<button class="btn btn-sm btn-success" 
        onclick="moveToMusic({{ queue_id }})">
  <i class="bi bi-folder-symlink"></i> Move to /Music
</button>
```

#### Copy & Tag Workflow
```python
def move_to_music_collection(queue_id):
    """
    1. Copy file from /downloads to /music
    2. Clear existing tags
    3. Write new tags from MusicBrainz
    4. Mark as completed
    """
    queue_item = get_queue_item(queue_id)
    
    if queue_item['status'] != 'matched':
        return {'error': 'Track must be matched first'}
    
    source_path = queue_item['matched_file_path']
    
    # Build destination path using album structure
    dest_dir = os.path.join(
        MUSIC_DIR,
        sanitize_filename(queue_item['album_artist'] or queue_item['artist']),
        sanitize_filename(queue_item['album'])
    )
    os.makedirs(dest_dir, exist_ok=True)
    
    # Destination filename
    track_num = queue_item['track_number'] or ''
    track_prefix = f"{track_num:02d} - " if track_num else ''
    dest_filename = f"{track_prefix}{sanitize_filename(queue_item['title'])}{get_extension(source_path)}"
    dest_path = os.path.join(dest_dir, dest_filename)
    
    # Copy file
    import shutil
    shutil.copy2(source_path, dest_path)
    
    # Clear and rewrite tags
    update_music_tags(dest_path, queue_item)
    
    # Mark as completed
    cursor.execute("""
        UPDATE download_queue
        SET status = 'completed',
            file_path = ?
        WHERE id = ?
    """, (dest_path, queue_id))
    
    return {'success': True, 'path': dest_path}
```

#### Tag Writing (Beets-style)
```python
def update_music_tags(file_path, queue_item):
    """
    Clear all existing tags and write fresh MusicBrainz metadata.
    Uses mutagen library (same as beets).
    """
    import mutagen
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, TRCK, TPE2
    from mutagen.flac import FLAC
    from mutagen.mp4 import MP4
    
    ext = os.path.splitext(file_path)[1].lower()
    
    if ext == '.mp3':
        audio = ID3(file_path)
        audio.delete()  # Clear all tags
        
        audio.add(TIT2(encoding=3, text=queue_item['title']))
        audio.add(TPE1(encoding=3, text=queue_item['artist']))
        audio.add(TALB(encoding=3, text=queue_item['album']))
        
        if queue_item['album_artist']:
            audio.add(TPE2(encoding=3, text=queue_item['album_artist']))
        
        if queue_item['release_year']:
            audio.add(TDRC(encoding=3, text=str(queue_item['release_year'])))
        
        if queue_item['track_number']:
            audio.add(TRCK(encoding=3, text=str(queue_item['track_number'])))
        
        # Add MusicBrainz IDs
        if queue_item['release_mbid']:
            from mutagen.id3 import TXXX
            audio.add(TXXX(encoding=3, desc='MusicBrainz Album Id', text=queue_item['release_mbid']))
        
        if queue_item['recording_mbid']:
            from mutagen.id3 import UFID
            audio.add(UFID(owner='http://musicbrainz.org', data=queue_item['recording_mbid'].encode()))
        
        audio.save(file_path)
    
    elif ext == '.flac':
        audio = FLAC(file_path)
        audio.delete()  # Clear all tags
        
        audio['TITLE'] = queue_item['title']
        audio['ARTIST'] = queue_item['artist']
        audio['ALBUM'] = queue_item['album']
        
        if queue_item['album_artist']:
            audio['ALBUMARTIST'] = queue_item['album_artist']
        
        if queue_item['release_year']:
            audio['DATE'] = str(queue_item['release_year'])
        
        if queue_item['track_number']:
            audio['TRACKNUMBER'] = str(queue_item['track_number'])
        
        if queue_item['release_mbid']:
            audio['MUSICBRAINZ_ALBUMID'] = queue_item['release_mbid']
        
        if queue_item['recording_mbid']:
            audio['MUSICBRAINZ_TRACKID'] = queue_item['recording_mbid']
        
        audio.save()
    
    # Add support for .m4a, .ogg, etc. as needed
```

---

### Phase 9: Auto-Cleanup & Status Management

#### Cleanup Job (runs every hour)
```python
def cleanup_download_queue():
    """
    1. Delete duplicates older than 24 hours
    2. Delete completed albums where all tracks are completed/in_collection
    """
    conn = get_db()
    cursor = conn.cursor()
    
    # Delete expired duplicates
    cursor.execute("""
        DELETE FROM download_queue
        WHERE status = 'duplicate'
        AND auto_delete_at IS NOT NULL
        AND auto_delete_at < CURRENT_TIMESTAMP
    """)
    deleted_duplicates = cursor.rowcount
    
    # Find completed albums
    cursor.execute("""
        SELECT DISTINCT album, artist
        FROM download_queue
        WHERE album IS NOT NULL
        GROUP BY album, artist
        HAVING COUNT(*) = SUM(CASE WHEN status IN ('completed', 'in_collection') THEN 1 ELSE 0 END)
    """)
    
    completed_albums = cursor.fetchall()
    
    for album_info in completed_albums:
        cursor.execute("""
            DELETE FROM download_queue
            WHERE album = ? AND artist = ?
            AND status IN ('completed', 'in_collection', 'duplicate')
        """, (album_info['album'], album_info['artist']))
    
    conn.commit()
    logger.info(f"Cleanup: Deleted {deleted_duplicates} expired duplicates, {len(completed_albums)} completed albums")
```

#### Queue Processor Updates
```python
def process_download_queue():
    """
    Updated queue processor respecting new statuses.
    Only process items with status = 'queued'.
    """
    cursor.execute("""
        SELECT * FROM download_queue
        WHERE status = 'queued'  -- ONLY queued items
        AND source = 'soulseek'
        ORDER BY priority ASC, created_at ASC
        LIMIT 10
    """)
    
    # Existing SLSK download logic...
```

---

## Implementation Priority

### High Priority (Core Functionality)
1. ✅ Database schema additions (Phase 1)
2. ✅ Duplicate detection (Phase 3)
3. ✅ Enhanced file matching (Phase 4)
4. ✅ Collection matching (Phase 6)

### Medium Priority (User Workflow)
5. ⚠️ Unmatched file auto-search (Phase 5)
6. ⚠️ Move to Music + tagging (Phase 8)
7. ⚠️ Auto-cleanup jobs (Phase 9)

### Lower Priority (Nice to Have)
8. 🔵 MBID manual search UI (Phase 7)
9. 🔵 Enhanced status visualization
10. 🔵 Batch operations

---

## Feasibility Assessment

### ✅ Fully Possible
- All database schema changes
- Duplicate detection
- File path tracking
- Collection matching
- Status workflow
- Auto-cleanup
- Tag management with mutagen

### ⚠️ Requires Integration
- MusicBrainz auto-search (reuse existing `folder_matching_enhancements.py`)
- File tagging (install `mutagen` package - same as beets uses)
- Navidrome collection queries (existing DB connection)

### 🔵 No Blockers Identified
Everything requested is technically feasible with existing infrastructure!

---

## Next Steps

1. **Review this plan** - Confirm priorities and approach
2. **Commit z-score confidence fix** - Complete pending work
3. **Begin Phase 1** - Database schema migration
4. **Implement phases incrementally** - Test each phase before moving to next

Would you like to proceed with implementation, starting with Phase 1 (database schema)?
