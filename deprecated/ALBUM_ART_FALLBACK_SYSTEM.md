# Album Art Fallback System

## Overview

The album art fallback system provides robust album cover image retrieval by trying multiple sources in a prioritized order. This ensures maximum success rate when downloading album art during popularity scans.

## Previous Issue

The original implementation relied solely on MusicBrainz Cover Art Archive (CAA), which resulted in failures when:

- A release had no cover art metadata uploaded to CAA
- CAA service was temporarily unavailable
- Network connectivity issues occurred

## New Fallback Strategy

### Source Priority Order

1. **MusicBrainz Cover Art Archive (CAA)** - Primary source
   - Fetches release-group MBID from database or MusicBrainz search
   - Constructs direct CAA URL: `https://coverartarchive.org/release-group/{mbid}/front-500`
   - Most reliable when metadata exists

2. **AudioDB** - Secondary fallback
   - Searches AudioDB by artist and album name
   - Returns album thumbnail URL
   - Good coverage for mainstream releases

3. **Discogs** - Tertiary fallback (requires API token)
   - Requires Discogs API token configured in `config.yaml`
   - Searches Discogs release database by artist and album
   - Returns release thumbnail if available
   - Falls back gracefully if token not configured

### Implementation Functions

#### `fetch_album_art_from_audiodb(artist, album)`

- Calls `api_clients.audiodb.get_album_artwork()`
- Returns URL string or None
- No authentication required (uses free API)

#### `fetch_album_art_from_discogs(artist, album, discogs_token)`

- Uses Discogs REST API for release search
- Requires valid API token
- Gracefully skips if token unavailable
- Respects rate limiting via token-based throttling

#### `fetch_album_art_url_from_musicbrainz(artist, album)`

- Existing function, enhanced with better error handling
- Attempts MBID lookup in database first
- Falls back to MusicBrainz API search if needed
- Returns CAA URL for release-group

#### `download_and_save_album_art(artist, album, art_url, conn, cursor, source)`

- Enhanced to track which source provided the art
- Updated `source` column in `album_art` table
- Still handles direct image download from URL
- Uses existing database connection for efficiency

#### `fetch_and_save_album_art_with_fallback(artist, album, conn, cursor, discogs_token)`

- Orchestrates the full fallback chain
- Tries each source in priority order
- Stops on first success
- Logs which source succeeded or why all failed
- Returns True/False for success/failure

## Integration Points

### Main Popularity Scan (Line ~3090)

```python
# Get Discogs token for fallback source
discogs_token = config.get('discogs', {}).get('token') if config else None

# Try to fetch and save album art using fallback chain
# (MusicBrainz -> AudioDB -> Discogs)
if fetch_and_save_album_art_with_fallback(artist, album, conn, cursor, discogs_token):
    log_info(f'[ALBUM_ART] Album art successfully downloaded and saved for {artist} - {album}')
else:
    log_debug(f'[ALBUM_ART] Failed to obtain album art from any source for {artist} - {album}')
```

### Database Schema Updates

The `album_art` table's `source` column now tracks which service provided the art:

- `"musicbrainz"` - From MusicBrainz CAA
- `"audiodb"` - From The AudioDB
- `"discogs"` - From Discogs API
- `"unknown"` - Legacy entries (pre-fallback system)

## Configuration

### Discogs API Token (Optional)

To enable Discogs fallback:

```yaml
# config/config.yaml
discogs:
  token: "YOUR_DISCOGS_API_TOKEN"
```

Get a Discogs token:

1. Register at [Discogs](https://www.discogs.com)
2. Go to Settings > Developers
3. Create a personal token

### No Configuration Required

AudioDB and MusicBrainz work without configuration:

- AudioDB uses free public API (limited by IP, ~100 requests/IP/day)
- MusicBrainz uses public API with rate limiting (1 request/sec max)

## Logging

All album art operations log to `[ALBUM_ART]` prefix for easy filtering:

```text
[ALBUM_ART] Found album art via AudioDB for Artist - Album
[ALBUM_ART] Successfully downloaded and saved album art for Artist - Album from discogs (15234 bytes)
[ALBUM_ART] Discogs lookup failed for Artist - Album: Connection timeout
[ALBUM_ART] All fallback sources exhausted for Artist - Album
```

## Performance Characteristics

- **MusicBrainz**: ~100-500ms (MBID lookup + CAA fetch)
- **AudioDB**: ~100-300ms (album search + URL retrieval)
- **Discogs**: ~200-800ms (requires token, may hit rate limits)

**Total fallback chain** per album: ~500-1500ms worst case

Timeouts are set to prevent blocking:

- MusicBrainz search: 3 seconds
- Image download: 5 seconds
- API calls: 5 seconds

## Error Handling

The system gracefully handles:

- Missing/unavailable MBIDs
- Network timeouts
- 404 responses (no art available)
- Rate limiting (returns None, tries next source)
- Missing API tokens (Discogs skipped silently)
- Corrupted/empty responses

## Benefits

1. **Higher Success Rate**: 3 sources instead of 1
2. **Resilience**: Automatic failover on any error
3. **Tracking**: Know which source provided each image
4. **No Breaking Changes**: Backward compatible API
5. **Efficient**: Tries cheapest sources first
6. **Configurable**: Discogs optional for power users

## Testing the System

### Manual Test

```python
from popularity import fetch_and_save_album_art_with_fallback
import sqlite3

conn = sqlite3.connect('database.db')
cursor = conn.cursor()

# Test with known album
success = fetch_and_save_album_art_with_fallback(
    "The Beatles",
    "Abbey Road",
    conn, cursor,
    discogs_token="YOUR_TOKEN"
)
print(f"Result: {success}")

# Check what was downloaded
cursor.execute("""
    SELECT source, LENGTH(image_data) as size 
    FROM album_art 
    WHERE artist_name = ? AND album_name = ?
""", ("The Beatles", "Abbey Road"))
result = cursor.fetchone()
if result:
    print(f"Source: {result[0]}, Size: {result[1]} bytes")
```

## Future Improvements

1. **Caching**: Store which sources have been tried for failed albums
2. **Parallel Fetching**: Try multiple sources concurrently
3. **Source Weighting**: Track success rates per source
4. **Image Quality**: Prefer higher-resolution images
5. **Metadata Extraction**: Extract artist/album from image metadata as fallback

