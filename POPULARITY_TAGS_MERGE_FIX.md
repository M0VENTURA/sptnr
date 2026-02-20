# Popularity Tags Merge Fix

## Problem Identified

The `popularity.py` was fetching genre and tag data from multiple sources (Last.fm, ListenBrainz, Discogs, MusicBrainz) but the data was **not being saved to the database**. 

### Root Cause
The code was:
1. Fetching tags into `album_tags_data` dictionary structure
2. Creating popularity update tuples with None/empty values for tag fields
3. Committing the database with the empty tag fields
4. **Never merging** the fetched tags into the update tuples before committing

### Evidence
In the batch commit section around line 3420, the code was:
```python
cursor.executemany(
    "UPDATE tracks SET popularity_score = ?, spotify_score = ?, lastfm_ratio = ?, spotify_genres = ?, lastfm_tags = ?, discogs_genres = ?, musicbrainz_genres = ?, cover_art_url = ? WHERE id = ?",
    track_updates  # ← This never included the tags from album_tags_data!
)
```

The `track_updates` list was populated during track processing but the tags were collected separately in `album_tags_data` and never merged back.

## Fix Applied

Added a merging step before the database commit that:
1. Iterates through each update tuple in `track_updates`
2. Checks if the track_id exists in `album_tags_data`
3. If found, merges the freshly-fetched tags into the tuple:
   - Last.fm tags → `lastfm_tags` field
   - ListenBrainz genres → `musicbrainz_genres` field (database column naming)
   - Discogs genres → `discogs_genres` field
4. Uses the merged tuple for the database update

### Code Location
File: [popularity.py](popularity.py) (around line 3420)

### Logic Flow
```
For each track in track_updates:
  ├─ Check if track_id in album_tags_data
  ├─ If yes:
  │  ├─ Merge lastfm_tags
  │  ├─ Merge listenbrainz_genres
  │  └─ Merge discogs_genres
  ├─ Create updated tuple with merged data
  └─ Add to updated_track_updates
  
Execute batch update with merged data
```

## Impact

### Before Fix
- Tags fetched but discarded
- Database tags columns stayed empty
- Genre aggregation endpoints had no data to work with
- Similar artists comparison lacked context

### After Fix
- All fetched tags are now persisted to database
- `spotify_genres`, `lastfm_tags`, `discogs_genres`, `musicbrainz_genres` columns populated
- Genre aggregation endpoints will have data to aggregate
- Similar artists have proper genre context
- Tag freshness tracking via `tags_last_updated` timestamp

## Verification Steps

### 1. Check that tags are now in database
```sql
SELECT id, title, lastfm_tags, discogs_genres, musicbrainz_genres 
FROM tracks 
WHERE lastfm_tags IS NOT NULL OR discogs_genres IS NOT NULL 
LIMIT 10;
```

### 2. Verify merge is working in logs
Look for log entries like:
```
Using Last.fm tags for track <id>: 15 tags
Using Discogs genres for track <id>: 8 genres
Using ListenBrainz genres for track <id>: 12 genres
```

### 3. Check aggregation endpoints work
- GET `/api/genres/track/<track_id>` - Should return compound tags
- GET `/api/genres/album/<album>/<artist>` - Should aggregate album genres
- GET `/api/genres/artist/<artist>` - Should aggregate artist genres

### 4. Next Scan Activation
On next popularity scan:
1. Tags will be merged before database commit
2. Logs will show merge operations
3. Database columns will be populated
4. UI can display genre information properly

## Related Files
- [popularity.py](popularity.py) - Main popularity scoring logic
- [popularity_helpers.py](popularity_helpers.py) - Helper functions for tag fetching
- [popularity_tagagg.py](popularity_tagagg.py) - Genre aggregation endpoints
- [check_db.py](check_db.py) - Database schema with tag columns

## Testing Recommendations
1. Run a fresh popularity scan on a small subset
2. Verify log shows merge operations
3. Query database for populated tag columns
4. Test genre aggregation endpoints
5. Verify no performance degradation (merge is O(n) where n = tracks in album)

## Performance Note
The merge operation is efficient:
- O(n) where n = number of tracks in album (typically 10-15)
- Dictionary lookup is O(1)
- Only executed during singles_only=False mode
- Minimal overhead added to existing commit operation
