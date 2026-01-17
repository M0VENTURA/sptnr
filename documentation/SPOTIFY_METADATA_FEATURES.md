# 🎵 Spotify Metadata & Smart Playlist Features

## Overview

SPTNR now includes comprehensive Spotify metadata fetching and smart playlist capabilities. This enhancement enables advanced filtering, genre-based playlists, and intelligent track categorization.

## What's New

### Comprehensive Metadata Collection

The system now fetches and stores a wide range of metadata from Spotify for each track:

#### 1. Core Track Metadata
- **spotify_track_id** - Unique Spotify track identifier
- **isrc** - International Standard Recording Code
- **track_name** - Track title
- **artist_names** - List of artists
- **album_name** - Album name
- **album_id** - Spotify album ID
- **release_date** - Release date
- **duration_ms** - Track duration in milliseconds
- **explicit** - Explicit content flag (boolean)
- **popularity** - Spotify popularity score (0–100)

#### 2. Audio Features (from `/audio-features` endpoint)
These features enable smart playlist creation based on musical characteristics:

- **tempo** - Beats per minute (BPM)
- **energy** - Energy level (0.0–1.0)
- **danceability** - How suitable for dancing (0.0–1.0)
- **valence** - Musical positivity/happiness (0.0–1.0)
- **acousticness** - Acoustic vs. electronic (0.0–1.0)
- **instrumentalness** - Vocal presence (0.0–1.0)
- **liveness** - Presence of audience (0.0–1.0)
- **speechiness** - Spoken word presence (0.0–1.0)
- **loudness** - Overall loudness in dB
- **key** - Musical key (0–11, where 0=C, 1=C#, etc.)
- **mode** - Major (1) or Minor (0)

#### 3. Artist Metadata (from `/artists` endpoint)
- **artist_genres** - Array of genre tags from Spotify
- **artist_popularity** - Artist popularity score (0–100)

#### 4. Album Metadata (from `/albums` endpoint)
- **album_type** - "album", "single", or "compilation"
- **total_tracks** - Number of tracks on the album
- **label** - Record label

#### 5. Special Genre Tags
The system automatically detects special categories based on metadata and audio features:

- **Christmas** - Holiday/seasonal tracks
- **Cover** - Cover versions or tribute songs
- **Live** - Live recordings
- **Acoustic** - Acoustic versions
- **Orchestral** - Orchestral arrangements
- **Instrumental** - Instrumental tracks

#### 6. Normalized Genres
Artist genres are normalized into broad categories:
- Rock, Metal, Pop, Electronic, Hip Hop, Jazz, Classical, Country, R&B, Folk

---

## Special Tag Detection Logic

### Christmas Detection
A track is tagged as "Christmas" if:
- Track name contains: `christmas`, `xmas`, `holiday`, `noel`, `santa`, `sleigh`, `jingle`, `silent night`, `holy night`, `winter wonderland`, etc.
- **OR** Album name contains the same keywords
- **OR** Artist genres contain `christmas` or `holiday`

### Cover Detection
A track is tagged as "Cover" if:
- Track name contains: `(cover)`, `(tribute)`, `(originally by)`, `cover version`, etc.
- **OR** Album name contains: `tribute`, `covers`, `in the style of`, etc.

### Live Detection
A track is tagged as "Live" if:
- Track name contains: `(live)`, `live at`, `live from`, `live version`
- **OR** Album name contains: `live`, `unplugged`, `in concert`, `live session`
- **OR** Audio feature `liveness > 0.8`

### Acoustic Detection
A track is tagged as "Acoustic" if:
- Track name contains: `(acoustic)`, `acoustic version`
- **OR** Audio feature `acousticness > 0.7`

### Orchestral Detection
A track is tagged as "Orchestral" if:
- Track name contains: `orchestral`, `symphonic`, `symphony`, `philharmonic`, `orchestra`
- **OR** Audio features: `instrumentalness > 0.8` AND `acousticness > 0.5`

### Instrumental Detection
A track is tagged as "Instrumental" if:
- Audio feature `instrumentalness > 0.8`

---

## Smart Playlist Examples

With comprehensive metadata, you can now create sophisticated playlists:

### Example 1: High Energy Rock
```sql
SELECT * FROM tracks 
WHERE normalized_genres LIKE '%rock%' 
  AND spotify_energy >= 0.8
  AND spotify_tempo >= 140
ORDER BY spotify_energy DESC
LIMIT 50
```

### Example 2: Christmas Music
```sql
SELECT * FROM tracks 
WHERE special_tags LIKE '%Christmas%'
ORDER BY spotify_popularity DESC
```

### Example 3: Relaxing Acoustic
```sql
SELECT * FROM tracks 
WHERE special_tags LIKE '%Acoustic%'
  AND spotify_energy <= 0.4
  AND spotify_valence >= 0.5
ORDER BY spotify_acousticness DESC
LIMIT 100
```

### Example 4: Sad Songs
```sql
SELECT * FROM tracks 
WHERE spotify_valence <= 0.3
  AND spotify_energy <= 0.5
ORDER BY spotify_valence ASC
LIMIT 50
```

### Example 5: Upbeat Dance Music
```sql
SELECT * FROM tracks 
WHERE spotify_danceability >= 0.7
  AND spotify_energy >= 0.8
  AND spotify_tempo BETWEEN 120 AND 140
ORDER BY spotify_danceability DESC
```

### Example 6: Instrumental Focus
```sql
SELECT * FROM tracks 
WHERE special_tags LIKE '%Instrumental%'
  AND spotify_instrumentalness >= 0.8
ORDER BY spotify_popularity DESC
```

### Example 7: Live Performances
```sql
SELECT * FROM tracks 
WHERE special_tags LIKE '%Live%'
ORDER BY spotify_liveness DESC
LIMIT 100
```

---

## Database Schema

### New Columns in `tracks` Table

#### Audio Features
- `spotify_tempo` (REAL) - Beats per minute
- `spotify_energy` (REAL) - Energy level (0.0–1.0)
- `spotify_danceability` (REAL) - Danceability (0.0–1.0)
- `spotify_valence` (REAL) - Positivity (0.0–1.0)
- `spotify_acousticness` (REAL) - Acousticness (0.0–1.0)
- `spotify_instrumentalness` (REAL) - Instrumentalness (0.0–1.0)
- `spotify_liveness` (REAL) - Liveness (0.0–1.0)
- `spotify_speechiness` (REAL) - Speechiness (0.0–1.0)
- `spotify_loudness` (REAL) - Loudness in dB
- `spotify_key` (INTEGER) - Musical key (0–11)
- `spotify_mode` (INTEGER) - Major (1) or Minor (0)
- `spotify_time_signature` (INTEGER) - Beats per measure

#### Artist & Album Metadata
- `spotify_artist_genres` (TEXT) - JSON array of artist genres
- `spotify_artist_popularity` (INTEGER) - Artist popularity (0–100)
- `spotify_album_label` (TEXT) - Record label
- `spotify_explicit` (INTEGER) - Explicit content flag

#### Derived Tags
- `special_tags` (TEXT) - JSON array of special tags
- `normalized_genres` (TEXT) - JSON array of normalized genres
- `merged_version_tags` (TEXT) - Tags from alternate versions
- `raw_spotify_genres` (TEXT) - Raw artist genres from Spotify

#### Tracking
- `metadata_last_updated` (TEXT) - Last metadata fetch timestamp

---

## How It Works

### Automatic Metadata Fetching

When the popularity scanner runs, it now:

1. **Searches for the track on Spotify** (as before)
2. **Fetches comprehensive metadata** including:
   - Track details (duration, ISRC, explicit flag)
   - Audio features (tempo, energy, danceability, etc.)
   - Artist metadata (genres, popularity)
   - Album metadata (label, type)
3. **Detects special tags** based on title, album, genres, and audio features
4. **Normalizes genres** to broad categories
5. **Stores everything in the database** for future querying

### Metadata Refresh Policy

- Metadata is cached for **30 days** to avoid unnecessary API calls
- Use `force=True` in scans to refresh metadata immediately
- Metadata is automatically fetched for new tracks

### Performance Optimization

- **Batch API requests** - Audio features are fetched in batches of up to 100 tracks
- **Caching** - Previously fetched metadata is reused
- **Rate limiting** - Built-in retry logic prevents API throttling
- **Selective updates** - Only tracks needing refresh are processed

---

## Using the Feature

### Running a Scan with Metadata Fetch

The metadata fetching is now integrated into the standard popularity scan:

```bash
# Run a full scan (includes metadata fetching)
python popularity.py

# Force refresh all metadata
python popularity.py --force

# Scan specific artist
python popularity.py --artist "Artist Name"
```

### Checking Metadata in Database

```bash
# View track metadata
sqlite3 /database/sptnr.db "SELECT title, spotify_tempo, spotify_energy, special_tags FROM tracks WHERE artist='Artist Name' LIMIT 10;"

# Find Christmas tracks
sqlite3 /database/sptnr.db "SELECT title, artist FROM tracks WHERE special_tags LIKE '%Christmas%';"

# Find high-energy tracks
sqlite3 /database/sptnr.db "SELECT title, artist, spotify_energy FROM tracks WHERE spotify_energy >= 0.9 ORDER BY spotify_energy DESC LIMIT 20;"
```

---

## API Integration

### Spotify API Endpoints Used

1. **`/v1/tracks/{id}`** - Core track metadata
2. **`/v1/audio-features/{id}`** - Audio features (batch supported)
3. **`/v1/artists/{id}`** - Artist metadata and genres
4. **`/v1/albums/{id}`** - Album metadata and label

### Rate Limits

- Spotify API allows approximately **180 requests per minute**
- The system handles rate limiting automatically with retries
- Batch requests reduce API calls significantly

---

## Configuration

No additional configuration is needed! The feature uses your existing Spotify credentials from `config.yaml`:

```yaml
api_integrations:
  spotify:
    enabled: true
    client_id: "your_client_id"
    client_secret: "your_client_secret"
```

---

## Troubleshooting

### Metadata Not Being Fetched

1. **Check Spotify credentials** in `config.yaml`
2. **Verify track has Spotify ID** - Only tracks matched to Spotify will have metadata
3. **Check logs** for API errors:
   ```bash
   tail -f /config/sptnr.log | grep "metadata"
   ```

### Missing Audio Features

- Not all tracks on Spotify have audio features
- The system handles missing features gracefully
- Check `spotify_tempo IS NULL` to find tracks without features

### Slow Scans

- Large libraries take time due to API rate limits
- Consider using `--artist` filter to scan incrementally
- Metadata caching reduces re-scan time significantly

---

## Future Enhancements

Planned improvements include:

- **Web UI for smart playlists** - Visual playlist builder with filters
- **Version detection** - Identify and merge metadata from different versions of the same song
- **Genre refinement** - More sophisticated genre normalization
- **Playlist templates** - Pre-configured smart playlists (workout, focus, party, etc.)
- **Metadata editing** - Manual tag adjustment in web UI

---

## Credits

This feature integrates with:

- **Spotify Web API** - Track, artist, album, and audio features data
- **Genre Detection** - Custom logic for special tag identification
- **Existing SPTNR infrastructure** - Seamless integration with popularity scanning

---

## Questions or Issues?

If you encounter problems or have suggestions:

1. Check the logs: `/config/sptnr.log` and `/config/unified_scan.log`
2. Verify Spotify API credentials
3. Open an issue on GitHub with relevant log excerpts

Happy playlist building! 🎵
