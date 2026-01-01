#!/usr/bin/env python3
# 🎧 SPTNR – Navidrome Rating CLI with Spotify + Last.fm + Navidrome API Integration

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
from difflib import SequenceMatcher
from datetime import datetime, timedelta
from statistics import median
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

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
            "refresh_playlists_on_start": False,
            "refresh_artist_index_on_start": False,
            "discogs_min_interval_sec": 0.35,  # keeps throttle consistent when unset
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

# ✅ Merge defaults with existing config to avoid KeyErrors
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
    "artist": []
}

config.setdefault("features", {})
config["features"] = {**default_features, **config["features"]}  # existing values win

# Context gate toggles (strict by default)
CONTEXT_GATE = bool(config["features"].get("single_context_gate_live", True))
CONTEXT_FALLBACK_STUDIO = bool(config["features"].get("single_context_fallback_studio", False))

# ✅ Extract feature flags
dry_run = config["features"]["dry_run"]
sync = config["features"]["sync"]
force = config["features"]["force"]
verbose = config["features"]["verbose"]
perpetual = config["features"]["perpetual"]
batchrate = config["features"]["batchrate"]
artist_list = config["features"]["artist"]

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

    # --- Spotify ---
    if config["spotify"].get("client_id") in ["your_spotify_client_id", "", None]:
        issues.append("Spotify Client ID is missing or placeholder.")
    if config["spotify"].get("client_secret") in ["your_spotify_client_secret", "", None]:
        issues.append("Spotify Client Secret is missing or placeholder.")

    # --- Last.fm ---
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
                time.sleep(60)
        except KeyboardInterrupt:
            print("\nℹ️ Exiting script.")
            sys.exit(0)

# ✅ Call this right after loading config
validate_config(config)

# ✅ Extract credentials and settings
NAV_USERS = config.get("navidrome_users", [])

_primary_user = get_primary_nav_user(config) or {"base_url": "", "user": "", "pass": ""}

NAV_BASE_URL = _primary_user.get("base_url", "")
USERNAME     = _primary_user.get("user", "")
PASSWORD     = _primary_user.get("pass", "")

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
        "is_spotify_single","spotify_total_tracks","spotify_album_type","lastfm_ratio",
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
        float(track_data.get("lastfm_ratio",0.0) or 0.0),
    ]

    placeholders = ", ".join(["?"] * len(columns))
    sql = f"INSERT OR REPLACE INTO tracks ({', '.join(columns)}) VALUES ({placeholders})"
    cursor.execute(sql, values)
    conn.commit()
    conn.close()

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
            s = requests.Session()
            retry = Retry(
                total=5, connect=5, read=5,
                backoff_factor=1.2,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset(["GET", "POST"])
            )
            s.mount("https://", HTTPAdapter(max_retries=retry))
            s.mount("http://",  HTTPAdapter(max_retries=retry))
            _spotify_session = s
        return _spotify_session

# --- Spotify caches for artist lookups & singles ---
_SPOTIFY_ARTIST_ID_CACHE: dict[str, str] = {}
_SPOTIFY_ARTIST_SINGLES_CACHE: dict[str, set[str]] = {}

def get_spotify_artist_id(artist_name: str) -> str | None:
    """Search for the artist and cache ID."""
    key = (artist_name or "").strip().lower()
    if key in _SPOTIFY_ARTIST_ID_CACHE:
        return _SPOTIFY_ARTIST_ID_CACHE[key]
    try:
        sess = _get_spotify_session()
        params = {"q": f'artist:"{artist_name}"', "type": "artist", "limit": 1}
        r = sess.get("https://api.spotify.com/v1/search", headers=_spotify_headers(), params=params, timeout=10)
        r.raise_for_status()
        items = r.json().get("artists", {}).get("items", [])
        if items:
            aid = items[0].get("id")
            _SPOTIFY_ARTIST_ID_CACHE[key] = aid
            return aid
    except Exception as e:
        logging.debug(f"Spotify artist search failed for '{artist_name}': {e}")
    return None

def get_spotify_artist_single_track_ids(artist_id: str) -> set[str]:
    """
    Fetch all track IDs from releases where include_groups=single for the artist.
    Cached per artist_id for the lifetime of the process.
    """
    if not artist_id:
        return set()
    if artist_id in _SPOTIFY_ARTIST_SINGLES_CACHE:
        return _SPOTIFY_ARTIST_SINGLES_CACHE[artist_id]
    sess = _get_spotify_session()
    headers = _spotify_headers()
    singles_album_ids: list[str] = []
    # Paginate artist albums filtered to singles
    url = f"https://api.spotify.com/v1/artists/{artist_id}/albums"
    params = {"include_groups": "single", "limit": 50}
    try:
        while True:
            ra = sess.get(url, headers=headers, params=params, timeout=12)
            ra.raise_for_status()
            j = ra.json()
            singles_album_ids.extend([a.get("id") for a in j.get("items", []) if a.get("id")])
            next_url = j.get("next")
            if next_url:
                url, params = next_url, None  # 'next' already contains the query
            else:
                break
    except Exception as e:
        logging.debug(f"Spotify artist singles album fetch failed for '{artist_id}': {e}")
    # Fetch tracks for each single album (bounded concurrency)
    single_track_ids: set[str] = set()

    def _album_tracks(album_id: str) -> list[str]:
        try:
            rt = sess.get(f"https://api.spotify.com/v1/albums/{album_id}/tracks",
                          headers=headers, params={"limit": 50}, timeout=12)
            rt.raise_for_status()
            return [t.get("id") for t in (rt.json().get("items") or []) if t.get("id")]
        except Exception:
            return []
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_album_tracks, aid) for aid in singles_album_ids[:250]]  # safety cap
        for f in futures:
            for tid in (f.result() or []):
                single_track_ids.add(tid)
    _SPOTIFY_ARTIST_SINGLES_CACHE[artist_id] = single_track_ids
    return single_track_ids

def search_spotify_track(title, artist, album=None):
    sess = _get_spotify_session()  # your shared Session with retries
    def query(q):
        params = {"q": q, "type": "track", "limit": 10}
        res = sess.get("https://api.spotify.com/v1/search",
                       headers=_spotify_headers(), params=params, timeout=10)
        res.raise_for_status()
        return res.json().get("tracks", {}).get("items", []) or []
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

# --- Discogs call hygiene: global session with retry/backoff ---
_discogs_session = None
_discogs_lock = threading.Lock()

def _get_discogs_session():
    """Return a shared requests.Session with sensible retries and backoff."""
    global _discogs_session
    with _discogs_lock:
        if _discogs_session is None:
            s = requests.Session()
            # Retry on common transient status codes including 429 when allowed
            retry = Retry(
                total=5,
                connect=5,
                read=5,
                backoff_factor=1.2,            # exponential: 1.2, 2.4, 4.8...
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset(["GET", "POST"])
            )
            s.mount("https://", HTTPAdapter(max_retries=retry))
            s.mount("http://", HTTPAdapter(max_retries=retry))
            _discogs_session = s
        return _discogs_session

# --- Simple RPM throttle (authenticated limit is generous, but be safe) ---
_last_call_ts = 0.0
_min_interval_sec = float(config.get("features", {}).get("discogs_min_interval_sec", 0.35))
# 0.35s ~ 171 req/min theoretical max; adjust to 1.0s if you still see 429s
_discogs_throttle_lock = threading.Lock()

def _throttle_discogs():
    """Sleep briefly between Discogs calls to avoid 429s (thread-safe)."""
    global _last_call_ts
    now = time.time()
    with _discogs_throttle_lock:
        wait = _min_interval_sec - (now - _last_call_ts)
        if wait > 0:
            time.sleep(wait)
        _last_call_ts = time.time()

def _respect_retry_after(resp):
    """If Discogs returns Retry-After, sleep that amount."""
    ra = resp.headers.get("Retry-After")
    if ra:
        try:
            delay = float(ra)
            time.sleep(min(delay, 10.0))  # cap at 10s per call
        except Exception:
            pass
            
# --- Discogs "Official Video / Official Lyric Video" signal (with cache) ---
# Very small in-memory cache to reduce Discogs API calls during batch runs.
# Keyed by normalized (artist, title, context_key) -> result dict.
_DISCOGS_VID_CACHE: dict[tuple[str, str, str], dict] = {}

