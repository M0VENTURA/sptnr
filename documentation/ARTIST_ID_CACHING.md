# Artist ID Caching Implementation

## Overview

This document describes the artist ID caching system implemented to optimize API calls to external music services (Spotify, Last.fm, MusicBrainz, Discogs).

## Problem Statement

Previously, the system would look up the Spotify artist ID for every single track during scanning. For an artist with 100 tracks, this meant 100 identical API calls to get the same artist ID.

## Solution

Implemented a database-level caching system that:
1. Looks up artist IDs once per artist (not per track)
2. Stores the artist ID in the database
3. Reuses cached IDs on subsequent scans
4. Batch updates all tracks for an artist when ID is discovered

## Database Schema Changes

New columns added to the `tracks` table:
- `spotify_artist_id` (TEXT) - Spotify artist identifier
- `lastfm_artist_mbid` (TEXT) - Last.fm artist MusicBrainz ID (if available)
- `discogs_artist_id` (TEXT) - Discogs artist identifier
- `musicbrainz_artist_id` (TEXT) - MusicBrainz artist identifier

Indexes created for fast lookups:
- `idx_tracks_spotify_artist_id`
- `idx_tracks_musicbrainz_artist_id`
- `idx_tracks_discogs_artist_id`

## How It Works

### Before (Inefficient)
```python
for track in album_tracks:
    artist_id = get_spotify_artist_id(artist_name)  # API call for EVERY track
    # Use artist_id to find track...
```

### After (Optimized)
```python
# Look up artist ID once per artist
artist_id = get_spotify_artist_id(artist_name)  # Checks DB cache first, then API
if artist_id:
    update_artist_id_for_artist(artist_name, artist_id)  # Batch update all tracks

for track in album_tracks:
    # Use cached artist_id for all tracks
    # ...
```

### get_spotify_artist_id() Flow
1. Check database for existing `spotify_artist_id` for this artist
2. If found in DB, return cached ID (no API call)
3. If not found, query Spotify API
4. Store result in in-memory cache (existing behavior)
5. Return artist ID

### Batch Update
When an artist ID is first discovered, `update_artist_id_for_artist()` updates all existing tracks for that artist in a single SQL statement:
```sql
UPDATE tracks 
SET spotify_artist_id = ? 
WHERE artist = ? AND spotify_artist_id IS NULL
```

## Performance Impact

### API Call Reduction
- **Before**: N API calls per artist (where N = number of tracks)
- **After**: 1 API call per artist on first scan, 0 on subsequent scans

### Example Calculation
For an artist with 100 tracks:
- **First scan**: 1 API call (vs 100 previously) → 99% reduction
- **Subsequent scans**: 0 API calls (vs 100 previously) → 100% reduction

For a library with 10,000 tracks from 100 artists:
- **Before**: ~10,000 API calls
- **After (first scan)**: ~100 API calls → 99% reduction
- **After (subsequent scans)**: ~0 API calls → 100% reduction

## Migration

The schema changes are automatically applied through `check_db.py`:
```python
from check_db import update_schema
update_schema('/path/to/database/sptnr.db')
```

Existing databases will have the new columns added automatically on first run.

## Testing

Run the test suite:
```bash
python3 test_artist_id_caching.py
```

Tests verify:
- Database schema updates correctly
- Columns and indexes are created
- Batch update function works
- Database lookup path is functional

## Notes on Other Services

### MusicBrainz & Discogs
These services use artist **names** in their API queries, not artist IDs. The columns are reserved for future use if artist-level caching becomes beneficial for these services.

### Last.fm
Last.fm primarily uses artist names. The `lastfm_artist_mbid` column can store MusicBrainz IDs when available from Last.fm responses.

## Files Modified

1. `check_db.py` - Added new columns to schema
2. `popularity_helpers.py` - Added database cache lookup and batch update
3. `popularity.py` - Moved artist ID lookup to per-artist level
4. `templates/dashboard.html` - Removed missing releases scan section
5. `migrations/add_artist_id_columns.sql` - Migration script
6. `test_artist_id_caching.py` - Test suite

## Future Enhancements

Potential improvements:
- Cache artist IDs for other scanning operations (beets, single detection)
- Add TTL (time-to-live) for cached IDs
- Periodic refresh of cached IDs for active artists
- Statistics tracking for cache hit rates
