# Fix for Excluded Track Star Ratings

## Problem Statement

Tracks with parentheses at the end of an album (like "Track (Live in Wacken 2022)") were being correctly excluded from statistics calculation but were still participating in confidence-based star rating upgrades. This caused them to receive inflated star ratings.

## Root Cause

The issue was in the star rating assignment logic in `popularity.py`:

1. **Statistics Calculation (CORRECT)**: Parenthesis tracks at the end of albums were excluded from:
   - Mean calculation
   - Standard deviation calculation
   - Z-score calculation
   - Top 50% z-score calculation

2. **Star Rating Assignment (BUG)**: When looping through ALL tracks to assign star ratings:
   - Each track's z-score was calculated using the mean/stddev from non-excluded tracks
   - This made excluded tracks' z-scores artificially high relative to the filtered statistics
   - Excluded tracks could then qualify for confidence-based upgrades (HIGH or MEDIUM confidence)
   - If they had metadata sources, they would get 5★ even though they should only get baseline stars

## Example of the Bug

Consider an album with:
- 11 regular tracks (popularity: 75, 70, 68, 66, 64, 62, 60, 58, 56, 54, 52)
- 3 bonus tracks with parentheses (popularity: 70, 50, 40)

**Statistics calculated from regular tracks only:**
- Mean: 62.27
- Stddev: 7.13
- Medium confidence threshold: 0.59 (z-score)

**Bug behavior:**
- Bonus track "Track (Live)" with popularity 70:
  - Z-score = (70 - 62.27) / 7.13 = 1.08
  - This exceeds the medium confidence threshold (0.59)
  - If it has metadata, it gets upgraded to 5★
  - **This is wrong!** It should only get baseline stars because it was excluded from statistics

## The Fix

Modified the star rating loop in `popularity.py` to check if a track is excluded:

```python
# Check if this track was excluded from statistics
# Excluded tracks should not participate in confidence-based star rating upgrades
is_excluded_track = i in excluded_indices

# Skip confidence-based upgrades for excluded tracks (e.g., bonus tracks with parentheses)
# These tracks were excluded from statistics calculation, so their z-scores are not meaningful
if not is_excluded_track:
    # High Confidence (auto 5★): popularity >= mean + 6
    if popularity_score >= high_conf_threshold:
        stars = 5
    
    # Medium Confidence (requires metadata): zscore >= mean_top50_zscore - 0.3 + metadata
    elif track_zscore >= medium_conf_zscore_threshold:
        # ... metadata checks ...
    
    # Legacy logic for backwards compatibility
    # ... single confidence checks ...
else:
    # Track is excluded from statistics - log if verbose
    if verbose:
        log_unified(f"   ⏭️ Skipped confidence checks for excluded track: {title} (baseline stars={stars})")
```

## Test Coverage

Created comprehensive test in `test_excluded_track_star_ratings.py` that:

1. Creates an album with regular tracks and bonus tracks with parentheses
2. Verifies that excluded tracks are correctly identified
3. Simulates star rating assignment
4. Confirms that excluded tracks:
   - Get baseline band-based star ratings
   - Do NOT get confidence-based upgrades
   - Do NOT get 5★ even if their z-scores would qualify them

**Example test output:**
```
Track 12: Bonus Track (Live in Wacken 2022)  pop= 70.0 zscore=  1.08 → 2★ (EXCLUDED - baseline only)
  ⚠️ NOTE: This track has zscore=1.08 which WOULD exceed
  medium_conf_zscore_threshold=0.59
  BUT it's excluded, so it correctly gets baseline stars only!
```

## Impact

### Before Fix
- Excluded tracks could get inflated star ratings (up to 5★) if their z-scores exceeded confidence thresholds
- This was incorrect because their z-scores were calculated against a mean that excluded them

### After Fix
- Excluded tracks only get baseline band-based star ratings (1-4★ based on position in album)
- They do NOT participate in confidence-based upgrades
- This ensures fair star ratings for all tracks

## Verification

All tests pass:
- ✅ `test_mean_and_top50_filtering.py` - Verifies parenthesis tracks are excluded from statistics
- ✅ `test_parenthesis_filter.py` - Verifies exclusion logic works correctly
- ✅ `test_excluded_track_star_ratings.py` - Verifies excluded tracks don't get inflated ratings

Code review found no issues, and security scan found no vulnerabilities.

## Files Modified

- `popularity.py` - Added `is_excluded_track` check in star rating loop
- `test_excluded_track_star_ratings.py` - New comprehensive test for the fix

## Backward Compatibility

The changes are fully backward compatible:
- All existing tests pass
- The fix only affects excluded tracks (those with parentheses at the end of albums)
- Non-excluded tracks continue to get star ratings as before
- The exclusion criteria (consecutive tracks with parentheses at the end) remain unchanged
