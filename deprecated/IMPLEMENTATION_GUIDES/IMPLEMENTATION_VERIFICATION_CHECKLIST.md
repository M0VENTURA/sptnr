# Implementation Verification Checklist

## Pre-Integration Verification

Before integrating artist_identity.py with popularity.py, verify these items:

### 1. Code Files

- [ ] `artist_identity.py` exists at workspace root
- [ ] File is 327 lines (or close)
- [ ] No syntax errors: `python -m py_compile artist_identity.py`
- [ ] Can import module: `python -c "from artist_identity import *"`

Verify command:
```bash
python -m py_compile artist_identity.py && echo "✓ Syntax OK"
```

### 2. Database Schema

- [ ] `app.db` contains `tracks` table
- [ ] Has column: `artist` (TEXT)
- [ ] Has column: `album_artist` (TEXT)
- [ ] Has column: `album` (TEXT)
- [ ] Has column: `album_type` (TEXT)
- [ ] Has column: `track_count` (INTEGER)
- [ ] Has column: `is_live` (INTEGER/BOOLEAN)
- [ ] Has column: `is_alternate_version` (INTEGER/BOOLEAN)
- [ ] Has column: `is_compilation` (INTEGER/BOOLEAN)
- [ ] Has column: `popularity_score` (REAL/INTEGER)

Verify command:
```python
import sqlite3
conn = sqlite3.connect('app.db')
cursor = conn.cursor()
cursor.execute("PRAGMA table_info(tracks)")
columns = {row[1]: row[2] for row in cursor.fetchall()}
required = ['artist', 'album_artist', 'album', 'album_type', 'track_count', 
            'is_live', 'is_alternate_version', 'is_compilation', 'popularity_score']
for col in required:
    print(f"{'✓' if col in columns else '✗'} {col}: {columns.get(col, 'MISSING')}")
```

### 3. Documentation Files

- [ ] `ARTIST_IDENTITY_RULES.md` exists (500+ lines)
- [ ] `ARTIST_IDENTITY_INTEGRATION.md` exists (550+ lines)
- [ ] `ARTIST_IDENTITY_QUICK_REFERENCE.md` exists (400+ lines)
- [ ] `ARTIST_IDENTITY_IMPLEMENTATION_SUMMARY.md` exists

### 4. Sample Data Availability

Have sample tracks ready for testing:
- [ ] At least 1 normal artist album (artist == album_artist)
- [ ] At least 1 album with historical alias (Pink Floyd / The Pink Floyd Sound)
- [ ] At least 1 Various Artists compilation
- [ ] At least 1 EP (3-6 tracks)
- [ ] At least 1 track with guest artist

---

## Integration Step Verification

### Phase 1: Import and Initialize

After adding imports to popularity.py:

```python
python -c "
from artist_identity import ArtistIdentityResolver, PopularityCalculator
from popularity import _get_identity_resolver, _get_popularity_calculator
print('✓ Imports successful')
"
```

- [ ] No import errors
- [ ] No circular imports
- [ ] Module-level initialization works

### Phase 2: Single Track Calculation

Test with sample track:

```python
import sqlite3
from artist_identity import ArtistIdentityResolver, PopularityCalculator

conn = sqlite3.connect('app.db')

# Get a test track
cursor = conn.cursor()
cursor.execute("""
    SELECT id, artist, album_artist, album, track_count, 
           is_live, is_alternate_version, album_type, popularity_score 
    FROM tracks LIMIT 1
""")
track = cursor.fetchone()

if track:
    track_dict = {
        'id': track[0],
        'artist': track[1],
        'album_artist': track[2],
        'album': track[3],
        'track_count': track[4],
        'is_live': track[5],
        'is_alternate_version': track[6],
        'album_type': track[7],
        'popularity_score': track[8]
    }
    
    # Test identity resolution
    resolver = ArtistIdentityResolver(conn)
    identity = resolver.resolve_identity(
        artist=track_dict['artist'],
        album_artist=track_dict['album_artist'],
        album=track_dict['album'],
        track_count=track_dict['track_count']
    )
    
    print(f"✓ Track {track_dict['id']}")
    print(f"  Artist: {track_dict['artist']} → {identity.canonical_artist}")
    print(f"  Is guest: {identity.is_guest}")
    print(f"  Is alias: {identity.is_alias}")
    
    # Test context
    calc = PopularityCalculator(conn)
    context = calc.get_popularity_context(
        album=track_dict['album'],
        album_type=track_dict['album_type'],
        track_count=track_dict['track_count'],
        is_live=track_dict['is_live'],
        is_alternate=track_dict['is_alternate_version']
    )
    
    print(f"  Is EP: {context.is_ep}")
    print(f"  Is live: {context.is_live}")
    
    # Test z-score calculation
    album_z, artist_z, weighted = calc.calculate_zscore_with_context(
        popularity=track_dict['popularity_score'],
        identity=identity,
        album=track_dict['album'],
        canonical_artist=identity.canonical_artist,
        context=context
    )
    
    print(f"  Album z-score: {album_z:.2f}")
    print(f"  Artist z-score: {artist_z:.2f}")
    print(f"  Weighted popularity: {weighted:.1f}")
    print('✓ All calculations successful')
else:
    print('✗ No tracks found in database')

conn.close()
```

