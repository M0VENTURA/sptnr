
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
        print(f"⚠️ Config file missing at {CONFIG_PATH}. Creating default...")
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

    # Report issues
    if issues:
        print("\n⚠️ Configuration issues detected:")
        for issue in issues:
            print(f" - {issue}")
        print("\n❌ Please update config.yaml before running the script.")
        sys.exit(1)

# ✅ Call this right after loading config
validate_config(config)

# ✅ Validate that there is work to do
if not artist_list and not batchrate and not perpetual:
    print("⚠️ No artist specified and batchrate/perpetual not enabled. Nothing to do.")
    sys.exit(0)

# ✅ Extract credentials and settings
NAV_BASE_URL = config["navidrome"]["base_url"]
USERNAME = config["navidrome"]["user"]
PASSWORD = config["navidrome"]["pass"]

client_id = config["spotify"]["client_id"]
client_secret = config["spotify"]["client_secret"]
LASTFM_API_KEY = config["lastfm"]["api_key"]

SPOTIFY_WEIGHT = config["weights"]["spotify"]
LASTFM_WEIGHT = config["weights"]["lastfm"]
LISTENBRAINZ_WEIGHT = config["weights"]["listenbrainz"]
AGE_WEIGHT = config["weights"]["age"]

DB_PATH = config["database"]["path"]


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

# ✅ Cache paths
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)
INDEX_FILE = os.path.join(DATA_DIR, "artist_index.json")
RATING_CACHE_FILE = os.path.join(DATA_DIR, "rating_cache.json")
SINGLE_CACHE_FILE = os.path.join(DATA_DIR, "single_cache.json")

for path in [RATING_CACHE_FILE, SINGLE_CACHE_FILE, INDEX_FILE]:
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}")

