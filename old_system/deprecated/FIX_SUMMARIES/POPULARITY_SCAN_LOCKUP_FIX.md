# Popularity Scan Lockup Fix

## Problem

The popularity scan was locking up (appearing to hang) during Spotify popularity lookups when processing albums with many bonus/live tracks that have parenthetical suffixes (e.g., "Track (Live)", "Track (Remix)", etc.).

## Root Cause

The recent parenthesis filtering changes added logic to exclude tracks with parenthetical suffixes from **statistics calculations** (mean/stddev), but these tracks were still being processed during the **API lookup phase**. This meant:

1. For each track with keywords like "live", "remix", "acoustic", etc., the scan would:
   - Call Spotify API with timeout (up to 30 seconds per track)
   - Call Last.fm API with timeout (up to 30 seconds per track)
   - Process results even though they would be filtered out later

2. Albums with 10-20 live/bonus tracks could take 10-20 minutes just for API lookups, making the scan appear to hang

## Solution

Added an **early keyword filter** before Spotify and Last.fm API lookups to skip tracks that contain keywords indicating they are not album tracks:

```python
# Skip Spotify lookup for obvious non-album tracks (live, remix, etc.)
# This prevents the scan from hanging on albums with many bonus/live tracks
skip_spotify_lookup = any(k in title.lower() for k in IGNORE_SINGLE_KEYWORDS)
if skip_spotify_lookup:
    log_unified(f'⏭ Skipping Spotify lookup for: {title} (keyword filter: live/remix/etc.)')
```

The keyword list (`IGNORE_SINGLE_KEYWORDS`) includes:
- `intro`, `outro`, `jam` (intros/outros/jams)
- `live`, `unplugged` (live performances)
- `remix`, `edit`, `mix` (remixes and edits)
- `acoustic`, `orchestral` (alternate arrangements)
- `demo`, `instrumental`, `karaoke` (alternate versions)
- `remaster`, `remastered` (remasters)

## Impact

### Before Fix
- Albums with 13 live tracks: ~13 × 60 seconds = **13 minutes of API calls**
- Scan appeared to "lock up" during Spotify lookups
- Wasted API quota on tracks that won't be rated highly anyway

### After Fix
- Live/bonus tracks are skipped immediately
- Log shows: `⏭ Skipping Spotify lookup for: Track (Live) (keyword filter: live/remix/etc.)`
- Scan completes much faster
- API quota is preserved for tracks that matter

## Files Modified

- **popularity.py** (lines 1179-1183, 1280-1309)
  - Added `skip_spotify_lookup` check before Spotify API call
  - Added `skip_spotify_lookup` check before Last.fm API call
  - Added log message when tracks are skipped

## Comparison with start.py (January 2nd)

The user asked to compare scan logic between `start.py` from January 2nd and current `popularity.py`:

**start.py** is a legacy/utility file with:
- Helper functions for Spotify matching
- Genre normalization utilities
- Database connection helpers
- Old scan logic that's no longer used

**popularity.py** has better scan logic:
- ✓ Proper timeout handling with `_run_with_timeout()`
- ✓ Thread pool for API call timeouts
- ✓ Comprehensive exception handling
- ✓ Better logging with `log_unified()` and `log_verbose()`
- ✓ Batch database updates for performance
- ✓ Parenthesis filtering for accurate statistics
- ✓ **NEW: Early keyword filtering to prevent lockups**

The scan logic in `popularity.py` is much more robust and efficient than the old logic in `start.py`.

## Testing

All existing tests pass:
- ✓ Parenthesis filtering tests (`test_parenthesis_filter.py`)
- ✓ Syntax validation passes
- ✓ No infinite loops or hangs

## Security

No security vulnerabilities introduced - the change only adds an early return/skip condition.
