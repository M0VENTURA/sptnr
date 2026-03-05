# MusicBrainz Release Download Flow

## Overview

This document describes the complete flow for downloading entire MusicBrainz releases track-by-track through the queue system.

## Flow Stages

### 1. User Initiates Download

**Endpoint:** `POST /api/musicbrainz/download`

**Input:**
- `release_id`: MusicBrainz release ID
- `release_title`: Album title
- `artist`: Artist name
- `method`: Download method ('slskd' or 'qbittorrent')

**Process:**
1. Fetch release details from MusicBrainz API
2. Get full track listing with:
   - Track numbers
   - Track names
   - Track artists
   - Duration
   - ISRC codes (for matching)
3. Create monitoring folder: `/downloads/Music/YEAR - ARTIST - ALBUM/`
4. Create database entry linking release to monitoring folder
5. Add each track to `download_queue` as separate items
6. Set all queue items to `status='queued'`

### 2. Display in Active Queue

**Location:** `/downloads/monitor` → "Active Queue"

**Display Format:**
- Artist Name - Song Name
- Show track number
- Display MusicBrainz release context (highlighted)
- Show status: queued, searching, downloading

**Database Link:**
- Each queue item references the release_id
- Allow filtering/grouping by release

### 3. Download Management

**Queue Processor:** `downloads_queue.py` or via slskd integration

**Process:**
1. Pick queue item with `status='queued'`
2. Initiate slskd search for "Artist - Title"
3. Update status to `status='searching'`
4. Once file found and selected:
   - Update status to `status='downloading'`
   - Record `found_filename`
5. When download completes:
   - Update status to `status='discovered'`
   - Record `file_path` (actual location)

### 4. File Discovery & Movement

**Process:** When file appears in `/downloads/`

1. Check if file matches any release's track list:
   - Use filename matching (fuzzy matching on artist/track)
   - Check ID3 tags (artist, track number, title)
2. If match found:
   - Create release monitoring folder if doesn't exist
   - Move file to: `/downloads/Music/YEAR - ARTIST - ALBUM/FILENAME`
   - Update queue item: `status='organized'`, `file_path=new_location`
3. Track progress in release:
   - Count files in monitoring folder
   - Compare to expected track count

### 5. Release Completion

**Trigger:** When all expected tracks found in monitoring folder

1. For each file in monitoring folder:
   - Parse/read ID3 tags or filename
   - Extract: track number, artist, title
   - Generate final name: `01. Artist - Song Name.mp3`
2. Create final directory:
   - `/music/ALBUM_ARTIST/YEAR - ALBUM_NAME/`
3. Move and rename files:
   - `/music/ALBUM_ARTIST/YEAR - ALBUM_NAME/01. Artist - Song Name.ext`
4. Delete monitoring folder: `/downloads/Music/YEAR - ARTIST - ALBUM/`
5. Update release status to `status='finalized'`

## Database Schema

### musicbrainz_releases (NEW)

Tracks active release downloads.

```sql
CREATE TABLE musicbrainz_releases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id TEXT NOT NULL UNIQUE,
    release_title TEXT NOT NULL,
    artist TEXT NOT NULL,
    release_year INTEGER,
    total_tracks INTEGER,
    
    -- Paths
    monitoring_folder_path TEXT,
    final_folder_path TEXT,
    
    -- Status
    status TEXT DEFAULT 'active',  -- active, finalizing, finalized, failed
    method TEXT,  -- slskd, qbittorrent
    
    -- Tracking
    discovered_count INTEGER DEFAULT 0,
    organized_count INTEGER DEFAULT 0,
    finalized_count INTEGER DEFAULT 0,
    
    -- Timestamps
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    finalized_at TIMESTAMP
);
```

### musicbrainz_release_tracks (NEW)

Tracks which queue items belong to which release.

