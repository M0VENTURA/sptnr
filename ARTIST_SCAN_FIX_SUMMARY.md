# Artist Scan and Force Rescan Fixes - Summary

## Issues Addressed

### Issue 1: Artist Scan Not Running/Logging
**Problem**: Clicking "Scan Artist" button showed no logs in unified_scan.log or Recent Scans view.

**Root Cause**: The artist scan was calling `unified_scan_pipeline()` instead of calling `popularity.py` directly. This caused the scan to behave differently than expected.

**Fix Applied**:
1. Changed `_run_artist_scan_pipeline()` to call `popularity_scan()` directly (app.py line 2547)
2. Removed call to `unified_scan_pipeline()` which was redundant
3. Removed unused import of `rate_artist_single_detection`
4. Updated flow to be: navidrome_import.py → popularity.py

**New Flow**:
- Step 1: `scan_artist_to_db()` - Import metadata from Navidrome
- Step 2: `popularity_scan()` - Run popularity + singles detection + star rating

**Why This Works**:
- `popularity.py` already includes singles detection (lines 814-967)
- `popularity.py` already includes star rating calculation (lines 969-1046)
- Direct calls ensure proper logging to unified_scan.log
- Both navidrome_import and popularity modules use `log_unified()` function

**Files Changed**:
- `app.py` - Changed artist scan to call popularity.py directly

### Issue 2: Force Rescan Not Working
**Problem**: Setting `force=true` and `album_skip_days=0` was still skipping already-scanned albums.

**Root Cause**: The `popularity_scan()` function only checked the `SPTNR_FORCE_RESCAN` environment variable and ignored the `force` parameter passed to it.

**Fix Applied**:
1. Added `force` parameter to `popularity_scan()` function signature (line 563)
2. Updated force rescan check at line 581 to: `if FORCE_RESCAN or force:`
3. Updated album skip check at line 697 to: `if not (FORCE_RESCAN or force) and was_album_scanned(...)`
4. Updated `unified_scan_pipeline()` to pass `force` parameter to `popularity_scan()` (line 288)
5. Added `--force` CLI argument support

**Files Changed**:
- `popularity.py` - Added force parameter and updated logic
- `unified_scan.py` - Passes force parameter to popularity_scan

## Debugging Added

Extensive logging to track execution flow and identify issues:

1. **scan_start() route** (`app.py` line 2689):
   - Logs when route is called
   - Logs scan_type and artist parameters
   - Logs when thread is started
   - Error handling for missing artist

2. **_run_artist_scan_pipeline()** (`app.py` line 2510):
   - Writes to `/tmp/artist_scan_debug.log` immediately
   - Logs at function entry
   - Logs artist_id lookup steps
   - Logs database query results
   - Full exception traceback on errors

## Testing Instructions

### Test Artist Scan Button
1. Navigate to artist page
2. Click "Scan Artist" button
3. Check logs:
   - `/config/sptnr.log` - Should see "scan_start called: scan_type=artist"
   - `/tmp/artist_scan_debug.log` - Should see function call timestamp
   - `/config/unified_scan.log` - Should see artist scan progress from navidrome_import and popularity
4. Check Recent Scans view on dashboard - Should show scan history

### Test Force Rescan
1. Run a regular scan for an artist
2. Run scan again (force=True is hardcoded in artist scan)
3. Verify that albums are re-scanned (not skipped)
4. Check logs for: "⚠ Force rescan mode enabled"

## Expected Log Flow

When artist scan works correctly, you should see:

```
# In /config/sptnr.log
[INFO] scan_start called: scan_type=artist, artist=<name>
[INFO] Starting artist scan thread for: <name>
[INFO] 🎤 Artist scan pipeline started for: <name>
[INFO] Looking up artist_id for '<name>' in database...
[INFO] Database lookup result: artist_id=<id>
[INFO] Step 1/2: Navidrome import for artist '<name>'
[INFO] Step 2/2: Running popularity scan for artist '<name>'
[INFO] ✅ Scan complete for artist '<name>'

# In /config/unified_scan.log
navidrome_import_🎤 [Navidrome] Starting import for artist: <name>
navidrome_import_   💿 Found N albums for <name>
navidrome_import_      💿 [Album 1/N] <album>
...
popularity_🔍 Filtering: artist='<name>'
popularity_Currently Scanning Artist: <name>
popularity_Scanning "<name> - <album>" for Popularity
popularity_Album Scanned: "<name> - <album>". Popularity Applied to N tracks.
popularity_Detecting singles for "<name> - <album>"
popularity_Singles Detection Complete: N single(s) detected
popularity_Calculating star ratings for "<name> - <album>"
...
```

## API Changes

### popularity_scan()
**Before**: `popularity_scan(verbose, resume_from, artist_filter, album_filter, skip_header)`
**After**: `popularity_scan(verbose, resume_from, artist_filter, album_filter, skip_header, force)`

New parameter:
- `force` (bool): Force re-scan of albums even if already scanned

## Environment Variables

No changes to environment variables. Existing variables still work:
- `SPTNR_FORCE_RESCAN=1` - Force rescan globally
- New: Can pass `force=True` parameter instead

## Backward Compatibility

✅ All changes are backward compatible:
- New `force` parameter has default value `False`
- Existing code calling `popularity_scan()` without `force` will work unchanged
- Environment variable `SPTNR_FORCE_RESCAN` still works as before
- CLI without `--force` flag works as before

## Key Differences from Previous Implementation

### Previous (Incorrect):
```python
# Artist scan called:
1. scan_artist_to_db()          # navidrome_import.py
2. unified_scan_pipeline()      # unified_scan.py
   └─> popularity_scan()        # Called internally
   └─> rate_artist()            # Called per album
```

### New (Correct):
```python
# Artist scan calls:
1. scan_artist_to_db()          # navidrome_import.py  
2. popularity_scan()            # popularity.py (includes singles + rating)
```

The new implementation is simpler, more direct, and ensures proper logging since both modules use `log_unified()` to write to unified_scan.log.
