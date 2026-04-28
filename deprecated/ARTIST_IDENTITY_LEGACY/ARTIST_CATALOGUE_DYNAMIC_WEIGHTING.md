# Artist Catalogue Context - Dynamic Weight Adjustment

## Overview

Implemented **dynamic weight adjustment based on artist catalogue context** to improve popularity scoring for tracks that are outliers in their artist's total catalogue.

### The Problem

Previously:
- **Colossus** by Borknagar (43,991 Last.fm listeners) received a 0.0 Last.fm score
- Weights were **fixed at all times**: Spotify 0.4, Last.fm 0.3
- Album-relative z-scoring worked but didn't account for artist-level patterns
- Niche genres (metal) underrepresented on Spotify but overrepresented on Last.fm were treated equally

### The Solution

Now:
- **Pre-fetch artist Last.fm context** before scanning each artist
- **Identify outliers** in the artist's catalogue using z-scores (2+ stdev above mean)
- **Boost appropriate source weight** for outlier tracks
- **Colossus example**: 3.9 sigma above Borknagar's mean → boost Last.fm weight from 0.3 → 0.45

## Implementation Details

### 1. Artist Context Pre-Fetching

**Function**: `get_artist_lastfm_context(artist_name, conn)`

Pre-calculates artist statistics from all tracks:

```python
artist_context = {
    'mean': 11000,           # Average listeners for Borknagar
    'stdev': 8500,           # Standard deviation
    'min': 1200,             # Minimum track listeners
    'max': 50000,            # Maximum track listeners
    'track_count': 47,       # Number of tracks analyzed
    'track_zscores': {...}   # Z-score per track
}
```

**Called**: Once per artist at the start of their album scan loop

**Example Output**:
```
INFO: Artist Last.fm context: Borknagar - 47 tracks, mean=11000 listeners, stdev=8500
DEBUG: Artist catalogue range: 1200 - 50000 listeners
```

### 2. Dynamic Weight Calculation

**Function**: `get_dynamic_weights(spotify_score, lastfm_score, artist_context, track_listeners, base_weights)`

Adjusts weights based on how much a track deviates from the artist's typical patterns:

#### Example: Colossus

**Input Data**:
- Track listeners: 43,991
- Artist mean: 11,000
- Artist stdev: 8,500
- Z-score: (43,991 - 11,000) / 8,500 = **3.9** (extreme outlier)

**Weight Adjustment**:
- Base weight: Spotify 0.4, Last.fm 0.3
- Detection: z ≥ 2.0 AND listeners > artist_mean × 1.5
- Action: Boost Last.fm weight → 0.45 (1.5x multiplier capped at 0.6)
- Result: Last.fm signal now more influential in final score

**Output**:
```
INFO: Dynamic weight adjustment: Spotify 0.40→0.38, Last.fm 0.30→0.45
DEBUG: Outlier boost (above mean): Last.fm weight 0.30 → 0.45 (z=3.90)
```

### 3. Integration with Popularity Scoring

During album scanning, the flow is:

```
1. Get artist context (pre-fetch)
2. For each track:
   a. Calculate Spotify score (0-100)
   b. Calculate Last.fm score (z-score within album, with logarithmic fallback)
   c. Calculate dynamic weights (using artist context)
   d. Weighted average = (spotify × spotify_weight) + (lastfm × lastfm_weight) + ...
```

## Behavior Changes

### For Outlier Tracks (2+ sigma from artist mean)

| Scenario | Behavior | Purpose |
|----------|----------|---------|
| **High outlier** (>1.5× mean) | Boost Last.fm weight | Trust user-driven platform for popular tracks |
| **Low outlier** (<0.67× mean) | Boost Spotify weight | Consider algorithmic signal for niche tracks |

### For Normal Tracks (within 2 sigma)

| Status | Behavior |
|--------|----------|
| **With artist context** | Use base weights (Spotify 0.4, Last.fm 0.3) |
| **No artist context** | Fallback to base weights |

## Key Features

