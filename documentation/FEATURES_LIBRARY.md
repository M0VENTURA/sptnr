# Library Features: Artists, Albums & Tracks

Browse, search, and manage your music library through the SPTNR web interface.

## Artists Page

### Overview
The Artists page displays all artists in your library with sortable columns and search.

**URL**: http://localhost:5000/artists

### Features

#### Artist List Table
Displays all artists with the following columns:
- **Artist Name**: Click to view artist detail page
- **Album Count**: Number of albums by this artist
- **Track Count**: Total tracks across all albums
- **Actions**: Quick action buttons

#### Sorting
- Click any column header to sort
- Click again to reverse sort order
- Default: Alphabetical by artist name

#### Search
- Search box at top of page
- Searches artist names in real-time
- Case-insensitive matching
- Clears with "X" button

#### Pagination
- Shows 50 artists per page by default
- Navigation buttons at bottom
- Jump to specific page number
- Shows total artist count

### Artist Detail Page

**URL**: http://localhost:5000/artist/<artist-name>

#### Artist Information
- Artist name (large heading)
- Total album count
- Total track count
- Optional artist image

#### Albums Section
Shows all albums by the artist in a grid layout:
- **Album Cover**: Click to view album details
- **Album Title**: Name of the album
- **Year**: Release year (if available)
- **Track Count**: Number of tracks on album
- **Album Type**: Studio, Compilation, Single, EP

#### Artist Actions

**Scan Artist**
- Rates all tracks for this artist
- Uses API data for popularity scores
- Updates existing ratings
- Shows progress indicator

**View Missing Releases**
- Checks MusicBrainz for discography
- Compares with your library
- Shows releases you don't have
- Integrates with download tools

**Create Essential Playlist**
- Generates playlist of top tracks
- Based on 5-star ratings and singles
- Automatically named "{Artist} - Essentials"
- Syncs to Navidrome

**qBittorrent Search** (if enabled)
- Opens inline search modal
- Pre-filled with "{Artist} discography"
- Search torrents directly
- Add downloads with one click

#### Artist Biography
- Fetched from Last.fm or MusicBrainz
- Shows artist background and history
- Collapses for long biographies
- "Read more" to expand

#### Singles Count
- Shows number of confirmed singles
- Helps identify hit tracks
- Links to singles view/filter

### Album Detail Page

**URL**: http://localhost:5000/album/<artist>/<album>

#### Album Header
- Album title and artist name
- Album cover art (if available)
- Release year
- Total track count
- Album-level statistics

#### Track List
All tracks in the album displayed in a table:

| # | Title | Rating | Duration | Single | Last Scanned |
|---|-------|--------|----------|--------|--------------|
| 1 | Song Title | ⭐⭐⭐⭐⭐ | 3:45 | Yes | 2024-01-15 |

Columns:
- **Track #**: Position on album
- **Title**: Track name (click for detail)
- **Rating**: Star rating (0-5 stars)
- **Duration**: Track length
- **Single Status**: Yes/No indicator
- **Last Scanned**: When rating was last updated

#### Album Actions

**Play Album** (if Navidrome URL configured)
- Opens album in Navidrome
- Starts playback from track 1
- External link to music player

**Scan Album**
- Rates all tracks in this album
- Faster than full artist scan
- Updates album statistics

**Download Album** (if integrations enabled)
- Search for album in download sources
- Options for qBittorrent and Soulseek
- Pre-fills search with "Artist - Album"

### Track Detail Page

**URL**: http://localhost:5000/track/<track-id>

#### Track Information (Read-Only)
- Track title and artist
- Album name (link to album)
- Track number and disc number
- Duration
- File path (if available)

#### Metadata Display
Shows additional metadata:
- **MusicBrainz ID**: MBID for track
- **Spotify ID**: Spotify track identifier
- **Last.fm Listeners**: Listener count
- **ListenBrainz Plays**: Total plays across users
- **Release Year**: Original release date
- **Last Scanned**: When rating was calculated

