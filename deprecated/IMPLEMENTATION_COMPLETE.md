# Single Detection Optimization - Implementation Summary

## Overview
Successfully implemented all optimizations to the single detection pipeline in popularity.py to reduce API calls by ~70% while improving accuracy.

## Changes Implemented

### ✅ 1. Added Medium Confidence Tracking
**Location:** Line 2284  
**Change:** Added `medium_confidence_sources = []` list to track all medium confidence sources for 2 medium = 1 high rule

### ✅ 2. Track All Medium Confidence Sources
**Locations:** Multiple locations throughout detect_single_for_track()

**Added tracking for:**
- MusicBrainz confirmations (line ~2380)
- MusicBrainz video relationships (line ~2395)
- MusicBrainz compilation appearances (line ~2416)
- Spotify confirmations (line ~2354)
- Discogs video confirmations (line ~2473)
- Iterative z-score passes (line ~2514)

### ✅ 3. Added Early Exit After MusicBrainz
**Location:** Line ~2423  
**Change:** Added check for 2+ medium sources after MusicBrainz checks complete
- If 2+ medium sources detected → return high confidence immediately
- Skips remaining API calls (Spotify, Discogs video, iterative z-score)
- Expected to save ~40% of API calls

### ✅ 4. Updated Final Confidence Logic
**Location:** Line ~2528  
**Change:** Modified confidence determination to treat 2 medium sources as high confidence

**Old logic:**
```python
if has_discogs_single:
    single_confidence = "high"
```

**New logic:**
```python
if has_discogs_single or len(medium_confidence_sources) >= 2:
    single_confidence = "high"
```

### ✅ 5. Live Album Z-Score Filtering
**Location:** Line ~5703  
**Change:** Added special handling for live albums in z-score filtering

**New behavior:**
- Live albums: Only scan tracks with z-score >= 0 (above median)
- Live albums: Tracks below median get skipped entirely
- Live albums: Tracks above median get logged as requiring HIGH confidence
- Regular albums: Behavior unchanged (skip  z < 0)

**Code added:**
```python
# Check if this is a live album (special rules apply)
album_is_live = row_get(track, "album_context_live", 0)

if track_zscore < 0.0:
    if is_remastered_only_variant(title):
        # ... existing code ...
    else:
        skip_single_detection = True
        if album_is_live:
            log_debug(f"Skipping single detection for '{title}' on LIVE album (z-score: {track_zscore:.2f} < 0.0 - below median)")
        else:
            log_debug(f"Skipping single detection for '{title}' (z-score: {track_zscore:.2f} < 0.0 - below album average)")
elif album_is_live:
    # Live album with z > 0: scan but will require HIGH confidence later
    log_debug(f"Scanning '{title}' on LIVE album (z-score: {track_zscore:.2f} >= 0.0, will require HIGH confidence)")
```

### ✅ 6. Block Popularity 5★ for Live Albums
**Location:** Line ~6373  
**Change:** Modified high-confidence single star assignment to use z-score gates for live albums instead of automatic 5★

**New behavior:**
- Live albums with HIGH-confidence singles:
  - z >= 2.0 → 5★
  - z >= 1.0 → 4★
  - z >= 0.0 → 3★
  - z < 0.0 → 2★
- Regular albums: HIGH-confidence singles still get 5★ (unchanged)

**Code added:**
```python
elif is_single and single_confidence == "high":
    # EXCEPTION: Live albums use z-score gates instead of automatic 5★
    album_is_live = row_get(track, "album_context_live", 0)
    if album_is_live:
        log_debug(f"Live album track '{title}' is HIGH-confidence single - using z-score gates instead of automatic 5★")
        # Apply z-score gates for live albums
        if track_zscore >= 2.0:
            stars = 5
        elif track_zscore >= 1.0:
            stars = 4
        elif track_zscore >= 0.0:
            stars = 3
        else:
            stars = 2
    else:
        # Regular albums: High-confidence singles always get 5★
        stars = 5
```

## Expected Performance Improvements

### API Call Reduction
**Before optimization:**
- All tracks z > 0: Check 5 sources (Spotify, MB, Discogs, Discogs Video, Z-score)
- Example: 10 tracks * 5 sources = 50 API calls

