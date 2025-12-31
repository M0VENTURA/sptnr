
#!/usr/bin/env python3
# 🎧 SPTNR – Navidrome Rating CLI with Spotify + Last.fm + Navidrome API Integration
import argparse, os, sys, requests, time, random, json, logging, base64, re, sqlite3, math, yaml
from colorama import init, Fore, Style
from datetime import datetime, timedelta
from statistics import median
from collections import defaultdict

# 🎨 Colorama setup
init(autoreset=True)
LIGHT_RED = Fore.RED + Style.BRIGHT
LIGHT_GREEN = Fore.GREEN + Style.BRIGHT
LIGHT_BLUE = Fore.BLUE + Style.BRIGHT
LIGHT_YELLOW = Fore.YELLOW + Style.BRIGHT
LIGHT_CYAN = Fore.CYAN + Style.BRIGHT
RESET = Style.RESET_ALL

# ✅ Load config.yaml

CONFIG_PATH = "/config/config.yaml"


def create_default_config(path):
    default_config = {
        "navidrome": {
            "base_url": "http://localhost:4533",
            "user": "admin",
            "pass": "password"
        },
        "spotify": {
            "client_id": "your_spotify_client_id",
            "client_secret": "your_spotify_client_secret"
        },
        "lastfm": {
            "api_key": "your_lastfm_api_key"
        },
        "discogs": {
            "token": "your_discogs_token"
        },
        "audiodb": {
            "api_key": "your_audiodb_api_key"
        },
        "google": {
            "api_key": "your_google_api_key",
            "cse_id": "your_google_cse_id"
        },
        "youtube": {
            "api_key": "your_youtube_api_key"
        },
        "listenbrainz": {
            "enabled": True
        },
        "weights": {
            "spotify": 0.4,
            "lastfm": 0.3,
            "listenbrainz": 0.2,
            "age": 0.1
        },
        "database": {
            "path": "/database/sptnr.db"
        },
        "logging": {
            "level": "INFO",
            "file": "/config/app.log"
        },
        "features": {
            "dry_run": False,
            "sync": True,
            "force": False,
            "verbose": False,
            "perpetual": False,
            "batchrate": False,
            "artist": []
        }
    }

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            yaml.safe_dump(default_config, f)
        print(f"✅ Default config.yaml created at {path}")
    except Exception as e:
        print(f"❌ Failed to create default config.yaml: {e}")
        sys.exit(1)



def load_config():
    if not os.path.exists(CONFIG_PATH):
        print(f"⚠️ Config file not found at {CONFIG_PATH}. Creating default config...")
        create_default_config(CONFIG_PATH)
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f)

config = load_config()

# ✅ Ensure 'features' section exists in config
if "features" not in config:
    config["features"] = {
        "dry_run": False,
        "sync": True,
        "force": False,
        "verbose": False,
        "perpetual": False,
        "batchrate": False,
        "artist": []
    }

# ✅ Extract feature flags
dry_run = config["features"]["dry_run"]
sync = config["features"]["sync"]
force = config["features"]["force"]
verbose = config["features"]["verbose"]
perpetual = config["features"]["perpetual"]
batchrate = config["features"]["batchrate"]
artist_list = config["features"]["artist"]



def validate_config(config):
    issues = []

    # Check Navidrome credentials
    if config["navidrome"].get("user") in ["admin", "", None]:
        issues.append("Navidrome username is not set (currently 'admin').")
    if config["navidrome"].get("pass") in ["password", "", None]:
        issues.append("Navidrome password is not set (currently 'password').")

    # Check Spotify credentials
    if config["spotify"].get("client_id") in ["your_spotify_client_id", "", None]:
        issues.append("Spotify Client ID is missing or placeholder.")
    if config["spotify"].get("client_secret") in ["your_spotify_client_secret", "", None]:
        issues.append("Spotify Client Secret is missing or placeholder.")
    
    # Check Last.fm API key
    if config["lastfm"].get("api_key") in ["your_lastfm_api_key", "", None]:
        issues.append("Last.fm API key is missing or placeholder.")
    
    if issues:
        print("\n⚠️ Configuration issues detected:")
        for issue in issues:
            print(f" - {issue}")

        print("\n❌ Please update config.yaml before continuing.")
        print("👉 To edit the file inside the container, run:")
        print("   vi /config/config.yaml")
        print("✅ After saving changes, restart the container")
        # Keep container alive and wait for user action
        print("⏸ Waiting for config update... Container will stay alive. Please restart the container after editing the config.")
        try:
            while True:
                time.sleep(60)  # Sleep indefinitely until container is restarted
        except KeyboardInterrupt:
            print("\nℹ️ Exiting script.")
            sys.exit(0)



# ✅ Call this right after loading config
validate_config(config)



# ✅ Extract credentials and settings
NAV_BASE_URL = config["navidrome"]["base_url"]
USERNAME = config["navidrome"]["user"]
PASSWORD = config["navidrome"]["pass"]

# Spotify
SPOTIFY_CLIENT_ID = config["spotify"]["client_id"]
SPOTIFY_CLIENT_SECRET = config["spotify"]["client_secret"]

# Last.fm
LASTFM_API_KEY = config["lastfm"]["api_key"]

# Discogs
DISCOGS_TOKEN = config["discogs"]["token"]

