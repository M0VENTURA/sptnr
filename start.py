
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

from statistics import median

def _clean_values(values):
    """Return list of numeric values excluding None; keep zeros as informative."""
    return [v for v in values if v is not None]

def _mad(values):
    """Median Absolute Deviation (robust dispersion)."""
    vals = _clean_values(values)
    if not vals:
        return 0.0
    m = median(vals)
    return median([abs(v - m) for v in vals])

def _cv(values):
    """Coefficient of Variation (std/mean) – simple, less robust; use MAD if you prefer."""
    vals = _clean_values(values)
    if not vals:
        return 0.0
    mean = sum(vals) / len(vals)
    if mean == 0:
        return 0.0
    # A lightweight std approximation (no statistics.stdev to avoid tiny samples).
    var = sum((v - mean) ** 2 for v in vals) / max(1, (len(vals) - 1))
    std = var ** 0.5
    return std / mean

def _coverage(values):
    """Fraction of tracks with non-None values."""
    total = len(values)
    non_null = len([v for v in values if v is not None])
    return (non_null / total) if total else 0.0

def _reliability(dispersion, coverage, n_effective, disp_floor=1e-6):
    """
    Combine dispersion & coverage into a reliability score.
    - dispersion: MAD or CV (prefer MAD for robustness)
    - coverage: fraction in [0,1]
    - n_effective: non-null count, shrinks score for tiny samples
    """
    disp = max(dispersion, disp_floor)
    size_factor = min(1.0, n_effective / 8.0)  # shrink when few points
    return disp * coverage * size_factor

def compute_adaptive_weights(album_tracks, base_weights, clamp=(0.25, 1.75), use='mad'):
    """
    Compute per-album adaptive weights for spotify/lastfm/listenbrainz.
    base_weights: dict like {'spotify': 0.4, 'lastfm': 0.3, 'listenbrainz': 0.2}
    clamp: (min_factor, max_factor) relative to base weight
    use: 'mad' (robust) or 'cv' (simple)
    Returns normalized weights that sum to 1 across the three sources.
    """
    # Collect per-track raw values
    sp = [t.get('spotify_score') for t in album_tracks]
    lf = [t.get('lastfm_ratio')   for t in album_tracks]  # you’ll add this field below
    lb = [t.get('listenbrainz_score') for t in album_tracks]

    # Choose dispersion metric
    disp_fn = _mad if use == 'mad' else _cv

    # Compute metrics per source
    def metrics(vals):
        disp = disp_fn(vals)
        cov  = _coverage(vals)
        n_eff = len([v for v in vals if v is not None])
        rel = _reliability(disp, cov, n_eff)
        return disp, cov, n_eff, rel

    sp_d, sp_c, sp_n, sp_rel = metrics(sp)
    lf_d, lf_c, lf_n, lf_rel = metrics(lf)
    lb_d, lb_c, lb_n, lb_rel = metrics(lb)

    # Relative reliability as multipliers vs. mean reliability
    rels = {'spotify': sp_rel, 'lastfm': lf_rel, 'listenbrainz': lb_rel}
    mean_rel = sum(rels.values()) / max(1, len(rels))
    # If all reliabilities are ~0 (no info anywhere), fall back to base
    if mean_rel == 0:
        return base_weights.copy()

    factors = {k: (rels[k] / mean_rel) for k in rels}
    # Clamp relative factors to avoid extreme swings
    min_f, max_f = clamp
    factors = {k: min(max(factors[k], min_f), max_f) for k in factors}

    # Apply to base weights and renormalize to sum=1
    adapted = {k: base_weights.get(k, 0.0) * factors[k] for k in factors}
    total = sum(adapted.values())
    if total == 0:
        return base_weights.copy()
    adapted = {k: adapted[k] / total for k in adapted}
    return adapted

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
    """
    Check if a track is a single using Discogs API with title-aware matching.
    Requires a token in config.yaml.
    """
    if not DISCOGS_TOKEN:
        print("❌ Missing Discogs token in config.yaml.")
        return False

    headers = {
        "Authorization": f"Discogs token={DISCOGS_TOKEN}",
        "User-Agent": "sptnr-cli/1.0"
    }
    params = {
        "q": f"{artist} {title}",
        "type": "release",
        "format": "Single",
        "per_page": 5
    }

    try:
        res = requests.get("https://api.discogs.com/database/search", headers=headers, params=params, timeout=10)
        res.raise_for_status()
        results = res.json().get("results", [])
        title_norm = title.lower().strip()
        artist_norm = artist.lower().strip()

        for r in results:
            formats = r.get("format", [])
            rtitle = (r.get("title") or "").lower()
            # require 'Single' AND a reasonable title match
            if "Single" in formats and (title_norm in rtitle or rtitle.startswith(f"{artist_norm} - {title_norm}")):
                return True
        return False
    except Exception as e:
        print(f"⚠️ Discogs lookup failed for '{title}': {type(e).__name__} - {e}")
        return False


