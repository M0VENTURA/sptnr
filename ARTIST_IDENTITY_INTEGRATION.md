# Integration Guide: artist_identity.py with popularity.py

## Overview

This guide provides step-by-step instructions for integrating the new `artist_identity.py` module into the existing `popularity.py` scanning pipeline. The integration ensures all popularity calculations follow the 7-point rule system.

## Phase 1: Import and Initialize

### Step 1.1: Add Imports to popularity.py

At the top of `popularity.py`, add:

```python
from artist_identity import (
    ArtistIdentityResolver,
    PopularityCalculator,
    apply_normalization_order
)
```

### Step 1.2: Initialize Resolvers at Module Level

After the existing client initialization in `popularity.py`:

```python
# Initialize at module level (after API clients)
_IDENTITY_RESOLVER = None
_POPULARITY_CALCULATOR = None

def _get_identity_resolver(conn):
    """Lazy initialize identity resolver"""
    global _IDENTITY_RESOLVER
    if _IDENTITY_RESOLVER is None:
        _IDENTITY_RESOLVER = ArtistIdentityResolver(conn)
    return _IDENTITY_RESOLVER

def _get_popularity_calculator(conn):
    """Lazy initialize popularity calculator"""
    global _POPULARITY_CALCULATOR
    if _POPULARITY_CALCULATOR is None:
        _POPULARITY_CALCULATOR = PopularityCalculator(conn)
    return _POPULARITY_CALCULATOR
```

---

## Phase 2: Update Single Track Popularity Calculation

### Step 2.1: Identify the Main Popularity Calculation Function

Locate the function that calculates popularity for a single track. This is likely:
- `calculate_track_popularity(track, ...)`
- `scan_track_popularity(track, ...)`
- Or similar within the main`popularity_scan()` function

### Step 2.2: Add Identity Resolution

Within this function, after fetching track metadata, add:

```python
# NEW: Resolve artist identity
identity_resolver = _get_identity_resolver(conn)
identity = identity_resolver.resolve_identity(
    artist=track.get('artist', ''),
    album_artist=track.get('album_artist', ''),
    album=track.get('album', ''),
    track_count=track.get('track_count', 0),
    is_compilation=track.get('is_compilation', False)
)

# Log identity resolution (debug level)
logger.debug(
    f"Identity resolved for {track['id']}: "
    f"canonical={identity.canonical_artist}, "
    f"is_guest={identity.is_guest}, "
    f"is_alias={identity.is_alias}, "
    f"is_compilation={identity.is_compilation}"
)
```

### Step 2.3: Use Canonical Artist for Lookups

When querying Spotify, Last.fm, etc., use `identity.canonical_artist` instead of raw artist:

```python
# OLD: spotify_client.get_artist(track['artist'])
# NEW: 
popularity_data = spotify_client.get_artist(identity.canonical_artist)
```

### Step 2.4: Add Popularity Context

After fetching base popularity, calculate context:

```python
# NEW: Get popularity context
popularity_calc = _get_popularity_calculator(conn)
context = popularity_calc.get_popularity_context(
    album=track.get('album', ''),
    album_type=track.get('album_type', ''),
    track_count=track.get('track_count', 0),
    is_live=track.get('is_live', False),
    is_alternate=track.get('is_alternate_version', False)
)

logger.debug(
    f"Popularity context for {track['id']}: "
    f"is_ep={context.is_ep}, "
    f"is_live={context.is_live}, "
    f"is_alternate={context.is_alternate}"
)
```

### Step 2.5: Apply Weighting and Z-Scores

Replace existing popularity calculation with new weighted version:

