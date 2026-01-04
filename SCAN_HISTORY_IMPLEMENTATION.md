# Scan History Implementation

## Overview
This implementation adds album-level scan tracking to the dashboard, showing each album as it's processed by different scan types (Navidrome, Popularity, Beets) in real-time.

## Changes Made

### 1. Database Schema (`check_db.py`)
Added `scan_history` table to track individual album scans:

```sql
CREATE TABLE IF NOT EXISTS scan_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artist TEXT NOT NULL,
    album TEXT NOT NULL,
    scan_type TEXT NOT NULL,
    scan_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    tracks_processed INTEGER DEFAULT 0,
    status TEXT DEFAULT 'completed'
);
```

Indexes added:
- `idx_scan_history_timestamp` - for fast recent scans queries
- `idx_scan_history_type` - for filtering by scan type

### 2. Scan History Module (`scan_history.py`)
New module providing helper functions:

**`log_album_scan(artist, album, scan_type, tracks_processed, status)`**
- Logs completion of an album scan
- Creates scan_history table if it doesn't exist
- Records artist, album, scan type, timestamp, tracks processed, and status

**`get_recent_album_scans(limit=10)`**
- Returns most recent album scans
- Ordered by timestamp descending
- Returns list of dicts with all scan details

### 3. Dashboard Route (`app.py`)
Updated dashboard to use `get_recent_album_scans()`:

```python
from scan_history import get_recent_album_scans
# ...
recent_scans = get_recent_album_scans(limit=10)
```

### 4. Dashboard UI (`templates/dashboard.html`)
Added "Scan Type" column to Recent Scans table with colored badges:

- **Navidrome** - Blue badge with database icon
- **Popularity** - Green badge with graph icon  
- **Beets** - Cyan badge with music note icon

Each badge displays the scan source that processed the album.

### 5. Popularity Scanner (`popularity.py`)
Added album-level tracking:

- Imports `log_album_scan` with fallback
- Tracks current album as it processes tracks
- Logs album completion when album changes
- Logs final album after loop completes

Example:
```python
current_album = None
album_tracks = 0

# In track loop
if current_album != (artist_name, album_name):
    if current_album is not None and album_tracks > 0:
        log_album_scan(current_album[0], current_album[1], 'popularity', album_tracks, 'completed')
    current_album = (artist_name, album_name)
    album_tracks = 0

album_tracks += 1
```

### 6. Beets Importer (`beets_auto_import.py`)
Added progress tracking and album logging:

- Imports `log_album_scan` with fallback
- Added `save_beets_progress()` function for dashboard polling
- Modified `sync_beets_to_sptnr()` to:
  - Track album changes
  - Save progress every 50 tracks
  - Log each album completion
  - Write to `mp3_scan_progress.json`

### 7. Navidrome Scanner (`scan_helpers.py`)
Added album logging to `scan_artist_to_db()`:

- Imports `log_album_scan` with fallback
- Tracks `album_tracks_processed` count
- Logs album completion after processing all tracks

```python
album_tracks_processed = 0
# ... process tracks
album_tracks_processed += 1

# After processing all tracks in album
if album_tracks_processed > 0:
    log_album_scan(artist_name, album_name, 'navidrome', album_tracks_processed, 'completed')
```

## How It Works

### Scan Flow
1. **Scan starts** - Any of the three scan types (Navidrome/Popularity/Beets)
2. **Album processing** - Scanner processes tracks grouped by album
3. **Album completion** - `log_album_scan()` called when album finishes
4. **Database insert** - Album scan recorded in `scan_history` table
5. **Dashboard display** - Recent Scans section shows the album with scan type badge

### Real-Time Updates
- JavaScript polls `/api/scan-progress` and `/api/scan-logs` every 3 seconds
- Dashboard queries `scan_history` table for 10 most recent scans
- Each scan shows:
  - Artist name
  - Album name  
  - Scan type (with colored badge)
  - Timestamp
  - Number of tracks processed

### Scan Type Badges
| Scan Type | Color | Icon | Description |
|-----------|-------|------|-------------|
| navidrome | Blue | 📊 | Library sync from Navidrome API |
| popularity | Green | 📈 | External API popularity scoring |
| beets | Cyan | 🎵 | MusicBrainz metadata import |

## Progress Files
Each scan type writes progress to JSON files:

- **Popularity**: `/config/popularity_scan_progress.json`
- **Beets**: `/config/mp3_scan_progress.json`  
- **Navidrome**: `/config/navidrome_scan_progress.json`

Progress files contain:
```json
{
  "is_running": true,
  "scan_type": "popularity_scan",
  "processed": 150,
  "total": 2000,
  "percent_complete": 7
}
```

## Testing
To test the implementation:

1. **Start a scan** - Use any scan button on the dashboard
2. **Watch Recent Scans** - Albums should appear as they're processed
3. **Check scan type badges** - Each album shows correct colored badge
4. **Verify progress bars** - Progress indicators should update in real-time
5. **Check logs** - Scan logs should show album completion messages

## Database Migration
The `scan_history` table is created automatically on first run via `check_db.py`:

```python
from check_db import update_schema
update_schema(DB_PATH)
```

This is called on app startup in both `app.py` and `start.py`.

## Benefits

### User Experience
- **Real-time visibility** - See exactly which albums are being processed
- **Scan type identification** - Know which service processed each album
- **Progress tracking** - Monitor scan progress album-by-album
- **Historical record** - View recent scan activity at a glance

### Technical
- **Granular tracking** - Album-level instead of just artist-level
- **Multiple scan types** - Support for all three scanner types
- **Consistent API** - Single `log_album_scan()` function for all scanners
- **Fallback support** - Gracefully handles missing module imports
- **Indexed queries** - Fast database lookups for dashboard display

## Future Enhancements
Potential improvements:

- Add scan duration tracking
- Show failed/skipped albums differently
- Filter Recent Scans by scan type
- Export scan history to CSV
- Show scan statistics (avg albums/hour, etc.)
- Add retry mechanism for failed albums
- Display which API sources were used per album
