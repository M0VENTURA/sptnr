# Singles Display & Retrieval - Comprehensive Code Analysis

## Overview
This document maps out all the locations where singles are being displayed or listed to users in the SPTNR application. It covers HTML templates, Flask routes, API endpoints, and JavaScript code.

---

## 1. HTML TEMPLATES

### 1.1 Album View - [templates/album.html](templates/album.html)
**Purpose:** Display tracks in an album with single status information

**Singles-related columns displayed:**
- **Column: "Single"** (visible on desktop lg+ screens)
  - Displays "Single" badge if `track.is_single == 1`
  - Hidden on mobile devices
  - Badge styling: `badge bg-info`

- **Column: "Conf"** (Confidence - visible on desktop lg+ screens)
  - Displays single detection confidence level: low/medium/high
  - Badge styling: `badge bg-secondary`
  - Font size: 0.75rem

**Code locations:**
- Line ~240: Multi-disc album view - single status display
- Line ~310: Single-disc album view - single status display
- Both sections use identical single status column markup:
```html
<td class="d-none d-lg-table-cell text-center">
    {% if track.is_single == 1 %}
        <span class="badge bg-info">Single</span>
    {% else %}
        <span class="text-muted">—</span>
    {% endif %}
</td>
<td class="d-none d-lg-table-cell text-center">
    {% if track.single_confidence %}
        <span class="badge bg-secondary" style="font-size: 0.75rem;">
            {{ track.single_confidence }}
        </span>
    {% else %}
        <span class="text-muted">—</span>
    {% endif %}
</td>
```

**Data source:** Track data passed from `album_detail()` Flask route

---

### 1.2 Search Results Template - [templates/search.html](templates/search.html)
**Purpose:** Display search results for artists, albums, and tracks

**Singles-related display:**
- No direct singles list
- Tracks table shows individual track ratings but NOT single status
- All search results are retrieved via `/api/search` endpoint

**Table columns shown for tracks:**
- Title
- Artist
- Album
- Rating (stars)
- Action (View button)

---

### 1.3 Dashboard Template - [templates/dashboard.html](templates/dashboard.html)
**Purpose:** Main overview of library statistics

**Singles statistics displayed:**
- Total singles count from database query: `COUNT(*) FROM tracks WHERE is_single = 1`
- Displayed as a metric card: `singles_count`

---

### 1.4 Artist Detail Template - [templates/artist.html](templates/artist.html)
**Purpose:** Show artist's albums and overall statistics

**Singles-related display per album:**
- `singles_count` - number of singles detected in each album
- Shown alongside other album stats: track count, average rating
- Used in album listing for the artist

---

## 2. FLASK ROUTES & API ENDPOINTS

### 2.1 Dashboard Route - [app.py](app.py) Line ~490
**Route:** `GET /dashboard`
**Handler:** `dashboard()`

**Singles data retrieved:**
```python
cursor.execute("SELECT COUNT(*) FROM tracks WHERE is_single = 1")
singles_count = cursor.fetchone()[0]
```

**Returns to template:**
- `singles_count` - total number of tracks marked as singles
- Used in main statistics display

---

### 2.2 Artist Detail Route - [app.py](app.py) Line ~525
**Route:** `GET /artist/<path:name>`
**Handler:** `artist_detail(name)`

**Singles data per album:**
```python
cursor.execute("""
    SELECT 
        album,
        COUNT(*) as track_count,
        AVG(stars) as avg_stars,
        SUM(CASE WHEN is_single = 1 THEN 1 ELSE 0 END) as singles_count,
        MAX(last_scanned) as last_updated
    FROM tracks
    WHERE artist = ?
    GROUP BY album
    ORDER BY album COLLATE NOCASE
""", (name,))
```

**Returns:**
- `singles_count` - number of singles per album
- Passed to `artist.html` template for display

---

### 2.3 Album Detail Route - [app.py](app.py) Line ~570
**Route:** `GET /album/<path:artist>/<path:album>`
**Handler:** `album_detail(artist, album)`

**Singles data retrieved:**
```python
cursor.execute("""
    SELECT *
    FROM tracks
    WHERE artist = ? AND album = ?
    ORDER BY COALESCE(disc_number, 1), COALESCE(track_number, 999), title COLLATE NOCASE
""", (artist, album))
tracks_data = cursor.fetchall()
```

**Single-related fields in track data:**
- `track.is_single` - binary flag (0 or 1)
- `track.single_confidence` - confidence level (low/medium/high)
- `track.single_sources` - array of sources that detected it as single

**Template receives:**
- All track records with full single metadata
- Displayed in HTML table with conditional badges

---

### 2.4 Search API Endpoint - [app.py](app.py) Line ~615
**Route:** `POST /api/search`
**Handler:** `api_search()`

**What gets searched:**
1. **Artists search** - no single-specific fields
2. **Albums search** - returns track_count and avg_stars
3. **Tracks search** - returns track data including stars but NOT single status

**Note:** Singles are not explicitly included in search results, but individual track records could contain single data if needed

---

### 2.5 Track Detail Route - [app.py](app.py) Line ~670
**Route:** `GET /track/<track_id>`
**Handler:** `track_detail(track_id)`

**Singles data retrieved:**
```python
cursor.execute("SELECT * FROM tracks WHERE id = ?", (track_id,))
track = cursor.fetchone()
```

**Single-related fields in track record:**
- `track['is_single']` - boolean flag
- `track['single_confidence']` - confidence level
- (Additional fields stored but not shown in basic track template)

---