```python
# OLD:
# popularity_score = spotify_popularity
# album_z = (popularity_score - album_median) / album_stddev

# NEW:
album_z_score, artist_z_score, weighted_popularity = (
    popularity_calc.calculate_zscore_with_context(
        popularity=spotify_popularity,
        identity=identity,
        album=track.get('album', ''),
        canonical_artist=identity.canonical_artist,
        context=context
    )
)

logger.debug(
    f"Popularity scores for {track['id']}: "
    f"raw={spotify_popularity}, "
    f"weighted={weighted_popularity:.1f}, "
    f"album_z={album_z_score:.2f}, "
    f"artist_z={artist_z_score:.2f}"
)
```

### Step 2.6: Store New Fields

When updating the track in the database, save new fields:

```python
# NEW fields to store
update_data = {
    'album_z_score': album_z_score,
    'artist_z_score': artist_z_score,
    'popularity_score': weighted_popularity,  # Or keep raw, use weighted for calculations
    'canonical_artist': identity.canonical_artist,
    'is_guest': identity.is_guest,
    'is_alias': identity.is_alias,
    'is_compilation': identity.is_compilation,
    # ... existing fields ...
}

cursor.execute("""
    UPDATE tracks SET
        album_z_score = ?,
        artist_z_score = ?,
        popularity_score = ?,
        canonical_artist = ?,
        is_guest = ?,
        is_alias = ?,
        is_compilation = ?
    WHERE id = ?
""", (
    album_z_score,
    artist_z_score,
    weighted_popularity,
    identity.canonical_artist,
    identity.is_guest,
    identity.is_alias,
    identity.is_compilation,
    track['id']
))
```

---

## Phase 3: Batch Processing Integration

### Step 3.1: Locate Batch Popularity Scan

Find the function that scans multiple tracks, likely:
- `popularity_scan()`
- Main scan loop or generator

### Step 3.2: Apply Normalization Order to Batch

After collecting all tracks and before updating database:

```python
# NEW: Apply 7-step normalization to entire batch
try:
    all_tracks = list(tracks_to_process)  # Materialize generator if needed
    
    logger.info(
        f"Applying 7-step normalization to {len(all_tracks)} tracks..."
    )
    
    normalized_tracks = apply_normalization_order(
        conn=conn,
        tracks=all_tracks,
        log_level=logging.DEBUG
    )
    
    logger.info(
        f"Normalization complete. Updating database..."
    )
    
    # Update database with normalized results
    for track in normalized_tracks:
        cursor.execute("""
            UPDATE tracks SET
                canonical_artist = ?,
                is_guest = ?,
                is_alias = ?,
                is_compilation = ?,
                album_z_score = ?,
                artist_z_score = ?,
                metadata_single = ?
            WHERE id = ?
        """, (
            track['canonical_artist'],
            track['is_guest'],
            track['is_alias'],
            track['is_compilation'],
            track['album_z_score'],
            track['artist_z_score'],
            track.get('metadata_single', False),
            track['id']
        ))
    
    conn.commit()

except Exception as e:
    logger.error(f"Error in normalization order: {e}")
    logger.exception(e)
    conn.rollback()
    raise
```

---

## Phase 4: Update Single Detection

### Step 4.1: Update advanced_single_detection.py

At the top, add imports:

```python
from artist_identity import ArtistIdentityResolver, PopularityCalculator
```

### Step 4.2: Modify detect_single_advanced()

Replace the existing artist lookup with identity-aware lookup:

