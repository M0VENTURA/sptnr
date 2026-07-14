# Artist Page Data - Setup & Population Guide

## Overview

Your artist pages are currently missing:
- ✗ Artist artwork/images
- ✗ Last.fm tags/genres
- ✗ Similar artists  
- ✗ Artist bio
- ✗ Album art info

This is **normal** - these fields need to be populated by running scans. Here's what you need to do.

## Why the Data is Missing

The system has three types of data storage:

1. **Track Data** (from Navidrome library)
   - Status: ✅ Populated automatically when Navidrome imports music
   - Contains: Song titles, artists, albums, Navidrome genres

2. **Genre Tags** (from external sources)
   - Status: ⚠️ Empty - requires popularity scan
   - Sources: Spotify, Last.fm, ListenBrainz, Discogs, MusicBrainz
   - Contains: `lastfm_tags`, `spotify_genres`, `listenbrainz_genres`, etc.

3. **Artist Profile Data** (from artists table)
   - Status: ⚠️ Empty - requires scan to populate
   - Contains: `bio`, `image_url`, `similar_artists_lastfm`, `similar_artists_listenbrainz`

## How to Populate the Data

### Option 1: Run Scan for Specific Artist (Recommended to start)

1. Go to an **Artist Detail Page** (e.g., Adema, Cherri Bomb, etc.)
2. Click the **"Scan Artist"** button (yellow button with play icon)
3. Wait for completion (~2-5 minutes depending on album count)

**This will populate:**
- ✅ Artist bio (from MusicBrainz)
- ✅ Artist image (from MusicBrainz/Discogs/Spotify)
- ✅ Tags for all tracks (from Last.fm, ListenBrainz, Discogs, Spotify)
- ✅ Similar artists (from Last.fm and ListenBrainz)
- ✅ Artist stats and popularity scores

### Option 2: Run Full Popularity Scan

From **Downloads** page → **Run Scan** dropdown → Select scan type

**Scan types:**
- `Navidrome Sync` - Updates track counts only
- `Popularity Scan` - Fetches tags, popularity, similar artists (30 min - 2 hours)
- `Singles Detection` - Identifies single tracks vs album tracks
- `Full Scan` - All of the above (2-4 hours)

## What Happens During a Scan

During popularity/full scan, the system:

1. **Fetches Artist Bio**
   - Calls MusicBrainz API for biography
   - Stores in `artists.bio` column
   - Falls back to Discogs if MusicBrainz doesn't have it

2. **Fetches Artist Image**
   - Tries: MusicBrainz → Discogs → Spotify
   - Stores URL in `artists.image_url` column  
   - User can manually override via "Change Image" button

3. **Fetches Similar Artists**
   - Last.fm API: Up to 50 similar artists by playcount
   - ListenBrainz API: Similar artists from user listening data
   - Stores as JSON in `artists.similar_artists_lastfm` and `similar_artists_listenbrainz`

4. **Fetches & Aggregates Genre Tags**
   - Per-track fetching from 5 sources:
     - **Spotify**: Track audio features classification
     - **Last.fm**: User-tagged genres (your Last.fm account recommended)
     - **ListenBrainz**: Community genres (requires MBID)
     - **Discogs**: Release-level genres
     - **MusicBrainz**: Standardized genres
   - Stores JSON in columns: `spotify_genres`, `lastfm_tags`, etc.
   - Artist page aggregates across all tracks and shows top genres

## Configuration Requirements

### Last.fm (Recommended)
```yaml
api_integrations:
  last_fm:
    enabled: true              # ← Should be TRUE
    api_key: YOUR_KEY_HERE
```

**Status**: ✅ You just enabled this! Last scan will fetch Last.fm data.

### ListenBrainz (Optional but Recommended)
```yaml
api_integrations:
  listenbrainz:
    enabled: true
    user_token: YOUR_TOKEN    # (optional, for better personalization)
```