#### Rating Information
- **Current Rating**: Star rating (0-5)
- **Rating Source**: How rating was determined
- **Popularity Score**: Composite score breakdown
- **Single Status**: Confirmed single or not
- **Detection Confidence**: Low/Medium/High

#### Track Editing

**Edit Track Metadata**
Editable fields:
- Track Title
- Artist Name
- Album Name
- Star Rating (0-5 slider)
- Single Status (checkbox)
- Single Confidence (dropdown)

**Save Changes**
- Updates database immediately
- Syncs to Navidrome (if enabled)
- Shows success/error message
- Reloads track data

#### Track Actions

**Play in Navidrome**
- Opens track in Navidrome player
- Starts playback immediately
- External link

**Re-scan Track**
- Recalculates rating for this track
- Fetches fresh API data
- Updates all metadata
- Shows new rating

**View in Context**
- Link to album page
- Link to artist page
- Related tracks section

## Search Page

**URL**: http://localhost:5000/search

### Global Search
Search across all library content:
- Artists
- Albums  
- Tracks

#### Search Features
- Real-time results as you type
- Searches multiple fields:
  - Artist names
  - Album titles
  - Track titles
  - Metadata fields
- Results grouped by type
- Click any result to navigate to detail page

#### Search Filters
- **Type**: Artists, Albums, Tracks, or All
- **Rating**: Filter by star rating (e.g., 5-star only)
- **Singles**: Show only singles
- **Year**: Filter by release year range

#### Results Display
- **Artists**: Name, album count, track count
- **Albums**: Cover, title, artist, year
- **Tracks**: Title, artist, album, rating

## Navigation Tips

### Breadcrumbs
Every page shows navigation breadcrumbs:
```
Home > Artists > The Beatles > Abbey Road > Come Together
```
Click any breadcrumb to navigate back.

### Quick Links
- Artist page → Albums → Individual album
- Album page → Artist page (click artist name)
- Track page → Album → Artist
- Search → Direct to any result

### Keyboard Shortcuts
- `/` - Focus search box
- `Esc` - Clear search
- `Arrow keys` - Navigate search results

## Performance Notes

### Large Libraries
For libraries with thousands of artists:
- Artist list uses pagination
- Search is indexed for speed
- Lazy loading for album covers
- Consider using search instead of browsing

### Loading Times
- Initial artist list: <1 second
- Artist detail page: 1-2 seconds
- Album detail page: <1 second
- Track detail page: <1 second

### Caching
- Album covers cached in browser
- API responses cached in database
- Recent searches cached for speed

## Screenshots

### Artists List
![Artists Page](screenshots/artists_list.png)

### Artist Detail
![Artist Detail](screenshots/artist_detail.png)

### Album View
![Album Tracks](screenshots/album_view.png)

### Track Editor
![Edit Track](screenshots/track_edit.png)

## Related Documentation

- [Web UI Guide](WEB_UI_README.md) - Full web interface docs
- [Search Features](FEATURES_SEARCH.md) - Advanced search
- [Star Rating Algorithm](STAR_RATING_ALGORITHM.md) - How ratings work
- [Single Detection](SINGLE_DETECTION_FIX_SUMMARY.md) - Single identification

## Troubleshooting

### Artists Not Showing
- Run initial scan from dashboard
- Check Navidrome connection
- Verify database has data: `sqlite3 database/sptnr.db "SELECT COUNT(*) FROM artists;"`

### Album Covers Missing
- Covers fetched from Navidrome
- Check Navidrome has cover art
- Verify image URLs in browser console

### Ratings Not Updating
- Click "Re-scan" on track/album/artist
- Check API keys configured correctly
- Review logs for API errors

### Search Not Working
- Check JavaScript console for errors
- Verify `/api/search` endpoint responds
- Try exact match vs partial match
- Clear browser cache
