# 🎧 Popularr – Navidrome Rating CLI

> **Note:** This tool was created with the help of AI assistance. While the code works well, it’s still evolving. The goal is to provide intelligent star ratings for your Navidrome library using multiple data sources.

Popularr is a command-line tool that automates and enriches star ratings inside your Navidrome library. It intelligently fuses data from **Last.fm**, **MusicBrainz**, **Discogs**, and other sources to assign culturally aware ratings — perfect for playlist curation, auto-tagging, or metadata enrichment.

---

## 🧠 What Is Popularr?
Popularr works by blending multiple sources of listening data:

- 📊 **Last.fm playcount ratios**
- 🎵 **MusicBrainz & Discogs metadata**
- 🕰️ **Age-based momentum scoring**
- 🎬 **Single detection via metadata**

---

## 🚀 What Does It Do?
✅ Automatically rate all tracks for one or more artists  
✅ Detect singles using trusted metadata  
✅ Sync star ratings back to Navidrome  
✅ Cache API results to optimize speed and reduce API calls  
✅ Run in perpetual mode for scheduled catalog enrichment  
✅ Print debugging info with scoring breakdowns  
✅ Resume batch scans from the last synced artist  
✅ Force re-scan of all tracks, overriding the cache  
✅ Auto-scan MP3 files in music folder with progress indicators  
✅ Auto-scan Navidrome library with progress indicators and save current ratings  

---


## 🧪 How Does It Work?
Popularr fetches each artist’s tracks from Navidrome and calculates a composite score using:

- **Spotify popularity** (weighted)
- **Last.fm track vs. artist ratio** (weighted)
- **Age momentum** (older tracks decay unless historically significant)
- **Single detection boost** if confirmed via metadata (see Modular Structure below)

You can adjust score weights in `config.yaml`.

---

## 🧩 Modular Structure
Popularr is now fully modularized for maintainability and clarity:

- **single_detector.py**: All advanced single detection logic and helpers (multi-source, weighted, explainable decisions)
- **singledetection.py**: Only DB helpers and orchestration wrappers for single detection state (no detection logic)
- **popularity.py**: Popularity scan logic and integration with single detection
- **popularity_helpers.py**: Shared helpers for popularity scoring and API lookups

If you want to extend or debug single detection, start with `single_detector.py`.

---

---

## 🛠 Setup Requirements
Before running Popularr, you’ll need:

- A **Navidrome API token**
- **Spotify API credentials**
- **Last.fm API key**
- Docker or Python installed locally

---

## 📦 Installation Options

### ✅ Docker Installation
Clone the repo:

git clone https://github.com/M0VENTURA/Popularr.git
cd Popularr

Build and run:

docker build -t popularr .
docker run -v ./config:/config popularr --batchrate --sync

Or use **Docker Compose**:

version: "3.9"
services:
  popularr:
    build: .
    container_name: popularr
    image: moventura/popularr:latest
    volumes:
      - ./config:/config
    command: ["--batchrate", "--sync"]

Run:
docker compose up -d

---

### ✅ Local Installation (Python)

git clone https://github.com/M0VENTURA/Popularr.git
cd Popularr
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python start.py --artist "Radiohead" --sync --verbose

---

## 🔑 How to Get API Keys
- **Navidrome:** Settings → API Tokens → Generate token
- **Spotify:** https://developer.spotify.com/dashboard/ → Create App → Get Client ID & Secret
- **Last.fm:** https://www.last.fm/api/account/create

---

## ⚙️ Configuration
Edit `/config/config.yaml`:

navidrome:
base_url: "http://navidrome:4533"
api_token: "your_navidrome_api_token"
spotify:
client_id: "your_spotify_client_id"
client_secret: "your_spotify_client_secret"
lastfm:
api_key: "your_lastfm_api_key"
features:
dry_run: false
sync: true
force: false
verbose: false
perpetual: false
batchrate: false
artist: []

**Optional Environment Variables:**
- `MUSIC_FOLDER`: Path to music folder for MP3 metadata scanning (default: `/music`)

---

## 🧭 CLI Switches

python start.py [options]

| Switch        | Description                                      |
|---------------|--------------------------------------------------|
| --artist      | Rate one or more artists manually               |
| --batchrate   | Rate the entire library                         |
| --dry-run     | Preview without syncing to Navidrome            |
| --sync        | Push ratings to Navidrome                       |
| --refresh     | Rebuild cached artist index                     |
| --pipeoutput  | Print cached artist index                       |
| --perpetual   | Run a full rating scan every 12 hours           |
| --verbose     | Show scoring breakdowns                         |
| --force       | Force re-scan of all tracks                     |

---

## 📌 Usage Examples
Rate and sync a single artist:

python start.py --artist "Nine Inch Nails" --sync

Rate entire library silently:

python start.py --batchrate --sync

Preview scoring details without syncing:

python start.py --artist "Radiohead" --dry-run --verbose

Run auto rating every 12 hours:

python start.py --perpetual

Run auto rating every 12 hours with MP3 and Navidrome scans:

python start.py --perpetual --batchrate --sync

---


## 🔧 Advanced Options
Customize scoring weights in `config.yaml`:

weights:
	spotify: 0.3
	lastfm: 0.5
	age: 0.2

---

## 🛠️ Development Notes
- All advanced single detection logic is in `single_detector.py`.
- `singledetection.py` is now only for DB helpers and orchestration wrappers.
- For popularity scan logic, see `popularity.py` and `popularity_helpers.py`.

---


---

## 📂 Data Files
| File                | Purpose                                  |
|----------------------|------------------------------------------|
| artist_index.json    | Cached Navidrome artist IDs            |
| rating_cache.json    | Last synced ratings                    |
| single_cache.json    | Confirmed singles                      |

---

## 📬 Feedback & Support
Popularr is designed for personal/local use. PRs and ideas welcome!
