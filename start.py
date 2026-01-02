#!/usr/bin/env python3
# ðŸŽ§ SPTNR â€“ Navidrome Rating CLI with Spotify + Last.fm + Navidrome API Integration

import argparse
import os
import sys
import time
import logging
import base64
import re
import sqlite3
import math
import json
import threading
import difflib
import unicodedata
import requests
import yaml
from colorama import init, Fore, Style
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from helpers import strip_parentheses, create_retry_session
from datetime import datetime, timedelta
from statistics import median
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

# âœ… Import modular API clients
from api_clients.navidrome import NavidromeClient
from api_clients.spotify import SpotifyClient
from api_clients.lastfm import LastFmClient
from api_clients.musicbrainz import MusicBrainzClient
from api_clients.discogs import DiscogsClient
from api_clients.audiodb_and_listenbrainz import ListenBrainzClient, AudioDbClient

# ðŸŽ¨ Colorama setup
init(autoreset=True)
LIGHT_RED = Fore.RED + Style.BRIGHT
LIGHT_GREEN = Fore.GREEN + Style.BRIGHT
LIGHT_BLUE = Fore.BLUE + Style.BRIGHT
LIGHT_YELLOW = Fore.YELLOW + Style.BRIGHT
LIGHT_CYAN = Fore.CYAN + Style.BRIGHT
RESET = Style.RESET_ALL

# âœ… Load config.yaml

CONFIG_PATH = "/config/config.yaml"

def create_default_config(path):
    default_config = {
        "navidrome": {
            "base_url": "http://localhost:4533",
            "user": "admin",
            "pass": "password"
        },
        "api_integrations": {
            "spotify": {
                "enabled": True,
                "client_id": "your_spotify_client_id",
                "client_secret": "your_spotify_client_secret"
            },
            "lastfm": {
                "enabled": True,
                "api_key": "your_lastfm_api_key"
            },
            "listenbrainz": {
                "enabled": True
            },
            "discogs": {
                "enabled": True,
                "token": "your_discogs_token"
            },
            "musicbrainz": {
                "enabled": True
            },
            "audiodb": {
                "enabled": False,
                "api_key": ""
            },
            "google": {
                "enabled": False,
                "api_key": "",
                "cse_id": ""
            },
            "youtube": {
                "enabled": False,
                "api_key": ""
            }
        },
        "weights": {
            "spotify": 0.4,
            "lastfm": 0.3,
            "listenbrainz": 0.2,
            "age": 0.1
        },
        "database": {
            "path": "/database/sptnr.db",
            "vacuum_on_start": False
        },
        "logging": {
            "level": "INFO",
            "file": "/config/app.log",
            "console": True
        },
        "features": {
            "dry_run": False,
            "sync": True,
            "force": False,
            "verbose": False,
            "perpetual": False,
            "batchrate": False,
            "refresh_playlists_on_start": False,
            "refresh_artist_index_on_start": False,
            "discogs_min_interval_sec": 0.35,
            "album_skip_days": 7,
            "album_skip_min_tracks": 1,
            "clamp_min": 0.75,
            "clamp_max": 1.25,
            "cap_top4_pct": 0.25,
            "title_sim_threshold": 0.92,
            "short_release_counts_as_match": False,
            "secondary_single_lookup_enabled": True,
            "secondary_lookup_metric": "score",
            "secondary_lookup_delta": 0.05,
            "secondary_required_strong_sources": 2,
            "median_gate_strategy": "hard",
            "use_lastfm_single": True,
            "include_user_ratings_on_scan": True,
            "scan_worker_threads": 4,
            "spotify_prefetch_timeout": 30,
            "artist": []
        }
    }

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            yaml.safe_dump(default_config, f)
        print(f"âœ… Default config.yaml created at {path}")
    except Exception as e:
        print(f"âŒ Failed to create default config.yaml: {e}")
        sys.exit(1)

def load_config():
    if not os.path.exists(CONFIG_PATH):
        print(f"âš ï¸ Config file not found at {CONFIG_PATH}.")
        # Try to copy from built-in template in /app/config/config.yaml
        template_path = "/app/config/config.yaml"
        if os.path.exists(template_path):
            print(f"ðŸ“‹ Copying default config from {template_path}...")
            import shutil
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            shutil.copy2(template_path, CONFIG_PATH)
            print(f"âœ… Default config copied to {CONFIG_PATH}")
        else:
            print(f"âš ï¸ No template found. Creating default config...")
            create_default_config(CONFIG_PATH)
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f)

config = load_config()

# âœ… Create persistent HTTP session with connection pooling & retry strategy
session = create_retry_session(
    retries=3,
    backoff=0.3,
    status_forcelist=(429, 500, 502, 503, 504)
)

# âœ… Merge defaults with existing config to avoid KeyErrors
default_features = {
    "dry_run": False,
    "sync": True,
    "force": False,
    "verbose": False,
    "perpetual": False,
    "batchrate": False,
    "refresh_playlists_on_start": False,
    "refresh_artist_index_on_start": False,
    "discogs_min_interval_sec": 0.35,
    "album_skip_days": 7,
    "album_skip_min_tracks": 1,
    "clamp_min": 0.75,
    "clamp_max": 1.25,
    "cap_top4_pct": 0.25,
    "title_sim_threshold": 0.92,
    "short_release_counts_as_match": False,
    "secondary_single_lookup_enabled": True,
    "secondary_lookup_metric": "score",
    "secondary_lookup_delta": 0.05,
    "secondary_required_strong_sources": 2,
    "median_gate_strategy": "hard",
    "use_lastfm_single": True,
    "use_audiodb": False,
    "include_user_ratings_on_scan": True,
    "scan_worker_threads": 4,
    "spotify_prefetch_timeout": 30,
    "artist": []
}

config.setdefault("features", {})
config["features"] = {**default_features, **config["features"]}  # existing values win

# Context gate toggles (strict by default)
CONTEXT_GATE = bool(config["features"].get("single_context_gate_live", True))
CONTEXT_FALLBACK_STUDIO = bool(config["features"].get("single_context_fallback_studio", False))

# âœ… Extract feature flags
dry_run = config["features"]["dry_run"]
sync = config["features"]["sync"]
force = config["features"]["force"]
verbose = config["features"]["verbose"]
perpetual = config["features"]["perpetual"]
batchrate = config["features"]["batchrate"]
artist_list = config["features"]["artist"]
WORKER_THREADS = int(config["features"].get("scan_worker_threads", 4))
SPOTIFY_PREFETCH_TIMEOUT = int(config["features"].get("spotify_prefetch_timeout", 30))

def get_primary_nav_user(cfg: dict) -> dict | None:
    """
    Return the first Navidrome user entry if 'navidrome_users' is present,
    otherwise return the single-user dict under 'navidrome'. If neither exists, None.
    """
    if isinstance(cfg.get("navidrome_users"), list) and cfg["navidrome_users"]:
        return cfg["navidrome_users"][0]
    if isinstance(cfg.get("navidrome"), dict):
        return cfg["navidrome"]
    return None

def validate_config(config):
    issues = []

    # --- Navidrome credentials (support single or multi) ---
    primary = get_primary_nav_user(config)
    if not primary:
        issues.append("No Navidrome credentials found. Provide either 'navidrome' or 'navidrome_users'.")

    else:
        if primary.get("user") in ["admin", "", None]:
            issues.append("Navidrome username is not set (currently 'admin').")
        if primary.get("pass") in ["password", "", None]:
            issues.append("Navidrome password is not set (currently 'password').")
        if not primary.get("base_url"):
            issues.append("Navidrome base_url is missing.")

    # --- API Integrations (support both old and new structure) ---
    api = config.get("api_integrations", {})
    
    # Backward compatibility
    if not api:
        api = {
            "spotify": config.get("spotify", {}),
            "lastfm": config.get("lastfm", {}),
        }
    
    # --- Spotify (required) ---
    spotify = api.get("spotify", {})
    if spotify.get("enabled", True):  # Default to enabled if not specified
        if spotify.get("client_id") in ["your_spotify_client_id", "", None]:
            issues.append("Spotify Client ID is missing or placeholder (required).")
        if spotify.get("client_secret") in ["your_spotify_client_secret", "", None]:
            issues.append("Spotify Client Secret is missing or placeholder (required).")
    
    # --- Last.fm (required) ---
    lastfm = api.get("lastfm", {})
    if lastfm.get("enabled", True):  # Default to enabled if not specified
        if lastfm.get("api_key") in ["your_lastfm_api_key", "", None]:
            issues.append("Last.fm API key is missing or placeholder (required).")
    
    # --- Discogs (optional but warn if enabled without token) ---
    discogs = api.get("discogs", {})
    if discogs.get("enabled", True):
        if discogs.get("token") in ["your_discogs_token", "", None]:
            issues.append("Discogs is enabled but token is missing or placeholder. Single detection may be limited.")

    if issues:
        print("\nâš ï¸ Configuration issues detected:")
        for issue in issues:
            print(f" - {issue}")

        print("\nâŒ Please update config.yaml before continuing.")
        print("ðŸ‘‰ To edit the file inside the container, run:")
        print("   vi /config/config.yaml")
        print("âœ… After saving changes, restart the container")
        # Keep container alive and wait for user action
        print("â¸ Waiting for config update... Container will stay alive. Please restart the container after editing the config.")
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            print("\nâ„¹ï¸ Exiting script.")
            sys.exit(0)

# âœ… Call this right after loading config
validate_config(config)

# âœ… Extract credentials and settings
NAV_USERS = config.get("navidrome_users", [])

_primary_user = get_primary_nav_user(config) or {"base_url": "", "user": "", "pass": ""}

NAV_BASE_URL = _primary_user.get("base_url", "")
USERNAME     = _primary_user.get("user", "")
PASSWORD     = _primary_user.get("pass", "")

# âœ… API Integrations - Support both old and new structure
api = config.get("api_integrations", {})

# Backward compatibility: if api_integrations doesn't exist, use old structure
if not api:
    api = {
        "spotify": config.get("spotify", {}),
        "lastfm": config.get("lastfm", {}),
        "listenbrainz": config.get("listenbrainz", {"enabled": True}),
        "discogs": config.get("discogs", {}),
        "audiodb": config.get("audiodb", {}),
        "google": config.get("google", {}),
        "youtube": config.get("youtube", {}),
        "musicbrainz": {"enabled": True}
    }

# Extract enabled flags and credentials
SPOTIFY_ENABLED = api.get("spotify", {}).get("enabled", True)
SPOTIFY_CLIENT_ID = api.get("spotify", {}).get("client_id", "")
SPOTIFY_CLIENT_SECRET = api.get("spotify", {}).get("client_secret", "")

LASTFM_ENABLED = api.get("lastfm", {}).get("enabled", True)
LASTFM_API_KEY = api.get("lastfm", {}).get("api_key", "")

LISTENBRAINZ_ENABLED = api.get("listenbrainz", {}).get("enabled", True)

DISCOGS_ENABLED = api.get("discogs", {}).get("enabled", True)
DISCOGS_TOKEN = api.get("discogs", {}).get("token", "")

MUSICBRAINZ_ENABLED = api.get("musicbrainz", {}).get("enabled", True)

AUDIODB_ENABLED = api.get("audiodb", {}).get("enabled", False)
AUDIODB_API_KEY = api.get("audiodb", {}).get("api_key", "") if AUDIODB_ENABLED else ""

GOOGLE_ENABLED = api.get("google", {}).get("enabled", False)
GOOGLE_API_KEY = api.get("google", {}).get("api_key", "") if GOOGLE_ENABLED else ""
GOOGLE_CSE_ID = api.get("google", {}).get("cse_id", "") if GOOGLE_ENABLED else ""

YOUTUBE_ENABLED = api.get("youtube", {}).get("enabled", False)
YOUTUBE_API_KEY = api.get("youtube", {}).get("api_key", "") if YOUTUBE_ENABLED else ""

# Weights
SPOTIFY_WEIGHT = config["weights"]["spotify"]
LASTFM_WEIGHT = config["weights"]["lastfm"]
LISTENBRAINZ_WEIGHT = config["weights"]["listenbrainz"]
AGE_WEIGHT = config["weights"]["age"]

# Database path
DB_PATH = config["database"]["path"]


# âœ… Ensure database directory exists
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# âœ… Import schema updater and update DB schema
from check_db import update_schema
update_schema(DB_PATH)

