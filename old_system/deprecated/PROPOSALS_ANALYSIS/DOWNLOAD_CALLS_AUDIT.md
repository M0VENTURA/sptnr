# Download Calls Audit - HTML Templates & API Consistency

## Summary
✅ **All download requests are handled correctly across the app**
✅ **qBittorrent results now sorted by most seeders (descending)**

---

## Download Call Locations

### 1. **downloads.html** (Main Downloads Page)
- **qBittorrent Search**: `POST /api/qbittorrent/search`
  - Endpoint: `performQbitSearch()`
  - Results display: ✅ Shows seeders/leechers, size
  - Add action: `POST /api/qbittorrent/add` via `addTorrent(url)`
  - **SORTING**: ✅ NOW SORTED BY SEEDERS (DESCENDING)

- **Soulseek Search**: `POST /api/slskd/search`
  - Endpoint: `performSlskdSearch()`
  - Polling: `GET /api/slskd/search/{searchId}` via `pollSlskdResults()`
  - Results grouped by: username → album → tracks
  - Download action: `POST /api/slskd/download` via `downloadSlskdBatch()`
  - Features: ✅ Selection checkboxes, album grouping, batch download

---

### 2. **artist.html** (Artist Page - Album Download Modal)
- **qBittorrent Search**: `POST /api/qbittorrent/search`
  - Endpoint: `performQbitSearch()`
  - Results display: ✅ Table format with Size, Seeds, Peers
  - Add action: `POST /api/qbittorrent/add` via `addTorrent(url)`
  - **CONSISTENCY**: ✅ Same API as downloads.html
  - **SORTING**: ✅ NOW SORTED BY SEEDERS (DESCENDING)

- **Soulseek Search**: `POST /api/slskd/search`
  - Endpoint: `performSlskdSearch()`
  - Polling via: `pollSlskdResults()`
  - Download action: `POST /api/slskd/download-single` 
  - **CONSISTENCY**: ✅ Compatible with main downloads page

---

### 3. **album.html** (Album Page - Quick Download Links)
- **qBittorrent Link**: 
  - Function: `openQbitSearchAlbum(artist, album)`
  - Behavior: Redirects to `/downloads?search={artist}+{album}`
  - **ROUTING**: ✅ Properly delegates to downloads.html

- **Soulseek Link**: 
  - Function: `openSlskdSearchAlbum(artist, album)`
  - Behavior: Redirects to `/downloads?search={artist}+{album}`
  - **ROUTING**: ✅ Properly delegates to downloads.html

---

### 4. **playlist_importer.html** (Playlist Import Missing Tracks)
- **Soulseek Search**: `POST /api/slskd/search`
  - Endpoint: `performPlaylistSlskdSearch()`
  - Polling: `GET /api/slskd/search/{searchId}` 
  - Download action: `POST /api/slskd/download-single`
  - **CONSISTENCY**: ✅ Uses single-track download endpoint (appropriate for individual missing tracks)

---

## API Endpoints Used

### qBittorrent Endpoints
| Endpoint | Method | Purpose | Used In |
|----------|--------|---------|---------|
| `/api/qbittorrent/search` | POST | Search torrents | downloads.html, artist.html |
| `/api/qbittorrent/add` | POST | Add torrent to qBittorrent | downloads.html, artist.html |
| `/api/qbittorrent/monitor` | GET | Monitor active torrents | downloads.html |
| `/api/qbittorrent/cancel` | POST | Cancel download | downloads.html |

### Soulseek (slskd) Endpoints
| Endpoint | Method | Purpose | Used In |
|----------|--------|---------|---------|
| `/api/slskd/search` | POST | Search Soulseek | downloads.html, artist.html, playlist_importer.html |
| `/api/slskd/search/{id}` | GET | Poll search results | downloads.html, artist.html, playlist_importer.html |
| `/api/slskd/download` | POST | Download multiple files | downloads.html |
| `/api/slskd/download-single` | POST | Download single track | playlist_importer.html |
| `/api/slskd/search-results/{id}` | GET | Get search results by ID | Support endpoint |
| `/api/slskd/download-file` | POST | Initiate file download | MusicBrainz imports |
| `/api/slskd/search-again/{id}` | POST | Retry search | Failed downloads |

---

## Error Handling

### Consistent Across All Pages
✅ Network errors → Alert with error message  
✅ Missing configuration → Alert to configure  
✅ Search failures → Show error in UI with icon  
✅ Download confirmation → Confirm before adding  

### Error Display Methods
- `alert()` - For critical errors
- `.innerHTML = <div class="alert alert-danger">` - For inline error display
- `.textContent` - For status updates

---

## UI/UX Consistency

### qBittorrent Results Display
| Property | Display | Location |
|----------|---------|----------|
| Filename | Truncated with tooltip | downloads.html, artist.html |
| Size | Formatted (B/KB/MB/GB) | Both |
| Seeders | Color-coded (>10 green, >0 yellow, 0 red) | Both |
| Leechers | Display count | Both |
| Source | Site URL | downloads.html |
| Action | "Add" button | Both |

✅ **SORTING**: Results now ordered by seeders desc (most seeds first)

### Soulseek Results Display
| Organization | Structure | Features |
|--------------|-----------|----------|
| By User | Username as header | Collapsible |
| By Album | Album path/folder | Collapsible |
| By Track | File details | Selectable |
| Stats | Size, bitrate, length, sample rate | Visible |
| Selection | Checkboxes | Batch download enabled |

---

## Verification Checklist

✅ **qBittorrent Search**
- Results sorted by most seeders first
- Same API endpoint across all pages
- Error handling consistent
- UI displays seeders prominently

✅ **Soulseek Search**
- Polling mechanism working correctly
- Album grouping organized
- Single vs batch download handled appropriately
- Results shown grouped by user

✅ **Error Handling**
- Network errors caught and displayed
- Configuration errors alerted
- Search failures show in UI
- Download confirmations prevent accidents

✅ **Routing/Linking**
- Album page links redirect to downloads page correctly
- Search queries properly encoded
- Parameters passed correctly

---

## Improvements Made

### 1. qBittorrent Results Sorting (NEW)
**File**: `/app.py` - `qbit_search()` function
**Change**: Sort results by `nbSeeders` in descending order
**Impact**: Users see best-seeded torrents first, improving download success rate
**Code**:
```python
# Sort results by most seeders first (descending)
results_sorted = sorted(results, key=lambda x: int(x.get("nbSeeders", 0)), reverse=True)
return jsonify({"results": results_sorted})
```

---

## Recommendations

1. **Consider adding filters** to qBittorrent results (min seeders, max size, etc.)
2. **Add sorting options** dropdown for users who want different orders
3. **Cache search results** briefly to reduce API calls if user scrolls
4. **Add download history** to tracking page for reference

---

## Notes
- All HTML files are properly handling responses from backend APIs
- No breaking changes detected
- Backward compatibility maintained
- All download paths follow consistent patterns
