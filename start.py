

#!/usr/bin/env python3
# SPTNR – Navidrome Rating CLI with Spotify + Last.fm + Navidrome API Integration

import argparse
import os
import sys
import time
import logging
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
from scan_history import log_album_scan

# Import modular API clients
from api_clients.navidrome import NavidromeClient
from api_clients.spotify import SpotifyClient
from api_clients.lastfm import LastFmClient
from api_clients.musicbrainz import MusicBrainzClient
from api_clients.discogs import DiscogsClient
from api_clients.audiodb_and_listenbrainz import ListenBrainzClient, AudioDbClient
from popularity_helpers import (
    configure_popularity_helpers,
    get_spotify_artist_id,
    get_spotify_artist_single_track_ids,
    search_spotify_track,
    get_lastfm_track_info,
    get_listenbrainz_score,
    score_by_age,
    SPOTIFY_WEIGHT,
    LASTFM_WEIGHT,
    LISTENBRAINZ_WEIGHT,
    AGE_WEIGHT,
)

__all__ = [
    "get_db_connection",
    "fetch_artist_albums",
    "fetch_album_tracks",
    "save_to_db"
]

# 🎨 Colorama setup
init(autoreset=True)
LIGHT_RED = Fore.RED + Style.BRIGHT
LIGHT_GREEN = Fore.GREEN + Style.BRIGHT
LIGHT_BLUE = Fore.BLUE + Style.BRIGHT
LIGHT_YELLOW = Fore.YELLOW + Style.BRIGHT
LIGHT_CYAN = Fore.CYAN + Style.BRIGHT
RESET = Style.RESET_ALL

# Helper function to parse datetime flexibly
def parse_datetime_flexible(date_string):
    """Parse datetime with flexible format handling for both 'T' and space separators."""
    formats = ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"]
    for fmt in formats:
        try:
            return datetime.strptime(date_string, fmt)
        except ValueError:
            continue
    raise ValueError(f"Could not parse datetime: {date_string}")

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
        loaded = yaml.safe_load(f) or {}
    # Merge defaults to avoid KeyErrors
    def deep_update(d, u):
        for k, v in u.items():
            if isinstance(v, dict):
                d[k] = deep_update(d.get(k, {}), v)
            else:
                d.setdefault(k, v)
        return d
    defaults = {
        "database": {"path": "/database/sptnr.db", "vacuum_on_start": False},
        "logging": {"level": "INFO", "file": "/config/app.log", "console": True},
        "features": {
            "dry_run": False, "sync": True, "force": False, "verbose": False, "perpetual": False,
            "batchrate": False, "refresh_playlists_on_start": False, "refresh_artist_index_on_start": False,
            "discogs_min_interval_sec": 0.35, "album_skip_days": 7, "album_skip_min_tracks": 1,
            "clamp_min": 0.75, "clamp_max": 1.25, "cap_top4_pct": 0.25, "title_sim_threshold": 0.92,
            "short_release_counts_as_match": False, "secondary_single_lookup_enabled": True,
            "secondary_lookup_metric": "score", "secondary_lookup_delta": 0.05,
            "secondary_required_strong_sources": 2, "median_gate_strategy": "hard", "use_lastfm_single": True,
            "include_user_ratings_on_scan": True, "scan_worker_threads": 4, "spotify_prefetch_timeout": 30,
            "artist": []
        }
    }
    config = deep_update(loaded, defaults)
    return config

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
        print("\n⚠️ Configuration issues detected:")
        for issue in issues:
            print(f" - {issue}")
        print("\n❌ Please update config.yaml using the web UI setup wizard.")
        print("You can access the setup wizard at /setup once the web UI is running.")

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

# Configure shared popularity helpers with live clients and current config
configure_popularity_helpers(
    spotify_client=spotify_client,
    lastfm_client=lastfm_client,
    listenbrainz_client=listenbrainz_client,
    config=config,
)

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



# --- Dual Logger Setup: sptnr.log and unified_scan.log ---
LOG_PATH = os.environ.get("LOG_PATH", "/config/sptnr.log")
UNIFIED_LOG_PATH = os.environ.get("UNIFIED_SCAN_LOG_PATH", "/config/unified_scan.log")
VERBOSE = (
    os.environ.get("SPTNR_VERBOSE_START") or os.environ.get("SPTNR_VERBOSE") or "0"
) == "1"
SERVICE_PREFIX = "start_"

