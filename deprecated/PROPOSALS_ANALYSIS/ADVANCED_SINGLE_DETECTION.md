# Advanced Single Detection - Implementation Guide

## Overview

This document describes the advanced single detection system implemented in SPTNR that provides comprehensive and accurate single detection using multiple data sources and sophisticated matching algorithms.

## Key Features

The advanced single detection system implements 8 core rules:

### 1. Track Version Matching

**ISRC-based matching (primary):**
- Matches tracks across different releases using International Standard Recording Code (ISRC)
- Most reliable method as ISRC uniquely identifies recordings

**Title + Duration matching (fallback):**
- When ISRC is not available, matches by normalized title and duration (±2 seconds tolerance)
- Handles variations in encoding and metadata

### 2. Alternate Version Filtering

Automatically excludes tracks with these patterns:
- `(remix)`, `(orchestral)`, `(acoustic)`
- `(demo)`, `(instrumental)`, `(karaoke)`
- `(radio edit)`, `(edit)`, `(extended)`, `(club mix)`
- `(alternate)`, `(alt version)`
- `(re-recorded)`, `(re-recording)`, `(cover)`

### 3. Live/Unplugged Context Handling

**Smart context-aware matching:**
- If current album is live/unplugged → only match with other live/unplugged versions
- If current album is NOT live/unplugged → exclude all live/unplugged versions

**Detection patterns:**
- Track titles: `(live)`, `(unplugged)`
- Album names: `Live at...`, `MTV Unplugged`, etc.

### 4. Album Release Deduplication

**Groups releases by normalized album identity:**
- Same album title (case-insensitive, punctuation removed)
- Same track titles in same order (ignoring suffixes)

**Treats as ONE logical album:**
- Remasters (e.g., "Album (Remastered)")
- Reissues (e.g., "Album [Deluxe Edition]")
- Regional variants (e.g., "Album [US Release]")

### 5. Metadata Single Status

A track is considered a **metadata single** if:
- Has a Spotify single release, OR
- Appears in a MusicBrainz release group of type "single"

### 6. Global Popularity Calculation

**Computed across all matched versions:**
1. Find all versions of the same song (using ISRC or title+duration)
2. Filter out alternate versions
3. Take the **maximum popularity** across remaining canonical versions
4. Use this global popularity for z-score calculation

**Example:**
```
Version 1 (Single Release): popularity = 80
Version 2 (Album Release):  popularity = 60
Version 3 (Remix):         popularity = 90 (excluded as alternate)
→ Global popularity = max(80, 60) = 80
```

### 7. Z-Score Based Final Determination

**Dual-condition requirement:**

A track is marked as a **single** ONLY if BOTH conditions are true:
1. `(isSpotifySingle OR isMusicBrainzSingle)` - Metadata confirms it's a single
2. `zscore >= 0.20` - Track is significantly more popular than album average

**Z-score calculation:**
```python
zscore = (global_popularity - album_mean) / album_stddev
```

**Default threshold: 0.20**
- Configurable via `zscore_threshold` parameter
- Higher values = more strict (fewer singles detected)
- Lower values = more lenient (more singles detected)

### 8. Compilation/Greatest Hits Handling

**Special rules for compilations:**

**Detection:**
- Album type = "compilation" (from Spotify), OR
- Album name contains: "Greatest Hits", "Best of", "Collection", "Anthology", "Essentials"

**Behavior:**
- Uses **album-version popularity** only (not global popularity)
- Only detects singles **released FROM the compilation**
- Does not treat historical singles as singles for this album
- Prevents all tracks on a greatest hits album from being marked as singles

## Database Schema

### New Fields Added

```sql
-- Track matching and popularity
global_popularity REAL          -- Max popularity across all track versions
zscore REAL                      -- Z-score within album (for single detection)

-- Single detection metadata
metadata_single INTEGER          -- 1 if Spotify OR MusicBrainz confirms single
is_compilation INTEGER           -- 1 if album is compilation/greatest hits

-- Existing fields used
isrc TEXT                        -- International Standard Recording Code
duration REAL                    -- Track duration in seconds
spotify_album_type TEXT          -- Album type from Spotify API
is_spotify_single INTEGER        -- 1 if track has Spotify single release
source_musicbrainz_single INTEGER -- 1 if MusicBrainz confirms single
```

## API Reference

### Core Functions

#### `detect_single_advanced()`

Main detection function implementing all 8 rules.

```python
def detect_single_advanced(
    conn: sqlite3.Connection,
    track_id: str,
    title: str,
    artist: str,
    album: str,
    isrc: Optional[str],
    duration: Optional[float],
    popularity: float,
    album_type: Optional[str],
    zscore_threshold: float = 0.20,
    verbose: bool = False
) -> Dict
```

**Returns:**
```python
{
    'is_single': bool,              # Final single determination
    'confidence': str,              # 'high', 'medium', or 'low'
    'sources': List[str],           # ['spotify', 'musicbrainz', 'popularity_zscore']
    'global_popularity': float,     # Max popularity across versions
    'zscore': float,                # Z-score within album
    'metadata_single': bool,        # Metadata single status
    'is_compilation': bool          # Compilation album flag
}
```

#### `batch_update_advanced_singles()`

Batch process all tracks in database.

```python
def batch_update_advanced_singles(
    conn: sqlite3.Connection,
    artist: Optional[str] = None,
    album: Optional[str] = None,
    zscore_threshold: float = 0.20,
    verbose: bool = False
) -> int  # Returns number of tracks updated
```

### Helper Functions

#### Track Matching
- `find_matching_versions()` - Find all versions of same song
- `normalize_title()` - Normalize title for comparison
- `normalize_album_identity()` - Create album fingerprint