# AudioDB
AUDIODB_API_KEY = config["audiodb"]["api_key"]

# Google Custom Search
GOOGLE_API_KEY = config["google"]["api_key"]
GOOGLE_CSE_ID = config["google"]["cse_id"]

# YouTube
YOUTUBE_API_KEY = config["youtube"]["api_key"]

# Weights
SPOTIFY_WEIGHT = config["weights"]["spotify"]
LASTFM_WEIGHT = config["weights"]["lastfm"]
LISTENBRAINZ_WEIGHT = config["weights"]["listenbrainz"]
AGE_WEIGHT = config["weights"]["age"]

# Database path
DB_PATH = config["database"]["path"]


# ✅ Ensure database directory exists
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# ✅ Import schema updater and update DB schema
from check_db import update_schema
update_schema(DB_PATH)



# ✅ Compatibility check for OpenSubsonic extensions
def get_supported_extensions():
    url = f"{NAV_BASE_URL}/rest/getOpenSubsonicExtensions.view"
    params = {"u": USERNAME, "p": PASSWORD, "v": "1.16.1", "c": "sptnr", "f": "json"}
    try:
        res = requests.get(url, params=params)
        res.raise_for_status()
        extensions = res.json().get("subsonic-response", {}).get("openSubsonicExtensions", [])
        print(f"✅ Supported extensions: {extensions}")
        return extensions
    except Exception as e:
        print(f"⚠️ Failed to fetch extensions: {e}")
        return []

SUPPORTED_EXTENSIONS = get_supported_extensions()

# ✅ Decide feature usage
USE_FORMPOST = "formPost" in SUPPORTED_EXTENSIONS
USE_SEARCH3 = "search3" in SUPPORTED_EXTENSIONS


# ✅ Logging setup
logging.basicConfig(
    level=getattr(logging, config["logging"]["level"].upper(), logging.INFO),
    filename=config["logging"]["file"],
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# ✅ Ensure database directory exists
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


def save_to_db(track_data):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
    INSERT OR REPLACE INTO tracks (
        id, artist, album, title, spotify_score, lastfm_score, listenbrainz_score,
        age_score, final_score, stars, genres, navidrome_genres, spotify_genres, lastfm_tags,
        spotify_album, spotify_artist, spotify_popularity, spotify_release_date, spotify_album_art_url,
        lastfm_track_playcount, lastfm_artist_playcount, file_path, is_single, single_confidence, last_scanned
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        track_data["id"], track_data["artist"], track_data["album"], track_data["title"],
        track_data.get("spotify_score", 0), track_data.get("lastfm_score", 0),
        track_data.get("listenbrainz_score", 0), track_data.get("age_score", 0),
        track_data["score"], track_data["stars"], ",".join(track_data["genres"]),
        ",".join(track_data.get("navidrome_genres", [])), ",".join(track_data.get("spotify_genres", [])),
        ",".join(track_data.get("lastfm_tags", [])), track_data.get("spotify_album", ""),
        track_data.get("spotify_artist", ""), track_data.get("spotify_popularity", 0),
        track_data.get("spotify_release_date", ""), track_data.get("spotify_album_art_url", ""),
        track_data.get("lastfm_track_playcount", 0), track_data.get("lastfm_artist_playcount", 0),
        track_data.get("file_path", ""), track_data.get("is_single", False),
        track_data.get("single_confidence", ""), track_data.get("last_scanned", "")
    ))
    conn.commit()
    conn.close()

# --- Spotify API Helpers ---
import requests
import base64
import difflib