# âœ… Initialize API clients with credentials
nav_client = NavidromeClient(NAV_BASE_URL, USERNAME, PASSWORD)
spotify_client = SpotifyClient(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, worker_threads=WORKER_THREADS)
lastfm_client = LastFmClient(LASTFM_API_KEY)
musicbrainz_client = MusicBrainzClient(enabled=MUSICBRAINZ_ENABLED)
discogs_client = DiscogsClient(DISCOGS_TOKEN, enabled=DISCOGS_ENABLED)
audiodb_client = AudioDbClient(AUDIODB_API_KEY, enabled=AUDIODB_ENABLED)
listenbrainz_client = ListenBrainzClient(enabled=LISTENBRAINZ_ENABLED)

# âœ… Compatibility check for OpenSubsonic extensions
def get_supported_extensions():
    url = f"{NAV_BASE_URL}/rest/getOpenSubsonicExtensions.view"
    params = {"u": USERNAME, "p": PASSWORD, "v": "1.16.1", "c": "sptnr", "f": "json"}
    try:
        res = session.get(url, params=params)
        res.raise_for_status()
        extensions = res.json().get("subsonic-response", {}).get("openSubsonicExtensions", [])
        print(f"âœ… Supported extensions: {extensions}")
        return extensions
    except Exception as e:
        print(f"âš ï¸ Failed to fetch extensions: {e}")
        return []

SUPPORTED_EXTENSIONS = get_supported_extensions()

# âœ… Decide feature usage
USE_FORMPOST = "formPost" in SUPPORTED_EXTENSIONS
USE_SEARCH3 = "search3" in SUPPORTED_EXTENSIONS


# âœ… Logging setup
logging.basicConfig(
    level=getattr(logging, config["logging"]["level"].upper(), logging.INFO),
    filename=config["logging"]["file"],
    format="%(asctime)s [%(levelname)s] %(message)s"
)


# âœ… Database connection helper with WAL mode and increased timeout
def get_db_connection():
    """
    Create a database connection with WAL mode for better concurrency.
    WAL mode allows multiple readers and one writer simultaneously.
    """
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def save_to_db(track_data):
    conn = get_db_connection()
    cursor = conn.cursor()

    # Prepare multi-value fields
    genres             = ",".join(track_data.get("genres", []))
    navidrome_genres   = ",".join(track_data.get("navidrome_genres", []))
    spotify_genres     = ",".join(track_data.get("spotify_genres", []))
    lastfm_tags        = ",".join(track_data.get("lastfm_tags", []))
    discogs_genres     = ",".join(track_data.get("discogs_genres", []) or [])
    audiodb_genres     = ",".join(track_data.get("audiodb_genres", []) or [])
    musicbrainz_genres = ",".join(track_data.get("musicbrainz_genres", []) or [])
    single_sources_json = json.dumps(track_data.get("single_sources", []), ensure_ascii=False)

    columns = [
        "id","artist","album","title",
        "spotify_score","lastfm_score","listenbrainz_score","age_score","final_score","stars",
        "genres","navidrome_genres","spotify_genres","lastfm_tags",
        "discogs_genres","audiodb_genres","musicbrainz_genres",
        "spotify_album","spotify_artist","spotify_popularity","spotify_release_date","spotify_album_art_url",
        "lastfm_track_playcount","lastfm_artist_playcount","file_path",
        "is_single","single_confidence","last_scanned",
        "mbid","suggested_mbid","suggested_mbid_confidence","single_sources",
        "is_spotify_single","spotify_total_tracks","spotify_album_type",
        "navidrome_rating","lastfm_ratio",
        # âœ… Audit and scoring context fields
        "discogs_single_confirmed","discogs_video_found","is_canonical_title","title_similarity_to_base",
        "album_context_live","adaptive_weight_spotify","adaptive_weight_lastfm","adaptive_weight_listenbrainz",
        "album_median_score","spotify_release_age_days",
    ]

    values = [
        track_data["id"],
        track_data.get("artist",""),
        track_data.get("album",""),
        track_data.get("title",""),
        float(track_data.get("spotify_score",0) or 0),
        float(track_data.get("lastfm_score",0) or 0),
        float(track_data.get("listenbrainz_score",0) or 0),
        float(track_data.get("age_score",0) or 0),
        float(track_data.get("score",0) or 0),  # final_score
        int(track_data.get("stars",0) or 0),
        genres, navidrome_genres, spotify_genres, lastfm_tags,
        discogs_genres, audiodb_genres, musicbrainz_genres,
        track_data.get("spotify_album",""),
        track_data.get("spotify_artist",""),
        int(track_data.get("spotify_popularity",0) or 0),
        track_data.get("spotify_release_date",""),
        track_data.get("spotify_album_art_url",""),
        int(track_data.get("lastfm_track_playcount",0) or 0),
        int(track_data.get("lastfm_artist_playcount",0) or 0),
        track_data.get("file_path",""),
        int(bool(track_data.get("is_single",False))),
        track_data.get("single_confidence",""),
        track_data.get("last_scanned",""),
        track_data.get("mbid","") or "",
        track_data.get("suggested_mbid","") or "",
        float(track_data.get("suggested_mbid_confidence",0.0) or 0.0),
        single_sources_json,
        int(bool(track_data.get("is_spotify_single",False))),
        int(track_data.get("spotify_total_tracks",0) or 0),
        track_data.get("spotify_album_type",""),
        int(track_data.get("navidrome_rating", 0) or 0),
        float(track_data.get("lastfm_ratio",0.0) or 0.0),
        # âœ… Audit and context values
        int(track_data.get("discogs_single_confirmed", 0) or 0),
        int(track_data.get("discogs_video_found", 0) or 0),
        int(track_data.get("is_canonical_title", 0) or 0),
        float(track_data.get("title_similarity_to_base", 0.0) or 0.0),
        int(track_data.get("album_context_live", 0) or 0),
        float(track_data.get("adaptive_weight_spotify", 0.0) or 0.0),
        float(track_data.get("adaptive_weight_lastfm", 0.0) or 0.0),
        float(track_data.get("adaptive_weight_listenbrainz", 0.0) or 0.0),
        float(track_data.get("album_median_score", 0.0) or 0.0),
        int(track_data.get("spotify_release_age_days", 0) or 0),
    ]

    placeholders = ", ".join(["?"] * len(columns))
    sql = f"INSERT OR REPLACE INTO tracks ({', '.join(columns)}) VALUES ({placeholders})"
    cursor.execute(sql, values)
    conn.commit()
    conn.close()