# ✅ Ensure database directory exists
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# ✅ SQLite DB setup
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tracks (
        id TEXT PRIMARY KEY,
        artist TEXT,
        album TEXT,
        title TEXT,
        spotify_score REAL,
        lastfm_score REAL,
        listenbrainz_score REAL,
        age_score REAL,
        final_score REAL,
        stars INTEGER,
        genres TEXT,
        is_single BOOLEAN,
        single_confidence TEXT,
        last_scanned TEXT
    );
    """)
    conn.commit()
    conn.close()

def save_to_db(track_data):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
    INSERT OR REPLACE INTO tracks (
        id, artist, album, title, spotify_score, lastfm_score, listenbrainz_score,
        age_score, final_score, stars, genres, is_single, single_confidence, last_scanned
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        track_data["id"], track_data["artist"], track_data["album"], track_data["title"],
        track_data.get("spotify_score", 0), track_data.get("lastfm_score", 0),
        track_data.get("listenbrainz_score", 0), track_data.get("age_score", 0),
        track_data["score"], track_data["stars"], ",".join(track_data["genres"]),
        track_data["is_single"], track_data["single_confidence"], track_data["last_scanned"]
    ))
    conn.commit()
    conn.close()

init_db()

# --- Spotify API Helpers ---
import requests
import base64
import difflib

def get_spotify_token():
    """Retrieve Spotify API token using client credentials."""
    auth_str = f"{client_id}:{client_secret}"
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

# --- Scoring Logic ---
def compute_track_score(title, artist_name, release_date, sp_score, mbid=None, verbose=False):
    """Compute weighted score for a track using Spotify, Last.fm, ListenBrainz, and age decay."""
    lf_data = get_lastfm_track_info(artist_name, title)
    lf_track = lf_data["track_play"] if lf_data else 0
    lf_artist = lf_data["artist_play"] if lf_data else 0
    lf_ratio = round((lf_track / lf_artist) * 100, 2) if lf_artist > 0 else 0
    momentum, days_since = score_by_age(lf_track, release_date)
    lb_score = 0  # ListenBrainz integration placeholder

    score = (SPOTIFY_WEIGHT * sp_score) + \
            (LASTFM_WEIGHT * lf_ratio) + \
            (LISTENBRAINZ_WEIGHT * lb_score) + \
            (AGE_WEIGHT * momentum)

    if verbose:
        print(f"🔢 Raw score for '{title}': {round(score)} "
              f"(Spotify: {sp_score}, Last.fm: {lf_ratio}, Age: {momentum})")

    return score, days_since

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
        artist_map = {a["name"]: a["id"] for group in index for a in group.get("artist", [])}
        with open(INDEX_FILE, "w") as f:
            json.dump(artist_map, f, indent=2)
        logging.info(f"✅ Cached {len(artist_map)} artists to {INDEX_FILE}")
        return artist_map
    except Exception as e:
        logging.error(f"❌ Failed to build artist index: {e}")
        return {}

# --- Main Rating Logic ---

def rate_artist(artist_id, artist_name, verbose=False, force=False):
    """
    Rate all tracks for a given artist:
    - Compute scores
    - Assign stars
    - Save to DB
    - Update Navidrome genres and ratings
    - Create playlist for top tracks
    """
    albums = fetch_artist_albums(artist_id)
    if not albums:
        print(f"⚠️ No albums found for artist '{artist_name}'")
        return {}

    rated_map = {}
    playlist_tracks = []

    for album in albums:
        album_name = album.get("name", "Unknown Album")
        album_id = album.get("id")
        tracks = fetch_album_tracks(album_id)
        if not tracks:
            continue

        print(f"\n🎧 Processing album: {album_name}")
        album_tracks = []

        for track in tracks:
            track_id = track["id"]
            title = track["title"]
            nav_genres = [track.get("genre")] if track.get("genre") else []

            # Spotify lookup
            spotify_results = search_spotify_track(title, artist_name, album_name)
            selected = select_best_spotify_match(spotify_results, title)
            sp_score = selected.get("popularity", 0)
            release_date = selected.get("album", {}).get("release_date") or "1992-01-01"

            # Compute score
            score, _ = compute_track_score(title, artist_name, release_date, sp_score, verbose=verbose)

            # Genre aggregation
            spotify_genres = selected.get("artists", [{}])[0].get("genres", [])
            lastfm_tags = []  # Simplified for now
            discogs_genres = []
            audiodb_genres = []
            mb_genres = []
            top_genres, _ = get_top_genres_with_navidrome({
                "spotify": spotify_genres,
                "lastfm": lastfm_tags,
                "discogs": discogs_genres,
                "audiodb": audiodb_genres,
                "musicbrainz": mb_genres
            }, nav_genres, title=title, album=album_name)

            # Build track object
            album_tracks.append({
                "id": track_id,
                "title": title,
                "album": album_name,
                "artist": artist_name,
                "score": score,
                "spotify_score": sp_score,
                "genres": top_genres,
                "last_scanned": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            })

        # Assign stars based on score distribution
        sorted_album = sorted(album_tracks, key=lambda x: x["score"], reverse=True)
        band_size = max(1, math.ceil(len(sorted_album) / 4))
        median_score = median(track["score"] for track in sorted_album) or 10
        jump_threshold = median_score * 1.7

        for i, track in enumerate(sorted_album):
            stars = max(1, 4 - (i // band_size))
            if track["score"] >= jump_threshold:
                stars = 5

            track["stars"] = stars
            track["score"] = round(track["score"]) if track["score"] > 0 else random.randint(5, 15)

            # Save to DB
            save_to_db(track)

            # Update Navidrome rating and genres
            set_track_rating(track["id"], stars)

            # Add top-rated tracks to playlist
            if stars >= 4:
                playlist_tracks.append(track["id"])

            rated_map[track["id"]] = track

    # Create playlist for artist's top tracks
    if playlist_tracks:
        playlist_name = f"Top Tracks - {artist_name}"
        create_playlist(playlist_name, playlist_tracks)

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

    # ✅ Refresh artist index if requested or missing
    if args.refresh or not os.path.exists(INDEX_FILE):
        build_artist_index()

    # ✅ Pipe output if requested
    if args.pipeoutput is not None:
        with open(INDEX_FILE) as f:
            artist_map = json.load(f)
        filtered = {name: aid for name, aid in artist_map.items() if not args.pipeoutput or args.pipeoutput.lower() in name.lower()}
        print(f"\n📁 Cached Artist Index ({len(filtered)} matches):")
        for name, aid in filtered.items():
            print(f"🎨 {name} → ID: {aid}")
        sys.exit(0)

    # ✅ Load artist index
    artist_index = json.load(open(INDEX_FILE))

    # ✅ Determine execution mode
    if artist_list:
        print("ℹ️ Running artist-specific rating based on config.yaml...")
        for name in artist_list:
            artist_id = artist_index.get(name)
            if not artist_id:
                print(f"⚠️ No ID found for '{name}', skipping.")
                continue
            if dry_run:
                print(f"👀 Dry run: would scan '{name}' (ID {artist_id})")
                continue
            rated = rate_artist(artist_id, name, verbose=verbose, force=force)
            print(f"✅ Completed rating for {name}. Tracks rated: {len(rated)}")

    elif batchrate:
        print("ℹ️ Running full library batch rating based on config.yaml...")
        for name, artist_id in artist_index.items():
            if dry_run:
                print(f"👀 Dry run: would scan '{name}' (ID {artist_id})")
                continue
            rated = rate_artist(artist_id, name, verbose=verbose, force=force)
            print(f"✅ Completed rating for {name}. Tracks rated: {len(rated)}")
            time.sleep(1.5)

    elif perpetual:
        print("ℹ️ Running perpetual mode based on config.yaml...")
        while True:
            print("🔄 Starting scheduled scan...")
            for name, artist_id in artist_index.items():
                rated = rate_artist(artist_id, name, verbose=verbose, force=force)
                print(f"✅ Completed rating for {name}. Tracks rated: {len(rated)}")
                time.sleep(1.5)
            print("🕒 Scan complete. Sleeping for 12 hours...")
            time.sleep(12 * 60 * 60)

    else:
        print("⚠️ No CLI arguments and no enabled features in config.yaml. Exiting...")
        sys.exit(0)

