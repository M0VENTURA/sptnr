# Last.fm Tags Not Loading - Root Cause & Fix

## Root Cause Analysis

The Last.fm tags and similar artists aren't displaying because of **two issues**:

### Issue 1: Last.fm API is Disabled ⚠️
- **Current Status**: Last.fm integration is **disabled** in your configuration
- **Evidence**: Configuration check shows: `Last.fm enabled: False`
- **Impact**: Tags are never fetched from the Last.fm API, even during popularity scans

### Issue 2: No Track Data in Database
- **Current Status**: The database is empty
- **Evidence**: Database query shows: `Tracks: 0`
- **Impact**: Even if Last.fm were enabled, there are no tracks to fetch tags for

## How Last.fm Tags Are Fetched

The data flow is:

```
1. Popularity Scan (popularity.py)
   └─→ Initializes Last.fm API client
       └─→ For each track in an album:
           ├─ Check if Last.fm is enabled in config
           ├─ Fetch tags via Last.fm API with:
           │  └─ Artist name + Track title
           └─ Store tags in database (lastfm_tags field)

2. Album/Track Detail Page (app.py)
   └─→ `/api/genres/track/<track_id>` endpoint
       └─→ Retrieves lastfm_tags from database
           └─→ Aggregates with other tag sources
               └─→ Returns JSON to frontend

3. Frontend Display (album.html)
   └─→ Fetches genres from API
       └─→ Iterates through data.genres.lastfm_tags
           └─→ Displays as genre buttons/tags
```

## Quick Fix Guide

### Step 1: Enable Last.fm in Configuration

You need to set up your configuration file with Last.fm enabled:

**Location**: `/config/config.yaml` (or set `CONFIG_PATH` environment variable)

**Add this section**:

```yaml
api_integrations:
  last_fm:
    enabled: true
    api_key: YOUR_LAST_FM_API_KEY_HERE
```

### Step 2: Get a Last.fm API Key

1. Go to: https://www.last.fm/api/account/create
2. Create a new API account
3. You'll receive:
   - **API Key**: Copy this value
   - **Shared Secret**: Not needed for read-only access

### Step 3: Configure Your Environment

**Option A: Docker/Container Setup**
- Mount your config file to `/config/config.yaml`
- Restart the application

**Option B: Local Development**
- Create `config.yaml` in the sptnr directory:
  ```bash
  cat > config.yaml << 'EOF'
  navidrome:
    database_path: ./navidrome.db
    
  api_integrations:
    last_fm:
      enabled: true
      api_key: YOUR_LAST_FM_API_KEY
  EOF
  ```

**Option C: Environment Variable**
```bash
export CONFIG_PATH=/path/to/your/config.yaml
```

### Step 4: Import Tracks from Navidrome

Before popularity scans can fetch any data, you need to import tracks:

```bash
# Option 1: Scan all albums
python scan_navidrome.py

# Option 2: Scan specific albums
python scan_navidrome.py "Artist" "Album"

# Option 3: Using beets integration
python beets_integration.py
```

### Step 5: Run Popularity Scan

Once tracks are imported and Last.fm is enabled:

```bash
# Scan popularity and fetch Last.fm tags
python popularity.py

# Or for specific albums
python popularity.py "Artist" "Album"
```

## Verify the Fix

### Check 1: Last.fm Configuration

```bash
python debug_lastfm_tags.py
```

Should show:
```
✓ Tracks with lastfm_tags populated: [number > 0]
Sample tracks with Last.fm tags: [shows tags with names and counts]
```

### Check 2: Database Content

```bash
python check_db.py
```

Should show:
```
Configured database path: navidrome.db
Last.fm enabled: True
Last.fm API key set: True
```

### Check 3: API Response

Visit in browser:
```
http://localhost:5000/api/genres/track/{track_id}
```

Should return:
```json
{
  "genres": {
    "lastfm_tags": [
      {"name": "rock", "count": 1000},
      {"name": "alternative", "count": 850}
    ],
    "spotify_genres": [...],
    ...
  }
}
```

### Check 4: Frontend Display