def _strip_video_noise(s: str) -> str:
    """
    Remove common boilerplate to improve title matching:
      - 'official music video', 'official video', 'music video', 'hd', '4k', 'uhd', 'remastered'
      - bracketed content [..], (..), {..}
      - normalize 'feat.' / 'ft.' to 'feat '
    Returns a canonicalized string via _canon.
    """
    s = (s or "").lower()
    noise_phrases = [
        "official music video", "official video", "music video",
        "hd", "4k", "uhd", "remastered", "lyrics", "lyric video",
        "audio", "visualizer"
    ]
    for p in noise_phrases:
        s = s.replace(p, " ")
    # Drop bracketed content
    s = re.sub(r"\[.*?\]|\(.*?\)|\{.*?\}", " ", s)
    # Normalize common abbreviations
    s = s.replace("feat.", "feat ").replace("ft.", "feat ")
    return _canon(s)

def _has_official(vt_raw: str, vd_raw: str, allow_lyric: bool = True) -> bool:
    """Require 'official' in title/description; optionally accept 'lyric' as official."""
    t = (vt_raw or "").lower()
    d = (vd_raw or "").lower()
    if ("official" in t) or ("official" in d):
        return True
    return allow_lyric and (("lyric" in t) or ("lyric" in d))

def infer_album_context(album_title: str, release_types: list[str] | None = None) -> dict:
    """
    Infer album context flags (live/unplugged) from album title and optional release_types.
    - release_types can be Discogs-like list: ["Album", "Live"]
    """
    t = (album_title or "").lower()
    types_norm = [(rt or "").lower() for rt in (release_types or [])]
    is_unplugged = ("unplugged" in t) or ("mtv unplugged" in t)
    is_live = is_unplugged or ("live" in t) or ("live" in types_norm)
    return {
        "is_live": is_live,
        "is_unplugged": is_unplugged,
        "title": album_title,
        "raw_types": types_norm,
    }

def _banned_flavor(vt_raw: str, vd_raw: str, *, allow_live: bool = False) -> bool:
    """
    Reject 'live' and 'remix' unless allow_live=True.
    Radio edits are allowed (without 'remix').
    """
    t = (vt_raw or "").lower()
    d = (vd_raw or "").lower()

    # Live only banned when album context doesn't allow it
    if (not allow_live) and ("live" in t or "live" in d):
        return True

    # 'remix' anywhere is banned; radio edits allowed if no 'remix'
    if "remix" in t or "remix" in d:
        return True

    return False

def discogs_official_video_signal(
    title: str,
    artist: str,
    *,
    discogs_token: str,
    timeout: int = 10,
    per_page: int = 10,
    min_ratio: float = 0.55,
    allow_lyric_as_official: bool = True,
    album_context: dict | None = None,
    permissive_fallback: bool = False,
) -> dict:
    """
    Detect an 'official' (or 'lyric' if allowed) video for a track on Discogs,
    honoring album context (live/unplugged), with:
      - Candidate shortlist (title similarity + 'Single' OR 'Album' hint),
      - Parallel inspections (bounded executor),
      - Early bailouts and caching.
    Returns (on success):
      {"match": True, "uri": <video_url>, "release_id": <id>, "ratio": <float>, "why": "discogs_official_video"}
    Dependencies already present in your codebase:
      - _get_discogs_session, _throttle_discogs, _respect_retry_after
      - _strip_video_noise, _canon, strip_parentheses
      - infer_album_context (for context flags), CONTEXT_GATE, CONTEXT_FALLBACK_STUDIO
      - _DEF_USER_AGENT
    """
    # ---- Basic token check ---------------------------------------------------
    if not discogs_token:
        return {"match": False, "uri": None, "release_id": None, "ratio": None, "why": "no_token"}
    # ---- Context gate --------------------------------------------------------
    allow_live_ctx = bool(album_context and (album_context.get("is_live") or album_context.get("is_unplugged")))
    context_key = "live" if allow_live_ctx else "studio"
    cache_key = (_canon(artist), _canon(title), context_key)
    # Fast cache path
    cached = _DISCOGS_VID_CACHE.get(cache_key)
    if cached is not None:
        return cached
    # ---- Setup ---------------------------------------------------------------
    session = _get_discogs_session()
    headers = {"Authorization": f"Discogs token={discogs_token}", "User-Agent": _DEF_USER_AGENT}
    nav_title_raw = strip_parentheses(title)
    nav_title_clean = _strip_video_noise(nav_title_raw)
    nav_title = _canon(nav_title_raw)  # canonical for shortlist similarity
    # Context rules
    require_live = allow_live_ctx and CONTEXT_GATE
    forbid_live  = (not allow_live_ctx) and CONTEXT_GATE
    allow_live_for_video = allow_live_ctx  # allow 'live' in video title only if album context is live

    # ---- Small helpers -------------------------------------------------------
    def _search(kind: str) -> list:
        """Discogs database search (with throttle & retries)."""
        try:
            _throttle_discogs()
            resp = session.get(
                "https://api.discogs.com/database/search",
                headers=headers,
                params={"q": f"{artist} {title}", "type": kind, "per_page": per_page},
                timeout=timeout,
            )
            if resp.status_code == 429:
                _respect_retry_after(resp)
            resp.raise_for_status()
            return resp.json().get("results", []) or []
        except Exception as e:
            logging.debug(f"Discogs {kind} search failed for '{title}': {e}")
            return []

    def _release_context_compatible(rel_json: dict, *, require_live: bool, forbid_live: bool) -> bool:
        """
        Decide if a release is compatible with the album context.
        Signals for 'live/unplugged':
          - 'Live' in format descriptions,
          - 'Unplugged' / 'MTV Unplugged' in title,
          - 'Recorded live' / 'Unplugged' in notes.
        """
        title_l = (rel_json.get("title") or "").lower()
        notes_l = (rel_json.get("notes") or "").lower()
        formats = rel_json.get("formats") or []
        tags = {d.lower() for f in formats for d in (f.get("descriptions") or [])}

        has_live_signal = (
            ("live" in tags) or ("unplugged" in title_l) or ("mtv unplugged" in title_l) or
            ("recorded live" in notes_l) or ("unplugged" in notes_l)
        )

        if require_live and not has_live_signal:
            return False
        if forbid_live and has_live_signal:
            return False
        return True

    def _inspect_release(rel_id: int, *, require_live: bool, forbid_live: bool, allow_live_for_video: bool) -> dict | None:
        """
        Pull the release, apply context compatibility, then scan videos:
          - Require 'official' (or 'lyric' if allowed),
          - Ban 'remix' always; ban 'live' unless album context allows it,
          - Title/description similarity >= min_ratio against cleaned nav title.
        """
        try:
            _throttle_discogs()
            r = session.get(f"https://api.discogs.com/releases/{rel_id}", headers=headers, timeout=timeout)
            if r.status_code == 429:
                _respect_retry_after(r)
            r.raise_for_status()
            data = r.json()
        except Exception:
            return None
        if not _release_context_compatible(data, require_live=require_live, forbid_live=forbid_live):
            return None
        best = None
        for v in (data.get("videos") or []):
            vt_raw = v.get("title", "") or ""
            vd_raw = v.get("description", "") or ""

            # official/lyric requirement
            t_l = vt_raw.lower(); d_l = vd_raw.lower()
            if ("official" not in t_l and "official" not in d_l):
                if not allow_lyric_as_official or ("lyric" not in t_l and "lyric" not in d_l):
                    continue
            # flavor bans (live only allowed for live context; remix always banned)
            def _banned_flavor(vt_raw: str, vd_raw: str, *, allow_live: bool = False) -> bool:
                t = (vt_raw or "").lower()
                d = (vd_raw or "").lower()
                if (not allow_live) and ("live" in t or "live" in d):
                    return True
                if "remix" in t or "remix" in d:
                    return True
                return False

            if _banned_flavor(vt_raw, vd_raw, allow_live=allow_live_for_video):
                continue
            # similarity check vs. cleaned nav title
            vt_clean = _strip_video_noise(vt_raw)
            vd_clean = _strip_video_noise(vd_raw)
            ratio = max(
                difflib.SequenceMatcher(None, vt_clean, nav_title_clean).ratio(),
                difflib.SequenceMatcher(None, vd_clean, nav_title_clean).ratio(),
            )
            if ratio >= min_ratio:
                current = {
                    "match": True,
                    "uri": v.get("uri"),
                    "release_id": rel_id,
                    "ratio": round(ratio, 3),
                    "why": "discogs_official_video",
                }
                if best is None or current["ratio"] > best["ratio"]:
                    best = current

        return best
    # ---- Release search & shortlist (REVISED) --------------------------------
    results = _search("release")
    if not results:
        res = {"match": False, "uri": None, "release_id": None, "ratio": None, "why": "no_video_match"}
        _DISCOGS_VID_CACHE[cache_key] = res
        return res
    cands: list[tuple[int, bool, float]] = []
    for r in results[:15]:  # inspect a few more results; still bounded
        rid = r.get("id")
        if not rid:
            continue
        rel_title = _canon(r.get("title", ""))
        title_ratio = difflib.SequenceMatcher(None, rel_title, nav_title).ratio()
        # 'format' hint list may include entries like ['CD', 'Album'] or ['VHS', 'Promo']
        formats_hint = r.get("format", []) or []
        fmt_norm = [(fmt or "").lower() for fmt in formats_hint]
        prefer_single  = any("single" in f for f in fmt_norm)
        is_album_like  = any("album" in f for f in fmt_norm)
        # Keep candidate if:
        #  - it's an obvious SINGLE, OR
        #  - release title is reasonably similar to the track title, OR
        #  - it's an ALBUM (many official videos live on the album's Discogs page)
        keep = prefer_single or (title_ratio >= 0.65) or is_album_like
        if keep:
            cands.append((rid, prefer_single, title_ratio))
    # Prefer singles; otherwise title similarity. Cap to 8 to keep requests sane.
    cands = sorted(cands, key=lambda x: (not x[1], -x[2]))[:8]
    # ---- Parallel inspections of shortlist ----------------------------------
    best = None
    from concurrent.futures import ThreadPoolExecutor
    if cands:
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [
                pool.submit(
                    _inspect_release,
                    rid,
                    require_live=require_live,
                    forbid_live=forbid_live,
                    allow_live_for_video=allow_live_for_video,
                )
                for rid, _, _ in cands
            ]
            for f in futures:
                hit = f.result()
                if hit and (best is None or hit["ratio"] > best["ratio"]):
                    best = hit
                    # Cancel remaining inspections to save time
                    for other in futures:
                        if other is not f:
                            try:
                                other.cancel()
                            except:
                                pass
                    break
    if best:
        _DISCOGS_VID_CACHE[cache_key] = best
        return best
    # ---- Optional permissive fallback (studio allowed if album is live) -----
    if allow_live_ctx and (permissive_fallback or CONTEXT_FALLBACK_STUDIO):
        relaxed_best = None
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    _inspect_release,
                    rid,
                    require_live=False,
                    forbid_live=False,
                    allow_live_for_video=False,
                )
                for rid, _, _ in cands
            ]
            for f in futures:
                hit = f.result()
                if hit and (relaxed_best is None or hit["ratio"] > relaxed_best["ratio"]):
                    relaxed_best = hit
                    for other in futures:
                        if other is not f:
                            try:
                                other.cancel()
                            except:
                                pass
                    break
        if relaxed_best:
            _DISCOGS_VID_CACHE[cache_key] = relaxed_best
            return relaxed_best
    # ---- No match -----------------------------------------------------------
    res = {"match": False, "uri": None, "release_id": None, "ratio": None, "why": "no_video_match"}
    _DISCOGS_VID_CACHE[cache_key] = res
    return res

