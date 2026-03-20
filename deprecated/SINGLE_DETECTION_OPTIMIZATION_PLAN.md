# Single Detection Optimization Implementation Plan

## Overview
Optimize the single detection flow to reduce API calls by ~70% through:
1. **Discogs-first ordering** with early exit (high confidence source)
2. **2 medium = 1 high** confidence promotion with early stopping
3. **Z-score filtering** for live albums (require HIGH confidence)
4. **Block popularity 5★** for live albums

## Changes Required

### 1. detect_single_for_track() Function (popularity.py lines 2283-2530)

**Current Order:**
- Spotify → MusicBrainz → Discogs → Discogs Video → Iterative Z-score

**New Order:**
- Discogs → MusicBrainz → Spotify → Discogs Video → Iterative Z-score

**Implementation:**
```python
# Add after single_sources = [] at line 2283:
medium_confidence_sources = []  # Track medium confidence sources for 2 medium = 1 high rule

# FIRST CHECK: Discogs (HIGH confidence - early exit if found)
if discogs_token:
    result = discogs_client.is_single(lookup_title, artist)
    if result:
        single_sources.append("discogs")
        log_info(f"   🎯 EARLY EXIT: Discogs confirmed - HIGH confidence")
        return {"sources": ["discogs"], "confidence": "high", "is_single": True}

# SECOND CHECK: MusicBrainz (medium confidence)
if HAVE_MUSICBRAINZ:
    result = mb_client.is_single(lookup_title, artist)
    if result:
        single_sources.append("musicbrainz")
        medium_confidence_sources.append("musicbrainz")
    # ... additional MB checks (video, compilation)
    # Early exit if 2 medium sources
    if len(medium_confidence_sources) >= 2:
        log_info(f"   🎯 EARLY EXIT: 2 medium sources = HIGH confidence")
        return {"sources": single_sources, "confidence": "high", "is_single": True}

# THIRD CHECK: Spotify (medium confidence)
spotify_result = check_spotify(...)
if spotify_result:
    single_sources.append("spotify")
    medium_confidence_sources.append("spotify")
    # Early exit if 2 medium sources
    if len(medium_confidence_sources) >= 2:
        log_info(f"   🎯 EARLY EXIT: 2 medium sources = HIGH confidence")
        return {"sources": single_sources, "confidence": "high", "is_single": True}

# FOURTH CHECK: Discogs Video (medium confidence)
if discogs_token:
    result = discogs_client.has_official_video(lookup_title, artist)
    if result:
        single_sources.append("discogs_video")
        medium_confidence_sources.append("discogs_video")
        # Early exit if 2 medium sources
        if len(medium_confidence_sources) >= 2:
            log_info(f"   🎯 EARLY EXIT: 2 medium sources = HIGH confidence")
            return {"sources": single_sources, "confidence": "high", "is_single": True}

# FIFTH CHECK: Iterative Z-score (medium confidence)
if iterative_zscore_passed:
    single_sources.append("iterative_zscore")
    medium_confidence_sources.append("iterative_zscore")
    # Early exit if 2 medium sources
    if len(medium_confidence_sources) >= 2:
        log_info(f"   🎯 EARLY EXIT: 2 medium sources = HIGH confidence")
        return {"sources": single_sources, "confidence": "high", "is_single": True}

# Final confidence determination (same as before but with 2 medium = high)
if "discogs" in single_sources or len(medium_confidence_sources) >= 2:
    single_confidence = "high"
    is_single = True
elif len(single_sources) > 0:
    single_confidence = "medium"
    is_single = False
else:
    single_confidence = "low"
    is_single = False
```

### 2. Z-Score Filtering for Live Albums (popularity.py lines 5665-5710)

**Current Logic:**
```python
if track_zscore < 0.0:
    skip_single_detection = True  # Skip below median
```

