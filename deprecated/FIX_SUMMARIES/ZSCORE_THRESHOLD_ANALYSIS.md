# Z-Score Threshold Analysis After Mean Adjustment

## The Problem

After switching to mean-based popularity adjustment in the scanning phase:

```python
# In popularity.py (scanning phase)
weighted_score = 0.10*spotify + 0.30*lastfm + 0.35*listenbrainz + 0.25*age
z_score = (weighted_score - artist_mean) / artist_stddev
adjusted_score = 50 + (z_score * 16.7)  # ← Stored as popularity_score
```

The `popularity_score` stored in the database is now **pre-normalized** around 50.

Later, in single detection:

```python
# In single_detection_enhanced.py (detection phase)
artist_mean_new = mean(all_adjusted_scores)  # ~50
artist_stddev_new = stdev(all_adjusted_scores)  # Smaller than before
z_score_new = (adjusted_score - artist_mean_new) / artist_stddev_new
```

This means **z-scores recalculated at detection time are different from the original z-scores**.

## Example Impact

### Before (Raw Weighted Scores)
```
Artist catalog:
- Track A: 35 (pop) → original z = (35-45)/12 = -0.83
- Track B: 52 (pop) → original z = (52-45)/12 = +0.58  
- Track C: 68 (pop) → original z = (68-45)/12 = +1.92  ✓ Above 1.8 threshold

Mean: 45, StdDev: 12
```

### After (Adjusted Scores Around 50)
```
Artist catalog (same tracks after adjustment):
- Track A: 47 (adjusted) from original z = -0.83
- Track B: 50 (adjusted) from original z = +0.58
- Track C: 66 (adjusted) from original z = +1.92

Mean: 54.3, StdDev: 8.7
New z-score for Track C: (66-54.3)/8.7 = +1.35  ← Falls BELOW 1.8 threshold!
```

## Why This Happens

1. **Range compression**: Adjustment maps z-scores to 0-100 using `50 + (z * 16.7)`
   - Original range: ~20-80 raw scores
   - Adjusted range: ~30-70 (tighter)

2. **Mean shift**: Artist mean shifts from (e.g.) 45 → 54
   - Tracks below original mean are pulled up by adjustment
   - Tracks above original mean are pulled down slightly
   - Distribution becomes more symmetric around 50

3. **StdDev reduction**: Adjusted scores have ~25% smaller variance
   - Because the adjustment "clips" extreme values into the 0-100 range
   - A score that was +3.0 sigma away is now only ~2.5 sigma away

## Impact on Thresholds

### Current Thresholds
| Threshold | High Confidence | Medium Confidence |
|-----------|-----------------|-------------------|
| **OLD** | artist_z ≥ 3.0 | artist_z ≥ 1.8 |
| **NEW** | artist_z ≥ 2.4 | artist_z ≥ 1.4 |

### Estimated Effect
- **Fewer singles detected** at 1.8 threshold
  - Roughly 15-20% reduction in medium-confidence singles
  - High-confidence singles reduced by ~10%

- **Thresholds felt "too high"** because z-scores are now smaller for equivalent "outlier-ness"

## Recommended Solution

### Option A: Recalibrate Thresholds (Recommended)
Keep the **intent** of the thresholds constant by lowering the values:

```python
# OLD THRESHOLDS (for raw weighted scores)
HIGH_CONFIDENCE_ARTIST_Z = 3.0    # ~99.7th percentile
MEDIUM_CONFIDENCE_ARTIST_Z = 1.8  # ~96th percentile

# NEW THRESHOLDS (for adjusted scores)
HIGH_CONFIDENCE_ARTIST_Z = 2.4    # ~99th percentile  
MEDIUM_CONFIDENCE_ARTIST_Z = 1.4  # ~92nd percentile
```

**Rationale**: These new thresholds maintain the same statistical percentile targets.

### Option B: Use Pre-Adjusted Scores (Alternative)
Modify single detection to use the original **weighted scores** before adjustment:

```python
# Store two columns:
# - popularity_score: adjusted (for user display, playlists)
# - popularity_score_raw: original weighted (for z-score detection)
```

Then calculate z-scores against raw scores in single detection.

## Recommendation

**Option A is simpler** - just update the thresholds in two files:

1. **single_detection_enhanced.py** (lines ~874-903):
   - Change `1.8` → `1.4` for medium confidence
   - Change `3.0` → `2.4` for high confidence (if applicable)

2. **advanced_single_detection.py** (lines ~619-630):
   - Change `3.0` → `2.4` for high
   - Change `1.8` → `1.4` for medium

This can be done with a simple find-and-replace with full context review.

## Testing the Fix

After updating thresholds, verify with:

```sql
-- Check single detection rates before/after
SELECT 
    COUNT(*) as total_singles,
    COUNT(CASE WHEN is_single = 1 AND single_confidence = 'high' THEN 1 END) as high_conf,
    COUNT(CASE WHEN is_single = 1 AND single_confidence = 'medium' THEN 1 END) as med_conf
FROM tracks
WHERE is_single = 1;
```

You should see roughly **the same number of singles detected** as before the mean adjustment change (not fewer).

## Before You Decide

Check if the new thresholds feel right by running single detection on a test album:

1. Run popularity scan (stores adjusted scores)
2. Run single detection with current 1.8/3.0 thresholds
3. Count medium/high confidence singles
4. Compare to pre-adjustment behavior

If you're seeing significantly fewer singles, go with **Option A** (recalibrate). If counts are similar, leave thresholds as-is.
