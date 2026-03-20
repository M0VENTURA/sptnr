# Fix: Unwarranted 5-Star Ratings for Average Tracks

**Date**: March 2, 2026  
**Issue**: Songs receiving 5★ ratings without meeting popularity thresholds  
**Root Cause**: Overly permissive `is_standout_track` flag logic  
**Status**: FIXED

## Problem

The star rating system was assigning 5 stars to ANY track marked as `is_standout_track`, without verifying the track actually justified a 5-star rating. The `is_standout_track` flag only means "above album average", not "5-star worthy".

### Example

A track with:
- Album z-score: +0.9 (slightly above average)
- Popularity score: 44.7 (low)
- is_standout_track: 1 (from prior scan)

Would automatically receive 5 stars, despite mediocre actual metrics.

## Root Cause Analysis

**Location**: [popularity.py](popularity.py#L5188-L5190) (lines 5188-5190)

**Problematic Code**:
```python
elif is_standout_track:
    stars = 5
    log_info(f"5-star assignment: {title} (standout track...)")
```

**Why It's Wrong**:

- `is_standout_track` is a boolean flag set during the standout detection phase
- Flag only indicates: track_z_score >= 0.8 AND in top standout cluster for album
- This is a LOW bar - many mediocre album tracks can exceed it
- Converting flag → 5 stars without popularity validation creates false positives

### Example Impact

For Papa Roach "The Connection":
- "Walking Dead": popularity=50.0, z-score=0, but if `is_standout_track=1` → 5★ ❌
- "Still Swingin'": popularity=18.8, but if `is_standout_track=1` → 5★ ❌

## Solution

**New Logic**: Standout tracks now require BOTH conditions to upgrade to 4+ stars:

```python
elif is_standout_track and track_zscore >= 1.5 and popularity_score >= 65:
    # Strong album standout with high absolute popularity
    stars = 4 if stars < 4 else stars  # Upgrade to at least 4 stars
```

**Requirements**:
1. **Standout flag**: `is_standout_track = 1` (above album average)
2. **Strong z-score**: `track_zscore >= 1.5` (top ~7% of album, not just above average)
3. **High popularity**: `popularity_score >= 65` (significant absolute score)

## Changes Made

### File: [popularity.py](popularity.py)

**Line 5188-5190**: Removed blanket condition
```python
# REMOVED:
# elif is_standout_track:
#     stars = 5
#     log_info(f"5-star assignment: {title}...")
```

**Line 5188-5193**: Added restrictive condition
```python
# NEW:
elif is_standout_track and track_zscore >= 1.5 and popularity_score >= 65:
    stars = 4 if stars < 4 else stars
    log_info(f"4+ star assignment: {title} (strong standout - zscore={track_zscore:.2f}, pop={popularity_score:.1f})")
    log_debug(f"Standout track with strong metrics - track_id: {track_id}, zscore: {track_zscore:.2f}, popularity: {popularity_score}")
```

## Impact

| Condition | Before | After |
| --- | --- | --- |
| `is_standout_track=1` only | 5★ ❌ | 3-4★ (base rating) ✓ |
| Standout + z≥1.5 + pop≥65 | 5★ | 4★ (conditional upgrade) ✓ |
| High-confidence single | 5★ | 5★ (unchanged) ✓ |
| Top 15% of artist | 5★ | 5★ (unchanged) ✓ |
| User-set single | 5★ | 5★ (unchanged) ✓ |

## Testing

After running next popularity scan, verify:

1. ✅ No unexpected 5-star tracks in mediocre albums
2. ✅ Genuinely popular standout tracks (z≥1.5, pop≥65) still get 4+ stars
3. ✅ Single detection still assigns 5 stars to high-confidence singles
4. ✅ Artist top 15% rule still assigns 5 stars appropriately
5. ✅ Logs show clear reasoning for each star assignment

## Next Steps

1. Run full popularity scan: `python popularity.py --all --verbose`
2. Monitor logs for "Standout track with" messages
3. Spot-check 4-star Standout tracks to verify metrics
4. Verify no unexpected 5-star tracks in underperforming albums

## Related Issues

- [Download Organization Fixes](./DOWNLOAD_ORGANIZATION_FIXES.md) - Prior reliability work
- [Single Detection Algorithm](./documentation/STAR_RATING_ALGORITHM.md) - Full rating system
- [Artist Identity System](./documentation/ARTIST_IDENTITY_IMPLEMENTATION_SUMMARY.md) - Context-aware ratings

## Code References

- **Main Logic**: [popularity.py lines 5080-5210](popularity.py#L5080-L5210) - Star rating decision tree
- **Standout Detection**: [popularity.py lines 4340-4400](popularity.py#L4340-L4400) - is_standout_track flag setting
- **Z-Score Calculation**: [popularity.py lines 4559-4561](popularity.py#L4559-L4561) - Track z-score computation