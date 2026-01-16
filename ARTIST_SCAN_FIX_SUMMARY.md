# Artist Scan and Force Rescan Fixes - Final Summary

## All Issues Resolved ✅

### Issue 1: Artist Scan Not Running/Logging ✅
**Problem**: Clicking "Scan Artist" button showed no logs in unified_scan.log or Recent Scans view.

**Root Cause**: The artist scan was calling `unified_scan_pipeline()` instead of calling `popularity.py` directly.

**Solution**:
- Changed `_run_artist_scan_pipeline()` to call `popularity_scan()` directly
- New flow: `navidrome_import.py` → `popularity.py`
- Both modules use `log_unified()` which writes to unified_scan.log
- This ensures proper logging and Recent Scans tracking

**Files Changed**: `app.py`

### Issue 2: Force Rescan Not Working ✅
**Problem**: Setting `force=True` and `album_skip_days=0` still skipped already-scanned albums.

**Root Cause**: `popularity_scan()` only checked `SPTNR_FORCE_RESCAN` env var, ignored `force` parameter.

**Solution**:
- Added `force` parameter to `popularity_scan()` signature
- Updated logic: `if FORCE_RESCAN or force:`
- Pass `force` from `unified_scan_pipeline()` to `popularity_scan()`
- Added CLI `--force` flag support

**Files Changed**: `popularity.py`, `unified_scan.py`

### Issue 3: Redundant Code ✅
**Problem**: 144 lines of dead code in start.py.

**Solution**: Removed unused functions:
1. Nested `scan_artist_to_db()` inside `scan_library_to_db()` (121 lines) - never called
2. `rate_artist_single_detection()` (23 lines) - no longer needed

**Files Changed**: `start.py`

### Issue 4: Essential Playlist Cleanup ✅
**Problem**: Playlists remained when artist no longer met requirements.

**Solution**:
- Added `_delete_nsp_file()` call in `popularity.py` when requirements not met
- Added cleanup in `unified_scan.py` when no qualifying tracks
- Requirements: 10+ five-star tracks OR 100+ total tracks
- Logs deletion with reason

**Files Changed**: `popularity.py`, `unified_scan.py`

## Code Overlap Analysis

### Major Overlaps Identified

#### 1. Duplicate Helper Functions
These functions exist in BOTH unified_scan.py and popularity.py:
- `log_unified()` - Log to unified_scan.log
- `log_verbose()` - Conditional verbose logging
- `get_db_connection()` - Get SQLite connection with WAL

**Recommendation**: Create shared utilities module

#### 2. Singles Detection - CRITICAL OVERLAP ⚠️
**Problem**: In unified_scan flow, singles detection happens TWICE:

```
unified_scan_pipeline()
  └─> Phase 1: popularity_scan()
        └─> Singles detection (Spotify, MusicBrainz, Discogs)
  └─> Phase 2: rate_artist()
        └─> Singles detection AGAIN (may overwrite Phase 1 results)
```

**Good News**: Artist scan now bypasses this issue!
- Artist scan: `navidrome_import` → `popularity_scan` ✅
- No duplicate singles detection for artist scans

**Still An Issue**: Full unified scan has duplicate detection
- Wastes API calls
- Inconsistent results possible

**Recommendation**: Remove `rate_artist()` call from unified_scan Phase 2

#### 3. Star Rating Calculation - DUPLICATE
Both `popularity_scan()` and `rate_artist()` calculate star ratings.

#### 4. Essential Playlist Creation - DUPLICATE  
Both `unified_scan.py` and `popularity.py` create Essential playlists.

## Testing Checklist

### Force Rescan
- [x] Albums re-scanned when `force=True`
- [x] Logs show "⚠ Force rescan mode enabled"
- [x] Already-scanned albums not skipped

### Artist Scan
- [x] Logs appear in `/config/unified_scan.log`
- [x] Scan shows in Recent Scans view on dashboard
- [x] Flow: navidrome_import → popularity_scan (no unified_scan)
- [x] Singles detection runs once (in popularity_scan)
- [x] Star ratings calculated
- [x] Essential playlists created when requirements met

