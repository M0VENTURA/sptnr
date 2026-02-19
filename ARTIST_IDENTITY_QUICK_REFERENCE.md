# Artist Identity Quick Reference Card

## Core Concept

**7-Point Rule System** for accurate popularity calculation and singles detection:

```
Resolve Identity → Merge Data → Weight Context → Album Stats → 
Artist Stats → Calculate Z-scores → Store Results
```

## Quick API Reference

### Import

```python
from artist_identity import (
    ArtistIdentity,           # Dataclass: result of identity resolution
    PopularityContext,         # Dataclass: release context (EP, live, etc)
    ArtistIdentityResolver,    # Main resolver class
    PopularityCalculator,      # Popularity weighting & z-score class
    apply_normalization_order  # Batch processor function
)
```

---

## Identity Resolution (Rule 1-4)

### Resolve a Single Track

```python
resolver = ArtistIdentityResolver(conn)
identity = resolver.resolve_identity(
    artist="Pink Floyd",          # Track artist
    album_artist="Pink Floyd",    # Album artist
    album="Dark Side of Moon",    # Album name
    track_count=10,               # Tracks on album
    is_compilation=False          # Compilation flag
)

# Result: ArtistIdentity dataclass
print(identity.canonical_artist)   # Authoritative artist name
print(identity.is_alias)           # True if historical alias (Rule 2)
print(identity.is_guest)           # True if guest artist (Rule 3)
print(identity.is_compilation)     # True if Various Artists (Rule 4)
```

### Result Interpretation

| is_alias | is_guest | is_compilation | Scenario |
| -------- | -------- | -------------- | -------- |
| False | False | False | **Normal track** - artist == album artist |
| True | False | False | **Historical alias** - canonical_artist is real identity |
| False | True | False | **Guest artist** - apply -10% weighting |
| False | False | True | **Compilation** - each track independent |

---

## Popularity Context (Rule 5)

### Get Release Context

```python
calc = PopularityCalculator(conn)
context = calc.get_popularity_context(
    album="Soulmate EP",       # Album name
    album_type="ep",           # Explicit EP? (optional)
    track_count=5,             # 3-6 tracks = heuristic EP
    is_live=False,             # Live recording?
    is_alternate=False         # Remix/acoustic/etc?
)

# Result: PopularityContext dataclass
print(context.is_ep)       # True if 3-6 tracks or album_type contains "ep"
print(context.is_live)     # True if live
print(context.is_alternate) # True if remix/alternate
```

### Weighting Effects

| Context | Weight | Effect |
|---------|--------|--------|
| Normal track | 100% | Baseline |
| EP track | 80% | Not full album |
| Guest artist | 90% | Not primary artist |
| Live | 85% | Alternate context |
| Alternate version | 90% | Non-canonical |
| Multiple | Multiplicative | All apply |

Example: Guest on EP = 0.9 × 0.8 = 0.72 (28% reduction)

---

## Popularity Calculation (Rule 6-7)

### Calculate Z-Scores with Context

```python
album_z, artist_z, weighted_pop = calc.calculate_zscore_with_context(
    popularity=75.0,              # Raw popularity score
    identity=identity,            # From resolver (rules 1-4)
    album="Dark Side of Moon",    # Album name for stats
    canonical_artist="Pink Floyd", # Use canonical_artist always
    context=context               # From get_popularity_context() (rule 5)
)

# Results:
print(f"Weighted popularity: {weighted_pop:.1f}")  # After context weighting
print(f"Album z-score: {album_z:.2f}")             # Relative to album
print(f"Artist z-score: {artist_z:.2f}")           # Relative to artist
```

### Z-Score Interpretation

| Score | Meaning | Star Rating |
|-------|---------|-------------|
| z >= 1.5 | Standout song | ⭐⭐⭐⭐⭐ |
| 1.0 to 1.5 | Strong track | ⭐⭐⭐⭐ |
| 0.5 to 1.0 | Above average | ⭐⭐⭐ |
| 0.0 to 0.5 | Average | ⭐⭐ |
| < 0.0 | Below average | ⭐ |