# Cache for single detection
_DISCOGS_SINGLE_CACHE: dict[tuple[str, str, str], bool] = {}

def is_discogs_single(
    title: str,
    artist: str,
    *,
    album_context: dict | None = None,
    timeout: int = 10
) -> bool:
    """
    Discogs single detection (best-effort, rate-limit safe).

    Strong paths:
      - Explicit 'Single' in release formats
      - EP with first track == A-side AND an official video on the same release
      - (structural fallback) 1–2 track A/B sides where the matched title is present
        and not live/remix (medium path; confidence handled upstream)

    Respects live/unplugged album context via CONTEXT_GATE.
    Uses cache keyed by (artist,title,context).
    """
    # --- Fast exits / cache ---------------------------------------------------
    if not DISCOGS_TOKEN:
        return False
    allow_live_ctx = bool(album_context and (album_context.get("is_live") or album_context.get("is_unplugged")))
    context_key = "live" if allow_live_ctx else "studio"
    cache_key = (_canon(artist), _canon(title), context_key)
    if cache_key in _DISCOGS_SINGLE_CACHE:
        return _DISCOGS_SINGLE_CACHE[cache_key]

    # --- Local helpers (self-contained) --------------------------------------
    def _release_title_core(rel_title: str, artist_name: str) -> str:
        """
        Discogs search 'title' often looks like 'Artist - Title / B-side'.
        Keep only the 'Title' part for fair similarity vs. track title.
        """
        t = (rel_title or "").strip()
        parts = t.split(" - ", 1)
        if len(parts) == 2 and _canon(parts[0]) == _canon(artist_name):
            t = parts[1].strip()
        return t.split(" / ")[0].strip()

    def _is_variant_of(base: str, candidate: str) -> bool:
        """
        Treat instrumental/radio edit/remaster as benign; ban live/remix.
        """
        b = _canon(strip_parentheses(base)); c = _canon(candidate)
        if "live" in c or "remix" in c:
            return False
        ok = {"instrumental", "radio edit", "edit", "remaster"}
        return (b in c) or any(tok in c for tok in ok)

    def _has_official_on_release(data: dict, nav_title: str, *, allow_live: bool, min_ratio: float = 0.50) -> bool:
        """
        Inspect release.videos for an 'official' (or 'lyric') match of nav_title.
        """
        vids = data.get("videos") or []
        nav_clean = _strip_video_noise(nav_title)
        for v in vids:
            vt_raw = (v.get("title") or "")
            vd_raw = (v.get("description") or "")
            if not _has_official(vt_raw, vd_raw, allow_lyric=True):
                continue
            if _banned_flavor(vt_raw, vd_raw, allow_live=allow_live):
                continue
            vt = _strip_video_noise(vt_raw)
            vd = _strip_video_noise(vd_raw)
            r = max(SequenceMatcher(None, vt, nav_clean).ratio(),
                    SequenceMatcher(None, vd, nav_clean).ratio())
            if r >= min_ratio:
                return True
        return False

    def _release_context_compatible(rel_json: dict, *, require_live: bool, forbid_live: bool) -> bool:
        """
        Decide if a release is compatible with album context (live/unplugged).
        Recognises 'Live' in format descriptions, 'Unplugged'/'MTV Unplugged' in title/notes, etc.
        """
        title_l = (rel_json.get("title") or "").lower()
        notes_l = (rel_json.get("notes") or "").lower()
        formats = rel_json.get("formats") or []
        tags = {d.lower() for f in formats for d in (f.get("descriptions") or [])}
        has_live_signal = (
            ("live" in tags) or ("unplugged" in title_l) or ("mtv unplugged" in title_l) or
            ("recorded live" in notes_l) or ("unplugged" in notes_l)
        )
        if require_live and not has_live_signal:
            return False
        if forbid_live and has_live_signal:
            return False
        return True
    # --- Setup & search -------------------------------------------------------
    headers = {
        "Authorization": f"Discogs token={DISCOGS_TOKEN}",
        "User-Agent": _DEF_USER_AGENT,
    }
    session = _get_discogs_session()
    nav_title = _canon(strip_parentheses(title))
    require_live = allow_live_ctx and CONTEXT_GATE
    forbid_live  = (not allow_live_ctx) and CONTEXT_GATE
    params = {"q": f"{artist} {title}", "type": "release", "per_page": 15}
    try:
        _throttle_discogs()
        res = session.get("https://api.discogs.com/database/search", headers=headers, params=params, timeout=timeout)
        if res.status_code == 429:
            _respect_retry_after(res)
        res.raise_for_status()
    except Exception:
        _DISCOGS_SINGLE_CACHE[cache_key] = False
        return False
    results = res.json().get("results", []) or []
    if not results:
        _DISCOGS_SINGLE_CACHE[cache_key] = False
        return False
    # --- Shortlist: prefer 'Single' formats; otherwise core-title similarity ---
    cands: list[tuple[int, bool, float]] = []
    for r in results[:15]:
        rid = r.get("id")
        if not rid:
            continue
        rel_title_raw  = r.get("title", "")
        rel_title_core = _release_title_core(rel_title_raw, artist)
        rel_title      = _canon(rel_title_core)
        title_ratio    = difflib.SequenceMatcher(None, rel_title, nav_title).ratio()
        formats_hint = r.get("format", []) or []
        fmt_norm     = [(fmt or "").lower() for fmt in formats_hint]
        prefer_single = any("single" in f for f in fmt_norm)
        is_album_like = any("album"  in f for f in fmt_norm)
        keep = prefer_single or (title_ratio >= 0.60) or is_album_like
        if keep:
            cands.append((rid, prefer_single, title_ratio))
    # Prefer singles; then higher title similarity; cap requests
    cands = sorted(cands, key=lambda x: (not x[1], -x[2]))[:10]
    # --- Inspect releases -----------------------------------------------------
    for rel_id, _, _ in cands:
        try:
            _throttle_discogs()
            rel = session.get(f"https://api.discogs.com/releases/{rel_id}", headers=headers, timeout=timeout)
            if rel.status_code == 429:
                _respect_retry_after(rel)
            rel.raise_for_status()
            data = rel.json()
        except Exception:
            continue

        # Context gate first
        if not _release_context_compatible(data, require_live=require_live, forbid_live=forbid_live):
            continue
        formats = data.get("formats", []) or []
        names   = [f.get("name","").lower() for f in formats]
        descs   = [d.lower() for f in formats for d in (f.get("descriptions") or [])]
        # Albums out; EPs allowed (per policy)
        is_album = ("album" in names) or ("album" in descs)
        is_ep    = ("ep"    in names) or ("ep"    in descs)
        if is_album:
            continue
        tracks = data.get("tracklist", []) or []
        if not tracks or len(tracks) > 7:
            continue
        # Robust title match across any track
        best_idx, best_ratio = -1, 0.0
        for i, t in enumerate(tracks):
            r = difflib.SequenceMatcher(None, _canon(t.get("title","")), nav_title).ratio()
            if r > best_ratio:
                best_idx, best_ratio = i, r
        if best_ratio < 0.80:
            continue
        mtitle = (tracks[best_idx].get("title","") or "").lower()
        if (("live" in mtitle) or ("remix" in mtitle)) and not allow_live_ctx:
            continue
        # --- Strong path 1: explicit Single in formats ------------------------
        if ("single" in names) or ("single" in descs):
            _DISCOGS_SINGLE_CACHE[cache_key] = True
            return True
        # --- Strong path 2: EP + first track A-side + official video ----------
        if is_ep and best_idx == 0:
            if _has_official_on_release(data, title, allow_live=allow_live_ctx, min_ratio=0.50):
                _DISCOGS_SINGLE_CACHE[cache_key] = True
                return True
        # --- Structural fallback: classic A/B sides (≤ 2 tracks) --------------
        if 1 <= len(tracks) <= 2:
            if best_idx == 0:
                _DISCOGS_SINGLE_CACHE[cache_key] = True
                return True
            else:
                # If matched is track 2, accept when track 1 is a benign variant
                t1 = tracks[0].get("title","")
                if _is_variant_of(title, t1):
                    _DISCOGS_SINGLE_CACHE[cache_key] = True
                    return True

        # Otherwise continue inspecting next candidate

    # --- No match -------------------------------------------------------------
    _DISCOGS_SINGLE_CACHE[cache_key] = False
    return False

