# Album Rescan Prevention

## Overview

The popularity scanner now tracks which albums have been successfully scanned and will skip them on subsequent scans to prevent unnecessary API calls and duplicate processing.

## How It Works

1. When an album is scanned, it's logged to the `scan_history` table with:
   - Artist name
   - Album name
   - Scan type (e.g., 'popularity')
   - Timestamp
   - Status (completed/error/skipped)

2. Before scanning an album, the scanner checks if it was already successfully scanned
3. If already scanned, the album is skipped (logged with ⏭ emoji)
4. At the end of the scan, a summary shows: tracks updated and albums skipped

## Configuration

### Normal Mode (Default)

Skips albums that were already successfully scanned:

```bash
# Default behavior - no configuration needed
python3 popularity.py
```

### Force Rescan Mode

Rescans all albums regardless of scan history:

```bash
# Set environment variable
SPTNR_FORCE_RESCAN=1 python3 popularity.py

# Or in Docker Compose
environment:
  - SPTNR_FORCE_RESCAN=1
```

## Use Cases

### When to Use Normal Mode
- Daily/regular scans
- After adding new music
- Most production scenarios

### When to Use Force Rescan Mode
- After algorithm changes
- Database maintenance/recovery
- Testing new configurations
- Manual override needed

## Example Output

### Normal Mode
```
📋 Normal scan mode - will skip albums that were already scanned
Currently Scanning Artist: A Killer's Confession
⏭ Skipping already-scanned album: "A Killer's Confession - Remember"
✅ Popularity scan completed: 0 tracks updated, 1 albums skipped (already scanned)
```

### Force Rescan Mode
```
⚠ Force rescan mode enabled - will rescan all albums regardless of scan history
Currently Scanning Artist: A Killer's Confession
Scanning "A Killer's Confession - Remember" for Popularity
✅ Popularity scan completed: 12 tracks updated, 0 albums skipped
```

## Database Schema

The `scan_history` table tracks all scans:

```sql
CREATE TABLE scan_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artist TEXT NOT NULL,
    album TEXT NOT NULL,
    scan_type TEXT NOT NULL,
    scan_timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    tracks_processed INTEGER DEFAULT 0,
    status TEXT DEFAULT 'completed',
    source TEXT DEFAULT ''
)
```

Indexes for performance:
- `idx_scan_history_timestamp` - for recent scans
- `idx_scan_history_artist_album` - for lookup efficiency

## Troubleshooting

### Albums Still Being Rescanned?

1. Check scan_history table:
   ```sql
   SELECT * FROM scan_history 
   WHERE artist = 'Artist Name' AND album = 'Album Name'
   ORDER BY scan_timestamp DESC;
   ```

2. Verify the scan status is 'completed':
   - Only 'completed' scans prevent rescanning
   - 'error' or 'skipped' scans don't prevent rescanning

3. Check if SPTNR_FORCE_RESCAN is set to 1

### Need to Clear Scan History?

To force a rescan of all albums, either:

1. Set `SPTNR_FORCE_RESCAN=1` (temporary)
2. Clear the scan_history table (permanent):
   ```sql
   DELETE FROM scan_history WHERE scan_type = 'popularity';
   ```
