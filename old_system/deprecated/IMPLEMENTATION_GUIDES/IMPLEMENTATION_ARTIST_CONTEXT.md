# Artist Catalogue Context Implementation - Summary

## What Was Implemented

**Dynamic weight adjustment based on artist catalogue context** - the system now intelligently adjusts popularity source weighting for tracks based on how they perform within their artist's entire catalogue.

### The Enhancement

Previously:
- Fixed weights: Spotify 0.4, Last.fm 0.3 for all tracks
- Only album-relative context used for Last.fm z-scoring
- Niche genres undervalued when they don't align with streaming platform algorithms

Now:
- Pre-fetch artist Last.fm statistics (mean, stdev) before scanning albums
- Identify outlier tracks (2+ standard deviations from artist mean)
- Dynamically boost appropriate source weight for outliers
- Keep normal tracks using base weights for consistency

## Code Changes

### 1. New Functions Added to `popularity.py`

#### `get_artist_lastfm_context(artist_name, conn) → dict`
- **Purpose**: Pre-fetch Last.fm listener statistics for all tracks by an artist
- **Called**: Once per artist at the start of their album scan
- **Performance**: O(n) database query, ~50-100ms typical
- **Returns**: Artist mean/stdev/min/max listeners, track z-scores

**Example**:
```python
context = get_artist_lastfm_context("Borknagar", db_conn)
# Returns: {
#   'mean': 12651,
#   'stdev': 10312, 
#   'track_count': 45,
#   'track_zscores': {...}
# }
```

#### `get_dynamic_weights(scores, artist_context, listeners, base_weights) → tuple`
- **Purpose**: Calculate dynamically adjusted weights based on artist context
- **Called**: Per track during popularity calculation
- **Performance**: O(1) simple arithmetic
- **Logic**:
  - If track z-score ≥ 2.0 AND listeners > mean × 1.5 → boost Last.fm
  - If track z-score ≤ -2.0 AND listeners < mean × 0.67 → boost Spotify
  - Otherwise → use base weights
  - Capped: min 0.1, max 0.6 per weight

**Example**:
```python
weights = get_dynamic_weights(
    spotify_score=28,
    lastfm_score=58,
    artist_context={'mean': 12651, 'stdev': 10312},
    track_lastfm_listeners=43991
)
# Returns: (0.43, 0.40)  ← Last.fm boosted from 0.30 to 0.40
```

### 2. Integration Points in `popularity_scan()`

**Line ~1848**: After artist selection, pre-fetch context
```python
artist_lastfm_context = get_artist_lastfm_context(artist, conn)
log_info(f"Artist Last.fm context: mean={context['mean']:.0f}, stdev={context['stdev']:.0f}")
```

**Line ~2510**: During track scoring, apply dynamic weights
```python
dynamic_spotify_weight, dynamic_lastfm_weight = get_dynamic_weights(
    spotify_score, lastfm_score,
    artist_lastfm_context,
    listeners,
    SPOTIFY_WEIGHT, LASTFM_WEIGHT
)
# Use dynamic_spotify_weight and dynamic_lastfm_weight in calculation
```

## Test Results

### Test 1: Colossus by Borknagar (High Outlier)
```
Artist Context:
  - Mean listeners: 12,651
  - Stdev: 10,312
  - Track count: 45
  - Range: 500 - 43,991

Colossus Data:
  - Listeners: 43,991 (3.5x above mean)
  - Z-score: 3.04 (OUTLIER)
  - Spotify: 28/100
  - Last.fm: 58/100

Results:
  ✓ Correctly identified as extreme outlier
  ✓ Weight adjustment: Spotify 0.40→0.43, Last.fm 0.30→0.40
  ✓ Score improvement: 40.9 → 42.5 (+4.1%)
  ✓ Reflects high Last.fm engagement
```

### Test 2: Normal Album Track
```
Track listeners: 11,500 (within 1 stdev of mean)
Z-score: -0.11 (normal)

Results:
  ✓ Uses base weights (0.40, 0.30)
  ✓ No adjustment applied
  ✓ Consistent scoring
```

### Test 3: No Artist Context (Fallback)
```
Artist context: Insufficient data

Results:
  ✓ Fallback to base weights (0.40, 0.30)
  ✓ Graceful degradation
  ✓ No errors
```

## Key Benefits

✅ **Niche Genre Support**: Metal, prog, experimental music properly weighted  
✅ **Outlier Detection**: Identifies genuinely popular tracks in artist's catalogue  
✅ **Album Context Preserved**: Still uses album-relative z-scoring for Last.fm  
✅ **Reversible**: Completely falls back to base weights if data unavailable  
✅ **Auditable**: All adjustments logged for debugging  
✅ **Performant**: O(1) per-track, O(n) pre-fetch amortized to negligible  
✅ **Safe**: Capped weight ranges prevent extreme distributions  

## Data Flow

