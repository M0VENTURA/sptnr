# Playlist Implementation Analysis: ListenBrainz & Last.fm

**Date**: March 31, 2026  
**Status**: **ListenBrainz BROKEN**, **Last.fm SAFE but suboptimal**

---

## Executive Summary

### ListenBrainz: BROKEN ❌
The current implementation uses a **non-existent API endpoint** (`/1/user/{username}/playlists/createdfor`) that doesn't return recommendations. This is why the buttons in the playlist browse page fail.

**Root Cause**: The endpoint should be `/1/cf/recommendation/user/{user}/recording` (Collaborative Filtering API), which requires a **2-step process**: first fetch MBIDs, then fetch metadata in a second call.

### Last.fm: FUNCTIONAL but NOT RECOMMENDATIONS ⚠️
The implementation uses `user.getTopArtists`, `user.getTopAlbums`, `user.getTopTracks` (user's most played), not true "recommendations" API (which doesn't exist officially). This works but gives user's own top tracks, not discovery.

**Issue**: Last.fm API has **inconsistent key naming** (documented in lfm README) — some responses use `artist['#text']`, others use `artist.name`.

---

## Detailed Analysis

### ListenBrainz API Comparison

#### ❌ Current sptnr Implementation
```python
# WRONG ENDPOINT - does not exist or doesn't work
url = f"{self.base_url}/user/{username}/playlists/createdfor"
res = self.session.get(url, headers=self.headers, timeout=(5, 30))
```

**Issues**:
1. Endpoint `/user/{username}/playlists/createdfor` ≠ recommendation endpoint
2. Returns nested structure that doesn't match actual API responses
3. No second metadata fetch for track details
4. "Last week" variants don't exist as separate endpoints

#### ✅ Correct Implementation (from Explo)
```python
# STEP 1: Get recommendations (MBIDs only)
url = f"{self.base_url}/cf/recommendation/user/{user}/recording?count=200"
res = self.session.get(url, headers=self.headers, timeout=(5, 30))
data = res.json()
payload = data.get("payload", {})
rec_mbids = payload.get("recordings", [])  # List of MBID strings

# STEP 2: Fetch metadata for those MBIDs
if rec_mbids:
    mbid_list = ",".join(rec_mbids[:200])
    url = f"{self.base_url}/metadata/recording/?recording_mbids={mbid_list}&inc=release+artist"
    res = self.session.get(url, headers=self.headers, timeout=(5, 30))
    metadata = res.json()
    recordings = metadata.get("recordings", [])
    # Extract: artist.name, recording.title, release.name, recording_mbid
```

#### Response Structure Differences

**Wrong endpoint returns** (if it works at all):
```json
{
  "payload": {
    "playlists": [
      {
        "title": "Weekly Jams",
        "recordings": [...]  // Structure unclear
      }
    ]
  }
}
```

**Correct CF endpoint returns**:
```json
{
  "payload": {
    "recordings": ["mbid1", "mbid2", ...]  // Just MBID strings
  }
}
```

**Metadata endpoint returns**:
```json
{
  "recordings": [
    {
      "recording_mbid": "mbid1",
      "title": "Track Name",
      "artist-credit": [
        {
          "artist": {
            "name": "Artist Name",
            "mbid": "artist-mbid"
          }
        }
      ],
      "release": {
        "title": "Album Name",
        "mbid": "release-mbid"
      }
    },
    ...
  ]
}
```

---

### Last.fm API Analysis

#### Current sptnr Implementation
```python
# Uses user.getTopArtists/getTopAlbums/getTopTracks
# Returns user's most-played items (not discovery recommendations)
# Handles some key naming variations like artist.get("image")
```

**What's in place**:
✅ Retry logic with exponential backoff  
✅ Rate limiting  
✅ Caching  
✅ Some handling of image array structures  