def get_discogs_genres(title, artist):
    """
    Fetch genres and styles from Discogs API.
    Always use token from config.yaml.
    """
    if not DISCOGS_TOKEN:
        logging.warning("Discogs token missing in config.yaml. Skipping Discogs genre lookup.")
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


def get_current_rating(track_id: str) -> int | None:
    """
    Fetch the current user rating (1–5) for a Navidrome track via Subsonic API.
    Returns None if not present.
    """
    url = f"{NAV_BASE_URL}/rest/getSong.view"
    params = {"u": USERNAME, "p": PASSWORD, "v": "1.16.1", "c": "sptnr", "id": track_id, "f": "json"}
    try:
        res = requests.get(url, params=params, timeout=10)
        res.raise_for_status()
        song = res.json().get("subsonic-response", {}).get("song", {})
        # OpenSubsonic typically uses userRating; some servers expose rating
        current = song.get("userRating", song.get("rating", None))
        # Navidrome stores stars 1–5; ensure type int if present
        if current is None:
            return None
        try:
            return int(current)
        except (ValueError, TypeError):
            return None
    except Exception as e:
        logging.debug(f"get_current_rating failed for track {track_id}: {e}")
        return None


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
      - Enrich per-track metadata (Spotify, Last.fm, ListenBrainz, Age, Genres)
      - Compute adaptive source weights per album (MAD/coverage-based, clamped)
      - Recompute combined score using adapted weights (+ age)
      - Strict single detection -> singles become 5★
      - Non-singles spread via Median/MAD into 1★–4★ (no 5★ for non-singles)
      - Cap density of 4★ among non-singles to keep albums realistic
      - Save to DB; update Navidrome (respecting sync/dry_run); print prior → new comparison
      - Build 5★ list for "Essential {artist}" playlist creation

    Returns:
      dict of track_id -> track_data
    """
    from statistics import median
    import math

    # --- Adaptive weighting helpers (local, robust) -------------------------
    def _clean_values(values):
        return [v for v in values if v is not None]

    def _mad(values):
        vals = _clean_values(values)
        if not vals:
            return 0.0
        m = median(vals)
        return median([abs(v - m) for v in vals])

    def _cv(values):
        vals = _clean_values(values)
        if not vals:
            return 0.0
        mean = sum(vals) / len(vals)
        if mean == 0:
            return 0.0
        var = sum((v - mean) ** 2 for v in vals) / max(1, (len(vals) - 1))
        std = var ** 0.5
        return std / mean

    def _coverage(values):
        total = len(values)
        non_null = len([v for v in values if v is not None])
        return (non_null / total) if total else 0.0

    def _reliability(dispersion, coverage, n_effective, disp_floor=1e-6):
        disp = max(dispersion, disp_floor)
        size_factor = min(1.0, n_effective / 8.0)
        return disp * coverage * size_factor

    def compute_adaptive_weights(album_tracks, base_weights, clamp=(0.75, 1.25), use='mad'):
        sp_vals = [t.get('spotify_score') for t in album_tracks]
        lf_vals = [t.get('lastfm_ratio')   for t in album_tracks]
        lb_vals = [t.get('listenbrainz_score') for t in album_tracks]

        disp_fn = _mad if use == 'mad' else _cv

        def metrics(vals):
            disp = disp_fn(vals)
            cov  = _coverage(vals)
            n_eff = len([v for v in vals if v is not None])
            rel = _reliability(disp, cov, n_eff)
            return disp, cov, n_eff, rel

        sp_d, sp_c, sp_n, sp_rel = metrics(sp_vals)
        lf_d, lf_c, lf_n, lf_rel = metrics(lf_vals)
        lb_d, lb_c, lb_n, lb_rel = metrics(lb_vals)

        rels = {'spotify': sp_rel, 'lastfm': lf_rel, 'listenbrainz': lb_rel}
        mean_rel = sum(rels.values()) / max(1, len(rels))
        if mean_rel == 0:
            return base_weights.copy()

        factors = {k: (rels[k] / mean_rel) for k in rels}
        min_f, max_f = clamp
        factors = {k: min(max(factors[k], min_f), max_f) for k in factors}

        adapted = {k: base_weights.get(k, 0.0) * factors[k] for k in factors}
        total = sum(adapted.values())
        if total == 0:
            return base_weights.copy()
        adapted = {k: adapted[k] / total for k in adapted}
        return adapted

    # --- Fetch albums -------------------------------------------------------
    albums = fetch_artist_albums(artist_id)
    if not albums:
        print(f"⚠️ No albums found for artist '{artist_name}'")
        return {}

    print(f"\n🎨 Starting rating for artist: {artist_name} ({len(albums)} albums)")
    rated_map = {}
    all_five_star_tracks = []

    for album in albums:
        album_name = album.get("name", "Unknown Album")
        album_id = album.get("id")
        tracks = fetch_album_tracks(album_id)
        if not tracks:
            print(f"⚠️ No tracks found in album '{album_name}'")
            continue

        print(f"\n🎧 Scanning album: {album_name} ({len(tracks)} tracks)")
        album_tracks = []

        # ── Per-track enrichment ─────────────────────────────────────────────
        for track in tracks:
            track_id = track["id"]
            title = track["title"]
            file_path = track.get("path", "")
            nav_genres = [track.get("genre")] if track.get("genre") else []
            mbid = track.get("mbid", None)

            if verbose:
                print(f"   🔍 Processing track: {title}")

            # Spotify lookup
            spotify_results = search_spotify_track(title, artist_name, album_name)
            selected = select_best_spotify_match(spotify_results, title)
            sp_score = selected.get("popularity", 0)
            spotify_album = selected.get("album", {}).get("name", "")
            spotify_artist = selected.get("artists", [{}])[0].get("name", "")
            spotify_genres = selected.get("artists", [{}])[0].get("genres", [])
            spotify_release_date = selected.get("album", {}).get("release_date", "")
            images = selected.get("album", {}).get("images") or []
            spotify_album_art_url = images[0].get("url", "") if images and isinstance(images[0], dict) else ""
            spotify_album_type = (selected.get("album", {}).get("album_type", "") or "").lower()
            spotify_total_tracks = selected.get("album", {}).get("total_tracks", 0)
            is_spotify_single = (spotify_album_type == "single")

            # Last.fm
            lf_data = get_lastfm_track_info(artist_name, title)
            lf_track_play = lf_data.get("track_play", 0)
            lf_artist_play = lf_data.get("artist_play", 0)
            lf_ratio = round((lf_track_play / lf_artist_play) * 100, 2) if lf_artist_play > 0 else 0

            # Core score components
            score, momentum, lb_score = compute_track_score(
                title, artist_name, spotify_release_date or "1992-01-01", sp_score, mbid, verbose
            )

            # Genres
            discogs_genres = get_discogs_genres(title, artist_name)
            audiodb_genres = get_audiodb_genres(artist_name) if use_audiodb and AUDIODB_API_KEY else []
            mb_genres = get_musicbrainz_genres(title, artist_name)
            lastfm_tags = []  # populate if you fetch Last.fm tags

            online_top, _ = get_top_genres_with_navidrome(
                {
                    "spotify": spotify_genres,
                    "lastfm": lastfm_tags,
                    "discogs": discogs_genres,
                    "audiodb": audiodb_genres,
                    "musicbrainz": mb_genres,
                },
                nav_genres,
                title=title,
                album=album_name,
            )
            genre_context = "metal" if any("metal" in g.lower() for g in online_top) else ""
            top_genres = adjust_genres(online_top, artist_is_metal=(genre_context == "metal"))

            album_tracks.append({
                "id": track_id,
                "title": title,
                "album": album_name,
                "artist": artist_name,

                # combined score (will be updated after adaptive weighting)
                "score": score,

                # components for adaptive weighting
                "spotify_score": sp_score,
                "lastfm_ratio": lf_ratio,
                "lastfm_score": lf_ratio,
                "listenbrainz_score": lb_score,
                "age_score": momentum,

                # metadata & genres
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

                # single evidence
                "spotify_album_type": spotify_album_type,
                "spotify_total_tracks": spotify_total_tracks,
                "is_spotify_single": is_spotify_single,

                # placeholders
                "is_single": False,
                "single_confidence": "low",
                "single_sources": [],
                "stars": 1,
            })

        # --- Adaptive weights per album & recompute combined score -----------
        base_weights = {
            'spotify': SPOTIFY_WEIGHT,
            'lastfm': LASTFM_WEIGHT,
            'listenbrainz': LISTENBRAINZ_WEIGHT,
        }
        adaptive = compute_adaptive_weights(album_tracks, base_weights=base_weights, clamp=(0.75, 1.25), use='mad')

        for t in album_tracks:
            sp = t.get('spotify_score', 0)
            lf = t.get('lastfm_ratio', 0)
            lb = t.get('listenbrainz_score', 0)
            age = t.get('age_score', 0)
            t['score'] = (adaptive['spotify'] * sp) + \
                         (adaptive['lastfm'] * lf) + \
                         (adaptive['listenbrainz'] * lb) + \
                         (AGE_WEIGHT * age)

        # --- Strict singles detection (canonical only) -----------------------
        for trk in album_tracks:
            title = trk["title"]
            canonical = is_valid_version(title, allow_live_remix=False)

            spotify_source = bool(trk.get("is_spotify_single"))          # album_type == "single"
            discogs_source = is_discogs_single(title, artist_name)       # consider tightening to title-aware matching
            short_release_source = (trk.get("spotify_total_tracks", 99) <= 2)

            sources = []
            if spotify_source:
                sources.append("spotify")
            if discogs_source:
                sources.append("discogs")
            if short_release_source:
                sources.append("short_release")

            confidence = (
                "high" if len(sources) >= 2 else
                "medium" if len(sources) == 1 else
                "low"
            )

            if canonical and (
                len(sources) >= 2 or
                (spotify_source and short_release_source)
            ):
                trk["is_single"] = True
                trk["single_confidence"] = confidence
                trk["single_sources"] = sources
            elif sources and not canonical:
                trk["is_single"] = False
                trk["single_confidence"] = "ignored-noncanonical"
                trk["single_sources"] = sources
            else:
                trk["is_single"] = False
                trk["single_confidence"] = confidence
                trk["single_sources"] = sources

        # --- Sort by score and normalize WITHOUT random bump -----------------
        sorted_album = sorted(album_tracks, key=lambda x: x["score"], reverse=True)
        for trk in sorted_album:
            trk["score"] = max(0.0, float(trk["score"]))  # clamp to non-negative, no artificial inflation

        # --- Median/MAD spreading for NON‑SINGLES (1★–4★ only) --------------
        EPS = 1e-6
        scores_all = [t["score"] for t in sorted_album]
        med = median(scores_all)

        def mad(vals):
            m = median(vals)
            return median([abs(v - m) for v in vals])

        mad_val = max(mad(scores_all), EPS)

        def zrobust(x, m=med, s=mad_val):
            return (x - m) / s

        non_single_tracks = [t for t in sorted_album if not t.get("is_single")]
        BANDS = [
            (-float("inf"), -1.0, 1),   # far below median -> 1★
            (-1.0,          -0.3, 2),   # below median     -> 2★
            (-0.3,           0.6, 3),   # around median    -> 3★
            (0.6,  float("inf"), 4),    # above median     -> 4★
        ]

        z_list = []
        for t in non_single_tracks:
            z = zrobust(t["score"])
            z_list.append((t, z))
            for lo, hi, stars in BANDS:
                if lo <= z < hi:
                    t["stars"] = stars
                    break

        # Cap 4★ density among non‑singles (default ~25%)
        CAP_TOP4_PCT = 0.25
        top4 = [t for (t, z) in z_list if t.get("stars") == 4]
        max_top4 = max(1, round(len(non_single_tracks) * CAP_TOP4_PCT))
        if len(top4) > max_top4:
            top4_sorted = sorted(
                [(t, zrobust(t["score"])) for t in top4],
                key=lambda x: x[1],
                reverse=True
            )
            for t, _ in top4_sorted[max_top4:]:
                t["stars"] = 3

        # --- Singles override to 5★ -----------------------------------------
        single_count = 0
        non_single_fours = sum(1 for t in non_single_tracks if t.get("stars") == 4)
        for trk in sorted_album:
            if trk.get("is_single"):
                trk["stars"] = 5
                single_count += 1

        # --- Finalize, persist, and print prior → new comparison ------------
        for trk in sorted_album:
            prior_stars = get_current_rating(trk["id"])

            # Save to DB
            save_to_db(trk)

            # Set rating only when allowed
            if sync and not dry_run:
                set_track_rating(trk["id"], trk["stars"])
                action_prefix = "✅ Navidrome rating updated:"
            else:
                action_prefix = "🧪 DRY-RUN (no push):"

            single_label = " (single)" if trk.get("is_single") else ""
            src = trk.get("single_sources", [])
            src_str = f" [sources: {', '.join(src)}]" if trk.get("is_single") and src else ""

            title = trk["title"]
            if prior_stars is None:
                print(f"   {action_prefix} {title}{single_label}{src_str} → {trk['stars']}★")
            else:
                print(f"   {action_prefix} {title}{single_label}{src_str} — {prior_stars}★ → {trk['stars']}★")

            if trk["stars"] == 5:
                all_five_star_tracks.append(trk["id"])

        # Per‑album summary
        print(f"   ℹ️ Singles detected: {single_count} | Non‑single 4★: {non_single_fours} "
              f"| Cap: 25% | MAD: {mad_val:.2f} | Weights clamp: (0.75, 1.25)")

        print(f"✔ Completed album: {album_name}")

        # Record in rated_map
        for trk in sorted_album:
            rated_map[trk["id"]] = trk

    # --- Essential playlist (post-artist) -----------------------------------
    all_five_star_tracks = list(dict.fromkeys(all_five_star_tracks))  # dedupe
    if artist_name.lower() != "various artists" and len(all_five_star_tracks) >= 10 and sync and not dry_run:
        playlist_name = f"Essential {artist_name}"
        create_playlist(playlist_name, all_five_star_tracks)
        print(f"🎶 Essential playlist created: {playlist_name} with {len(all_five_star_tracks)} tracks")
    else:
        print(f"ℹ️ No Essential playlist created for {artist_name} (5★ tracks: {len(all_five_star_tracks)})")

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

artist_map = {
    row[1]: {
        "id": row[0],
        "album_count": row[2],
        "track_count": row[3],
        "last_updated": row[4],
    }
    for row in artist_stats
}


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

        # ✅ If force is enabled, clear cached data for this artist
        if force:
            print(f"⚠️ Force enabled: clearing cached data for artist '{name}'...")
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM tracks WHERE artist = ?", (name,))
            cursor.execute("DELETE FROM artist_stats WHERE artist_name = ?", (name,))
            conn.commit()
            conn.close()
            print(f"✅ Cache cleared for artist '{name}'")

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


# ✅ If force is enabled for batch mode, clear entire cache before scanning
if force and batchrate:
    print("⚠️ Force enabled: clearing entire cached library...")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM tracks")
    cursor.execute("DELETE FROM artist_stats")
    conn.commit()
    conn.close()
    print("✅ Entire cache cleared. Starting fresh...")

    print("ℹ️ Rebuilding artist index from Navidrome after force clear...")
    build_artist_index()

# 🔧 Always run batch rating when requested (even if force just ran)
if batchrate:
    print("ℹ️ Running full library batch rating based on DB...")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT artist_id, artist_name, album_count, track_count, last_updated
        FROM artist_stats
    """)
    artist_stats = cursor.fetchall()
    conn.close()

    
    artist_map = {
        row[1]: {
            "id": row[0],
            "album_count": row[2],
            "track_count": row[3],
            "last_updated": row[4],
        }
        for row in artist_stats
    }


    if not artist_map:
        print("⚠️ Artist index is empty; rebuilding once more...")
        build_artist_index()
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT artist_id, artist_name, album_count, track_count, last_updated
            FROM artist_stats
        """)
        artist_stats = cursor.fetchall()
        conn.close()
        artist_map = {
            row[1]: {
                "id": row[0],
                "album_count": row[2],
                "track_count": row[3],
                "last_updated": row[4],
            }
            for row in artist_stats
        }

    if not artist_map:
        print("❌ No artists found after rebuild. Aborting batch rating.")
    else:
        for name, artist_info in artist_map.items():
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

# ♻️ Perpetual mode with self-healing index
if perpetual:
    print("ℹ️ Running perpetual mode based on DB (optimized for stale artists)...")
    while True:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT artist_id, artist_name FROM artist_stats
            WHERE last_updated IS NULL OR last_updated < DATE('now','-7 days')
        """)
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM artist_stats")
            total_artists = cursor.fetchone()[0]
            conn.close()

            if total_artists == 0:
                print("⚠️ No artists found in DB; rebuilding index from Navidrome...")
                build_artist_index()
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

            update_artist_stats(artist_id, artist_name)
            time.sleep(1.5)

        print("🕒 Scan complete. Sleeping for 12 hours...")
        time.sleep(12 * 60 * 60)

else:
    print("⚠️ No CLI arguments and no enabled features in config.yaml. Exiting...")
    sys.exit(0)






