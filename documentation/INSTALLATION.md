# Installation & Setup Guide

This guide covers all installation methods for Popularr.

## Table of Contents
- [Prerequisites](#prerequisites)
- [Docker Installation](#docker-installation)
- [Local Installation](#local-installation)
- [Configuration](#configuration)
- [First Run](#first-run)
- [Verification](#verification)

## Prerequisites

Before installing Popularr, ensure you have:

### Required Services
- **Navidrome** music server (running and accessible)
  - URL to your Navidrome instance
  - Valid user account credentials

### API Keys
You'll need API credentials for:
- **Last.fm API** (for listening statistics)
  - Get at: https://www.last.fm/api/account/create
- **ListenBrainz** (optional, for love/hate tracking)
  - Get token at: https://listenbrainz.org/settings/profile/
- **Discogs** (optional, for format data and release verification)
  - Get token at: https://www.discogs.com/settings/developers

### Optional Integrations
- **qBittorrent** with Web UI (for torrent downloads)
- **slskd** (for Soulseek P2P downloads)
- **Beets** music tagger (for advanced metadata management)

## Docker Installation

### Method 1: Docker Compose (Recommended)

1. **Clone the repository:**
```bash
git clone https://github.com/M0VENTURA/Popularr.git
cd Popularr
```

2. **Copy the example docker-compose file:**
```bash
cp docker-compose.yml.example docker-compose.yml
```

3. **Edit docker-compose.yml:**
```yaml
version: "3.9"
services:
  popularr:
    build: .
    container_name: popularr
    image: moventura/popularr:latest
    ports:
      - "5000:5000"  # Web UI port
    volumes:
      - ./config:/config
      - /path/to/music:/music:ro  # Optional: for MP3 scanning
    environment:
      - SECRET_KEY=change-this-to-something-random
      - CONFIG_PATH=/config/config.yaml
      - PG_HOST=your-postgres-host
      - PG_PORT=5432
      - PG_USER=your_postgres_username
      - PG_PASSWORD=your_postgres_password
      - PG_DATABASE=popularr
    command: python /app/app.py
    restart: unless-stopped
```

4. **Create config directory:**
```bash
mkdir -p config
```

5. **Copy example config:**
```bash
cp .env.example config/config.yaml
```

6. **Edit config/config.yaml** (see [Configuration](#configuration) section)

7. **Start the container:**
```bash
docker compose up -d
```

8. **Access the Web UI:**
   - Open http://localhost:5000 in your browser

### Method 2: Docker Run

```bash
docker build -t popularr .
docker run -d \
  --name popularr \
  -p 5000:5000 \
  -v $(pwd)/config:/config \
  -v /path/to/music:/music:ro \
  -e SECRET_KEY=your-secret-key \
  -e PG_HOST=your-postgres-host \
  -e PG_USER=your_postgres_username \
  -e PG_PASSWORD=your_postgres_password \
  -e PG_DATABASE=popularr \
  popularr
```

### Updating Docker Installation

```bash
cd Popularr
git pull
docker compose down
docker compose build
docker compose up -d
```

## Local Installation

### Requirements
- Python 3.8 or higher
- pip (Python package manager)

### Steps

1. **Clone the repository:**
```bash
git clone https://github.com/M0VENTURA/Popularr.git
cd Popularr
```

2. **Create virtual environment:**
```bash
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

3. **Install dependencies:**
```bash
pip install -r requirements.txt
```

4. **Create config directory:**
```bash
mkdir -p config
```

5. **Create configuration file:**
```bash
cp .env.example config/config.yaml
```

6. **Edit config/config.yaml** (see [Configuration](#configuration) section)

7. **Run the web server:**
```bash
python app.py
```

8. **Access the Web UI:**
   - Open http://localhost:5000 in your browser

### Running as a Service (Linux)

Create `/etc/systemd/system/popularr.service`:

```ini
[Unit]
Description=Popularr Music Rating Service
After=network.target

[Service]
Type=simple
User=your-user
WorkingDirectory=/path/to/popularr
Environment="PATH=/path/to/popularr/.venv/bin"
ExecStart=/path/to/popularr/.venv/bin/python /path/to/popularr/app.py
Restart=always

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl enable popularr
sudo systemctl start popularr
sudo systemctl status popularr
```

## Configuration

### Basic Configuration

Edit `config/config.yaml`:

```yaml
# Multi-user configuration
navidrome_users:
  - username: admin
    display_name: "My Account"
    navidrome_base_url: "http://localhost:4533"
    navidrome_password: "your_password"
    spotify_client_id: "your_spotify_client_id"
    spotify_client_secret: "your_spotify_client_secret"
    listenbrainz_user_token: "your_listenbrainz_token"

# Last.fm API (shared across users)
lastfm:
  api_key: "your_lastfm_api_key"

# Features configuration
features:
  dry_run: false
  sync: true
  force: false
  verbose: false
  perpetual: false
  batchrate: false

# Rating weights
weights:
  spotify: 0.3
  lastfm: 0.5
  age: 0.2

# Optional: qBittorrent integration
qbittorrent:
  enabled: false
  web_url: "http://localhost:8080"
  username: "admin"
  password: "adminpass"

# Optional: Soulseek integration
slskd:
  enabled: false
  web_url: "http://localhost:5030"
  api_key: "your_slskd_api_key"

# Optional: Beets integration
beets:
  enabled: false
  config_path: "/config/beets.yaml"
```

### Environment Variables

You can also use environment variables (they override config.yaml):

```bash
# Core settings
export CONFIG_PATH=/path/to/config.yaml
export LOG_PATH=/path/to/app.log
export SECRET_KEY=your-secret-key-here

# PostgreSQL connection
export PG_HOST=your-postgres-host
export PG_PORT=5432
export PG_USER=your_postgres_username
export PG_PASSWORD=your_postgres_password
export PG_DATABASE=popularr

# Music folder for MP3 scanning
export MUSIC_FOLDER=/path/to/music
```

### Getting API Keys

#### Spotify
1. Visit https://developer.spotify.com/dashboard/
2. Log in with your Spotify account
3. Click "Create App"
4. Fill in app name and description
5. Accept terms and create
6. Copy **Client ID** and **Client Secret**

#### Last.fm
1. Visit https://www.last.fm/api/account/create
2. Fill in application details
3. Submit application
4. Copy **API Key**

#### ListenBrainz
1. Visit https://listenbrainz.org/settings/profile/
2. Scroll to "API Tokens" section
3. Copy your **User Token** (not API token)

## First Run

### Using the Setup Wizard (Web UI)

1. Start Popularr (Docker or local)
2. Navigate to http://localhost:5000
3. You'll be redirected to the setup page
4. Fill in your credentials:
   - Navidrome URL and credentials
   - Last.fm API key
   - Discogs token (optional)
   - (Optional) ListenBrainz token
5. Click "Save Configuration"
6. You'll be redirected to the dashboard

### Using CLI

Run an initial scan of a single artist:
```bash
python start.py --artist "Radiohead" --sync --verbose
```

Or scan your entire library:
```bash
python start.py --batchrate --sync
```

## Verification

### Check Web UI
1. Access http://localhost:5000
2. You should see the dashboard with statistics
3. Navigate to Artists page - you should see your library

### Check Database
```bash
psql -h $PG_HOST -U $PG_USER -d $PG_DATABASE -c "SELECT COUNT(*) FROM artists;"
psql -h $PG_HOST -U $PG_USER -d $PG_DATABASE -c "SELECT COUNT(*) FROM albums;"
psql -h $PG_HOST -U $PG_USER -d $PG_DATABASE -c "SELECT COUNT(*) FROM tracks;"
```

### Check Logs
```bash
# Docker
docker logs popularr

# Local
tail -f config/app.log
```

### Test API Connection
```bash
curl http://localhost:5000/api/stats
```

Should return JSON with statistics:
```json
{
  "artists": 123,
  "albums": 456,
  "tracks": 7890,
  "five_stars": 234,
  "singles": 567
}
```

## Troubleshooting

### Port Already in Use
If port 5000 is already taken:
- **Docker**: Change port mapping in docker-compose.yml: `"5001:5000"`
- **Local**: Set `PORT` environment variable: `export PORT=5001`

### Database Connection Errors
- Verify `PG_HOST`, `PG_USER`, and `PG_DATABASE` are set correctly
- Ensure the PostgreSQL server is running and accessible
- Check that the database exists and the user has appropriate permissions

### Config Not Found
Ensure config.yaml exists:
```bash
ls -la config/config.yaml
```

If missing, copy from example:
```bash
cp .env.example config/config.yaml
```

### Navidrome Connection Failed
- Verify Navidrome URL is accessible
- Check username and password are correct
- Ensure Navidrome API is enabled
- Test with: `curl http://your-navidrome-url/api/ping`

### No Artists Showing
Run an initial scan:
```bash
# Via web UI: Dashboard > Start Scan
# Via CLI:
python start.py --batchrate --sync
```

## Next Steps

After installation:
1. Read the [Web UI Guide](WEB_UI_README.md) for interface features
2. Configure [Multi-User Setup](MULTI_USER_CONFIG_GUIDE.md) if needed
3. Set up [Download Integrations](FEATURES_DOWNLOADS.md) (optional)
4. Review [Quick Reference](QUICK_REFERENCE.md) for common tasks

## Support

For issues:
1. Check [Quick Fix Reference](QUICK_FIX_REFERENCE.md)
2. Review logs in `config/app.log`
3. Open an issue on GitHub with log excerpts
