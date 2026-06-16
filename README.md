# 🎧 Popularr – Intelligent Navidrome Rating & Music Management

Popularr is a powerful music library management system designed to enhance your Navidrome collection with automated ratings, enriched metadata, and a modern web interface.

It uses data from Last.fm, ListenBrainz, MusicBrainz, and Discogs to intelligently score, organise, and manage your library at scale.

---

## ✨ Key Features

### 🌟 Intelligent Rating Engine
- Fully automated 1–5 star ratings
- Combines:
  - Last.fm scrobble data
  - ListenBrainz community listens
  - Track age and recency weighting
- Configurable weighting system
- Multi-source single detection
- Scan tracking to avoid unnecessary reprocessing

---

### 🎵 Metadata Enrichment

Popularr enriches your music library using multiple data sources:

- MusicBrainz
  - Release dates, album types, ISRC codes, labels
- Discogs
  - Format detection (single vs album)
  - Release validation and tracklists
- Last.fm
  - Scrobble counts, tags, similar artists
- ListenBrainz
  - Open, community-based listen data

Additional capabilities:
- Genre normalisation and cleanup
- Cross-source validation
- Optional Essentia integration for mood/audio features

---

### 🖥️ Web Interface

- Browse artists, albums, and tracks
- Real-time statistics dashboard
- Live scan monitoring
- Inline metadata editing
- Live log viewer
- Upcoming releases tracking

---

### 📥 Download Integration

- qBittorrent integration
- Soulseek (slskd) support
- Automatic download ingestion
- Optional format filtering and conversion

---

### 📝 Playlist Automation

- Dynamic playlists (rating, genre, mood, tags)
- Essential artist playlists
- Bookmarking system

---

### 👥 Multi-User Support

- Multiple Navidrome accounts
- Per-user Last.fm and ListenBrainz
- Isolated processing contexts

---

### 🔧 Additional Capabilities

- Continuous background scanning
- Resume scans after interruption
- MusicBrainz folder matching
- Wikipedia release tracking

---

## 🗄️ Database (PostgreSQL)

Popularr uses PostgreSQL as its primary database.

It handles:
- Metadata caching
- Rating calculations
- Scan tracking
- Queue processing

---

## 🚀 Quick Start (Docker)

### 1. Clone the repository

```bash
git clone https://github.com/M0VENTURA/Popularr.git
cd Popularr
```

### 2. Start the stack

```bash
docker compose up -d
```

Access the UI at:
http://localhost:5000

---

## 🐳 Docker Compose Example

```yaml
version: '3.8'

services:

  flask:
    container_name: webui
    image: moventura/sptnr:develop
    entrypoint: ["./entrypoint.sh"]

    depends_on:
      postgres-sptnr:
        condition: service_healthy

    volumes:
      - ./config:/config
      - ./database:/database
      - smbmusic:/music
      - downloads:/downloads

    environment:
      - PG_HOST=sptnr-postgres
      - PG_PORT=5432
      - PG_USER=sptnr
      - PG_PASSWORD=sptnr
      - PG_DATABASE=sptnr
      - PYTHONUNBUFFERED=1
      - FLASK_ENV=production
      - FLASK_DEBUG=0
      - MUSIC_ROOT=/music
      - DOWNLOADS_DIR=/downloads

    ports:
      - "5000:5000"

    networks:
      docker_VLAN:
        ipv4_address: 192.168.1.204

    dns:
      - 192.168.1.250

  postgres-sptnr:
    image: postgres:16
    container_name: sptnr-postgres
    restart: unless-stopped

    environment:
      - POSTGRES_DB=sptnr
      - POSTGRES_USER=sptnr
      - POSTGRES_PASSWORD=sptnr

    volumes:
      - pgdata:/var/lib/postgresql/data

    networks:
      docker_VLAN:
        ipv4_address: 192.168.1.225

    dns:
      - 192.168.1.250

    healthcheck:
      test: ["CMD-SHELL", "pg_isready -h localhost -p 5432 -U sptnr"]
      interval: 5s
      timeout: 3s
      retries: 10

volumes:
  pgdata:
  smbmusic:
  downloads:

networks:
  docker_VLAN:
    external: true
```

---

## 🐛 Troubleshooting

- Check PostgreSQL logs:
  ```bash
  docker logs sptnr-postgres
  ```

- Check application logs:
  ```bash
  docker logs webui
  ```

---

## 📜 License

See the LICENSE file.
