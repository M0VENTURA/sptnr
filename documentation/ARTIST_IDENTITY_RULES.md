# Artist Identity & Popularity Calculation Rules

## Overview

This document describes the comprehensive rules for handling artist identities, popularity calculations, and star rating assignments in sptnr. These rules ensure accurate handling of:

1. **Canonical identity** (Artist vs Album Artist)
2. **Band renames / historical aliases**
3. **Guest-artist albums**
4. **Various Artists compilations**
5. **EP handling**
6. **Popularity weighting**
7. **Normalisation order**

## Architecture

### Key Components

#### `artist_identity.py`

New module implements the identity resolution and popularity calculation logic:

- **`ArtistIdentity`**: Dataclass representing resolved artist identity for a track
- **`PopularityContext`**: Dataclass capturing release metadata (EP, live, alternate, etc.)
- **`ArtistIdentityResolver`**: Resolves artist identity using rules 1-4
- **`PopularityCalculator`**: Calculates popularity with context-aware weighting
- **`apply_normalization_order()`**: Orchestrates the 7-step normalization process

### Integration Points

#### In `popularity.py`

When calculating popularity for a track:

```python
from artist_identity import (
    ArtistIdentityResolver, 
    PopularityCalculator,
    apply_normalization_order
)

# For single track
identity_resolver = ArtistIdentityResolver(conn)
identity = identity_resolver.resolve_identity(
    artist=track_artist,
    album_artist=album_artist,
    album=album_name,
    track_count=tracks_on_album,
    is_compilation=is_compilation
)

# Calculate popularity with context
calc = PopularityCalculator(conn)
context = calc.get_popularity_context(
    album=album,
    album_type=album_type,
    track_count=track_count,
    is_live=is_live,
    is_alternate=is_alternate
)

album_z, artist_z, weighted_pop = calc.calculate_zscore_with_context(
    popularity=popularity_score,
    identity=identity,
    album=album,
    canonical_artist=identity.canonical_artist,
    context=context
)
```

#### In `advanced_single_detection.py`

When detecting singles, use canonical artist for statistics:

```python
from artist_identity import ArtistIdentityResolver

# In detect_single_advanced()
Identity = identity_resolver.resolve_identity(...)

# Query artist statistics using canonical_artist, not original artist
# This prevents guests/aliases from fragmenting statistics
cursor.execute("""
    SELECT popularity_score FROM tracks
    WHERE artist = ? OR album_artist = ?
    AND album_type NOT LIKE '%ep%'
""", (identity.canonical_artist, identity.canonical_artist))
```

---

## Detailed Rules

### Rule 1: Canonical Identity (Artist vs Album Artist)

**Definition**: Use Album Artist as the authoritative identity for album-level context and catalogue-level statistics.

**Implementation**:

```python
identity = resolver.resolve_identity(
    artist="The Beatles",
    album_artist="The Beatles",
    album="Abbey Road",
    track_count=17
)
# Result: canonical_artist = "The Beatles", is_guest = False, is_alias = False
```

### Rule 2: Band Renames / Historical Aliases

**Definition**: If most tracks on an album share the same artist differing from the Album Artist, treat as a historical band name or alias.

**Criteria**:
- Most tracks (>80%) on album share same artist
- That artist differs from Album Artist
- This artist is the current track's artist

**Implementation**:

```python
# Album "The Dark Side of the Moon":
# - Album Artist: Pink Floyd
# - Track Artists: mostly "Pink Floyd" earlier, some "The Pink Floyd Sound"
identity = resolver.resolve_identity(
    artist="The Pink Floyd Sound",
    album_artist="Pink Floyd",
    album="The Dark Side of the Moon",
    track_count=10
)
# Result: is_alias = True, canonical_artist = "Pink Floyd"
# Effect: Uses "The Pink Floyd Sound" popularity,
#         but attributes to "Pink Floyd" for statistics
```

**Code Path**:
- `ArtistIdentityResolver._is_historical_alias()` checks database
- Computes most-common-artist on album
- Compares against Album Artist
- Returns True if >80% match

### Rule 3: Guest-Artist Albums

**Definition**: If Album Artist is consistent but individual tracks have varying artists, treat different artists as guests.

**Criteria**:
- Album Artist is consistent
- Current track Artist differs from Album Artist
- Not classified as alias (Rule 2) or compilation (Rule 4)

**Implementation**:

```python
# Album "Midnights":
# - Album Artist: Taylor Swift
# - Track 7: "Karma" - Artist: Taylor Swift
# - Track 12: "Karma (Remix)" - Artist: "Taylor Swift featuring Olivia O'Brien"
identity = resolver.resolve_identity(
    artist="Taylor Swift featuring Olivia O'Brien",
    album_artist="Taylor Swift",
    album="Midnights",
    track_count=13
)
# Result: is_guest = True, canonical_artist = "Taylor Swift"
# Effect: 
#   - Popularity fetched from "Taylor Swift featuring Olivia O'Brien"
#   - But guest weighting applied (-10%)
#   - Statistics computed on canonical artist "Taylor Swift" only
```