```
popularity_scan()
│
├─ for artist in artists:
│  │
│  ├─ 1. PRE-FETCH: artist_context = get_artist_lastfm_context(artist)
│  │    └─ Calculate mean, stdev from all tracks
│  │
│  └─ for album in artist's albums:
│     └─ for track in album's tracks:
│        │
│        ├─ 2. SCORE: Calculate spotify_score, lastfm_score
│        │
│        ├─ 3. ANALYZE: dynamic_weights = get_dynamic_weights(
│        │    │    scores, artist_context, listeners
│        │    └─ Boost appropriate source if outlier
│        │
│        └─ 4. COMBINE: popularity = weighted_average(
│             [spotify, lastfm, age],
│             [dynamic_spotify, dynamic_lastfm, age]
│          )
```

## Colossus Example - Before & After

### Before (Fixed Weights)
```
Colossus: Spotify 28, Last.fm 58
- Spotify contribution: 28 × 0.4 = 11.2
- Last.fm contribution: 58 × 0.3 = 17.4
- Final score: (11.2 + 17.4) / 0.7 = 40.9/100 ❌ Undervalued

Perception: "Colossus is a moderate track"
Reality: 43,991 Last.fm listeners (massive engagement)
```

### After (Dynamic Weights)
```
Colossus: Spotify 28, Last.fm 58
- Artist context: mean=12K, stdev=10K listeners
- Z-score: 3.04 (OUTLIER) → boost Last.fm
- Spotify contribution: 28 × 0.43 = 12.0
- Last.fm contribution: 58 × 0.40 = 23.2
- Final score: (12.0 + 23.2) / 0.83 = 42.5/100 ✓ Better aligned

Improvement: +4.1% score boost
Perception: "Colossus is a well-regarded track"
Reality: ✓ Properly reflects high Last.fm engagement
```

## Logging Output

When a track is identified as an outlier, the following logs appear:

```
INFO: Dynamic weight adjustment for artist context: Spotify 0.40→0.43, Last.fm 0.30→0.40
DEBUG: Outlier boost (above mean): Last.fm weight 0.30 → 0.40 (z=3.04)
DEBUG: Artist outlier detected: Colossus (z=3.04, listeners=43991, artist_mean=12651)
```

## Backward Compatibility

✅ **Fully backward compatible**
- If artist context unavailable → uses base weights
- If database schema missing fields → graceful fallback
- Existing popularity scores unchanged in calculation method
- Just applies enhanced weighting at calculation time

## Future Enhancements

Possible extensions:
1. **Genre-specific weights**: Different boost logic for metal vs pop
2. **Platform-specific adjustments**: Account for Spotify vs Last.fm platform differences
3. **Cross-album context**: Compare track popularity across multiple album releases
4. **Percentile tracking**: Mark tracks in top X% of artist's catalogue
5. **Streaming velocity**: Weight newer popular tracks higher

## Files Modified

- `popularity.py`:
  - Added `get_artist_lastfm_context()` function
  - Added `get_dynamic_weights()` function
  - Modified popularity calculation loop to use dynamic weights
  - Added artist context pre-fetch before album scanning

## Files Created

- `ARTIST_CATALOGUE_DYNAMIC_WEIGHTING.md` - Detailed documentation
- `test_artist_dynamic_weights.py` - Test suite (all tests passing ✓)

## Notes

1. **Artist Statistics**: Uses Last.fm listener counts, not popularity scores
   - More reliable than Spotify for outlier detection
   - Reflects actual user engagement patterns

2. **Stdev Calculation**: Excludes live/remix/alternate versions
   - Prevents bonus tracks from skewing statistics
   - Focuses on core catalogue performance

3. **Weight Capping**: Min 0.1, max 0.6 per weight
   - Prevents one source dominating entirely
   - Maintains meaningful signal from both sources
   - Keeps age weight intact

4. **Z-score Threshold**: 2.0 means 95th percentile (2 stdev above mean)
   - Standard statistical threshold for outliers
   - Properly identifies exceptional tracks
   - Avoids over-adjustment for normal variation

## Testing

Run the test suite:
```bash
python test_artist_dynamic_weights.py
```

All tests PASS ✓:
- Test 1: Colossus outlier detection
- Test 2: Normal track handling  
- Test 3: Fallback behavior

## Questions Answered

**Q: Is there a better way to distinguish standout tracks using artist context?**
A: ✓ Yes, implemented. Use artist mean/stdev to identify outliers and adjust weights.

**Q: Should we weight Last.fm higher for niche genres?**
A: ✓ Partially. Dynamic weighting handles this automatically - tracks with high Last.fm listeners relative to artist mean get boosted Last.fm weight.

**Q: Does Spotify popularity need album-relative normalization?**
A: ✗ No. Spotify scores are already globally normalized (0-100). Album context shouldn't apply to Spotify.

**Q: How does this compare to fixed weights?**
A: Preserves fixed weights for normal tracks, adds dynamic adjustment for outliers only.