def get_spotify_token():
    """Retrieve Spotify API token using client credentials."""
    auth_str = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}"
    auth_bytes = auth_str.encode("utf-8")
    auth_base64 = base64.b64encode(auth_bytes).decode("utf-8")

    headers = {
        "Authorization": f"Basic {auth_base64}",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {"grant_type": "client_credentials"}

    try:
        res = requests.post("https://accounts.spotify.com/api/token", headers=headers, data=data)
        res.raise_for_status()
        return res.json()["access_token"]
    except Exception as e:
        logging.error(f"Spotify Token Error: {e}")
        sys.exit(1)

def search_spotify_track(title, artist, album=None):
    """Search for a track on Spotify by title, artist, and optional album."""
    def query(q):
        params = {"q": q, "type": "track", "limit": 10}
        token = get_spotify_token()
        headers = {"Authorization": f"Bearer {token}"}
        res = requests.get("https://api.spotify.com/v1/search", headers=headers, params=params)
        res.raise_for_status()
        return res.json().get("tracks", {}).get("items", [])

    queries = [
        f"{title} artist:{artist} album:{album}" if album else None,
        f"{strip_parentheses(title)} artist:{artist}",
        f"{title.replace('Part', 'Pt.')} artist:{artist}"
    ]

    all_results = []
    for q in filter(None, queries):
        try:
            results = query(q)
            if results:
                all_results.extend(results)
        except:
            continue

    return all_results

def select_best_spotify_match(results, track_title):
    """Select the best Spotify match based on popularity and album type."""
    allow_live_remix = version_requested(track_title)
    filtered = [r for r in results if is_valid_version(r["name"], allow_live_remix)]
    if not filtered:
        return {"popularity": 0}
    singles = [r for r in filtered if r.get("album", {}).get("album_type", "").lower() == "single"]
    if singles:
        return max(singles, key=lambda r: r.get("popularity", 0))
    return max(filtered, key=lambda r: r.get("popularity", 0))

def is_discogs_single(title, artist):
    token = os.getenv("DISCOGS_TOKEN")
    if not token:
        print("❌ Missing Discogs token.")
        return False

    headers = {
        "Authorization": f"Discogs token={token}",
        "User-Agent": "sptnr-cli/1.0"
    }
    query = f"{artist} {title}"
    url = "https://api.discogs.com/database/search"
    params = {
        "q": query,
        "type": "release",
        "format": "Single",
        "per_page": 5
    }

    try:
        res = requests.get(url, headers=headers, params=params)
        res.raise_for_status()
        results = res.json().get("results", [])
        return any("Single" in r.get("format", []) for r in results)
    except Exception as e:
        print(f"⚠️ Discogs lookup failed for '{title}': {type(e).__name__} - {e}")
        return False

def get_discogs_genres(title, artist):
    if not DISCOGS_TOKEN:
        logging.warning("Discogs token missing. Skipping Discogs genre lookup.")
        return []

    headers = {
        "Authorization": f"Discogs token={DISCOGS_TOKEN}",
        "User-Agent": "sptnr-cli/1.0"
    }
    params = {"q": f"{artist} {title}", "type": "release", "per_page": 5}

    try:
        res = requests.get("https://api.discogs.com/database/search", headers=headers, params=params)
        res.raise_for_status()
        results = res.json().get("results", [])
        genres = []
        for r in results:
            genres.extend(r.get("genre", []))
            genres.extend(r.get("style", []))
        return genres
    except Exception as e:
        logging.error(f"Discogs lookup failed for '{title}': {e}")
        return []


def get_audiodb_genres(artist):
    if not AUDIODB_API_KEY:
        return []
    try:
        res = requests.get(f"https://theaudiodb.com/api/v1/json/{AUDIODB_API_KEY}/search.php?s={artist}", timeout=10)
        res.raise_for_status()
        data = res.json().get("artists", [])
        if data and data[0].get("strGenre"):
            return [data[0]["strGenre"]]
        return []
    except Exception as e:
        logging.warning(f"AudioDB lookup failed for '{artist}': {e}")
        return []

def get_musicbrainz_genres(title, artist):
    try:
        res = requests.get("https://musicbrainz.org/ws/2/recording/", params={
            "query": f'"{title}" AND artist:"{artist}"',
            "fmt": "json",
            "limit": 1
        }, headers={"User-Agent": "sptnr-cli/2.0"}, timeout=10)
        res.raise_for_status()
        recordings = res.json().get("recordings", [])
        if recordings and "tags" in recordings[0]:
            return [t["name"] for t in recordings[0]["tags"]]
        return []
    except Exception as e:
        logging.warning(f"MusicBrainz lookup failed for '{title}': {e}")
        return []


def version_requested(track_title):
    """Check if track title suggests a live or remix version."""
    keywords = ["live", "remix"]
    return any(k in track_title.lower() for k in keywords)

def is_valid_version(track_title, allow_live_remix=False):
    """Validate track version against blacklist and whitelist."""
    title = track_title.lower()
    blacklist = ["live", "remix", "mix", "edit", "rework", "bootleg"]
    whitelist = ["remaster"]
    if allow_live_remix:
        blacklist = [b for b in blacklist if b not in ["live", "remix"]]
    if any(b in title for b in blacklist) and not any(w in title for w in whitelist):
        return False
    return True

def strip_parentheses(s):
    """Remove text inside parentheses from a string."""
    return re.sub(r"\s*\(.*?\)\s*", " ", s).strip()

# --- Last.fm Helpers ---
def get_lastfm_track_info(artist, title):
    """Fetch track and artist play counts from Last.fm."""
    if not LASTFM_API_KEY:
        logging.warning(f"Last.fm API key missing. Skipping lookup for '{title}' by '{artist}'.")
        return {"track_play": 0, "artist_play": 0}

    headers = {"User-Agent": "sptnr-cli"}
    params = {
        "method": "track.getInfo",
        "artist": artist,
        "track": title,
        "api_key": LASTFM_API_KEY,
        "format": "json"
    }

    try:
        res = requests.get("https://ws.audioscrobbler.com/2.0/", headers=headers, params=params, timeout=10)
        res.raise_for_status()
        data = res.json().get("track", {})
        track_play = int(data.get("playcount", 0))
        artist_play = int(data.get("artist", {}).get("stats", {}).get("playcount", 0))
        return {"track_play": track_play, "artist_play": artist_play}
    except Exception as e:
        logging.error(f"Last.fm fetch failed for '{title}': {e}")
        return {"track_play": 0, "artist_play": 0}

def get_listenbrainz_score(mbid):
    """Fetch ListenBrainz listen count for a track using MBID."""
    try:
        url = f"https://api.listenbrainz.org/1/stats/track/{mbid}"
        res = requests.get(url, timeout=10)
        res.raise_for_status()
        data = res.json()
        return data.get("count", 0)  # Normalize later if needed
    except Exception as e:
        logging.warning(f"ListenBrainz fetch failed for MBID {mbid}: {e}")
        return 0

# --- Scoring Logic ---



def compute_track_score(title, artist_name, release_date, sp_score, mbid=None, verbose=False):
    """Compute weighted score for a track using Spotify, Last.fm, ListenBrainz, and age decay."""
    lf_data = get_lastfm_track_info(artist_name, title)
    lf_track = lf_data["track_play"] if lf_data else 0
    lf_artist = lf_data["artist_play"] if lf_data else 0
    lf_ratio = round((lf_track / lf_artist) * 100, 2) if lf_artist > 0 else 0
    momentum, days_since = score_by_age(lf_track, release_date)

    lb_score = get_listenbrainz_score(mbid) if mbid and config["listenbrainz"]["enabled"] else 0

    score = (SPOTIFY_WEIGHT * sp_score) + \
            (LASTFM_WEIGHT * lf_ratio) + \
            (LISTENBRAINZ_WEIGHT * lb_score) + \
            (AGE_WEIGHT * momentum)

    if verbose:
        print(f"🔢 Raw score for '{title}': {round(score)} "
              f"(Spotify: {sp_score}, Last.fm: {lf_ratio}, ListenBrainz: {lb_score}, Age: {momentum})")

    return score, momentum, lb_score


def score_by_age(playcount, release_str):
    """Apply age decay to score based on release date."""
    try:
        release_date = datetime.strptime(release_str, "%Y-%m-%d")
        days_since = max((datetime.now() - release_date).days, 30)
        capped_days = min(days_since, 5 * 365)
        decay = 1 / math.log2(capped_days + 2)
        return playcount * decay, days_since
    except:
        return 0, 9999

# --- Genre Handling ---
GENRE_WEIGHTS = {
    "musicbrainz": 0.40,
    "discogs": 0.25,
    "audiodb": 0.20,
    "lastfm": 0.10,
    "spotify": 0.05
}

def normalize_genre(genre):
    """Normalize genre names to avoid duplicates and inconsistencies."""
    genre = genre.lower().strip()
    synonyms = {"hip hop": "hip-hop", "r&b": "rnb"}
    return synonyms.get(genre, genre)

def clean_conflicting_genres(genres):
    """Remove conflicting or irrelevant genres based on dominant tags."""
    genres_lower = [g.lower() for g in genres]
    if any("punk" in g for g in genres_lower):
        genres_lower = [g for g in genres_lower if g not in ["electronic", "electro"]]
    if any("metal" in g for g in genres_lower):
        genres_lower = [g for g in genres_lower if g not in ["electronic", "electro"]]
    if any("progressive metal" in g for g in genres_lower):
        genres_lower = [g for g in genres_lower if g not in ["metal", "heavy metal"]]
    return genres_lower

def get_top_genres_with_navidrome(sources, nav_genres, title="", album=""):
    """Combine online-sourced genres with Navidrome genres for comparison."""
    genre_scores = defaultdict(float)
    for source, genres in sources.items():
        weight = GENRE_WEIGHTS.get(source, 0)
        for genre in genres:
            norm = normalize_genre(genre)
            genre_scores[norm] += weight
    if "live" in title.lower() or "live" in album.lower():
        genre_scores["live"] += 0.5
    if any(word in title.lower() or word in album.lower() for word in ["christmas", "xmas"]):
        genre_scores["christmas"] += 0.5
    sorted_genres = sorted(genre_scores.items(), key=lambda x: x[1], reverse=True)
    filtered = [g for g, _ in sorted_genres]
    filtered = clean_conflicting_genres(filtered)
    filtered = list(dict.fromkeys(filtered))
    metal_subgenres = [g for g in filtered if "metal" in g.lower() and g.lower() != "heavy metal"]
    if metal_subgenres:
        filtered = [g for g in filtered if g.lower() != "heavy metal"]
    if not filtered:
        filtered = [g for g, _ in sorted_genres]
    online_top = [g.capitalize() for g in filtered[:3]]
    nav_cleaned = [normalize_genre(g).capitalize() for g in nav_genres if g]
    return online_top, nav_cleaned

def set_track_rating(track_id, stars):
    """
    Set user rating for a track in Navidrome using Subsonic API.
    :param track_id: Track ID in Navidrome
    :param stars: Rating (1–5)
    """
    url = f"{NAV_BASE_URL}/rest/setRating.view"
    params = {"u": USERNAME, "p": PASSWORD, "v": "1.16.1", "c": "sptnr", "id": track_id, "rating": stars}
    try:
        res = requests.get(url, params=params)
        res.raise_for_status()
        logging.info(f"✅ Set rating {stars}/5 for track {track_id}")
    except Exception as e:
        logging.error(f"❌ Failed to set rating for track {track_id}: {e}")


def create_playlist(name, track_ids):
    url = f"{NAV_BASE_URL}/rest/createPlaylist.view"
    if USE_FORMPOST:
        print("ℹ️ Using formPost for playlist creation.")
        data = {"u": USERNAME, "p": PASSWORD, "v": "1.16.1", "c": "sptnr", "name": name}
        for tid in track_ids:
            data.setdefault("songId", []).append(tid)
        res = requests.post(url, data=data)
    else:
        params = {"u": USERNAME, "p": PASSWORD, "v": "1.16.1", "c": "sptnr", "name": name}
        for tid in track_ids:
            params.setdefault("songId", []).append(tid)
        res = requests.get(url, params=params)
    res.raise_for_status()
    print(f"✅ Playlist '{name}' created with {len(track_ids)} tracks.")


def fetch_artist_albums(artist_id):
    url = f"{NAV_BASE_URL}/rest/getArtist.view"
    params = {"u": USERNAME, "p": PASSWORD, "v": "1.16.1", "c": "sptnr", "id": artist_id, "f": "json"}
    try:
        res = requests.get(url, params=params)
        res.raise_for_status()
        return res.json().get("subsonic-response", {}).get("artist", {}).get("album", [])
    except Exception as e:
        logging.error(f"❌ Failed to fetch albums for artist {artist_id}: {e}")
        return []


def fetch_album_tracks(album_id):
    """
    Fetch all tracks for an album using Subsonic API.
    :param album_id: Album ID in Navidrome
    :return: List of track objects
    """
    url = f"{NAV_BASE_URL}/rest/getAlbum.view"
    params = {"u": USERNAME, "p": PASSWORD, "v": "1.16.1", "c": "sptnr", "id": album_id, "f": "json"}
    try:
        res = requests.get(url, params=params)
        res.raise_for_status()
        return res.json().get("subsonic-response", {}).get("album", {}).get("song", [])
    except Exception as e:
        logging.error(f"❌ Failed to fetch tracks for album {album_id}: {e}")
        return []



def build_artist_index():
    url = f"{NAV_BASE_URL}/rest/getArtists.view"
    params = {"u": USERNAME, "p": PASSWORD, "v": "1.16.1", "c": "sptnr", "f": "json"}
    try:
        res = requests.get(url, params=params)
        res.raise_for_status()
        index = res.json().get("subsonic-response", {}).get("artists", {}).get("index", [])
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        artist_map = {}
        for group in index:
            for a in group.get("artist", []):
                artist_id = a["id"]
                artist_name = a["name"]
                cursor.execute("""
                    INSERT OR REPLACE INTO artist_stats (artist_id, artist_name, album_count, track_count, last_updated)
                    VALUES (?, ?, ?, ?, ?)
                """, (artist_id, artist_name, 0, 0, None))
                artist_map[artist_name] = {"id": artist_id, "album_count": 0, "track_count": 0, "last_updated": None}
        conn.commit()
        conn.close()
        logging.info(f"✅ Cached {len(artist_map)} artists in DB")
        return artist_map
    except Exception as e:
        logging.error(f"❌ Failed to build artist index: {e}")
        return {}


# --- Main Rating Logic ---

def update_artist_stats(artist_id, artist_name):
    album_count = len(fetch_artist_albums(artist_id))
    track_count = sum(len(fetch_album_tracks(a['id'])) for a in fetch_artist_albums(artist_id))
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO artist_stats (artist_id, artist_name, album_count, track_count, last_updated)
        VALUES (?, ?, ?, ?, ?)
    """, (artist_id, artist_name, album_count, track_count, datetime.now().strftime("%Y-%m-%dT%H:%M:%S")))
    conn.commit()
    conn.close()


def load_artist_map():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT artist_id, artist_name, album_count, track_count, last_updated FROM artist_stats")
    rows = cursor.fetchall()
    conn.close()
    return {row[1]: {"id": row[0], "album_count": row[2], "track_count": row[3], "last_updated": row[4]} for row in rows}


def adjust_genres(genres, artist_is_metal=False):
    """
    Adjust genres based on artist context:
    - If artist is metal-dominant, convert rock sub-genres to metal equivalents.
    - Always deduplicate and remove generic 'metal' if sub-genres exist.
    """
    adjusted = []
    for g in genres:
        g_lower = g.lower()
        if artist_is_metal:
            if g_lower in ["prog rock", "progressive rock"]:
                adjusted.append("Progressive metal")
            elif g_lower == "folk rock":
                adjusted.append("Folk metal")
            elif g_lower == "goth rock":
                adjusted.append("Gothic metal")
            else:
                adjusted.append(g)
        else:
            adjusted.append(g)

    # Remove generic 'metal' if specific sub-genres exist
    metal_subgenres = [x for x in adjusted if "metal" in x.lower() and x.lower() != "metal"]
    if metal_subgenres:
        adjusted = [x for x in adjusted if x.lower() not in ["metal", "heavy metal"]]

    return list(dict.fromkeys(adjusted))  # Deduplicate


def rate_artist(artist_id, artist_name, verbose=False, force=False):
    """
    Rate all tracks for a given artist:
    - Compute scores (Spotify, Last.fm, ListenBrainz, Age)
    - Detect singles (Spotify album_type or album length)
    - Assign stars (boost for singles)
    - Save to DB with enriched metadata
    - Update Navidrome ratings
    - Create playlist for top tracks
    """
    albums = fetch_artist_albums(artist_id)
    if not albums:
        print(f"⚠️ No albums found for artist '{artist_name}'")
        return {}

    print(f"\n🎨 Starting rating for artist: {artist_name} ({len(albums)} albums)")
    rated_map = {}
    playlist_tracks = []

    for album in albums:
        album_name = album.get("name", "Unknown Album")
        album_id = album.get("id")
        tracks = fetch_album_tracks(album_id)
        if not tracks:
            print(f"⚠️ No tracks found in album '{album_name}'")
            continue

        print(f"\n🎧 Scanning album: {album_name} ({len(tracks)} tracks)")
        album_tracks = []

        for track in tracks:
            track_id = track["id"]
            title = track["title"]
            file_path = track.get("path", "")
            nav_genres = [track.get("genre")] if track.get("genre") else []
            mbid = track.get("mbid", None)  # MusicBrainz ID if available

            print(f"   🔍 Processing track: {title}")

            # ✅ Spotify lookup
            spotify_results = search_spotify_track(title, artist_name, album_name)
            selected = select_best_spotify_match(spotify_results, title)
            sp_score = selected.get("popularity", 0)
            spotify_album = selected.get("album", {}).get("name", "")
            spotify_artist = selected.get("artists", [{}])[0].get("name", "")
            spotify_genres = selected.get("artists", [{}])[0].get("genres", [])
            spotify_release_date = selected.get("album", {}).get("release_date", "")
            spotify_album_art_url = selected.get("album", {}).get("images", [{}])[0].get("url", "")

            # ✅ Last.fm lookup
            lf_data = get_lastfm_track_info(artist_name, title)
            lf_track_play = lf_data.get("track_play", 0)
            lf_artist_play = lf_data.get("artist_play", 0)
            lf_ratio = round((lf_track_play / lf_artist_play) * 100, 2) if lf_artist_play > 0 else 0
            lastfm_tags = []  # Placeholder for future tag fetch

            # ✅ Compute score using unified function
            score, momentum, lb_score = compute_track_score(
                title,
                artist_name,
                spotify_release_date or "1992-01-01",
                sp_score,
                mbid,
                verbose
            )
            
            
            # ✅ Full genre enrichment
            discogs_genres = get_discogs_genres(title, artist_name)  # Always used
            audiodb_genres = []
            if use_audiodb and AUDIODB_API_KEY:
                audiodb_genres = get_audiodb_genres(artist_name)
            
            mb_genres = get_musicbrainz_genres(title, artist_name)  # Always used
            
            # ✅ Placeholder for Google lookup
            if use_google and GOOGLE_API_KEY and GOOGLE_CSE_ID:
                # TODO: Implement Google lookup for single detection
                pass
            else:
                logging.info("Skipping Google lookup (disabled in config)")
            
            # ✅ Placeholder for YouTube lookup
            if use_youtube and YOUTUBE_API_KEY:
                # TODO: Implement YouTube lookup for single detection
                pass
            else:
                logging.info("Skipping YouTube lookup (disabled in config)")
            
            lastfm_tags = []  # Optional: fetch Last.fm tags if needed
            
            # ✅ Combine all genres from all sources
            all_genres = spotify_genres + lastfm_tags + discogs_genres + audiodb_genres + mb_genres + nav_genres
            
            def determine_weighted_genres(all_genres):
                """
                Decide if track should be rock or metal weighted based on full genre collection.
                Ignore generic 'rock' or 'metal' when sub-genres exist.
                """
                normalized = [normalize_genre(g) for g in all_genres if g]
                normalized = list(dict.fromkeys(normalized))  # Deduplicate
            
                # Detect sub-genres
                metal_subgenres = [g for g in normalized if "metal" in g and g not in ["metal", "heavy metal"]]
                rock_subgenres = [g for g in normalized if "rock" in g and g != "rock"]
            
                if metal_subgenres:
                    # Remove generic metal if sub-genres exist
                    normalized = [g for g in normalized if g not in ["metal", "heavy metal"]]
                    return normalized, "metal"
                elif rock_subgenres:
                    # Remove generic rock if sub-genres exist
                    normalized = [g for g in normalized if g != "rock"]
                    return normalized, "rock"
                else:
                    return normalized, "neutral"
            
            # ✅ Determine weighted genres and context
            adjusted_genres, genre_context = determine_weighted_genres(all_genres)
            
            # ✅ Weighted aggregation using cleaned genres instead of raw
            top_genres, _ = get_top_genres_with_navidrome({
                "spotify": spotify_genres,
                "lastfm": lastfm_tags,
                "discogs": discogs_genres,
                "audiodb": audiodb_genres,
                "musicbrainz": mb_genres
            }, nav_genres, title=title, album=album_name)
            
            # ✅ Replace weighted top genres with adjusted genres for final use
            top_genres = adjust_genres(adjusted_genres, artist_is_metal=(genre_context == "metal"))


            album_tracks.append({
                "id": track_id,
                "title": title,
                "album": album_name,
                "artist": artist_name,
                "score": score,
                "spotify_score": sp_score,
                "genres": top_genres,
                "navidrome_genres": nav_genres,
                "spotify_genres": spotify_genres,
                "lastfm_tags": lastfm_tags,
                "spotify_album": spotify_album,
                "spotify_artist": spotify_artist,
                "spotify_popularity": sp_score,
                "spotify_release_date": spotify_release_date,
                "spotify_album_art_url": spotify_album_art_url,
                "lastfm_track_playcount": lf_track_play,
                "lastfm_artist_playcount": lf_artist_play,
                "file_path": file_path,
                "last_scanned": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "listenbrainz_score": lb_score,
                "age_score": momentum
            })

        # ✅ Sort and assign stars
        sorted_album = sorted(album_tracks, key=lambda x: x["score"], reverse=True)
        band_size = max(1, math.ceil(len(sorted_album) / 4))
        median_score = median(track["score"] for track in sorted_album) or 10
        jump_threshold = median_score * 1.7

        for i, track in enumerate(sorted_album):
            stars = max(1, 4 - (i // band_size))
            if track["score"] >= jump_threshold:
                stars = 5

            # ✅ Detect single
            
            is_single = False
            single_confidence = ""
            sources = []
            
            if selected.get("album", {}).get("album_type", "").lower() == "single":
                sources.append("spotify")
            if is_discogs_single(title, artist_name):
                sources.append("discogs")
            if len(tracks) == 1:
                sources.append("album_length")
            
            if sources:
                is_single = True
                if len(sources) >= 2:
                    single_confidence = "high"
                elif len(sources) == 1:
                    single_confidence = "medium"
                else:
                    single_confidence = "low"


            # ✅ Boost stars for singles
            if is_single and stars < 4:
                stars = min(stars + 1, 5)

            track["stars"] = stars
            track["score"] = round(track["score"]) if track["score"] > 0 else random.randint(5, 15)
            track["is_single"] = is_single
            track["single_confidence"] = single_confidence

            # ✅ Save to DB with enriched metadata
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO tracks (
                    id, artist, album, title, spotify_score, lastfm_score, listenbrainz_score,
                    age_score, final_score, stars, genres, navidrome_genres, spotify_genres, lastfm_tags,
                    discogs_genres, audiodb_genres, musicbrainz_genres,  -- ✅ NEW
                    spotify_album, spotify_artist, spotify_popularity, spotify_release_date, spotify_album_art_url,
                    lastfm_track_playcount, lastfm_artist_playcount, file_path, is_single, single_confidence, last_scanned
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                track["id"], track["artist"], track["album"], track["title"],
                track.get("spotify_score", 0), lf_ratio, track["listenbrainz_score"], track["age_score"],
                track["score"], track["stars"], ",".join(track["genres"]),
                ",".join(track["navidrome_genres"]), ",".join(track["spotify_genres"]), ",".join(track["lastfm_tags"]),
                ",".join(discogs_genres), ",".join(audiodb_genres), ",".join(mb_genres),  # ✅ NEW
                track["spotify_album"], track["spotify_artist"], track["spotify_popularity"], track["spotify_release_date"], track["spotify_album_art_url"],
                track["lastfm_track_playcount"], track["lastfm_artist_playcount"], track["file_path"],
                track["is_single"], track["single_confidence"], track["last_scanned"]
            ))

            conn.commit()
            conn.close()

            # ✅ Update Navidrome rating
            set_track_rating(track["id"], stars)
            print(f"   ✅ Navidrome rating updated: {track['title']} → {stars} stars")

            # ✅ Add top-rated tracks to playlist
            if stars >= 4:
                playlist_tracks.append(track["id"])

            rated_map[track["id"]] = track

        print(f"✔ Completed album: {album_name}")
   
    # ✅ Create playlist for artist's top tracks
    five_star_tracks = [track["id"] for track in sorted_album if track["stars"] == 5]
    
    if artist_name.lower() != "various artists" and len(five_star_tracks) > 10:
        playlist_name = f"Essential {artist_name}"
        create_playlist(playlist_name, five_star_tracks)
        print(f"🎶 Essential playlist created: {playlist_name} with {len(five_star_tracks)} tracks")
    else:
        print(f"ℹ️ No Essential playlist created for {artist_name} (5★ tracks: {len(five_star_tracks)})")

    print(f"✅ Finished rating for artist: {artist_name}")
    return rated_map

# --- CLI Handling ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="🎧 SPTNR – Navidrome Rating CLI with API Integration")
    parser.add_argument("--artist", type=str, nargs="+", help="Rate one or more artists by name")
    parser.add_argument("--batchrate", action="store_true", help="Rate the entire library")
    parser.add_argument("--refresh", action="store_true", help="Rebuild artist index cache")
    parser.add_argument("--pipeoutput", type=str, nargs="?", const="", help="Print cached artist index")
    parser.add_argument("--perpetual", action="store_true", help="Run perpetual 12-hour scan loop")
    parser.add_argument("--dry-run", action="store_true", help="Preview artist list only (no rating)")
    parser.add_argument("--sync", action="store_true", help="Push ratings to Navidrome after calculation")
    parser.add_argument("--force", action="store_true", help="Force re-scan of all tracks")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose debug output")
    args = parser.parse_args()

    # ✅ Update config.yaml with CLI overrides if provided
    def update_config_with_cli(args, config, config_path=CONFIG_PATH):
        updated = False
        if args.dry_run:
            config["features"]["dry_run"] = True; updated = True
        if args.sync:
            config["features"]["sync"] = True; updated = True
        if args.force:
            config["features"]["force"] = True; updated = True
        if args.verbose:
            config["features"]["verbose"] = True; updated = True
        if args.perpetual:
            config["features"]["perpetual"] = True; updated = True
        if args.batchrate:
            config["features"]["batchrate"] = True; updated = True
        if args.artist:
            config["features"]["artist"] = args.artist; updated = True

        if updated:
            try:
                with open(config_path, "w") as f:
                    yaml.safe_dump(config, f)
                print(f"✅ Config updated with CLI overrides in {config_path}")
            except Exception as e:
                print(f"❌ Failed to update config.yaml: {e}")

    update_config_with_cli(args, config)

    # ✅ Merge config values for runtime
    dry_run = config["features"]["dry_run"]
    sync = config["features"]["sync"]
    force = config["features"]["force"]
    verbose = config["features"]["verbose"]
    perpetual = config["features"]["perpetual"]
    batchrate = config["features"]["batchrate"]
    artist_list = config["features"]["artist"]
    use_google = config["features"].get("use_google", False)
    use_youtube = config["features"].get("use_youtube", False)
    use_audiodb = config["features"].get("use_audiodb", False)


    # ✅ Refresh artist index if requested or missing
    if args.refresh:
        build_artist_index()


    # ✅ Pipe output if requested
    
    if args.pipeoutput is not None:
        artist_map = load_artist_map()
        filtered = {name: info for name, info in artist_map.items() if not args.pipeoutput or args.pipeoutput.lower() in name.lower()}
        print(f"\n📁 Cached Artist Index ({len(filtered)} matches):")
        for name, info in filtered.items():
            print(f"🎨 {name} → ID: {info['id']} (Albums: {info['album_count']}, Tracks: {info['track_count']}, Last Updated: {info['last_updated']})")
        sys.exit(0)


# ✅ Load artist stats from DB instead of JSON
conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()
cursor.execute("""
    SELECT artist_id, artist_name, album_count, track_count, last_updated
    FROM artist_stats
""")
artist_stats = cursor.fetchall()
conn.close()

# Convert to dict for easy lookup
artist_map = {row[1]: {"id": row[0], "album_count": row[2], "track_count": row[3], "last_updated": row[4]} for row in artist_stats}

# ✅ If DB is empty, fallback to Navidrome API
if not artist_map:
    print("⚠️ No artist stats found in DB. Building index from Navidrome...")
    artist_map = build_artist_index()  # This should also insert into artist_stats after fetching

# ✅ Determine execution mode
if artist_list:
    print("ℹ️ Running artist-specific rating based on config.yaml...")
    for name in artist_list:
        artist_info = artist_map.get(name)
        if not artist_info:
            print(f"⚠️ No data found for '{name}', skipping.")
            continue
        if dry_run:
            print(f"👀 Dry run: would scan '{name}' (ID {artist_info['id']})")
            continue
        rated = rate_artist(artist_info['id'], name, verbose=verbose, force=force)
        print(f"✅ Completed rating for {name}. Tracks rated: {len(rated)}")

        # ✅ Update artist_stats after rating
        album_count = len(fetch_artist_albums(artist_info['id']))
        track_count = sum(len(fetch_album_tracks(a['id'])) for a in fetch_artist_albums(artist_info['id']))
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO artist_stats (artist_id, artist_name, album_count, track_count, last_updated)
            VALUES (?, ?, ?, ?, ?)
        """, (artist_info['id'], name, album_count, track_count, datetime.now().strftime("%Y-%m-%dT%H:%M:%S")))
        conn.commit()
        conn.close()

