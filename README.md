# 🎵 Popularr

*Music intelligence for Navidrome — scores, rates, curates and completes your library.*

**Self-hosted** · **Python / Quart** · **PostgreSQL** · **Docker**

[![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![Quart](https://img.shields.io/badge/Framework-Quart-6CA315)](https://quart.palletsprojects.com)
[![PostgreSQL](https://img.shields.io/badge/Database-PostgreSQL_16-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org)
[![Docker](https://img.shields.io/badge/Docker-compose-2496ED?logo=docker&logoColor=white)](https://www.docker.com)
[![Navidrome](https://img.shields.io/badge/Works_with-Navidrome-1DB954)](https://www.navidrome.org)

</div>

Popularr is a self-hosted music management companion for **[Navidrome](https://www.navidrome.org)**.
It looks at your library the way a data analyst would: it **scores every track for popularity**,
turns those scores into **1–5★ ratings**, **detects singles and cover songs**, **builds playlists**
from what you actually love, **downloads what's missing** over Soulseek, and pushes the ratings
back into Navidrome so every app that reads your library — Plex, Jellyfin, DSub, Feishin — shows
the same stars.

It is *not* a media server, a tag editor, or a replacement for Navidrome. It is the intelligence
layer that sits beside it.

## ✨ Why Popularr?

| Problem | Popularr's answer |
| --- | --- |
| "I have 40,000 tracks and no idea what deserves 5★" | Objective, explainable popularity scores blended from Last.fm, ListenBrainz and release age — re-anchored per album and per artist |
| "My ratings are inconsistent" | One deterministic pipeline rates every album the same way, and syncs the result to every Navidrome user |
| "I never hear my own deep cuts" | Auto-generated **Essential**, **Genre Top-Tracks** and **New Music** playlists from your 4★/5★ tracks |
| "Which of these albums is the single?" | Multi-source singles detection (Discogs, MusicBrainz, Last.fm, ListenBrainz) with confidence levels that feed the ratings |
| "I keep finding covers mislabeled as originals" | A full CoverDetector pass (ISRC → MusicBrainz relations → writer analysis) renames confirmed covers to `Title (Artist Cover)` |
| "My library has gaps" | MusicBrainz search + Soulseek (slskd) downloads with a quality filter, a queue, retries and a watcher that imports completed files |
| "I don't trust automation with my files" | A master **tag-writing toggle** turns Popularr into a read-only database scanner — your audio files are never touched |

---

## 🚀 Key Features

### Scoring & ratings

- Blended popularity scores (Last.fm 0.55 / ListenBrainz 0.35 / age 0.10, auto-normalised, dynamic re-weighting when sources disagree)
- Album-relative re-anchoring + artist-catalogue context (z-scores, top-10% standout marking)
- 1–5★ ratings with era-based caps per album, synced back to Navidrome
- Fully explained: every score is logged with its weights, z-scores and evidence

### Six independent scan modes

`Full` · `Navidrome` · `Popularity` · `Metadata` · `Singles` · `Essentia` — each decoupled with its
own rescan window, so you can re-check singles without re-scoring popularity, or run a full refresh
whenever you like.

### Library intelligence

- Single detection from 6 evidence sources with configurable confidence weights
- Cover-song detection (album-level pass with `(Artist Cover)` renames)
- Genre aggregation across MusicBrainz / Discogs / AudioDB / Essentia / Last.fm with synonym normalisation
- Similar-artist recommendations from Last.fm + ListenBrainz
- Live / remix / alternate-take tagging, MusicBrainz ID resolution and re-matching
- Corrections page for metadata disagreements with MusicBrainz

### Playlists

- Per-artist `Essential Collection.m3u`, per-genre `Top Tracks.m3u`, rolling `New Music.m3u`
- CSV import (Exportify), add-to-playlist from any track, ZIP export of any playlist

### Downloads

- Soulseek via **slskd**: search, queue, retry scheduler, quality filter (FLAC / MP3 320), duplicate cleanup
- Watcher service auto-imports completed downloads using your naming convention (with optional FLAC→MP3 conversion)

### Extras

- Essentia on-device mood & genre tagging (TensorFlow, no internet needed)
- Upcoming-releases automation (daily Wikipedia scrape + weekly MusicBrainz refresh)
- Multi-user Navidrome sync, live log streaming with filters, dark modern UI
- 6-step setup wizard, guided configuration, in-app help

---

## 🧠 How It Works

```text
Navidrome ──► import ──► PostgreSQL (local mirror)
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
         Popularity      Singles       Metadata
         scoring         detection     enrichment
              │              │              │
              └──────┬───────┴───────┬──────┘
                     ▼               ▼
              Star ratings      Covers · playlists
                     │
                     ▼
        Synced back to Navidrome (all users)
```

A **Full scan** runs the passes in a deliberately decoupled order:

1. **Basic metadata** — album type + artist MusicBrainz ID (inputs to singles detection)
2. **Popularity** — score every track from Last.fm / ListenBrainz / age
3. **Singles detection** — label each track high / medium / low confidence
4. **Per-track metadata + covers + genres** (MusicBrainz lookups batched per album)
5. **Full album enrichment** — art, artist bios, similar artists, live/remix tagging, alternate takes
6. **Album cover pass** — the full CoverDetector, with renames
7. **Star ratings** → persisted → synced to Navidrome

Every scan is incremental: albums processed within a mode's **rescan window** are skipped with a
single log line — no API calls, no re-scoring, no re-sync. Set a window to `0` and that scan always
runs in full.

> 📘 **Want the details?** The in-app **[User Guide](documentation/USER_GUIDE.md)** explains the whole
> pipeline in plain language — how scores become stars, what every setting does, and how config
> changes affect your library.

---

## 🛠️ Quick Start

### Prerequisites

- [Docker](https://docs.docker.com/engine/install/) + Docker Compose
- A [Navidrome](https://www.navidrome.org) instance (v0.52+ recommended)
- Optional: an [slskd](https://github.com/slskd/slskd) instance for downloads

### Run it

```bash
git clone https://github.com/M0VENTURA/Popularr.git
cd Popularr
# point MUSIC_ROOT (and DOWNLOADS_DIR if you use downloads) at your folders
cp .env.example .env   # adjust paths / ports
docker compose up -d
```

Open `http://localhost:5000` — the 6-step setup wizard will connect you to Navidrome and
PostgreSQL, then run your first **Full** scan. That's it.

### Useful commands

```bash
docker compose up -d          # start
docker compose logs -f app    # follow the app log
docker compose down           # stop
docker compose build --no-cache && docker compose up -d   # rebuild
```

---

## ⚙️ Configuration

Everything is adjustable from the web UI (**Config** page), persisted to `config.yaml`:

- **System** — music users (Navidrome + per-user Last.fm/ListenBrainz), logging, schedulers
- **Integrations** — API keys (Last.fm, Discogs, ListenBrainz, AudioDB)
- **Downloads** — slskd, transfer timeouts, quality filter, watcher, retry/cleanup schedulers
- **Files** — naming convention, FLAC→MP3 conversion, tag-writing (read-only mode), Essentia
- **Matching** — matching thresholds, search filters, Last.fm advanced
- **Analytics & Scoring** — popularity weights, rescan windows, single detection, star bands, genre weights, album scaling
- **Playlists** — essential / genre / new-music generation thresholds and templates

See [documentation/Services/CONFIGURATION_GUIDE.md](documentation/Services/CONFIGURATION_GUIDE.md)
for the full reference.

---

## 📚 Documentation

| Doc | Purpose |
| --- | --- |
| [User Guide](documentation/USER_GUIDE.md) | How the app works, the scoring pipeline, and what settings do — start here |
| [Architecture](documentation/POPULARR_ARCHITECTURE.md) | System design and decision notes |
| [Configuration Guide](documentation/Services/CONFIGURATION_GUIDE.md) | Every configurable parameter |
| [Documentation Index](documentation/README.md) | All docs, change logs and technical notes |

The application also ships an in-app help section (**Help** in the navbar, `/help`) with the full
user guide and searchable documentation.

---

## 🧰 Tech Stack

- **Backend** — Python 3.11, [Quart](https://quart.palletsprojects.com) (async), SQLAlchemy 2.0
- **Database** — PostgreSQL 16
- **Frontend** — Server-rendered Jinja2 templates, Bootstrap 5.3 dark, vanilla JS (no build step required; esbuild for the bundled dist)
- **Integrations** — Navidrome/Subsonic API, Last.fm, ListenBrainz, MusicBrainz, Discogs, AudioDB, Cover Art Archive, Wikidata, slskd/Soulseek, Essentia-to-Metadata
- **Infrastructure** — Docker Compose, APScheduler background jobs, SSE live log streaming

---

## 🧑‍💻 Development

- `requirements.txt` — Python dependencies
- `alembic/` + `migrations/` — database migrations
- `services/` — the application layer (scans, popularity pipeline, enrichment, playlists, downloads)
- `routes/` — Quart blueprints (UI + API)
- `tests/` — pytest suite (`pytest.ini`)

---

*Popularr — let your library tell you what it loves.*
