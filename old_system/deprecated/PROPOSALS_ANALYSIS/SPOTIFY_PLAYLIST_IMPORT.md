# Spotify Playlist Import Guide

## Overview

The Spotify Playlist Import feature allows you to import playlists from Spotify into Navidrome. The feature is already implemented and available at `/playlist/import`.

## How It Works

### Without User Authentication (Default)
By default, the playlist importer uses **Client Credentials Flow** which allows you to:
- Import any public Spotify playlist by URL or ID
- Browse featured playlists from Spotify
- Match tracks from the playlist to your local library
- Create a Navidrome playlist with matched tracks

### With User Authentication (Recommended)
To access your personal Spotify playlists, you need to set up user authentication:

1. **Get a Spotify User Token:**
   - Go to [Spotify Web API Console](https://developer.spotify.com/console/get-current-user-playlists/)
   - Click "Get Token" and authorize with your Spotify account
   - Copy the generated OAuth token

2. **Set Environment Variable:**
   ```bash
   SPOTIFY_USER_TOKEN=your_token_here
   ```

3. **Update Config (config.yaml):**
   ```yaml
   api_integrations:
     spotify:
       enabled: true
       client_id: your_client_id
       client_secret: your_client_secret
   ```

## Using the Playlist Importer

### Access the Importer
Navigate to: `http://your-sptnr-instance/playlist/import`

### Import a Playlist

1. **From Your Spotify Playlists:**
   - If `SPOTIFY_USER_TOKEN` is set, your playlists will load automatically
   - Click "Import" on any playlist card

2. **From a Spotify URL:**
   - Paste any Spotify playlist URL in the form
   - Supported formats:
     - `https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M`
     - `spotify:playlist:37i9dQZF1DXcBWIGoYBM5M`
     - `37i9dQZF1DXcBWIGoYBM5M` (just the ID)
   - Enter a name for the playlist
   - Click "Import Playlist"

### Review Import Results

The importer will show:
- **Matched Tracks**: Tracks found in your local library
- **Missing Tracks**: Tracks not in your library (with search/download options)
- **Coverage**: Percentage of playlist matched

### Create Playlist

Once you've reviewed the matches:
1. Click "Create Playlist" to add matched tracks to Navidrome
2. Optionally, search for missing tracks using:
   - **Soulseek (slskd)** - if configured
   - **Manual selection** - from your library

## Features

### Track Matching
The importer uses fuzzy matching to find tracks in your library:
- Matches by artist, title, and album
- Normalizes text (removes accents, version tags, etc.)
- Handles various naming conventions
- Shows confidence scores for matches

### Missing Track Handling
For tracks not in your library, you can:
- Search in Soulseek (if slskd is enabled)
- Replace with a different track from your library
- Add tracks manually from your database

### Playlist Management
After import:
- Playlists are created in Navidrome via API
- Track order is preserved from Spotify
- Playlist description is copied from Spotify

## Troubleshooting

### "Spotify not enabled or configured"
- Ensure Spotify credentials are set in `config.yaml`
- Verify `client_id` and `client_secret` are correct

### "No playlists found"
- Check that `SPOTIFY_USER_TOKEN` is set and valid
- Tokens expire after 1 hour - refresh if needed
- Without user token, only featured playlists are shown

### Import fails or times out
- Check Spotify API rate limits
- Verify network connectivity to Spotify
- Try importing smaller playlists first

## API Endpoints

The playlist importer uses these endpoints:

- `GET /api/spotify/playlists` - List user's playlists
- `POST /api/playlist/import` - Import and match tracks
- `POST /api/playlist/create` - Create Navidrome playlist
- `POST /api/playlist/search-songs` - Search local library

## Notes

- Spotify user tokens expire after 1 hour and need to be refreshed
- For production use, consider implementing OAuth 2.0 refresh token flow
- The importer only creates playlists - it doesn't download missing tracks automatically
- Use slskd or qBittorrent integration for downloading missing content