### Discogs (Optional)
```yaml
api_integrations:
  discogs:
    enabled: true
    token: YOUR_TOKEN
```

### MusicBrainz (Optional)
```yaml
api_integrations:
  musicbrainz:
    enabled: true
```

## Current Status Check

To see what data is currently populated:

### Via Database
```bash
# Check if artists table has rows
sqlite3 navidrome.db "SELECT COUNT(*) FROM artists WHERE bio IS NOT NULL OR image_url IS NOT NULL;"

# Check if tracks have genre tags
sqlite3 navidrome.db "SELECT lastfm_tags FROM tracks WHERE lastfm_tags IS NOT NULL LIMIT 1;"

# Check similar artists 
sqlite3 navidrome.db "SELECT similar_artists_lastfm FROM artists WHERE similar_artists_lastfm IS NOT NULL LIMIT 1;"
```

### Via Debug Script
```bash
python debug_lastfm_tags.py    # Shows Last.fm tag status
python check_db.py            # Shows database status
```

## Troubleshooting

### Tags Still Not Showing After Scan?

1. **Check Last.fm Config**: Ensure Last.fm enabled
   ```yaml
   api_integrations:
     last_fm:
       enabled: true          # NOT false!
       api_key: YOUR_KEY      # NOT empty
   ```

2. **Check Scan Logs**: Look for these messages
   - ✅ "Last.fm client initialized"
   - ✅ "Fetched X Last.fm tags for 'Track Title'"
   - ❌ "Last.fm client not configured"
   - ❌ "Failed to fetch Last.fm tags"

3. **Rate Limiting**: Last.fm allows 60 requests per 5 minutes
   - Check logs for: "Last.fm rate limit exceeded"
   - Wait 5 minutes and try again

### Similar Artists Not Showing?

- Run "Scan Artist" (not just popularity scan)
- Takes 1-5 minutes to fetch from Last.fm + ListenBrainz
- Check logs for: "Found X similar artists from Last.fm"

### Artist Bio Missing?

- Click "Scan Artist" button - fetches from MusicBrainz
- Can also manually edit via "Change Image" modal
- Bio is permanently cached after scan

## Timeline

**Immediate (now):**
- Artist pages load but show "No data yet" messages
- Placeholder images show
- Similar artists section is empty

**After Scan (30 min - 2 hours):**
- ✅ Artist bios populate from MusicBrainz
- ✅ Artist images appear
- ✅ Genre tags from 5 sources display
- ✅ Similar artists show up
- ✅ Popularity scores update

## Next Steps

1. **Go to any artist page** (Adema, Cherri Bomb, etc.)
2. **Click "Scan Artist"** button (yellow, with play icon)
3. **Wait for completion** (check status at top of page)
4. **Refresh the page** to see populated data

**For full library**, later run **Popularity Scan** from Downloads page to scan all artists at once.

## API Endpoints Reference

| Endpoint | Returns | Requires | Status |
|----------|---------|----------|--------|
| `/api/artist/bio?name=Artist` | Biography text | DB Artist record | ⚠️ Empty until scan |
| `/api/artist/image?name=Artist` | Image URL | DB Artist record | ⚠️ Empty until scan |
| `/api/artist/Artist/similar` | Similar artists list | DB Artist record | ⚠️ Empty until scan |
| `/api/genres/artist/Artist` | Genre aggregation | Track genre fields | ⚠️ Empty until scan |
| `/api/genres/track/id` | Per-track tags | Track genre columns | ⚠️ Empty until scan |

All endpoints return helpful messages when data is missing, guiding you to run scans.

## Files Related to This Data

- Database schema: [check_db.py](./check_db.py)
- Genre aggregation: [genre_tag_aggregator.py](./genre_tag_aggregator.py)
- Popularity scan (fetches data): [popularity.py](./popularity.py)
- Artist page template: [templates/artist.html](./templates/artist.html)
- API endpoints: [app.py](./app.py) - search for `/api/artist/`