```python
def detect_single_advanced(track, conn, metadata_override=None):
    """Detect if track is a single using identity-aware statistics"""
    
    # NEW: Resolve identity first
    identity_resolver = ArtistIdentityResolver(conn)
    identity = identity_resolver.resolve_identity(
        artist=track.get('artist', ''),
        album_artist=track.get('album_artist', ''),
        album=track.get('album', ''),
        track_count=track.get('track_count', 0),
        is_compilation=track.get('is_compilation', False)
    )
    
    # Use canonical_artist for all queries
    canonical_artist = identity.canonical_artist
    
    # NEW: Get context for weighting
    pop_calc = PopularityCalculator(conn)
    context = pop_calc.get_popularity_context(
        album=track.get('album', ''),
        album_type=track.get('album_type', ''),
        track_count=track.get('track_count', 0),
        is_live=track.get('is_live', False),
        is_alternate=track.get('is_alternate_version', False)
    )
    
    # Apply weighting
    weighted_popularity = pop_calc.weight_popularity(
        popularity=track.get('popularity_score', 0),
        identity=identity,
        context=context
    )
    
    # Query artist catalogue using canonical_artist
    cursor = conn.cursor()
    cursor.execute("""
        SELECT popularity_score FROM tracks
        WHERE (artist = ? OR album_artist = ?)
        AND album_type NOT LIKE '%ep%'
        AND is_live = 0
        AND is_alternate_version = 0
    """, (canonical_artist, canonical_artist))
    
    artist_scores = [row[0] for row in cursor.fetchall()]
    
    # Rest of detection logic using artist_scores
    # ...
```

---

## Phase 5: Database Schema Updates

### Step 5.1: Create Migration Script

Create `migrations/add_identity_columns.py`:

```python
#!/usr/bin/env python3
"""Migration: Add artist identity and popularity weighting columns"""

import sqlite3
import sys

def migrate(db_path):
    """Add new columns to tracks table"""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    new_columns = {
        'canonical_artist': 'TEXT',
        'is_guest': 'INTEGER DEFAULT 0',
        'is_alias': 'INTEGER DEFAULT 0',
        'is_compilation': 'INTEGER DEFAULT 0',
        'album_z_score': 'REAL',
        'artist_z_score': 'REAL',
    }
    
    for col_name, col_type in new_columns.items():
        try:
            cursor.execute(f"ALTER TABLE tracks ADD COLUMN {col_name} {col_type}")
            print(f"✓ Added column: {col_name}")
        except sqlite3.OperationalError as e:
            if "duplicate column" in str(e):
                print(f"~ Column exists: {col_name}")
            else:
                print(f"✗ Error adding {col_name}: {e}")
                raise
    
    conn.commit()
    conn.close()
    print("Migration complete!")

if __name__ == '__main__':
    db_path = sys.argv[1] if len(sys.argv) > 1 else 'app.db'
    migrate(db_path)
```

### Step 5.2: Run Migration

```bash
python migrations/add_identity_columns.py
```

---

## Phase 6: Update Star Rating Algorithm

### Step 6.1: Modify Star Rating Calculation

Assuming a `calculate_star_rating()` function exists:

```python
def calculate_star_rating(track, conn):
    """Calculate star rating using weighted z-scores"""
    
    # Use z-scores already calculated during popularity scan
    album_z = track.get('album_z_score', 0.0)
    artist_z = track.get('artist_z_score', 0.0)
    
    # Stars based on z-scores (rule of thumb)
    # 5 stars: album_z >= 1.5 AND artist_z >= 1.5
    # 4 stars: album_z >= 1.0 AND artist_z >= 0.5
    # 3 stars: album_z >= 0.5 OR artist_z >= 0.5
    # 2 stars: album_z >= 0.0 OR artist_z >= 0.0
    # 1 star: default
    
    if album_z >= 1.5 and artist_z >= 1.5:
        return 5
    elif album_z >= 1.0 and artist_z >= 0.5:
        return 4
    elif album_z >= 0.5 or artist_z >= 0.5:
        return 3
    elif album_z >= 0.0 or artist_z >= 0.0:
        return 2
    else:
        return 1
```

---

## Phase 7: Validation and Testing

### Step 7.1: Verify Schema

```python
# Run this after migration
def verify_schema(conn):
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(tracks)")
    columns = {row[1]: row[2] for row in cursor.fetchall()}
    
    required = [
        'canonical_artist', 'is_guest', 'is_alias', 
        'is_compilation', 'album_z_score', 'artist_z_score'
    ]
    
    for col in required:
        if col not in columns:
            print(f"✗ Missing column: {col}")
            return False
        print(f"✓ Column exists: {col} ({columns[col]})")
    
    return True
```

