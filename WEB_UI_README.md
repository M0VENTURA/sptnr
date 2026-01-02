# Sptnr Web UI

## Overview

The Sptnr Web UI provides a comprehensive web-based interface for managing your music library ratings and scans.

## Features

### 📊 Dashboard
- Real-time statistics: artists, albums, tracks, 5-star tracks, singles
- Scan status indicator
- Scan controls (start, stop, force rescan)
- Recent scans list

### 🎵 Music Library Browser
- **Artists Page**: Browse all artists with album and track counts
- **Artist Detail**: View all albums by an artist with statistics
  - **qBittorrent Integration**: Search for missing releases directly from artist pages
- **Album Detail**: View all tracks in an album with ratings and metadata
- **Track Detail**: View and edit individual track metadata

### 📥 Downloads Page
- **Unified search interface** for music downloads
- **qBittorrent search**: Search torrents across all enabled search plugins
- **Soulseek search**: Search P2P network for high-quality music files
- **Real-time results** with file details (size, bitrate, seeds/peers)
- **One-click downloads** directly to your download clients

### ✏️ Track Editing
Edit track metadata including:
- Title, Artist, Album
- Star rating (0-5)
- Single status (Yes/No)
- Single detection confidence (low/medium/high)

View metadata:
- Track ID, MBID, Spotify ID
- Last.fm listeners, ListenBrainz play count
- Last scanned timestamp

### 🔍 Scan Management
- Start batch rating scan
- Force rescan all tracks
- Stop running scans
- Real-time scan status

### 📝 Log Viewer
- Real-time log streaming using Server-Sent Events
- View last 100 log lines on load
- Start/Stop live streaming
- Clear display
- Auto-scroll to bottom

### ⚙️ Configuration Editor
- Edit config.yaml directly in the browser
- YAML syntax validation
- Save changes with feedback

## qBittorrent Integration

Search for missing artist releases directly from artist pages with an **embedded inline search window**.

### Setup

1. Enable qBittorrent in your `config.yaml`:

```yaml
qbittorrent:
  enabled: true
  web_url: "http://localhost:8080"  # Your qBittorrent Web UI URL
  username: "admin"  # Optional: qBittorrent username (required for API access)
  password: "adminpass"  # Optional: qBittorrent password (required for API access)
```

2. Adjust the `web_url` to match your qBittorrent Web UI address
   - Local: `http://localhost:8080`
   - Remote: `http://your-server-ip:8080`
   - Reverse proxy: `https://qbit.yourdomain.com`

3. Configure authentication if your qBittorrent requires login
   - Username and password are needed for API access
   - Leave blank if running without authentication (not recommended)

4. Save the configuration and refresh the artist page

### Usage

Once enabled, a **qBittorrent** button appears on every artist page:

1. Click the button to open an **inline search modal**
2. The search auto-populates with "{Artist} discography"
3. Edit the search query and press Enter or click Search
4. Browse results showing:
   - Torrent name and source
   - File size
   - Seeds (green/yellow/red indicator)
   - Peers
5. Click **Add** to send the torrent directly to qBittorrent
6. Torrent begins downloading immediately

### Features

- **Inline search**: No need to leave the page
- **Real-time results**: Direct integration with qBittorrent's search API
- **One-click download**: Add torrents without leaving Sptnr
- **Seeders indicator**: Color-coded to show torrent health
- **Size formatting**: Human-readable file sizes
- **Multiple search engines**: Uses all enabled search plugins in qBittorrent

### Requirements

- qBittorrent with Web UI enabled
- qBittorrent search plugins installed and enabled
- Network access from Sptnr to qBittorrent Web UI
- Authentication credentials (if required by your qBittorrent setup)

### Troubleshooting

**No results found:**
- Ensure qBittorrent search plugins are installed
- Check that search plugins are enabled in qBittorrent settings
- Try a different search query

**Authentication errors:**
- Verify username and password in config.yaml
- Check qBittorrent Web UI settings for authentication requirements
- Ensure Web UI is not using HTTPS with self-signed certificates

**Connection errors:**
- Verify `web_url` is correct and accessible
- Check firewall rules allowing access to qBittorrent port
- Test Web UI access in browser first

## Soulseek (slskd) Integration

Search and download music from the Soulseek P2P network using the **slskd** daemon.

### Setup

