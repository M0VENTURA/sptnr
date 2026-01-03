# Unified Scan Implementation - Summary of Changes

## Problem Statement
Re-merge popularity detection and single check to run sequentially by artist → album in perpetual and batch mode. The dashboard should have a start button, progress manager, and ticker bar showing current progress in non-verbose detail.

## Solution Implemented

### 1. Unified Scan Pipeline (`unified_scan.py`)

**New Module:** Coordinates all scan operations in a single workflow

**Key Features:**
- Sequential processing: Artist → Album → Track
- Three integrated phases per album:
  1. Popularity detection (Spotify, Last.fm, ListenBrainz)
  2. Single detection (Discogs, MusicBrainz, etc.)
  3. Rating calculation and Navidrome sync

**Progress Tracking:**
- `ScanProgress` class maintains state
- Saved to JSON file for persistence
- Tracks: current artist/album, processed counts, phase, completion %

### 2. Dashboard Enhancements (`templates/dashboard.html`)

**New UI Elements:**

**Progress Bar:**
```html
<div class="progress">
  <div class="progress-bar progress-bar-striped progress-bar-animated">
    <!-- Shows completion % -->
  </div>
</div>
```

**Ticker Display:**
- 🎵 Current Artist
- 💿 Current Album  
- 📊 Phase (Popularity/Singles/Rating)
- Track counts (processed/total)

**Unified Scan Button:**
- Replaced "Batch Rate" with "Start Unified Scan"
- Single button launches full workflow
- Green Spotify-style button for clarity

### 3. Progress Manager Backend (`app.py`)

**New Routes:**
- `POST /scan/unified` - Start unified scan
- `GET /api/scan-progress` - Get current progress

**Smart Progress Detection:**
- Checks unified scan first
- Falls back to MP3 scan progress
- Falls back to Navidrome scan progress
- Returns appropriate progress data

### 4. Integration Points

**Modified `batch_rate()` in `sptnr.py`:**
```python
# Now uses unified_scan_pipeline()
from unified_scan import unified_scan_pipeline
unified_scan_pipeline(verbose=True, force=force, artist_filter=resume_from)
```

**Modified `run_perpetual_mode()` in `sptnr.py`:**
```python
# 12-hour loop now uses unified scan
unified_scan_pipeline(
    verbose=args.verbose,
    force=args.force,
    artist_filter=resume_artist
)
```

### 5. Additional Progress Tracking

**MP3 Scanner (`mp3scanner.py`):**
- Tracks current folder being scanned
- Counts files scanned and matched
- Saves to `/database/mp3_scan_progress.json`

**Navidrome Sync (`scan_helpers.py`):**
- Tracks current artist and album
- Counts albums processed
- Saves to `/database/navidrome_scan_progress.json`

### 6. JavaScript Enhancements (`dashboard.html`)

**Real-time Updates:**
- Polls `/api/scan-progress` every 1 second
- Updates progress bar dynamically
- Shows different displays for different scan types
- Handles unified, MP3, and Navidrome scans

**Smart Display Logic:**
```javascript
if (scanType === 'unified') {
  // Show artist → album → phase
} else if (scanType === 'mp3_scan') {
  // Show folder → file counts
} else if (scanType === 'navidrome_scan') {
  // Show artist → album import
}
```

## Files Modified

### New Files
- `unified_scan.py` - Main pipeline coordinator
- `UNIFIED_SCAN_README.md` - Implementation documentation

### Modified Files
- `app.py` - Added routes and API endpoints
- `templates/dashboard.html` - Progress UI and JavaScript
- `sptnr.py` - Integrated unified scan into batch/perpetual
- `mp3scanner.py` - Added progress tracking
- `scan_helpers.py` - Added progress tracking

## Progress Data Structure

### Unified Scan Progress
```json
{
  "current_artist": "Pink Floyd",
  "current_album": "Dark Side of the Moon",
  "total_artists": 100,
  "processed_artists": 42,
  "total_tracks": 5000,
  "processed_tracks": 2125,
  "scan_type": "unified",
  "is_running": true,
  "current_phase": "singles",
  "elapsed_seconds": 1800,
  "percent_complete": 42.5
}
```

### MP3 Scan Progress
```json
{
  "current_folder": "Pink Floyd/Dark Side of the Moon",
  "scanned_files": 1000,
  "total_files": 5000,
  "matched_files": 950,
  "is_running": true,
  "scan_type": "mp3_scan"
}
```

### Navidrome Scan Progress
```json
{
  "current_artist": "Pink Floyd",
  "current_album": "Dark Side of the Moon",
  "scanned_albums": 10,
  "total_albums": 50,
  "is_running": true,
  "scan_type": "navidrome_scan"
}
```

## User Experience Improvements

### Before
1. Click "Navidrome" scan → wait
2. Click "Popularity" scan → wait  
3. Click "Singles" scan → wait
4. No progress visibility
5. No idea what's happening

### After
1. Click "Start Unified Scan" → done
2. See progress bar advancing
3. See "🎵 Pink Floyd → 💿 Dark Side of the Moon"
4. See "📊 Detecting singles & rating..."
5. Know exactly where scan is at

## Technical Benefits

✅ **Coordination** - Single pipeline, no race conditions
✅ **Visibility** - Real-time progress updates
✅ **Efficiency** - Better API rate limiting
✅ **Maintainability** - Centralized scan logic
✅ **User-friendly** - Non-verbose, clear progress

## Backward Compatibility

- Legacy scan buttons still available in "Detailed Scans"
- Individual scans (Popularity, Singles) still work
- Fallback to legacy batch_rate if unified scan fails
- No breaking changes to existing functionality

## Testing Checklist

Manual testing recommended:
- [ ] Start unified scan from dashboard
- [ ] Verify progress bar updates in real-time
- [ ] Check ticker shows current artist/album
- [ ] Verify phase indicator changes (popularity → singles → rating)
- [ ] Test with single artist filter
- [ ] Test perpetual mode (12-hour loop)
- [ ] Test MP3 scan progress display
- [ ] Test Navidrome scan progress display
- [ ] Verify database updates correctly
- [ ] Check Navidrome sync works

## Performance Characteristics

- **Memory:** Low - processes one artist at a time
- **CPU:** Moderate - API calls and calculations
- **Network:** Optimized - sequential processing respects rate limits
- **Database:** Efficient - WAL mode for concurrency

## Deployment Notes

**Environment Variables:**
```bash
DB_PATH=/database/sptnr.db
PROGRESS_FILE=/database/scan_progress.json
MP3_PROGRESS_FILE=/database/mp3_scan_progress.json
NAVIDROME_PROGRESS_FILE=/database/navidrome_scan_progress.json
```

**Directory Permissions:**
- `/database/` must be writable for progress files
- `/config/` must be writable for logs

**First Run:**
- Progress files will be created automatically
- Old scan processes will complete normally
- New scans use unified pipeline

## Future Enhancements

Potential improvements:
- Pause/resume capability
- Scan scheduling (specific times)
- Parallel artist processing (with rate limiting)
- Historical progress charts
- Email notifications on completion
- Webhook integration

## Summary

This implementation successfully merges popularity detection and single detection into a unified, coordinated workflow with comprehensive progress tracking. The dashboard now provides clear, real-time visibility into scan progress with a single button to start the entire workflow. All scans (unified, MP3, Navidrome) now report progress in a consistent, user-friendly manner.