- [ ] No errors during execution
- [ ] canonical_artist populated
- [ ] Z-scores calculated
- [ ] Weighted popularity computed

### Phase 3: Batch Processing

Test on small batch:

```python
import sqlite3
from artist_identity import apply_normalization_order
import logging

logging.basicConfig(level=logging.DEBUG, format='%(name)s: %(message)s')

conn = sqlite3.connect('app.db')
cursor = conn.cursor()

# Get first 10 tracks
cursor.execute("""
    SELECT id, artist, album_artist, album, track_count, 
           is_live, is_alternate_version, album_type, popularity_score 
    FROM tracks LIMIT 10
""")

tracks = []
for row in cursor.fetchall():
    tracks.append({
        'id': row[0],
        'artist': row[1],
        'album_artist': row[2],
        'album': row[3],
        'track_count': row[4],
        'is_live': row[5],
        'is_alternate_version': row[6],
        'album_type': row[7],
        'popularity_score': row[8]
    })

print(f"Processing {len(tracks)} tracks...")

try:
    normalized = apply_normalization_order(conn, tracks)
    print(f"✓ Successfully normalized {len(normalized)} tracks")
    for track in normalized:
        if track.get('id') == tracks[0]['id']:  # Show first track
            print(f"  First track z-scores: album={track.get('album_z_score', 'N/A')}, artist={track.get('artist_z_score', 'N/A')}")
except Exception as e:
    print(f"✗ Error during normalization: {e}")
    import traceback
    traceback.print_exc()

conn.close()
```

- [ ] No errors during batch processing
- [ ] All tracks processed
- [ ] Z-scores populated in results

### Phase 4: Database Update

Test column creation and update:

```python
import sqlite3

conn = sqlite3.connect('app.db')
cursor = conn.cursor()

# Check if new columns exist
cursor.execute("PRAGMA table_info(tracks)")
columns = {row[1]: row[2] for row in cursor.fetchall()}

new_cols = ['canonical_artist', 'is_guest', 'is_alias', 'is_compilation', 
            'album_z_score', 'artist_z_score']

# Add missing columns
for col in new_cols:
    if col not in columns:
        col_type = 'TEXT' if 'artist' in col else 'REAL' if 'z_score' in col else 'INTEGER'
        try:
            cursor.execute(f"ALTER TABLE tracks ADD COLUMN {col} {col_type}")
            print(f"✓ Added column: {col}")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e):
                print(f"✗ Error adding {col}: {e}")
    else:
        print(f"~ Column exists: {col}")

conn.commit()

# Test update with sample data
cursor.execute("""
    UPDATE tracks SET
        canonical_artist = artist,
        is_guest = 0,
        is_alias = 0,
        is_compilation = 0,
        album_z_score = 0.0,
        artist_z_score = 0.0
    WHERE id = (SELECT id FROM tracks LIMIT 1)
""")

updated = cursor.rowcount
print(f"✓ Updated {updated} row(s)")

conn.commit()
conn.close()
```

- [ ] New columns created successfully
- [ ] Update query works
- [ ] Rows updated correctly

---

## Testing Edge Cases

### Test 1: Historical Alias Detection