def get_current_track_rating(track_id: str) -> int:
    """Query the current rating for a track from the database. Returns 0 if not found."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT stars FROM tracks WHERE id = ?", (track_id,))
        row = cursor.fetchone()
        conn.close()
        return int(row[0]) if row else 0
    except Exception as e:
        logging.debug(f"Failed to get current rating for track {track_id}: {e}")
        return 0


# --- Spotify API Helpers ---
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
    """Coefficient of Variation (std/mean) â€“ simple, less robust; use MAD if you prefer."""
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
    lf = [t.get('lastfm_ratio')   for t in album_tracks]  # youâ€™ll add this field below
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

# --- REPLACEMENT: cached token helper ---
_spotify_token = None
_spotify_token_exp = 0  # epoch seconds when token expires

def get_spotify_token():
    """
    Retrieve and cache Spotify API token using Client Credentials.
    Refreshes automatically when near expiry.
    """
    global _spotify_token, _spotify_token_exp

    # Return cached token if still valid (refresh 60s before expiry)
    if _spotify_token and time.time() < (_spotify_token_exp - 60):
        return _spotify_token

    auth_str = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}"
    headers = {
        "Authorization": "Basic " + base64.b64encode(auth_str.encode()).decode(),
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {"grant_type": "client_credentials"}

    try:
        res = requests.post("https://accounts.spotify.com/api/token", headers=headers, data=data, timeout=10)
        res.raise_for_status()
        payload = res.json()
        _spotify_token = payload["access_token"]
        _spotify_token_exp = time.time() + int(payload.get("expires_in", 3600))
        return _spotify_token
    except Exception as e:
        logging.error(f"Spotify Token Error: {e}")
        sys.exit(1)

def _spotify_headers():
    return {"Authorization": f"Bearer {get_spotify_token()}"}

# --- Spotify call hygiene: global session with retries/backoff ---
_spotify_session = None
_spotify_lock = threading.Lock()

def _get_spotify_session():
    """Shared requests.Session with sensible retries/backoff for Spotify APIs."""
    global _spotify_session
    with _spotify_lock:
        if _spotify_session is None:
            _spotify_session = create_retry_session(user_agent=_DEF_USER_AGENT, retries=5, backoff=1.2)
        return _spotify_session

# --- Spotify caches for artist lookups & singles ---
_SPOTIFY_ARTIST_ID_CACHE: dict[str, str] = {}
_SPOTIFY_ARTIST_SINGLES_CACHE: dict[str, set[str]] = {}

# --- Spotify API wrappers (now using SpotifyClient) ---

def get_spotify_artist_id(artist_name: str) -> str | None:
    """Search for the artist and cache ID (wrapper using SpotifyClient)."""
    return spotify_client.get_artist_id(artist_name)

def get_spotify_artist_single_track_ids(artist_id: str) -> set[str]:
    """
    Fetch all track IDs from single releases for an artist (wrapper using SpotifyClient).
    """
    return spotify_client.get_artist_singles(artist_id)

def search_spotify_track(title, artist, album=None):
    """Search for a track on Spotify with fallback queries (wrapper using SpotifyClient)."""
    return spotify_client.search_track(title, artist, album)

def select_best_spotify_match(results, track_title, album_context: dict | None = None):
    """
    Select the best Spotify match based on popularity and album type,
    allowing 'live' only when album context permits (live/unplugged).
    """
    allow_live_remix = bool(album_context and (album_context.get("is_live") or album_context.get("is_unplugged")))
    filtered = [r for r in results if is_valid_version(r["name"], allow_live_remix=allow_live_remix)]
    if not filtered:
        return {"popularity": 0}

    singles = [r for r in filtered if (r.get("album", {}).get("album_type", "").lower() == "single")]
    if singles:
        return max(singles, key=lambda r: r.get("popularity", 0))
    return max(filtered, key=lambda r: r.get("popularity", 0))

try:
    from bs4 import BeautifulSoup
    HAVE_BS4 = True
except Exception:
    HAVE_BS4 = False

# --- Helpers (reuse your existing normalizers) ---
_DEF_USER_AGENT = "sptnr-cli/2.0"

def _canon(s: str) -> str:
    """Lowercase, strip parentheses and punctuation, normalize whitespace."""
    s = (s or "").lower()
    s = re.sub(r"\(.*?\)", " ", s)            # drop parenthetical
    s = re.sub(r"[^\w\s]", " ", s)            # strip punctuation
    s = re.sub(r"\s+", " ", s).strip()
    return s


# --- Title helpers (centralized to avoid duplicated nested versions) -----
def _base_title(s: str) -> str:
    """Return title without parenthetical subtitle and without ' - ' suffix."""
    s1 = re.sub(r"\s*\(.*?\)\s*", " ", s or "")
    s1 = re.sub(r"\s-\s.*$", "", s1).strip()
    return s1


def _has_subtitle_variant(s: str) -> bool:
    t = (s or "").lower()
    if re.search(r"\(.*?(version|remix|mix|live|acoustic|orchestral|demo|alt|instrumental|edit).*?\)", t):
        if "radio edit" in t or "remaster" in t:
            return False
        return True
    if re.search(r"\s-\s.*?(version|remix|mix|live|acoustic|orchestral|demo|alt|instrumental|edit)", t):
        if "radio edit" in t or "remaster" in t:
            return False
        return True
    return False


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _canon(a), _canon(b)).ratio()


def _release_title_core(rel_title: str, artist_name: str) -> str:
    """Discogs search 'title' often looks like 'Artist - Title / B-side'.
    Keep only the 'Title' part for fair similarity vs. track title.
    """
    t = (rel_title or "").strip()
    parts = t.split(" - ", 1)
    if len(parts) == 2 and _canon(parts[0]) == _canon(artist_name):
        t = parts[1].strip()
    return t.split(" / ")[0].strip()


def _is_variant_of(base: str, candidate: str) -> bool:
    """Treat instrumental/radio edit/remaster as benign; ban live/remix."""
    b = _canon(strip_parentheses(base)); c = _canon(candidate)
    if "live" in c or "remix" in c:
        return False
    ok = {"instrumental", "radio edit", "edit", "remaster"}
    return (b in c) or any(tok in c for tok in ok)


def _has_official_on_release(data: dict, nav_title: str, *, allow_live: bool, min_ratio: float = 0.50) -> bool:
    """Compatibility alias to the shared inspect helper."""
    return _has_official_on_release_top(data, nav_title, allow_live=allow_live, min_ratio=min_ratio)




def get_suggested_mbid(title: str, artist: str, limit: int = 5) -> tuple[str, float]:
    """Get suggested MusicBrainz ID (wrapper using MusicBrainzClient)."""
    return musicbrainz_client.get_suggested_mbid(title, artist, limit)

# --- Genre Helpers ---

def get_discogs_genres(title, artist):
    """Fetch genres from Discogs (wrapper using DiscogsClient)."""
    return discogs_client.get_genres(title, artist)

def get_audiodb_genres(artist):
    """Fetch genres from AudioDB (wrapper using AudioDbClient)."""
    return audiodb_client.get_artist_genres(artist)

def get_musicbrainz_genres(title: str, artist: str) -> list[str]:
    """Fetch genres from MusicBrainz (wrapper using MusicBrainzClient)."""
    return musicbrainz_client.get_genres(title, artist)

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

# `strip_parentheses` moved to `helpers.py` for reuse across modules
# --- Last.fm Helpers ---

def get_lastfm_track_info(artist: str, title: str) -> dict:
    """Fetch Last.fm track playcount (wrapper using LastFmClient)."""
    return lastfm_client.get_track_info(artist, title)

def get_listenbrainz_score(mbid: str, artist: str = "", title: str = "") -> int:
    """Fetch ListenBrainz listen count (wrapper using ListenBrainzClient)."""
    return listenbrainz_client.get_listen_count(mbid, artist, title)

def score_by_age(playcount, release_str):
    """Apply age decay to score based on release date (wrapper)."""
    from api_clients.audiodb_and_listenbrainz import score_by_age as _score_by_age
    return _score_by_age(playcount, release_str)

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

def set_track_rating_for_all(track_id, stars):
    targets = NAV_USERS if NAV_USERS else (
        [{"base_url": NAV_BASE_URL, "user": USERNAME, "pass": PASSWORD}] if NAV_BASE_URL and USERNAME else []
    )
    for user_cfg in targets:
        url = f"{user_cfg['base_url']}/rest/setRating.view"
        params = {
            "u": user_cfg["user"],
            "p": user_cfg["pass"],
            "v": "1.16.1",
            "c": "sptnr",
            "id": track_id,
            "rating": stars
        }
        try:
            res = session.get(url, params=params, timeout=10)
            res.raise_for_status()
            logging.info(f"âœ… Set rating {stars}/5 for track {track_id} (user {user_cfg['user']})")
        except Exception as e:
            logging.error(f"âŒ Failed for {user_cfg['user']}: {e}")

def refresh_all_playlists_from_db():
    print("ðŸ”„ Refreshing smart playlists for all artists from DB cache (no track rescans)...")
    # Pull distinct artists that have cached tracks
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT artist FROM tracks")
    artists = [row[0] for row in cursor.fetchall()]
    conn.close()
    if not artists:
        print("âš ï¸ No cached tracks in DB. Skipping playlist refresh.")
        return
    for name in artists:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, artist, album, title, stars FROM tracks WHERE artist = ?", (name,))
        rows = cursor.fetchall()
        conn.close()
        if not rows:
            print(f"âš ï¸ No cached tracks found for '{name}', skipping.")
            continue
        tracks = [{"id": r[0], "artist": r[1], "album": r[2], "title": r[3], "stars": int(r[4]) if r[4] else 0}
                  for r in rows]
        create_or_update_playlist_for_artist(name, tracks)
        print(f"âœ… Playlist refreshed for '{name}' ({len(tracks)} tracks)")

def _normalize_name(name: str) -> str:
    # Normalize typographic quotes and trim spaces
    return (
        (name or "")
        .replace("â€œ", '"').replace("â€", '"').replace("â€™", "'")
        .strip()
    )

def _log_resp(resp, action, name):
    try:
        txt = resp.text[:500]
    except Exception:
        txt = "<no text>"
    logging.info(f"{action} '{name}' â†’ {resp.status_code}: {txt}")

# --- NSP Playlist Helpers (Consolidated) ---
def _sanitize_playlist_name(name: str) -> str:
    """Sanitize playlist name for filesystem use."""
    return "".join(c for c in name if c.isalnum() or c in ('-', '_', ' ')).strip()

def _delete_nsp_file(playlist_name: str) -> None:
    """Delete an NSP playlist file if it exists."""
    try:
        music_folder = os.environ.get("MUSIC_FOLDER", "/Music")
        playlists_dir = os.path.join(music_folder, "Playlists")
        safe_name = _sanitize_playlist_name(playlist_name)
        file_path = os.path.join(playlists_dir, f"{safe_name}.nsp")
        if os.path.exists(file_path):
            os.remove(file_path)
            logging.info(f"ðŸ—‘ï¸ Deleted playlist: {playlist_name}")
    except Exception as e:
        logging.warning(f"Failed to delete playlist '{playlist_name}': {e}")

def _create_nsp_file(playlist_name: str, playlist_data: dict) -> bool:
    """Create an NSP playlist file. Returns True on success."""
    try:
        music_folder = os.environ.get("MUSIC_FOLDER", "/Music")
        playlists_dir = os.path.join(music_folder, "Playlists")
        os.makedirs(playlists_dir, exist_ok=True)
        
        safe_name = _sanitize_playlist_name(playlist_name)
        file_path = os.path.join(playlists_dir, f"{safe_name}.nsp")
        
        # Skip if file exists
        if os.path.exists(file_path):
            logging.warning(f"Playlist file already exists: {file_path}")
            return False
        
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(playlist_data, f, indent=2, ensure_ascii=False)
        
        logging.info(f"ðŸ“ NSP created: {file_path}")
        return True
    except Exception as e:
        logging.error(f"Failed to create NSP playlist '{playlist_name}': {e}")
        return False


def create_or_update_playlist_for_artist(artist: str, tracks: list[dict]):
    """
    Create/refresh 'Essential {artist}' smart playlist using Navidrome's 0â€“5 rating scale.

    Logic:
      - Case A: if artist has >= 10 five-star tracks, build a pure 5â˜… essentials playlist.
      - Case B: if total tracks >= 100, build top 10% essentials sorted by rating.
    """

    total_tracks = len(tracks)
    five_star_tracks = [t for t in tracks if (t.get("stars") or 0) == 5]
    playlist_name = f"Essential {artist}"

    # CASE A â€” 10+ five-star tracks â†’ purely 5â˜… essentials
    if len(five_star_tracks) >= 10:
        _delete_nsp_file(playlist_name)
        playlist_data = {
            "name": playlist_name,
            "comment": "Auto-generated by SPTNR",
            "all": [{"is": {"artist": artist, "rating": 5}}],
            "sort": "random"
        }
        _create_nsp_file(playlist_name, playlist_data)
        logging.info(f"Essential playlist created for '{artist}' (5â˜… essentials)")
        return

    # CASE B â€” 100+ total tracks â†’ top 10% by rating
    if total_tracks >= 100:
        _delete_nsp_file(playlist_name)
        limit = max(1, math.ceil(total_tracks * 0.10))
        playlist_data = {
            "name": playlist_name,
            "comment": "Auto-generated by SPTNR",
            "all": [{"is": {"artist": artist}}],
            "sort": "-rating,random",
            "limit": limit
        }
        _create_nsp_file(playlist_name, playlist_data)
        logging.info(f"Essential playlist created for '{artist}' (top 10% by rating)")
        return

    logging.info(
        f"No Essential playlist created for '{artist}' "
        f"(total={total_tracks}, fiveâ˜…={len(five_star_tracks)})"
    )


# --- Navidrome API wrappers (now using NavidromeClient) ---

def fetch_artist_albums(artist_id):
    """Fetch albums for an artist (wrapper using NavidromeClient)."""
    return nav_client.fetch_artist_albums(artist_id)

def fetch_album_tracks(album_id):
    """
    Fetch all tracks for an album using Subsonic API (wrapper using NavidromeClient).
    :param album_id: Album ID in Navidrome
    :return: List of track objects
    """
    return nav_client.fetch_album_tracks(album_id)

def build_artist_index(verbose: bool = False):
    """Build artist index from Navidrome (wrapper using NavidromeClient)."""
    artist_map_from_api = nav_client.build_artist_index()
    
    # Persist to database
    conn = get_db_connection()
    cursor = conn.cursor()
    for artist_name, info in artist_map_from_api.items():
        artist_id = info.get("id")
        cursor.execute("""
            INSERT OR REPLACE INTO artist_stats (artist_id, artist_name, album_count, track_count, last_updated)
            VALUES (?, ?, ?, ?, ?)
        """, (artist_id, artist_name, 0, 0, None))
        if verbose:
            print(f"   ðŸ“ Added artist to index: {artist_name} (ID: {artist_id})")
            logging.info(f"Added artist to index: {artist_name} (ID: {artist_id})")
    conn.commit()
    conn.close()
    
    logging.info(f"âœ… Cached {len(artist_map_from_api)} artists in DB")
    print(f"âœ… Cached {len(artist_map_from_api)} artists in DB")
    return artist_map_from_api


def scan_library_to_db(verbose: bool = False, force: bool = False):
    """
    Scan the entire Navidrome library (artists -> albums -> tracks) and persist
    a lightweight representation of each track into the local DB.

    Behavior:
      - Uses NavidromeClient API helpers: build_artist_index(), fetch_artist_albums(), fetch_album_tracks()
      - For each track, writes a minimal `track_data` record via `save_to_db()`
      - Uses INSERT OR REPLACE semantics (so re-running is safe and refreshes `last_scanned`)
    """
    print("ðŸ”Ž Scanning Navidrome library into local DB...")
    artist_map_local = build_artist_index(verbose=verbose) or {}
    if not artist_map_local:
        print("âš ï¸ No artists available from Navidrome; aborting library scan.")
        return

    # Cache existing track IDs to avoid re-writing cached rows unless force=True
    existing_track_ids: set[str] = set()
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM tracks")
        existing_track_ids = {row[0] for row in cursor.fetchall()}
        conn.close()
    except Exception as e:
        logging.debug(f"Prefetch existing track IDs failed: {e}")

    total_written = 0
    total_skipped = 0
    total_albums_skipped = 0
    total_artists = len(artist_map_local)
    artist_count = 0
    
    print(f"ðŸ“Š Starting scan of {total_artists} artists...")
    
    for name, info in artist_map_local.items():
        artist_count += 1
        artist_id = info.get("id")
        if not artist_id:
            print(f"âš ï¸ [{artist_count}/{total_artists}] Skipping '{name}' (no artist ID)")
            continue
        
        print(f"ðŸŽ¨ [{artist_count}/{total_artists}] Processing artist: {name}")
        logging.info(f"Processing artist {artist_count}/{total_artists}: {name} (ID: {artist_id})")

        # Prefetch cached tracks for this artist to enable per-artist skip decisions
        existing_album_tracks: dict[str, set[str]] = {}
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT album, id FROM tracks WHERE artist = ?", (name,))
            for alb_name, tid in cursor.fetchall():
                if alb_name not in existing_album_tracks:
                    existing_album_tracks[alb_name] = set()
                existing_album_tracks[alb_name].add(tid)
            conn.close()
        except Exception as e:
            logging.debug(f"Prefetch existing tracks for artist '{name}' failed: {e}")
        
        try:
            albums = fetch_artist_albums(artist_id)
            if albums:
                print(f"   ðŸ“€ Found {len(albums)} albums")
                logging.info(f"Found {len(albums)} albums for artist '{name}'")
        except Exception as e:
            print(f"   âŒ Failed to fetch albums: {e}")
            logging.error(f"Failed to fetch albums for '{name}': {e}")
            albums = []
        
        album_count = 0
        for alb in albums:
            album_count += 1
            album_name = alb.get("name") or ""
            album_id = alb.get("id")
            if not album_id:
                continue
            
            print(f"   ðŸ“€ [{album_count}/{len(albums)}] Album: {album_name[:50]}...")
            logging.info(f"Scanning album {album_count}/{len(albums)}: {album_name}")
            
            try:
                tracks = fetch_album_tracks(album_id)
                if tracks:
                    print(f"      ðŸŽµ Found {len(tracks)} tracks")
                    logging.info(f"Found {len(tracks)} tracks in album '{album_name}'")
            except Exception as e:
                print(f"      âŒ Failed to fetch tracks: {e}")
                logging.error(f"Failed to fetch tracks for album '{album_name}': {e}")
                tracks = []

            # Album-level skip if counts already match cached tracks (unless force=True)
            cached_ids_for_album = existing_album_tracks.get(album_name, set())
            if not force and tracks and len(cached_ids_for_album) >= len(tracks):
                total_albums_skipped += 1
                print(f"      â© Skipping album (already cached): {album_name}")
                logging.info(f"Skipping album '{album_name}' â€” cached {len(cached_ids_for_album)} tracks matches API {len(tracks)}")
                continue
            
            tracks_written = 0
            tracks_skipped = 0
            tracks_updated = 0
            for t in tracks:
                track_id = t.get("id")
                if not track_id:
                    continue
                
                # Check if track exists and needs metadata update
                needs_update = False
                if not force and (track_id in existing_track_ids or track_id in cached_ids_for_album):
                    # Check if existing track is missing new metadata fields
                    try:
                        conn_check = get_db_connection()
                        cursor_check = conn_check.cursor()
                        cursor_check.execute("""
                            SELECT duration, track_number, year, bitrate 
                            FROM tracks 
                            WHERE id = ?
                        """, (track_id,))
                        row = cursor_check.fetchone()
                        conn_check.close()
                        
                        # If any of these critical fields are NULL, we MUST update (especially duration)
                        if row and (row[0] is None or row[1] is None or row[2] is None or row[3] is None):
                            needs_update = True
                            logging.info(f"Track {track_id} needs metadata update (missing: duration={row[0] is None}, track_number={row[1] is None}, year={row[2] is None}, bitrate={row[3] is None})")
                        else:
                            tracks_skipped += 1
                            continue
                    except Exception as e:
                        logging.debug(f"Error checking track metadata: {e}")
                        tracks_skipped += 1
                        continue
                
                td = {
                    "id": track_id,
                    "title": t.get("title", ""),
                    "album": album_name,
                    "artist": name,
                    "score": 0.0,
                    "spotify_score": 0,
                    "lastfm_score": 0,
                    "listenbrainz_score": 0,
                    "age_score": 0,
                    "genres": [],
                    "navidrome_genres": [t.get("genre")] if t.get("genre") else [],
                    "spotify_genres": [],
                    "lastfm_tags": [],
                    "discogs_genres": [],
                    "audiodb_genres": [],
                    "musicbrainz_genres": [],
                    "spotify_album": "",
                    "spotify_artist": "",
                    "spotify_popularity": 0,
                    "spotify_release_date": "",
                    "spotify_album_art_url": "",
                    "lastfm_track_playcount": 0,
                    "file_path": t.get("path", ""),
                    "last_scanned": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                    "spotify_album_type": "",
                    "spotify_total_tracks": 0,
                    "spotify_id": None,
                    "is_spotify_single": False,
                    "is_single": False,
                    "single_confidence": "low",
                    "single_sources": [],
                    "stars": 0,
                    "mbid": t.get("mbid", "") or "",
                    "suggested_mbid": "",
                    "suggested_mbid_confidence": 0.0,
                    "navidrome_rating": int(t.get("userRating", 0) or 0),
                    # Enhanced metadata from Navidrome for better matching
                    "duration": t.get("duration"),  # Track duration in seconds
                    "track_number": t.get("track"),  # Track number
                    "disc_number": t.get("discNumber"),  # Disc number
                    "year": t.get("year"),  # Release year
                    "album_artist": t.get("albumArtist", ""),  # Album artist
                    "bitrate": t.get("bitRate"),  # Bitrate in kbps
                    "sample_rate": t.get("samplingRate"),  # Sample rate in Hz
                }
                try:
                    save_to_db(td)
                    total_written += 1
                    if needs_update:
                        tracks_updated += 1
                    else:
                        tracks_written += 1
                    existing_track_ids.add(track_id)
                    cached_ids_for_album.add(track_id)
                except Exception as e:
                    logging.debug(f"Failed to save track {track_id} -> {e}")
            
            if tracks_written > 0:
                print(f"      âœ… Saved {tracks_written} new tracks to DB")
                logging.info(f"Saved {tracks_written} new tracks from album '{album_name}'")
            if tracks_updated > 0:
                print(f"      ðŸ”„ Updated {tracks_updated} tracks with new metadata")
                logging.info(f"Updated {tracks_updated} tracks with metadata from album '{album_name}'")
            if tracks_skipped > 0:
                total_skipped += tracks_skipped
                print(f"      â© Skipped {tracks_skipped} cached tracks")
                logging.info(f"Skipped {tracks_skipped} cached tracks for album '{album_name}'")
        
        if album_count > 0:
            print(f"   âœ… Completed {album_count} albums for '{name}'")
            
    print(f"âœ… Library scan complete. Tracks written/updated: {total_written}; skipped cached: {total_skipped}")
    logging.info(f"Library scan complete. Written/updated: {total_written}; skipped cached: {total_skipped}; albums skipped: {total_albums_skipped}")


# --- Main Rating Logic ---

def update_artist_stats(artist_id, artist_name):
    album_count = len(fetch_artist_albums(artist_id))
    track_count = sum(len(fetch_album_tracks(a['id'])) for a in fetch_artist_albums(artist_id))
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO artist_stats (artist_id, artist_name, album_count, track_count, last_updated)
        VALUES (?, ?, ?, ?, ?)
    """, (artist_id, artist_name, album_count, track_count, datetime.now().strftime("%Y-%m-%dT%H:%M:%S")))
    conn.commit()
    conn.close()


