# Popularity-Based Confidence System

## Overview

The popularity-based confidence system implements a two-tier approach to automatically rating tracks as 5★ based on their popularity scores and metadata confirmation.

## Confidence Levels

### High Confidence (Auto 5★)

**Criteria:** `popularity >= mean(popularity) + 6`

Tracks that meet this threshold are automatically rated 5★ without requiring metadata confirmation.

**Example:**
- Album mean popularity: 50
- High confidence threshold: 56
- Any track with popularity ≥ 56 gets auto 5★

### Medium Confidence (Requires Metadata)

**Criteria:** `zscore >= mean(zscore of top 50%) - 0.3` AND metadata confirmation

Tracks must satisfy BOTH conditions:
1. Z-score threshold: Track's z-score must be at least 0.3 below the mean z-score of the top 50% of tracks
2. Metadata confirmation from at least ONE source:
   - Discogs (single or music video)
   - Spotify single
   - MusicBrainz single
   - Last.fm single (if implemented)

**Example:**
- Album has 10 tracks
- Top 5 tracks have z-scores: [2.1, 1.8, 1.5, 1.2, 0.9]
- Mean of top 50%: 1.5
- Medium confidence threshold: 1.5 - 0.3 = 1.2
- Track with z-score 1.3 + Spotify single metadata → 5★
- Track with z-score 1.3 but NO metadata → keeps band-based rating

## Source Confidence Classification

### High Confidence Sources
- **Discogs single detection**: Listed as a single release on Discogs
- **Discogs music video**: Listed as an official music video on Discogs

### Medium Confidence Sources  
- **Spotify single**: Listed as a single on Spotify
- **MusicBrainz single**: Listed as a single on MusicBrainz
- **Last.fm single**: Listed as a single on Last.fm (if API supports it)

## Implementation Details

### Statistical Calculations

```python
# 1. Calculate album popularity statistics
album_mean = mean(valid_popularity_scores)
album_stddev = stdev(valid_popularity_scores)

# 2. Calculate z-scores for all tracks
zscore = (track_popularity - album_mean) / album_stddev

# 3. Get top 50% z-scores
sorted_zscores = sorted(zscores, reverse=True)
top_50_count = len(sorted_zscores) // 2
top_50_zscores = sorted_zscores[:top_50_count]
mean_top50_zscore = mean(top_50_zscores)

# 4. Set thresholds
high_conf_threshold = album_mean + 6
medium_conf_zscore_threshold = mean_top50_zscore - 0.3
```

### Rating Decision Flow

```
For each track:
  1. Calculate z-score
  2. Check high confidence:
     - If popularity >= mean + 6:
       → Auto 5★ (no metadata needed)
  3. Check medium confidence:
     - If zscore >= mean_top50_zscore - 0.3:
       → Check metadata sources
       → If ANY source confirms:
         → 5★
       → Else:
         → Keep band-based rating
  4. Fallback to legacy band-based rating
```

## Verbose Logging

When verbose mode is enabled in config.yaml, the system logs:

1. Album statistics:
   ```
   📊 Album Stats: mean=45.2, stddev=12.3
   📈 High confidence threshold: 51.2
   📉 Medium confidence zscore threshold: 0.85
   ```

2. Track-level decisions:
   ```
   ⭐ HIGH CONFIDENCE: Track Name (pop=55.3 >= 51.2)
   ⭐ MEDIUM CONFIDENCE: Track Name (zscore=1.2 >= 0.85, metadata=Spotify, Discogs)
   ⚠️ Medium conf threshold met but no metadata: Track Name (zscore=0.9, keeping stars=3)
   ```

3. Metadata source checks:
   ```
   Checking Discogs for single: Track Name
   ✓ Discogs confirms single: Track Name
   Checking Discogs for music video: Track Name
   ✓ Discogs confirms music video: Track Name
   Checking MusicBrainz for single: Track Name
   ⓘ MusicBrainz does not confirm single: Track Name
   ```

## Configuration

Enable verbose logging in `/config/config.yaml`:

```yaml
features:
  verbose: true
```

Disable automatic Navidrome sync (set perpetual to false):

```yaml
features:
  perpetual: false
```

When `perpetual: false`, the Navidrome library scan will not run automatically when track counts don't match. You must manually trigger a sync by setting `perpetual: true` or running `navidrome_import.py` directly.

## Adaptive Behavior

This system adapts to different album types:

- **Flat albums** (similar popularity across tracks): Fewer tracks will meet the high confidence threshold
- **Spiky albums** (some tracks much more popular): Popular tracks will trigger high confidence
- **Compilations**: More tracks may qualify if they were individually popular singles
- **Niche releases**: Works with any popularity distribution as long as sources are available

## Backward Compatibility

The system maintains backward compatibility with the existing band-based rating system:
- If a track doesn't meet high or medium confidence thresholds, it falls back to the legacy 4-band system
- Medium confidence singles can still achieve 5★ by meeting the zscore threshold with metadata confirmation
- High confidence singles still get auto 5★ (as before)