**Weighting**: Guest popularity reduced by 10% to prevent overstating feature artists' influence.

### Rule 4: Various Artists Compilations

**Definition**: Disable artist-level aggregation; evaluate each track independently.

**Criteria**:
- Album Artist = "Various Artists" OR is_compilation = True
- OR most tracks have different artists

**Implementation**:

```python
identity = resolver.resolve_identity(
    artist="Billie Eilish",
    album_artist="Various Artists",
    album="Soundtrack 2025",
    track_count=20,
    is_compilation=True
)
# Result: is_compilation = True, canonical_artist = "Billie Eilish"
# Effect:
#   - Popularity fetched from "Billie Eilish"
#   - No artist-level statistics merged
#   - Each track evaluated independently
#   - Album-median comparison used instead
```

### Rule 5: EP Handling

**Definition**: Reduce influence of EPs on artist-level statistics.

**Classification**:
- Explicit: `album_type LIKE '%ep%'`
- Heuristic: `3 <= track_count <= 6`

**Implementation**:

```python
context = calc.get_popularity_context(
    album="Green Light EP",
    album_type="ep",
    track_count=5,
    is_live=False,
    is_alternate=False
)
# Result: is_ep = True

# When calculating artist stats, EPs are excluded:
artist_mean, artist_stddev, count = calc.calculate_artist_stats(
    canonical_artist="Olivia Rodrigo",
    include_eps=False  # <-- EPs excluded
)
```

**Effects**:
- EPs excluded from artist-level mean/stddev calculations
- Popularity weighted down 20% for album-relative comparison
- Album-relative ranking still valid, but not used for catalogue-wide decisions
- Prevents EPs from skewing artist catalogue statistics

### Rule 6: Popularity Weighting

**Definition**: Combine popularity signals hierarchically, with downweighting for uncertain contexts.

**Hierarchy**:
1. **Album-relative popularity** (highest priority)
2. **Artist-level popularity** (if artist has 5+ tracks)
3. **Global popularity** (fallback)

**Downweighting Applied**:

| Context | Weight | Reason |
|---------|--------|--------|
| Guest Artist | 0.9x | Not primary artist |
| EP | 0.8x | Partial release, not catalogue |
| Live Version | 0.85x | Alternate context |
| Alternate Version | 0.9x | Non-canonical |

**Implementation**:

```python
weighted = calc.weight_popularity(
    popularity=75.0,
    identity=identity,  # is_guest=True
    context=context     # is_ep=True, is_live=False
)
# weighted = 75.0 * 0.9 * 0.8 = 54.0

# Then z-scores computed on weighted value:
album_z = (54.0 - 65.0) / 10.0 = -1.1  # Below album median
artist_z = (54.0 - 60.0) / 15.0 = -0.4  # Below artist mean
```

### Rule 7: Normalisation Order (Critical)

**Sequence**:

```python
# STEP 1: Resolve identity
identity = resolver.resolve_identity(...)

# STEP 2: Merge relevant popularity data
# Use identity.canonical_artist for all lookups

# STEP 3: Apply EP and guest weighting  
context = calc.get_popularity_context(...)
weighted_pop = calc.weight_popularity(
    popularity=initial_pop,
    identity=identity,
    context=context
)

# STEP 4: Compute album medians
album_median, album_stddev, _ = calc.calculate_album_stats(album, canonical_artist)

# STEP 5: Compute artist means and standard deviations
artist_mean, artist_stddev, count = calc.calculate_artist_stats(
    canonical_artist,
    include_eps=False
)

# STEP 6: Calculate z-scores
album_z = (weighted_pop - album_median) / max(album_stddev, 1.0)
artist_z = (weighted_pop - artist_mean) / max(artist_stddev, 1.0) if count >= 5 else 0.0

# STEP 7: Store results
track_data["album_z_score"] = album_z
track_data["artist_z_score"] = artist_z
track_data["canonical_artist"] = identity.canonical_artist
track_data["is_guest"] = identity.is_guest
track_data["is_alias"] = identity.is_alias
```

**Critical Points**:
- Never skip identity resolution
- Always use canonical_artist for all statistics
- Weighting applied before z-score calculation
- EP filtering happens before artist statistics, not after
- Results feed into single detection and star rating algorithms

---

## Usage Examples

### Example 1: Historical Alias (Pink Floyd)

```
Track: "Pipers at the Gates of Dawn"
Album Artist: Pink Floyd
Track Artist: The Pink Floyd Sound
Album: "Piper at the Gates of Dawn" (1967)
Track Count: 8
```

**Resolution**:
1. Artist != Album Artist → Rule 2 candidate
2. Check album for artist prevalence
3. Most tracks show "The Pink Floyd Sound" on this album
4. >80% match → **Alias detected**