class ServicePrefixFormatter(logging.Formatter):
    def __init__(self, prefix, fmt=None):
        super().__init__(fmt or '%(asctime)s [%(levelname)s] %(message)s')
        self.prefix = prefix
    def format(self, record):
        record.msg = f"{self.prefix}{record.msg}"
        return super().format(record)

formatter = ServicePrefixFormatter(SERVICE_PREFIX)
file_handler = logging.FileHandler(LOG_PATH)
file_handler.setFormatter(formatter)
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
logging.basicConfig(level=logging.INFO, handlers=[file_handler, stream_handler])

# Dedicated logger for unified_scan.log
unified_logger = logging.getLogger("unified_scan")
unified_file_handler = logging.FileHandler(UNIFIED_LOG_PATH)
unified_file_handler.setFormatter(formatter)
unified_logger.setLevel(logging.INFO)
if not unified_logger.hasHandlers():
    unified_logger.addHandler(unified_file_handler)

def log_basic(msg):
    logging.info(msg)

def log_unified(msg):
    unified_logger.info(msg)

def log_verbose(msg):
    if VERBOSE:
        logging.info(msg)


# âœ… Database connection helper with WAL mode and increased timeout
def get_db_connection():
    """
    Create a database connection with WAL mode for better concurrency.
    WAL mode allows multiple readers and one writer simultaneously.
    """
    conn = sqlite3.connect(DB_PATH, timeout=60.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")  # 60 seconds
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def save_to_db(track_data, max_retries=3):
    """Save track data to database with retry logic for handling locks."""
    for attempt in range(max_retries):
        try:
            log_verbose(f"Attempting to get DB connection for save_to_db (attempt {attempt+1})")
            conn = get_db_connection()
            cursor = conn.cursor()
            break
        except sqlite3.OperationalError as e:
            if "locked" in str(e) and attempt < max_retries - 1:
                log_verbose(f"Database locked, retrying ({attempt + 1}/{max_retries})...")
                time.sleep(0.5 * (attempt + 1))  # Exponential backoff
                continue
            log_verbose(f"Database error in save_to_db: {e}")
            raise
    else:
        log_verbose("Failed to acquire database connection after retries")
        logging.error("Failed to acquire database connection after retries")
        return

    # Preserve existing file path if the incoming payload does not provide one (Navidrome import is metadata-only)
    incoming_path = track_data.get("file_path")
    if not incoming_path:
        try:
            log_verbose(f"Fetching file_path and album_folder for track id {track_data['id']}")
            cursor.execute("SELECT file_path, album_folder FROM tracks WHERE id = ?", (track_data["id"],))
            row = cursor.fetchone()
            if row:
                if row[0]:
                    track_data["file_path"] = row[0]
                if row[1]:
                    track_data["album_folder"] = row[1]
        except Exception as e:
            log_verbose(f"Error fetching file_path/album_folder: {e}")
            pass

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
        "lastfm_track_playcount","lastfm_artist_playcount","file_path","album_folder",
        "duration","track_number","disc_number","year","album_artist","bitrate","sample_rate",
        "is_single","single_confidence","last_scanned",
        "mbid","suggested_mbid","suggested_mbid_confidence","single_sources",
        "is_spotify_single","spotify_total_tracks","spotify_album_type",
        "lastfm_ratio",
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
        track_data.get("album_folder",""),
        track_data.get("duration"),
        track_data.get("track_number"),
        track_data.get("disc_number"),
        track_data.get("year"),
        track_data.get("album_artist",""),
        track_data.get("bitrate"),
        track_data.get("sample_rate"),
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

    try:
        placeholders = ", ".join(["?"] * len(columns))
        sql = f"INSERT OR REPLACE INTO tracks ({', '.join(columns)}) VALUES ({placeholders})"
        log_verbose(f"Executing SQL: {sql} with values for track id {track_data['id']}")
        cursor.execute(sql, values)
        conn.commit()
        log_verbose(f"Track {track_data['id']} committed to DB.")
    except sqlite3.OperationalError as e:
        if "locked" in str(e):
            log_verbose(f"Database locked during save for track {track_data.get('id')}, will retry on next scan")
            logging.warning(f"Database locked during save for track {track_data.get('id')}, will retry on next scan")
        else:
            log_verbose(f"Database error saving track {track_data.get('id')}: {e}")
            logging.error(f"Database error saving track {track_data.get('id')}: {e}")
        raise
    finally:
        conn.close()
        log_verbose(f"DB connection closed for track {track_data['id']}")


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



# Import single detection helpers from single_detector.py
from single_detector import get_current_single_detection


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
    # Import here to avoid circular dependency
    from singledetection import _has_official_on_release_top
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
        log_unified(f"Essential playlist created for '{artist}' (5â˜… essentials)")
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
        log_unified(f"Essential playlist created for '{artist}' (top 10% by rating)")
        return

    log_unified(
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
    
    # Persist to database with retry logic
    max_retries = 3
    for attempt in range(max_retries):
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            for artist_name, info in artist_map_from_api.items():
                artist_id = info.get("id")
                cursor.execute("""
                    INSERT OR REPLACE INTO artist_stats (artist_id, artist_name, album_count, track_count, last_updated)
                    VALUES (?, ?, ?, ?, ?)
                """, (artist_id, artist_name, 0, 0, None))
                if verbose:
                    print(f"   📝 Added artist to index: {artist_name} (ID: {artist_id})")
                    logging.info(f"Added artist to index: {artist_name} (ID: {artist_id})")
            conn.commit()
            conn.close()
            break  # Success, exit retry loop
        except sqlite3.OperationalError as e:
            if "locked" in str(e) and attempt < max_retries - 1:
                logging.debug(f"Database locked during artist index build, retrying ({attempt + 1}/{max_retries})...")
                time.sleep(1.0 * (attempt + 1))  # Exponential backoff
                continue
            else:
                logging.error(f"Failed to build artist index after {max_retries} attempts: {e}")
                raise
    
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
    
    def _safe_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    print("ðŸ”Ž Scanning Navidrome library into local DB...")
    artist_map_local = build_artist_index(verbose=verbose) or {}
    if not artist_map_local:
        print("âš ï¸ No artists available from Navidrome; aborting library scan.")
        return


    def scan_artist_to_db(artist_name: str, artist_id: str, verbose: bool = False, force: bool = False):
        """Scan a single artist from Navidrome and persist tracks to DB."""
        try:
            # Prefetch cached track IDs for this artist
            existing_track_ids: set[str] = set()
            existing_album_tracks: dict[str, set[str]] = {}
            try:
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("SELECT album, id FROM tracks WHERE artist = ?", (artist_name,))
                for alb_name, tid in cursor.fetchall():
                    existing_track_ids.add(tid)
                    existing_album_tracks.setdefault(alb_name, set()).add(tid)
                conn.close()
            except Exception as e:
                logging.debug(f"Prefetch existing tracks for artist '{artist_name}' failed: {e}")

            albums = fetch_artist_albums(artist_id)
            if verbose:
                print(f"\ud83c\udfa8 Scanning artist: {artist_name} ({len(albums)} albums)")
                logging.info(f"Scanning artist {artist_name} ({len(albums)} albums)")

            for alb in albums:
                album_name = alb.get("name") or ""
                album_id = alb.get("id")
                if not album_id:
                    continue

                try:
                    tracks = fetch_album_tracks(album_id)
                except Exception as e:
                    logging.debug(f"Failed to fetch tracks for album '{album_name}': {e}")
                    tracks = []

                cached_ids_for_album = existing_album_tracks.get(album_name, set())
                if not force and tracks and len(cached_ids_for_album) >= len(tracks):
                    if verbose:
                        print(f"   \u23e9 Skipping cached album: {album_name}")
                    continue

                for t in tracks:
                    track_id = t.get("id")
                    if not track_id:
                        continue

                    # Get current single detection state to preserve user edits during Navidrome sync
                    current_single = get_current_single_detection(track_id)

                    td = {
                        "id": track_id,
                        "title": t.get("title", ""),
                        "album": album_name,
                        "artist": artist_name,
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
                        "spotify_release_date": t.get("year", "") or "",
                        "spotify_album_art_url": "",
                        "lastfm_track_playcount": 0,
                        "file_path": t.get("path", ""),
                        "last_scanned": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                        "spotify_album_type": "",
                        "spotify_total_tracks": 0,
                        "spotify_id": None,
                        "is_spotify_single": False,
                        "is_single": current_single["is_single"],  # Preserve user edits
                        "single_confidence": current_single["single_confidence"],  # Preserve user edits
                        "single_sources": current_single["single_sources"],  # Preserve user edits
                        "stars": int(t.get("userRating", 0) or 0),
                        "mbid": t.get("mbid", "") or "",
                        "suggested_mbid": "",
                        "suggested_mbid_confidence": 0.0,
                        "duration": t.get("duration"),
                        "track_number": t.get("track"),
                        "disc_number": t.get("discNumber"),
                        "year": t.get("year"),
                        "album_artist": t.get("albumArtist", ""),
                        "bitrate": t.get("bitRate"),
                        "sample_rate": t.get("samplingRate"),
                    }
                    save_to_db(td)

            if verbose:
                print(f"\u2705 Artist scan complete: {artist_name}")
                logging.info(f"Artist scan complete: {artist_name}")
        except Exception as e:
            logging.error(f"scan_artist_to_db failed for {artist_name}: {e}")
            raise

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
        logging.debug(f"Processing artist {artist_count}/{total_artists}: {name} (ID: {artist_id})")

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
                logging.debug(f"Found {len(albums)} albums for artist '{name}'")
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
            
            print(f"   ðŸ“€ [{album_count}/{len(albums)}] Album: {album_name}")
            logging.debug(f"Scanning album {album_count}/{len(albums)}: {album_name}")
            
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
                    "stars": int(t.get("userRating", 0) or 0),
                    # Enhanced metadata from Navidrome for better matching
                    # Normalize track/disc numbers from Navidrome (trackNumber/track, discNumber/disc)
                    "duration": t.get("duration"),  # Track duration in seconds
                    "track_number": _safe_int(t.get("trackNumber") or t.get("track")),
                    "disc_number": _safe_int(t.get("discNumber") or t.get("disc") or 1) or 1,
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

    # Navidrome import should not trigger singles detection/rating
    if os.environ.get("SPTNR_SKIP_SINGLES") == "1":
        logging.info(f"Skipping singles/rating for {artist_name} (Navidrome import-only mode)")
        return []

    # Import helpers from singledetection module
    from singledetection import infer_album_context
    from single_detector import get_current_single_detection, rate_track_single_detection
    
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
        
        if not album_id:
            continue
            
        if verbose:
            print(f"\n   💿 Album: {album_name}")
        
        # Fetch album tracks
        try:
            album_tracks = fetch_album_tracks(album_id)
        except Exception as e:
            logging.error(f"Failed to fetch tracks for album '{album_name}': {e}")
            continue
            
        if not album_tracks:
            if verbose:
                print(f"      ⚠️ No tracks found for album '{album_name}'")
            continue
        
        # Check if we should skip this album (recently scanned)
        skip_album = False
        if not force and ALBUM_SKIP_DAYS > 0 and len(album_tracks) >= ALBUM_SKIP_MIN_TRACKS:
            try:
                album_track_count = get_album_track_count_in_db(artist_name, album_name)
                if album_track_count >= len(album_tracks):
                    # Album already has all tracks, check when it was last scanned
                    conn = get_db_connection()
                    cursor = conn.cursor()
                    cursor.execute("""
                        SELECT MAX(last_scanned) FROM tracks 
                        WHERE artist = ? AND album = ?
                    """, (artist_name, album_name))
                    last_scanned = cursor.fetchone()[0]
                    conn.close()
                    
                    if last_scanned:
                        last_scan_date = parse_datetime_flexible(last_scanned)
                        if datetime.now() - last_scan_date < timedelta(days=ALBUM_SKIP_DAYS):
                            skip_album = True
                            if verbose:
                                print(f"      ⏩ Skipping recently scanned album (last scan: {last_scan_date.strftime('%Y-%m-%d')})")
            except Exception as e:
                logging.debug(f"Error checking album skip status: {e}")
        
        if skip_album:
            continue
        
        # Infer album context (is_live, is_unplugged, etc.)
        album_ctx = infer_album_context(album_name)
        
        # Collect popularity scores for all tracks in the album
        for t in album_tracks:
            track_id = t.get("id", "")
            title = t.get("title", "")
            
            # Initialize track dict
            t["artist_name"] = artist_name
            t["album_name"] = album_name
            t["spotify_score"] = 0
            t["lastfm_score"] = 0
            t["listenbrainz_score"] = 0
            t["age_score"] = 0
            t["score"] = 0.0
            t["stars"] = 1
            t["is_single"] = False
            t["single_sources"] = []
            t["single_confidence"] = "low"
            
            # Get Spotify popularity
            try:
                spotify_results = search_spotify_track(title, artist_name, album_name)
                if spotify_results:
                    best_match = select_best_spotify_match(spotify_results, title, album_ctx)
                    t["spotify_score"] = best_match.get("popularity", 0)
                    t["spotify_popularity"] = best_match.get("popularity", 0)
                    
                    # Check if it's a Spotify single
                    spotify_album = best_match.get("album", {})
                    if spotify_album.get("album_type") == "single":
                        t["is_spotify_single"] = True
                        t["spotify_album_type"] = "single"
                    t["spotify_total_tracks"] = spotify_album.get("total_tracks", 0)
            except Exception as e:
                if verbose:
                    logging.debug(f"Spotify lookup failed for '{title}': {e}")
            
            # Get Last.fm popularity
            try:
                lastfm_info = get_lastfm_track_info(artist_name, title)
                if lastfm_info and lastfm_info.get("track_play"):
                    # Normalize Last.fm playcount to 0-100 scale
                    lastfm_playcount = int(lastfm_info["track_play"])
                    t["lastfm_score"] = min(100, lastfm_playcount // 1000)  # 1000 plays = 1 point
                    t["lastfm_track_playcount"] = lastfm_playcount
                    # Store ratio for adaptive weighting
                    t["lastfm_ratio"] = t["lastfm_score"]
            except Exception as e:
                if verbose:
                    logging.debug(f"Last.fm lookup failed for '{title}': {e}")
            
            # Get ListenBrainz score
            try:
                lb_score = get_listenbrainz_score(artist_name, title)
                if lb_score:
                    t["listenbrainz_score"] = lb_score
            except Exception as e:
                if verbose:
                    logging.debug(f"ListenBrainz lookup failed for '{title}': {e}")
            
            # Calculate age score if we have release date
            if t.get("year"):
                t["age_score"] = score_by_age(t.get("year"))
        
        # Normalize scores by album median
        # Collect all non-zero scores
        spotify_scores = [t.get("spotify_score", 0) for t in album_tracks if t.get("spotify_score", 0) > 0]
        lastfm_scores = [t.get("lastfm_score", 0) for t in album_tracks if t.get("lastfm_score", 0) > 0]
        lb_scores = [t.get("listenbrainz_score", 0) for t in album_tracks if t.get("listenbrainz_score", 0) > 0]
        
        # Calculate medians for normalization
        from statistics import median
        spotify_median = median(spotify_scores) if spotify_scores else 50
        lastfm_median = median(lastfm_scores) if lastfm_scores else 50
        lb_median = median(lb_scores) if lb_scores else 50
        
        # Compute adaptive weights per album
        base_weights = {
            'spotify': SPOTIFY_WEIGHT,
            'lastfm': LASTFM_WEIGHT,
            'listenbrainz': LISTENBRAINZ_WEIGHT
        }
        adaptive_weights = compute_adaptive_weights(album_tracks, base_weights, clamp=(CLAMP_MIN, CLAMP_MAX))
        
        # Calculate final scores with adaptive weights
        for t in album_tracks:
            spotify_score = t.get("spotify_score", 0)
            lastfm_score = t.get("lastfm_score", 0)
            lb_score = t.get("listenbrainz_score", 0)
            age_score = t.get("age_score", 0)
            
            # Normalize by album median, then weight
            spotify_norm = (spotify_score / spotify_median) if spotify_median > 0 else 0
            lastfm_norm = (lastfm_score / lastfm_median) if lastfm_median > 0 else 0
            lb_norm = (lb_score / lb_median) if lb_median > 0 else 0
            
            # Calculate weighted final score
            final_score = (
                spotify_norm * adaptive_weights['spotify'] +
                lastfm_norm * adaptive_weights['lastfm'] +
                lb_norm * adaptive_weights['listenbrainz'] +
                age_score * AGE_WEIGHT
            )
            
            t["score"] = final_score
            t["adaptive_weight_spotify"] = adaptive_weights['spotify']
            t["adaptive_weight_lastfm"] = adaptive_weights['lastfm']
            t["adaptive_weight_listenbrainz"] = adaptive_weights['listenbrainz']
            t["album_median_score"] = spotify_median  # Store for reference
        
        # Single detection for each track
        single_sources_list = []
        for t in album_tracks:
            result = rate_track_single_detection(
                track=t,
                artist_name=artist_name,
                album_ctx=album_ctx,
                config={"features": config.get("features", {})},
                title_sim_threshold=TITLE_SIM_THRESHOLD,
                count_short_release_as_match=COUNT_SHORT_RELEASE_AS_MATCH,
                use_lastfm_single=use_lastfm_single,
                verbose=verbose
            )
            
            # Track sources for this track's single detection
            if t.get("is_single"):
                sources_used = t.get("single_sources", [])
                if sources_used:
                    single_sources_list.extend(sources_used)
        
        # Assign stars based on is_single flag and scores
        # Singles get automatic 5 stars
        for t in album_tracks:
            if t.get("is_single"):
                t["stars"] = 5
            else:
                # Assign stars based on z-score bands relative to album
                # Calculate z-score for non-singles
                album_scores = [track.get("score", 0) for track in album_tracks]
                track_score = t.get("score", 0)
                
                # Simple percentile-based star assignment
                sorted_scores = sorted(album_scores, reverse=True)
                if track_score in sorted_scores:
                    percentile = sorted_scores.index(track_score) / len(sorted_scores) if sorted_scores else 0.5
                    
                    if percentile < 0.1:  # Top 10%
                        t["stars"] = 4
                    elif percentile < 0.3:  # Top 30%
                        t["stars"] = 3
                    elif percentile < 0.7:  # Top 70%
                        t["stars"] = 2
                    else:
                        t["stars"] = 1
        
        # Save all tracks to database
        for t in album_tracks:
            t["last_scanned"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            try:
                save_to_db(t)
            except Exception as e:
                logging.error(f"Failed to save track '{t.get('title', '')}': {e}")
        
        # Collect unique sources used for this album
        unique_sources = list(set(single_sources_list))
        source_str = ", ".join(unique_sources) if unique_sources else "None"
        
        # Log singles detection scan for this album
        log_album_scan(artist_name, album_name, 'singles', len(album_tracks), 'completed', source_str)
        
        # Update rated_map
        rated_map.update({t["id"]: t for t in album_tracks})
        
        if verbose:
            singles_count = sum(1 for t in album_tracks if t.get("is_single"))
            print(f"      ✓ Processed {len(album_tracks)} tracks, {singles_count} singles detected")
    
    # --------------------------------------------------------------------------
    # Essential Artist Playlist Creation (after all albums processed)
    # --------------------------------------------------------------------------
    try:
        # Collect all tracks for this artist from database to create playlist
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, title, stars, score FROM tracks 
            WHERE artist = ?
        """, (artist_name,))
        all_tracks = cursor.fetchall()
        conn.close()
        
        # Convert to list of dicts for create_or_update_playlist_for_artist
        tracks_list = [{"id": t[0], "title": t[1], "stars": t[2], "score": t[3]} for t in all_tracks]
        
        # Use existing function to create/update Essential playlist
        create_or_update_playlist_for_artist(artist_name, tracks_list)
        
        # Log the result
        five_star_count = sum(1 for t in tracks_list if t.get("stars") == 5)
        if five_star_count >= 10:
            if verbose:
                print(f"\n   ⭐ Created 'Essential {artist_name}' playlist ({five_star_count} five-star songs)")
        elif len(tracks_list) >= 100:
            if verbose:
                print(f"\n   ⭐ Created 'Essential {artist_name}' playlist (top 10% by rating)")
        else:
            if verbose:
                print(f"\n   ℹ️ Not enough tracks for Essential playlist ({len(tracks_list)} total, {five_star_count} five-star)")
    except Exception as e:
        logging.error(f"Failed to create Essential playlist for {artist_name}: {e}")
    
    if aux_pool:
        aux_pool.shutdown(wait=False)

    print(f"✅ Finished rating for artist: {artist_name}")
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
    # Log scan start to unified log
    log_unified(f"🟢 SPTNR scan started: type={scan_type}, time={datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
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
        # add a console handler if none exists
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

            log_unified(f"🎤 Starting rating for artist: {name} (ID: {artist_info['id']}) at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            rated = rate_artist(artist_info['id'], name, verbose=verbose, force=force)
            print(f"âœ… Completed rating for {name}. Tracks rated: {len(rated)}")
            log_unified(f"🎤 Completed rating for artist: {name} (ID: {artist_info['id']}) at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}. Tracks rated: {len(rated)}")

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
                    (datetime.now() - parse_datetime_flexible(artist_info['last_updated'])).days > 7
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
    # Log scan completion to unified log
    log_unified(f"✅ SPTNR scan complete: type={scan_type}, time={datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


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



# ...existing code...

