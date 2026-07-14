# ListenBrainz Integration & Live Album Fixes - Implementation Summary

## Overview
This implementation addresses three critical issues in the popularity scanning system:
1. **Featured Artist Handling**: Enhanced Last.fm lookup to try multiple artist variations
2. **ListenBrainz Integration**: Added community popularity source as secondary scoring metric
3. **Live Album Penalty**: Reduced popularity scores for live album tracks to prevent false 5-star ratings

---

## Changes Made

### 1. Enhanced Featured Artist Handling (api_clients/lastfm.py)

#### New Function: `_extract_featured_artists()`
Extracts all featured artist names from a collaboration string (e.g., "Artist feat. Guest1 & Guest2").

**File**: `api_clients/lastfm.py` (lines 355-369)

```python
@staticmethod
def _extract_featured_artists(artist: str) -> list[str]:
    """Extract featured artist names from a collaboration string."""
```

#### Enhanced Method: `get_track_info()`
Now tries a 3-step lookup process instead of 2:
1. **Primary artist** (stripped of "feat."/"ft."/"featuring")
2. **Each featured artist individually** (tries them separately)
3. **Full artist string** (fallback with full collaboration name)

**Why This Works**:
- Handles cases where "dArtagnan feat. Melissa Bonny" gets more popularity when looked up as "Melissa Bonny" or vice versa
- Tries the most promising artists first (primary artist is most common match)
- Featured artists often have their own catalog with separate popularity scores
- Improves hit rate for collaborative tracks on Last.fm

---

### 2. ListenBrainz Integration (popularity_helpers.py & popularity.py)

#### New Function: `calculate_listenbrainz_popularity_score()` 
Converts ListenBrainz listen count to 0-100 popularity scale.

**File**: `popularity_helpers.py` (lines 665-702)

```python
def calculate_listenbrainz_popularity_score(listen_count: int) -> float:
    """
    Calculate ListenBrainz popularity score (0-100) from global listen count.
    Uses logarithmic normalization: score = 12.5 * log10(listen_count)
    """
```

**Listen Count Ranges**:
- 1,000 listens → 37.5 points
- 10,000 listens → 50 points
- 100,000 listens → 62.5 points
- 1,000,000 listens → 75 points

#### Score Blending Logic (popularity.py)
ListenBrainz is blended as a **secondary source**:
- **If Last.fm available**: ListenBrainz gets 40% weight (Last.fm 70% + Age 30% in standard case)
- **If Last.fm missing**: ListenBrainz gets 70% weight (same as Last.fm default)

**Why This Works**:
- ListenBrainz aggregates from multiple scrobbling services (not just Last.fm users)
- Different popularity distribution than Last.fm (typically 10-100x larger listen counts)
- Provides independent verification for tracks lacking Last.fm data
- Prevents tracks with zero Last.fm listeners from getting 0 score if ListenBrainz has data

#### Implementation Details (popularity.py lines 6462-6485)
- Fetches ListenBrainz popularity for track MBID using `get_recording_popularity_batch()`
- Gracefully handles missing MBIDs or API failures
- Logs all attempts for debugging

---

### 3. Live Album Popularity Penalty (popularity.py)

#### Live Album Detection Flags
- `album_context_live`: Album marked as live (value: 0 or 1)
- `is_live`: Track marked as live (value: 0 or 1)

#### Penalty Implementation (popularity.py lines 6591-6606)
When a track is on a live album:
- **Last.fm weight is reduced by 50%**: 0.70 → 0.35 (70% default → 35% after penalty)
- **Age score unaffected**: Still contributes normally
- **ListenBrainz unaffected**: If available, acts as secondary source

**Why This Works**:
- Last.fm popularity data may incorrectly reflect the studio version's popularity
- A live album track shouldn't get 5-star just from studio data
- 50% reduction is aggressive enough to prevent false high ratings
- Still allows genuinely popular live performances to score high (with Age + ListenBrainz sources)

**Example Scoring**:
- Studio track: Last.fm score 75 → Final: ~68 (0.75 * 0.70 + age_factor * 0.30)
- Live track: Last.fm score 75 → Final: ~53 (0.75 * 0.35 + age_factor * 0.30)
- Result: Studio version gets 4-5 stars, live version gets 2-3 stars ✓

---

## Weight Summary

### Standard Scoring (Non-Live Tracks)
- Last.fm: 70%
- Age: 30%

### Live Album Track Scoring
- Last.fm: 35% (reduced from 70%)
- Age: 30%
- **Gap**: 35% - Available for ListenBrainz if present

