# 🎧 SPTNR – Navidrome Rating CLI

**Note:** This tool was created with the help of CoPilot. I apologize in advance for any poor coding. While I understand coding, I struggle with doing it from scratch and really wanted the ability to tag my songs based on popularity and while the original version was great, it didn't seem to be a good fit for more obscure genre's of music (such as Melodic Death Metal).

SPTNR (pronounced "Spotner") is a command-line tool that automates and enriches star ratings inside your Navidrome library. It intelligently fuses data from Spotify, Last.fm, YouTube, MusicBrainz, and Discogs to assign culturally aware ratings — perfect for playlist curation, auto-tagging, or metadata enrichment.

---

## 🧠 What Is SPTNR?

SPTNR works by blending multiple sources of listening data:
* 🎵 Spotify popularity
* 📊 Last.fm playcount ratios
* 🕰️ Age-based momentum scoring
* 🎬 Single detection via YouTube, MusicBrainz & Discogs

---

## 🚀 What Does It Do?

* ✅ Automatically rate all tracks for one or more artists
* ✅ Detect singles using trusted metadata & official video channels
* ✅ Sync star ratings back to Navidrome
* ✅ Cache API results to optimize speed and reduce API calls
* ✅ Run in perpetual mode for scheduled catalog enrichment
* ✅ Print debugging info with scoring breakdowns, sources used, and star distributions
* ✅ Resume batch scans from the last synced artist
* ✅ Force re-scan of all tracks, overriding the cache
* ✅ **Web Dashboard** for managing configuration and bookmarks

---

## 🌐 Web Dashboard

SPTNR now includes a web dashboard for easy configuration and bookmark management!

### Running the Dashboard

    python dashboard.py

The dashboard will be available at `http://localhost:5000` by default.

You can customize the port by setting the `DASHBOARD_PORT` environment variable:

    DASHBOARD_PORT=8080 python dashboard.py

### Dashboard Features

* 📚 **Bookmark Management**: Add, edit, and delete bookmarks to your favorite music-related websites
* 🔗 **Quick Access**: Use the dropdown menu to quickly navigate to your bookmarked sites
* ⚙️ **Configuration**: Manage SPTNR settings through the web interface
* 🎨 **Modern UI**: Clean and responsive design for easy navigation

### Adding Bookmarks

Bookmarks can be added in two ways:

1. **Through the Web Dashboard**: Click "Add Bookmark" and fill in the site name and URL
2. **Directly in config.yaml**: Add entries under the `bookmarks` section:

```yaml
bookmarks:
  - name: "Navidrome"
    url: "http://localhost:4533"
  - name: "Spotify"
    url: "https://spotify.com"
```

---

## 🧪 How Does It Work?

SPTNR fetches each artist’s tracks from Navidrome and calculates a composite score using:
* **Spotify popularity** (weighted)
* **Last.fm track vs. artist ratio** (weighted)
* **Age momentum** (older tracks receive decay unless historically significant)
* **Single detection boost** if confirmed via metadata or video channels

You can adjust score weights and boosts in your `.env` file.

---

## 🛠️ Setup Requirements

Before running SPTNR, you’ll need a `.env` file with valid credentials and the following Python packages installed.

### Installation Requirements
Install the necessary Python packages using pip:

    pip install -r requirements.txt

Required Packages:
* `requests==2.31.0`
* `python-dotenv==1.0.0`
* `colorama==0.4.6`
* `beautifulsoup4`

### .env Configuration
Create a `.env` file in the root directory with your API keys and Navidrome access details:

    SPOTIFY_CLIENT_ID=your_spotify_client_id
    SPOTIFY_CLIENT_SECRET=your_spotify_client_secret
    LASTFMAPIKEY=your_lastfm_api_key
    YOUTUBE_API_KEY=your_youtube_api_key
    DISCOGS_TOKEN=your_discogs_token
    NAV_BASE_URL=http://localhost:4533
    NAV_USER=admin
    NAV_PASS=yourpassword

### 🐳 Docker Installation

1.  **Clone the Repo**

        git clone https://github.com/M0VENTURA/sptnr.git
        cd sptnr

2.  **Create .env File**
    Use the example above to configure your API keys and Navidrome access.
3.  **Create `docker-compose.yml`**

        version: "3.9"
        services:
          sptnr:
            build: .
            container_name: sptnr
            image: moventura/sptnr:latest
            volumes:
              - ./data:/usr/src/app/data
            env_file:
              - .env
            command: ["--batchrate", "--sync"]

4.  **Run It**

        docker compose build
        docker compose up

    Or run in background:

        docker compose up -d

### 🧪 Local Installation (Python)

    git clone https://github.com/M0VENTURA/sptnr.git
    cd sptnr
    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt

Create your `.env` file and run:

    python sptnr.py --artist "Radiohead" --sync --verbose

### 🔐 How to Get API Keys

* **Spotify**: Developer Dashboard
* **Last.fm**: API key creation
* **YouTube**: Google Cloud Console → enable "YouTube Data API v3" → create API key
* **Discogs**: Developer Settings → generate token

---

## 🧭 CLI Switches

    python sptnr.py [options]

| Switch          | Description                                                    |
| :-------------- | :------------------------------------------------------------- |
| `--artist`      | Rate one or more artists manually                              |
| `--batchrate`   | Rate the entire library in one go                              |
| `--dry-run`     | Preview without syncing stars to Navidrome                     |
| `--sync`        | Push ratings to Navidrome after scoring                        |
| `--refresh`     | Rebuild cached artist index from Navidrome                     |
| `--pipeoutput`  | Print cached artist index (optionally filter with a string)    |
| `--perpetual`   | Run a full rating scan every 12 hours (headless mode)          |
| `--verbose`     | Show scoring breakdowns and summary                            |
| `--resume`      | Resume batch scan from the last synced artist                  |
| `--force`       | Force re-scan of all tracks (override cache)                   |

---

## 📌 Usage Examples

#### Rate and sync a single artist

    python sptnr.py --artist "Nine Inch Nails" --sync

#### Rate entire library silently

    python sptnr.py --batchrate --sync

#### Preview scoring details without syncing

    python sptnr.py --artist "Radiohead" --dry-run --verbose

#### Run auto rating every 12 hours

    python sptnr.py --perpetual

---

## 🧠 Behind the Scenes

SPTNR caches and intelligently handles metadata:
* 📝 Stores synced ratings in `rating_cache.json`
* ⭐ Remembers confirmed singles in `single_cache.json`
* 📺 Tracks YouTube channel authenticity via `channel_cache.json`
* 🚫 Avoids syncing unchanged ratings
* 🔍 Falls back to fuzzy artist matching when needed

---

## 🔧 Advanced Options (.env tuning)

Customize scoring weights and boosts:

    SPOTIFY_WEIGHT=0.3
    LASTFM_WEIGHT=0.5
    AGE_WEIGHT=0.2
    SINGLE_BOOST=10
    LEGACY_BOOST=4

---

## 📂 Data Files

| File                  | Purpose                                   |
| :-------------------- | :---------------------------------------- |
| `artist_index.json`   | Cached Navidrome artist IDs               |
| `rating_cache.json`   | Last synced ratings to avoid duplicates   |
| `single_cache.json`   | Confirmed singles with source info        |
| `channel_cache.json`  | Verified YouTube channel lookups          |

---

## 📬 Feedback & Support

SPTNR is designed for personal and local use.
Ideas for future enhancements (PRs welcome!):