**Issues**:
❌ No true "recommendations" API (Last.fm official API doesn't have one)  
⚠️ Inconsistent key naming not fully handled:
- `artist` can be `{'#text': 'name'}` OR `{'name': 'name'}` in same response
- `image` is array with `#text` field
- Some endpoints return wrapped keys like `topartists.artist[]` vs `topalbums.album[]`

#### Comparison with lfm Reference

**lfm approach**:
```python
# Gets user top tracks:
method=user.gettoptracks&period=7day

# Response structure:
{
  "toptracks": {
    "track": [
      {
        "name": "Track", 
        "artist": {"name": "Artist"},  # or could be {"#text": "Artist"}
        "playcount": 123
      }
    ]
  }
}

# For recommendations, lfm uses web scraping:
# /player/station/user/{user}/recommended (NOT official API)
```

**sptnr approach**:
```python
# Gets user top albums/artists instead
# Has caching and retry logic
# But... uses user.getTopArtists/Albums as proxy for recommendations
```

---

## API Endpoint Reference

### ListenBrainz Endpoints That Work

| Endpoint | Purpose | Response | Notes |
|----------|---------|----------|-------|
| `GET /1/cf/recommendation/user/{user}/recording` | Get personalized track recommendations | `{"payload": {"recordings": ["mbid1", ...]}}` | **Primary** — returns list of MBIDs |
| `GET /1/metadata/recording/?recording_mbids=...` | Fetch metadata for MBIDs | `{"recordings": [{recording_mbid, title, artist-credit, release}]}` | **Required 2nd call** — full track info |
| `GET /1/user/{user}/listens` | User's recent listens | Get's user's listening history | For user context |

### ❌ Endpoints That Don't Work

| Endpoint | Issue | Current Usage | Fix |
|----------|-------|---------------|-----|
| `/1/user/{username}/playlists/createdfor` | **Does not exist or wrong structure** | `get_created_for_playlists()` | Replace with CF endpoint |
| `weekly_jams` (separate endpoint) | **No such endpoint** | `get_weekly_jams()` | Use CF endpoint, not separate |
| `last_week_jams` | **No such endpoint** | `get_last_week_jams()` | Either remove or use RSS feeds |

### Last.fm Official Endpoints (Working)

| Endpoint | Purpose | Notes |
|----------|---------|-------|
| `user.getTopArtists` | User's most-played artists | Safe, working, but NOT recommendations |
| `user.getTopTracks` | User's most-played tracks | Safe, working, but NOT recommendations |
| `user.getTopAlbums` | User's most-played albums | Safe, working, but NOT recommendations |
| `user.getRecentTracks` | Recent scrobbles | Working |
| `tag.getTopTracks` | Tracks with tag (discovery) | Possible recommendations alternative |

### Last.fm Unofficial/Problematic

| Endpoint | Issue | Used By |
|----------|-------|---------|
| `/player/station/user/{user}/recommended` | Web scraping required | lfm project (not official API) |
| User recommendations | **No official API exists** | Must use top tracks or scrape |

---

## Issues Found

### ListenBrainz Issues in sptnr

| Issue | Severity | Impact | Fix |
|-------|----------|--------|-----|
| Wrong endpoint in `get_created_for_playlists()` | **CRITICAL** | Playlist buttons return 404/errors | Use `/1/cf/recommendation/user/{user}/recording` |
| Missing 2nd metadata API call | **CRITICAL** | No track details (artist/title) | Add metadata fetch after getting MBIDs |
| Assumes "last week" endpoints exist | **HIGH** | Can't get last week recommendations | Remove or use RSS fallback |
| Response structure mismatch | **HIGH** | Parsing fails silently | Update to match CF response format |
| No error logging for failed matches | **MEDIUM** | Hard to debug failures | Add logging for MBID→track resolution |

### Last.fm Issues in sptnr

| Issue | Severity | Impact | Fix |
|-------|----------|--------|-----|
| Uses Top Tracks, not recommendations | **MEDIUM** | Returns user's own top songs (not discovery) | Consider using `tag.getTopTracks` or mention limitation |
| Inconsistent key handling | **LOW-MEDIUM** | May fail silently on some responses | Add defensive key extraction with fallbacks |
| No documented API limitations | **LOW** | Users expect true "recommendations" | Document that we use top tracks as proxy |

---

## Fixes Required

### Fix 1: ListenBrainz `get_created_for_playlists()` 

Replace the entire method with 2-call pattern:

```python
def get_created_for_playlists(self, username: str) -> dict:
    """Fetch ListenBrainz recommendations using CF API"""
    result = {
        "weekly_jams": [],
        "weekly_exploration": [],
        "last_week_jams": [],      # Note: Not available via API
        "last_week_exploration": [],  # Note: Not available via API
    }
    
    try:
        # STEP 1: Get recommendation MBIDs
        url = f"{self.base_url}/cf/recommendation/user/{username}/recording?count=200"
        res = self.session.get(url, headers=self.headers, timeout=(5, 30))
        res.raise_for_status()
        data = res.json()
        
        payload = data.get("payload", {})
        rec_mbids = payload.get("recordings", [])  # List of MBID strings
        
        if not rec_mbids:
            logger.warning(f"No recommendations from CF API for {username}")
            return result
        
        # STEP 2: Fetch metadata for those MBIDs (in chunks of 100)
        all_tracks = []
        for i in range(0, len(rec_mbids), 100):
            chunk = rec_mbids[i:i+100]
            mbid_list = ",".join(chunk)
            
            meta_url = f"{self.base_url}/metadata/recording/?recording_mbids={mbid_list}&inc=release+artist"
            meta_res = self.session.get(meta_url, timeout=(5, 30))
            meta_res.raise_for_status()
            meta_data = meta_res.json()
            
            recordings = meta_data.get("recordings", [])
            for rec in recordings:
                if not isinstance(rec, dict):
                    continue
                
                # Extract artist name
                artist_name = ""
                artist_credit = rec.get("artist-credit", [])
                if isinstance(artist_credit, list) and len(artist_credit) > 0:
                    first_artist = artist_credit[0]
                    if isinstance(first_artist, dict):
                        artist_obj = first_artist.get("artist", {})
                        artist_name = artist_obj.get("name", "") if isinstance(artist_obj, dict) else ""
                
                track = {
                    "artist_name": artist_name,
                    "track_name": rec.get("title", ""),
                    "release_name": rec.get("release", {}).get("title", "") if isinstance(rec.get("release"), dict) else "",
                    "recording_mbid": rec.get("id", ""),
                    "release_mbid": rec.get("release", {}).get("id", "") if isinstance(rec.get("release"), dict) else "",
                    "source": "listenbrainz-cf",
                }
                all_tracks.append(track)
        
        # For now, put all recommendations under weekly_jams
        # In future, could parse playlist names from different API call
        result["weekly_jams"] = all_tracks
        
        logger.info(f"Got {len(all_tracks)} CF recommendations for {username}")
        return result
        
    except Exception as e:
        logger.error(f"Failed to fetch CF recommendations for {username}: {e}")
        return result
```

### Fix 2: Last.fm Key Name Consistency

Add defensive key extraction utility:

```python
def _safe_get_artist_name(artist_obj):
    """Safely extract artist name from Last.fm response (handles #text wrapper)"""
    if not isinstance(artist_obj, dict):
        return str(artist_obj) if artist_obj else ""
    
    # Try different possible keys
    return (
        artist_obj.get("name", "")  # Standard key
        or artist_obj.get("#text", "")  # Wrapped in #text
        or (artist_obj.get("artist", {}).get("name", "") if isinstance(artist_obj.get("artist"), dict) else "")
    )
```

### Fix 3: Deprecate non-working methods

Update `get_weekly_jams()`, `get_last_week_jams()`, etc to just call `get_created_for_playlists()` and extract the relevant key:

```python
def get_weekly_jams(self, username: str) -> list:
    """Get Weekly Jams recommendations (uses CF API)"""
    created_for = self.get_created_for_playlists(username)
    return created_for.get("weekly_jams", [])

def get_weekly_exploration(self, username: str) -> list:
    """Get Weekly Exploration (not available via ListenBrainz API - consider RSS feeds)"""
    created_for = self.get_created_for_playlists(username)
    result = created_for.get("weekly_exploration", [])
    if not result:
        logger.warning(f"Weekly Exploration not available for {username} - ListenBrainz API limitation")
    return result

def get_last_week_jams(self, username: str) -> list:
    """Last week recommendations not available via ListenBrainz API"""
    logger.warning("get_last_week_jams: ListenBrainz doesn't provide archived weekly recommendations via API")
    return []

def get_last_week_exploration(self, username: str) -> list:
    """Last week exploration not available via ListenBrainz API"""
    logger.warning("get_last_week_exploration: ListenBrainz doesn't provide archived exploration via API")
    return []
```

### Fix 4: Last.fm Documentation

Add comment to playlist code:

```python
# NOTE: Last.fm official API does not have a "recommendations" endpoint.
# We use user.getTopTracks/Albums/Artists as a discovery proxy.
# For true recommendations, consider: tag.getTopTracks or web scraping
```

---

## What to Use from External Repos

### From Explo (LumePart/Explo)
✅ **Use**: 2-step CF API + metadata pattern  
✅ **Use**: Track metadata extraction from nested artist-credit structure  
✅ **Use**: Chunking MBIDs for metadata calls (100 at a time)  
✅ **Use**: Error handling and logging pattern  

### From lfm (xiffy/lfm)
✅ **Reference**: Inconsistent key naming documentation  
✅ **Reference**: Web scraping as Last.fm recommendations alternative  
⚠️ **Consider but verify**: lfm's approach to handling #text wrappers  

---

## Implementation Priority

1. **URGENT**: Fix `get_created_for_playlists()` to use CF API (2 calls)
2. **URGENT**: Update response parsing for new structure
3. **HIGH**: Add defensive key extraction for Last.fm
4. **MEDIUM**: Deprecate/document last_week methods
5. **MEDIUM**: Add logging for failed track matching
6. **LOW**: Consider RSS feeds for archived recommendations

---

## Testing Checklist

After implementing fixes:

- [ ] ListenBrainz weekly jams button returns tracks
- [ ] ListenBrainz weekly exploration button returns tracks
- [ ] Last.fm tracks button returns user's top tracks
- [ ] Last.fm albums button returns user's top albums
- [ ] No 404 errors in browser console
- [ ] Matching shows correct matched/missing track counts
- [ ] Playlist created with correct track count
- [ ] No SQL injection or parameter binding issues