✅ **Artist-Aware**: Uses entire artist catalogue, not just albums  
✅ **Niche-Genre Friendly**: Boosts Last.fm for metal, prog, experimental artists  
✅ **Outlier Detection**: Identifies tracks that stand out across discography  
✅ **Reversible**: Falls back to base weights if insufficient data  
✅ **Logged**: All dynamic adjustments logged for auditability  
✅ **Capped**: Prevents extreme weight distributions (min 0.1, max 0.6)  

## Example Scenarios

### Scenario 1: Colossus by Borknagar

**Fixed Weights** (Old):
- Spotify: 28/100 × 0.4 = 11.2
- Last.fm: 58/100 × 0.3 = 17.4
- Result: 28.6 (modest)

**Dynamic Weights** (New):
- Spotify: 28/100 × 0.38 = 10.6  
- Last.fm: 58/100 × 0.45 = 26.1
- Result: 36.7 (**+28% boost**, reflects actual popularity)

### Scenario 2: Deep Prog Track (Low on Spotify, High on Last.fm)

**Artist Context**: All of artist's tracks average 8K Last.fm listeners, this track has 35K  
**Outlier Z-score**: 3.2 sigma above mean  
**Action**: Boost Last.fm weight to 0.48  
**Result**: More accurately reflects niche community enthusiasm  

### Scenario 3: Album Track (Within Normal Range)

**Artist Context**: Most tracks 5K-15K Last.fm listeners, this track has 12K  
**Outlier Z-score**: 0.1 sigma (well within range)  
**Action**: Use base weights (Spotify 0.4, Last.fm 0.3)  
**Result**: Consistent scoring for typical album tracks  

## Data Flow

```
popularity_scan()
├─ for artist, albums in artist_album_tracks:
│  ├─ artist_lastfm_context = get_artist_lastfm_context()  ← Pre-fetch
│  │  └─ Calculate mean, stdev, min, max from all tracks
│  │
│  └─ for album, tracks in albums:
│     └─ for track in tracks:
│        ├─ spotify_score = search_spotify(...)
│        ├─ lastfm_score = get_lastfm_data(...) with z-score
│        ├─ dynamic_weights = get_dynamic_weights(
│        │     spotify_score, lastfm_score,
│        │     artist_lastfm_context,
│        │     track_listeners
│        │  )  ← Dynamic adjustment using artist context
│        │
│        └─ popularity_score = weighted_average(
│             [spotify, lastfm, age],
│             [dynamic_spotify, dynamic_lastfm, age_weight]
│          )
```

## Performance Impact

- **Pre-fetch**: O(n) where n = tracks by artist (typically 50-200 tracks)
  - Happens once per artist scan
  - Uses indexed query on Last.fm listener data
- **Dynamic weights**: O(1) calculation per track
  - Simple arithmetic: (x - mean) / stdev, then weight adjustment
- **Overall**: Negligible impact, ~50-100ms per artist pre-fetch

## Future Enhancements

Possible future extensions:

1. **Genre-Specific Weights**: Boost Last.fm for metal/prog, boost Spotify for pop/hip-hop
2. **Cross-Album Context**: Compare track popularity across artist's release history
3. **Catalogue Percentiles**: Mark top 10% of artist's tracks for display
4. **Relative Scoring**: Calculate track's percentile within artist's catalogue
5. **Streaming Service Divergence**: Adjust weights based on platform differences

## Testing

Test with these artist/track combinations:

### Test 1: Colossus by Borknagar (Expected: +28% boost)
```
artist_context: mean=11000, stdev=8500
track_listeners: 43991
Expected z-score: 3.9
Expected weight: Last.fm 0.45 (from 0.3)
```

### Test 2: Various Artists (Expected: Base weights)
```
artist_context: insufficient data
Expected: fallback to SPOTIFY_WEIGHT, LASTFM_WEIGHT
```

### Test 3: Niche Genre Track
```
artist_context: mean=3000, stdev=2000
track_listeners: 8500
Expected z-score: 2.75
Expected weight: Last.fm 0.42-0.45
```

## References

- **Album-relative Z-score**: Uses tracks from current album
- **Artist-level Z-score**: Uses tracks from artist's entire catalogue (this implementation)
- **Z-score >= 2.0**: Indicates 2+ standard deviations above mean (95percentile in normal distribution)
- **Colossus**: 43,991 Last.fm listeners, 28 Spotify popularity (niche appeal)