def is_lastfm_single(title: str, artist: str) -> bool:
    """Placeholder; returns False until implemented."""
    return False

def is_musicbrainz_single(title: str, artist: str) -> bool:
    """
    Query MusicBrainz release-group by title+artist and check primary-type=Single.
    """
    try:
        res = requests.get(
            "https://musicbrainz.org/ws/2/release-group/",
            params={
                "query": f'"{title}" AND artist:"{artist}" AND primarytype:Single',
                "fmt": "json",
                "limit": 5
            },
            headers={"User-Agent": "sptnr-cli/1.0 (support@example.com)"},
            timeout=8
        )
        res.raise_for_status()
        rgs = res.json().get("release-groups", [])
        return any((rg.get("primary-type") or "").lower() == "single" for rg in rgs)
    except Exception as e:
        logging.debug(f"MusicBrainz single check failed for '{title}': {e}")
        return False

def get_suggested_mbid(title: str, artist: str, limit: int = 5) -> tuple[str, float]:
    """
    Search MusicBrainz recordings and compute (mbid, confidence).
    Confidence:
      - Title similarity (SequenceMatcher)
      - +0.15 bonus if associated release-group primary-type == 'Single'
    We fetch 'releases' in the recording include, then second-hop to /release/{id}?inc=release-groups
    to reliably check the release-group primary-type.
    """
    try:
        headers = {"User-Agent": "sptnr-cli/2.1 (support@example.com)"}

        # 1) Find recordings (with releases included for second hop)
        rec_url = "https://musicbrainz.org/ws/2/recording/"
        rec_params = {
            "query": f'"{title}" AND artist:"{artist}"',
            "fmt": "json",
            "limit": limit,
            "inc": "releases+artist-credits",  # releases needed to inspect release-group via second hop
        }
        r = requests.get(rec_url, params=rec_params, headers=headers, timeout=10)
        r.raise_for_status()
        recordings = r.json().get("recordings", []) or []
        if not recordings:
            return "", 0.0

        best_mbid = ""
        best_score = 0.0
        nav_title = (title or "").lower()

        for rec in recordings:
            rec_mbid = rec.get("id", "")
            rec_title = (rec.get("title") or "").lower()
            title_sim = difflib.SequenceMatcher(None, nav_title, rec_title).ratio()

            # Default: no bonus
            single_bonus = 0.0

            # 2) If we have at least one release, second hop: /release/{id}?inc=release-groups
            #    so we can read the primary-type reliably.
            releases = rec.get("releases") or []
            if releases:
                rel_id = releases[0].get("id")
                if rel_id:
                    rel_url = f"https://musicbrainz.org/ws/2/release/{rel_id}"
                    rel_params = {"fmt": "json", "inc": "release-groups"}
                    rr = requests.get(rel_url, params=rel_params, headers=headers, timeout=10)
                    if rr.ok:
                        rel_json = rr.json()
                        rg = rel_json.get("release-group") or {}
                        primary_type = (rg.get("primary-type") or "").lower()
                        if primary_type == "single":
                            single_bonus = 0.15
            confidence = min(1.0, title_sim + single_bonus)
            if confidence > best_score:
                best_score = confidence
                best_mbid = rec_mbid
        return best_mbid, round(best_score, 3)
    except Exception as e:
        logging.debug(f"MusicBrainz suggested MBID lookup failed for '{title}' by '{artist}': {e}")
        return "", 0.0