elif batchrate:
    print("ℹ️ Running full library batch rating based on DB...")
    for name, artist_info in artist_map.items():
        # ✅ Check if update is needed
        
        needs_update = True if force else (
            not artist_info['last_updated'] or
            (datetime.now() - datetime.strptime(artist_info['last_updated'], "%Y-%m-%dT%H:%M:%S")).days > 7
        )

        if not needs_update:
            print(f"⏩ Skipping '{name}' (last updated {artist_info['last_updated']})")
            continue

        if dry_run:
            print(f"👀 Dry run: would scan '{name}' (ID {artist_info['id']})")
            continue

        rated = rate_artist(artist_info['id'], name, verbose=verbose, force=force)
        print(f"✅ Completed rating for {name}. Tracks rated: {len(rated)}")

        # ✅ Update artist_stats after rating
        album_count = len(fetch_artist_albums(artist_info['id']))
        track_count = sum(len(fetch_album_tracks(a['id'])) for a in fetch_artist_albums(artist_info['id']))
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO artist_stats (artist_id, artist_name, album_count, track_count, last_updated)
            VALUES (?, ?, ?, ?, ?)
        """, (artist_info['id'], name, album_count, track_count, datetime.now().strftime("%Y-%m-%dT%H:%M:%S")))
        conn.commit()
        conn.close()
        time.sleep(1.5)

if perpetual:
    print("ℹ️ Running perpetual mode based on DB (optimized for stale artists)...")
    while True:
        # ✅ Query only artists that need updates
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT artist_id, artist_name FROM artist_stats
            WHERE last_updated IS NULL OR last_updated < DATE('now','-7 days')
        """)
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            print("✅ No artists need updating. Sleeping for 12 hours...")
            time.sleep(12 * 60 * 60)
            continue

        print(f"🔄 Starting scheduled scan for {len(rows)} stale artists...")
        for artist_id, artist_name in rows:
            print(f"🎨 Processing artist: {artist_name} (ID: {artist_id})")
            rated = rate_artist(artist_id, artist_name, verbose=verbose, force=force)
            print(f"✅ Completed rating for {artist_name}. Tracks rated: {len(rated)}")

            # ✅ Update artist stats after rating
            update_artist_stats(artist_id, artist_name)
            time.sleep(1.5)

        print("🕒 Scan complete. Sleeping for 12 hours...")
        time.sleep(12 * 60 * 60)

else:
    print("⚠️ No CLI arguments and no enabled features in config.yaml. Exiting...")
    sys.exit(0)

