### 2.6 Metadata API Endpoint - [app.py](app.py) Line ~2260
**Route:** `GET /api/metadata`
**Handler:** `api_metadata()`

**Parameters:** 
- `type` - 'track', 'album', or 'artist'
- `id` - identifier

**For tracks (type='track'):**
- Retrieves from database: `get_track_metadata_from_db(identifier, DB_PATH)`
- Returns comprehensive metadata including:
  - `is_single` - boolean
  - `single_confidence` - confidence level
  - Additional scoring metadata from database

**For albums (type='album'):**
```python
cursor.execute("""
    SELECT 
        AVG(stars) as avg_stars,
        COUNT(*) as track_count,
        SUM(CASE WHEN is_single = 1 THEN 1 ELSE 0 END) as singles_count,
        MAX(last_scanned) as last_scanned
    FROM tracks
    WHERE artist = ? AND album = ?
""", (artist, album))
```
- Returns `singles_detected` - count of singles in album

---

## 3. DATABASE SCHEMA - SINGLES FIELDS

The following fields in the `tracks` table store singles information:

| Field | Type | Purpose |
|-------|------|---------|
| `is_single` | INTEGER (0/1) | Whether track is detected as single |
| `single_confidence` | TEXT | Detection confidence: 'low', 'medium', 'high' |
| `single_sources` | JSON/TEXT | Array of sources that identified it as single |
| `is_spotify_single` | INTEGER (0/1) | Spotify identified it as single |
| `spotify_total_tracks` | INTEGER | Album track count from Spotify |
| `spotify_album_type` | TEXT | Album type from Spotify: 'single', 'album', etc. |
| `discogs_single_confirmed` | INTEGER (0/1) | Discogs format confirmed as Single |

---

## 4. JAVASCRIPT CODE - DYNAMIC SINGLES LOADING

### 4.1 Base Template JavaScript - [templates/base.html](templates/base.html)
**Metadata modal functionality (Line ~909):**

```javascript
async function lookupMetadata(type, identifier) {
    const response = await fetch(`/api/metadata?type=${type}&id=${encodeURIComponent(identifier)}`);
    const data = await response.json();
    
    // Field categories for display:
    const fieldCategories = {
        'IDs': ['mbid', 'musicbrainz_id', 'isrc', 'ean', 'spotify_uri'],
        'Scores': ['spotify_score', 'lastfm_ratio', 'listenbrainz_score', 'final_score'],
        // ... other categories
    };
}
```

**Note:** The metadata modal does NOT include single-specific fields in its default categories, but they could be added if needed.

---

## 5. SUMMARY OF SINGLES DISPLAY LOCATIONS

### Where Singles Are Currently Shown:
1. ✅ **Album view** - Single status badge and confidence level per track
2. ✅ **Artist view** - Singles count per album
3. ✅ **Dashboard** - Total singles count in library
4. ✅ **Track detail page** - Full metadata available (via database)
5. ✅ **Metadata API** - Singles data returned in JSON response

### Where Singles Are NOT Currently Shown:
1. ❌ **Search results** - No single status in track results table
2. ❌ **Smart playlists** - No filtering or display of single status
3. ❌ **Downloads UI** - No single detection for downloaded tracks
4. ❌ **Bookmarks** - No single status displayed

---

## 6. BACKEND SINGLE DETECTION & STORAGE

### Data Pipeline:
1. **Detection:** [singledetection.py](singledetection.py)
   - `rate_track_single_detection()` - Main detection function
   - Checks: Discogs, MusicBrainz, YouTube, Last.fm, Spotify
   - Stores results in `track_data` dict

2. **Storage:** [app.py](app.py) + [start.py](start.py)
   - `scan_artist_to_db()` - Persists track data to database
   - Stores singles fields in tracks table

3. **Retrieval:** Database queries throughout [app.py](app.py)
   - All track queries automatically retrieve single fields
   - No special query needed - fields are always included

---

## 7. CONFIGURATION & KNOBS

### Config options affecting singles display/behavior:

**From [start.py](start.py) & config.yaml:**
```yaml
features:
    use_lastfm_single: true                    # Last.fm single detection
    secondary_single_lookup_enabled: true      # Secondary sources
    singles_require_strong_source_for_5_star: false  # Rating requirement
    known_singles:                             # Manual singles list
        Artist Name:
            - "Song Title"
```

---

## 8. API DATA STRUCTURE FOR SINGLES

### Track Record with Singles Fields:
```json
{
    "id": "track_id",
    "title": "Song Title",
    "artist": "Artist Name",
    "album": "Album Name",
    "is_single": 1,
    "single_confidence": "high",
    "single_sources": ["discogs", "musicbrainz"],
    "is_spotify_single": true,
    "spotify_total_tracks": 1,
    "stars": 5,
    "final_score": 95.5
}
```

### Album Record with Singles Summary:
```json
{
    "album": "Album Name",
    "artist": "Artist Name",
    "track_count": 12,
    "singles_count": 2,
    "avg_stars": 4.2
}
```

---

## 9. KEY TAKEAWAYS

1. **Singles are fully tracked in database** with confidence levels and detection sources
2. **Album view shows singles prominently** with badges and confidence indicators
3. **Dashboard displays total singles count** for quick statistics
4. **API endpoints return singles data** in metadata responses
5. **No dedicated "Singles List" view** exists - singles are shown contextually with albums/tracks
6. **Single detection is sophisticated** - uses 5+ different sources for high-confidence detection
7. **Frontend selectively shows singles** - hidden on mobile, only shown on larger screens in album view

