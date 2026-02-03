# Auto-Import and UI Improvements

This document describes the new features implemented to enhance the sptnr music management system.

## Features

### 1. Auto-Import New Songs from Navidrome

The music watcher service now automatically imports new songs detected in Navidrome and triggers popularity scans.

**How it works:**
- Monitors the `/music` directory for changes
- When changes are detected, triggers a Navidrome library scan
- After the Navidrome scan completes, automatically imports new songs into the local database
- Optionally runs a popularity scan on newly imported songs
- All settings are configurable via the settings page

**Configuration:**
```yaml
watcher:
  scan_interval: 30  # How often to check for new files (seconds)
  navidrome_sync_wait: 600  # Wait time for Navidrome scan (seconds)
  auto_import_enabled: true  # Enable auto-import feature
  auto_popularity_scan: true  # Run popularity scan on new songs
  downloads_watcher_enabled: true  # Monitor downloads folder
```

**Settings Page:**
Navigate to the Settings page to adjust watcher service parameters:
- Scan interval (10-3600 seconds)
- Navidrome sync wait time (60-3600 seconds)
- Enable/disable auto-import
- Enable/disable automatic popularity scanning
- Enable/disable downloads folder monitoring

### 2. Artist Page Categorization

Artist pages now properly separate releases into Albums, EPs, and Singles with inline display of missing releases.

**Features:**
- Three distinct sections: Albums, EPs, and Singles
- Each section shows both discovered (in library) and missing (detected on MusicBrainz) releases
- Visual badges indicate the count of available and missing releases in each category
- Missing releases can be imported directly with one click
- Categories are determined by:
  - Spotify album type metadata
  - Track count heuristics (Albums: >6 tracks, EPs: 3-6 tracks, Singles: <3 tracks)

**Benefits:**
- Easier to browse artist discography
- Quick identification of missing releases
- Better organization of large artist catalogs

### 3. Genre Recommendation System

Track pages now include genre recommendations from multiple sources.

**Features:**
- "Get Recommendations" button on track editing page
- Aggregates genres from:
  - Spotify
  - Last.fm
  - Discogs
  - MusicBrainz
  - Navidrome
  - Artist-level genre data
- Click-to-add genre suggestions
- Autocomplete datalist with common genres
- Displays top 20 most relevant genres

**Usage:**
1. Go to any track page
2. Click "Get Recommendations" in the Genres section
3. Click any suggested genre to add it to the track
4. Save the track to persist changes

### 4. Enhanced Metadata Editing UI

Improved track and album pages for easier metadata management.

**Improvements:**
- Better genre editing with add/remove functionality
- Genre autocomplete with common options
- Cleaner layout with organized sections
- All MP3 metadata fields accessible
- Visual genre badges for easy identification

## API Endpoints

### Genre Recommendations
```
GET /api/track/genre-recommendations?track_id=<track_id>
```
Returns genre suggestions for a specific track.

**Response:**
```json
{
  "track_id": "abc123",
  "artist": "Artist Name",
  "title": "Track Title",
  "recommendations": ["Rock", "Alternative", "Indie"]
}
```

### Artist Missing Releases (Cached)
```
GET /api/artist/cached-missing-releases?artist=<artist_name>
```
Returns cached missing releases from database.

### Import Release
```
POST /api/artist/import-release
Content-Type: application/json

{
  "artist": "Artist Name",
  "release_id": "musicbrainz-id",
  "title": "Album Title"
}
```
Imports a missing release from MusicBrainz into the database.

## Migration Notes

### Configuration
If you have an existing `config.yaml`, add the watcher section:

```yaml
watcher:
  scan_interval: 30
  navidrome_sync_wait: 600
  auto_import_enabled: true
  auto_popularity_scan: true
  downloads_watcher_enabled: true
```

### Database
No database migrations are required. The `missing_releases` table should already exist.

### Running the Music Watcher
The music watcher service should be started automatically if configured. It can also be run manually:

```bash
python3 music_watcher.py
```

## Troubleshooting

### Auto-import not working
- Check that `auto_import_enabled` is `true` in config.yaml
- Verify Navidrome connection settings
- Check music watcher logs at `/config/music_watcher.log`

### Genre recommendations empty
- Ensure track has been scanned with popularity scan
- Check that API integrations are configured (Spotify, Last.fm, etc.)
- Verify artist has genre metadata in the database

### Missing releases not showing
- Click "Check for Missing" button on artist page
- Missing releases are cached - re-check to refresh
- Ensure artist name matches MusicBrainz exactly

## Future Enhancements

Potential improvements for future releases:
- Bulk genre editing across multiple tracks
- Custom genre taxonomies
- Album-level metadata editing improvements
- Automated missing release detection on schedule
- Smart genre suggestions based on acoustic features
