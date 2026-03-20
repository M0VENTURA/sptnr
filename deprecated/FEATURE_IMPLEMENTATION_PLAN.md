# Feature Implementation Plan - Similar Artists & Interactive Genres

**Status:** Work in Progress  
**Last Updated:** March 2, 2026  
**Estimated Timeline:** 3-4 hours

---

## Features Requested

### 1. ✅ Last.fm Tags Analysis (COMPLETE)
**Finding:** Last.fm tracks have no user-submitted tags available
- Discogs genres ARE working (11-12 per track)
- Album/artist tags ARE working
- Track-level tags returning empty - this is data availability, not code issue
**Solution:** Use fallback to Discogs + ListenBrainz when Last.fm unavailable

### 2. 🔄 Similar Artists with Images (IN PROGRESS)
**Requirement:**
- Fetch artist images along with similar artist names
- Store images in database alongside similar artist data
- Display images in UI

**Implementation Plan:**
1. Modify popularity.py similar artists fetch to include images from:
   - Last.fm artist image URL
   - AudioDB artist fanart
   - MusicBrainz Cover Art Archive
2. Update artists table schema if needed to store images per artist
3. Update API endpoints to return images
4. Update templates to display images

**Files to Modify:**
- `popularity.py` - Fetch images during similar artists lookup
- `api_clients/lastfm.py` - Extract image URLs from artist data
- `app.py` - API endpoint to serve images  
- `templates/(artist|album|track).html` - Display images

### 3. 🔄 Similar Artist Selection with Popup (NOT STARTED)
**Requirement:**
- Click similar artist name
- If exists in database → go to artist page
- If not in database → show popup with MusicBrainz search results
- Show albums with "Add to Queue" button

**Implementation Plan:**
1. Create `static/js/similar-artists-popup.js`
2. Add onclick handler to similar artist badges
3. Create AJAX endpoint `/api/artist/search` for MusicBrainz lookup
4. Create popup modal in templates
5. Add button handlers for queue addition

**Files to Modify:**
- `templates/base.html` - Add modal template
- `app.py` - Add `/api/artist/search` endpoint
- `static/js/main.js` or new file - Add popup logic

### 4. 🔄 Tag Styling Update (NOT STARTED)
**Requirement:**
- Change tag colors to dark blue (match genres)
- Match font size with genre badges
- Update across all pages

**Current Styling:**
```css
/* Genres use Bootstrap badge bg-secondary (dark gray) */
.badge.bg-secondary { background-color: #6c757d; }

/* Tags should match genres */
.badge.genre-badge { background-color: #004085; } /* Dark blue */
```

**Files to Modify:**
- `templates/base.html` - CSS updates
- `templates/(track|album|artist).html` - Apply consistent styling
- `templates/metadata_compare.html` - Update genre display

### 5. 🔄 Interactive Genre Selection (NOT STARTED)
**Requirement:**
- Click genres/tags to select them
- Aggregation modal showing genre sources
- Button to save selected genres to track/album/artist
- Changes reflected across all views

**Implementation Plan:**
1. Add `data-genre` attributes to genre badges
2. Create genre selection modal
3. Implement AJAX endpoint `/api/track/{id}/genres` (POST)
4. Update database: track.lastfm_tags, spotify_genres, etc.
5. Invalidate cache and refresh views

**Files to Modify:**
- `app.py` - Add genre management endpoints
- `templates/base.html` - Create genre selection modal
- `templates/(track|album|artist).html` - Add click handlers
- `static/js/main.js` - Genre selection logic

---

## Database Considerations

### Current Schema
```sql
-- Artists table
CREATE TABLE artists (
    id TEXT PRIMARY KEY,
    name TEXT,
    similar_artists_lastfm JSON,      -- Separate columns!
    similar_artists_listenbrainz JSON, -- No overwriting
    image_url TEXT,
    bio TEXT,
    ...
)

-- Tracks table
CREATE TABLE tracks (
    id TEXT PRIMARY KEY,
    title TEXT,
    spotify_genres JSON,      -- Multiple genre sources
    lastfm_tags JSON,
    discogs_genres JSON,
    musicbrainz_genres JSON,
    ...
)
```

### Needed Additions
- `artists.lastfm_artist_image_url` - Last.fm provided
- `artists.audiodb_artist_image_url` - AudioDB provided  
- `artists.musicbrainz_artist_image_url` - CoverArtArchive
- Consider consolidating to single `artist.image_url` with fallback priority

---

## Implementation Priority

1. **Phase 1 (High Priority):**
   - Add images to similar artists data
   - Update database schema
   - Display images in templates

2. **Phase 2 (Medium Priority):**
   - Tag styling updates (dark blue, font size)
   - Similar artist click handler + exists check

3. **Phase 3 (Lower Priority):**
   - MusicBrainz search popup
   - Interactive genre selection
   - Genre editing endpoints

---

## API Endpoints Needed

```
GET  /api/artist/<name>/similar - Get similar artists with images
GET  /api/artist/search?q=... - MusicBrainz search for artist
GET  /api/genres/<source>/<artist|track|album> - Get aggregated genres
POST /api/track/<id>/genres - Update track genres
POST /api/album/<artist>/<album>/genres - Update album genres
POST /api/artist/<name>/genres - Update artist genres
```

---

## Testing Checklist

- [ ] Similar artists display with images
- [ ] Clicking similar artist with existing artist goes to artist page
- [ ] Clicking similar artist without match shows search popup
- [ ] MusicBrainz popup shows albums
- [ ] "Add to Queue" button works for each album
- [ ] Tags display in dark blue
- [ ] Clicking genre shows aggregation modal
- [ ] Genre changes save to database
- [ ] Changes reflect on artist/album/track pages immediately

---

## Code Examples

### Modified similar artist fetch with images:
```python
# In popularity.py
similar_artists_lastfm = [
    {
        "name": "Three Days Grace",
        "mbid": "...",
        "image_url": "https://..."  # NEW
    },
    ...
]
```

### Updated API response:
```json
{
  "similar_artists": [
    {
      "name": "Three Days Grace",
      "image": "https://...",
      "exists_in_db": true/false
    }
  ]
}
```

