# Artist Scan and Force Rescan Fixes - Summary

## Issues Addressed

### Issue 1: Force Rescan Not Working
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

### Issue 2: Artist Scan Button Not Logging
**Problem**: Clicking "Scan Artist" button showed no logs in unified_scan.log or Recent Scans view.

**Debugging Added**:
Multiple layers of logging to identify where the execution fails:

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

**Files Changed**:
- `app.py` - Added extensive logging throughout artist scan pipeline

## Testing Instructions

### Test Force Rescan
1. Run a regular scan for an artist
2. Run scan again with force=True
3. Verify that albums are re-scanned (not skipped)
4. Check logs for: "⚠ Force rescan mode enabled"

### Test Artist Scan Button
1. Navigate to artist page
2. Click "Scan Artist" button
3. Check logs:
   - `/config/sptnr.log` - Should see "scan_start called: scan_type=artist"
   - `/tmp/artist_scan_debug.log` - Should see function call timestamp
   - `/config/unified_scan.log` - Should see artist scan progress
4. Check Recent Scans view on dashboard - Should show scan history

## Expected Log Flow

When artist scan works correctly, you should see:

```
# In /config/sptnr.log
[INFO] scan_start called: scan_type=artist, artist=<name>
[INFO] Starting artist scan thread for: <name>
[INFO] 🎤 Artist scan pipeline started for: <name>
[INFO] Looking up artist_id for '<name>' in database...
[INFO] Database lookup result: artist_id=<id>
[INFO] Step 1/3: Navidrome import for artist '<name>'
[INFO] Step 2/3: Running unified scan (popularity + singles) for artist '<name>'
[INFO] ✅ Scan complete for artist '<name>'

# In /config/unified_scan.log
unified_scan_🟢 ==================== UNIFIED SCAN PIPELINE STARTED ====================
unified_scan_📊 Phase 1: Popularity Detection (Spotify, Last.fm, ListenBrainz)
unified_scan_🎤 [Artist 1/1] <name>
unified_scan_💿 [Album 1/N] <album>
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