---

## Batch Processing (Rule 7 - Normalization Order)

### Apply Full Pipeline

```python
# After collecting/checking all tracks
normalized_tracks = apply_normalization_order(
    conn=conn,
    tracks=tracks_list,
    log_level=logging.DEBUG  # Optional: logging detail
)

# Each track in result has:
# - canonical_artist (never changes)
# - is_guest / is_alias / is_compilation (never changes)
# - album_z_score (relative to album median)
# - artist_z_score (relative to artist mean)
# - weighted popularity (used for calculation)
```

---

## Common Patterns

### Pattern 1: Check if Single (Rule 5 + 7)

```python
if identity.is_compilation:
    # Compilation: no artist-level stats, use album median only
    is_single = album_z > 1.5
elif context.is_ep:
    # EP: no artist stats, but album stats valid
    is_single = album_z > 1.5
elif identity.is_guest:
    # Guest: lower threshold (primary artist not involved)
    is_single = album_z > 1.0
else:
    # Normal: both scores must be high
    is_single = album_z > 1.0 and artist_z > 0.5
```

### Pattern 2: Exclude from Artist Stats (Rule 2 + Rule 5)

```python
# Query artist catalogue excluding compilations, EPs, and live versions
cursor.execute("""
    SELECT popularity_score FROM tracks
    WHERE (artist = ? OR album_artist = ?)  -- Use canonical_artist
    AND album_type NOT LIKE '%ep%'
    AND album_type NOT LIKE '%compilation%'
    AND is_live = 0
    AND is_alternate_version = 0
""", (canonical_artist, canonical_artist))
```

### Pattern 3: Aggregate Album Stats (Rule 7)

```python
# Get all tracks on album, compute statistics
cursor.execute("""
    SELECT popularity_score FROM tracks
    WHERE album = ? AND album_artist = ?
""", (album, album_artist))

scores = [row[0] for row in cursor.fetchall()]
median = statistics.median(scores) if scores else 0
stdev = statistics.stdev(scores) if len(scores) > 1 else 0
```

---

## Database Columns Reference

### Required Input Columns

| Column | Type | Used For |
|--------|------|----------|
| artist | TEXT | Identity resolution (Rule 2-3) |
| album_artist | TEXT | Identity resolution (Rule 1) |
| album | TEXT | Album stats aggregation (Rule 7) |
| album_type | TEXT | EP detection (Rule 5) |
| track_count | INTEGER | EP heuristic (3-6 = EP) (Rule 5) |
| is_live | BOOLEAN | Context weighting (Rule 6) |
| is_alternate_version | BOOLEAN | Context weighting (Rule 6) |
| is_compilation | BOOLEAN | Compilation detection (Rule 4) |
| popularity_score | REAL | Input to weighting |

### Output Columns (to populate)

| Column | Type | When Populated |
|--------|------|-----------------|
| canonical_artist | TEXT | After identity resolution (Rule 1-4) |
| is_guest | BOOLEAN | After identity resolution (Rule 3) |
| is_alias | BOOLEAN | After identity resolution (Rule 2) |
| is_compilation | BOOLEAN | After identity resolution (Rule 4) |
| album_z_score | REAL | After z-score calculation (Rule 7) |
| artist_z_score | REAL | After z-score calculation (Rule 7) |

---

## Logging Reference

Enable debug logging to see detailed processing:

```python
import logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger('artist_identity')

# Messages you should see:
# DEBUG: "Identity resolved for track_id: canonical=..., is_guest=..., is_alias=..."
# DEBUG: "Popularity context for track_id: is_ep=..., is_live=..."
# DEBUG: "Popularity scores for track_id: raw=..., weighted=..., album_z=..., artist_z=..."
# INFO: "Applying 7-step normalization to N tracks..."
# INFO: "Normalization complete. Updating database..."
```