def load_artist_map():
    conn = get_db_connection()
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


def get_album_last_scanned_from_db(artist_name: str, album_name: str) -> str | None:
    """
    Return the most recent 'last_scanned' timestamp among tracks already saved
    for (artist, album). Timestamp is in '%Y-%m-%dT%H:%M:%S' or None if missing.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT MAX(last_scanned) FROM tracks WHERE artist = ? AND album = ?",
            (artist_name, album_name),
        )
        row = cursor.fetchone()
        conn.close()
        return (row[0] if row and row[0] else None)
    except Exception as e:
        logging.debug(f"get_album_last_scanned_from_db failed for '{artist_name} / {album_name}': {e}")
        return None


def get_album_track_count_in_db(artist_name: str, album_name: str) -> int:
    """
    Return how many tracks for (artist, album) currently exist in DB.
    Useful to avoid skipping albums that have no cached tracks yet.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM tracks WHERE artist = ? AND album = ?",
            (artist_name, album_name),
        )
        count = cursor.fetchone()[0] or 0
        conn.close()
        return count
    except Exception as e:
        logging.debug(f"get_album_track_count_in_db failed for '{artist_name} / {album_name}': {e}")
        return 0

def rate_artist(artist_id, artist_name, verbose=False, force=False):
    """
    Rate all tracks for a given artist and build a single smart "Essential {artist}" playlist.

    New single policy (paired/stop rules):
      - Hard stop: Discogs Single â†’ is_single=True, stars=5.
      - Hard stop: Discogs Official Video AND Spotify both match â†’ is_single=True, stars=5.
      - Continue: If at least one of {discogs_video, spotify} matched, keep checking other sources
        (MusicBrainz, Last.fm). As soon as we have TWO matches total among {spotify, discogs_video,
        musicbrainz, lastfm} â†’ is_single=True, stars=5.
      - short_release (â‰¤ 2 tracks) is shown in single_sources for audit, but does NOT count toward
        the twoâ€‘matches rule unless features.short_release_counts_as_match=True.

    Canonical/variant guard remains:
      - We still require canonical title (no remix/live edit subtitling) and high base similarity.

    Other logic unchanged:
      - Adaptive weights per album, zâ€‘bands, 4â˜… density cap, Spotify-only boost (applies only when
        is_single is True but without strong sources).
      - Median gate/secondary lookup blocks are kept but will not trigger for videoâ€‘only cases,
        because videoâ€‘only cannot set is_single=True under this policy.
    """

    # ----- Tunables & feature flags ------------------------------------------
    CLAMP_MIN    = float(config["features"].get("clamp_min", 0.75))
    CLAMP_MAX    = float(config["features"].get("clamp_max", 1.25))
    CAP_TOP4_PCT = float(config["features"].get("cap_top4_pct", 0.25))

    ALBUM_SKIP_DAYS       = int(config["features"].get("album_skip_days", 7))
    ALBUM_SKIP_MIN_TRACKS = int(config["features"].get("album_skip_min_tracks", 1))

    use_lastfm_single = bool(config["features"].get("use_lastfm_single", True))
    KNOWN_SINGLES     = (config.get("features", {}).get("known_singles", {}).get(artist_name, [])) or []

    # Confidence weights retained for reporting; five_star decision is now explicit.
    SINGLE_SOURCE_WEIGHTS = {
        "discogs": 2,
        "discogs_video": 2,
        "spotify": 1,
        "short_release": 1,
        "musicbrainz": 1,
        "youtube": 1,
        "lastfm": 1,
    }

    TITLE_SIM_THRESHOLD = float(config["features"].get("title_sim_threshold", 0.92))
    COUNT_SHORT_RELEASE_AS_MATCH = bool(config["features"].get("short_release_counts_as_match", False))

    # Median gate knobs (kept but will not affect video-only because it never forms a single alone)
    SECONDARY_ENABLED    = bool(config["features"].get("secondary_single_lookup_enabled", True))
    SECONDARY_METRIC     = str(config["features"].get("secondary_lookup_metric", "score")).lower()
    SECONDARY_DELTA      = float(config["features"].get("secondary_lookup_delta", 0.05))
    SECONDARY_REQ_STRONG = int(config["features"].get("secondary_required_strong_sources", 2))
    MEDIAN_STRATEGY      = str(config["features"].get("median_gate_strategy", "hard")).lower()  # "hard" | "soft"

    STRICT_5STAR = bool(config["features"].get("singles_require_strong_source_for_5_star", False))  # unused by new rules
    SPOTIFY_SOLO_MAX_BOOST = int(config["features"].get("spotify_solo_boost", 1))
    SPOTIFY_SOLO_MAX_STARS = int(config["features"].get("spotify_solo_boost_max_stars", 4))

    # use top-level title helpers: _base_title, _has_subtitle_variant, _similar

    def _gate_threshold(metric_key: str, album_medians: dict, delta: float) -> float:
        """Scale delta appropriately for 'score' (0..1) vs 'spotify' (0..100)."""
        album_med = album_medians.get(metric_key, 0.0)
        if metric_key == "spotify":
            delta_points = delta if delta >= 1.0 else 5.0
            return album_med - delta_points
        return album_med - delta

    # --------------------------------------------------------------------------
    # Prefetch Spotify artist singles
    # --------------------------------------------------------------------------
    try:
        spotify_artist_id = get_spotify_artist_id(artist_name)
    except Exception as e:
        logging.debug(f"Spotify artist ID lookup failed for '{artist_name}': {e}")
        spotify_artist_id = None

    singles_set_future = None
    singles_set: set[str] = set()

    if spotify_artist_id:
        aux_pool = ThreadPoolExecutor(max_workers=1)
        singles_set_future = aux_pool.submit(get_spotify_artist_single_track_ids, spotify_artist_id)
    else:
        aux_pool = None

    # --------------------------------------------------------------------------
    # Fetch albums
    # --------------------------------------------------------------------------
    albums = fetch_artist_albums(artist_id)
    if not albums:
        print(f"âš ï¸ No albums found for artist '{artist_name}'")
        if aux_pool:
            aux_pool.shutdown(wait=False)
        return {}

    if verbose:
        msg = f"Starting rating for artist: {artist_name} ({len(albums)} albums)"
        print(f"\nðŸŽ¨ {msg}")
        logging.info(msg)
    else:
        print(f"\nðŸŽ¨ Scanning artist: {artist_name}")
    
    # Aggressively collect genres for this artist from all sources
    print(f"ðŸ·ï¸ Enriching genres for {artist_name}...")
    genres_found = enrich_genres_aggressively(artist_name, verbose=verbose)
    if genres_found:
        print(f"  âœ“ Found {len(genres_found)} genres")
    
    rated_map = {}

    # --------------------------------------------------------------------------
    # MAIN ALBUM LOOP
    # --------------------------------------------------------------------------
    for album in albums:
        album_name = album.get("name", "Unknown Album")
        album_id   = album.get("id")

        # --- Album-level skip/resume ------------------------------------------
        if not force:
            album_last_scanned = get_album_last_scanned_from_db(artist_name, album_name)
            cached_track_count = get_album_track_count_in_db(artist_name, album_name)
            if album_last_scanned and cached_track_count >= ALBUM_SKIP_MIN_TRACKS:
                try:
                    last_dt = datetime.strptime(album_last_scanned, "%Y-%m-%dT%H:%M:%S")
                    days_since = (datetime.now() - last_dt).days
                except Exception:
                    days_since = 9999
                if days_since <= ALBUM_SKIP_DAYS:
                    if verbose:
                        msg = (f"Skipping album: {album_name} (last scanned {album_last_scanned}, "
                               f"cached tracks={cached_track_count}, threshold={ALBUM_SKIP_DAYS}d)")
                        print(f"â© {msg}")
                        logging.info(msg)
                    continue

        tracks = fetch_album_tracks(album_id)
        if not tracks:
            if verbose:
                msg = f"No tracks found in album '{album_name}'"
                print(f"âš ï¸ {msg}")
                logging.info(msg)
            continue

        print(f"\nðŸŽ§ Scanning album: {album_name} ({len(tracks)} tracks)")
        logging.info(f"Scanning album: {album_name} ({len(tracks)} tracks)")
        if verbose:
            print(f"   ðŸ’¾ Processing album for database: {album_name}")
        album_ctx = infer_album_context(album_name)

        album_tracks = []

        # Resolve singles set lazily
        if singles_set_future and not singles_set:
            try:
                singles_set = singles_set_future.result(timeout=SPOTIFY_PREFETCH_TIMEOUT) or set()
            except Exception as e:
                logging.debug(f"Spotify singles prefetch failed for '{artist_name}': {e}")
                singles_set = set()

        # ----------------------------------------------------------------------
        # PER-TRACK ENRICHMENT (CONCURRENT)
        # ----------------------------------------------------------------------
        with ThreadPoolExecutor(max_workers=WORKER_THREADS) as ex:
            for track in tracks:
                track_id   = track["id"]
                title      = track["title"]
                file_path  = track.get("path", "")
                nav_genres = [track.get("genre")] if track.get("genre") else []
                mbid       = track.get("mbid", None)

                if verbose:
                    print(f"   ðŸ” Processing track: {title}")
                    logging.info(f"Processing track: {title}")

                fut_sp = ex.submit(search_spotify_track, title, artist_name, album_name)
                fut_lf = ex.submit(get_lastfm_track_info, artist_name, title)
                fut_lb = ex.submit(get_listenbrainz_score, mbid, artist_name, title)

                spotify_results = fut_sp.result() or []
                lf_data         = fut_lf.result() or {"track_play": 0}
                lb_score_raw    = int(fut_lb.result() or 0)

                selected              = select_best_spotify_match(spotify_results, title, album_context=album_ctx)
                selected_id           = selected.get("id")
                sp_score              = selected.get("popularity", 0)
                spotify_album         = selected.get("album", {}).get("name", "")
                spotify_artist        = selected.get("artists", [{}])[0].get("name", "")
                spotify_genres        = selected.get("artists", [{}])[0].get("genres", [])
                spotify_release_date  = selected.get("album", {}).get("release_date", "") or "1992-01-01"
                images                = selected.get("album", {}).get("images") or []
                spotify_album_art_url = images[0].get("url", "") if images and isinstance(images[0], dict) else ""
                spotify_album_type    = (selected.get("album", {}).get("album_type", "") or "").lower()
                spotify_total_tracks  = selected.get("album", {}).get("total_tracks", None)

                is_spotify_single = bool(
                    (selected_id and selected_id in singles_set) or (spotify_album_type == "single")
                )

                lf_track_play  = int(lf_data.get("track_play", 0) or 0)
                momentum_raw, _ = score_by_age(lf_track_play, spotify_release_date)

                sp_norm = sp_score / 100.0
                lf_norm = math.log10(lf_track_play + 1)
                lb_norm = math.log10(lb_score_raw + 1)
                age_norm = math.log10(momentum_raw + 1)

                score = (SPOTIFY_WEIGHT * sp_norm) + \
                        (LASTFM_WEIGHT * lf_norm) + \
                        (LISTENBRAINZ_WEIGHT * lb_norm) + \
                        (AGE_WEIGHT * age_norm)

                if verbose:
                    msg = f"Raw score for '{title}': {score:.4f} (Spotify: {sp_norm:.3f}, Last.fm: {lf_norm:.3f}, ListenBrainz: {lb_norm:.3f}, Age: {age_norm:.3f})"
                    logging.info(msg)
                    print(f"   ðŸ”¢ {msg}")

                discogs_genres = get_discogs_genres(title, artist_name)
                audiodb_genres = get_audiodb_genres(artist_name) if AUDIODB_ENABLED else []
                mb_genres      = get_musicbrainz_genres(title, artist_name)
                lastfm_tags    = []

                online_top, _ = get_top_genres_with_navidrome(
                    {
                        "spotify":      spotify_genres,
                        "lastfm":       lastfm_tags,
                        "discogs":      discogs_genres,
                        "audiodb":      audiodb_genres,
                        "musicbrainz":  mb_genres,
                    },
                    nav_genres,
                    title=title,
                    album=album_name,
                )
                genre_context = "metal" if any("metal" in g.lower() for g in online_top) else ""
                top_genres    = adjust_genres(online_top, artist_is_metal=(genre_context == "metal"))

                suggested_mbid = ""
                suggested_confidence = 0.0
                if not mbid:
                    suggested_mbid, suggested_confidence = get_suggested_mbid(title, artist_name)
                    if verbose and suggested_mbid:
                        print(f"      â†” Suggested MBID: {suggested_mbid} (confidence {suggested_confidence})")

                track_data = {
                    "id": track_id,
                    "title": title,
                    "album": album_name,
                    "artist": artist_name,
                    "score": score,
                    "spotify_score": sp_score,
                    "lastfm_score": lf_norm,
                    "lastfm_ratio": lf_norm,
                    "listenbrainz_score": lb_norm,
                    "age_score": age_norm,
                    "genres": top_genres,
                    "navidrome_genres": nav_genres,
                    "spotify_genres": spotify_genres,
                    "lastfm_tags": lastfm_tags,
                    "discogs_genres": discogs_genres,
                    "audiodb_genres": audiodb_genres,
                    "musicbrainz_genres": mb_genres,
                    "spotify_album": spotify_album,
                    "spotify_artist": spotify_artist,
                    "spotify_popularity": sp_score,
                    "spotify_release_date": spotify_release_date,
                    "spotify_album_art_url": spotify_album_art_url,
                    "lastfm_track_playcount": lf_track_play,
                    "file_path": file_path,
                    "last_scanned": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                    "spotify_album_type": spotify_album_type,
                    "spotify_total_tracks": spotify_total_tracks,
                    "spotify_id": selected_id,
                    "is_spotify_single": is_spotify_single,
                    "is_single": False,
                    "single_confidence": "low",
                    "single_sources": [],
                    "stars": 1,
                    "mbid": mbid or "",
                    "suggested_mbid": suggested_mbid,
                    "suggested_mbid_confidence": suggested_confidence,
                    "navidrome_rating": int(track.get("userRating", 0) or 0),
                    # âœ… Audit fields (populated later after single detection)
                    "discogs_single_confirmed": 0,
                    "discogs_video_found": 0,
                    "is_canonical_title": 0,
                    "title_similarity_to_base": 0.0,
                    "album_context_live": 0,
                    # âœ… Scoring context (populated after adaptive weights computed)
                    "adaptive_weight_spotify": 0.0,
                    "adaptive_weight_lastfm": 0.0,
                    "adaptive_weight_listenbrainz": 0.0,
                    "album_median_score": 0.0,
                    "spotify_release_age_days": 0,
                }

                album_tracks.append(track_data)

        # ----------------------------------------------------------------------
        # ADAPTIVE WEIGHTS (per album) â†’ recompute 'score'
        # ----------------------------------------------------------------------
        base_weights = {'spotify': SPOTIFY_WEIGHT, 'lastfm': LASTFM_WEIGHT, 'listenbrainz': LISTENBRAINZ_WEIGHT}
        adaptive = compute_adaptive_weights(album_tracks, base_weights, clamp=(CLAMP_MIN, CLAMP_MAX), use='mad')

        for t in album_tracks:
            sp  = (t['spotify_score'] or 0) / 100.0
            lf  = t['lastfm_ratio'] or 0.0
            lb  = t['listenbrainz_score'] or 0.0
            age = t['age_score'] or 0.0
            t['score'] = (adaptive['spotify'] * sp) + (adaptive['lastfm'] * lf) + \
                         (adaptive['listenbrainz'] * lb) + (AGE_WEIGHT * age)
            # âœ… Store adaptive weights in track for DB
            t['adaptive_weight_spotify'] = adaptive['spotify']
            t['adaptive_weight_lastfm'] = adaptive['lastfm']
            t['adaptive_weight_listenbrainz'] = adaptive['listenbrainz']

        if verbose:
            logging.info(f"Adaptive weights for album {album_name}: Spotify={adaptive['spotify']:.3f}, LastFM={adaptive['lastfm']:.3f}, LB={adaptive['listenbrainz']:.3f}")

        # Album medians for gate
        scores_all     = [t["score"] for t in album_tracks]
        score_median   = median(scores_all)
        spotify_all    = [t.get("spotify_score", 0) for t in album_tracks]
        spotify_median = median(spotify_all)
        album_medians  = {"score": score_median, "spotify": spotify_median}
        
        # âœ… Store album context and median in all tracks
        for t in album_tracks:
            t['album_context_live'] = 1 if (album_ctx.get("is_live") or album_ctx.get("is_unplugged")) else 0
            t['album_median_score'] = score_median
            # âœ… Compute release age and store
            try:
                rel_date = datetime.strptime(t.get("spotify_release_date", "1992-01-01"), "%Y-%m-%d")
                age_days = max((datetime.now() - rel_date).days, 30)
                t['spotify_release_age_days'] = age_days
            except Exception:
                t['spotify_release_age_days'] = 0

        # ----------------------------------------------------------------------
        # SINGLE DETECTION â€” User's workflow: Discogs=5â˜…, Spotify/Video=+1â˜…, 2-source=5â˜…
        # ----------------------------------------------------------------------
        if verbose:
            logging.info(f"Starting single detection for album: {album_name} ({len(album_tracks)} tracks)")
            print(f"\n   ðŸ” Single Detection: {album_name}")
            logging.info(f"ðŸ” Single Detection: {album_name}")
        
        low_evidence_bumps = []  # Track songs with +1â˜… bump from single hints
        
        for trk in album_tracks:
            title          = trk["title"]
            canonical_base = _base_title(title)
            sim_to_base    = _similar(title, canonical_base)
            has_subtitle   = _has_subtitle_variant(title)

            if verbose:
                print(f"      ðŸŽµ Checking: {title}")
                logging.info(f"ðŸŽµ Checking: {title}")

            allow_live_remix = bool(album_ctx.get("is_live") or album_ctx.get("is_unplugged"))
            canonical        = is_valid_version(title, allow_live_remix=allow_live_remix)

            # âœ… Store canonical title audit fields
            trk['is_canonical_title'] = 1 if canonical else 0
            trk['title_similarity_to_base'] = sim_to_base

            spotify_matched  = bool(trk.get("is_spotify_single"))
            tot              = trk.get("spotify_total_tracks")
            short_release    = (tot is not None and tot > 0 and tot <= 2)

            # Accumulate sources for visibility; short_release is audit-only unless configured otherwise
            sources = set()
            if spotify_matched:
                sources.add("spotify")
            if short_release:
                sources.add("short_release")
            
            if verbose:
                hints = []
                if spotify_matched:
                    hints.append("Spotify single")
                if short_release:
                    hints.append(f"short release ({tot} tracks)")
                if hints:
                    print(f"         ðŸ’¡ Initial hints: {', '.join(hints)}")
                    logging.info(f"ðŸ’¡ Initial hints: {', '.join(hints)}")

            # --- Discogs Single (hard stop) -----------------------------------
            discogs_single_hit = False
            try:
                if verbose:
                    print(f"         ðŸ” Checking Discogs single...")
                    logging.info("ðŸ” Checking Discogs single...")
                logging.debug("Checking Discogs single for '%s' by '%s'", title, artist_name)
                if DISCOGS_ENABLED and DISCOGS_TOKEN and is_discogs_single(title, artist=artist_name, album_context=album_ctx):
                    sources.add("discogs")
                    discogs_single_hit = True
                    trk['discogs_single_confirmed'] = 1  # âœ… Audit field
                    logging.debug("Discogs single detected for '%s' (sources=%s)", title, sources)
                    if verbose:
                        print(f"         âœ… Discogs single FOUND")
                        logging.info("âœ… Discogs single FOUND")
                else:
                    logging.debug("Discogs single not detected for '%s'", title)
                    if verbose and DISCOGS_ENABLED:
                        print(f"         âŒ Discogs single not found")
                        logging.info("âŒ Discogs single not found")
            except Exception as e:
                logging.exception("is_discogs_single failed for '%s': %s", title, e)

            if discogs_single_hit and canonical and not has_subtitle and sim_to_base >= TITLE_SIM_THRESHOLD:
                trk["is_single"] = True
                trk["single_sources"] = sorted(sources)
                trk["single_confidence"] = "high"
                trk["stars"] = 5
                logging.info("Single CONFIRMED (Discogs): '%s' â†’ 5â˜…", title)
                if verbose:
                    print(f"         â­â­â­â­â­ CONFIRMED via Discogs single (sources: {', '.join(sorted(sources))})")
                    logging.info(f"â­â­â­â­â­ CONFIRMED via Discogs single (sources: {', '.join(sorted(sources))})")
                # Hard stop for this track
                continue

            # --- Discogs Official Video (gives +1â˜… bump if Spotify or Video match) -----
            discogs_video_hit = False
            try:
                if DISCOGS_ENABLED and DISCOGS_TOKEN:
                    if verbose:
                        print(f"         ðŸ” Checking Discogs official video...")
                        logging.info("ðŸ” Checking Discogs official video...")
                    logging.debug("Searching Discogs for official video for '%s' by '%s'", title, artist_name)
                    dv = discogs_official_video_signal(
                        title, artist_name,
                        discogs_token=DISCOGS_TOKEN,
                        album_context=album_ctx,
                        permissive_fallback=CONTEXT_FALLBACK_STUDIO,
                    )
                    logging.debug("Discogs video check result for '%s': %s", title, dv)
                    if dv.get("match"):
                        sources.add("discogs_video")
                        discogs_video_hit = True
                        trk['discogs_video_found'] = 1  # âœ… Audit field
                        if verbose:
                            print(f"         âœ… Discogs official video FOUND")
                            logging.info("âœ… Discogs official video FOUND")
                    elif verbose:
                        print(f"         âŒ Discogs official video not found")
                        logging.info("âŒ Discogs official video not found")
            except Exception as e:
                logging.exception("discogs_official_video_signal failed for '%s': %s", title, e)

            # Paired hard stop: Spotify + Official Video both match â†’ 5â˜…
            if (discogs_video_hit and spotify_matched) and canonical and not has_subtitle and sim_to_base >= TITLE_SIM_THRESHOLD:
                trk["is_single"] = True
                trk["single_sources"] = sorted(sources)
                trk["single_confidence"] = "high"
                trk["stars"] = 5
                logging.info("Single CONFIRMED (Spotify + Video): '%s' â†’ 5â˜…", title)
                if verbose:
                    print(f"         â­â­â­â­â­ CONFIRMED via Spotify + Discogs video (sources: {', '.join(sorted(sources))})")
                    logging.info(f"â­â­â­â­â­ CONFIRMED via Spotify + Discogs video (sources: {', '.join(sorted(sources))})")
                continue

            # --- If neither Spotify nor Video match â†’ stop (not a single) ----
            if not (discogs_video_hit or spotify_matched):
                trk["is_single"] = False
                trk["single_sources"] = sorted(sources)
                trk["single_confidence"] = "low" if len(sources) == 0 else "medium"
                logging.debug("No single hint (Spotify/Video) for '%s' â†’ not checking further", title)
                if verbose:
                    print(f"         â­ï¸  No Spotify/Video hints - skipping further checks")
                    logging.info("â­ï¸  No Spotify/Video hints - skipping further checks")
                # let z-bands assign stars later
                continue

            # Add corroborative sources
            if verbose:
                print(f"         ðŸ” Checking additional sources (MusicBrainz, Last.fm)...")
                logging.info("ðŸ” Checking additional sources (MusicBrainz, Last.fm)...")
            
            try:
                logging.debug("Checking MusicBrainz single for '%s' by '%s'", title, artist_name)
                if is_musicbrainz_single(title, artist_name):
                    sources.add("musicbrainz")
                    logging.debug("MusicBrainz reports single for '%s'", title)
                    if verbose:
                        print(f"         âœ… MusicBrainz single FOUND")
                        logging.info("âœ… MusicBrainz single FOUND")
                elif verbose and MUSICBRAINZ_ENABLED:
                    print(f"         âŒ MusicBrainz single not found")
                    logging.info("âŒ MusicBrainz single not found")
            except Exception as e:
                logging.exception("MusicBrainz single check failed for '%s': %s", title, e)

            try:
                logging.debug("Checking Last.fm single for '%s' by '%s' (enabled=%s)", title, artist_name, use_lastfm_single)
                if use_lastfm_single and is_lastfm_single(title, artist_name):
                    sources.add("lastfm")
                    logging.debug("Last.fm reports single for '%s'", title)
                    if verbose:
                        print(f"         âœ… Last.fm single FOUND")
                        logging.info("âœ… Last.fm single FOUND")
                elif verbose and use_lastfm_single:
                    print(f"         âŒ Last.fm single not found")
                    logging.info("âŒ Last.fm single not found")
            except Exception as e:
                logging.exception("Last.fm single check failed for '%s': %s", title, e)

            # Count matches (MusicBrainz + Last.fm) toward 5â˜… confirmation
            match_pool = {"spotify", "discogs_video", "musicbrainz", "lastfm"}
            if COUNT_SHORT_RELEASE_AS_MATCH:
                match_pool.add("short_release")
            total_matches = len(sources & match_pool)

            if verbose:
                print(f"         ðŸ“Š Total sources: {', '.join(sorted(sources))} ({total_matches} matches)")
                logging.info(f"ðŸ“Š Total sources: {', '.join(sorted(sources))} ({total_matches} matches)")

            if (total_matches >= 2) and canonical and not has_subtitle and sim_to_base >= TITLE_SIM_THRESHOLD:
                trk["is_single"] = True
                trk["single_sources"] = sorted(sources)
                trk["single_confidence"] = "high"
                trk["stars"] = 5
                logging.info("Single CONFIRMED (2+ sources): '%s' sources=%s â†’ 5â˜…", title, sorted(sources))
                if verbose:
                    print(f"         â­â­â­â­â­ CONFIRMED via 2+ sources: {', '.join(sorted(sources))}")
                    logging.info(f"â­â­â­â­â­ CONFIRMED via 2+ sources: {', '.join(sorted(sources))}")
            else:
                # Got Spotify or Video hit, but only 1 source total â†’ +1â˜… bump
                trk["is_single"] = False
                trk["single_sources"] = sorted(sources)
                trk["single_confidence"] = "medium" if total_matches >= 1 else "low"
                # Apply +1â˜… bump if we have Spotify or Video signal
                if (spotify_matched or discogs_video_hit) and canonical and not has_subtitle:
                    trk["stars"] = 2  # +1 from default 1
                    low_evidence_bumps.append(title)
                    logging.debug("Low-evidence +1â˜… bump for '%s' (Spotify/Video hint)", title)
                    if verbose:
                        print(f"         â­â­ Low-evidence bump (Spotify/Video hint)")
                        logging.info("â­â­ Low-evidence bump (Spotify/Video hint)")
                elif verbose:
                    print(f"         â„¹ï¸  Not enough sources for single confirmation")
                    logging.info("â„¹ï¸  Not enough sources for single confirmation")
                logging.debug("Single NOT confirmed for '%s' â€” sources=%s total_matches=%d", title, sorted(sources), total_matches)

            # ------------------------------------------------------------------
            # Median gate + secondary lookup (kept, but video-only cannot reach here as single)
            # ------------------------------------------------------------------
            if SECONDARY_ENABLED and trk.get("is_single"):
                metric_key = SECONDARY_METRIC if SECONDARY_METRIC in ("score", "spotify") else "score"
                metric_val = float(trk.get("score", 0)) if metric_key == "score" else float(trk.get("spotify_score", 0))
                threshold  = _gate_threshold(metric_key, album_medians, SECONDARY_DELTA)

                has_video_only = (("discogs_video" in sources) and not ({"discogs", "musicbrainz"} & sources))
                under_median   = (metric_val < threshold)

                if has_video_only and under_median:
                    sec = secondary_single_lookup(
                        trk, artist_name, album_ctx,
                        singles_set=singles_set,
                        required_strong_sources=SECONDARY_REQ_STRONG
                    )
                    logging.debug("Secondary lookup result for '%s': %s", title, sec)
                    merged_sources = sorted(set(trk.get("single_sources", [])) | set(sec["sources"]))
                    trk["single_sources"]    = merged_sources
                    trk["single_confidence"] = sec["confidence"]
                    strong_sources = {"discogs", "discogs_video", "musicbrainz"}
                    strong_count   = len(set(merged_sources) & strong_sources)

                    if strong_count >= SECONDARY_REQ_STRONG:
                        trk["is_single"] = True
                    else:
                        if MEDIAN_STRATEGY == "soft":
                            trk["is_single"] = True
                            if set(merged_sources) == {"discogs_video"}:
                                trk["single_confidence"] = "medium"
                            if int(trk.get("stars", 0)) == 5:
                                trk["stars"] = 4
                        else:
                            trk["is_single"] = False

                        logging.info(
                            f"[median-gate] '{title}' {metric_key}={metric_val:.3f} < "
                            f"{album_medians[metric_key]:.3f}-{SECONDARY_DELTA:.3f} "
                            f"â†’ strategy={MEDIAN_STRATEGY} sources={','.join(trk['single_sources'])}"
                        )

        # ----------------------------------------------------------------------
        # Z-BANDS (apply to everyone except confirmed 5â˜… singles)
        # ----------------------------------------------------------------------
        sorted_album = sorted(album_tracks, key=lambda x: x["score"], reverse=True)
        EPS = 1e-6
        scores_all = [t["score"] for t in sorted_album]
        med = median(scores_all)
        mad_val = max(median([abs(v - med) for v in scores_all]), EPS)

        if verbose:
            logging.info(f"Z-band assignment for album {album_name}: median={med:.3f}, MAD={mad_val:.3f}")

        def zrobust(x): return (x - med) / mad_val

        eligible_for_zband = [
            t for t in sorted_album
            if not (t.get("is_single") and t.get("stars") == 5)
        ]

        BANDS = [
            (-float("inf"), -1.0, 1),
            (-1.0, -0.3, 2),
            (-0.3, 0.6, 3),
            (0.6, float("inf"), 4)
        ]

        for t in eligible_for_zband:
            z = zrobust(t["score"])
            for lo, hi, stars in BANDS:
                if lo <= z < hi:
                    t["stars"] = stars
                    break

        # Cap density of 4â˜… among non-singles
        non_single_tracks = [t for t in sorted_album if not t.get("is_single")]
        top4 = [t for t in non_single_tracks if t.get("stars") == 4]
        max_top4 = max(1, round(len(non_single_tracks) * CAP_TOP4_PCT))
        if len(top4) > max_top4:
            for t in sorted(top4, key=lambda x: zrobust(x["score"]), reverse=True)[max_top4:]:
                t["stars"] = 3

        # ----------------------------------------------------------------------
        # Spotify-only single BOOST (AFTER z-bands) â€” retained
        # ----------------------------------------------------------------------
        for t in sorted_album:
            already_strong = any(s in t.get("single_sources", []) for s in ("discogs", "discogs_video", "musicbrainz"))
            is_spotify_hint = ("spotify" in t.get("single_sources", [])) or ("short_release" in t.get("single_sources", []))

            if t.get("is_single") and is_spotify_hint and not already_strong:
                current_stars = int(t.get("stars", 0))
                if current_stars >= 4:
                    t["stars"] = 5
                else:
                    t["stars"] = min(SPOTIFY_SOLO_MAX_STARS, current_stars + SPOTIFY_SOLO_MAX_BOOST)

        # ----------------------------------------------------------------------
        # SAVE + SYNC
        # -----------------------------------------------------------------------
        for trk in sorted_album:
            track_id = trk["id"]
            old_stars = get_current_track_rating(track_id)  # âœ… Fetch current rating before update
            save_to_db(trk)
            if verbose:
                logging.debug(f"Saved track to DB: {trk['title']} (ID: {track_id})")

            new_stars = int(trk.get("stars", 0))
            title     = trk.get("title", trk["id"])
            rating_changed = (old_stars != new_stars)
            
            # âœ… Always log, only print if verbose OR rating changed
            if trk.get("is_single"):
                sources_list = trk.get("single_sources") or []
                srcs = ", ".join(sources_list) if sources_list else "low confidence"
                logging.info(f"Rating set (single via {srcs}): {track_id} '{title}' -> {old_stars}â˜… â†’ {new_stars}â˜…")
                
                if verbose or rating_changed:
                    rating_change = f"{old_stars}â˜… â†’ {new_stars}â˜…" if rating_changed else f"{new_stars}â˜… (unchanged)"
                    print(f"   ðŸŽ›ï¸ Rating set (single via {srcs}): '{title}' â€” {rating_change}")
            else:
                logging.info(f"Rating set: {track_id} '{title}' -> {old_stars}â˜… â†’ {new_stars}â˜…")
                
                if verbose or rating_changed:
                    rating_change = f"{old_stars}â˜… â†’ {new_stars}â˜…" if rating_changed else f"{new_stars}â˜… (unchanged)"
                    print(f"   ðŸŽ›ï¸ Rating set: '{title}' â€” {rating_change}")

            if config["features"].get("dry_run", False):
                continue

            if config["features"].get("sync", True):
                set_track_rating_for_all(track_id, new_stars)

        # ----------------------------------------------------------------------
        # ALBUM SUMMARY â€” Show 5â˜… singles and low-evidence +1â˜… bumps
        # ----------------------------------------------------------------------
        five_star_singles = [t for t in sorted_album if t.get("is_single") and t.get("stars") == 5]
        low_evidence_2stars = [t for t in sorted_album if not t.get("is_single") and t.get("stars") == 2]
        
        if verbose:
            msg = (f"Album summary: 5â˜… singles={len(five_star_singles)}, low-evidence +1â˜… bumps={len(low_evidence_2stars)}, "
                   f"non-single 4â˜…={sum(1 for t in non_single_tracks if t['stars']==4)}, cap={int(CAP_TOP4_PCT*100)}%, MAD={mad_val:.2f}")
            logging.info(msg)
            print(
                f"   â„¹ï¸ Album Stats: 5â˜…={len(five_star_singles)} | Low-evidence +1â˜…={len(low_evidence_2stars)} | "
                f"Nonâ€‘single 4â˜…={sum(1 for t in non_single_tracks if t['stars']==4)} | "
                f"Cap={int(CAP_TOP4_PCT*100)}% | MAD={mad_val:.2f}"
            )

        if len(five_star_singles) > 0:
            if verbose:
                logging.info(f"5â˜… Singles found: {len(five_star_singles)}")
                print("   ðŸŽ¯ 5â˜… Singles:")
                for t in five_star_singles:
                    sources_list = t.get("single_sources") or []
                    srcs = ", ".join(sources_list) if sources_list else "low confidence"
                    logging.info(f"  â€¢ {t['title']} (via {srcs})")
                    print(f"      â€¢ {t['title']} (via {srcs})")
            else:
                # Non-verbose: show count only if singles found
                print(f"   ðŸŽ¯ {len(five_star_singles)} 5â˜… singles detected")
                logging.info(f"5â˜… singles detected: {len(five_star_singles)}")
                for t in five_star_singles:
                    sources_list = t.get("single_sources") or []
                    srcs = ", ".join(sources_list) if sources_list else "low confidence"
                    logging.info(f"  â€¢ {t['title']} (via {srcs})")

        if len(low_evidence_2stars) > 0:
            if verbose:
                logging.info(f"Low-evidence +1â˜… bumps found: {len(low_evidence_2stars)}")
                print("   âš ï¸ Low-evidence +1â˜… bumps (Spotify/Video hint):")
                for t in low_evidence_2stars:
                    sources_list = t.get("single_sources") or []
                    srcs = ", ".join(sources_list) if sources_list else "low confidence"
                    logging.info(f"  â—¦ {t['title']} (via {srcs})")
                    print(f"      â—¦ {t['title']} (via {srcs})")
            else:
                # Non-verbose: show count and list
                print(f"   âš ï¸ {len(low_evidence_2stars)} low-evidence +1â˜… bumps")
                logging.info(f"Low-evidence +1â˜… bumps: {len(low_evidence_2stars)}")
                for t in low_evidence_2stars:
                    sources_list = t.get("single_sources") or []
                    srcs = ", ".join(sources_list) if sources_list else "low confidence"
                    logging.info(f"  â—¦ {t['title']} (via {srcs})")
                    print(f"      â—¦ {t['title']} (via {srcs})")

        if verbose:
            print(f"âœ” Completed album: {album_name}")
        rated_map.update({t["id"]: t for t in sorted_album})

    # --------------------------------------------------------------------------
    # SMART PLAYLIST CREATION
    # --------------------------------------------------------------------------
    if artist_name.lower() != "various artists" and config["features"].get("sync", True) and not config["features"].get("dry_run", False):
        playlist_name = f"Essential {artist_name}"
        total_tracks = len(rated_map)
        five_star_tracks = [t for t in rated_map.values() if (t.get("stars") or 0) == 5]

        if len(five_star_tracks) >= 10:
            _delete_nsp_file(playlist_name)
            playlist_data = {
                "name": playlist_name,
                "comment": "Auto-generated by SPTNR",
                "all": [{"is": {"artist": artist_name, "rating": 5}}],
                "sort": "random"
            }
            _create_nsp_file(playlist_name, playlist_data)
            logging.info(f"Essential playlist created for '{artist_name}' (5â˜… essentials)")

        elif total_tracks >= 100:
            _delete_nsp_file(playlist_name)
            limit = max(1, math.ceil(total_tracks * 0.10))
            playlist_data = {
                "name": playlist_name,
                "comment": "Auto-generated by SPTNR",
                "all": [{"is": {"artist": artist_name}}],
                "sort": "-rating,random",
                "limit": limit
            }
            _create_nsp_file(playlist_name, playlist_data)
            logging.info(f"Essential playlist created for '{artist_name}' (top 10% by rating)")

        else:
            logging.info(
                f"No Essential playlist created for '{artist_name}' "
                f"(total={total_tracks}, fiveâ˜…={len(five_star_tracks)})"
            )

    if aux_pool:
        aux_pool.shutdown(wait=False)

    print(f"âœ… Finished rating for artist: {artist_name}")
    return rated_map

