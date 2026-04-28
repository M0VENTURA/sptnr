# 🎧 SPTNR – Navidrome Rating & Management System

> **⚠️ AI-Assisted Project Notice**
> The bulk of this project is AI-assisted. SPTNR has evolved substantially from its origins — it originally integrated with Spotify, but due to Spotify's API changes and restrictions it has completely moved away from Spotify. The project now uses **MusicBrainz**, **Discogs**, and **Last.fm** as its primary data sources. Much of the codebase has been rewritten with AI assistance to support this new direction.

SPTNR (pronounced "Spotner") is a comprehensive music library management system that automates star ratings, provides a rich web interface, and integrates with multiple music metadata services and download clients.

---

## ✨ What Can SPTNR Do?

### 🌟 Intelligent Star Rating System
- Automated 1–5 star ratings for your entire Navidrome library
- Fuses **Last.fm** scrobble data with an age/recency weighting
- Customisable weighting — tune the balance between Last.fm and age
- Single track detection via Discogs, Last.fm, and MusicBrainz metadata
- Scan history tracking to avoid re-scanning unchanged albums

### 🎵 Rich Metadata Enrichment
- **MusicBrainz**: Release dates, album types, ISRC codes, label info
- **Discogs**: Format detection (single vs album), release verification, tracklisting
- **Last.fm**: Scrobble counts, tags, similar artists
- **ListenBrainz**: Community listen counts, open and unbiased data
- Smart genre detection and normalisation
- Mood/audio feature tagging via optional **Essentia** integration

### 🖥️ Full-Featured Web Interface
- Browse and search your artists, albums, and tracks
- Real-time library statistics dashboard
- Manage and monitor scans with live progress
- Edit track metadata inline
- Log viewer with live streaming
- Upcoming release tracker (scraped from Wikipedia)

### 📥 Download Integration
- **qBittorrent**: Search and queue torrent downloads from artist pages
- **Soulseek (slskd)**: P2P music file acquisition
- Downloads watcher — auto-imports and rates new music as it arrives
- Quality filtering (format, bitrate preferences)
- Optional FLAC→MP3 conversion on import

### 📝 Smart Playlist Management
- Create and auto-update playlists by genre, mood, rating, or tag
- Essential artist playlists (top rated tracks per artist)
- Bookmark favourite items for quick access

### 👥 Multi-User Support
- Multiple Navidrome accounts, each with their own credentials
- Per-user Last.fm and ListenBrainz tokens
- Isolated user contexts

### 🔧 Additional Features
- Perpetual background scanning mode
- YAML-based configuration — no database required for settings
- Scan resume after interruption
- Wikipedia upcoming releases scraper
- MusicBrainz folder-level matching and import

---

## 🚀 Quick Start with Docker (Recommended)

### 1. Clone and configure

```bash
git clone https://github.com/M0VENTURA/sptnr.git
cd sptnr
cp docker-compose.yml.example docker-compose.yml
```

