# 🎧 SPTNR – Navidrome Rating & Management System

> **Note:** This tool was created with the help of AI assistance. While the code works well, it's still evolving. The goal is to provide intelligent star ratings and comprehensive music library management for your Navidrome library.

SPTNR (pronounced "Spotner") is a comprehensive music library management system that automates star ratings, provides a rich web interface, and integrates with multiple music services and download clients.

---

## 🚀 Quick Start

### Docker Installation (Recommended)

```bash
git clone https://github.com/M0VENTURA/sptnr.git
cd sptnr
cp docker-compose.yml.example docker-compose.yml
# Edit docker-compose.yml with your settings
docker compose up -d
```

Access the web interface at **http://localhost:5000**

### Local Installation

```bash
git clone https://github.com/M0VENTURA/sptnr.git
cd sptnr
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

---

## 📚 Complete Documentation

**All comprehensive documentation has been moved to the `/documentation` folder.**

### Quick Links

- **[📖 Documentation Index](documentation/INDEX.md)** - Start here for all documentation
- **[⚙️ Installation Guide](documentation/INSTALLATION.md)** - Detailed setup instructions
- **[🖥️ Web UI Guide](documentation/WEB_UI_README.md)** - Complete web interface documentation
- **[👥 Multi-User Configuration](documentation/MULTI_USER_CONFIG_GUIDE.md)** - Setting up multiple users
- **[⭐ Rating Algorithm](documentation/STAR_RATING_ALGORITHM.md)** - How ratings are calculated

### Feature Documentation

- **[📊 Dashboard Features](documentation/FEATURES_DASHBOARD.md)** - Dashboard overview and statistics
- **[🎵 Library Features](documentation/FEATURES_LIBRARY.md)** - Artists, albums, and tracks
- **[📥 Downloads Manager](documentation/FEATURES_DOWNLOADS.md)** - qBittorrent and Soulseek integration
- **[📝 Playlist Management](documentation/FEATURES_PLAYLISTS.md)** - Smart playlists and imports

### Help in Web Interface

When using the web interface, click the **Help** link in the navigation bar or the help buttons (?) on each page to access context-specific documentation.

---

## ✨ Key Features

### 🎯 Smart Rating System
- Automated star ratings using Spotify, Last.fm, and ListenBrainz data
- Intelligent single detection via metadata
- Customizable rating weights and algorithms
- Age-based momentum scoring

### 🎵 Comprehensive Spotify Metadata (NEW!)
- **Audio Features**: Tempo, energy, danceability, valence, acousticness, and more
- **Smart Tags**: Automatic detection of Christmas, Cover, Live, Acoustic, Orchestral, and Instrumental tracks
- **Genre Normalization**: Artist genres mapped to broad categories
- **Smart Playlists**: Filter tracks by energy, mood, tempo, and special tags
- **[📖 Full Documentation](documentation/SPOTIFY_METADATA_FEATURES.md)**

### 🖥️ Rich Web Interface
- Browse artists, albums, and tracks
- Real-time library statistics
- Scan management and monitoring
- Track metadata editing
- Log viewer with live streaming

### 📥 Download Integration
- **qBittorrent**: Search and download torrents
- **Soulseek (slskd)**: P2P music downloads
- Integrated search from artist pages
- One-click download management

### 📝 Advanced Playlist Features
- Smart playlists with auto-update
- Import Spotify playlists
- Essential artist playlists
- Bookmark favorite items

### 👥 Multi-User Support
- Multiple Navidrome accounts
- Per-user Spotify credentials
- Per-user ListenBrainz tokens
- Isolated user contexts

### 🔧 Additional Features
- Beets music tagger integration
- MusicBrainz metadata enrichment
- Real-time log monitoring
- YAML-based configuration
- Scan history tracking

---

## 🔑 Required API Keys

You'll need credentials for:
- **Navidrome** (your music server)
- **Spotify API** - https://developer.spotify.com/dashboard/
- **Last.fm API** - https://www.last.fm/api/account/create

Optional:
- **ListenBrainz** - https://listenbrainz.org/settings/profile/
- **qBittorrent** with Web UI enabled
- **slskd** (Soulseek daemon)

See the [Installation Guide](documentation/INSTALLATION.md) for detailed setup instructions.

---

## 📖 Documentation Structure

```
documentation/
├── INDEX.md                      # Documentation index
├── INSTALLATION.md               # Setup guide
├── README.md                     # Original detailed README
├── WEB_UI_README.md             # Web interface guide
├── FEATURES_*.md                 # Feature-specific docs
├── MULTI_USER_CONFIG_GUIDE.md   # Multi-user setup
├── STAR_RATING_ALGORITHM.md     # Rating system
└── [Many more technical docs]    # See INDEX.md for complete list
```

---

## 🎯 Common Tasks

### Rate a Single Artist
```bash
python start.py --artist "Radiohead" --sync --verbose
```

### Rate Entire Library
```bash
python start.py --batchrate --sync
```

### Run Web Interface
```bash
python app.py
# Access at http://localhost:5000
```

### Automated Scans
```bash
python start.py --perpetual --batchrate --sync
```

---

## 🐛 Troubleshooting

For troubleshooting and support:
1. Check the [Installation Guide](documentation/INSTALLATION.md#troubleshooting)
2. Review the [Quick Fix Reference](documentation/QUICK_FIX_REFERENCE.md)
3. Check logs at `/config/sptnr.log`
4. Open an issue on GitHub with log excerpts

---

## 🤝 Contributing

SPTNR is designed for personal/local use. PRs and ideas welcome!

---

## 📜 License

See [LICENSE](LICENSE) file for details.

---

## 🌟 What Makes SPTNR Special?

- **Intelligent Rating**: Fuses multiple data sources for culturally aware ratings
- **Complete Solution**: CLI + Web UI + API in one package
- **Modern Stack**: Flask, Bootstrap 5, SQLite/PostgreSQL
- **Extensible**: Modular design for easy customization
- **Well Documented**: Comprehensive docs in `/documentation` folder
- **Active Development**: Regular updates and improvements

---

**For complete documentation, see the [Documentation Index](documentation/INDEX.md) or click Help in the web interface.**