def _self_test_single_gate():
    """
    Sanity tests for Discogs-first single gate logic (HIGH confidence required).
    No network calls; uses synthetic combinations of sources.
    """
    SINGLE_SOURCE_WEIGHTS = {
        "discogs": 2, "discogs_video": 2,
        "spotify": 1, "short_release": 1,
        "musicbrainz": 1, "youtube": 1, "lastfm": 1,
    }

    def confidence_for(sources_set, spotify_source=False, short_release_source=False):
        weighted_count = sum(SINGLE_SOURCE_WEIGHTS.get(s, 0) for s in sources_set)
        high_combo     = (spotify_source and short_release_source)
        discogs_strong = any(s in sources_set for s in ("discogs", "discogs_video"))
        if discogs_strong or high_combo or weighted_count >= 3:
            return "high"
        elif weighted_count >= 2:
            return "medium"
        else:
            return "low"

    cases = [
        ("Discogs Single only",                 True, {"discogs"},          True),
        ("Discogs Official Video only",         True, {"discogs_video"},    True),
        ("YouTube + Last.fm (two sources)",     True, {"youtube","lastfm"}, False),  # medium -> NOT single
        ("Spotify single + short (combo)",      True, {"spotify","short_release"}, True),
        ("Short release only",                  True, {"short_release"},    False),
        ("Spotify single only",                 True, {"spotify"},          False),   # high confidence policy
        ("Discogs strong but non-canonical",    False, {"discogs"},         False),
    ]

    print("\nðŸ§ª Self-test: HIGH confidence required")
    passes = 0
    for name, canonical, sset, expected in cases:
        conf = confidence_for(sset, "spotify" in sset, "short_release" in sset)
        decision = canonical and (conf == "high")
        ok = (decision == expected)
        passes += int(ok)
        print(f" - {name:<35} â†’ conf={conf}, decision={decision}  [{'PASS' if ok else 'FAIL'}]")
    print(f"âœ… {passes}/{len(cases)} cases passed.\n")