### Step 7.2: Test Edge Cases

```python
def test_identity_resolution(conn):
    """Test identity resolution with known cases"""
    resolver = ArtistIdentityResolver(conn)
    test_cases = [
        # (artist, album_artist, album, expected_canonical, expected_is_alias)
        ("Pink Floyd", "Pink Floyd", "Dark Side", "Pink Floyd", False),
        ("The Pink Floyd Sound", "Pink Floyd", "Piper", "Pink Floyd", True),
        ("Taylor Swift", "Taylor Swift", "Folklore", "Taylor Swift", False),
        ("Various Artists", "Various Artists", "Compilation", "Various Artists", False),
    ]
    
    for artist, album_artist, album, exp_canonical, exp_alias in test_cases:
        identity = resolver.resolve_identity(
            artist=artist,
            album_artist=album_artist,
            album=album,
            track_count=10
        )
        
        status = "✓" if (
            identity.canonical_artist == exp_canonical and
            identity.is_alias == exp_alias
        ) else "✗"
        
        print(f"{status} {artist} → {identity.canonical_artist} (alias={identity.is_alias})")
```

### Step 7.3: Sample Scan Test

```python
# Run a small test scan on a subset of tracks
def test_scan(conn, limit=10):
    cursor = conn.cursor()
    cursor.execute("SELECT id, artist, album_artist, album FROM tracks LIMIT ?", (limit,))
    tracks = cursor.fetchall()
    
    resolver = ArtistIdentityResolver(conn)
    calc = PopularityCalculator(conn)
    
    for track_id, artist, album_artist, album in tracks:
        identity = resolver.resolve_identity(
            artist=artist,
            album_artist=album_artist,
            album=album,
            track_count=0  # Simplified for testing
        )
        print(f"Track {track_id}: {artist} → {identity.canonical_artist}")
```

---

## Phase 8: Deployment Checklist

- [ ] Backup database before migration
- [ ] Run migration script on test database
- [ ] Verify schema with `verify_schema()` test
- [ ] Run identity resolution tests
- [ ] Run small sample scan test
- [ ] Review logs for any errors
- [ ] Full scan on production database
- [ ] Verify z-scores populated correctly
- [ ] Check star ratings updated appropriately
- [ ] Validate single detection still working

---

## Rollback Plan

If issues arise:

1. Restore database from backup
2. Comment out new imports in `popularity.py` and `advanced_single_detection.py`
3. Revert to previous code path
4. Use `populate_from_backup()` to restore old values

---

## Performance Monitoring

After integration, monitor:

```python
# Log performance metrics
import time

start = time.time()
identity = resolver.resolve_identity(...)
elapsed = (time.time() - start) * 1000
logger.debug(f"Identity resolution: {elapsed:.1f}ms")

# Per-batch timing
start = time.time()
normalized = apply_normalization_order(conn, batch)
elapsed = (time.time() - start)
logger.info(f"Normalized {len(batch)} tracks in {elapsed:.2f}s ({elapsed/len(batch)*1000:.1f}ms/track)")
```

Expected performance:
- Identity resolution: 1-2ms per track
- Popularity weighting: <1ms per track
- Z-score calculation: 1-3ms per track
- Full normalization: 100-500ms per 100 tracks

---

## Troubleshooting

### Issue: "Column canonical_artist already exists"
**Solution**: Column was added manually. Remove duplicates from new_columns dict.

### Issue: Very slow identity resolution
**Solution**: Add index on (album_artist, artist) columns for faster lookups.

### Issue: Z-scores all zero
**Solution**: Check that album/artist statistics are being calculated. Review logs for errors in `calculate_album_stats()` / `calculate_artist_stats()`.

### Issue: Singles detection not working
**Solution**: Verify `advanced_single_detection.py` integrated correctly. Check that canonical_artist is being used in queries.