### Playlist Cleanup
- [x] Playlist deleted when < 10 five-star AND < 100 total tracks
- [x] Deletion logged with reason
- [x] File removed from `/music/Playlists/` folder

### Code Quality
- [x] 144 lines of dead code removed
- [x] No unused imports
- [x] Debugging logs added for troubleshooting

## Expected Log Flow

When artist scan works correctly:

```bash
# In /config/sptnr.log
[INFO] scan_start called: scan_type=artist, artist=Bagster
[INFO] Starting artist scan thread for: Bagster
[INFO] 🎤 Artist scan pipeline started for: Bagster
[INFO] Step 1/2: Navidrome import for artist 'Bagster'
[INFO] Step 2/2: Running popularity scan for artist 'Bagster'
[INFO] ✅ Scan complete for artist 'Bagster'

# In /config/unified_scan.log
navidrome_import_🎤 [Navidrome] Starting import for artist: Bagster
navidrome_import_   💿 Found 3 albums for Bagster
navidrome_import_      💿 [Album 1/3] Wrecking Your Life
...
popularity_🔍 Filtering: artist='Bagster'
popularity_⚠ Force rescan mode enabled - will rescan all albums
popularity_Currently Scanning Artist: Bagster
popularity_Scanning "Bagster - Wrecking Your Life" for Popularity
popularity_✓ Track scanned successfully: "Song Name" (score: 75.0)
popularity_Album Scanned: "Bagster - Wrecking Your Life". Popularity Applied to 10 tracks.
popularity_Detecting singles for "Bagster - Wrecking Your Life"
popularity_   ✓ Single detected: "Hit Song" (high confidence, sources: spotify, musicbrainz)
popularity_Singles Detection Complete: 2 single(s) detected
popularity_Calculating star ratings for "Bagster - Wrecking Your Life"
popularity_   ★★★★★ (5/5) - Hit Song (Single) (popularity: 85.0)
```

## Files Changed Summary

| File | Lines Changed | Purpose |
|------|--------------|---------|
| app.py | +20, -8 | Fixed artist scan flow, added debugging |
| popularity.py | +7, -2 | Added force parameter, playlist cleanup |
| unified_scan.py | +8, -1 | Pass force parameter, playlist cleanup |
| start.py | -144 | Removed redundant code |
| ARTIST_SCAN_FIX_SUMMARY.md | +172 | Documentation |

**Total**: +207 additions, -155 deletions

## API Changes

### popularity_scan()
**Before**:
```python
popularity_scan(verbose, resume_from, artist_filter, album_filter, skip_header)
```

**After**:
```python
popularity_scan(verbose, resume_from, artist_filter, album_filter, skip_header, force)
```

**New parameter**: `force` (bool, default=False) - Force re-scan of already-scanned albums

## Backward Compatibility

✅ **All changes are backward compatible**:
- New `force` parameter has default value `False`
- Existing code calling `popularity_scan()` without `force` works unchanged
- Environment variable `SPTNR_FORCE_RESCAN` still works
- Artist scan button automatically uses `force=True`

## Future Improvements (Optional)

Based on overlap analysis:

1. **Create shared utilities module** (`scan_utils.py`)
   - Move `log_unified()`, `log_verbose()`, `get_db_connection()`
   - Reduce code duplication

2. **Remove duplicate singles detection from unified_scan**
   - Let `popularity_scan()` handle all detection
   - Remove `rate_artist()` call from Phase 2
   - Save API calls and processing time

3. **Consolidate playlist creation**
   - Use only `create_or_update_playlist_for_artist()` from popularity.py
   - Remove duplicate logic from unified_scan.py

4. **Remove debug logging before production**
   - Remove `/tmp/artist_scan_debug.log` writing
   - Use proper exception handling (not bare `except:`)

## Conclusion

All reported issues have been fixed:
- ✅ Artist scan now runs and logs properly
- ✅ Force rescan parameter works correctly
- ✅ Dead code removed (144 lines)
- ✅ Essential playlists cleaned up when requirements not met
- ✅ Code overlaps documented for future optimization

The artist scan now has a clean, simple flow: `navidrome_import` → `popularity_scan`, which handles everything: popularity detection, singles detection, star rating, and playlist creation.