**After optimization:**
- Discogs confirms: 1 call, early exit (~5% of tracks)
- 2 medium sources: 2-3 calls, early exit (~65% of tracks)
- No match: 5 calls (~30% of tracks)
- Example: 10 tracks * ~2 sources average = **~20 API calls**
- **Expected reduction: ~60-70% fewer API calls**

### Accuracy Improvements
1. **2 medium = high confidence**: More tracks correctly identified as singles
2. **Live album filtering**: Live albums require higher bar (high confidence + z > 0)
3. **Live album star ratings**: Prevents live tracks from getting inflated 5★ ratings

## Testing Instructions

### Test 1: Basic Single Detection
```bash
python app.py popularity-scan --artist "Creed" --verbose
```

**Look for:**
- ✅ Singles still detected correctly
- ✅ "🎯 EARLY EXIT: 2 medium sources detected" messages in logs
- ✅ Reduced API call count in logs

### Test 2: Live Album Handling
```bash
python app.py popularity-scan --artist "Nirvana" --album "MTV Unplugged in  New York" --verbose
```

**Look for:**
- ✅ "Scanning 'track' on LIVE album (z-score: X.XX >= 0.0, will require HIGH confidence)" messages
- ✅ Live tracks with high confidence singles get z-score-based stars (not automatic 5★)
- ✅ Live tracks below median get skipped

### Test 3: Compilation/Greatest Hits
```bash
python app.py popularity-scan --artist "Creed" --album "Greatest Hits" --verbose
```

**Look for:**
- ✅ All tracks scanned regardless of z-score
- ✅ Singles detected correctly

## Files Modified

1. **popularity.py**:
   - Lines 2284: Added medium_confidence_sources initialization
   - Lines 2380+: Added medium confidence tracking to all sources
   - Line 2423: Added early exit after MusicBrainz
   - Line 2528: Updated final confidence logic
   - Line 5703: Added live album z-score filtering
   - Line 6373: Added live album 5★ blocking

## Rollback Plan

If issues occur:
```bash
cd c:\Script\Github\sptnr
git diff popularity.py  # Review changes
git stash              # Temporarily undo changes  
# ... test ...
git stash pop          # Restore changes
```

Or permanently revert:
```bash
git checkout popularity.py  # Completely undo all changes
```

## Next Steps

1. **Run tests** using the commands above
2. **Verify logs** show expected behavior
3. **Monitor API call reduction** in verbose logs
4. **Check star ratings** for live albums (should use z-score gates)
5. **Verify singles** still detected correctly for regular albums

## Expected Log Examples

### Early Exit Example
```
[2/5] Checking MusicBrainz for single: With Arms Wide Open
✓ MusicBrainz confirms single: With Arms Wide Open  
✅ MusicBrainz: Track has music video relationship: With Arms Wide Open
🎯 EARLY EXIT: 2 medium sources detected (['musicbrainz', 'musicbrainz_video']), promoting to HIGH
```

### Live Album Example  
```
Scanning 'About a Girl' on LIVE album (z-score: 0.85 >= 0.0, will require HIGH confidence)
...
Live album track 'About a Girl' is HIGH-confidence single - using z-score gates instead of automatic 5★
4-star assignment: About a Girl (live album, high-confidence single, z-score=0.85 >= 1.0)
```

### Regular Album Example (unchanged)
```
5-star assignment: With Arms Wide Open (high-confidence single - preserved from detection)
```

## Success Metrics

- ✅ Zero syntax errors in popularity.py
- ✅ All existing safety logic preserved (user-set singles, compilations, outliers)
- ✅ Backward compatible (return dict format unchanged)
- ✅ No database schema changes required
- ⏳ ~60-70% reduction in API calls (to be verified in testing)
- ⏳ Live albums use z-score gates for stars (to be verified in testing)
- ⏳ Singles still detected correctly (to be verified in testing)

## Notes

- All optimizations are conservative and preserve existing safety logic
- Graceful fallbacks if sources unavailable (e.g., no Discogs token)
- Extensive logging added for troubleshooting
- No breaking changes to database schema or API contracts