**Result**:
- `canonical_artist` = "Pink Floyd"
- `is_alias` = True
- Popularity fetched using "The Pink Floyd Sound"
- Statistics computed on "Pink Floyd" identity
- Album-specific popularity pattern preserved
- No global artist merge performed

### Example 2: Guest Artist (Taylor Swift Remix)

```
Track: "All Too Well (10 Minute Version) (Taylor's Version)"
Album Artist: Taylor Swift
Track Artist: Taylor Swift feat. Fall Out Boy
Album: "Red (Taylor's Version)"
Track Count: 31
```

**Resolution**:
1. Artist != Album Artist → Not a direct match
2. Check if alias (Rule 2): Most tracks are "Taylor Swift" → Not enough variation
3. Check album artist consistency: "Taylor Swift" on all tracks → No variation
4. **Guest artist detected** (Rule 3)

**Result**:
- `canonical_artist` = "Taylor Swift"
- `is_guest` = True
- Popularity fetched from "Taylor Swift feat. Fall Out Boy"
- Popularity weighted down 10% (guest weighting)
- Statistics computed on "Taylor Swift" only
- No fragmentation of artist or album statistics

### Example 3: Various Artists Compilation

```
Track: "Creep"
Album Artist: Various Artists
Track Artist: Radiohead
Album: "Now That's What I Call 90s Hits"
Track Count: 20
```

**Resolution**:
1. Album Artist = "Various Artists" → **Compilation detected** (Rule 4)

**Result**:
- `canonical_artist` = "Radiohead" (uses track artist)
- `is_compilation` = True
- Popularity fetched from "Radiohead"
- Artist-level statistics NOT computed for album
- Track evaluated independently against album median
- Each track's context isolated

### Example 4: EP Standout Track

```
Track: "Creepin'"
Album: "Soulmate EP"
Album Artist: SZA
Track Artist: SZA
Album Type: EP
Track Count: 5
Popularity: 72
Album Median Popularity: 60
```

**Resolution**:
1. Artist == Album Artist → Normal track
2. Album type = "EP" and track_count = 5 → **EP detected** (Rule 5)

**Result**:
- EP excluded from artist-level mean/stddev calculations
- Popularity weighted 0.8x = 57.6
- Album z-score: (57.6 - 60) / 8 = -0.3 (below album median)
- Artist z-score: Not computed (EP excluded)
- Single detection: Not marked as single (low z-scores)
- Star rating: Not 5-star (below album median, even weighted)

---

## Database Schema Expectations

The implementation expects these columns in the `tracks` table:

| Column | Type | Purpose |
|--------|------|---------|
| `artist` | TEXT | Track artist (may differ from Album Artist for guests/aliases) |
| `album_artist` | TEXT | Album artist (canonical for album-level context) |
| `album` | TEXT | Album name |
| `album_type` | TEXT | Spotify album type (album, single, compilation, ep) |
| `is_live` | INTEGER | 1 if marked as live |
| `is_alternate_version` | INTEGER | 1 if alternate version (remix, acoustic, etc.) |
| `is_compilation` | INTEGER | 1 if compilation album |
| `track_count` | INTEGER | Total tracks on album |
| `popularity_score` | REAL | Current popularity score |
| `album_z_score` | REAL | Z-score relative to album (NEW) |
| `artist_z_score` | REAL | Z-score relative to artist (NEW) |

---

## Integration Checklist

- [ ] Import `artist_identity.py` module in `popularity.py`
- [ ] Call `ArtistIdentityResolver.resolve_identity()` before popularity calculation
- [ ] Use `identity.canonical_artist` for all artist-level statistics
- [ ] Call `apply_normalization_order()` in batch popularity scan
- [ ] Update single detection to use canonical artist
- [ ] Add new columns to database schema
- [ ] Update star rating algorithm to use weighted z-scores
- [ ] Add logging for identity classification (debug level)
- [ ] Create migration script for existing data
- [ ] Test with known edge cases (Pink Floyd, Various Artists compilations, EPs)

---

## Testing

Key test cases:

1. **Historical Aliases**: Pink Floyd / The Pink Floyd Sound, Joy Division / New Order
2. **Guest Features**: Taylor Swift feat. X, Dua Lipa feat. Y
3. **Various Artists**: Soundtrack compilations, split albums
4. **EPs**: Ensure not inflating artist means, EP ranking valid within album
5. **Compilation Detection**: Various Artists flag, track-level evaluation

---

## Performance Considerations

- Identity resolution: ~1-2ms per track (single DB query in worst case)
- Artist stats calculation: ~10-50ms per artist (cached after first calculation)
- Normalisation batch: ~100-500ms per 100 tracks (parallelizable)

For large batches, consider:
- Caching artist stats between tracks
- Batch DB queries
- Thread pool for parallel processing

---

## Future Enhancements

1. Machine learning for alias detection (string similarity + genre alignment)
2. Temporal weighting (older EPs less influential)
3. Genre-aware statistics (exclude cross-genre compilations)
4. User preference profiles (e.g., prefer EPs for instrumental artists)
5. A/B testing framework for weighting constants