#### Version Filtering
- `is_alternate_version()` - Check if track is alternate version
- `is_live_version()` - Check if track/album is live/unplugged
- `is_compilation_album()` - Check if album is compilation

#### Popularity & Scoring
- `calculate_global_popularity()` - Max popularity across versions
- `calculate_zscore()` - Z-score within album
- `is_metadata_single()` - Check metadata single status

## Integration with Existing Code

### In `popularity.py`

The `detect_single_for_track()` function has been enhanced:

```python
def detect_single_for_track(
    title: str,
    artist: str,
    # ... existing parameters ...
    
    # NEW: Advanced detection parameters
    track_id: str = None,
    album: str = None,
    isrc: str = None,
    duration: float = None,
    popularity: float = None,
    album_type: str = None,
    use_advanced_detection: bool = True,
    zscore_threshold: float = 0.20
) -> dict
```

**Backwards compatible:**
- If `use_advanced_detection=False` or required params missing → uses standard detection
- Falls back to standard detection on errors

### In `popularity_scan()`

The scan now passes additional parameters:

```python
detection_result = detect_single_for_track(
    title=title,
    artist=artist,
    # ... existing parameters ...
    
    # Advanced detection
    track_id=track_id,
    album=album,
    isrc=track_isrc,
    duration=track_duration,
    popularity=track_popularity,
    album_type=track_album_type,
    use_advanced_detection=True,
    zscore_threshold=0.20
)
```

## Usage Examples

### Example 1: Single Track Detection

```python
from advanced_single_detection import detect_single_advanced
import sqlite3

conn = sqlite3.connect('sptnr.db')

result = detect_single_advanced(
    conn=conn,
    track_id="abc123",
    title="Hit Song",
    artist="Artist Name",
    album="Album Name",
    isrc="USXXX1234567",
    duration=180.5,
    popularity=75.0,
    album_type="album",
    zscore_threshold=0.20,
    verbose=True
)

print(f"Is Single: {result['is_single']}")
print(f"Confidence: {result['confidence']}")
print(f"Sources: {result['sources']}")
print(f"Z-score: {result['zscore']:.3f}")
```

### Example 2: Batch Update Artist

```python
from advanced_single_detection import batch_update_advanced_singles
import sqlite3

conn = sqlite3.connect('sptnr.db')

# Update all tracks for an artist
num_updated = batch_update_advanced_singles(
    conn=conn,
    artist="Artist Name",
    zscore_threshold=0.20,
    verbose=True
)

print(f"Updated {num_updated} tracks")
conn.commit()
conn.close()
```

### Example 3: Custom Threshold

```python
# More strict detection (fewer singles)
result = detect_single_advanced(
    # ... parameters ...
    zscore_threshold=0.50,  # Higher threshold
)

# More lenient detection (more singles)
result = detect_single_advanced(
    # ... parameters ...
    zscore_threshold=0.10,  # Lower threshold
)
```

## Testing

Run the comprehensive test suite:

```bash
python3 test_advanced_single_detection.py
```

**Test coverage:**
- Title normalization
- Alternate version detection
- Live/unplugged detection
- Z-score calculation
- Compilation detection
- Global popularity calculation
- Metadata single detection
- Integrated end-to-end detection

## Configuration

### Environment Variables

```bash
# Enable advanced detection (default: true)
export USE_ADVANCED_SINGLE_DETECTION=1

# Set z-score threshold (default: 0.20)
export SINGLE_DETECTION_ZSCORE_THRESHOLD=0.20
```

### In Code

```python
# Disable advanced detection (use standard detection)
result = detect_single_for_track(
    # ... parameters ...
    use_advanced_detection=False
)

# Custom threshold per call
result = detect_single_for_track(
    # ... parameters ...
    zscore_threshold=0.30
)
```

## Performance Considerations

### Database Queries

The advanced detection performs several queries:
1. Find matching versions by ISRC
2. Find matching versions by title+duration (fallback)
3. Get all track popularities in album (for z-score)

**Optimization:**
- Uses indexes on `isrc`, `artist`, `album`
- Batch processing for multiple tracks
- Caches results within scan session

### Memory Usage

- Lightweight data structures (dataclasses)
- No in-memory caching of entire database
- Processes one album at a time

## Troubleshooting

### Issue: No singles detected

**Check:**
1. Are tracks marked as singles in Spotify/MusicBrainz?
2. Is the z-score threshold too high?
3. Is the album a compilation? (uses different logic)

**Debug:**
```python
result = detect_single_advanced(
    # ... parameters ...
    verbose=True  # Enable detailed logging
)
```

### Issue: Too many singles detected

**Solutions:**
1. Increase z-score threshold (e.g., from 0.20 to 0.30)
2. Check if metadata is correctly marking non-singles
3. Verify album type is not incorrectly set to "compilation"

### Issue: Advanced detection not running

**Check:**
1. Is `use_advanced_detection=True`?
2. Are all required parameters provided (track_id, album)?
3. Check logs for import errors

## Future Enhancements

Potential improvements:
- Machine learning model for single prediction
- User feedback loop for improving detection
- Integration with additional metadata sources
- Configurable alternate version patterns
- Smart compilation detection using track metadata
- Historical single data from charts/radio play

## References

- [ISRC Handbook](https://www.ifpi.org/isrc/)
- [MusicBrainz Release Group Types](https://musicbrainz.org/doc/Release_Group/Type)
- [Spotify Web API](https://developer.spotify.com/documentation/web-api/)
- [Z-Score Statistical Method](https://en.wikipedia.org/wiki/Standard_score)