```sql
CREATE TABLE musicbrainz_release_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id TEXT NOT NULL,
    queue_id INTEGER,
    
    -- Track info from MB
    track_number INTEGER,
    track_title TEXT,
    track_artist TEXT,
    duration INTEGER,  -- seconds
    isrc TEXT,
    
    -- Current file
    found_filename TEXT,
    file_path TEXT,
    
    -- Status
    status TEXT DEFAULT 'queued',  -- queued, searching, downloading, discovered, organized, finalized
    
    -- Timestamps
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    FOREIGN KEY (release_id) REFERENCES musicbrainz_releases(release_id),
    FOREIGN KEY (queue_id) REFERENCES download_queue(id)
);
```

### Modifications to download_queue

Add these columns:

```sql
ALTER TABLE download_queue ADD COLUMN release_id TEXT;
ALTER TABLE download_queue ADD COLUMN track_number INTEGER;
ALTER TABLE download_queue ADD COLUMN is_final_file INTEGER DEFAULT 0;
```

## API Endpoints

### GET /api/musicbrainz/releases/active

Lists all active release downloads with progress.

**Response:**
```json
{
  "releases": [
    {
      "id": "...",
      "release_id": "...",
      "release_title": "...",
      "artist": "...",
      "release_year": 2026,
      "total_tracks": 12,
      "discovered_count": 3,
      "organized_count": 2,
      "finalized_count": 0,
      "status": "active",
      "monitoring_folder": "/downloads/Music/2026 - Artist - Album/",
      "created_at": "..."
    }
  ]
}
```

### POST /api/musicbrainz/download (MODIFIED)

Now fetches release data and creates queue items.

**Input:**
```json
{
  "release_id": "...",
  "release_title": "...",
  "artist": "...",
  "method": "slskd"
}
```

**Process:**
1. Fetch from MusicBrainz API
2. Create monitoring folder
3. Add queue items
4. Return release_id and track count

### POST /api/queue/{id}/finalize

Finalizes a release when all tracks are discovered.

**Response:**
```json
{
  "success": true,
  "files_moved": 12,
  "final_path": "/music/Artist/2026 - Album/"
}
```

## File Matching Logic

### Filename Matching

```
Query: "Artist - Title"
File: "artist_-_title_320kbps.mp3"

Score factors:
- All query words appear in filename (weight: 0.6)
- Word order matches (weight: 0.2)
- Extension is audio file (weight: 0.1)
- No extra artist info contradicts (weight: 0.1)

Threshold: 0.5+ matches
```

### ID3 Tag Matching

1. Check ID3 artist tag against queue artist
2. Check ID3 title against queue title
3. Check ID3 track number (if available)

**Preferred:** Tag matching > Filename matching

## Folder Naming Convention

### Monitoring Folder
`/downloads/Music/{YEAR} - {ARTIST} - {ALBUM}/`

Example: `/downloads/Music/2026 - Angus McSix - Angus McSix and the All Seeing Astral Eye/`

### Final Folder
`/music/{ALBUM_ARTIST}/{YEAR} - {ALBUM}/`

Example: `/music/Angus McSix/2026 - Angus McSix and the All Seeing Astral Eye/`

### Final File
`{TRACK_NUM}. {ARTIST} - {TITLE}.{EXT}`

Example: `01. Angus McSix - Track One.mp3`

## Status Workflow

```
Queue Item Status:
  queued → searching → downloading → discovered → organized → finalized

Release Status:
  active → finalizing → finalized

Folder States:
  monitoring_folder (active) → deleted (finalized)
```

## Retry Logic

- Queue items follow standard retry logic (configurable per item)
- Failed items remain in queue for manual retry
- Release doesn't finalize until all tracks discovered

## Implementation Phases

1. **Phase 1:** Database schema + basic endpoints
2. **Phase 2:** MusicBrainz release data fetching
3. **Phase 3:** Queue item creation + display
4. **Phase 4:** File matching + movement logic
5. **Phase 5:** Release finalization + cleanup
6. **Phase 6:** UI integration + testing