1. Install and configure slskd (https://github.com/slskd/slskd)
   - Run slskd daemon: `slskd --http-port 5030`
   - Access Web UI and configure your Soulseek credentials
   - Generate an API key in Settings > API

2. Enable slskd in your `config.yaml`:

```yaml
slskd:
  enabled: true
  web_url: "http://localhost:5030"  # Your slskd Web UI URL
  api_key: "your-api-key-here"      # From slskd Settings > API
```

3. Save configuration and visit the Downloads page

### Usage

The **Downloads** page provides unified search:

1. Navigate to **Downloads** in the navigation bar
2. Use **Soulseek Search** panel on the right
3. Enter artist/album name and click Search
4. Browse results showing:
   - File name and owner
   - File size
   - Bitrate (audio quality)
   - Track length
5. Click **Download** to add to slskd download queue
6. Files download to your configured slskd directory

### Features

- **P2P network access**: Direct connection to Soulseek users
- **High quality files**: Often lossless (FLAC) or high-bitrate MP3
- **Metadata display**: Bitrate, length, file size
- **Queue management**: Downloads appear in slskd interface
- **Side-by-side search**: Compare qBittorrent and Soulseek results

### Requirements

- slskd daemon running and accessible
- Valid Soulseek account credentials configured in slskd
- API key from slskd settings
- Network access from Sptnr to slskd Web UI

### Troubleshooting

**No results:**
- Ensure slskd is running and connected to Soulseek network
- Check slskd logs for connection issues
- Try broader search terms
- Verify API key is correct

**Authentication errors:**
- Regenerate API key in slskd Settings > API
- Update config.yaml with new key
- Restart Sptnr

**Download fails:**
- Check slskd download directory permissions
- Verify user is online and sharing the file
- Check slskd transfer limits and queue settings

## Running the Web UI

### Standalone Mode

```bash
python app.py
```

The web UI will be available at http://localhost:5000

### Docker Mode

Update your docker-compose.yml to expose port 5000:

```yaml
services:
  sptnr:
    build: .
    container_name: sptnr
    ports:
      - "5000:5000"
    volumes:
      - ./config:/config
      - ./database:/database
    command: python /app/app.py
```

Or run both the CLI and web UI with a modified entrypoint.

### Environment Variables

- `CONFIG_PATH`: Path to config.yaml (default: `/config/config.yaml`)
- `DB_PATH`: Path to SQLite database (default: `/database/sptnr.db`)
- `LOG_PATH`: Path to log file (default: `/config/app.log`)
- `SECRET_KEY`: Flask secret key for sessions (change in production!)

## Navigation

```
Dashboard (/)
├── Artists (/artists)
│   └── Artist Detail (/artist/<name>)
│       └── Album Detail (/album/<artist>/<album>)
│           └── Track Detail (/track/<id>)
├── Logs (/logs)
│   └── Log Stream (/logs/stream - SSE)
└── Config (/config)
```

## API Endpoints

- `GET /api/stats` - JSON statistics (artists, albums, tracks)
- `GET /scan/status` - JSON scan status
- `POST /scan/start` - Start a scan (form: scan_type, artist)
- `POST /scan/stop` - Stop running scan
- `POST /track/<id>/edit` - Update track metadata
- `POST /config/edit` - Save configuration

## Technology Stack

- **Backend**: Flask 3.0.0
- **Frontend**: Bootstrap 5.3.2, Bootstrap Icons
- **Database**: SQLite3
- **Real-time**: Server-Sent Events (SSE)
- **Process Management**: subprocess, threading

## Security Notes

⚠️ **Important**: Change the `SECRET_KEY` environment variable in production!

The default secret key is for development only:
```bash
export SECRET_KEY="your-secure-random-key-here"
```

## Troubleshooting

### Database Not Found
- Check that `/database/sptnr.db` exists
- Run a batch rating scan to create the database

### Config Not Found
- Check that `/config/config.yaml` exists
- The app will copy from `/app/config/config.yaml` template on first run

### Scan Won't Start
- Check logs for errors
- Verify start.py is executable
- Check Python path in app.py (default: `python /app/start.py`)

### Logs Not Streaming
- Verify log file exists at configured path
- Check browser console for SSE connection errors
- Modern browsers required (Chrome, Firefox, Edge, Safari)

## Development

The web UI is designed to work alongside the existing CLI tool. You can:
- Run scans from the CLI while viewing results in the web UI
- Edit tracks in the web UI and see changes reflected in CLI operations
- Monitor CLI scans in real-time through the log viewer

## Future Enhancements

Potential features:
- User authentication
- Artist-specific scan triggers
- Bulk track editing
- Export/import functionality
- Advanced filtering and search
- Statistics graphs and charts
- Playlist management
