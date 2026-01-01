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
from datetime import datetime, timedelta
from statistics import median
from collections import defaultdict

import requests
import yaml
from colorama import init, Fore, Style
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter



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
    """
    Save or update track metadata in the database.

    Aligns with schema in check_db.update_schema():
    - Adds/uses fields: mbid, suggested_mbid, suggested_mbid_confidence, single_sources,
      is_spotify_single, spotify_total_tracks, spotify_album_type, lastfm_ratio.
    - Persists discogs_genres, audiodb_genres, musicbrainz_genres.
    - Stores single_sources as JSON string in a TEXT column for fidelity.
    """

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Prepare multi-value fields (comma-delimited for consistency with your existing schema)
    genres              = ",".join(track_data.get("genres", []))
    navidrome_genres    = ",".join(track_data.get("navidrome_genres", []))
    spotify_genres      = ",".join(track_data.get("spotify_genres", []))
    lastfm_tags         = ",".join(track_data.get("lastfm_tags", []))
    discogs_genres      = ",".join(track_data.get("discogs_genres", [])) if track_data.get("discogs_genres") else ""
    audiodb_genres      = ",".join(track_data.get("audiodb_genres", [])) if track_data.get("audiodb_genres") else ""
    musicbrainz_genres  = ",".join(track_data.get("musicbrainz_genres", [])) if track_data.get("musicbrainz_genres") else ""

    # Store sources as JSON for a clean list
    single_sources_json = json.dumps(track_data.get("single_sources", []), ensure_ascii=False)

    cursor.execute("""
    INSERT OR REPLACE INTO tracks (
        id, artist, album, title,
        spotify_score, lastfm_score, listenbrainz_score, age_score, final_score, stars,
        genres, navidrome_genres, spotify_genres, lastfm_tags,
        discogs_genres, audiodb_genres, musicbrainz_genres,
        spotify_album, spotify_artist, spotify_popularity, spotify_release_date, spotify_album_art_url,
        lastfm_track_playcount, lastfm_artist_playcount, file_path,
        is_single, single_confidence, last_scanned,
        mbid, suggested_mbid, suggested_mbid_confidence, single_sources,
        is_spotify_single, spotify_total_tracks, spotify_album_type, lastfm_ratio
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        track_data["id"],
        track_data.get("artist", ""),
        track_data.get("album", ""),
        track_data.get("title", ""),
        float(track_data.get("spotify_score", 0) or 0),
        float(track_data.get("lastfm_score", 0) or 0),
        float(track_data.get("listenbrainz_score", 0) or 0),
        float(track_data.get("age_score", 0) or 0),
        float(track_data.get("score", 0) or 0),
        int(track_data.get("stars", 0) or 0),
        genres,
        navidrome_genres,
        spotify_genres,
        lastfm_tags,
        discogs_genres,
        audiodb_genres,
        musicbrainz_genres,
        track_data.get("spotify_album", ""),
        track_data.get("spotify_artist", ""),
        int(track_data.get("spotify_popularity", 0) or 0),
        track_data.get("spotify_release_date", ""),
        track_data.get("spotify_album_art_url", ""),
        int(track_data.get("lastfm_track_playcount", 0) or 0),
        int(track_data.get("lastfm_artist_playcount", 0) or 0),
        track_data.get("file_path", ""),
        int(bool(track_data.get("is_single", False))),
        track_data.get("single_confidence", ""),
        track_data.get("last_scanned", ""),
        track_data.get("mbid", "") or "",
        track_data.get("suggested_mbid", "") or "",
        float(track_data.get("suggested_mbid_confidence", 0.0) or 0.0),
        single_sources_json,
        int(bool(track_data.get("is_spotify_single", False))),
        int(track_data.get("spotify_total_tracks", 0) or 0),
        track_data.get("spotify_album_type", ""),
        float(track_data.get("lastfm_ratio", 0.0) or 0.0)
    ))

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



# --- Last.fm Single Heuristic (API first, HTML fallback) --------------------
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

def _throttle_discogs():
    """Sleep briefly between Discogs calls to avoid 429s."""
    global _last_call_ts
    now = time.time()
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
# Keyed by normalized (artist, title) -> result dict returned by the function below.
_DISCOGS_VID_CACHE: dict[tuple[str, str], dict] = {}

def discogs_official_video_signal(
    title: str,
    artist: str,
    *,
    discogs_token: str,
    timeout: int = 10,
    per_page: int = 5,
    min_ratio: float = 0.60,   # relaxed threshold
) -> dict:
    """
    Detect ANY official or lyric video for a track on Discogs.
    Rules:
      ✔ Must contain 'official' or 'lyric'
      ✔ Must NOT be live
      ✔ Must NOT be remix (radio edits allowed)
      ✔ Does NOT require track 1
      ✔ Does NOT require single-format release
      ✔ Does NOT require canonical version
    """
    if not discogs_token:
        return {"match": False, "uri": None, "release_id": None, "ratio": None, "why": "no_token"}

    cache_key = (_canon(artist), _canon(strip_parentheses(title)))
    if cache_key in _DISCOGS_VID_CACHE:
        return _DISCOGS_VID_CACHE[cache_key]

    headers = {
        "Authorization": f"Discogs token={discogs_token}",
        "User-Agent": _DEF_USER_AGENT,
    }

    nav_title = _canon(strip_parentheses(title))
    session = _get_discogs_session()

    # 1) Search releases
    try:
        _throttle_discogs()
        s = session.get(
            "https://api.discogs.com/database/search",
            headers=headers,
            params={"q": f"{artist} {title}", "type": "release", "per_page": per_page},
            timeout=timeout,
        )
        if s.status_code == 429:
            _respect_retry_after(s)
        s.raise_for_status()
    except Exception as e:
        result = {"match": False, "uri": None, "release_id": None, "ratio": None, "why": f"search_fail:{e}"}
        _DISCOGS_VID_CACHE[cache_key] = result
        return result

    candidates = s.json().get("results", []) or []
    if not candidates:
        result = {"match": False, "uri": None, "release_id": None, "ratio": None, "why": "no_candidates"}
        _DISCOGS_VID_CACHE[cache_key] = result
        return result

    best = None

    # 2) Inspect videos
    for c in candidates:
        rid = c.get("id")
        if not rid:
            continue

        try:
            _throttle_discogs()
            r = session.get(f"https://api.discogs.com/releases/{rid}", headers=headers, timeout=timeout)
            if r.status_code == 429:
                _respect_retry_after(r)
            r.raise_for_status()
        except:
            continue

        videos = r.json().get("videos") or []
        for v in videos:
            vt_raw = v.get("title", "") or ""
            vd_raw = v.get("description", "") or ""
            vt = _canon(vt_raw)
            vd = _canon(vd_raw)
            uri = v.get("uri")

            # Must be official or lyric
            if "official" not in vt and "official" not in vd and "lyric" not in vt and "lyric" not in vd:
                continue

            # Reject live
            if "live" in vt_raw.lower() or "live" in vd_raw.lower():
                continue

            # Reject remixes (radio edits allowed)
            if "remix" in vt_raw.lower() or "remix" in vd_raw.lower():
                continue

            # Reject alternate mixes unless radio edit
            if "mix" in vt_raw.lower() or "mix" in vd_raw.lower():
                if "radio" not in vt_raw.lower() and "radio" not in vd_raw.lower():
                    continue

            # Reject edits unless radio edit
            if "edit" in vt_raw.lower() or "edit" in vd_raw.lower():
                if "radio" not in vt_raw.lower() and "radio" not in vd_raw.lower():
                    continue

            # Similarity check (relaxed)
            ratio = max(
                difflib.SequenceMatcher(None, vt, nav_title).ratio(),
                difflib.SequenceMatcher(None, vd, nav_title).ratio(),
            )

            if ratio >= min_ratio:
                current = {
                    "match": True,
                    "uri": uri,
                    "release_id": rid,
                    "ratio": round(ratio, 3),
                    "why": "official_or_lyric_video_detected",
                }
                if best is None or current["ratio"] > best["ratio"]:
                    best = current

    if best:
        _DISCOGS_VID_CACHE[cache_key] = best
        return best

    result = {"match": False, "uri": None, "release_id": None, "ratio": None, "why": "no_video_match"}
    _DISCOGS_VID_CACHE[cache_key] = result
    return result
    
def is_discogs_single(title: str, artist: str) -> bool:
    """
    Strict Discogs single detection (refined):
    - Release title must closely match the track title
    - Tracklist must contain the track as track 1
    - Release must have 1–2 tracks (true single)
    - Format must indicate a real single (not just promo)
    - Excludes promo compilations, samplers, and album promos
    """
    if not DISCOGS_TOKEN:
        return False

    headers = {
        "Authorization": f"Discogs token={DISCOGS_TOKEN}",
        "User-Agent": _DEF_USER_AGENT,
    }

    nav_title = _canon(strip_parentheses(title))

    params = {
        "q": f"{artist} {title}",
        "type": "release",
        "per_page": 10,
    }

    session = _get_discogs_session()

    try:
        _throttle_discogs()
        res = session.get(
            "https://api.discogs.com/database/search",
            headers=headers,
            params=params,
            timeout=10
        )
        if res.status_code == 429:
            _respect_retry_after(res)
        res.raise_for_status()
    except:
        return False

    results = res.json().get("results", []) or []
    if not results:
        return False

    for r in results:
        release_id = r.get("id")
        if not release_id:
            continue

        # Release title must closely match track title
        rel_title = _canon(r.get("title", ""))
        ratio = difflib.SequenceMatcher(None, rel_title, nav_title).ratio()
        if ratio < 0.70:
            continue

        # Fetch full release
        try:
            _throttle_discogs()
            rel = session.get(
                f"https://api.discogs.com/releases/{release_id}",
                headers=headers,
                timeout=10
            )
            if rel.status_code == 429:
                _respect_retry_after(rel)
            rel.raise_for_status()
        except:
            continue

        data = rel.json()

        # Reject releases with too many tracks (promo compilations)
        tracks = data.get("tracklist", [])
        if not tracks or len(tracks) > 2:
            continue

        # Track 1 must match the title
        first_track = _canon(tracks[0].get("title", ""))
        if nav_title != first_track:
            continue

        # Format validation
        formats = [f.get("name", "").lower() for f in data.get("formats", [])]
        descriptions = [
            d.lower()
            for f in data.get("formats", [])
            for d in f.get("descriptions", []) or []
        ]

        allowed_names = {"single", "vinyl", "cd", "file"}
        allowed_desc = {"single", "7\"", "7-inch"}

        # Promo-only releases are excluded unless they are 1-track promos
        if "promo" in formats or "promo" in descriptions:
            if len(tracks) == 1:
                return True
            continue

        if not (
            any(n in allowed_names for n in formats)
            or any(d in allowed_desc for d in descriptions)
        ):
            continue

        return True

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
) -> dict:
    """
    Detect if a track is a single.
    NEW ORDER:
      1. Discogs official/lyric video (first)
      2. Discogs single-format release
      3. MusicBrainz
      4. Last.fm
    """
    sources = []

    # Known singles override
    if known_list and title in known_list:
        return {"is_single": True, "confidence": "high", "sources": ["known_list"]}

    # --- 1. Discogs Official Video FIRST ---
    if discogs_token:
        try:
            dv = discogs_official_video_signal(title, artist, discogs_token=discogs_token)
            if dv.get("match"):
                return {"is_single": True, "confidence": "high", "sources": ["discogs_video"]}
        except Exception as e:
            logging.debug(f"Discogs video check failed for '{title}': {e}")

    # --- 2. Discogs Single Release ---
    if discogs_token:
        try:
            if is_discogs_single(title, artist):
                return {"is_single": True, "confidence": "high", "sources": ["discogs"]}
        except Exception as e:
            logging.debug(f"Discogs single check failed for '{title}': {e}")

    # --- 3. MusicBrainz ---
    try:
        if is_musicbrainz_single(title, artist):
            sources.append("musicbrainz")
    except Exception as e:
        logging.debug(f"MusicBrainz check failed for '{title}': {e}")

    # --- 4. Last.fm ---
    try:
        if use_lastfm and is_lastfm_single(title, artist):
            sources.append("lastfm")
    except Exception as e:
        logging.debug(f"Last.fm check failed for '{title}': {e}")

    # Confidence
    confidence = (
        "high" if len(sources) >= max(2, min_sources)
        else "medium" if len(sources) == 1
        else "low"
    )

    return {
        "is_single": len(sources) >= min_sources,
        "confidence": confidence,
        "sources": sources,
    }


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

def nav_get_all_playlists():
    url = f"{NAV_BASE_URL}/api/playlist"
    try:
        res = requests.get(url, auth=(USERNAME, PASSWORD))
        res.raise_for_status()
        return res.json().get("items", [])
    except Exception as e:
        logging.error(f"Failed to fetch playlists: {e}")
        return []

def nav_delete_playlist_by_name(name: str):
    playlists = nav_get_all_playlists()
    for pl in playlists:
        if pl.get("name") == name:
            pl_id = pl.get("id")
            if pl_id:
                try:
                    url = f"{NAV_BASE_URL}/api/playlist/{pl_id}"
                    res = requests.delete(url, auth=(USERNAME, PASSWORD))
                    res.raise_for_status()
                    logging.info(f"Deleted playlist '{name}' (id={pl_id})")
                except Exception as e:
                    logging.warning(f"Failed to delete playlist '{name}': {e}")


def nav_create_smart_playlist(name: str, rules: list, sort: list, limit: int | None = None):
    payload = {
        "name": name,
        "smart": True,
        "rules": rules,
        "sort": sort,
        "public": True  # <-- make playlist visible to all users
    }
    if limit:
        payload["limit"] = limit

    try:
        url = f"{NAV_BASE_URL}/api/playlist"
        res = requests.post(url, json=payload, auth=(USERNAME, PASSWORD))
        res.raise_for_status()
        logging.info(f"Created smart playlist '{name}' (public)")
    except Exception as e:
        logging.error(f"Failed to create smart playlist '{name}': {e}")

def create_or_update_playlist_for_artist(artist: str, tracks: list[dict]):
    total_tracks = len(tracks)
    five_star_tracks = [t for t in tracks if t.get("stars") == 5]

    playlist_name = f"Essential {artist}"

    # CASE A — 10+ five-star tracks
    if len(five_star_tracks) >= 10:
        nav_delete_playlist_by_name(playlist_name)

        rules = [
            {"field": "artist", "operator": "equals", "value": artist},
            {"field": "rating", "operator": "equals", "value": "5"}
        ]
        sort = [{"field": "random", "order": "asc"}]

        nav_create_smart_playlist(playlist_name, rules, sort)
        return

    # CASE B — 100+ total tracks
    if total_tracks >= 100:
        nav_delete_playlist_by_name(playlist_name)

        limit = max(1, math.ceil(total_tracks * 0.10))

        rules = [
            {"field": "artist", "operator": "equals", "value": artist}
        ]
        sort = [
            {"field": "rating", "order": "desc"},
            {"field": "random", "order": "asc"}
        ]

        nav_create_smart_playlist(playlist_name, rules, sort, limit)
        return

    logging.info(f"No Essential playlist created for '{artist}'")


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
    Rate all tracks for a given artist:
      - Enrich per-track metadata (Spotify, Last.fm, ListenBrainz, Age, Genres)
      - Compute adaptive source weights per album (MAD/coverage-based, clamped)
      - Album-level skip/resume (force=False)
      - Single detection (Discogs-first short-circuit)
      - Spread non-singles via robust z-bands
      - Cap density of 4★ among non-singles
      - Save to DB; optionally push ratings to Navidrome
      - Build one smart "Essential {artist}" playlist using Feishin-style API
    """

    # ----- Tunables & feature flags ------------------------------------------------
    CLAMP_MIN    = config["features"].get("clamp_min", 0.75)
    CLAMP_MAX    = config["features"].get("clamp_max", 1.25)
    CAP_TOP4_PCT = config["features"].get("cap_top4_pct", 0.25)

    ALBUM_SKIP_DAYS       = int(config["features"].get("album_skip_days", 7))
    ALBUM_SKIP_MIN_TRACKS = int(config["features"].get("album_skip_min_tracks", 1))

    use_lastfm_single = config["features"].get("use_lastfm_single", True)
    KNOWN_SINGLES     = (config.get("features", {}).get("known_singles", {}).get(artist_name, [])) or []

    SINGLE_SOURCE_WEIGHTS = {
        "discogs": 2,
        "discogs_video": 2,
        "spotify": 1,
        "short_release": 1,
        "musicbrainz": 1,
        "youtube": 1,
        "lastfm": 1,
    }

    albums = fetch_artist_albums(artist_id)
    if not albums:
        print(f"⚠️ No albums found for artist '{artist_name}'")
        return {}

    print(f"\n🎨 Starting rating for artist: {artist_name} ({len(albums)} albums)")
    rated_map = {}
    all_five_star_tracks = []

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
                    print(f"⏩ Skipping album: {album_name} (last scanned {album_last_scanned}, "
                          f"cached tracks={cached_track_count}, threshold={ALBUM_SKIP_DAYS}d)")
                    continue

        tracks = fetch_album_tracks(album_id)
        if not tracks:
            print(f"⚠️ No tracks found in album '{album_name}'")
            continue

        print(f"\n🎧 Scanning album: {album_name} ({len(tracks)} tracks)")
        album_tracks = []

        # ----------------------------------------------------------------------
        # PER-TRACK ENRICHMENT
        # ----------------------------------------------------------------------
        
        for track in tracks:
            track_id   = track["id"]
            title      = track["title"]
            file_path  = track.get("path", "")
            nav_genres = [track.get("genre")] if track.get("genre") else []
            mbid       = track.get("mbid", None)  # Navidrome MBID if available
        
            if verbose:
                print(f"   🔍 Processing track: {title}")
        
            # Spotify lookup
            spotify_results       = search_spotify_track(title, artist_name, album_name)
            selected              = select_best_spotify_match(spotify_results, title)
            sp_score              = selected.get("popularity", 0)
            spotify_album         = selected.get("album", {}).get("name", "")
            spotify_artist        = selected.get("artists", [{}])[0].get("name", "")
            spotify_genres        = selected.get("artists", [{}])[0].get("genres", [])
            spotify_release_date  = selected.get("album", {}).get("release_date", "")
            images                = selected.get("album", {}).get("images") or []
            spotify_album_art_url = images[0].get("url", "") if images and isinstance(images[0], dict) else ""
            spotify_album_type    = (selected.get("album", {}).get("album_type", "") or "").lower()
            spotify_total_tracks  = selected.get("album", {}).get("total_tracks", None)
            is_spotify_single     = (spotify_album_type == "single")
        
            # Last.fm
            lf_data        = get_lastfm_track_info(artist_name, title)
            lf_track_play  = lf_data.get("track_play", 0)
            lf_artist_play = lf_data.get("artist_play", 0)
            lf_ratio       = round((lf_track_play / lf_artist_play) * 100, 2) if lf_artist_play > 0 else 0
        
            # Global score
            score, momentum, lb_score = compute_track_score(
                title, artist_name, spotify_release_date or "1992-01-01", sp_score, mbid, verbose
            )
        
            # Genres
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
        
            # ✅ MBID logic: if missing, suggest one via MusicBrainz
            suggested_mbid = ""
            suggested_confidence = 0.0
            if not mbid:
                suggested_mbid, suggested_confidence = get_suggested_mbid(title, artist_name)
                if verbose and suggested_mbid:
                    print(f"      ↔ Suggested MBID: {suggested_mbid} (confidence {suggested_confidence})")
        
            # Build track_data dictionary
            track_data = {
                "id": track_id,
                "title": title,
                "album": album_name,
                "artist": artist_name,
                "score": score,
                "spotify_score": sp_score,
                "lastfm_ratio": lf_ratio,
                "lastfm_score": lf_ratio,
                "listenbrainz_score": lb_score,
                "age_score": momentum,
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
                "lastfm_artist_playcount": lf_artist_play,
                "file_path": file_path,
                "last_scanned": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "spotify_album_type": spotify_album_type,
                "spotify_total_tracks": spotify_total_tracks,
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
        # ADAPTIVE WEIGHTS
        # ----------------------------------------------------------------------
        base_weights = {
            'spotify': SPOTIFY_WEIGHT,
            'lastfm': LASTFM_WEIGHT,
            'listenbrainz': LISTENBRAINZ_WEIGHT
        }
        adaptive = compute_adaptive_weights(album_tracks, base_weights, clamp=(CLAMP_MIN, CLAMP_MAX), use='mad')

        for t in album_tracks:
            sp, lf, lb, age = t['spotify_score'], t['lastfm_ratio'], t['listenbrainz_score'], t['age_score']
            t['score'] = (adaptive['spotify'] * sp) + (adaptive['lastfm'] * lf) + (adaptive['listenbrainz'] * lb) + (AGE_WEIGHT * age)

        # ----------------------------------------------------------------------
        # SINGLE DETECTION
        # ----------------------------------------------------------------------
        for trk in album_tracks:
            title     = trk["title"]
            canonical = is_valid_version(title, allow_live_remix=False)

            spotify_source       = bool(trk.get("is_spotify_single"))
            tot                  = trk.get("spotify_total_tracks")
            short_release_source = (tot is not None and tot > 0 and tot <= 2)

            agg = detect_single_status(
                title, artist_name,
                youtube_api_key=YOUTUBE_API_KEY,
                discogs_token=DISCOGS_TOKEN,
                known_list=KNOWN_SINGLES,
                use_lastfm=use_lastfm_single,
                min_sources=1,
            )

            sources = []
            if spotify_source:       sources.append("spotify")
            if short_release_source: sources.append("short_release")
            sources.extend(agg.get("sources", []))
            sources_set = set(sources)

            discogs_strong = any(s in sources_set for s in ("discogs", "discogs_video"))
            if canonical and discogs_strong:
                trk["single_sources"]    = sorted(list(sources_set))
                trk["single_confidence"] = "high"
                trk["is_single"]         = True
                trk["stars"]             = 5
                continue

            weighted_count = sum(SINGLE_SOURCE_WEIGHTS.get(s, 0) for s in sources_set)
            high_combo     = (spotify_source and short_release_source)

            if discogs_strong or high_combo or weighted_count >= 3:
                single_conf = "high"
            elif weighted_count >= 2:
                single_conf = "medium"
            else:
                single_conf = "low"

            trk["single_sources"]    = sorted(list(sources_set))
            trk["single_confidence"] = single_conf

            if canonical and single_conf == "high":
                trk["is_single"] = True
                trk["stars"]     = 5

        # ----------------------------------------------------------------------
        # Z-BANDS FOR NON-SINGLES
        # ----------------------------------------------------------------------
        sorted_album = sorted(album_tracks, key=lambda x: x["score"], reverse=True)
        EPS = 1e-6
        scores_all = [t["score"] for t in sorted_album]
        med = median(scores_all)
        mad_val = max(median([abs(v - med) for v in scores_all]), EPS)

        def zrobust(x): return (x - med) / mad_val

        non_single_tracks = [t for t in sorted_album if not t.get("is_single")]
        BANDS = [
            (-float("inf"), -1.0, 1),
            (-1.0, -0.3, 2),
            (-0.3, 0.6, 3),
            (0.6, float("inf"), 4)
        ]

        for t in non_single_tracks:
            z = zrobust(t["score"])
            for lo, hi, stars in BANDS:
                if lo <= z < hi:
                    t["stars"] = stars
                    break

        top4 = [t for t in non_single_tracks if t.get("stars") == 4]
        max_top4 = max(1, round(len(non_single_tracks) * CAP_TOP4_PCT))
        if len(top4) > max_top4:
            for t in sorted(top4, key=lambda x: zrobust(x["score"]), reverse=True)[max_top4:]:
                t["stars"] = 3

        # ----------------------------------------------------------------------
        # SAVE + SYNC
        # ----------------------------------------------------------------------
        
        for trk in sorted_album:
            save_to_db(trk)
            if sync:
                sync_rating_and_report(trk)


        # ----------------------------------------------------------------------
        # ALBUM SUMMARY
        # ----------------------------------------------------------------------
        single_count = sum(1 for trk in sorted_album if trk.get("is_single"))
        print(f"   ℹ️ Singles detected: {single_count} | Non‑single 4★: {sum(1 for t in non_single_tracks if t['stars']==4)} "
              f"| Cap: {int(CAP_TOP4_PCT*100)}% | MAD: {mad_val:.2f}")

        if single_count > 0:
            print("   🎯 Singles:")
            for t in sorted_album:
                if t.get("is_single"):
                    print(f"      • {t['title']} (via {', '.join(t['single_sources'])}, conf={t['single_confidence']})")

        print(f"✔ Completed album: {album_name}")
        rated_map.update({t["id"]: t for t in sorted_album})

    # --------------------------------------------------------------------------
    # SMART PLAYLIST CREATION (Feishin-style API)
    # --------------------------------------------------------------------------
    if artist_name.lower() != "various artists" and sync and not dry_run:
        create_or_update_playlist_for_artist(artist_name, list(rated_map.values()))

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

































