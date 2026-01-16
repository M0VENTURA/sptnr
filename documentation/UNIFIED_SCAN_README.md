# Unified Scan Pipeline - Implementation Guide

## Overview

The unified scan pipeline merges popularity detection and single detection into a single, coordinated workflow that processes music library artist-by-artist and album-by-album. This provides better tracking, clearer progress reporting, and ensures data consistency.

## Key Features

### 1. Sequential Processing
- **Artist → Album → Track** workflow
- Each artist is processed completely before moving to the next
- Within each artist, albums are processed one at a time
- Ensures data consistency and reduces API rate limiting issues

### 2. Integrated Workflow
The pipeline executes three phases for each album:
1. **Popularity Detection** - Fetches scores from Spotify, Last.fm, and ListenBrainz
2. **Single Detection** - Identifies which tracks are singles using multiple sources
3. **Rating & Sync** - Calculates star ratings and syncs to Navidrome

### 3. Progress Tracking
Real-time progress tracking shows:
- Current artist being processed
- Current album being processed
- Current phase (popularity/singles/rating)
- Track count and completion percentage
- Estimated progress

## Usage

### Web Dashboard
1. Navigate to the dashboard
2. Click "Start Unified Scan" button
3. Monitor progress in real-time with the progress bar
4. View current artist/album and phase in the ticker

### Command Line
```bash
# Run unified scan on all artists
python unified_scan.py --verbose

# Run on specific artist
python unified_scan.py --artist "Radiohead" --verbose

# Force re-scan (ignore cache)
python unified_scan.py --force --verbose
```

### Perpetual Mode
The unified scan automatically runs in perpetual mode when configured:
```python
# In config.yaml
features:
  perpetual: true
  batchrate: true
```

## Progress Files

Progress is stored in JSON files for persistence and API access:

### Unified Scan Progress
**Location:** `/database/scan_progress.json`

**Fields:**
```json
{
  "current_artist": "Artist Name",
  "current_album": "Album Name",
  "total_artists": 100,
  "processed_artists": 25,
  "total_tracks": 5000,
  "processed_tracks": 1250,
  "scan_type": "unified",
  "is_running": true,
  "current_phase": "popularity",
  "elapsed_seconds": 300,
  "percent_complete": 25.0
}
```

### MP3 Scan Progress
**Location:** `/database/mp3_scan_progress.json`

**Fields:**
```json
{
  "current_folder": "Artist/Album",
  "scanned_files": 1000,
  "total_files": 5000,
  "matched_files": 950,
  "is_running": true,
  "scan_type": "mp3_scan"
}
```

### Navidrome Scan Progress
**Location:** `/database/navidrome_scan_progress.json`

**Fields:**
```json
{
  "current_artist": "Artist Name",
  "current_album": "Album Name",
  "scanned_albums": 10,
  "total_albums": 50,
  "is_running": true,
  "scan_type": "navidrome_scan"
}
```

## API Endpoints

### Get Scan Progress
**Endpoint:** `GET /api/scan-progress`

**Response:**
```json
{
  "is_running": true,
  "percent_complete": 42.5,
  "current_artist": "Pink Floyd",
  "current_album": "Dark Side of the Moon",
  "current_phase": "singles",
  "processed_artists": 42,
  "total_artists": 100,
  "processed_tracks": 2125,
  "total_tracks": 5000,
  "scan_type": "unified"
}
```

### Get Scan Status
**Endpoint:** `GET /api/scan-status`

**Response:**
```json
{
  "main_scan": {
    "name": "Main Rating Scan",
    "running": false
  },
  "mp3_scan": {
    "name": "File Path Scan",
    "running": false
  },
  "navidrome_scan": {
    "name": "Navidrome Sync",
    "running": false
  },
  "popularity_scan": {
    "name": "Popularity Update",
    "running": false
  },
  "singles_scan": {
    "name": "Single Detection",
    "running": false
  }
}
```

## Architecture

### File Structure
```
sptnr/
├── unified_scan.py          # Main unified scan pipeline
├── popularity.py             # Popularity detection module
├── singledetection.py        # Single detection module
├── start.py                  # Rating calculation and Navidrome sync
├── scan_helpers.py           # Navidrome import helpers
├── mp3scanner.py             # MP3 file scanner
└── app.py                    # Web UI and API endpoints
```

### Data Flow
```
┌─────────────────────────────────────────┐
│         Unified Scan Pipeline           │
└─────────────────────────────────────────┘
                    │
        ┌───────────┼───────────┐
        ▼           ▼           ▼
┌─────────────┐ ┌──────────┐ ┌─────────┐
│ Popularity  │ │  Single  │ │ Rating  │
│ Detection   │ │Detection │ │  & Sync │
└─────────────┘ └──────────┘ └─────────┘
        │           │           │
        └───────────┼───────────┘
                    ▼
            ┌──────────────┐
            │   Database   │
            └──────────────┘
                    │
                    ▼
            ┌──────────────┐
            │  Navidrome   │
            └──────────────┘
```

## Benefits

### 1. Better Coordination
- Single pipeline ensures all steps run in correct order
- No race conditions between separate scans
- Consistent data state throughout process

### 2. Improved Progress Tracking
- Real-time updates on current progress
- Clear visibility into what's happening
- Non-verbose yet informative status

### 3. Resource Efficiency
- Better API rate limiting management
- Reduced redundant lookups
- Optimized database access

### 4. User Experience
- Single "Start Scan" button for full workflow
- Clear progress indication
- Predictable completion times

## Migration from Legacy Scans

The unified scan replaces the need to run separate scans:

**Before:**
1. Run Navidrome import
2. Run popularity scan
3. Run single detection
4. Run rating calculation

**After:**
1. Run unified scan (does all of the above)

Legacy scans are still available as individual operations in the "Detailed Scans" section of the dashboard.

## Troubleshooting

### Scan appears stuck
- Check `/config/unified_scan.log` for errors
- Verify API credentials in config.yaml
- Check network connectivity

### Progress not updating
- Ensure JSON progress files are writable
- Check browser console for JavaScript errors
- Verify API endpoint is accessible

### High memory usage
- Unified scan processes one artist at a time to minimize memory
- Adjust database connection timeout if needed
- Consider running with smaller batches

## Configuration

### Environment Variables
```bash
# Database path
export DB_PATH="/database/sptnr.db"

# Progress file locations
export PROGRESS_FILE="/database/scan_progress.json"
export MP3_PROGRESS_FILE="/database/mp3_scan_progress.json"
export NAVIDROME_PROGRESS_FILE="/database/navidrome_scan_progress.json"
```

### Config.yaml Settings
```yaml
features:
  perpetual: true          # Enable 12-hour auto-scan
  batchrate: true          # Enable batch processing
  verbose: false           # Detailed logging
  force: false             # Force re-scan all
```

## Performance Considerations

- **API Rate Limits:** Unified scan respects rate limits by processing sequentially
- **Database Load:** WAL mode enabled for better concurrency
- **Memory Usage:** Processes one artist at a time to minimize memory footprint
- **Network:** Bulk operations reduced to minimize network overhead

## Future Enhancements

Potential improvements:
- Parallel processing of independent artists
- Configurable scan priorities
- Resume capability from interruptions
- Historical progress tracking
- Performance metrics and analytics
