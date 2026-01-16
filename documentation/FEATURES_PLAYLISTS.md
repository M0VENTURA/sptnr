# Playlist Management

SPTNR provides comprehensive playlist management including smart playlists, Spotify imports, and Navidrome integration.

## Playlist Features Overview

- **Smart Playlists**: Auto-generated based on ratings, genres, years
- **Spotify Import**: Import playlists from Spotify to Navidrome
- **Essential Playlists**: Artist-specific top tracks playlists
- **Manual Playlists**: Create custom playlists via Navidrome
- **Bookmarks**: Quick access to favorite playlists

## Smart Playlists

**URL**: http://localhost:5000/smart_playlists

### What Are Smart Playlists?

Smart playlists automatically update based on rules and criteria. SPTNR generates playlists using:
- Star ratings
- Single status
- Release years
- Genres
- Play counts
- Artist information

### Available Smart Playlists

#### Top Rated (5 Stars)
- All tracks rated 5 stars
- Updates when new 5-star ratings are added
- Perfect for "best of library" collections
- Syncs to Navidrome as "SPTNR - 5 Stars"

#### Singles Collection
- All tracks marked as singles
- Detected via metadata and APIs
- Hit songs across all artists
- Syncs as "SPTNR - Singles"

#### Recent Additions
- Tracks added in last 30 days
- Based on scan date
- Discover new music easily
- Syncs as "SPTNR - Recently Added"

#### By Decade
- Separate playlists for each decade
- 1960s, 1970s, 1980s, 1990s, 2000s, 2010s, 2020s
- Based on release year metadata
- Syncs as "SPTNR - 1980s", etc.

#### By Genre
- Auto-generated for each genre
- Fetched from MusicBrainz and Last.fm
- Top 50 tracks per genre
- Syncs as "SPTNR - Genre: Rock", etc.

### Creating Smart Playlists

1. Navigate to Smart Playlists page
2. Select playlist type from dropdown
3. Configure criteria:
   - Minimum rating
   - Date range
   - Maximum tracks
   - Sort order
4. Click "Generate Playlist"
5. Preview tracks before saving
6. Click "Save to Navidrome"

### Smart Playlist Settings

**Rating Threshold**
- Minimum star rating (1-5)
- Default: 4 stars
- Higher = more selective

**Track Limit**
- Maximum number of tracks
- Default: 100
- Range: 10-1000

**Sort Order**
- Rating (highest first)
- Release date (newest first)
- Artist name (alphabetical)
- Random

**Auto-Update**
- Enable automatic updates
- Frequency: Daily, weekly, monthly
- Updates happen during scans

## Spotify Playlist Import

**URL**: http://localhost:5000/playlist_importer

### Features

Import playlists from your Spotify account to Navidrome:
- Match tracks to your local library
- Show match percentage
- Handle missing tracks gracefully
- Create Navidrome playlist automatically

### Setup

1. **Spotify Authentication**
   - Ensure Spotify API credentials configured
   - Client ID and Secret in config.yaml
   - See [Configuration Guide](MULTI_USER_CONFIG_GUIDE.md)

2. **Navidrome Connection**
   - Verify Navidrome URL and credentials
   - Test connection on Config page

### Import Process

#### Step 1: List Spotify Playlists
1. Navigate to Playlist Importer
2. Click "Fetch My Playlists"
3. See all your Spotify playlists
4. Shows track count for each

#### Step 2: Select Playlist
1. Click on playlist name
2. View all tracks in playlist
3. See match status for each track:
   - ✅ **Found**: Track exists in library
   - ❌ **Missing**: Track not in library
   - ⚠️ **Partial**: Similar track found

#### Step 3: Review Matches
- Match percentage shown at top
- Green: 100% matches
- Yellow: 70-99% matches
- Red: <70% matches

#### Step 4: Import
1. Click "Import to Navidrome"
2. Choose import option:
   - **Matched Only**: Import only found tracks
   - **All Tracks**: Create playlist, note missing
3. Enter playlist name (default: original name)
4. Click "Create Playlist"

#### Step 5: Verify
- Check Navidrome for new playlist
- Play playlist to verify tracks
- Edit playlist in Navidrome if needed

### Matching Algorithm

SPTNR matches tracks using multiple criteria:

1. **Exact Match**
   - Artist name
   - Track title
   - Album name (optional)

2. **Fuzzy Match**
   - Handles variations in spelling
   - Ignores "featuring" artists
   - Matches remasters to originals

3. **Duration Match**
   - Compares track lengths
   - Allows ±10 seconds difference
   - Helps distinguish remixes

4. **ISRC/Spotify ID**
   - Uses unique identifiers if available
   - Most accurate matching
   - Requires metadata in library

### Handling Missing Tracks

When tracks don't match:

**Option 1: Download Missing**
- View list of missing tracks
- Click "Search Downloads"
- Automatically searches qBittorrent/Soulseek
- Import after download

**Option 2: Import Anyway**
- Create placeholder entries
- Shows "unavailable" in Navidrome
- Fill in later when tracks added

