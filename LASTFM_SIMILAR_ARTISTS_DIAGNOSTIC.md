# Last.fm Genres & Similar Artists Configuration Guide

## Current Status ✅

**CODE IS READY:** All features are implemented and working:
- ✅ Last.fm tags are being fetched during batch genre collection
- ✅ Similar artists are being fetched during scan (compilation guard removed)
- ✅ Both features use proper rate limiting with `wait_if_needed_lastfm()`
- ✅ Rate limit respects 2-second wait window before skipping

## Why These Features Aren't Running

The debug log shows **NO Last.fm tags or similar artists being fetched** because:

### 1. Last.fm API Not Enabled in Config
The scan log shows zero entries for:
- Last.fm tag fetching
- Similar artist lookups from Last.fm

**Root Cause:** Last.fm API key is not configured in `/config/config.yaml`

### 2. How to Enable Last.fm API

#### Option A: Via Web UI (Recommended)
1. Go to **Settings → API Keys → Last.fm**
2. Check **Enable Last.fm**
3. Enter your **Last.fm API Key** (from https://www.last.fm/api/account/create)
4. Optionally enter your **Last.fm Username**
5. Click **Save Configuration**

#### Option B: Direct YAML Edit
Edit `/config/config.yaml`:
```yaml
api_integrations:
  last_fm:
    enabled: true
    api_key: "your_actual_lastfm_api_key_here"
    username: "your_lastfm_username"  # optional
```

### 3. Verify Configuration Saved
Run this command to verify Last.fm config is loaded:
```bash
python3 -c "
import yaml, os
cfg_path = os.environ.get('CONFIG_PATH', '/config/config.yaml')
cfg = yaml.safe_load(open(cfg_path))
lfm = cfg.get('api_integrations', {}).get('last_fm', {})
print('Last.fm Config:')
print(f'  Enabled: {lfm.get(\"enabled\")}')
print(f'  Has API Key: {bool(lfm.get(\"api_key\"))}')
print(f'  Username: {lfm.get(\"username\", \"Not set\")}')
"
```

## What Happens After Configuration

Once Last.fm is enabled:

### During Album Scan
1. **Per-track Last.fm lookups** (already working):
   - Fetches listeners and play counts
   - Uses rate limiter + 2-second wait window
   - Calculates popularity scores

2. **NEW: Batch Last.fm tag fetching** (will start working):
   - For each album, fetches tags for all tracks
   - Uses same rate limiter as track lookups
   - Stores in database column: `lastfm_tags`
   - Included in "Batch committed" logs with "merged tag data"

3. **NEW: Similar artist fetching** (will start working):
   - For each artist, fetches 10 similar artists from Last.fm
   - Uses rate limiter with wait window
   - Stores in database column: `similar_artists_lastfm`
   - Also attempts ListenBrainz similar artists (requires MusicBrainz MBID)

### On Track Pages
After next scan:
- Genre tabs will show Last.fm tags alongside:
  - Spotify genres
  - ListenBrainz genres
  - Discogs genres
  - MusicBrainz genres

- Similar Artists tabs will show:
  - Last.fm similar artists
  - ListenBrainz similar artists

## Debug Log Signs

### ✅ Last.fm Enabled & Working
```
[DEBUG] Last.fm client initialized for batch tag fetching
[DEBUG] Fetched N Last.fm tags for "Track Name"
[DEBUG] Found N similar artists for 'Artist' from Last.fm
[INFO] Batch committed X popularity scores and genre sources... with merged tag data
```

### ❌ Last.fm Not Enabled
```
[DEBUG] Last.fm client not configured or disabled
[DEBUG] Last.fm not enabled or API key missing - skipping...
```

## Rate Limiting Behavior

When Last.fm API rate limit is hit (429 Too Many Requests):

1. **First attempt fails** → Rate limiter detects limit
2. **Automatic wait** → System waits up to 2 seconds
3. **Second attempt** → Tries again after wait
4. **If still limited** → Skips gracefully with debug message

Example log:
```
[DEBUG] Rate limit hit for Last.fm tags (Track Name): Too many requests, waiting...
[DEBUG] Resuming Last.fm queries after rate limit recovery
```

## Troubleshooting

### "Last.fm API Key" field is empty after saving
- Check file permissions on `/config/config.yaml`
- Verify config file was actually written
- Try saving again with API key visible (not masked)

### No similar artists appearing in logs
- Last.fm API key must be valid (not placeholder)
- Some artists may have no similar artists data in Last.fm
- ListenBrainz requires valid MusicBrainz MBID lookup

### Genre sources not showing all 5 sources
- Ensure all APIs are enabled: Spotify, Last.fm, ListenBrainz, Discogs, MusicBrainz
- Some tracks may not have data from all sources
- Recent tracks may need additional scans for all sources to populate

## Code References

**Similar Artists Fetching:**
- `popularity.py` lines 2640-2750: Similar artist lookup with rate limiting
- `popularity.py` lines 2648-2675: Last.fm similar artist API calls
- `popularity.py` lines 2689-2720: ListenBrainz similar artist API calls

**Last.fm Tags Fetching:**
- `popularity.py` lines 3013-3115: Batch tag/genre collection per album
- `popularity.py` lines 3070-3090: Last.fm tag fetch with rate limiting
- `popularity.py` lines 3611-3650: Database update with merged tag data

**Database Columns:**
- `tracks.lastfm_tags`: JSON array of Last.fm tags
- `tracks.spotify_genres`: JSON array of Spotify genres
- `tracks.listenbrainz_genres`: JSON array of ListenBrainz genres
- `tracks.discogs_genres`: JSON array of Discogs genres
- `tracks.musicbrainz_genres`: JSON array of MusicBrainz genres
- `artists.similar_artists_lastfm`: JSON object with Last.fm similar artists
- `artists.similar_artists_listenbrainz`: JSON object with ListenBrainz similar artists

## Next Steps

1. **Enable Last.fm API in config** (see steps above)
2. **Run "Scan Artist" again** for your library
3. **Monitor scan log** for "Found N similar artists" entries
4. **Check track pages** for Last.fm tags and similar artists tabs
5. **Verify database** populated: `SELECT COUNT(*) FROM tracks WHERE lastfm_tags IS NOT NULL AND lastfm_tags != '[]'`