def detect_single_status(
    title: str,
    artist: str,
    *,
    youtube_api_key: str | None = None,
    discogs_token: str | None = None,
    known_list: list[str] | None = None,
    use_lastfm: bool = False,
    min_sources: int = 1,
    album_context: dict | None = None,
    permissive_fallback: bool = False,
    spotify_hint: bool = False,   # IMPORTANT: pass True ONLY if Spotify itself matched (not short_release)
) -> dict:
    """
    New decision model:
      - Hard stop: Discogs Single -> 5★ single (is_single=True, five_star=True).
      - Hard stop: Discogs Video + Spotify -> 5★ single.
      - Continue: If Discogs Video or Spotify hit (at least one), keep looking at other sources
        until we have 2 total matches -> 5★ single.
      - Otherwise -> not a 5★ single for this pass.

    Returns:
      {
        "is_single": bool,         # True only when confirmed by discogs single OR two matches rule
        "confidence": "high"|"medium"|"low",
        "sources": [ ... ],        # accumulated sources
        "five_star": bool          # explicit flag to grant 5★ upstream
      }
    """
    # Known list remains authoritative
    if known_list and title in known_list:
        return {"is_single": True, "confidence": "high", "sources": ["known_list"], "five_star": True}
    sources: set[str] = set()
    if spotify_hint:
        sources.add("spotify")
    discogs_video_hit = False
    # 1) Discogs Official Video — do NOT return early (unless combined with Spotify)
    if discogs_token:
        try:
            dv = discogs_official_video_signal(
                title, artist,
                discogs_token=discogs_token,
                album_context=album_context,
                permissive_fallback=permissive_fallback or CONTEXT_FALLBACK_STUDIO,
            )
            if dv.get("match"):
                discogs_video_hit = True
                sources.add("discogs_video")
        except Exception as e:
            logging.debug(f"Discogs video check failed for '{title}': {e}")
    # 2) Discogs Single — hard stop
    if discogs_token:
        try:
            if is_discogs_single(title, artist, album_context=album_context):
                sources.add("discogs")
                return {"is_single": True, "confidence": "high", "sources": sorted(sources), "five_star": True}
        except Exception as e:
            logging.debug(f"Discogs single check failed for '{title}': {e}")
    # 3) Two‑match confirmation: Discogs Video + Spotify — hard stop
    if discogs_video_hit and ("spotify" in sources):
        return {"is_single": True, "confidence": "high", "sources": sorted(sources), "five_star": True}
    # 4) Continue only if at least one of {video, spotify} matched
    must_continue = discogs_video_hit or ("spotify" in sources)
    # 5) Accumulate other sources and check for 2 matches total
    try:
        if is_musicbrainz_single(title, artist):
            sources.add("musicbrainz")
    except Exception as e:
        logging.debug(f"MusicBrainz check failed for '{title}': {e}")
    try:
        if use_lastfm and is_lastfm_single(title, artist):
            sources.add("lastfm")
    except Exception as e:
        logging.debug(f"Last.fm single check failed for '{title}': {e}")
    # If we had at least one of {video, spotify}, confirm when total matches >= 2
    eligible_sources = {"spotify", "discogs_video", "musicbrainz", "lastfm"}
    total_matches = len(sources & eligible_sources)
    if must_continue and total_matches >= 2:
        return {"is_single": True, "confidence": "high", "sources": sorted(sources), "five_star": True}
    # No confirmation -> not a 5★ single for now
    confidence = "medium" if len(sources) >= 1 else "low"
    return {"is_single": False, "confidence": confidence, "sources": sorted(sources), "five_star": False}

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

def get_musicbrainz_genres(title: str, artist: str) -> list[str]:
    """
    Fetch tags/genres from MusicBrainz with explicit includes on recordings.
    Strategy:
      1) Search recording with inc=tags+artist-credits+releases
      2) Use recording-level tags if present
      3) If no recording tags, try tags on the first associated release (via /release/{id}?inc=tags)
    """
    try:
        # Step 1: search recording with richer includes
        rec_url = "https://musicbrainz.org/ws/2/recording/"
        rec_params = {
            "query": f'"{title}" AND artist:"{artist}"',
            "fmt": "json",
            "limit": 3,
            # 'inc' on recording: tags + releases + artist-credits helps locate usable metadata
            "inc": "tags+artist-credits+releases",
        }
        headers = {"User-Agent": "sptnr-cli/2.1 (support@example.com)"}
        r = requests.get(rec_url, params=rec_params, headers=headers, timeout=10)
        r.raise_for_status()
        recs = r.json().get("recordings", []) or []
        if not recs:
            return []

        # Prefer the top match
        rec = recs[0]
        # 2) use recording-level tags if present
        tags = rec.get("tags") or []
        tag_names = [t.get("name", "") for t in tags if t.get("name")]
        if tag_names:
            return tag_names

        # 3) fallback: pull tags from the first release if any
        releases = rec.get("releases") or []
        if releases:
            rel_id = releases[0].get("id")
            if rel_id:
                rel_url = f"https://musicbrainz.org/ws/2/release/{rel_id}"
                rel_params = {"fmt": "json", "inc": "tags"}
                rr = requests.get(rel_url, params=rel_params, headers=headers, timeout=10)
                rr.raise_for_status()
                rel_tags = rr.json().get("tags", []) or []
                return [t.get("name", "") for t in rel_tags if t.get("name")]
        return []
    except Exception as e:
        logging.warning(f"MusicBrainz genres lookup failed for '{title}' by '{artist}': {e}")
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

def get_lastfm_track_info(artist: str, title: str) -> dict:
    """
    Fetch Last.fm track playcount.
    Returns raw playcount; normalization happens in compute_track_score().
    """
    if not LASTFM_API_KEY:
        logging.warning("Last.fm API key missing. Skipping lookup.")
        return {"track_play": 0}

    params = {
        "method": "track.getInfo",
        "artist": artist,
        "track": title,
        "api_key": LASTFM_API_KEY,
        "format": "json"
    }

    try:
        res = requests.get("https://ws.audioscrobbler.com/2.0/", params=params, timeout=10)
        res.raise_for_status()
        data = res.json().get("track", {})
        track_play = int(data.get("playcount", 0))
        return {"track_play": track_play}
    except Exception as e:
        logging.error(f"Last.fm fetch failed for '{title}' by '{artist}': {e}")
        return {"track_play": 0}

def get_listenbrainz_score(mbid: str, artist: str = "", title: str = "") -> int:
    """
    Fetch ListenBrainz listen count using MBID or fallback search.
    Returns raw listen count; normalization happens in compute_track_score().
    """
    if not mbid:
        # Fallback: search by artist/title
        try:
            url = "https://api.listenbrainz.org/1/recording/search"
            params = {"artist_name": artist, "recording_name": title, "limit": 1}
            res = requests.get(url, params=params, timeout=10)
            res.raise_for_status()
            hits = res.json().get("recordings", [])
            if hits:
                return int(hits[0].get("listen_count", 0))
        except Exception as e:
            logging.debug(f"ListenBrainz fallback search failed for '{title}': {e}")
        return 0
    # Primary: stats by MBID
    try:
        url = f"https://api.listenbrainz.org/1/stats/recording/{mbid}"
        res = requests.get(url, timeout=10)
        res.raise_for_status()
        data = res.json()
        return int(data.get("count", 0))
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
    """
    Compute weighted score using Spotify, Last.fm, ListenBrainz, and age decay.
    Last.fm: use raw track playcount.
    ListenBrainz: use MBID or fallback search.
    """
    lf_data = get_lastfm_track_info(artist_name, title)
    lf_track = lf_data.get("track_play", 0)

    lb_score = get_listenbrainz_score(mbid, artist_name, title) if config["listenbrainz"]["enabled"] else 0

    momentum, _ = score_by_age(lf_track, release_date)

    score = (SPOTIFY_WEIGHT * sp_score) + \
            (LASTFM_WEIGHT * lf_track) + \
            (LISTENBRAINZ_WEIGHT * lb_score) + \
            (AGE_WEIGHT * momentum)
    if verbose:
        print(f"🔢 Raw score for '{title}': {round(score)} "
              f"(Spotify: {sp_score}, Last.fm: {lf_track}, ListenBrainz: {lb_score}, Age: {momentum})")
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
            res = requests.get(url, params=params, timeout=10)
            res.raise_for_status()
            logging.info(f"✅ Set rating {stars}/5 for track {track_id} (user {user_cfg['user']})")
        except Exception as e:
            logging.error(f"❌ Failed for {user_cfg['user']}: {e}")

def sync_rating_and_report(trk: dict):
    """
    Compare server rating (primary user) with computed stars; print/log only if update is needed.
    - single check happens once via primary user
    - applies update to all users
    - honors dry_run
    """
    track_id   = trk["id"]
    title      = trk.get("title", track_id)
    new_stars  = int(trk.get("stars", 0))
    # Fetch current server rating using primary user only
    old_stars = get_current_rating(track_id)  # None if unrated
    # No change → stay quiet (unless verbose)
    if old_stars == new_stars:
        if verbose:
            print(f"   ↔️ No change: '{title}' already {new_stars}★")
        return
    old_label = f"{old_stars}★" if isinstance(old_stars, int) else "unrated"
    # 🔹 Single-aware output: include sources when this scan marked the track as a single
    if trk.get("is_single"):
        srcs = ", ".join(trk.get("single_sources", []))
        print(f"   🎛️ Rating update (single via {srcs}): '{title}' — {old_label} → {new_stars}★")
        logging.info(f"Rating update (single via {srcs}): {track_id} '{title}' {old_label} -> {new_stars}★ (primary check)")
    else:
        print(f"   🎛️ Rating update: '{title}' — {old_label} → {new_stars}★")
        logging.info(f"Rating update: {track_id} '{title}' {old_label} -> {new_stars}★ (primary check)")
    if dry_run:
        return

    # Apply the update to all configured users
    set_track_rating_for_all(track_id, new_stars)