1. Open an album page in the web UI
2. Open browser Dev Tools (F12)
3. Check Network tab for `/api/genres/track/` requests
4. Verify response contains `lastfm_tags` array
5. Check Console for any JavaScript errors

## Implementation Details

### Last.fm Tag Fetching (popularity.py, lines 3043-3055)

```python
if lastfm_client:
    rate_limiter = get_rate_limiter()
    can_proceed = rate_limiter.check_lastfm_limit()[0]
    if can_proceed:
        lastfm_tags = _run_with_timeout(
            lastfm_client.get_track_tags,
            5,
            f"Last.fm tags lookup timed out",
            track_artist, title, limit=10
        )
        if lastfm_tags:
            track_tags["lastfm_tags"] = lastfm_tags
```

**What it does**:
1. Checks rate limiting before making request
2. Fetches up to 10 tags per track
3. Has 5-second timeout protection
4. Gracefully handles failures

### Storage (popularity.py, line 3614)

```python
lastfm_tags_json = json.dumps(track_tags["lastfm_tags"])
cursor.execute("UPDATE tracks SET lastfm_tags = ? WHERE id = ?",
              (lastfm_tags_json, track_id))
```

Stores tags as JSON array in the `lastfm_tags` database field.

### Fallback (popularity.py, lines 3460-3468)

If batch fetch fails, the system falls back to extracting tags from the Last.fm track info:

```python
if not track_tags["lastfm_tags"]:  # Fallback if batch fetch didn't get tags
    lastfm_info = album_lastfm_data.get(track_id, {})
    toptags = lastfm_info.get("toptags", {}).get("tag", [])
    tag_names = [tag.get("name", "") for tag in toptags]
    lastfm_tags_json = json.dumps(tag_names)
```

## Troubleshooting

### Tags still not showing after fix?

1. **Check API client initialization logs**: During popularity scan, should see:
   ```
   Last.fm client initialized for batch tag fetching
   ```

2. **Check tag fetch logs**: Should see:
   ```
   Fetched X Last.fm tags for "Track Title"
   ```

3. **Verify rate limiting**: Last.fm API has rate limits
   - Default: 60 requests per 5 minutes
   - Check logs for: `Last.fm rate limit exceeded`

4. **Check database field**: 
   ```bash
   sqlite3 navidrome.db "SELECT lastfm_tags FROM tracks WHERE title='Your Song' LIMIT 1;"
   ```
   Should return JSON array of tags

5. **Check API endpoint directly**:
   ```bash
   curl http://localhost:5000/api/genres/track/track_123
   ```

### Still seeing errors?

Check the logs from the last popularity scan:
```bash
tail -100 scan.log  # Or whatever log file is configured
```

Look for:
- `Failed to fetch Last.fm tags` - API error
- `Last.fm rate limit exceeded` - Rate limiting issue
- `Last.fm client not configured` - Configuration issue

## Related Features (Also Affected)

Since Last.fm is disabled, these features also won't work:

- ✗ Similar artists (from Last.fm)
- ✗ Last.fm track playcount and listener data
- ✗ Last.fm recommendations
- ✗ Last.fm tags and genres

Once Last.fm is enabled and tags are fetched, these will all start working.

## Recent Code Changes

Two improvements were just added to make tag fetching more robust:

1. **Commit 4fcb737**: Added automatic tagging of 5-star detected singles
   - 5-star songs with medium/high single confidence are now auto-tagged
   - Happens during popularity scan automatically

2. **Commit 4fcb737**: Improved batch tag fetch error handling
   - API clients initialize outside main try-catch
   - Loop continues even if client init fails
   - Better logging to track where failures occur

These improvements ensure better tag fetching reliability once Last.fm is enabled.

## Next Steps

1. ✓ Enable Last.fm in config
2. ✓ Get API key
3. ✓ Import tracks from Navidrome
4. ✓ Run popularity scan
5. ✓ Check `debug_lastfm_tags.py` output
6. ✓ Verify tags show in browser Dev Tools
7. ✓ Tags should display on album page

## Support

If you need help:

1. Run `debug_lastfm_tags.py` and share the output
2. Check logs from popularity scan
3. Verify config file is being loaded correctly
4. Test API endpoint directly with curl