```python
from artist_identity import ArtistIdentityResolver
import sqlite3

conn = sqlite3.connect('app.db')
resolver = ArtistIdentityResolver(conn)

# Find an album with multiple tracks from same artist
cursor = conn.cursor()
cursor.execute("""
    SELECT album, artist, album_artist, COUNT(*) as cnt
    FROM tracks
    GROUP BY album, artist
    HAVING cnt > 2
    LIMIT 1
""")

result = cursor.fetchone()
if result:
    album, artist, album_artist, cnt = result
    identity = resolver.resolve_identity(
        artist=artist,
        album_artist=album_artist,
        album=album,
        track_count=cnt
    )
    print(f"✓ Album: {album}")
    print(f"  Track artist: {artist}")
    print(f"  Album artist: {album_artist}")
    print(f"  Resolved to: {identity.canonical_artist}")
    print(f"  Is alias: {identity.is_alias}")
else:
    print("~ No multi-track albums found for testing")

conn.close()
```

- [ ] Test executes without error
- [ ] Alias flag properly set

### Test 2: Various Artists Compilation

```python
from artist_identity import ArtistIdentityResolver
import sqlite3

conn = sqlite3.connect('app.db')
resolver = ArtistIdentityResolver(conn)

# Check for Various Artists albums
cursor = conn.cursor()
cursor.execute("""
    SELECT album, artist, album_artist
    FROM tracks
    WHERE album_artist = 'Various Artists'
    LIMIT 1
""")

result = cursor.fetchone()
if result:
    album, artist, album_artist = result
    identity = resolver.resolve_identity(
        artist=artist,
        album_artist=album_artist,
        album=album,
        track_count=0,
        is_compilation=True
    )
    print(f"✓ Compilation detected")
    print(f"  Album: {album}")
    print(f"  Track artist: {artist}")
    print(f"  Is compilation: {identity.is_compilation}")
else:
    print("~ No Various Artists albums found for testing")

conn.close()
```

- [ ] Test executes without error
- [ ] is_compilation flag properly set

### Test 3: EP Detection

```python
from artist_identity import PopularityCalculator
import sqlite3

conn = sqlite3.connect('app.db')
calc = PopularityCalculator(conn)

# Find albums with 3-6 tracks (EP heuristic)
cursor = conn.cursor()
cursor.execute("""
    SELECT album, COUNT(*) as cnt
    FROM tracks
    GROUP BY album
    HAVING cnt BETWEEN 3 AND 6
    LIMIT 1
""")

result = cursor.fetchone()
if result:
    album, track_count = result
    context = calc.get_popularity_context(
        album=album,
        album_type='album',  # Not explicitly marked as EP
        track_count=track_count,
        is_live=False,
        is_alternate=False
    )
    print(f"✓ EP heuristic test")
    print(f"  Album: {album}")
    print(f"  Track count: {track_count}")
    print(f"  Detected as EP: {context.is_ep}")
else:
    print("~ No albums with 3-6 tracks found for testing")

conn.close()
```

- [ ] Test executes without error
- [ ] is_ep flag properly set based on heuristic

### Test 4: Weighting Calculation

```python
from artist_identity import PopularityCalculator, ArtistIdentity, PopularityContext
import sqlite3

conn = sqlite3.connect('app.db')
calc = PopularityCalculator(conn)

# Create test data
identity_normal = ArtistIdentity(
    canonical_artist="Test",
    album_artist="Test",
    track_artist="Test",
    is_alias=False,
    is_guest=False,
    is_compilation=False
)

identity_guest = ArtistIdentity(
    canonical_artist="Test",
    album_artist="Test",
    track_artist="Test feat. Guest",
    is_alias=False,
    is_guest=True,
    is_compilation=False
)

context_normal = PopularityContext(
    is_ep=False,
    album_type="album",
    track_count=10,
    is_live=False,
    is_alternate=False
)

context_ep = PopularityContext(
    is_ep=True,
    album_type="ep",
    track_count=5,
    is_live=False,
    is_alternate=False
)

popularity = 100.0

# Test combinations
tests = [
    (identity_normal, context_normal, "Normal track", 1.0),
    (identity_guest, context_normal, "Guest track", 0.9),
    (identity_normal, context_ep, "EP track", 0.8),
    (identity_guest, context_ep, "Guest EP", 0.72),
]

for identity, context, label, expected_mult in tests:
    weighted = calc.weight_popularity(
        popularity=popularity,
        identity=identity,
        context=context
    )
    actual_mult = weighted / popularity
    match = f"✓" if abs(actual_mult - expected_mult) < 0.01 else "✗"
    print(f"{match} {label}: {weighted:.1f} (expected {expected_mult*100:.0f}%)")

conn.close()
```

- [ ] All weighting calculations correct
- [ ] Multiplicative effects working

---

## Performance Baseline

Before production deployment, establish baseline:

```python
import sqlite3
from artist_identity import ArtistIdentityResolver, PopularityCalculator, apply_normalization_order
import time

conn = sqlite3.connect('app.db')
cursor = conn.cursor()

# Get sample tracks
cursor.execute("SELECT COUNT(*) FROM tracks")
total_tracks = cursor.fetchone()[0]
print(f"Total tracks in database: {total_tracks}")

# Get 100 sample tracks
cursor.execute("""
    SELECT id, artist, album_artist, album, track_count, 
           is_live, is_alternate_version, album_type, popularity_score 
    FROM tracks LIMIT 100
""")

tracks = []
for row in cursor.fetchall():
    tracks.append({
        'id': row[0],
        'artist': row[1],
        'album_artist': row[2],
        'album': row[3],
        'track_count': row[4],
        'is_live': row[5],
        'is_alternate_version': row[6],
        'album_type': row[7],
        'popularity_score': row[8]
    })

# Benchmark identity resolution
resolver = ArtistIdentityResolver(conn)
start = time.time()
for track in tracks:
    _ = resolver.resolve_identity(
        artist=track['artist'],
        album_artist=track['album_artist'],
        album=track['album'],
        track_count=track['track_count']
    )
identity_time = (time.time() - start) * 1000  # in milliseconds

print(f"\nIdentity Resolution:")
print(f"  100 tracks: {identity_time:.1f}ms")
print(f"  Per track: {identity_time/100:.2f}ms")

# Benchmark popularity calculation
calc = PopularityCalculator(conn)
start = time.time()
for track in tracks:
    context = calc.get_popularity_context(
        album=track['album'],
        album_type=track['album_type'],
        track_count=track['track_count'],
        is_live=track['is_live'],
        is_alternate=track['is_alternate_version']
    )
context_time = (time.time() - start) * 1000

print(f"\nContext Calculation:")
print(f"  100 tracks: {context_time:.1f}ms")
print(f"  Per track: {context_time/100:.2f}ms")

# Benchmark batch normalization
start = time.time()
_ = apply_normalization_order(conn, tracks)
batch_time = (time.time() - start) * 1000

print(f"\nBatch Normalization:")
print(f"  100 tracks: {batch_time:.1f}ms")
print(f"  Per track: {batch_time/100:.2f}ms")

print(f"\nTotal for 100 tracks: {identity_time + context_time + batch_time:.1f}ms")

# Extrapolate to full database
estimated_total = ((batch_time / 100) / 1000) * total_tracks
print(f"Estimated time for full database ({total_tracks} tracks): {estimated_total:.1f}s")

conn.close()
```

Record baseline:
- [ ] Identity resolution time per track: ___ms
- [ ] Context calculation time per track: ___ms
- [ ] Batch normalization time per track: ___ms
- [ ] Estimated full scan time: ___s

---

## Final Verification

Before going live:

- [ ] All verification tests pass (✓ 16/16)
- [ ] No error messages in logs
- [ ] Performance acceptable (<5ms per track)
- [ ] Database backups created
- [ ] Rollback plan tested
- [ ] Team notified of integration
- [ ] Monitoring in place for next scan

---

## Sign-off Checklist

Integration Quality Gate:

- [ ] Code review completed
- [ ] All tests passing
- [ ] Performance acceptable
- [ ] Documentation complete
- [ ] Team trained on implementation
- [ ] Backup and rollback plan verified
- [ ] Monitoring configured
- [ ] Ready for production

Integration Sign-off:
- Date: ___________
- Verified by: ___________
- Approved by: ___________

---

## Troubleshooting

### Issue: Import fails
**Check**:
1. File is in correct location: `c:\Script\Github\sptnr\artist_identity.py`
2. Python path includes workspace directory
3. No circular imports

### Issue: Database columns not found
**Check**:
1. Connected to correct database (app.db)
2. Schema verification script ran successfully
3. Migration script not having issues

### Issue: Z-scores all zeros
**Check**:
1. Album statistics being calculated
2. Artist statistics being calculated
3. Database queries returning results
4. Error logs for calculation errors

### Issue: Performance below baseline
**Check**:
1. Database not locked by other processes
2. Indexes created on (album_artist, album) columns
3. Query caching working
4. No other heavy workloads running

---

## Support Contact

For integration issues:
1. Check this checklist first
2. Review ARTIST_IDENTITY_INTEGRATION.md Phase 7 (Validation and Testing)
3. Check ARTIST_IDENTITY_QUICK_REFERENCE.md Support section
4. Contact: [team member responsible]