# âœ… Main scan function that can be called from app.py
def run_scan(scan_type='batchrate', verbose=False, force=False, dry_run=False):
    """
    Execute a scan of the music library.
    
    Args:
        scan_type: 'batchrate' or 'perpetual' (default: 'batchrate')
        verbose: Enable verbose output
        force: Force re-scan of all tracks
        dry_run: Preview only, don't apply ratings
    """
    global config
    
    
    # Create scan lock file to indicate scanning is in progress
    scan_lock_path = "/config/.scan_lock"
    try:
        with open(scan_lock_path, 'w') as f:
            f.write(str(os.getpid()))
    except Exception as e:
        logging.warning(f"Could not create scan lock file: {e}")
    
    # âœ… Reload config on each run
    config = load_config()
    
    # Get configuration options
    batchrate = scan_type == 'batchrate'
    perpetual = scan_type == 'perpetual'
    force = force or config["features"].get("force", False)
    dry_run = dry_run or config["features"].get("dry_run", False)
    verbose = verbose or config["features"].get("verbose", False)
    artist_list = config["features"].get("artist", [])
    
    # If verbose enabled, route debug logs to console as well
    if verbose:
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG)
        if not any(isinstance(h, logging.StreamHandler) for h in root_logger.handlers):
            ch = logging.StreamHandler(sys.stdout)
            ch.setLevel(logging.DEBUG)
            ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
            root_logger.addHandler(ch)

    # Load artist stats from DB
    conn = get_db_connection()
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

    # If DB is empty, fallback to Navidrome API
    if not artist_map:
        print("âš ï¸ No artist stats found in DB. Building index from Navidrome...")
        artist_map = build_artist_index()

    # Auto-populate track cache when empty
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM tracks")
        has_tracks = (cursor.fetchone()[0] or 0) > 0
        conn.close()
    except Exception:
        has_tracks = False

    if not has_tracks:
        print("âš ï¸ No cached tracks found in DB. Running full library scan to populate cache...")
        try:
            scan_library_to_db(verbose=verbose)
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT artist_id, artist_name, album_count, track_count, last_updated FROM artist_stats")
            artist_stats = cursor.fetchall()
            conn.close()
            artist_map = {row[1]: {"id": row[0], "album_count": row[2], "track_count": row[3], "last_updated": row[4]} for row in artist_stats}
        except Exception as e:
            logging.warning(f"Library scan failed at startup: {e}")
    else:
        if config.get("features", {}).get("scan_on_start", False):
            print("â„¹ï¸ scan_on_start enabled â€” checking Navidrome for new/updated tracks...")
            try:
                scan_library_to_db(verbose=verbose)
            except Exception as e:
                logging.warning(f"scan_on_start failed: {e}")

    # Determine execution mode
    if artist_list:
        print("â„¹ï¸ Running artist-specific rating based on config.yaml...")
        for name in artist_list:
            artist_info = artist_map.get(name)
            if not artist_info:
                print(f"âš ï¸ No data found for '{name}', skipping.")
                continue

            if dry_run:
                print(f"ðŸ‘€ Dry run: would scan '{name}' (ID {artist_info['id']})")
                continue

            if force:
                print(f"âš ï¸ Force enabled: clearing cached data for artist '{name}'...")
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("DELETE FROM tracks WHERE artist = ?", (name,))
                cursor.execute("DELETE FROM artist_stats WHERE artist_name = ?", (name,))
                conn.commit()
                conn.close()
                print(f"âœ… Cache cleared for artist '{name}'")

            rated = rate_artist(artist_info['id'], name, verbose=verbose, force=force)
            print(f"âœ… Completed rating for {name}. Tracks rated: {len(rated)}")

            album_count = len(fetch_artist_albums(artist_info['id']))
            track_count = sum(len(fetch_album_tracks(a['id'])) for a in fetch_artist_albums(artist_info['id']))
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO artist_stats (artist_id, artist_name, album_count, track_count, last_updated)
                VALUES (?, ?, ?, ?, ?)
            """, (artist_info['id'], name, album_count, track_count, datetime.now().strftime("%Y-%m-%dT%H:%M:%S")))
            conn.commit()
            conn.close()

    # If force is enabled for batch mode, clear entire cache before scanning
    if force and batchrate:
        print("âš ï¸ Force enabled: clearing entire cached library...")
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM tracks")
        cursor.execute("DELETE FROM artist_stats")
        conn.commit()
        conn.close()
        print("âœ… Entire cache cleared. Starting fresh...")
        print("â„¹ï¸ Rebuilding artist index from Navidrome after force clear...")
        build_artist_index()

    # Always run batch rating when requested
    if batchrate:
        print("â„¹ï¸ Running full library batch rating based on DB...")
        
        try:
            url = f"{NAV_BASE_URL}/rest/getArtists.view"
            params = {"u": USERNAME, "p": PASSWORD, "v": "1.16.1", "c": "sptnr", "f": "json"}
            res = session.get(url, params=params)
            res.raise_for_status()
            index = res.json().get("subsonic-response", {}).get("artists", {}).get("index", [])
            navidrome_artist_count = sum(len(group.get("artist", [])) for group in index)
            
            navidrome_album_count = 0
            navidrome_track_count = 0
            for group in index:
                for artist in group.get("artist", []):
                    artist_id = artist.get("id")
                    if artist_id:
                        albums = fetch_artist_albums(artist_id)
                        navidrome_album_count += len(albums)
                        for album in albums:
                            tracks = fetch_album_tracks(album.get("id"))
                            navidrome_track_count += len(tracks)
            
            print(f"ðŸ“Š Navidrome: {navidrome_artist_count} artists, {navidrome_album_count} albums, {navidrome_track_count} tracks")
        except Exception as e:
            print(f"âš ï¸ Failed to get counts from Navidrome: {e}")
            navidrome_track_count = 0
        
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(DISTINCT artist) FROM tracks")
        db_artist_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(DISTINCT album) FROM tracks")
        db_album_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM tracks")
        db_track_count = cursor.fetchone()[0]
        conn.close()
        
        print(f"ðŸ’¾ Database: {db_artist_count} artists, {db_album_count} albums, {db_track_count} tracks")
        
        if navidrome_track_count != db_track_count or db_track_count == 0:
            print("ðŸ”„ Track counts don't match. Running full library scan to sync database...")
            scan_library_to_db(verbose=verbose, force=force)
        else:
            print("âœ… Database is in sync with Navidrome. Refreshing artist index...")
            build_artist_index(verbose=verbose)

        conn = get_db_connection()
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
            print("âŒ No artists found after rebuild. Aborting batch rating.")
        else:
            for name, artist_info in artist_map.items():
                needs_update = True if force else (
                    not artist_info['last_updated'] or
                    (datetime.now() - datetime.strptime(artist_info['last_updated'], "%Y-%m-%dT%H:%M:%S")).days > 7
                )

                if not needs_update:
                    print(f"â© Skipping '{name}' (last updated {artist_info['last_updated']})")
                    continue

                if dry_run:
                    print(f"ðŸ‘€ Dry run: would scan '{name}' (ID {artist_info['id']})")
                    continue

                rated = rate_artist(artist_info['id'], name, verbose=verbose, force=force)
                print(f"âœ… Completed rating for {name}. Tracks rated: {len(rated)}")

                album_count = len(fetch_artist_albums(artist_info['id']))
                track_count = sum(len(fetch_album_tracks(a['id'])) for a in fetch_artist_albums(artist_info['id']))
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT OR REPLACE INTO artist_stats (artist_id, artist_name, album_count, track_count, last_updated)
                    VALUES (?, ?, ?, ?, ?)
                """, (artist_info['id'], name, album_count, track_count, datetime.now().strftime("%Y-%m-%dT%H:%M:%S")))
                conn.commit()
                conn.close()
                time.sleep(1.5)

    # Perpetual mode with self-healing index
    if perpetual:
        print("â„¹ï¸ Running perpetual mode based on DB (optimized for stale artists)...")
        while True:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT artist_id, artist_name FROM artist_stats
                WHERE last_updated IS NULL OR last_updated < DATE('now','-7 days')
            """)
            rows = cursor.fetchall()
            conn.close()

            if not rows:
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM artist_stats")
                total_artists = cursor.fetchone()[0]
                conn.close()

                if total_artists == 0:
                    print("âš ï¸ No artists found in DB; rebuilding index from Navidrome...")
                    build_artist_index()
                    conn = get_db_connection()
                    cursor = conn.cursor()
                    cursor.execute("""
                        SELECT artist_id, artist_name FROM artist_stats
                        WHERE last_updated IS NULL OR last_updated < DATE('now','-7 days')
                    """)
                    rows = cursor.fetchall()
                    conn.close()

            if not rows:
                print("âœ… No artists need updating. Sleeping for 12 hours...")
                time.sleep(12 * 60 * 60)
                continue

            print(f"ðŸ”„ Starting scheduled scan for {len(rows)} stale artists...")
            for artist_id, artist_name in rows:
                print(f"ðŸŽ¨ Processing artist: {artist_name} (ID: {artist_id})")
                rated = rate_artist(artist_id, artist_name, verbose=verbose, force=force)
                print(f"âœ… Completed rating for {artist_name}. Tracks rated: {len(rated)}")

                update_artist_stats(artist_id, artist_name)
                time.sleep(1.5)

            print("ðŸ•’ Scan complete. Sleeping for 12 hours...")
            time.sleep(12 * 60 * 60)
    
    # Remove scan lock file when scan completes (or if perpetual mode exits)
    try:
        if os.path.exists(scan_lock_path):
            os.remove(scan_lock_path)
    except Exception as e:
        logging.warning(f"Could not remove scan lock file: {e}")


# --- CLI Handling ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ðŸŽ§ SPTNR â€“ Navidrome Rating CLI with API Integration")
    parser.add_argument("--artist", type=str, nargs="+", help="Rate one or more artists by name")
    parser.add_argument("--batchrate", action="store_true", help="Rate the entire library")
    parser.add_argument("--refresh", action="store_true", help="Rebuild artist index cache")
    parser.add_argument("--pipeoutput", type=str, nargs="?", const="", help="Print cached artist index")
    parser.add_argument("--perpetual", action="store_true", help="Run perpetual 12-hour scan loop")
    parser.add_argument("--dry-run", action="store_true", help="Preview artist list only (no rating)")
    parser.add_argument("--sync", action="store_true", help="Push ratings to Navidrome after calculation")
    parser.add_argument("--force", action="store_true", help="Force re-scan of all tracks")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose debug output")
    parser.add_argument("--refresh-playlists", action="store_true",
                        help="Recreate smart playlists for all artists without rescanning tracks")

    args = parser.parse_args()

    # âœ… Update config.yaml with CLI overrides if provided
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
                print(f"âœ… Config updated with CLI overrides in {config_path}")
            except Exception as e:
                print(f"âŒ Failed to update config.yaml: {e}")

    update_config_with_cli(args, config)

    # âœ… Merge config values for runtime
    dry_run  = config["features"]["dry_run"]
    sync     = config["features"]["sync"]
    force    = config["features"]["force"]
    verbose  = config["features"]["verbose"]
    perpetual = config["features"]["perpetual"]
    batchrate = config["features"]["batchrate"]
    artist_list = config["features"]["artist"]
    # Legacy feature flags (deprecated - use api_integrations.enabled instead)
    use_google  = config["features"].get("use_google", GOOGLE_ENABLED)
    use_youtube = config["features"].get("use_youtube", YOUTUBE_ENABLED)
    use_audiodb = config["features"].get("use_audiodb", AUDIODB_ENABLED)
    refresh_playlists_on_start = config["features"].get("refresh_playlists_on_start", False)
    refresh_index_on_start     = config["features"].get("refresh_artist_index_on_start", False)

    # If verbose enabled, route debug logs to console as well
    if verbose:
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG)
        # add a console handler if none exists
        if not any(isinstance(h, logging.StreamHandler) for h in root_logger.handlers):
            ch = logging.StreamHandler(sys.stdout)
            ch.setLevel(logging.DEBUG)
            ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
            root_logger.addHandler(ch)

    # --- Early startup triggers from YAML flags ---
    if refresh_index_on_start:
        print("ðŸ“š Building artist index from Navidrome (startup)â€¦")
        build_artist_index()

    if refresh_playlists_on_start:
        # Guard: only useful if tracks exist in DB
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM tracks")
        has_tracks = (cursor.fetchone()[0] or 0) > 0
        conn.close()

        if not has_tracks:
            print("âš ï¸ No cached tracks yet; playlist refresh would be ineffective.")
            # Optional: trigger a small rating pass here if you want to auto-populate.

        print("ðŸš€ Startup flag enabled: refreshing smart playlists from DB cacheâ€¦")
        refresh_all_playlists_from_db()
        # Optional: exit after startup-only behavior:
        # sys.exit(0)

    # âœ… Rebuild artist index if requested by CLI
    if args.refresh:
        build_artist_index()

    # âœ… Pipe output if requested (print cached artist index and exit)
    if args.pipeoutput is not None:
        artist_map = load_artist_map()
        filtered = {
            name: info for name, info in artist_map.items()
            if not args.pipeoutput or args.pipeoutput.lower() in name.lower()
        }
        print(f"\nðŸ“ Cached Artist Index ({len(filtered)} matches):")
        for name, info in filtered.items():
            print(f"ðŸŽ¨ {name} â†’ ID: {info['id']} "
                  f"(Albums: {info['album_count']}, Tracks: {info['track_count']}, "
                  f"Last Updated: {info['last_updated']})")
        sys.exit(0)

    # âœ… Refresh smart playlists from DB cache when requested via CLI and exit
    if args.refresh_playlists:
        refresh_all_playlists_from_db()
        sys.exit(0)

    # âœ… Determine which scan type to run
    scan_type = None
    if args.batchrate:
        scan_type = 'batchrate'
    elif args.perpetual:
        scan_type = 'perpetual'
    elif config["features"].get("batchrate") and config["features"].get("perpetual"):
        scan_type = 'batchrate'
    
    # âœ… Only call run_scan if we have a scan type to execute
    if scan_type:
        run_scan(
            scan_type=scan_type, 
            verbose=args.verbose or config["features"].get("verbose", False),
            force=args.force or config["features"].get("force", False),
            dry_run=args.dry_run or config["features"].get("dry_run", False)
        )
    else:
        print("âš ï¸ No CLI arguments and no enabled features in config.yaml. Exiting...")
        sys.exit(0)



# scan_popularity() has been moved to popularity.py
# This module now calls popularity.scan_popularity() for popularity updates


def enrich_genres_aggressively(artist_name: str, verbose: bool = False):
    """
    Aggressively collect genres from all available sources for an artist.
    Called during rate_artist() to cache comprehensive genre data.
    """
    genres_collected = set()
    
    try:
        # Get from Discogs
        try:
            discogs_genres = get_discogs_genres(artist_name, "")
            if discogs_genres:
                genres_collected.update([g.lower() for g in discogs_genres])
                if verbose:
                    logging.info(f"Discogs genres for {artist_name}: {discogs_genres}")
        except Exception as e:
            logging.debug(f"Discogs genre lookup failed for {artist_name}: {e}")
        
        # Get from AudioDB
        try:
            audiodb_genres = get_audiodb_genres(artist_name)
            if audiodb_genres:
                genres_collected.update([g.lower() for g in audiodb_genres])
                if verbose:
                    logging.info(f"AudioDB genres for {artist_name}: {audiodb_genres}")
        except Exception as e:
            logging.debug(f"AudioDB genre lookup failed for {artist_name}: {e}")
        
        # Get from MusicBrainz
        try:
            mb_genres = get_musicbrainz_genres(artist_name, "")
            if mb_genres:
                genres_collected.update([g.lower() for g in mb_genres])
                if verbose:
                    logging.info(f"MusicBrainz genres for {artist_name}: {mb_genres}")
        except Exception as e:
            logging.debug(f"MusicBrainz genre lookup failed for {artist_name}: {e}")
        
        # Store in cache for later use
        if genres_collected:
            try:
                conn = get_db_connection()
                cursor = conn.cursor()
                
                # Update all tracks for this artist with collected genres
                genres_str = ", ".join(sorted(genres_collected))
                cursor.execute("""
                    UPDATE tracks SET genres = ?
                    WHERE artist = ? AND (genres IS NULL OR genres = '')
                """, (genres_str, artist_name))
                conn.commit()
                conn.close()
                
                if verbose:
                    logging.info(f"Updated {cursor.rowcount} tracks for {artist_name} with {len(genres_collected)} genres")
            except Exception as e:
                logging.debug(f"Failed to update genres for {artist_name}: {e}")
    
    except Exception as e:
        logging.debug(f"Genre enrichment failed for {artist_name}: {e}")
    
    return genres_collected

