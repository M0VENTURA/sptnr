# Mean Popularity Adjustment Implementation

## Overview

The popularity scanning system now applies **mean-based normalization** with **time decay** for pre-2005 releases. This replaces pure weighted averaging with artist-context-aware scoring.

## What Changed

### Before (Pure Weighted Scoring)
```
popularity_score = 0.10*spotify + 0.30*lastfm + 0.35*listenbrainz + 0.25*age
# Result: Global score, doesn't account for artist catalog context
```

### After (Weighted + Mean Adjustment)
```
# Step 1: Calculate weighted average (unchanged)
weighted_score = 0.10*spotify + 0.30*lastfm + 0.35*listenbrainz + 0.25*age

# Step 2: Normalize relative to artist mean catalog
z_score = (weighted_score - artist_mean) / artist_stddev

# Step 3: Apply time decay for pre-2005 releases
if release_year < 2005:
    years_before_2005 = 2005 - release_year
    decay_factor = max(0.2, 1.0 - (years_before_2005 * 0.04))
    z_score *= decay_factor

# Step 4: Convert z-score to 0-100 scale
adjusted_score = 50 + (z_score * 16.7)
```

## Why This Matters

### Problem 1: Algorithmic Bias
- Spotify popularity scores are algorithm-driven and increasingly gamed
- A track with 30 Spotify popularity might be genuinely popular on Last.fm
- Pure weighting allows Spotify to pull scores down unfairly

### Problem 2: Artist Context
- A 50-point track means different things for different artists
- Top 1% of artist catalog (z=+2) > global 50-point score
- Relative positioning matters more than absolute value

### Problem 3: Data Sparsity Pre-2005
- Last.fm launched in 2002, so pre-2005 data is sparse and incomplete
- Fewer active users → fewer scrobbles → artificially low listener counts
- Time decay reduces confidence in pre-2005 scores:
  - 2005: 1.0x (no decay)
  - 2000: 0.8x (5 years = 20% reduction)
  - 1995: 0.6x (10 years = 40% reduction)
  - 1990: 0.4x (15 years = 60% reduction)
  - Pre-1990: 0.2x (minimum confidence)

## How Artist Stats Are Calculated

Artist stats are computed from all non-live/non-remix/non-demo tracks:
- **mean_popularity**: Average of all track popularity scores
- **median_popularity**: Middle value (legacy field)
- **popularity_stddev**: Standard deviation (measures variance)

These are updated in `artist_stats` table after each album scan completes.

## Z-Score Interpretation

| Z-Score | Score | Interpretation |
|---------|-------|-----------------|
| -3.0 | 0 | 3 std devs below artist mean |
| -1.0 | 33 | 1 std dev below mean |
| 0.0 | 50 | At artist mean |
| +1.0 | 67 | 1 std dev above mean |
| +2.0 | 83 | 2 std devs above mean |
| +3.0 | 100 | 3 std devs above mean |

## Progressive Improvement

The system improves with successive scans:

### First Scan of Artist
- No prior artist_stats available
- Uses pure weighted scores
- Stores tracks in database

### Second Scan of Same Artist
- artist_stats now populated from first scan
- Applies z-score normalization and time decay
- Scores adjust to reflect artist context

## Example Scenarios

### Scenario A: 2004 Folk Album
```
Track: "Old Song"
Weighted score: 25 (sparse Last.fm data)
Artist mean: 35, stddev: 15
Z-score: (25-35)/15 = -0.67

Time decay (1 year before 2005):
decay_factor = max(0.2, 1.0 - 1*0.04) = 0.96
z_score = -0.67 * 0.96 = -0.64

Adjusted score: 50 + (-0.64 * 16.7) = 40

Result: Score unchanged by decay (minor), z-score properly reflects below-average standing
```

### Scenario B: 2010 Pop Album, Outlier Track
```
Track: "Viral Hit"
Weighted score: 75 (strong performance)
Artist mean: 40, stddev: 15
Z-score: (75-40)/15 = +2.33

No time decay (post-2005)

Adjusted score: 50 + (2.33 * 16.7) = 89

Result: 2.3 std devs above artist mean → 89 points
Properly identifies outlier track
```

### Scenario C: 1995 Jazz Album, Below Average
```
Track: "Session Track"
Weighted score: 35
Artist mean: 50, stddev: 20
Z-score: (35-50)/20 = -0.75

Time decay (10 years before 2005):
decay_factor = max(0.2, 1.0 - 10*0.04) = 0.6
z_score = -0.75 * 0.6 = -0.45

Adjusted score: 50 + (-0.45 * 16.7) = 42

Result: Below-average track properly marked lower, but not as low
as raw weighted score due to sparse data accounting
```

## Impact on Star Ratings

The single detection and star rating system uses `popularity_score`:
- **5-star tracks**: Top 15% of artist (previously used z-score >= 1.8, now uses adjusted score)
- **4-star tracks**: Top 50% of artist
- **3-star tracks**: Standard tracks
- **2-star tracks**: Below average
- **1-star tracks**: Poor performers

With mean adjustment:
- Scores better reflect artist context
- Pre-2005 tracks won't be unfairly penalized
- Breakthrough tracks still identified as outliers

## Edge Cases (Graceful Fallback)

1. **New artist, first scan**: Uses weighted scores (artist_stats not available yet)
2. **Missing artist_stats**: Returns original weighted score
3. **Zero variance**: Z-score = 0 (track at artist mean)
4. **No release year**: No time decay applied
5. **DB connection failure**: Returns original weighted score

## Validation Checklist

When testing the implementation:

- [ ] Run popularity scan on new artist (should use weighted scores)
- [ ] Run popularity scan again (should apply mean adjustment)
- [ ] Check artist_stats table: `SELECT artist_name, mean_popularity, popularity_stddev FROM artist_stats WHERE track_count > 0`
- [ ] Verify pre-2005 tracks get lower scores due to decay
- [ ] Check that 5-star outliers are still identified
- [ ] Verify log messages show "adjusted score differs from weighted"

## Code Changes

### Modified Files
- **popularity_helpers.py**: Added `apply_mean_popularity_adjustment()` function
- **popularity.py**: 
  - Integrated adjustment into main scoring loop (line ~3388)
  - Fixed artist_stats update to use correct column names
  - Import new function from popularity_helpers

### Commit
- `3792d07`: "Add: Mean popularity adjustment with time decay for pre-2005 releases (TODO 7)"

## References

Related documentation:
- `FINAL_SUMMARY.md`: Complete project overview
- `single_detection_enhanced.py`: Z-score usage for single detection
- `check_db.py`: artist_stats schema definition

## Next Steps

1. Run next popularity scan to activate adjustment
2. Monitor logs for "adjusted score" messages
3. Verify artist_stats are properly computed
4. Check pre-2005 releases for score adjustments
5. Validate 5-star ratings still accurate (may change during transition)