def refresh_all_playlists_from_db():
    print("🔄 Refreshing smart playlists for all artists from DB cache (no track rescans)...")
    # Pull distinct artists that have cached tracks
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT artist FROM tracks")
    artists = [row[0] for row in cursor.fetchall()]
    conn.close()
    if not artists:
        print("⚠️ No cached tracks in DB. Skipping playlist refresh.")
        return
    for name in artists:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT id, artist, album, title, stars FROM tracks WHERE artist = ?", (name,))
        rows = cursor.fetchall()
        conn.close()
        if not rows:
            print(f"⚠️ No cached tracks found for '{name}', skipping.")
            continue
        tracks = [{"id": r[0], "artist": r[1], "album": r[2], "title": r[3], "stars": int(r[4]) if r[4] else 0}
                  for r in rows]
        create_or_update_playlist_for_artist(name, tracks)
        print(f"✅ Playlist refreshed for '{name}' ({len(tracks)} tracks)")

def _normalize_name(name: str) -> str:
    # Normalize typographic quotes and trim spaces
    return (
        (name or "")
        .replace("“", '"').replace("”", '"').replace("’", "'")
        .strip()
    )

def _log_resp(resp, action, name):
    try:
        txt = resp.text[:500]
    except Exception:
        txt = "<no text>"
    logging.info(f"{action} '{name}' → {resp.status_code}: {txt}")

def nav_get_all_playlists():
    url = f"{NAV_BASE_URL}/api/playlist"
    try:
        res = requests.get(url, auth=(USERNAME, PASSWORD))
        _log_resp(res, "GET /api/playlist", "<list>")
        res.raise_for_status()
        return res.json().get("items", [])
    except Exception as e:
        logging.error(f"Failed to fetch playlists: {e}")
        return []

# --- NSP playlist writer replacements for Navidrome API helpers ---
NSP_ROOT = "/music/playlists"  # default destination for .nsp files

def _playlist_file_exists(playlist_name: str) -> bool:
    safe_name = unicodedata.normalize("NFKD", (playlist_name or "").strip())
    safe_name = safe_name.replace("/", "_").replace("\\", "_").replace(":", "-")
    file_path = os.path.join(NSP_ROOT, f"{safe_name}.nsp")
    return os.path.exists(file_path)

def _safe_fs_name(name: str) -> str:
    """Filesystem-safe name, still readable."""
    s = unicodedata.normalize("NFKD", (name or "").strip())
    s = s.replace("/", "_").replace("\\", "_").replace(":", "-")
    return s

def _rules_to_nsp_all(rules: list[dict]) -> list[dict]:
    """
    Convert API-style rules to NSP 'all' clauses (Navidrome 0–5 rating scale).
      - {"field":"artist","operator":"equals","value":"X"} -> {"is":{"artist":"X"}}
      - {"field":"userRating","operator":"equals","value":"5"} -> {"is":{"rating":5}}
      - {"field":"rating","operator":"equals","value":"N"} -> {"is":{"rating":N}}  # N in {0..5}
      - {"field":"rating","operator":"greaterThanOrEquals","value":"4"} -> {"is":{"rating":5}}  # tighten to top-rated
    """
    nsp_all = []
    for r in rules or []:
        field = (r.get("field") or "").lower()
        op    = (r.get("operator") or "").lower()
        val   = r.get("value")
        # Artist equality
        if field == "artist" and op == "equals":
            nsp_all.append({"is": {"artist": val}})
            continue
        # 5★ user rating -> rating: 5
        if field == "userrating" and op == "equals":
            if str(val) == "5":
                nsp_all.append({"is": {"rating": 5}})
            continue
        # Rating >= threshold -> tighten to 5 (top-rated)
        if field == "rating" and op in ("greaterthanorequals", "gte"):
            try:
                n = int(val)
            except (TypeError, ValueError):
                n = None
            if n is not None:
                nsp_all.append({"is": {"rating": 5 if n >= 4 else max(0, n)}})
            continue
        # Rating equality on 0..5
        if field == "rating" and op == "equals":
            try:
                n = int(val)
            except (TypeError, ValueError):
                n = None
            if n is not None and 0 <= n <= 5:
                nsp_all.append({"is": {"rating": n}})
            continue
        # Add more mappings as needed...
    return nsp_all

def _sort_to_nsp(sort: list[dict]) -> str:
    """
    Convert your sort list to NSP 'sort' string.
    - 'userRating' or 'rating' desc -> '-rating'
    - 'random' -> 'random'
    If multiple, join by comma (e.g. '-rating,random')
    """
    keys = []
    for s in sort or []:
        field = (s.get("field") or "").lower()
        order = (s.get("order") or "").lower()
        if field in ("userrating", "rating"):
            keys.append("-rating" if order in ("desc", "descending") else "rating")
        elif field == "random":
            keys.append("random")
        # You can add 'title', 'year' etc. if needed: '-year', 'title', etc.
    # Default to '-rating,random' if nothing useful was provided
    return ",".join(keys) if keys else "-rating,random"

def _build_nsp_payload(name: str, rules: list, sort: list, limit: int | None) -> dict:
    """
    Build the NSP JSON payload from the inputs used by your existing calls.
    """
    payload = {
        "name": _normalize_name(name),
        "comment": "Auto-generated by SPTNR",
        "all": _rules_to_nsp_all(rules),
        "sort": _sort_to_nsp(sort),
    }
    if limit and isinstance(limit, int):
        payload["limit"] = max(1, limit)
    else:
        # Keep it unconstrained unless your caller specifies 'limit'
        # For Essentials Case B you pass a limit already.
        pass
    return payload

def nav_create_smart_playlist(name: str, rules: list, sort: list, limit: int | None = None):
    """
    REPLACEMENT: Write a .nsp JSON file to /music/playlists instead of POSTing to Navidrome API.
    Keeps existing call signature intact.
    """
    try:
        os.makedirs(NSP_ROOT, exist_ok=True)
        payload = _build_nsp_payload(name, rules, sort, limit)
        file_name = _safe_fs_name(name) + ".nsp"
        file_path = os.path.join(NSP_ROOT, file_name)

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        logging.info(f"📝 NSP created: {file_path} rules={rules} sort={sort} limit={limit}")
        # Preserve your logging style
        _log_resp(type("DummyResp", (), {"status_code": 200, "text": "NSP written"})(), "WRITE .nsp", name)
    except Exception as e:
        logging.error(f"Failed to create NSP playlist '{name}': {e}")

def nav_delete_playlist_by_name(name: str):
    """
    REPLACEMENT: Delete the .nsp file in /music/playlists matching the given name.
    """
    try:
        safe = _safe_fs_name(name) + ".nsp"
        path = os.path.join(NSP_ROOT, safe)
        if os.path.exists(path):
            os.remove(path)
            logging.info(f"🗑️ Deleted NSP playlist '{name}' at {path}")
            _log_resp(type("DummyResp", (), {"status_code": 200, "text": "NSP deleted"})(), "DELETE .nsp", name)
        else:
            logging.info(f"ℹ️ NSP playlist '{name}' not found at {path}")
    except Exception as e:
        logging.warning(f"Failed to delete NSP playlist '{name}': {e}")




def create_or_update_playlist_for_artist(artist: str, tracks: list[dict]):
    """
    Create/refresh 'Essential {artist}' smart playlist using Navidrome's 0–5 rating scale.

    Logic:
      - Case A: if artist has >= 10 five-star tracks, build a pure 5★ essentials playlist.
        Rule: userRating == 5  (alternatively, rating == 5 if your source uses 'rating')
        Sort: random
      - Case B: if total tracks >= 100, build top 10% essentials sorted by userRating then rating (both 0–5).
        Sort: userRating desc, rating desc, random
    """

    total_tracks = len(tracks)
    # 'stars' should be 0..5 in your input. Adjust if your field is named differently.
    five_star_tracks = [t for t in tracks if (t.get("stars") or 0) == 5]
    playlist_name = f"Essential {artist}"

    def _playlist_exists():
        return _playlist_file_exists(playlist_name)

    # CASE A — 10+ five-star tracks → purely 5★ essentials
    if len(five_star_tracks) >= 10:
        nav_delete_playlist_by_name(playlist_name)
        rules = [
            {"field": "artist", "operator": "equals", "value": artist},
            {"field": "userRating", "operator": "equals", "value": "5"},
        ]
        sort = [{"field": "random", "order": "asc"}]
        nav_create_smart_playlist(playlist_name, rules, sort)
        return

    # CASE B — 100+ total tracks → top 10% by rating (0–5)
    if total_tracks >= 100:
        nav_delete_playlist_by_name(playlist_name)
        limit = max(1, math.ceil(total_tracks * 0.10))
        rules = [{"field": "artist", "operator": "equals", "value": artist}]
        sort = [
            {"field": "userRating", "order": "desc"},  # primary: explicit 0–5 stars
            {"field": "rating", "order": "desc"},      # secondary: numeric 0–5 rating if present
            {"field": "random", "order": "asc"},       # tie-breaker
        ]
        nav_create_smart_playlist(playlist_name, rules, sort, limit)
        return

    logging.info(
        f"No Essential playlist created for '{artist}' "
        f"(total={total_tracks}, five★={len(five_star_tracks)})"
    )


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