Edit `docker-compose.yml` and fill in your values (see [Environment Variables](#environment-variables) below).

### 2. Start the container

```bash
docker compose up -d
```

Access the web interface at **http://localhost:5000**

On first run you will be taken through the **Setup Wizard** where you can enter your Navidrome connection details and API keys.

---

## 🐳 Docker Compose Example

```yaml
version: '3.8'

services:
  sptnr:
    container_name: sptnr
    image: moventura/sptnr:latest
    entrypoint: ["./entrypoint.sh"]

    environment:
      - TZ=Australia/Melbourne
      - NAV_BASE_URL=http://your-navidrome-host:4533
      - NAV_USER=your_navidrome_username
      - NAV_PASS=your_navidrome_password
      - LASTFM_API_KEY=your_lastfm_api_key
      - LASTFM_API_SECRET=your_lastfm_api_secret
      - DISCOGS_TOKEN=your_discogs_personal_token
      - LASTFM_WEIGHT=0.70
      - AGE_WEIGHT=0.30
      - MUSIC_FOLDER=/music
      - DB_PATH=/database/sptnr.db

    ports:
      - "5000:5000"

    volumes:
      - ./logs:/config          # config.yaml and log files stored here
      - ./data:/database        # SQLite database stored here
      - /path/to/your/music:/music
      - /path/to/your/downloads:/downloads

    restart: unless-stopped
```

---

## 🗄️ Database Requirements

SPTNR uses **SQLite** by default. No extra setup is required — the database file is created automatically on first run.

- Default path inside the container: `/database/sptnr.db`
- Map a host folder to `/database` in your volume mounts so the database persists across container restarts:
  ```yaml
  volumes:
    - ./data:/database
  ```
- **PostgreSQL** is also supported for production deployments. See [documentation/INSTALLATION.md](documentation/INSTALLATION.md) for PostgreSQL setup instructions.

---

## 📄 Environment File Requirements

Copy `.env.example` to `.env` for local (non-Docker) runs, or set these as environment variables in your `docker-compose.yml`:

| Variable | Required | Description |
|---|---|---|
| `NAV_BASE_URL` | ✅ | Full URL to your Navidrome instance (e.g. `http://localhost:4533`) |
| `NAV_USER` | ✅ | Navidrome username |
| `NAV_PASS` | ✅ | Navidrome password |
| `LASTFM_API_KEY` | Recommended | Last.fm API key — https://www.last.fm/api/account/create |
| `LASTFM_API_SECRET` | Recommended | Last.fm API secret |
| `DISCOGS_TOKEN` | Recommended | Discogs personal access token — https://www.discogs.com/settings/developers |
| `MUSIC_FOLDER` | Optional | Path to your music library (default: `/music`) |
| `DB_PATH` | Optional | Path to SQLite database (default: `/database/sptnr.db`) |
| `LASTFM_WEIGHT` | Optional | Last.fm weighting (default: `0.70`) |
| `AGE_WEIGHT` | Optional | Age/recency weighting (default: `0.30`) |

> ListenBrainz and MusicBrainz are free public APIs — no key required.

Configuration can also be managed via the **Config** page in the web UI, which writes to `/config/config.yaml`.

---

## 🔑 Required API Keys

| Service | Purpose | Link |
|---|---|---|
| **Last.fm** | Scrobble counts, tags, single detection | https://www.last.fm/api/account/create |
| **Discogs** | Format/release type data, single detection | https://www.discogs.com/settings/developers |
| **ListenBrainz** | Community listen counts (no key needed) | https://listenbrainz.org |
| **MusicBrainz** | Release metadata (no key needed) | https://musicbrainz.org |

Optional integrations:
- **qBittorrent** Web UI
- **slskd** (Soulseek daemon)
- **Essentia** (local ML mood/genre tagging)

---

## 🏠 Local Installation

```bash
git clone https://github.com/M0VENTURA/sptnr.git
cd sptnr
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your settings
python app.py
# Access at http://localhost:5000
```

---

## 📚 Documentation

All detailed documentation lives in the `/documentation` folder.

- **[📖 Documentation Index](documentation/INDEX.md)** — Start here
- **[⚙️ Installation Guide](documentation/INSTALLATION.md)** — Full setup instructions
- **[🖥️ Web UI Guide](documentation/WEB_UI_README.md)** — Web interface reference
- **[👥 Multi-User Configuration](documentation/MULTI_USER_CONFIG_GUIDE.md)** — Multiple Navidrome users
- **[⭐ Rating Algorithm](documentation/STAR_RATING_ALGORITHM.md)** — How ratings are calculated
- **[📥 Downloads Manager](documentation/FEATURES_DOWNLOADS.md)** — qBittorrent/Soulseek setup
- **[📝 Playlists](documentation/FEATURES_PLAYLISTS.md)** — Playlist features

---

## 🎯 Common Tasks

```bash
# Rate a single artist
python start.py --artist "Radiohead" --sync --verbose

# Rate entire library
python start.py --batchrate --sync

# Run in perpetual background mode
python start.py --perpetual --batchrate --sync
```

---

## 🐛 Troubleshooting

1. Check the [Installation Guide](documentation/INSTALLATION.md#troubleshooting)
2. Review logs at `/config/app.log`
3. Open an issue on GitHub with relevant log excerpts

---

## 🤝 Contributing

SPTNR is designed for personal/local use. PRs and ideas are welcome!

---

## 📜 License

See [LICENSE](LICENSE) file for details.

