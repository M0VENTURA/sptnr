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

Search for missing artist releases directly from artist pages.

### Setup

1. Enable qBittorrent in your `config.yaml`:

```yaml
qbittorrent:
  enabled: true
  web_url: "http://localhost:8080"  # Your qBittorrent Web UI URL
  username: ""  # Optional: for future API features
  password: ""  # Optional: for future API features
```

2. Adjust the `web_url` to match your qBittorrent Web UI address
   - Local: `http://localhost:8080`
   - Remote: `http://your-server-ip:8080`
   - Reverse proxy: `https://qbit.yourdomain.com`

3. Save the configuration and refresh the artist page

### Usage

Once enabled, a **qBittorrent** button appears on every artist page:
- Click to open qBittorrent's search page with the artist name pre-filled
- Search for discographies, missing albums, or specific releases
- Add torrents directly from qBittorrent's web interface

### Notes

- Opens in a new tab/window
- Works with any qBittorrent Web UI version that supports the `/#/search/` URL pattern
- No authentication required (uses your existing qBittorrent session)

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
