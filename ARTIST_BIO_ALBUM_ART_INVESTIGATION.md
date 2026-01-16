# Artist Biography & Album Art Update Investigation

## Question
**Can you confirm that the artist biography and album art are meant to update during the popularity scan?**

## Answer: **NO**

The artist biography and album art are **NOT** updated during the popularity scan. These are handled separately from the popularity scoring system.

---

## Current Behavior

### 🔍 What the Popularity Scan DOES Update

The popularity scan (`popularity.py`) focuses exclusively on these fields:

| Field | Description |
|-------|-------------|
| `popularity_score` | Calculated from Spotify + Last.fm data |
| `stars` | Star rating (1-5) based on popularity |
| `is_single` | Whether track is detected as a single |
| `single_confidence` | Confidence level (high/medium/low) |
| `single_sources` | JSON list of sources that confirmed single status |
| `last_scanned` | Timestamp of when track was last scanned |
| `spotify_artist_id` | Cached Spotify artist ID for the track's artist |

**Plus:** Essential playlists are created/updated for each artist after scanning.

### ❌ What the Popularity Scan DOES NOT Update

1. **Artist Biography**
2. **Album Cover Art**
3. **Genre information** (handled separately)
4. **Metadata fields** (handled by Beets/Navidrome imports)

---

## How Artist Biography Works

### Implementation
- **File:** `app.py` (line ~1695) and `artist_api_additions.py` (line 17-72)
- **Endpoint:** `GET /api/artist/bio?name={artist_name}`
- **Storage:** **NOT stored in database** - fetched on-demand from external APIs
- **Sources:** 
  1. **MusicBrainz API** (primary) - via artist MBID lookup
  2. **Discogs API** (fallback) - if MusicBrainz has no bio

### Process Flow
```
User visits artist page
  ↓
Frontend calls /api/artist/bio
  ↓
Backend checks database for beets_artist_mbid
  ↓
If MBID exists → Query MusicBrainz API for bio
  ↓
If MusicBrainz has no bio → Try Discogs API
  ↓
Return bio to frontend (NOT saved to database)
```

### Database Requirements
- Requires `beets_artist_mbid` field to be populated
- This field is set during Beets metadata import, NOT during popularity scan

---

## How Album Cover Art Works

### Implementation
- **File:** `app.py` (multiple sections)
- **Endpoint:** `GET /api/album/art?artist={artist}&album={album}`
- **Storage:** Stored in `cover_art_url` (TEXT) field in tracks table
- **Sources:**
  1. Custom `album_art` table (user-uploaded)
  2. Database `cover_art_url` field (from MusicBrainz)
  3. Navidrome server (via Subsonic API)
  4. MusicBrainz Cover Art Archive
  5. iTunes Search API
  6. Discogs API

### When is `cover_art_url` Updated?

1. **During MusicBrainz Release Detection** (app.py lines 1558-1567)
   - When checking for missing releases, existing albums get their cover art updated
   - Only happens when user manually triggers "Check for Missing Releases"

2. **Manual MBID Application** (app.py lines 7510-7550)
   - When user manually applies MusicBrainz ID via web UI
   - Updates `cover_art_url` for all tracks in the album

3. **NOT during popularity scan** ❌

### Process Flow
```
User views album page
  ↓
Frontend requests album art via /api/album/art
  ↓
Backend checks (in order):
  1. Custom album_art table
  2. Database cover_art_url field
  3. Navidrome server
  4. MusicBrainz Cover Art Archive
  5. iTunes API
  6. Discogs API
  ↓
Return image (or placeholder if none found)
```

---

## Evidence from Code

### popularity.py Analysis
Lines 806-813 show the ONLY fields updated during batch commit:
```python
cursor.executemany(
    "UPDATE tracks SET popularity_score = ? WHERE id = ?",
    track_updates
)
```

Lines 997-1005 show singles detection updates:
```python
cursor.executemany(
    """UPDATE tracks 
    SET is_single = ?, single_confidence = ?, single_sources = ?
    WHERE id = ?""",
    singles_updates
)
```

Lines 1074-1077 show star rating updates:
```python
cursor.executemany(
    """UPDATE tracks SET stars = ? WHERE id = ?""",
    updates
)
```

**No mention of `cover_art_url` or any biography field.**

### Database Schema (check_db.py)
```python
"cover_art_url": "TEXT",                # Album cover art URL from MusicBrainz
"beets_artist_mbid": "TEXT",            # Artist MBID from beets (used for bio lookup)
```

**Note:** There is NO `artist_bio` or `artist_biography` field in the database schema.

---

## Recommendations

### Option 1: Keep Current Behavior ✅ (Recommended)
The current design is intentional and efficient:
- **Biography:** Fetched on-demand (saves database space, always current)
- **Album Art:** Updated during metadata scans (separate concern from popularity)
- **Popularity Scan:** Focused on its core purpose (scoring and rating)

**Pros:**
- Separation of concerns
- Efficient database usage
- Biography is always fresh from source
- Faster popularity scans

**Cons:**
- Biography requires extra API call when viewing artist page
- Album art might be missing if never triggered

### Option 2: Add to Popularity Scan ⚠️ (Not Recommended)
We could add biography and album art fetching to the popularity scan.

**Implementation would require:**
1. Add `artist_biography` TEXT field to database schema
2. Modify `popularity.py` to fetch and store biography per artist
3. Fetch and update `cover_art_url` for each album during scan
4. Significantly increase API calls during scan
5. Increase scan duration by 30-50%

**Pros:**
- Everything updated in one scan
- Album art always available

**Cons:**
- Much longer scan times
- More API rate limit issues
- Mixes concerns (popularity vs metadata)
- Biography data can become stale
- Increased database size

---

## Conclusion

**The current behavior is correct and intentional.** Artist biography and album art are NOT meant to update during the popularity scan.

**Why this makes sense:**
1. **Popularity scan** = Track scoring and rating (core function)
2. **Metadata scans** (Beets/Navidrome) = Artist/album metadata including art
3. **Biography** = On-demand feature (viewer-triggered, not scan-triggered)

These are different concerns and should remain separate for efficiency and maintainability.

---

## If You Need These Features Updated

### To Update Album Art for Existing Albums:
1. Go to artist page in web UI
2. Click "Check for Missing Releases" button
3. This will fetch MusicBrainz data and update `cover_art_url` for existing albums

### To Get Artist Biography:
1. Ensure `beets_artist_mbid` is populated via Beets import
2. Visit artist page - biography auto-loads from MusicBrainz/Discogs
3. No manual action needed (fetched on page load)

### To Manually Set Album Art:
1. Go to album page
2. Click the "Change album art" button below the album cover
3. Search MusicBrainz, Discogs, or Spotify, or provide manual URL
4. Apply selected image

---

**Created:** 2026-01-16  
**Purpose:** Clarify that biography and album art are NOT updated during popularity scan