def get_album_last_scanned_from_db(artist_name: str, album_name: str) -> str | None:
    """
    Return the most recent 'last_scanned' timestamp among tracks already saved
    for (artist, album). Timestamp is in '%Y-%m-%dT%H:%M:%S' or None if missing.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
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
        conn = sqlite3.connect(DB_PATH)
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
      - Hard stop: Discogs Single → is_single=True, stars=5.
      - Hard stop: Discogs Official Video AND Spotify both match → is_single=True, stars=5.
      - Continue: If at least one of {discogs_video, spotify} matched, keep checking other sources
        (MusicBrainz, Last.fm). As soon as we have TWO matches total among {spotify, discogs_video,
        musicbrainz, lastfm} → is_single=True, stars=5.
      - short_release (≤ 2 tracks) is shown in single_sources for audit, but does NOT count toward
        the two‑matches rule unless features.short_release_counts_as_match=True.

    Canonical/variant guard remains:
      - We still require canonical title (no remix/live edit subtitling) and high base similarity.

    Other logic unchanged:
      - Adaptive weights per album, z‑bands, 4★ density cap, Spotify-only boost (applies only when
        is_single is True but without strong sources).
      - Median gate/secondary lookup blocks are kept but will not trigger for video‑only cases,
        because video‑only cannot set is_single=True under this policy.
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

    # --- Local helpers (subtitle/title similarity guards) ---------------------
    def _canon_local(s: str) -> str:
        s = (s or "").lower()
        s = re.sub(r"\(.*?\)", " ", s)
        s = re.sub(r"[^\w\s]", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    def _base_title(s: str) -> str:
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
        return SequenceMatcher(None, _canon_local(a), _canon_local(b)).ratio()

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
        print(f"⚠️ No albums found for artist '{artist_name}'")
        if aux_pool:
            aux_pool.shutdown(wait=False)
        return {}

    print(f"\n🎨 Starting rating for artist: {artist_name} ({len(albums)} albums)")
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
                    print(
                        f"⏩ Skipping album: {album_name} (last scanned {album_last_scanned}, "
                        f"cached tracks={cached_track_count}, threshold={ALBUM_SKIP_DAYS}d)"
                    )
                    continue

        tracks = fetch_album_tracks(album_id)
        if not tracks:
            print(f"⚠️ No tracks found in album '{album_name}'")
            continue

        print(f"\n🎧 Scanning album: {album_name} ({len(tracks)} tracks)")
        album_ctx = infer_album_context(album_name)

        album_tracks = []

        # Resolve singles set lazily
        if singles_set_future and not singles_set:
            try:
                singles_set = singles_set_future.result(timeout=30) or set()
            except Exception as e:
                logging.debug(f"Spotify singles prefetch failed for '{artist_name}': {e}")
                singles_set = set()

        # ----------------------------------------------------------------------
        # PER-TRACK ENRICHMENT (CONCURRENT)
        # ----------------------------------------------------------------------
        with ThreadPoolExecutor(max_workers=4) as ex:
            for track in tracks:
                track_id   = track["id"]
                title      = track["title"]
                file_path  = track.get("path", "")
                nav_genres = [track.get("genre")] if track.get("genre") else []
                mbid       = track.get("mbid", None)

                if verbose:
                    print(f"   🔍 Processing track: {title}")

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
                    print(f"🔢 Raw score for '{title}': {score:.4f} "
                          f"(Spotify: {sp_norm:.3f}, Last.fm: {lf_norm:.3f}, ListenBrainz: {lb_norm:.3f}, Age: {age_norm:.3f})")

                discogs_genres = get_discogs_genres(title, artist_name)
                audiodb_genres = get_audiodb_genres(artist_name) if config["features"].get("use_audiodb", False) and AUDIODB_API_KEY else []
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
                        print(f"      ↔ Suggested MBID: {suggested_mbid} (confidence {suggested_confidence})")

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
                    "suggested_mbid_confidence": suggested_confidence
                }

                album_tracks.append(track_data)

        # ----------------------------------------------------------------------
        # ADAPTIVE WEIGHTS (per album) → recompute 'score'
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

        # Album medians for gate
        scores_all     = [t["score"] for t in album_tracks]
        score_median   = median(scores_all)
        spotify_all    = [t.get("spotify_score", 0) for t in album_tracks]
        spotify_median = median(spotify_all)
        album_medians  = {"score": score_median, "spotify": spotify_median}

        # ----------------------------------------------------------------------
        # SINGLE DETECTION — Paired/Stop rules with canonical title guard
        # ----------------------------------------------------------------------
        for trk in album_tracks:
            title          = trk["title"]
            canonical_base = _base_title(title)
            sim_to_base    = _similar(title, canonical_base)
            has_subtitle   = _has_subtitle_variant(title)

            allow_live_remix = bool(album_ctx.get("is_live") or album_ctx.get("is_unplugged"))
            canonical        = is_valid_version(title, allow_live_remix=allow_live_remix)

            spotify_matched  = bool(trk.get("is_spotify_single"))
            tot              = trk.get("spotify_total_tracks")
            short_release    = (tot is not None and tot > 0 and tot <= 2)

            # Accumulate sources for visibility; short_release is audit-only unless configured otherwise
            sources = set()
            if spotify_matched:
                sources.add("spotify")
            if short_release:
                sources.add("short_release")

            # --- Discogs Single (hard stop) -----------------------------------
            discogs_single_hit = False
            try:
                if DISCOGS_TOKEN and is_discogs_single(title, artist=artist_name, album_context=album_ctx):
                    sources.add("discogs")
                    discogs_single_hit = True
            except Exception as e:
                logging.debug(f"is_discogs_single failed for '{title}': {e}")

            if discogs_single_hit and canonical and not has_subtitle and sim_to_base >= TITLE_SIM_THRESHOLD:
                trk["is_single"] = True
                trk["single_sources"] = sorted(sources)
                trk["single_confidence"] = "high"
                trk["stars"] = 5
                # Hard stop for this track
                continue

            # --- Discogs Official Video (paired with Spotify → hard stop) -----
            discogs_video_hit = False
            try:
                if DISCOGS_TOKEN:
                    dv = discogs_official_video_signal(
                        title, artist_name,
                        discogs_token=DISCOGS_TOKEN,
                        album_context=album_ctx,
                        permissive_fallback=CONTEXT_FALLBACK_STUDIO,
                    )
                    if dv.get("match"):
                        sources.add("discogs_video")
                        discogs_video_hit = True
            except Exception as e:
                logging.debug(f"discogs_official_video_signal failed for '{title}': {e}")

            # Paired hard stop: video + spotify
            if (discogs_video_hit and spotify_matched) and canonical and not has_subtitle and sim_to_base >= TITLE_SIM_THRESHOLD:
                trk["is_single"] = True
                trk["single_sources"] = sorted(sources)
                trk["single_confidence"] = "high"
                trk["stars"] = 5
                continue

            # --- Continue only if we have at least one of {video, spotify} ----
            must_continue = discogs_video_hit or spotify_matched
            if not must_continue:
                # No paired sources yet → not a single for now
                trk["is_single"] = False
                trk["single_sources"] = sorted(sources)
                trk["single_confidence"] = "low" if len(sources) == 0 else "medium"
                # let z-bands assign stars later
                continue

            # Add corroborative sources
            try:
                if is_musicbrainz_single(title, artist_name):
                    sources.add("musicbrainz")
            except Exception as e:
                logging.debug(f"MusicBrainz single check failed for '{title}': {e}")

            try:
                if use_lastfm_single and is_lastfm_single(title, artist_name):
                    sources.add("lastfm")
            except Exception as e:
                logging.debug(f"Last.fm single check failed for '{title}': {e}")

            # Count matches toward confirmation
            match_pool = {"spotify", "discogs_video", "musicbrainz", "lastfm"}
            if COUNT_SHORT_RELEASE_AS_MATCH:
                match_pool.add("short_release")
            total_matches = len(sources & match_pool)

            if (total_matches >= 2) and canonical and not has_subtitle and sim_to_base >= TITLE_SIM_THRESHOLD:
                trk["is_single"] = True
                trk["single_sources"] = sorted(sources)
                trk["single_confidence"] = "high"
                trk["stars"] = 5
            else:
                trk["is_single"] = False
                trk["single_sources"] = sorted(sources)
                trk["single_confidence"] = "medium" if total_matches >= 1 else "low"
                # let z-bands assign stars later

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
                            f"→ strategy={MEDIAN_STRATEGY} sources={','.join(trk['single_sources'])}"
                        )

        # ----------------------------------------------------------------------
        # Z-BANDS (apply to everyone except confirmed 5★ singles)
        # ----------------------------------------------------------------------
        sorted_album = sorted(album_tracks, key=lambda x: x["score"], reverse=True)
        EPS = 1e-6
        scores_all = [t["score"] for t in sorted_album]
        med = median(scores_all)
        mad_val = max(median([abs(v - med) for v in scores_all]), EPS)

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

        # Cap density of 4★ among non-singles
        non_single_tracks = [t for t in sorted_album if not t.get("is_single")]
        top4 = [t for t in non_single_tracks if t.get("stars") == 4]
        max_top4 = max(1, round(len(non_single_tracks) * CAP_TOP4_PCT))
        if len(top4) > max_top4:
            for t in sorted(top4, key=lambda x: zrobust(x["score"]), reverse=True)[max_top4:]:
                t["stars"] = 3

        # ----------------------------------------------------------------------
        # Spotify-only single BOOST (AFTER z-bands) — retained
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
        # ----------------------------------------------------------------------
        for trk in sorted_album:
            save_to_db(trk)

            new_stars = int(trk.get("stars", 0))
            title     = trk.get("title", trk["id"])

            if trk.get("is_single"):
                srcs = ", ".join(trk.get("single_sources") or [])
                print(f"   🎛️ Rating set (single via {srcs if srcs else 'unknown'}): '{title}' — {new_stars}★")
                logging.info(f"Rating set (single via {srcs if srcs else 'unknown'}): {trk['id']} '{title}' -> {new_stars}★")
            else:
                print(f"   🎛️ Rating set: '{title}' — {new_stars}★")
                logging.info(f"Rating set: {trk['id']} '{title}' -> {new_stars}★")

            if config["features"].get("dry_run", False):
                continue

            if config["features"].get("sync", True):
                set_track_rating_for_all(trk["id"], new_stars)

        # ----------------------------------------------------------------------
        # ALBUM SUMMARY
        # ----------------------------------------------------------------------
        single_count = sum(1 for trk in sorted_album if trk.get("is_single"))
        print(
            f"   ℹ️ Singles detected: {single_count} | Non‑single 4★: "
            f"{sum(1 for t in non_single_tracks if t['stars']==4)} "
            f"| Cap: {int(CAP_TOP4_PCT*100)}% | MAD: {mad_val:.2f}"
        )

        if single_count > 0:
            print("   🎯 Singles:")
            for t in sorted_album:
                if t.get("is_single"):
                    srcs = ", ".join(t.get("single_sources") or [])
                    print(f"      • {t['title']} (via {srcs if srcs else 'unknown'}, conf={t['single_confidence']})")

        print(f"✔ Completed album: {album_name}")
        rated_map.update({t["id"]: t for t in sorted_album})

    # --------------------------------------------------------------------------
    # SMART PLAYLIST CREATION
    # --------------------------------------------------------------------------
    def _playlist_exists(playlist_name: str) -> bool:
        return _playlist_file_exists(playlist_name)

    if artist_name.lower() != "various artists" and config["features"].get("sync", True) and not config["features"].get("dry_run", False):
        playlist_name = f"Essential {artist_name}"
        total_tracks = len(rated_map)
        five_star_tracks = [t for t in rated_map.values() if (t.get("stars") or 0) == 5]

        if len(five_star_tracks) >= 10:
            nav_delete_playlist_by_name(playlist_name)
            rules_user = [
                {"field": "artist", "operator": "equals", "value": artist_name},
                {"field": "userRating", "operator": "equals", "value": "5"}
            ]
            sort = [{"field": "random", "order": "asc"}]
            nav_create_smart_playlist(playlist_name, rules_user, sort)

        elif total_tracks >= 100:
            nav_delete_playlist_by_name(playlist_name)
            limit = max(1, math.ceil(total_tracks * 0.10))
            sort = [
                {"field": "userRating", "order": "desc"},
                {"field": "random", "order": "asc"}
            ]
            rules = [{"field": "artist", "operator": "equals", "value": artist_name}]
            nav_create_smart_playlist(playlist_name, rules, sort, limit)

        else:
            logging.info(
                f"No Essential playlist created for '{artist_name}' "
                f"(total={total_tracks}, five★={len(five_star_tracks)})"
            )

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

    print("\n🧪 Self-test: HIGH confidence required")
    passes = 0
    for name, canonical, sset, expected in cases:
        conf = confidence_for(sset, "spotify" in sset, "short_release" in sset)
        decision = canonical and (conf == "high")
        ok = (decision == expected)
        passes += int(ok)
        print(f" - {name:<35} → conf={conf}, decision={decision}  [{'PASS' if ok else 'FAIL'}]")
    print(f"✅ {passes}/{len(cases)} cases passed.\n")



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
    parser.add_argument("--selftest", action="store_true", help="Run single-gate self-tests and exit")
    parser.add_argument("--refresh-playlists", action="store_true",
                        help="Recreate smart playlists for all artists without rescanning tracks")

    args = parser.parse_args()

    # ✅ Run self-test and exit immediately if requested
    if args.selftest:
        _self_test_single_gate()
        sys.exit(0)

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
    dry_run  = config["features"]["dry_run"]
    sync     = config["features"]["sync"]
    force    = config["features"]["force"]
    verbose  = config["features"]["verbose"]
    perpetual = config["features"]["perpetual"]
    batchrate = config["features"]["batchrate"]
    artist_list = config["features"]["artist"]
    use_google  = config["features"].get("use_google", False)
    use_youtube = config["features"].get("use_youtube", False)
    use_audiodb = config["features"].get("use_audiodb", False)
    refresh_playlists_on_start = config["features"].get("refresh_playlists_on_start", False)
    refresh_index_on_start     = config["features"].get("refresh_artist_index_on_start", False)

    # --- Early startup triggers from YAML flags ---
    if refresh_index_on_start:
        print("📚 Building artist index from Navidrome (startup)…")
        build_artist_index()

    if refresh_playlists_on_start:
        # Guard: only useful if tracks exist in DB
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM tracks")
        has_tracks = (cursor.fetchone()[0] or 0) > 0
        conn.close()

        if not has_tracks:
            print("⚠️ No cached tracks yet; playlist refresh would be ineffective.")
            # Optional: trigger a small rating pass here if you want to auto-populate.

        print("🚀 Startup flag enabled: refreshing smart playlists from DB cache…")
        refresh_all_playlists_from_db()
        # Optional: exit after startup-only behavior:
        # sys.exit(0)

    # ✅ Rebuild artist index if requested by CLI
    if args.refresh:
        build_artist_index()

    # ✅ Pipe output if requested (print cached artist index and exit)
    if args.pipeoutput is not None:
        artist_map = load_artist_map()
        filtered = {
            name: info for name, info in artist_map.items()
            if not args.pipeoutput or args.pipeoutput.lower() in name.lower()
        }
        print(f"\n📁 Cached Artist Index ({len(filtered)} matches):")
        for name, info in filtered.items():
            print(f"🎨 {name} → ID: {info['id']} "
                  f"(Albums: {info['album_count']}, Tracks: {info['track_count']}, "
                  f"Last Updated: {info['last_updated']})")
        sys.exit(0)

    # ✅ Refresh smart playlists from DB cache when requested via CLI and exit
    if args.refresh_playlists:
        refresh_all_playlists_from_db()
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
