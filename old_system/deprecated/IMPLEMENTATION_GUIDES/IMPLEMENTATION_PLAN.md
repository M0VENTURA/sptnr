# Implementation Summary: New Single Detection Logic

## Overview

This implementation creates a new single detection system that exactly matches the pseudocode in the problem statement. The logic is simpler and more explicit than the current implementation.

## Files Created

### 1. `/home/runner/work/sptnr/sptnr/single_detection_new.py`
Contains the complete new single detection pipeline:
- `exclude_trailing_parenthesis_tracks()` - Filters bonus tracks from stats
- `compute_z_threshold()` - Calculates z-score threshold  
- `compute_artist_mean_popularity()` - Artist-level stats
- `detect_single_new()` - Main detection function with 6-stage pipeline
- `calculate_star_rating()` - Simple star rating: HIGH=5★, MEDIUM with 2+ sources=5★, else baseline

### 2. `/home/runner/work/sptnr/sptnr/test_new_single_detection.py`
Unit tests that verify the new logic works correctly.

## Key Differences from Current Implementation

| Aspect | Current (`single_detection_enhanced.py`) | New (`single_detection_new.py`) |
|--------|------------------------------------------|----------------------------------|
| **Confidence Classification** | Complex z-score thresholds (album_z >= 1.0 AND artist_z >= 0.5) | Simple source counting (any high source = HIGH) |
| **Source Tracking** | Implicit tracking in determine_final_status() | Explicit high_conf_sources and med_conf_sources sets |
| **Star Rating** | Complex metadata confirmation, underperformance downgrade | Simple: HIGH=5★, MEDIUM with 2+ sources=5★, else baseline |
| **Artist Sanity Filter** | Not implemented | Implemented: popularity < artist_mean AND no metadata = skip |
| **Live Track Handling** | Basic filtering | Requires metadata for exact live version |

## Integration Steps

To integrate this new logic into the existing codebase:

### Option A: Replace `detect_single_enhanced()` (Recommended)

1. Backup current `single_detection_enhanced.py`
2. Update the `detect_single_enhanced()` function to call `detect_single_new()` internally
3. Map the return values to match the expected format
4. Update star rating calculation in `popularity.py` lines 1920-2040

### Option B: Add as Alternative Algorithm

1. Add `use_new_detection` parameter to `detect_single_for_track()` in `popularity.py`
2. When `use_new_detection=True`, call `detect_single_new()` instead of `detect_single_enhanced()`
3. Update star rating calculation to use `calculate_star_rating()` when new detection is used

### Option C: Gradual Migration

1. Add the new logic alongside existing logic
2. Run both algorithms and compare results  
3. Log differences for analysis
4. Switch to new algorithm once validated

## Recommended Integration (Minimal Changes)

The minimal change approach would be to update `popularity.py` to:

1. Import the new functions:
   ```python
   from single_detection_new import detect_single_new, calculate_star_rating as calc_stars_new
   ```

2. In the singles detection loop (around line 1773), replace the call to `detect_single_for_track()` with:
   ```python
   # Prepare track dict for new detection
   track_dict = {
       'id': track_id,
       'title': title,
       'artist': artist,
       'album': album,
       'popularity_score': track_popularity,
       'duration': track_duration,
       'isrc': track_isrc
   }
   
   # Prepare album tracks list
   album_tracks_list = [
       {
           'id': t['id'],
           'title': t['title'],
           'popularity_score': cursor.execute(
               "SELECT popularity_score FROM tracks WHERE id = ?", 
               (t['id'],)
           ).fetchone()[0] or 0
       }
       for t in album_tracks
   ]
   
   # Run new detection
   detection_result = detect_single_new(
       conn=conn,
       track=track_dict,
       album_tracks=album_tracks_list,
       artist_name=artist,
       discogs_client=discogs_client,
       musicbrainz_client=musicbrainz_client,
       spotify_results=spotify_results_cache.get(title) if spotify_results_cache else None,
       verbose=verbose
   )
   
   single_sources = detection_result['single_sources']
   single_confidence = detection_result['single_confidence']
   is_single = detection_result['is_single']
   ```

3. In the star rating calculation loop (around line 1922), replace the complex logic with:
   ```python
   # Calculate star rating using new simple logic
   stars = calc_stars_new(
       track={'id': track_id, 'popularity_score': popularity_score},
       album_tracks=album_tracks_with_scores,
       single_confidence=single_confidence,
       single_sources=single_sources
   )
   ```

## Testing

Run the unit tests to verify the new logic:
```bash
cd /home/runner/work/sptnr/sptnr
python test_new_single_detection.py
```

Expected output:
```
✓ test_exclude_trailing_parenthesis passed
✓ test_star_rating_logic passed
All tests passed! ✓
```

## Rollback Plan

If issues arise:
1. The new files (`single_detection_new.py`, `test_new_single_detection.py`) can be safely deleted
2. The old `single_detection_enhanced.py` logic remains intact
3. Restore `popularity.py` from git history if changes were made

## Next Steps

1. Review and approve this implementation
2. Choose an integration approach
3. Make the minimal necessary changes to integrate
4. Run existing test suite to ensure no regressions
5. Test with real data
6. Monitor for any issues
7. If successful, clean up old code

## Questions?

- Should we maintain backward compatibility with the old detection algorithm?
- Should we add a feature flag to switch between old/new logic?
- Do we need to migrate existing database records to the new format?