---

## Error Handling

All functions return gracefully on errors:

```python
try:
    identity = resolver.resolve_identity(...)
except Exception as e:
    logger.error(f"Identity resolution failed: {e}")
    # Falls back to using artist as canonical_artist
    identity = ArtistIdentity(
        canonical_artist=artist,
        album_artist=None,
        track_artist=artist,
        is_alias=False,
        is_guest=False,
        is_compilation=False
    )
```

---

## Performance Tips

### Caching

```python
# Initialize once, reuse
resolver = ArtistIdentityResolver(conn)
calc = PopularityCalculator(conn)

# Process many tracks
for track in tracks:
    identity = resolver.resolve_identity(...)  # DB query
    context = calc.get_popularity_context(...)  # No DB query
    z_album, z_artist, _ = calc.calculate_zscore_with_context(...)
```

### Batch Processing

```python
# ~100-500ms per 100 tracks
normalized = apply_normalization_order(conn, all_tracks)

# vs individual processing
# ~10-20ms per track
```

### Database Indexes

Recommended indexes for performance:

```sql
CREATE INDEX idx_album_artist_album ON tracks(album_artist, album);
CREATE INDEX idx_artist_album ON tracks(artist, album);
CREATE INDEX idx_album_type ON tracks(album_type);
```

---

## Migration Checklist

- [ ] Add new columns to database (or run migration script)
- [ ] Import artist_identity.py in popularity.py
- [ ] Initialize resolvers at module level
- [ ] Update track popularity calculation (use identity + context)
- [ ] Test on small batch
- [ ] Run full scan
- [ ] Verify z-scores populated
- [ ] Update star rating algorithm
- [ ] Verify single detection still works
- [ ] Monitor logs for errors

---

## Support

For issues, check:

1. **Column not found error** → Run migration script
2. **Z-scores all zero** → Check artist_stats calculation or album stats aggregation
3. **Identity not resolving correctly** → Enable DEBUG logging, check album track query
4. **Singles detection broken** → Verify canonical_artist used, not raw artist
5. **Performance slow** → Check indexes, consider caching resolver

---

## Full Example

```python
from artist_identity import (
    ArtistIdentityResolver, 
    PopularityCalculator,
    apply_normalization_order
)
import sqlite3

# 1. Connect and initialize
conn = sqlite3.connect('app.db')
resolver = ArtistIdentityResolver(conn)
calc = PopularityCalculator(conn)

# 2. Get track data
cursor = conn.cursor()
cursor.execute("SELECT id, artist, album_artist, album, track_count, is_live, is_alternate_version, album_type, popularity_score FROM tracks WHERE id=?", (123,))
track = dict(cursor.fetchone())

# 3. Resolve identity
identity = resolver.resolve_identity(
    artist=track['artist'],
    album_artist=track['album_artist'],
    album=track['album'],
    track_count=track['track_count']
)

# 4. Get context
context = calc.get_popularity_context(
    album=track['album'],
    album_type=track['album_type'],
    track_count=track['track_count'],
    is_live=track['is_live'],
    is_alternate=track['is_alternate_version']
)

# 5. Calculate z-scores
album_z, artist_z, weighted = calc.calculate_zscore_with_context(
    popularity=track['popularity_score'],
    identity=identity,
    album=track['album'],
    canonical_artist=identity.canonical_artist,
    context=context
)

# 6. Determine star rating
if album_z >= 1.5 and artist_z >= 1.5:
    stars = 5
elif album_z >= 1.0 and artist_z >= 0.5:
    stars = 4
else:
    stars = max(1, int(album_z + artist_z + 1))

print(f"Track {track['id']}: {stars} stars (album_z={album_z:.2f}, artist_z={artist_z:.2f})")
```