**Option 3: Skip Missing**
- Only import matched tracks
- Cleanest playlists
- May lose playlist context

## Essential Playlists

Create "best of" playlists for individual artists.

### From Artist Page

1. Navigate to artist page
2. Click "Create Essential Playlist"
3. Playlist auto-generated with:
   - All 5-star tracks
   - All confirmed singles
   - Top 20 highest-rated tracks
4. Syncs to Navidrome as "{Artist} - Essentials"

### Criteria

Essential playlists include:
- 5-star rated tracks
- Tracks marked as singles
- Tracks with high Last.fm play counts
- Tracks with high Spotify popularity
- Maximum 25 tracks per playlist

### Use Cases
- Quick artist introduction
- Party/background music
- Artist discovery for others
- Compilation for sharing

## Playlist Manager

**URL**: http://localhost:5000/playlist_manager

### View All Playlists

See playlists from all sources:
- Navidrome playlists
- SPTNR-created playlists
- Imported Spotify playlists
- Smart playlists

### Playlist Information
- Name and description
- Track count
- Duration
- Last updated
- Source (Navidrome/SPTNR/Spotify)

### Actions

**Edit Playlist**
- Opens in Navidrome
- Edit tracks, order, metadata
- Changes sync back to SPTNR

**Delete Playlist**
- Remove from Navidrome
- Confirm before deletion
- SPTNR playlists can be regenerated

**Refresh Playlist**
- Update track list
- Re-fetch from Navidrome
- Sync latest changes

**Export Playlist**
- Download as M3U
- Share with others
- Backup playlists

## Bookmarks

Quick access to favorite playlists and items.

### Adding Bookmarks

From any page, bookmark:
- Artists
- Albums
- Playlists
- Smart playlists

Click ⭐ (star) icon to bookmark.

### Bookmark Dropdown

Top navigation bar shows bookmark menu:
1. Click "Bookmarks" dropdown
2. See all bookmarked items
3. Organized by type
4. Click to navigate directly

### Managing Bookmarks

**Add Bookmark**
- Click star icon on item
- Automatically added to bookmarks
- Appears in dropdown instantly

**Remove Bookmark**
- Click star icon again (turns empty)
- Removes from bookmarks
- Updates dropdown

**Organize Bookmarks**
- Drag to reorder (future feature)
- Group by type
- Search bookmarks

## Best Practices

### Smart Playlists
- Update regularly (weekly)
- Keep track limits reasonable (50-200)
- Use meaningful criteria
- Test before syncing to Navidrome

### Spotify Imports
- Review match percentages
- Download missing tracks first
- Import in batches
- Verify playlists after import

### Essential Playlists
- Create for favorite artists
- Update after rating scans
- Use for quick listening
- Share with friends

### Playlist Organization
- Use consistent naming
- Prefix SPTNR playlists: "SPTNR - Name"
- Group by genre or mood
- Delete unused playlists

## Troubleshooting

### Smart Playlist Issues

**No Tracks in Playlist**
- Check criteria aren't too restrictive
- Verify tracks exist with those ratings
- Run rating scan first
- Check database has data

**Playlist Won't Update**
- Disable auto-update and re-enable
- Manually trigger update
- Check scan completed successfully
- Verify Navidrome connection

### Spotify Import Issues

**Can't Fetch Playlists**
- Check Spotify credentials
- Verify API permissions
- Re-authenticate if needed
- Check Spotify account has playlists

**Low Match Percentage**
- Library may not have those tracks
- Try fuzzy matching option
- Check artist/track name formatting
- Download missing tracks first

**Import Fails**
- Check Navidrome connection
- Verify playlist name is unique
- Ensure no special characters
- Check Navidrome API permissions

### Navidrome Sync Issues

**Playlist Not Appearing**
- Wait for Navidrome scan
- Manually refresh Navidrome
- Check playlist was created successfully
- Review Navidrome logs

**Tracks Missing from Playlist**
- Verify tracks exist in Navidrome
- Check file paths are correct
- Re-import playlist
- Verify track IDs match

## Screenshots

### Smart Playlists Page
![Smart Playlists](screenshots/smart_playlists.png)

### Spotify Import
![Playlist Import](screenshots/spotify_import.png)

### Essential Playlist
![Essential Playlist](screenshots/essential_playlist.png)

### Bookmarks Dropdown
![Bookmarks](screenshots/bookmarks_dropdown.png)

## Related Documentation

- [Web UI Guide](WEB_UI_README.md) - Full interface documentation
- [Spotify Integration](SPOTIFY_PLAYLIST_IMPORT.md) - Technical details
- [Configuration](MULTI_USER_CONFIG_GUIDE.md) - API setup
- [Bookmark Feature](BOOKMARK_DROPDOWN_FEATURE.md) - Bookmark details

## Future Features

Planned enhancements:
- Collaborative playlists
- Playlist sharing between users
- Advanced smart playlist rules
- AI-powered playlist generation
- Mood-based playlists
- Similar artist playlists