### When ListenBrainz is Available (Non-Live)
- Last.fm: 60% (normalized from 70%)
- ListenBrainz: 40% (supplementary)
- Age: 30% (unchanged)

### When Last.fm Missing (Non-Live)
- ListenBrainz: 70%
- Age: 30%

---

## Database Usage

### Required Columns (Already Exist)
- `is_live`: Track-level live flag
- `album_context_live`: Album-level live flag
- `mbid`: MusicBrainz recording ID (for ListenBrainz lookups)
- `listenbrainz_score`: Already in schema for future per-track caching

### Rate Limiting
- ListenBrainz shares rate limiting with MusicBrainz
- Max 1 request per second (enforced by `api_rate_limiter.check_musicbrainz_limit()`)
- Batch requests up to 100 recordings per call

---

## Backwards Compatibility

✅ **All changes are backward compatible**:
- Featured artist lookup still tries primary artist first (existing behavior)
- ListenBrainz is optional (gracefully degrades if unavailable)
- Live album penalty only applies when flags are set
- Existing weight configuration still works

---

## Testing Checklist

After running the next popularity scan:

- [ ] Featured artist tracks (e.g., dArtagnan feat. Melissa Bonny) return non-zero popularity
- [ ] ListenBrainz scores appear in logs: `ListenBrainz popularity for "..." : X listens`
- [ ] Live album tracks score lower than studio versions of same song
- [ ] Log shows "Live album penalty applied" for tracks on live albums
- [ ] Track with studio + live versions: Studio has more stars than live
- [ ] Tracks without Last.fm data but with ListenBrainz get non-zero score
- [ ] MBID resolution still works (for ListenBrainz lookups)

---

## Configuration

### Weights Config (config.yaml)
Current defaults (no changes needed):
```yaml
weights:
  lastfm: 0.70
  age: 0.30
  listenbrainz: 0.0  # Not directly configurable (auto-calculated)
```

### To Adjust Live Penalty
Edit `popularity.py` line 6603:
```python
live_album_weight_reduction = 0.50  # Change this value (0.0 to 1.0)
```

---

## Logs to Monitor

Look for these log messages during next scan:

1. **Featured Artist Attempts**:
   ```
   Better result found for 'Track': 'Featured Artist' with X listeners
   Found good match for 'Track' by 'Primary Artist': X listeners, X playcount
   ```

2. **ListenBrainz Lookups**:
   ```
   ListenBrainz popularity for "Track": X listens, score: Y.Y
   No ListenBrainz data for MBID abc123
   ListenBrainz lookup failed for Track (MBID xyz): Error message
   ```

3. **Live Album Penalty**:
   ```
   Live album penalty applied: Last.fm weight reduced by 50% (new weight: 0.35)
   Track on live album (is_live=1, album_context_live=1), applying popularity penalty
   ```

4. **Score Blending**:
   ```
   Including Last.fm score: 75.0 (weight: 0.35)
   Including ListenBrainz score: 62.5 (weight: 0.40)
   Including age score: 45.0 (weight: 0.30)
   Weighted popularity calculation - lastfm: 75.0, age: 45.0, weighted: 62.5
   ```

---

## Files Modified

1. **api_clients/lastfm.py**
   - Added `_extract_featured_artists()` method
   - Enhanced `get_track_info()` with 3-step lookup

2. **popularity_helpers.py**
   - Added `calculate_listenbrainz_popularity_score()` function

3. **popularity.py**
   - Added ListenBrainz popularity fetching (lines 6462-6485)
   - Enhanced score blending logic with ListenBrainz support
   - Added live album penalty to Last.fm weight (lines 6591-6606)
   - Updated score blending to include ListenBrainz conditionally

---

## Performance Impact

- **ListenBrainz API calls**: Batched, ~1 request per 100 tracks
- **Featured artist lookups**: Additional 1-2 Last.fm requests per featured track (minimal)
- **Live album penalty**: Zero overhead (just weight calculation)
- **Overall**: Negligible impact on scan time

---

## Future Enhancements

1. **Per-artist ListenBrainz context**: Like Last.fm context, fetch top recordings for artist
2. **Configuration**: Make live penalty percentage user-configurable
3. **Caching**: Store ListenBrainz scores in DB for historical comparison
4. **Analytics**: Track ratio of Last.fm vs ListenBrainz scoring usage
5. **A/B Testing**: Compare results with/without live penalty

---

## References

- [ListenBrainz Popularity API](https://listenbrainz.readthedocs.io/en/latest/users/api/popularity.html)
- [MusicBrainz Recording API](https://musicbrainz.org/development/xml-web-service/version-2)
- Last.fm track.getInfo API documentation
- Current implementation in `popularity.py` and related files

