# Album Deviation Adjustment Implementation

## What's New

Added `apply_album_deviation_adjustment()` function to `popularity_helpers.py` that refines popularity scores by analyzing tracks within their album context.

## When It Activates

- Requires 2+ tracks with popularity data in the album
- Automatically skips single-track albums
- Different weight factors based on album popularity tier:
  - **Low popularity albums** (<40): 40% album weight - helps identify gems in niche catalogs
  - **Mid-tier albums** (40-60): 30% album weight - balanced approach
  - **High popularity albums** (>60): 15% album weight - artist consistency dominates

## How It Works

1. Fetches all track popularities for the album
2. Calculates album mean and standard deviation
3. Computes track z-score within album distribution
4. Blends with original score using weighted average

## Integration Examples

**Simple usage in popularity.py:**

```python
from popularity_helpers import apply_album_deviation_adjustment

# After calculating popularity_score
adjusted_score = apply_album_deviation_adjustment(
    popular_score,
    artist_name,
    album_name,
    conn=conn
)
```

**Chained with mean adjustment:**

```python
# Apply artist context first
adjusted_score = apply_mean_popularity_adjustment(
    popularity_score,
    artist_name,
    release_year=year,
    conn=conn
)

# Then apply album context (optional refinement)
final_score = apply_album_deviation_adjustment(
    adjusted_score,
    artist_name,
    album_name,
    conn=conn
)
```

## Next Steps

Integration points to consider:

1. **Post-scan refinement**: Run after initial popularity scores are calculated
2. **Second-pass activation**: Activate when album has 2+ scanned tracks
3. **Optional flag**: Make it configurable per artist tier in config.yaml
4. **Logging**: Detailed debug logs show blending calculations for verification

## Test Coverage

- ✅ Low-popularity albums (Gothic Rock): 40% weight applied
- ✅ Single-track albums: Skipped (insufficient variance data)
- ✅ Edge case handling: Division by zero protection

## Database Requirements

- No new schema changes required
- Uses existing `tracks.popularity` column
- Reads from `artist_stats.mean_popularity` (populated by mean adjustment)