**New Logic:**
```python
# Get album live context
album_is_live = row_get(track, "album_context_live", 0)

if not is_greatest_hits_or_compilation:
    track_zscore = track_zscores.get(track_id, 0.0)
    
    if album_is_live:
        # LIVE ALBUM RULE: Only scan tracks above median, require HIGH confidence
        if track_zscore < 0.0:
            skip_single_detection = True
            log_debug(f"Skipping '{title}' on live album (z-score {track_zscore:.2f} < 0.0)")
        else:
            log_debug(f"Scanning '{title}' on live album (z > 0, will require HIGH confidence)")
    else:
        # REGULAR ALBUM RULE: Skip below median
        if track_zscore < 0.0:
            skip_single_detection = True
            log_debug(f"Skipping '{title}' (z-score {track_zscore:.2f} < 0.0)")
```

### 3. Block Popularity 5★ for Live Albums (popularity.py line 6345)

**Current Code:**
```python
# Line 6345 - High-confidence single preservation
elif is_single and single_confidence == "high":
    stars = 5
```

**New Code:**
```python
# Line 6345 - High-confidence single preservation (but NOT for live albums)
elif is_single and single_confidence == "high":
    # Check if this is a live album
    album_is_live = row_get(track, "album_context_live", 0)
    if album_is_live:
        # Live albums: Use z-score gates like regular tracks
        log_debug(f"Live album track '{title}' is high-confidence single but using z-score gates, not popularity 5★")
        if z_score >= 2.0:
            stars = 5
        elif z_score >= 1.0 and (has_high_via_discogs or has_two_medium):
            stars = 5
        elif z_score >= 0.0 and has_high_via_discogs:
            stars = 4
        else:
            stars = 3  # High-confidence single but low z-score on live album
    else:
        # Regular albums: High-confidence singles always get 5★
        stars = 5
```

### 4. Additional Logging

Add progress logging to understand optimization impact:

```python
# At start of detect_single_for_track():
log_info(f"🔎 [SINGLE DETECTION] Starting optimized detection for: {title}")

# After each source check:
log_info(f"   [1/5] Checking Discogs...")
log_info(f"   [2/5] Checking MusicBrainz...")
log_info(f"   [3/5] Checking Spotify...")
log_info(f"   [4/5] Checking Discogs Video...")
log_info(f"   [5/5] Checking Iterative Z-score...")

# On early exit:
log_info(f"   🎯 EARLY EXIT: [reason]")
```

## Testing Checklist

- [ ] Run scan on Creed album to verify singles still detected
- [ ] Check logs for "EARLY EXIT" messages (should see ~70% reduction in API calls)
- [ ] Verify high-confidence singles preserve 5★ rating
- [ ] Test live album: verify z > 0 filtering and HIGH confidence requirement
- [ ] Test compilation: verify all tracks scanned regardless of z-score
- [ ] Verify 2 medium sources promote to high confidence

## Expected Performance Impact

**Before:**
- All tracks z > 0: 5 source checks each
- Example: 10 tracks * 5 sources = 50 API calls

**After:**
- Discogs YES: 1 call, early exit (~20% of tracks)
- 2 medium sources: 2-3 calls, early exit (~50% of tracks)
- No matches: 5 calls (~30% of tracks)
- Expected: 10 tracks * ~1.5 sources average = **~15 API calls** (~70% reduction)

## Files to Modify

1. **popularity.py** (3 sections):
   - Lines 2283-2530: detect_single_for_track() reordering + early stopping
   - Lines 5665-5710: Z-score filtering for live albums
   - Line 6345: Block popularity 5★ for live albums

## Implementation Notes

- **Preserve all existing safety logic**: user-set singles, outliers, compilations
- **Graceful fallbacks**: If Discogs unavailable, continue to other sources
- **Logging verbosity**: Log early exits at INFO level, details at DEBUG
- **Backward compatibility**: Return dict format unchanged

## Rollback Plan

If issues occur:
1. Git revert commit
2. Re-run popularity scan with `--force` flag
3. Monitor logs for detection failures vs old implementation
