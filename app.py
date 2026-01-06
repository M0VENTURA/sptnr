# --- SETUP ROUTE (for initial config/setup wizard) ---
@app.route("/setup", methods=["GET", "POST"])
def setup():
    """Setup wizard for initial configuration."""
    # This is a minimal placeholder. You may want to expand this logic as needed.
    if request.method == "POST":
        # Handle form submission or config save here if needed
        flash("Configuration saved.", "success")
        return redirect(url_for("dashboard"))
    # Render the setup page (template must exist)
    return render_template("setup.html")
import re

# --- ENVIRONMENT VARIABLE EDITING SUPPORT ---
# List of all environment variables used in the project (compiled from codebase)
ALL_ENV_VARS = [
    "SECRET_KEY", "CONFIG_PATH", "DB_PATH", "LOG_PATH", "APP_DIR", "SPTNR_DISABLE_BOOT_ND_IMPORT", "SPTNR_SKIP_SINGLES",
    "MUSIC_ROOT", "MUSIC_FOLDER", "DOWNLOADS_DIR", "POPULARITY_LOG_PATH", "POPULARITY_LOG_STDOUT", "POPULARITY_PROGRESS_FILE",
    "NAVIDROME_PROGRESS_FILE", "SINGLES_PROGRESS_FILE", "PROGRESS_FILE", "TIMEZONE", "TZ", "SPOTIFY_USER_TOKEN",
    "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_WEIGHT", "LASTFM_WEIGHT", "LISTENBRAINZ_WEIGHT", "AGE_WEIGHT",
    "LASTFMAPIKEY", "NAV_BASE_URL", "NAV_USER", "NAV_PASS", "YOUTUBE_API_KEY", "GOOGLE_CSE_ID", "GOOGLE_API_KEY",
    "TRUSTED_CHANNEL_IDS", "DISCOGS_TOKEN", "AI_API_KEY", "DEV_BOOST_WEIGHT", "AUDIODB_API_KEY", "WEB_API_KEY",
    "ENABLE_WEB_API_KEY", "MP3_PROGRESS_FILE", "BEETS_LOG_PATH", "SEARCHAPI_IO_KEY",
    "PG_HOST", "PG_PORT", "PG_USER", "PG_PASSWORD", "PG_DATABASE"
]

def get_all_env_vars():
    # Return a dict of all relevant env vars and their current values
    return {var: os.environ.get(var, "") for var in ALL_ENV_VARS}


# Place all Flask route definitions after app = Flask(__name__)

#!/usr/bin/env python3
"""
Sptnr Web UI - Flask application for managing music ratings and scans
"""

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response, send_file, session
import sqlite3
import psycopg2
import psycopg2.extras
from contextlib import closing
import yaml
import json
from datetime import datetime
import copy
from functools import wraps
from scan_helpers import scan_artist_to_db
from start import rate_artist
from popularity import scan_popularity
from start import build_artist_index
from metadata_reader import aggregate_genres_from_tracks
from check_db import update_schema
from start import save_to_db
from scan_helpers import scan_artist_to_db

import os
import sys
from beets_integration import _get_beets_client
import secrets
import subprocess
import threading
import time
import logging
import re
from api_clients.slskd import SlskdClient
from metadata_reader import get_track_metadata_from_db, find_track_file, read_mp3_metadata
import io
from helpers import create_retry_session
import difflib
import unicodedata
import requests
import hashlib

# Configure logging for web UI
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("/config/webui.log"),
        logging.StreamHandler()
    ]
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-production")
app.config['PERMANENT_SESSION_LIFETIME'] = 86400  # 24 hours



# Standardized config/database/log path variables
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/config.yaml")
DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")
LOG_PATH = os.environ.get("LOG_PATH", "/config/app.log")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(APP_DIR, "config", "config.yaml")

# Standardized PostgreSQL connection info
PG_HOST = os.environ.get("PG_HOST", "")
PG_PORT = os.environ.get("PG_PORT", "5432")
PG_USER = os.environ.get("PG_USER", "")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "")
PG_DATABASE = os.environ.get("PG_DATABASE", "")

# Ensure expected log files exist so the log viewer doesn't 404
def _ensure_log_file(path: str):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            with open(path, "a", encoding="utf-8"):
                pass
    except Exception as e:
        # Don't block app start; just log to stderr
        print(f"Warning: could not ensure log file {path}: {e}")

_ensure_log_file(LOG_PATH)
_ensure_log_file(os.path.join(os.path.dirname(CONFIG_PATH), "webui.log"))
_ensure_log_file(os.path.join(os.path.dirname(CONFIG_PATH), "sptnr.log"))
_ensure_log_file(os.path.join(os.path.dirname(CONFIG_PATH), "mp3scanner.log"))
_ensure_log_file(os.path.join(os.path.dirname(CONFIG_PATH), "popularity.log"))
_ensure_log_file(os.path.join(os.path.dirname(CONFIG_PATH), "singledetection.log"))
_ensure_log_file(os.path.join(os.path.dirname(CONFIG_PATH), "downloads.log"))
_ensure_log_file(os.path.join(os.path.dirname(CONFIG_PATH), "beets_import.log"))

# Global scan process tracker
scan_process = None  # Main scan process (batchrate, force, artist-specific)
scan_process_mp3 = None  # MP3 scanner process
scan_process_navidrome = None  # Navidrome sync process
scan_process_popularity = None  # Popularity scan process
scan_process_singles = None  # Singles detection process
scan_process_missing_releases = None  # Missing releases scan process
scan_lock = threading.Lock()

# Optional auto-import toggle (default on)
AUTO_BOOT_ND_IMPORT = os.environ.get("SPTNR_DISABLE_BOOT_ND_IMPORT", "0") != "1"


def _write_progress_file(path: str, scan_type: str, is_running: bool, extra: dict | None = None):
    """Persist minimal scan progress state so the dashboard can show status."""
    try:
        payload = {"is_running": is_running, "scan_type": scan_type}
        if extra:
            payload.update(extra)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception as e:
        logging.debug(f"Failed to write progress file {path}: {e}")


def _monitor_process_for_progress(proc: subprocess.Popen, progress_path: str, scan_type: str):
    """Wait for a subprocess and mark its progress file as complete."""
    try:
        # Capture output while process runs
        output_lines = []
        if proc.stdout:
            for line in iter(proc.stdout.readline, ''):
                if not line:
                    break
                line = line.strip()
                if line:
                    output_lines.append(line)
                    logging.info(f"[{scan_type}] {line}")
        
        # Wait for process to complete
        returncode = proc.wait()
        
        # Capture any remaining output
        if proc.stdout:
            remaining = proc.stdout.read()
            if remaining:
                for line in remaining.strip().split('\n'):
                    if line.strip():
                        output_lines.append(line.strip())
                        logging.info(f"[{scan_type}] {line.strip()}")
        
        # Mark as complete with error info if failed
        result = {
            "exit_code": returncode,
            "completed_at": datetime.now().isoformat()
        }
        
        if returncode != 0:
            error_msg = f"Process exited with code {returncode}"
            if output_lines:
                # Include last few lines of output as error context
                result["error"] = error_msg
                result["output_tail"] = output_lines[-10:]
            logging.error(f"{scan_type} failed: {error_msg}")
            if output_lines:
                logging.error(f"Last output lines: {output_lines[-5:]}")
        else:
            logging.info(f"{scan_type} completed successfully")
        
        _write_progress_file(progress_path, scan_type, False, result)
    except Exception as e:
        logging.error(f"Progress monitor failed for {scan_type}: {e}", exc_info=True)
        _write_progress_file(progress_path, scan_type, False, {
            "exit_code": -1,
            "error": str(e)
        })


def _start_boot_navidrome_import():
    """Start a Navidrome metadata-only import in the background on startup.

    Uses force=False so it only fills missing metadata, and sets the
    SPTNR_SKIP_SINGLES flag so rating/single detection cannot run during this pass.
    
    Resumes from the last scanned artist if available, otherwise starts from the beginning.
    """
    global scan_process_navidrome

    # Avoid duplicate launches if already running
    if scan_process_navidrome and isinstance(scan_process_navidrome, dict):
        t = scan_process_navidrome.get("thread")
        if t and t.is_alive():
            logging.info("Navidrome import already running; boot kickoff skipped")
            return

    def run_import():
        os.environ["SPTNR_SKIP_SINGLES"] = "1"
        progress_path = os.path.join(os.path.dirname(DB_PATH), "navidrome_scan_progress.json")
        checkpoint_path = os.path.join(os.path.dirname(DB_PATH), "navidrome_scan_checkpoint.json")
        
        try:
            logging.info("[BOOT] Starting Navidrome import-only scan (missing-only)")
            _write_progress_file(progress_path, "navidrome_scan", True, {"status": "starting", "source": "boot"})
            
            artist_map = build_artist_index()
            artists = list(artist_map.items())
            total = len(artists)
            
            # Check if we have a checkpoint from a previous scan
            start_idx = 0
            last_scanned_artist = None
            if os.path.exists(checkpoint_path):
                try:
                    with open(checkpoint_path, 'r') as f:
                        checkpoint = json.load(f)
                        last_scanned_artist = checkpoint.get("last_scanned_artist")
                        if last_scanned_artist:
                            # Find the index of the last scanned artist
                            for idx, (artist_name, _) in enumerate(artists):
                                if artist_name == last_scanned_artist:
                                    start_idx = idx + 1  # Start from the next artist
                                    logging.info(f"[BOOT] Resuming Navidrome scan from artist index {start_idx} (after '{last_scanned_artist}')")
                                    break
                except Exception as e:
                    logging.warning(f"[BOOT] Error reading checkpoint: {e}, starting from beginning")
            
            # Scan remaining artists
            for idx in range(start_idx, total):
                artist_name, info = artists[idx]
                scan_artist_to_db(artist_name, info.get("id"), verbose=False, force=False, processed_artists=idx+1, total_artists=total)
                
                # Update checkpoint with the last scanned artist
                try:
                    with open(checkpoint_path, 'w') as f:
                        json.dump({"last_scanned_artist": artist_name}, f)
                except Exception as e:
                    logging.warning(f"[BOOT] Error saving checkpoint: {e}")
            
            # Clear checkpoint when scan completes
            if os.path.exists(checkpoint_path):
                os.remove(checkpoint_path)
            
            _write_progress_file(progress_path, "navidrome_scan", False, {"status": "complete", "exit_code": 0, "source": "boot"})
            logging.info("[BOOT] Navidrome import-only scan completed")
        except Exception as e:
            logging.error(f"[BOOT] Error in Navidrome import-only scan: {e}", exc_info=True)
            _write_progress_file(progress_path, "navidrome_scan", False, {"status": "error", "error": str(e), "exit_code": 1, "source": "boot"})
        finally:
            os.environ.pop("SPTNR_SKIP_SINGLES", None)
            scan_process_navidrome = None

    thread = threading.Thread(target=run_import, daemon=True)
    thread.start()
    scan_process_navidrome = {"thread": thread, "type": "navidrome_boot"}
    logging.info("Boot Navidrome import thread started")

def _read_yaml(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
            return yaml.safe_load(content) or {}, content
    except FileNotFoundError:
        return {}, ""
    except yaml.YAMLError:
        return {}, ""


def _baseline_config():
    """Return a config structure with sensible defaults for first-run."""
    existing, _ = _read_yaml(CONFIG_PATH)
    if existing:
        return existing

    sample, _ = _read_yaml(DEFAULT_CONFIG_PATH)
    if sample:
        return sample

    return {
        "navidrome": {"base_url": "", "user": "", "pass": ""},
        "api_integrations": {
            "spotify": {"enabled": False, "client_id": "", "client_secret": ""},
            "lastfm": {"enabled": False, "api_key": ""},
            "listenbrainz": {"enabled": True},
            "discogs": {"enabled": False, "token": ""},
            "musicbrainz": {"enabled": True},
            "audiodb": {"enabled": False, "api_key": ""},
            "google": {"enabled": False, "api_key": "", "cse_id": ""},
            "youtube": {"enabled": False, "api_key": ""},
        },
        "qbittorrent": {
            "enabled": False,
            "web_url": "http://localhost:8080",
            "username": "",
            "password": ""
        },
        "slskd": {
            "enabled": False,
            "web_url": "http://localhost:5030",
            "api_key": ""
        },
        "bookmarks": {
            "enabled": True,
            "max_bookmarks": 100,
            "custom_links": []
        },
        "downloads": {
            "folder": "/downloads/Music"
        },
        "weights": {"spotify": 0.4, "lastfm": 0.3, "listenbrainz": 0.2, "age": 0.1},
        "database": {"path": DB_PATH, "vacuum_on_start": False},
        "logging": {"level": "INFO", "file": LOG_PATH, "console": True},
        "features": {
            "dry_run": False,
            "sync": True,
            "force": False,
            "verbose": False,
            "perpetual": True,
            "batchrate": False,
            "artist": [],
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
            "refresh_artist_index_on_start": False,
            "discogs_min_interval_sec": 0.35,
            "include_user_ratings_on_scan": True,
            "scan_worker_threads": 4,
            "spotify_prefetch_timeout": 30,
        },
    }


# Kick off Navidrome metadata-only import at startup (missing-only)
if AUTO_BOOT_ND_IMPORT:
    try:
        _start_boot_navidrome_import()
    except Exception as e:
        logging.error(f"Failed to start boot Navidrome import: {e}")


def _needs_setup(cfg=None):
    cfg = cfg if cfg is not None else _read_yaml(CONFIG_PATH)[0]
    
    # Check for navidrome_users list first
    nav_users = cfg.get("navidrome_users", [])
    if isinstance(nav_users, list) and nav_users:
        # At least one user with all required fields
        first_user = nav_users[0]
        required = [first_user.get("base_url"), first_user.get("user"), first_user.get("pass")]
        return any(not (v and str(v).strip()) for v in required)
    
    # Fall back to single navidrome entry
    nav = cfg.get("navidrome", {}) or {}
    required = [nav.get("base_url"), nav.get("user"), nav.get("pass")]
    return any(not (v and str(v).strip()) for v in required)


def _authenticate_navidrome(username, password):
    """Authenticate against Navidrome API"""
    cfg, _ = _read_yaml(CONFIG_PATH)
    
    # Check navidrome_users list first
    nav_users = cfg.get("navidrome_users", [])
    if isinstance(nav_users, list) and nav_users:
        for user_config in nav_users:
            if user_config.get("user") == username:
                base_url = user_config.get("base_url", "")
                nav_user = user_config.get("user", "")
                nav_pass = user_config.get("pass", "")
                
                if password == nav_pass:
                    # Verify against Navidrome API
                    try:
                        import requests
                        import hashlib
                        salt = "sptnr-auth"
                        token = hashlib.md5((password + salt).encode()).hexdigest()
                        auth_url = f"{base_url}/rest/ping?u={nav_user}&t={token}&s={salt}&v=1.16.0&c=sptnr"
                        resp = requests.get(auth_url, timeout=5)
                        if resp.status_code == 200 and "ok" in resp.text.lower():
                            return True
                    except:
                        # If API check fails, fall back to password match
                        return True
                return False
    
    # Fall back to single navidrome entry
    nav = cfg.get("navidrome", {})
    if nav.get("user") == username and nav.get("pass") == password:
        try:
            import requests
            import hashlib
            base_url = nav.get("base_url", "")
            salt = "sptnr-auth"
            token = hashlib.md5((password + salt).encode()).hexdigest()
            auth_url = f"{base_url}/rest/ping?u={username}&t={token}&s={salt}&v=1.16.0&c=sptnr"
            resp = requests.get(auth_url, timeout=5)
            if resp.status_code == 200 and "ok" in resp.text.lower():
                return True
        except:
            # If API check fails, fall back to password match
            return True
    
    return False


def login_required(f):
    """Decorator to require login for routes"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Check if config exists - if not, allow access (setup mode)
        if not os.path.exists(CONFIG_PATH):
            return f(*args, **kwargs)
        
        cfg, _ = _read_yaml(CONFIG_PATH)
        
        # If setup is needed, redirect to setup
        if _needs_setup(cfg):
            return redirect(url_for('setup'))
        
        # Check if user is logged in
        if 'username' not in session:
            return redirect(url_for('login', next=request.url))
        
        return f(*args, **kwargs)
    return decorated_function


@app.context_processor
def inject_custom_bookmarks():
    """Inject custom bookmark links into all templates"""
    try:
        cfg, _ = _read_yaml(CONFIG_PATH)
        custom_links = cfg.get('bookmarks', {}).get('custom_links', [])
        return {'custom_bookmark_links': custom_links}
    except Exception:
        return {'custom_bookmark_links': []}


@app.before_request
def enforce_setup_wizard():
    try:
        exempt = {"setup", "static", "config_edit", "config_editor", "login", "logout"}
        if not request.endpoint or request.endpoint in exempt or request.endpoint.startswith("static"):
            return

        # If config doesn't exist, allow setup
        if not os.path.exists(CONFIG_PATH):
            if request.endpoint != "setup":
                return redirect(url_for("setup"))
            return

        cfg, _ = _read_yaml(CONFIG_PATH)
        
        # If setup is needed, redirect to setup
        if _needs_setup(cfg):
            if request.endpoint != "setup":
                return redirect(url_for("setup"))
            return
        
        # If setup is complete and not logged in, redirect to login
        if 'username' not in session:
            if request.endpoint != "login":
                return redirect(url_for("login"))
    except Exception as e:
        logging.error(f"Error in enforce_setup_wizard: {e}")
        import traceback
        traceback.print_exc()
        # Don't block the request, let it fail naturally so we can see the real error
        pass


# Track if schema has been updated this session
_schema_updated = False


def get_db():
    """Get a database connection (PostgreSQL if configured, else SQLite)."""
    global _schema_updated
    if PG_HOST and PG_USER and PG_DATABASE:
        # Connect to PostgreSQL
        conn = psycopg2.connect(
            host=PG_HOST,
            port=PG_PORT,
            user=PG_USER,
            password=PG_PASSWORD,
            dbname=PG_DATABASE,
            cursor_factory=psycopg2.extras.RealDictCursor
        )
        return conn
    else:
        # Fallback to SQLite
        db_dir = os.path.dirname(DB_PATH)
        def get_db():
            """Get a standardized database connection (SQLite only)."""
            global _schema_updated
            db_dir = os.path.dirname(DB_PATH)
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)
            if not _schema_updated:
                update_schema(DB_PATH)
                _schema_updated = True
            conn = sqlite3.connect(DB_PATH, timeout=30.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            return conn

        spotify_client_id = request.form.get("spotify_client_id", "").strip()
        spotify_client_secret = request.form.get("spotify_client_secret", "").strip()
        discogs_token = request.form.get("discogs_token", "").strip()
        lastfm_api_key = request.form.get("lastfm_api_key", "").strip()

        # Initialize credential arrays from form
        nav_base_urls = request.form.getlist("nav_base_url")
        nav_users = request.form.getlist("nav_user")
        nav_passes = request.form.getlist("nav_pass")

        errors = []

        # Validate that we have at least one complete user entry
        if not nav_base_urls or not nav_users or not nav_passes:
            errors.append("At least one Navidrome user is required")
        elif len(nav_base_urls) != len(nav_users) or len(nav_users) != len(nav_passes):
            errors.append("User credential arrays have mismatched lengths")
        else:
            # Validate each user entry
            for idx, (url, user, pwd) in enumerate(zip(nav_base_urls, nav_users, nav_passes), 1):
                if not url.strip():
                    errors.append(f"User {idx}: Navidrome URL is required")
                if not user.strip():
                    errors.append(f"User {idx}: Username is required")
                if not pwd.strip():
                    errors.append(f"User {idx}: Password is required")

        if errors:
            for err in errors:
                flash(err, "danger")

            # Reconstruct user list for template
            users_list = [
                {"base_url": url, "user": user, "pass": pwd}
                for url, user, pwd in zip(nav_base_urls, nav_users, nav_passes)
            ]

            return render_template(
                "setup.html",
                nav_users=users_list,
                spotify_client_id=spotify_client_id,
                spotify_client_secret=spotify_client_secret,
                discogs_token=discogs_token,
                lastfm_api_key=lastfm_api_key,
            )

        # Build navidrome_users list
        navidrome_users = [
            {"base_url": url.strip(), "user": user.strip(), "pass": pwd.strip()}
            for url, user, pwd in zip(nav_base_urls, nav_users, nav_passes)
            if url.strip() and user.strip() and pwd.strip()
        ]

        # Get baseline config
        baseline = _baseline_config()
        new_cfg = copy.deepcopy(baseline)
        
        # Store as navidrome_users list (multi-user format)
        new_cfg["navidrome_users"] = navidrome_users
        
        # Also set single navidrome entry for backward compatibility (first user)
        if navidrome_users:
            new_cfg.setdefault("navidrome", {})
            new_cfg["navidrome"].update(navidrome_users[0])

        api = new_cfg.setdefault("api_integrations", {})

        spotify_cfg = api.setdefault("spotify", {"enabled": False, "client_id": "", "client_secret": ""})
        if spotify_client_id and spotify_client_secret:
            spotify_cfg.update({
                "enabled": True,
                "client_id": spotify_client_id,
                "client_secret": spotify_client_secret,
            })
        elif not (spotify_cfg.get("client_id") and spotify_cfg.get("client_secret")):
            spotify_cfg["enabled"] = False

        discogs_cfg = api.setdefault("discogs", {"enabled": False, "token": ""})
        if discogs_token:
            discogs_cfg.update({"enabled": True, "token": discogs_token})
        elif not discogs_cfg.get("token"):
            discogs_cfg["enabled"] = False

        lastfm_cfg = api.setdefault("lastfm", {"enabled": False, "api_key": ""})
        if lastfm_api_key:
            lastfm_cfg.update({"enabled": True, "api_key": lastfm_api_key})
        elif not lastfm_cfg.get("api_key"):
            lastfm_cfg["enabled"] = False

        new_cfg.setdefault("database", {}).setdefault("path", DB_PATH)
        new_cfg.setdefault("logging", {}).setdefault("file", LOG_PATH)

        cfg_dir = os.path.dirname(CONFIG_PATH)
        if cfg_dir:
            os.makedirs(cfg_dir, exist_ok=True)

        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(new_cfg, f, sort_keys=False, allow_unicode=False)

        user_count = len(navidrome_users)
        flash(f"Configuration saved with {user_count} Navidrome user(s). You are ready to start your first scan.", "success")
        return redirect(url_for("dashboard"))

    # GET request - load existing config
    # Check for navidrome_users list first, then fall back to single navidrome entry
    nav_users_list = cfg.get("navidrome_users", []) if cfg else []
    
    # If no navidrome_users, check for single navidrome entry
    if not nav_users_list:
        nav_single = cfg.get("navidrome", {}) if cfg else baseline.get("navidrome", {})
        if nav_single and nav_single.get("base_url"):
            nav_users_list = [nav_single]
    
    # If still empty, provide default empty structure
    if not nav_users_list:
        nav_users_list = []
    
    api_defaults = cfg.get("api_integrations", {}) if cfg else baseline.get("api_integrations", {})

    return render_template(
        "setup.html",
        nav_users=nav_users_list,
        nav_base_url=nav_users_list[0].get("base_url", "") if nav_users_list else "",
        nav_user=nav_users_list[0].get("user", "") if nav_users_list else "",
        nav_pass=nav_users_list[0].get("pass", "") if nav_users_list else "",
        spotify_client_id=api_defaults.get("spotify", {}).get("client_id", ""),
        spotify_client_secret=api_defaults.get("spotify", {}).get("client_secret", ""),
        discogs_token=api_defaults.get("discogs", {}).get("token", ""),
        lastfm_api_key=api_defaults.get("lastfm", {}).get("api_key", ""),
    )


@app.route("/")
def index():
    """Redirect to dashboard"""
    return redirect(url_for("dashboard"))


@app.route("/dashboard")
def dashboard():
    """Main dashboard with statistics"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(DISTINCT artist) FROM tracks")
        artist_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(DISTINCT album) FROM tracks")
        album_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM tracks")
        track_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM tracks WHERE stars = 5")
        five_star_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM tracks WHERE is_single = 1")
        singles_count = cursor.fetchone()[0]
        
        conn.close()
        
        # Get recent scans from scan_history table
        from scan_history import get_recent_album_scans
        recent_scans = get_recent_album_scans(limit=10)
        
        with scan_lock:
            web_ui_running = scan_process is not None and scan_process.poll() is None
        
        # Check if background scan from start.py is running
        lock_file_path = os.path.join(os.path.dirname(CONFIG_PATH), ".scan_lock")
        background_running = os.path.exists(lock_file_path)
        
        scan_running = web_ui_running or background_running
        
        # Get Navidrome users from config
        cfg, _ = _read_yaml(CONFIG_PATH)
        nav_users_list = cfg.get("navidrome_users", [])
        if not nav_users_list and cfg.get("navidrome"):
            # Single user mode - convert to list format for consistency
            nav_users_list = [cfg.get("navidrome")]
        
        return render_template("dashboard.html",
                             artist_count=artist_count,
                             album_count=album_count,
                             track_count=track_count,
                             five_star_count=five_star_count,
                             singles_count=singles_count,
                             recent_scans=recent_scans,
                             scan_running=scan_running,
                             nav_users=nav_users_list)
    except Exception as e:
        logging.error(f"Dashboard error: {e}")
        import traceback
        traceback.print_exc()
        # Render a minimal dashboard with error message instead of redirecting
        return render_template("dashboard.html",
                             artist_count=0,
                             album_count=0,
                             track_count=0,
                             five_star_count=0,
                             singles_count=0,
                             recent_scans=[],
                             scan_running=False,
                             nav_users=[],
                             error=str(e))


@app.route("/artists")
def artists():
    """List all artists"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT 
            artist,
            COUNT(DISTINCT album) as album_count,
            COUNT(*) as track_count,
            SUM(CASE WHEN is_single = 1 THEN 1 ELSE 0 END) as single_count,
            MAX(last_scanned) as last_updated
        FROM tracks
        GROUP BY artist
        
        UNION ALL
        
        SELECT
            artist_name as artist,
            0 as album_count,
            0 as track_count,
            0 as single_count,
            last_updated
        FROM artist_stats
        WHERE artist_name NOT IN (SELECT DISTINCT artist FROM tracks)
        
        ORDER BY artist COLLATE NOCASE
    """)
    artists_data = cursor.fetchall()
    conn.close()
    
    return render_template("artists.html", artists=artists_data, DB_PATH=DB_PATH)


@app.route("/search")
def search():
    """Search page for artists, albums, and tracks"""
    return render_template("search.html")


@app.route("/api/search", methods=["POST"])
def api_search():
    """API endpoint to search the library for artists, albums, and tracks"""
    try:
        data = request.get_json()
        query = data.get("query", "").strip().lower()
        
        if not query or len(query) < 2:
            return jsonify({"error": "Search query must be at least 2 characters"}), 400
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Prepare search pattern for LIKE queries
        search_pattern = f"%{query}%"
        
        # Search artists
        cursor.execute("""
            SELECT 
                artist as name,
                COUNT(DISTINCT album) as album_count,
                COUNT(*) as track_count
            FROM tracks
            WHERE LOWER(artist) LIKE LOWER(?)
            GROUP BY artist
            ORDER BY track_count DESC
            LIMIT 20
        """, (search_pattern,))
        artists_results = [
            {
                "name": row["name"],
                "album_count": row["album_count"],
                "track_count": row["track_count"]
            }
            for row in cursor.fetchall()
        ]
        
        # Search albums
        cursor.execute("""
            SELECT 
                artist,
                album,
                COUNT(*) as track_count,
                AVG(stars) as avg_stars
            FROM tracks
            WHERE LOWER(album) LIKE LOWER(?)
            GROUP BY artist, album
            ORDER BY track_count DESC
            LIMIT 20
        """, (search_pattern,))
        albums_results = [
            {
                "artist": row["artist"],
                "album": row["album"],
                "track_count": row["track_count"],
                "avg_stars": row["avg_stars"]
            }
            for row in cursor.fetchall()
        ]
        
        # Search tracks
        cursor.execute("""
            SELECT 
                id,
                title,
                artist,
                album,
                stars
            FROM tracks
            WHERE LOWER(title) LIKE LOWER(?) OR LOWER(artist) LIKE LOWER(?)
            ORDER BY stars DESC, title COLLATE NOCASE
            LIMIT 50
        """, (search_pattern, search_pattern))
        tracks_results = [
            {
                "id": row["id"],
                "title": row["title"],
                "artist": row["artist"],
                "album": row["album"],
                "stars": row["stars"]
            }
            for row in cursor.fetchall()
        ]
        
        conn.close()
        
        return jsonify({
            "artists": artists_results,
            "albums": albums_results,
            "tracks": tracks_results
        })
    
    except Exception as e:
        logging.error(f"Search error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/artist/<path:name>")
def artist_detail(name):
    """View artist details and albums"""
    try:
        # URL decode the artist name
        from urllib.parse import unquote
        name = unquote(name)
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Get albums for this artist
        cursor.execute("""
            SELECT 
                album,
                COUNT(*) as track_count,
                AVG(stars) as avg_stars,
                SUM(CASE WHEN is_single = 1 THEN 1 ELSE 0 END) as singles_count,
                MAX(last_scanned) as last_updated,
                MIN(year) as album_year
            FROM tracks
            WHERE artist = ?
            GROUP BY album
            ORDER BY (album_year IS NULL), album_year DESC, album COLLATE NOCASE
        """, (name,))
        albums_data = cursor.fetchall()
        
        # Get artist stats with additional metrics
        try:
            cursor.execute("""
                SELECT 
                    COUNT(*) as track_count,
                    COUNT(DISTINCT album) as album_count,
                    AVG(stars) as avg_stars,
                    SUM(CASE WHEN stars = 5 THEN 1 ELSE 0 END) as five_star_count,
                    SUM(COALESCE(duration, 0)) as total_duration,
                    MIN(year) as earliest_year,
                    MAX(year) as latest_year,
                    MAX(beets_artist_mbid) as beets_artist_mbid
                FROM tracks
                WHERE artist = ?
            """, (name,))
        except:
            # Fallback for databases without beets columns
            cursor.execute("""
                SELECT 
                    COUNT(*) as track_count,
                    COUNT(DISTINCT album) as album_count,
                    AVG(stars) as avg_stars,
                    SUM(CASE WHEN stars = 5 THEN 1 ELSE 0 END) as five_star_count,
                    SUM(COALESCE(duration, 0)) as total_duration,
                    MIN(year) as earliest_year,
                    MAX(year) as latest_year,
                    NULL as beets_artist_mbid
                FROM tracks
                WHERE artist = ?
            """, (name,))
        
        artist_stats = cursor.fetchone()
        
        conn.close()
        
        # Convert Row to dict for template access
        if artist_stats:
            artist_stats = dict(artist_stats)
        
        # Aggregate genres from all tracks by this artist
        genres = aggregate_genres_from_tracks(name, DB_PATH)
        
        # Get qBittorrent config
        cfg, _ = _read_yaml(CONFIG_PATH)
        qbit_config = cfg.get("qbittorrent", {"enabled": False, "web_url": "http://localhost:8080"})
        
        return render_template("artist.html", 
                             artist_name=name,
                             albums=albums_data,
                             stats=artist_stats,
                             genres=genres,
                             qbit_config=qbit_config)
    except Exception as e:
        logging.error(f"Error loading artist details: {str(e)}")
        flash(f"Error loading artist: {str(e)}", "error")
        return redirect(url_for("artists"))


def _normalize_release_title(text: str) -> str:
    """Normalize release titles for comparison."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", text)
    text = re.sub(r"(?i)\b(remaster(?:ed)?\s*\d{0,4}|remaster|deluxe|live|mono|stereo|edit|mix|version|bonus track)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _fetch_musicbrainz_releases(artist_name: str, limit: int = 100) -> list[dict]:
    """
    Fetch release-groups from MusicBrainz for an artist with retry logic.
    
    Handles SSL errors, timeouts, and other network issues with exponential backoff.
    """
    if not artist_name:
        return []
    
    headers = {"User-Agent": "sptnr-web/1.0 (support@example.com)"}
    releases: list[dict] = []
    query = f'artist:"{artist_name}" AND (primarytype:album OR primarytype:ep OR primarytype:single)'
    url = "https://musicbrainz.org/ws/2/release-group"
    params = {"fmt": "json", "limit": limit, "query": query}
    
    # Retry with exponential backoff
    max_retries = 3
    base_delay = 1
    
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            for rg in data.get("release-groups", []) or []:
                rg_id = rg.get("id", "")
                primary_type = rg.get("primary-type", "")
                releases.append({
                    "id": rg_id,
                    "title": rg.get("title", ""),
                    "primary_type": primary_type,
                    "first_release_date": rg.get("first-release-date", ""),
                    "secondary_types": rg.get("secondary-types", []),
                    "cover_art_url": f"https://coverartarchive.org/release-group/{rg_id}/front-500" if rg_id else "",
                })
            return releases  # Success
        except requests.exceptions.Timeout:
            logging.debug(f"MusicBrainz timeout (attempt {attempt+1}/{max_retries}) for {artist_name}")
            if attempt < max_retries - 1:
                time.sleep(base_delay * (2 ** attempt))  # Exponential backoff
        except requests.exceptions.ConnectionError as e:
            # Includes SSLEOFError and other connection issues
            logging.debug(f"MusicBrainz connection error (attempt {attempt+1}/{max_retries}) for {artist_name}: {type(e).__name__}")
            if attempt < max_retries - 1:
                time.sleep(base_delay * (2 ** attempt))
        except requests.exceptions.RequestException as e:
            logging.debug(f"MusicBrainz request error for {artist_name}: {e}")
            break  # Don't retry for other request errors
        except Exception as e:
            logging.debug(f"Unexpected error fetching MusicBrainz releases for {artist_name}: {e}")
            break
    
    return releases


@app.route("/api/artist/missing-releases", methods=["GET"])
def api_artist_missing_releases():
    """Detect missing releases for an artist by comparing to MusicBrainz."""
    artist = request.args.get("artist", "").strip()
    if not artist:
        return jsonify({"error": "Artist is required"}), 400

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT album FROM tracks WHERE artist = ?
    """, (artist,))
    existing_albums = [row[0] for row in cursor.fetchall()]
    conn.close()

    existing_norm = {_normalize_release_title(a) for a in existing_albums if a}

    mb_releases = _fetch_musicbrainz_releases(artist)
    missing = []
    for rg in mb_releases:
        norm_title = _normalize_release_title(rg.get("title"))
        if not norm_title or norm_title in existing_norm:
            continue
        # skip compilations/secondary types if present
        secondary = [s.lower() for s in rg.get("secondary_types") or []]
        if "compilation" in secondary:
            continue
        primary_type = (rg.get("primary_type") or "").lower()
        category = "Album"
        if primary_type == "ep":
            category = "EP"
        elif primary_type == "single" or "single" in secondary:
            category = "Single"

        missing.append({
            "id": rg.get("id", ""),
            "title": rg.get("title", ""),
            "primary_type": rg.get("primary_type", ""),
            "first_release_date": rg.get("first_release_date", ""),
            "secondary_types": rg.get("secondary_types", []),
            "cover_art_url": rg.get("cover_art_url", ""),
            "category": category,
        })

    return jsonify({
        "artist": artist,
        "missing": missing,
        "total_musicbrainz": len(mb_releases),
        "existing_albums": existing_albums,
    })


@app.route("/api/artist/import-release", methods=["POST"])
def api_import_release():
    """Import a MusicBrainz release with full tracklisting to the database."""
    data = request.json or {}
    artist = data.get("artist", "").strip()
    release_id = data.get("release_id", "").strip()
    title = data.get("title", "").strip()
    
    if not artist or not release_id or not title:
        return jsonify({"error": "Artist, release_id, and title are required"}), 400
    
    try:
        # Fetch release details from MusicBrainz including media and recordings
        mb_url = f"https://musicbrainz.org/ws/2/release/{release_id}"
        headers = {"User-Agent": "sptnr-cli/2.1 (support@example.com)"}
        response = requests.get(
            mb_url,
            params={
                "fmt": "json",
                "inc": "recordings+artist-relations+release-groups"
            },
            headers=headers,
            timeout=15
        )
        response.raise_for_status()
        release_data = response.json()
        
        if not release_data:
            return jsonify({"error": "Release not found on MusicBrainz"}), 404
        
        # Extract media and tracks
        media = release_data.get("media", [])
        if not media:
            return jsonify({"error": "Release has no media/tracks"}), 400
        
        conn = get_db()
        cursor = conn.cursor()
        imported_count = 0
        
        # Get year from release-date
        year = release_data.get("date", "")
        if year:
            year = year[:4]
        
        # Process each track from all media (discs)
        for disc_idx, disc in enumerate(media, start=1):
            disc_number = disc.get("position", disc_idx)
            tracks_list = disc.get("tracks", [])
            
            for track_idx, track in enumerate(tracks_list, start=1):
                recording = track.get("recording", {})
                track_title = recording.get("title") or track.get("title") or "Unknown"
                duration = recording.get("length")
                mbid = recording.get("id", "")
                
                # Build track record
                track_record = {
                    "id": recording.get("id", f"{release_id}_{disc_number}_{track_idx}"),
                    "title": track_title,
                    "artist": artist,
                    "album": title,
                    "track_number": track_idx,
                    "disc_number": disc_number,
                    "duration": duration,
                    "year": year,
                    "mbid": mbid,
                    "score": 0.0,
                    "spotify_score": 0,
                    "lastfm_score": 0,
                    "listenbrainz_score": 0,
                    "age_score": 0,
                    "genres": [],
                    "file_path": None,
                    "stars": 0,
                    "last_scanned": datetime.now().isoformat(),
                }
                
                # Insert or update track in database
                save_to_db(track_record)
                imported_count += 1
        
        conn.close()
        
        logging.info(f"[IMPORT] Imported {imported_count} tracks from '{title}' by {artist} (MB ID: {release_id})")
        
        return jsonify({
            "success": True,
            "message": f"Imported {imported_count} tracks from '{title}'",
            "tracks_imported": imported_count,
            "artist": artist,
            "album": title
        })
        
    except requests.exceptions.HTTPError as e:
        logging.error(f"[IMPORT] MusicBrainz API error: {e}")
        return jsonify({"error": f"MusicBrainz API error: {e.response.status_code}"}), 500
    except Exception as e:
        logging.error(f"[IMPORT] Error importing release: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/artist/scan-all-missing-releases", methods=["POST"])
def api_scan_all_missing_releases():
    """Scan all artists in database for missing releases and cache results."""
    global scan_process_missing_releases
    
    # Check if already running
    if scan_process_missing_releases and isinstance(scan_process_missing_releases, dict):
        thread = scan_process_missing_releases.get('thread')
        if thread and thread.is_alive():
            return jsonify({"error": "Missing releases scan already running"}), 400
    
    def run_missing_releases_scan():
        try:
            conn = get_db()
            cursor = conn.cursor()
            
            # Get all distinct artists from tracks table
            cursor.execute("SELECT DISTINCT artist FROM tracks WHERE artist IS NOT NULL AND artist != '' ORDER BY artist")
            artists = [row[0] for row in cursor.fetchall()]
            total_artists = len(artists)
            
            logging.info(f"[MISSING_RELEASES] Starting scan for {total_artists} artists")
            
            # Clear old missing releases data
            cursor.execute("DELETE FROM missing_releases")
            conn.commit()
            
            processed = 0
            total_missing = 0
            
            for artist_name in artists:
                try:
                    processed += 1
                    
                    # Update progress file
                    progress_data = {
                        "is_running": True,
                        "scan_type": "missing_releases_scan",
                        "current_artist": artist_name,
                        "processed_artists": processed,
                        "total_artists": total_artists,
                        "total_missing_found": total_missing,
                        "percent_complete": int((processed / total_artists * 100)) if total_artists > 0 else 0
                    }
                    
                    progress_file = os.path.join(os.path.dirname(DB_PATH), "missing_releases_scan_progress.json")
                    with open(progress_file, 'w') as f:
                        json.dump(progress_data, f)
                    
                    # Get existing albums for this artist
                    cursor.execute("SELECT DISTINCT album FROM tracks WHERE artist = ?", (artist_name,))
                    existing_albums = [row[0] for row in cursor.fetchall()]
                    existing_norm = {_normalize_release_title(a) for a in existing_albums if a}
                    
                    # Fetch MusicBrainz releases
                    mb_releases = _fetch_musicbrainz_releases(artist_name)
                    
                    # Check for missing releases AND update cover art for existing albums
                    for rg in mb_releases:
                        norm_title = _normalize_release_title(rg.get("title"))
                        cover_art_url = rg.get("cover_art_url", "")
                        
                        # If album exists, update its cover art
                        if norm_title and norm_title in existing_norm:
                            if cover_art_url:
                                # Update cover_art_url for all tracks in this album
                                original_album = next((a for a in existing_albums if _normalize_release_title(a) == norm_title), None)
                                if original_album:
                                    cursor.execute("""
                                        UPDATE tracks 
                                        SET cover_art_url = ?
                                        WHERE artist = ? AND album = ?
                                    """, (cover_art_url, artist_name, original_album))
                            continue
                        
                        if not norm_title:
                            continue
                        
                        # Skip compilations
                        secondary = [s.lower() for s in rg.get("secondary_types") or []]
                        if "compilation" in secondary:
                            continue
                        
                        primary_type = (rg.get("primary_type") or "").lower()
                        category = "Album"
                        if primary_type == "ep":
                            category = "EP"
                        elif primary_type == "single" or "single" in secondary:
                            category = "Single"
                        
                        # Insert missing release into database
                        cursor.execute("""
                            INSERT OR REPLACE INTO missing_releases 
                            (artist, release_id, title, primary_type, first_release_date, cover_art_url, category, last_checked)
                            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        """, (
                            artist_name,
                            rg.get("id", ""),
                            rg.get("title", ""),
                            rg.get("primary_type", ""),
                            rg.get("first_release_date", ""),
                            cover_art_url,
                            category
                        ))
                        total_missing += 1
                    
                    conn.commit()
                    
                    # Rate limiting
                    time.sleep(1.1)  # MusicBrainz rate limit: 1 request per second
                    
                except Exception as e:
                    logging.error(f"[MISSING_RELEASES] Error scanning {artist_name}: {e}")
                    continue
            
            # Write final progress
            progress_data = {
                "is_running": False,
                "scan_type": "missing_releases_scan",
                "processed_artists": total_artists,
                "total_artists": total_artists,
                "total_missing_found": total_missing,
                "percent_complete": 100,
                "status": "complete"
            }
            with open(progress_file, 'w') as f:
                json.dump(progress_data, f)
            
            conn.close()
            logging.info(f"[MISSING_RELEASES] Scan complete. Found {total_missing} missing releases across {total_artists} artists")
            
        except Exception as e:
            logging.error(f"[MISSING_RELEASES] Scan failed: {e}", exc_info=True)
            progress_data = {
                "is_running": False,
                "scan_type": "missing_releases_scan",
                "status": "error",
                "error": str(e)
            }
            try:
                with open(progress_file, 'w') as f:
                    json.dump(progress_data, f)
            except:
                pass
    
    # Start scan in background thread
    scan_thread = threading.Thread(target=run_missing_releases_scan, daemon=False)
    scan_thread.start()
    scan_process_missing_releases = {'thread': scan_thread, 'type': 'missing_releases'}
    
    return jsonify({
        "success": True,
        "message": "Missing releases scan started"
    })


@app.route("/api/artist/cached-missing-releases", methods=["GET"])
def api_cached_missing_releases():
    """Get cached missing releases for an artist from the database."""
    artist = request.args.get("artist", "").strip()
    if not artist:
        return jsonify({"error": "Artist is required"}), 400
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT release_id, title, primary_type, first_release_date, cover_art_url, category, last_checked
            FROM missing_releases
            WHERE artist = ?
            ORDER BY first_release_date DESC
        """, (artist,))
        
        rows = cursor.fetchall()
        conn.close()
        
        missing = []
        for row in rows:
            missing.append({
                "id": row[0],
                "title": row[1],
                "primary_type": row[2],
                "first_release_date": row[3],
                "cover_art_url": row[4],
                "category": row[5],
                "last_checked": row[6]
            })
        
        return jsonify({
            "artist": artist,
            "missing": missing,
            "from_cache": True
        })
        
    except Exception as e:
        logging.error(f"[MISSING_RELEASES] Error fetching cached data: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/artist/bio")
def api_artist_bio():
    """Get artist biography from MusicBrainz with Discogs fallback"""
    artist_name = request.args.get("name", "").strip()
    if not artist_name:
        return jsonify({"error": "Artist name required"}), 400
    
    try:
        # First, get artist MBID from database
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT beets_artist_mbid FROM tracks WHERE artist = ? AND beets_artist_mbid IS NOT NULL LIMIT 1", (artist_name,))
        row = cursor.fetchone()
        conn.close()
        
        artist_mbid = row['beets_artist_mbid'] if row else None
        bio = ""
        source = "Unknown"
        
        # Try MusicBrainz first with shorter timeout
        if not artist_mbid:
            try:
                search_url = "https://musicbrainz.org/ws/2/artist"
                params = {"query": f'artist:"{artist_name}"', "fmt": "json", "limit": 1}
                headers = {"User-Agent": "sptnr-web/1.0 (support@example.com)"}
                
                resp = requests.get(search_url, params=params, headers=headers, timeout=5)
                resp.raise_for_status()
                data = resp.json()
                
                artists = data.get("artists", [])
                if artists:
                    artist_mbid = artists[0].get("id")
            except Exception as e:
                logging.debug(f"MusicBrainz artist search failed: {e}")
                artist_mbid = None
        
        # Fetch from MusicBrainz if we have MBID
        if artist_mbid:
            try:
                artist_url = f"https://musicbrainz.org/ws/2/artist/{artist_mbid}"
                params = {"fmt": "json", "inc": "annotation"}
                headers = {"User-Agent": "sptnr-web/1.0 (support@example.com)"}
                
                resp = requests.get(artist_url, params=params, headers=headers, timeout=5)
                resp.raise_for_status()
                artist_data = resp.json()
                
                bio = artist_data.get("annotation", {}).get("text", "") or artist_data.get("disambiguation", "")
                if bio:
                    source = "MusicBrainz"
            except Exception as e:
                logging.debug(f"MusicBrainz bio fetch failed: {e}")
                bio = ""
        
        # Fallback to Discogs if MusicBrainz failed or returned empty
        if not bio:
            try:
                from api_clients.discogs import DiscogsClient
                discogs_client = DiscogsClient()
                artist_data = discogs_client.search_artist(artist_name)
                if artist_data:
                    bio = artist_data.get("profile", "")
                    if bio:
                        source = "Discogs"
            except Exception as e:
                logging.debug(f"Discogs bio fetch failed: {e}")
        
        return jsonify({
            "bio": bio,
            "source": source,
            "artist_mbid": artist_mbid
        })
        
    except Exception as e:
        logging.error(f"Error fetching artist bio from all sources: {e}")
        return jsonify({"bio": "", "source": "Error"}), 200


@app.route("/api/artist/singles-count")
def api_artist_singles_count():
    """Get count of singles for an artist"""
    artist_name = request.args.get("name", "").strip()
    if not artist_name:
        return jsonify({"error": "Artist name required"}), 400
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as count FROM tracks WHERE artist = ? AND is_single = 1", (artist_name,))
        row = cursor.fetchone()
        conn.close()
        
        count = row['count'] if row else 0
        return jsonify({"count": count})
        
    except Exception as e:
        logging.error(f"Error fetching singles count: {e}")
        return jsonify({"count": 0, "error": str(e)}), 500


@app.route("/api/artist/create-essential-playlist", methods=["POST"])
def api_create_essential_playlist():
    """Create an Essential Playlist for an artist using single detection logic"""
    data = request.json or {}
    artist_name = data.get("artist", "").strip()
    
    if not artist_name:
        return jsonify({"error": "Artist name required"}), 400
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Get all singles with high confidence
        cursor.execute("""
            SELECT id, title, album, stars, score, single_confidence 
            FROM tracks 
            WHERE artist = ? AND is_single = 1
            ORDER BY 
                CASE single_confidence 
                    WHEN 'high' THEN 3
                    WHEN 'medium' THEN 2
                    WHEN 'low' THEN 1
                    ELSE 0
                END DESC,
                score DESC,
                stars DESC
            LIMIT 50
        """, (artist_name,))
        
        singles = cursor.fetchall()
        conn.close()
        
        if not singles:
            return jsonify({"error": "No singles found for this artist"}), 404
        
        # Create playlist name
        playlist_name = f"{artist_name} - Essential"
        
        # For now, just return success - Navidrome playlist creation would go here
        logging.info(f"Created essential playlist for {artist_name} with {len(singles)} tracks")
        
        return jsonify({
            "success": True,
            "message": f"Created Essential Playlist with {len(singles)} tracks",
            "playlist_name": playlist_name,
            "track_count": len(singles)
        })
        
    except Exception as e:
        logging.error(f"Error creating essential playlist: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/artist/image")
def api_artist_image():
    """Get artist image - from database or placeholder"""
    artist_name = request.args.get("name", "").strip()
    if not artist_name:
        svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">
            <rect fill="#2a2a2a" width="200" height="200"/>
            <text x="50%" y="50%" text-anchor="middle" dy=".3em" fill="#666" font-size="16">No Image</text>
        </svg>'''
        return Response(svg, mimetype='image/svg+xml')
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        # Check if we have custom artist_images table
        cursor.execute("""
            SELECT image_url FROM artist_images WHERE artist_name = ?
        """, (artist_name,))
        row = cursor.fetchone()
        conn.close()
        
        if row and row['image_url']:
            # Redirect to the stored image URL
            return redirect(row['image_url'])
        
    except Exception as e:
        logging.error(f"Error fetching artist image: {e}")
    
    # Return placeholder
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">
        <rect fill="#2a2a2a" width="200" height="200"/>
        <text x="50%" y="50%" text-anchor="middle" dy=".3em" fill="#666" font-size="16">No Image</text>
    </svg>'''
    return Response(svg, mimetype='image/svg+xml')


@app.route("/api/artist/search-images")
def api_artist_search_images():
    """Search for artist images on MusicBrainz, Discogs, or Spotify"""
    artist_name = request.args.get("name", "").strip()
    source = request.args.get("source", "musicbrainz").strip()
    
    if not artist_name:
        return jsonify({"error": "Artist name required"}), 400
    
    try:
        images = []
        
        if source == "musicbrainz":
            # Get artist MBID
            search_url = "https://musicbrainz.org/ws/2/artist"
            params = {"query": f'artist:"{artist_name}"', "fmt": "json", "limit": 5}
            headers = {"User-Agent": "sptnr-web/1.0"}
            
            resp = requests.get(search_url, params=params, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            
            for artist in data.get("artists", [])[:5]:
                mbid = artist.get("id")
                if mbid:
                    # Try to get image from CAA
                    image_url = f"https://coverartarchive.org/artist/{mbid}/front-500"
                    images.append({"url": image_url, "source": "MusicBrainz CAA"})
        
        elif source == "discogs":
            # Search Discogs for artist
            from singledetection import _discogs_search, _get_discogs_session
            from helpers import _read_yaml
            
            config_data, _ = _read_yaml(CONFIG_PATH)
            discogs_config = config_data.get("api_integrations", {}).get("discogs", {})
            discogs_token = discogs_config.get("token", "")
            
            session = _get_discogs_session()
            headers = {"User-Agent": "Sptnr/1.0"}
            if discogs_token:
                headers["Authorization"] = f"Discogs token={discogs_token}"
            
            results = _discogs_search(session, headers, artist_name, kind="artist", per_page=5)
            
            for result in results[:5]:
                if result.get("thumb"):
                    images.append({"url": result["thumb"], "source": "Discogs"})
        
        elif source == "spotify":
            # Search Spotify for artist
            from api_clients.spotify import SpotifyClient
            from helpers import _read_yaml
            
            config_data, _ = _read_yaml(CONFIG_PATH)
            spotify = SpotifyClient(config_data)
            
            if spotify.sp:
                results = spotify.sp.search(q=f'artist:{artist_name}', type='artist', limit=5)
                
                for artist in results.get('artists', {}).get('items', []):
                    artist_images = artist.get('images', [])
                    if artist_images:
                        # Get the medium-sized image (usually index 1)
                        img_url = artist_images[1]['url'] if len(artist_images) > 1 else artist_images[0]['url']
                        images.append({"url": img_url, "source": "Spotify"})
        
        return jsonify({"images": images})
        
    except Exception as e:
        logging.error(f"Error searching artist images: {e}")
        import traceback
        logging.error(traceback.format_exc())
        return jsonify({"error": str(e), "images": []}), 500


@app.route("/api/artist/set-image", methods=["POST"])
def api_artist_set_image():
    """Set custom artist image"""
    data = request.json or {}
    artist_name = data.get("artist", "").strip()
    image_url = data.get("image_url", "").strip()
    
    if not artist_name or not image_url:
        return jsonify({"error": "Artist name and image URL required"}), 400
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Create artist_images table if it doesn't exist
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS artist_images (
                artist_name TEXT PRIMARY KEY,
                image_url TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        
        # Insert or update
        cursor.execute("""
            INSERT OR REPLACE INTO artist_images (artist_name, image_url, updated_at)
            VALUES (?, ?, ?)
        """, (artist_name, image_url, datetime.now().isoformat()))
        
        conn.commit()
        conn.close()
        
        return jsonify({"success": True, "message": "Artist image updated"})
        
    except Exception as e:
        logging.error(f"Error setting artist image: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/album/search-art")
def api_album_search_art():
    """Search for album art on MusicBrainz, Discogs, or Spotify"""
    artist_name = request.args.get("artist", "").strip()
    album_name = request.args.get("album", "").strip()
    source = request.args.get("source", "musicbrainz").strip()
    
    if not artist_name or not album_name:
        return jsonify({"error": "Artist and album name required"}), 400
    
    logger = logging.getLogger('sptnr')
    try:
        images = []
        
        if source == "musicbrainz":
            # Search for release-group
            search_url = "https://musicbrainz.org/ws/2/release-group"
            params = {"query": f'release:"{album_name}" AND artist:"{artist_name}"', "fmt": "json", "limit": 20}
            headers = {"User-Agent": "sptnr-web/1.0"}
            
            resp = requests.get(search_url, params=params, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            
            logger.debug(f"MusicBrainz search returned {len(data.get('release-groups', []))} results")
            
            for rg in data.get("release-groups", [])[:20]:
                rg_id = rg.get("id")
                if rg_id:
                    # Try multiple image formats from CAA
                    for image_format in ["front-500", "front-250", "front"]:
                        image_url = f"https://coverartarchive.org/release-group/{rg_id}/{image_format}"
                        
                        # Verify the URL exists before adding
                        try:
                            head_resp = requests.head(image_url, timeout=3)
                            if head_resp.status_code == 200:
                                images.append({
                                    "url": image_url,
                                    "source": "MusicBrainz CAA",
                                    "title": rg.get("title", ""),
                                    "artist": rg.get("artist-credit", [{}])[0].get("name", "") if rg.get("artist-credit") else ""
                                })
                                break  # Found one, don't try other formats for this RG
                        except Exception as e:
                            logger.debug(f"HEAD request failed for {image_url}: {e}")
                            continue
        
        elif source == "discogs":
            # Search Discogs for release
            from singledetection import _discogs_search, _get_discogs_session
            from helpers import _read_yaml
            
            config_data, _ = _read_yaml(CONFIG_PATH)
            discogs_config = config_data.get("api_integrations", {}).get("discogs", {})
            discogs_token = discogs_config.get("token", "")
            
            session = _get_discogs_session()
            headers = {"User-Agent": "Sptnr/1.0"}
            if discogs_token:
                headers["Authorization"] = f"Discogs token={discogs_token}"
            
            # Search with album and artist - try different query formats
            for query in [f"{artist_name} {album_name}", f'"{album_name}" {artist_name}', album_name]:
                try:
                    logger.debug(f"Searching Discogs with query: {query}")
                    results = _discogs_search(session, headers, query, kind="release", per_page=15)
                    
                    for result in results[:15]:
                        if result.get("cover_image"):
                            # Verify the image URL is valid
                            try:
                                img_resp = requests.head(result["cover_image"], timeout=3)
                                if img_resp.status_code == 200:
                                    images.append({
                                        "url": result["cover_image"],
                                        "source": "Discogs",
                                        "title": result.get("title", ""),
                                        "artist": ", ".join([a.get("name", "") for a in result.get("artists", [])])
                                    })
                            except Exception as e:
                                logger.debug(f"Image verification failed for Discogs: {e}")
                                continue
                    
                    if images:
                        logger.debug(f"Found {len(images)} images on Discogs")
                        break  # Stop if we found images
                except Exception as e:
                    logger.debug(f"Discogs search with query '{query}' failed: {e}")
                    continue
        
        elif source == "spotify":
            # Search Spotify for album
            from api_clients.spotify import SpotifyClient
            from helpers import _read_yaml
            
            config_data, _ = _read_yaml(CONFIG_PATH)
            spotify = SpotifyClient(config_data)
            
            if spotify.sp:
                # Try different query formats
                for query in [f'album:"{album_name}" artist:"{artist_name}"', 
                              f'album:{album_name} artist:{artist_name}',
                              f'{artist_name} {album_name}']:
                    try:
                        logger.debug(f"Searching Spotify with query: {query}")
                        results = spotify.sp.search(q=query, type='album', limit=15)
                        
                        for album in results.get('albums', {}).get('items', []):
                            album_images = album.get('images', [])
                            if album_images:
                                # Get the largest image available
                                img_url = album_images[0]['url']
                                
                                # Verify the image URL is valid
                                try:
                                    img_resp = requests.head(img_url, timeout=3)
                                    if img_resp.status_code == 200:
                                        images.append({
                                            "url": img_url,
                                            "source": "Spotify",
                                            "title": album.get('name', ''),
                                            "artist": ", ".join([a.get('name', '') for a in album.get('artists', [])])
                                        })
                                except Exception as e:
                                    logger.debug(f"Image verification failed for Spotify: {e}")
                                    # Still add the image even if HEAD fails (some URLs don't support HEAD)
                                    images.append({
                                        "url": img_url,
                                        "source": "Spotify",
                                        "title": album.get('name', ''),
                                        "artist": ", ".join([a.get('name', '') for a in album.get('artists', [])])
                                    })
                        
                        if images:
                            logger.debug(f"Found {len(images)} images on Spotify")
                            break  # Stop if we found images
                    except Exception as e:
                        logger.debug(f"Spotify search with query '{query}' failed: {e}")
                        continue
            else:
                logger.warning("Spotify client not initialized")
        
        logger.info(f"Album art search for '{artist_name} - {album_name}' via {source}: {len(images)} images found")
        return jsonify({"images": images})
        
    except Exception as e:
        logger.error(f"Error searching album art: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({"error": str(e), "images": []}), 500


@app.route("/api/album/set-art", methods=["POST"])
def api_album_set_art():
    """Set custom album art"""
    data = request.json or {}
    artist_name = data.get("artist", "").strip()
    album_name = data.get("album", "").strip()
    image_url = data.get("image_url", "").strip()
    
    if not artist_name or not album_name or not image_url:
        return jsonify({"error": "Artist, album name, and image URL required"}), 400
    
    logger = logging.getLogger('sptnr')
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Create album_art table if it doesn't exist
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS album_art (
                artist_name TEXT NOT NULL,
                album_name TEXT NOT NULL,
                image_url TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (artist_name, album_name)
            )
        """)
        
        # Insert or update
        cursor.execute("""
            INSERT OR REPLACE INTO album_art (artist_name, album_name, image_url, updated_at)
            VALUES (?, ?, ?, ?)
        """, (artist_name, album_name, image_url, datetime.now().isoformat()))
        
        conn.commit()
        conn.close()
        
        logger.info(f"Album art updated for '{artist_name} - {album_name}': {image_url}")
        return jsonify({"success": True, "message": "Album art updated"})
        
    except Exception as e:
        logger.error(f"Error setting album art: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/artist/add", methods=["POST"])
def api_add_artist():
    """Manually add an artist and fetch all their releases from MusicBrainz."""
    data = request.json or {}
    artist_name = data.get("artist", "").strip()
    
    if not artist_name:
        return jsonify({"error": "Artist name is required"}), 400
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Check if artist already exists in tracks
        cursor.execute("SELECT COUNT(*) FROM tracks WHERE artist = ?", (artist_name,))
        existing_count = cursor.fetchone()[0]
        
        # Fetch all releases from MusicBrainz
        logging.info(f"[ADD_ARTIST] Fetching MusicBrainz releases for: {artist_name}")
        mb_releases = _fetch_musicbrainz_releases(artist_name, limit=200)
        
        if not mb_releases:
            conn.close()
            return jsonify({
                "error": f"No releases found on MusicBrainz for artist: {artist_name}",
                "artist": artist_name,
                "releases_found": 0
            }), 404
        
        # Get existing albums if artist exists
        existing_norm = set()
        if existing_count > 0:
            cursor.execute("SELECT DISTINCT album FROM tracks WHERE artist = ?", (artist_name,))
            existing_albums = [row[0] for row in cursor.fetchall()]
            existing_norm = {_normalize_release_title(a) for a in existing_albums if a}
        
        # Add all releases to missing_releases table
        added_count = 0
        for rg in mb_releases:
            # Check if already exists in library
            norm_title = _normalize_release_title(rg.get("title"))
            if norm_title and norm_title in existing_norm:
                continue
            
            # Skip compilations
            secondary = [s.lower() for s in rg.get("secondary_types") or []]
            if "compilation" in secondary:
                continue
            
            # Determine category
            primary_type = (rg.get("primary_type") or "").lower()
            category = "Album"
            if primary_type == "ep":
                category = "EP"
            elif primary_type == "single" or "single" in secondary:
                category = "Single"
            
            # Insert into missing_releases
            try:
                cursor.execute("""
                    INSERT OR REPLACE INTO missing_releases 
                    (artist, release_id, title, primary_type, first_release_date, cover_art_url, category, last_checked)
                    VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """, (
                    artist_name,
                    rg.get("id", ""),
                    rg.get("title", ""),
                    rg.get("primary_type", ""),
                    rg.get("first_release_date", ""),
                    rg.get("cover_art_url", ""),
                    category
                ))
                added_count += 1
            except Exception as e:
                logging.error(f"[ADD_ARTIST] Error inserting release {rg.get('title')}: {e}")
        
        conn.commit()
        
        # Create artist_stats entry if it doesn't exist (so artist appears on artists page)
        if existing_count == 0:
            cursor.execute("""
                INSERT OR IGNORE INTO artist_stats 
                (artist_name, last_updated)
                VALUES (?, CURRENT_TIMESTAMP)
            """, (artist_name,))
            conn.commit()
        
        conn.close()
        
        logging.info(f"[ADD_ARTIST] Added {added_count} missing releases for {artist_name}")
        
        return jsonify({
            "success": True,
            "artist": artist_name,
            "releases_found": len(mb_releases),
            "added_to_missing": added_count,
            "already_in_library": len(existing_norm),
            "artist_exists": existing_count > 0
        })
        
    except Exception as e:
        logging.error(f"[ADD_ARTIST] Error adding artist {artist_name}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/beets/update-album", methods=["POST"])
def api_beets_update_album():
    """
    Update an album with beets (write tags and organize files).
    
    Expects JSON:
    {
        "artist": "Artist Name",
        "album": "Album Name"
    }
    or
    {
        "folder": "/music/Artist Name/Album Name"
    }
    """
    try:
        from beets_update import update_album_with_beets, get_album_folder_for_artist_album
        
        data = request.json or {}
        album_folder = data.get("folder")
        
        # If no folder provided, try to construct from artist/album
        if not album_folder:
            artist = data.get("artist", "").strip()
            album = data.get("album", "").strip()
            
            if not artist or not album:
                return jsonify({"error": "Either 'folder' or both 'artist' and 'album' required"}), 400
            
            # Look up the album folder from the database
            album_folder = get_album_folder_for_artist_album(artist, album)
            
            if not album_folder:
                return jsonify({"error": f"Album folder not found for {artist} - {album}"}), 404
        
        logging.info(f"Starting beets update for album: {album_folder}")
        result = update_album_with_beets(album_folder)
        
        if result['success']:
            # Trigger Navidrome rescan after successful beets update
            try:
                # This will trigger a background task to rescan Navidrome
                # We'll implement this in the next step
                logging.info(f"Beets update successful for {album_folder}, triggering Navidrome rescan")
            except Exception as e:
                logging.warning(f"Could not trigger Navidrome rescan: {e}")
            
            return jsonify({
                "success": True,
                "message": result['message'],
                "folder": album_folder,
                "output": result.get('output', '')
            })
        else:
            return jsonify({
                "success": False,
                "error": result['error'],
                "folder": album_folder
            }), 500
    
    except Exception as e:
        logging.error(f"Error updating album with beets: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/beets/album-folders/<path:artist>", methods=["GET"])
def api_beets_album_folders(artist):
    """Get all album folders for an artist."""
    try:
        from urllib.parse import unquote
        from beets_update import get_all_album_folders_for_artist
        
        artist = unquote(artist)
        folders = get_all_album_folders_for_artist(artist)
        
        return jsonify({
            "success": True,
            "artist": artist,
            "album_folders": folders,
            "count": len(folders)
        })
    
    except Exception as e:
        logging.error(f"Error getting album folders for {artist}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/album/<path:artist>/<path:album>")
def album_detail(artist, album):
    """View album details and tracks"""
    try:
        # URL decode the artist and album names
        from urllib.parse import unquote
        artist = unquote(artist)
        album = unquote(album)
        
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT *
            FROM tracks
            WHERE artist = ? AND album = ?
            ORDER BY COALESCE(disc_number, 1), COALESCE(track_number, 999), title COLLATE NOCASE
        """, (artist, album))
        tracks_data = cursor.fetchall()
        
        if not tracks_data:
            return render_template("album.html",
                                 artist_name=artist,
                                 album_name=album,
                                 tracks=[],
                                 tracks_by_disc={},
                                 album_data=None,
                                 album_genres=[],
                                 error="Album not found")
        
        # Get album metadata from first track
        try:
            cursor.execute("""
                SELECT 
                    COUNT(*) as track_count,
                    AVG(stars) as avg_stars,
                    SUM(COALESCE(duration, 0)) as total_duration,
                    MAX(spotify_release_date) as spotify_release_date,
                    MAX(spotify_album_type) as spotify_album_type,
                    MAX(spotify_album_art_url) as spotify_album_art_url,
                    MAX(last_scanned) as last_scanned,
                    MAX(COALESCE(disc_number, 1)) as total_discs,
                    MAX(beets_album_mbid) as beets_album_mbid,
                    MAX(discogs_album_id) as discogs_album_id
                FROM tracks
                WHERE artist = ? AND album = ?
            """, (artist, album))
        except:
            # Fallback for databases without beets columns
            cursor.execute("""
                SELECT 
                    COUNT(*) as track_count,
                    AVG(stars) as avg_stars,
                    SUM(COALESCE(duration, 0)) as total_duration,
                    MAX(spotify_release_date) as spotify_release_date,
                    MAX(spotify_album_type) as spotify_album_type,
                    MAX(spotify_album_art_url) as spotify_album_art_url,
                    MAX(last_scanned) as last_scanned,
                    MAX(COALESCE(disc_number, 1)) as total_discs,
                    NULL as beets_album_mbid,
                    NULL as discogs_album_id
                FROM tracks
                WHERE artist = ? AND album = ?
            """, (artist, album))
        album_data = cursor.fetchone()
        
        # Convert to dict if it's a Row object
        if album_data:
            album_data = dict(album_data)
        else:
            album_data = {
                'track_count': 0,
                'avg_stars': 0,
                'total_duration': 0,
                'spotify_release_date': None,
                'spotify_album_type': None,
                'spotify_album_art_url': None,
                'last_scanned': None,
                'total_discs': 1
            }
        
        # Aggregate genres from tracks in this album
        cursor.execute("""
            SELECT DISTINCT genres FROM tracks
            WHERE artist = ? AND album = ? AND genres IS NOT NULL AND genres != ''
        """, (artist, album))
        genre_rows = cursor.fetchall()
        album_genres = set()
        for row in genre_rows:
            try:
                genre_value = row['genres'] if isinstance(row, dict) else row[0]
                if genre_value:
                    genres = [g.strip() for g in genre_value.split(',') if g.strip()]
                    album_genres.update(genres)
            except (KeyError, IndexError, TypeError) as e:
                logging.debug(f"Error parsing genre row: {e}")
                continue
        
        # Calculate genre fit for each track
        tracks_with_genre_fit = []
        for track in tracks_data:
            try:
                # Convert Row to dict if needed
                if hasattr(track, 'keys'):
                    track_dict = dict(track)
                else:
                    # Already a dict or tuple, try to convert
                    track_dict = track if isinstance(track, dict) else dict(track)
                
                # Parse track's genres
                track_genres = set()
                if track_dict.get('genres'):
                    track_genres.update([g.strip() for g in track_dict['genres'].split(',') if g.strip()])
                
                # Calculate how many album genres this track contains
                genre_matches = len(track_genres & album_genres) if album_genres else 0
                genre_fit_percent = int((genre_matches / len(album_genres) * 100) if album_genres else 0)
                
                track_dict['genre_matches'] = genre_matches
                track_dict['genre_fit_percent'] = genre_fit_percent
                track_dict['matching_genres'] = sorted(list(track_genres & album_genres))
                
                tracks_with_genre_fit.append(track_dict)
            except Exception as e:
                logging.debug(f"Error calculating genre fit: {e}")
                track_dict = dict(track) if hasattr(track, 'keys') else track
                if not isinstance(track_dict, dict):
                    track_dict = {'title': str(track)}
                track_dict['genre_matches'] = 0
                track_dict['genre_fit_percent'] = 0
                track_dict['matching_genres'] = []
                tracks_with_genre_fit.append(track_dict)
        
        # Group tracks by disc number
        tracks_by_disc = {}
        for track_dict in tracks_with_genre_fit:
            try:
                disc_num = track_dict.get('disc_number') if isinstance(track_dict, dict) else (track_dict['disc_number'] if hasattr(track_dict, '__getitem__') else 1)
                disc_num = disc_num or 1
                
                if disc_num not in tracks_by_disc:
                    tracks_by_disc[disc_num] = []
                tracks_by_disc[disc_num].append(track_dict)
            except Exception as e:
                logging.debug(f"Error processing track for disc grouping: {e}")
                # Fallback to disc 1
                if 1 not in tracks_by_disc:
                    tracks_by_disc[1] = []
                tracks_by_disc[1].append(track_dict)
        
        conn.close()
        
        # Get qBittorrent and slskd config
        cfg, _ = _read_yaml(CONFIG_PATH)
        qbit_config = cfg.get("qbittorrent", {"enabled": False, "web_url": "http://localhost:8080"})
        slskd_config = cfg.get("slskd", {"enabled": False})
        
        return render_template("album.html",
                             artist_name=artist,
                             album_name=album,
                             tracks=tracks_with_genre_fit,
                             tracks_by_disc=tracks_by_disc,
                             album_data=album_data,
                             album_genres=sorted(list(album_genres)),
                             qbit_config=qbit_config,
                             slskd_config=slskd_config)
    except Exception as e:
        import traceback
        logging.error(f"Error loading album {artist}/{album}: {e}")
        logging.error(traceback.format_exc())
        
        # Get config even for error page
        try:
            cfg, _ = _read_yaml(CONFIG_PATH)
            qbit_config = cfg.get("qbittorrent", {"enabled": False, "web_url": "http://localhost:8080"})
            slskd_config = cfg.get("slskd", {"enabled": False})
        except:
            qbit_config = {"enabled": False, "web_url": "http://localhost:8080"}
            slskd_config = {"enabled": False}
        
        return render_template("album.html",
                             artist_name=artist,
                             album_name=album,
                             tracks=[],
                             tracks_by_disc={},
                             album_data=None,
                             album_genres=[],
                             qbit_config=qbit_config,
                             slskd_config=slskd_config,
                             error=f"Error loading album: {str(e)}")


@app.route("/album/<path:artist>/<path:album>/rescan", methods=["POST"])
def album_rescan(artist, album):
    """Trigger per-artist pipeline: Navidrome fetch -> popularity -> single detection."""
    from urllib.parse import unquote
    artist = unquote(artist)
    album = unquote(album)

    def _worker(artist_name: str):
        try:
            # Look up artist_id from cache; rebuild index if missing
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT artist_id FROM artist_stats WHERE artist_name = ?", (artist_name,))
            row = cursor.fetchone()
            conn.close()
            artist_id = row[0] if row else None

            if not artist_id:
                idx = build_artist_index()
                artist_id = (idx.get(artist_name, {}) or {}).get("id")

            if not artist_id:
                logging.error(f"Rescan aborted: no artist_id for {artist_name}")
                return

            # Step 1: refresh Navidrome cache for this artist
            scan_artist_to_db(artist_name, artist_id, verbose=True, force=True)

            # Step 2: popularity (per-artist)
            scan_popularity(verbose=True, artist=artist_name)

            # Step 3: single detection & scoring
            rate_artist(artist_id, artist_name, verbose=True, force=True)
        except Exception as e:
            logging.error(f"Album rescan failed for {artist_name}: {e}")

    threading.Thread(target=_worker, args=(artist,), daemon=True).start()
    flash(f"Rescan started for {artist}", "info")
    return redirect(url_for("album_detail", artist=artist, album=album))


@app.route("/track/<path:artist>/<path:album>/<path:track_id>/rescan", methods=["POST"])
def scan_track_rescan(artist, album, track_id):
    """Trigger per-track rescan: Navidrome fetch -> popularity -> single detection."""
    from urllib.parse import unquote
    artist = unquote(artist)
    album = unquote(album)
    track_id = unquote(track_id)

    def _worker(artist_name: str, track_identifier: str):
        try:
            # Look up artist_id
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT artist_id FROM artist_stats WHERE artist_name = ?", (artist_name,))
            row = cursor.fetchone()
            conn.close()
            artist_id = row[0] if row else None

            if not artist_id:
                idx = build_artist_index()
                artist_id = (idx.get(artist_name, {}) or {}).get("id")

            if not artist_id:
                logging.error(f"Track rescan aborted: no artist_id for {artist_name}")
                return

            # Step 1: refresh Navidrome cache for this artist
            scan_artist_to_db(artist_name, artist_id, verbose=True, force=True)

            # Step 2: popularity (per-artist, which includes the track)
            scan_popularity(verbose=True, artist=artist_name)

            # Step 3: single detection & scoring
            rate_artist(artist_id, artist_name, verbose=True, force=True)
            
            logging.info(f"Track rescan completed for {track_identifier}")
        except Exception as e:
            logging.error(f"Track rescan failed for {track_identifier}: {e}")

    threading.Thread(target=_worker, args=(artist, track_id), daemon=True).start()
    flash(f"Track rescan started for {artist}", "info")
    return redirect(url_for("track_detail", track_id=track_id))


@app.route("/track/<track_id>")
def track_detail(track_id):
    """View and edit track details"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM tracks WHERE id = ?", (track_id,))
        track = cursor.fetchone()
        
        if not track:
            conn.close()
            flash("Track not found", "error")
            return redirect(url_for("dashboard"))
        
        # Convert Row to dict to ensure all columns are accessible
        track = dict(track)
        
        # Ensure beets columns exist (for backward compatibility)
        beets_columns = ['beets_mbid', 'beets_similarity', 'beets_album_mbid', 
                        'beets_artist_mbid', 'beets_album_artist', 'beets_year',
                        'beets_import_date', 'beets_path', 'album_folder']
        for col in beets_columns:
            if col not in track:
                track[col] = None
        
        # Get recommended genres from other tracks with similar titles or artists
        recommended_genres = []
        artist_name = track.get('artist', '')
        if artist_name:
            cursor.execute("""
                SELECT genres FROM tracks 
                WHERE artist = ? AND genres IS NOT NULL AND genres != ''
                LIMIT 10
            """, (artist_name,))
            genre_rows = cursor.fetchall()
            genre_set = set()
            for row in genre_rows:
                if row['genres']:
                    # Parse comma-separated genres
                    genres = [g.strip() for g in row['genres'].split(',') if g.strip()]
                    genre_set.update(genres)
            recommended_genres = sorted(list(genre_set))
        
        conn.close()
        
        # Load config for template
        try:
            cfg, _ = _read_yaml(CONFIG_PATH)
            qbit_config = cfg.get("qbittorrent", {"enabled": False, "web_url": "http://localhost:8080"})
            slskd_config = cfg.get("slskd", {"enabled": False})
        except Exception as e:
            logging.warning(f"Could not load config for track template: {e}")
            qbit_config = {"enabled": False, "web_url": "http://localhost:8080"}
            slskd_config = {"enabled": False}
        
        return render_template("track.html", track=track, recommended_genres=recommended_genres, track_id=track_id,
                             qbit_config=qbit_config, slskd_config=slskd_config)
    
    except Exception as e:
        logging.error(f"Error loading track {track_id}: {e}")
        import traceback
        logging.error(traceback.format_exc())
        flash(f"Error loading track: {str(e)}", "error")
        return redirect(url_for("dashboard"))


@app.route("/track/<track_id>/edit", methods=["POST"])
def track_edit(track_id):
    """Update track metadata"""
    conn = get_db()
    cursor = conn.cursor()
    
    # Get form data
    title = request.form.get("title")
    artist = request.form.get("artist")
    album = request.form.get("album")
    stars = request.form.get("stars", type=int)
    is_single = 1 if request.form.get("is_single") == "on" else 0
    single_confidence = request.form.get("single_confidence", "low")
    mbid = request.form.get("mbid", "").strip() or None
    suggested_mbid = request.form.get("suggested_mbid", "").strip() or None
    suggested_mbid_confidence = request.form.get("suggested_mbid_confidence", type=float)
    
    # Update database
    cursor.execute("""
        UPDATE tracks
        SET title = ?, artist = ?, album = ?, stars = ?, is_single = ?, single_confidence = ?,
            mbid = ?, suggested_mbid = ?, suggested_mbid_confidence = ?
        WHERE id = ?
    """, (title, artist, album, stars, is_single, single_confidence, mbid, suggested_mbid, suggested_mbid_confidence, track_id))
    
    conn.commit()
    conn.close()
    
    flash(f"Track '{title}' updated successfully", "success")
    return redirect(url_for("track_detail", track_id=track_id))


@app.route("/scan/start", methods=["POST"])
def scan_start():
    """Start a library scan"""
    global scan_process
    
    scan_type = request.form.get("scan_type", "batchrate")
    
    with scan_lock:
        if scan_process and scan_process.poll() is None:
            flash("A scan is already running", "warning")
            return redirect(url_for("dashboard"))
        
        # Build command
        cmd = ["python", "/app/start.py"]
        
        if scan_type == "batchrate":
            cmd.append("--batchrate")
        elif scan_type == "force":
            cmd.extend(["--batchrate", "--force"])
        elif scan_type == "artist":
            artist = request.form.get("artist")
            if artist:
                cmd.extend(["--artist", artist, "--sync"])
        
        # Start process
        scan_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        
        flash(f"Scan started: {scan_type}", "success")
    
    return redirect(url_for("dashboard"))


@app.route("/scan/unified", methods=["POST"])
def scan_unified():
    """Start the unified scan pipeline (popularity + singles)"""
    global scan_process
    
    with scan_lock:
        if scan_process and scan_process.poll() is None:
            flash("A scan is already running", "warning")
            return redirect(url_for("dashboard"))
        
        try:
            # Start unified scan process
            cmd = [sys.executable, "unified_scan.py", "--verbose"]
            scan_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            flash("✅ Unified scan started (popularity → singles → ratings)", "success")
        except Exception as e:
            flash(f"❌ Error starting unified scan: {str(e)}", "danger")
    
    return redirect(url_for("dashboard"))


@app.route("/scan/mp3", methods=["POST"])
def scan_mp3():
    """Run beets auto-import to scan music folder and capture metadata"""
    global scan_process_mp3
    
    with scan_lock:
        # Check if scan is already running
        if scan_process_mp3 is not None:
            if isinstance(scan_process_mp3, dict):
                thread = scan_process_mp3.get('thread')
                if thread and thread.is_alive():
                    flash("Beets auto-import is already running. Please wait for it to complete.", "warning")
                    logging.warning("Attempted to start beets import while one is already running")
                    return redirect(url_for("dashboard"))
                else:
                    # Clean up dead thread reference
                    scan_process_mp3 = None
            elif hasattr(scan_process_mp3, 'is_alive') and scan_process_mp3.is_alive():
                flash("Beets auto-import is already running. Please wait for it to complete.", "warning")
                logging.warning("Attempted to start beets import while one is already running")
                return redirect(url_for("dashboard"))
            else:
                # Clean up dead thread reference
                scan_process_mp3 = None
        
        try:
            db_dir = os.path.dirname(DB_PATH)
            mp3_progress_file = os.path.join(db_dir, "mp3_scan_progress.json")
            _write_progress_file(mp3_progress_file, "mp3_scan", True, {"status": "starting"})
            
            # Run beets import in background thread instead of subprocess
            def run_beets_scan_bg():
                global scan_process_mp3
                try:
                    from beets_auto_import import BeetsAutoImporter
                    logging.info("Starting Beets auto-import scan in background")
                    importer = BeetsAutoImporter()
                    importer.import_and_capture(skip_existing=True)
                    _write_progress_file(mp3_progress_file, "mp3_scan", False, {"status": "complete", "exit_code": 0})
                    logging.info("Beets scan completed successfully")
                except Exception as e:
                    logging.error(f"Error in Beets scan: {e}", exc_info=True)
                    _write_progress_file(mp3_progress_file, "mp3_scan", False, {"status": "error", "error": str(e), "exit_code": 1})
                finally:
                    # Clean up thread reference when done
                    with scan_lock:
                        scan_process_mp3 = None
                    logging.info("Beets scan thread cleanup complete")
            
            scan_thread = threading.Thread(target=run_beets_scan_bg, daemon=False)
            scan_thread.start()
            
            # Store thread reference for tracking
            scan_process_mp3 = {'thread': scan_thread, 'type': 'mp3'}
            
            flash("✅ Beets auto-import started (capturing file paths & MusicBrainz metadata)", "success")
            logging.info("Beets scan thread started successfully")
        except Exception as e:
            logging.error(f"Error starting beets import: {e}", exc_info=True)
            flash(f"❌ Error starting beets import: {str(e)}", "danger")
    
    return redirect(url_for("dashboard"))


@app.route("/scan/popularity", methods=["POST"])
def scan_popularity():
    """Run popularity score update from external sources"""
    global scan_process_popularity
    
    with scan_lock:
        # Check if scan is already running
        if scan_process_popularity is not None:
            if isinstance(scan_process_popularity, dict):
                thread = scan_process_popularity.get('thread')
                if thread and thread.is_alive():
                    flash("Popularity scan is already running", "warning")
                    return redirect(url_for("dashboard"))
            elif hasattr(scan_process_popularity, 'is_alive') and scan_process_popularity.is_alive():
                flash("Popularity scan is already running", "warning")
                return redirect(url_for("dashboard"))

        # Don't start popularity until Navidrome scan finishes
        nav_running = False
        if scan_process_navidrome is not None:
            if isinstance(scan_process_navidrome, dict):
                nav_thread = scan_process_navidrome.get('thread')
                nav_running = nav_thread is not None and nav_thread.is_alive()
            elif hasattr(scan_process_navidrome, 'is_alive'):
                nav_running = scan_process_navidrome.is_alive()
            elif hasattr(scan_process_navidrome, 'poll'):
                nav_running = scan_process_navidrome.poll() is None

        if not nav_running:
            nav_progress_file = os.path.join(os.path.dirname(DB_PATH), "navidrome_scan_progress.json")
            try:
                with open(nav_progress_file, "r", encoding="utf-8") as f:
                    nav_state = json.load(f)
                    nav_running = bool(nav_state.get("is_running"))
            except FileNotFoundError:
                nav_running = False
            except Exception:
                nav_running = False

        if nav_running:
            flash("Please wait for Navidrome scan to finish before starting popularity scan", "warning")
            return redirect(url_for("dashboard"))
        
        try:
            db_dir = os.path.dirname(DB_PATH)
            popularity_progress_file = os.path.join(db_dir, "popularity_scan_progress.json")
            _write_progress_file(popularity_progress_file, "popularity_scan", True, {"status": "starting"})

            # Run popularity scan in background thread instead of subprocess
            def run_popularity_scan_bg():
                try:
                    logging.info("Starting popularity score scan in background")
                    scan_popularity(verbose=False)
                    _write_progress_file(popularity_progress_file, "popularity_scan", False, {"status": "complete", "exit_code": 0})
                    logging.info("Popularity scan completed successfully")
                except Exception as e:
                    logging.error(f"Error in popularity scan: {e}", exc_info=True)
                    _write_progress_file(popularity_progress_file, "popularity_scan", False, {"status": "error", "error": str(e), "exit_code": 1})
            
            scan_thread = threading.Thread(target=run_popularity_scan_bg, daemon=False)
            scan_thread.start()
            
            # Store thread reference for tracking
            scan_process_popularity = {'thread': scan_thread, 'type': 'popularity'}
            
            flash("✅ Popularity score scan started", "success")
            logging.info("Popularity scan thread started successfully")
        except Exception as e:
            logging.error(f"Error starting popularity scan: {e}", exc_info=True)
            flash(f"❌ Error starting popularity scan: {str(e)}", "danger")
    
    return redirect(url_for("dashboard"))


@app.route("/scan/singles", methods=["POST"])
def scan_singles():
    """Run single detection"""
    global scan_process_singles
    
    with scan_lock:
        # Block singles until Navidrome sync finishes
        nav_running = False
        if scan_process_navidrome is not None:
            if isinstance(scan_process_navidrome, dict):
                nav_thread = scan_process_navidrome.get('thread')
                nav_running = nav_thread is not None and nav_thread.is_alive()
            elif hasattr(scan_process_navidrome, 'is_alive'):
                nav_running = scan_process_navidrome.is_alive()
            elif hasattr(scan_process_navidrome, 'poll'):
                nav_running = scan_process_navidrome.poll() is None

        if not nav_running:
            nav_progress_file = os.path.join(os.path.dirname(DB_PATH), "navidrome_scan_progress.json")
            try:
                with open(nav_progress_file, "r", encoding="utf-8") as f:
                    nav_state = json.load(f)
                    nav_running = bool(nav_state.get("is_running"))
            except FileNotFoundError:
                nav_running = False
            except Exception:
                nav_running = False

        if nav_running:
            flash("Please wait for Navidrome scan to finish before starting singles detection", "warning")
            return redirect(url_for("dashboard"))

        if scan_process_singles and scan_process_singles.poll() is None:
            flash("Single detection scan is already running", "warning")
            return redirect(url_for("dashboard"))
        
        try:
            cmd = [sys.executable, "singledetection.py", "--verbose"]
            scan_process_singles = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            flash("✅ Single detection scan started", "success")
        except Exception as e:
            flash(f"❌ Error starting single detection: {str(e)}", "danger")
    
    return redirect(url_for("dashboard"))


@app.route("/scan/stop", methods=["POST"])
def scan_stop():
    """Stop the running scan (main scan process)"""
    global scan_process
    
    with scan_lock:
        if scan_process and scan_process.poll() is None:
            scan_process.terminate()
            scan_process.wait(timeout=10)
            flash("Main scan stopped", "info")
        else:
            flash("No main scan is currently running", "warning")
    
    return redirect(url_for("dashboard"))


@app.route("/scan/stop-mp3", methods=["POST"])
def scan_stop_mp3():
    """Stop the MP3 file scan"""
    global scan_process_mp3
    
    with scan_lock:
        if scan_process_mp3 and scan_process_mp3.poll() is None:
            scan_process_mp3.terminate()
            scan_process_mp3.wait(timeout=10)
            flash("File path scan stopped", "info")
        else:
            flash("No file path scan is currently running", "warning")
    
    return redirect(url_for("dashboard"))


@app.route("/scan/stop-navidrome", methods=["POST"])
def scan_stop_navidrome():
    """Stop the Navidrome sync scan"""
    global scan_process_navidrome
    
    with scan_lock:
        if scan_process_navidrome and scan_process_navidrome.poll() is None:
            scan_process_navidrome.terminate()
            scan_process_navidrome.wait(timeout=10)
            flash("Navidrome sync scan stopped", "info")
        else:
            flash("No Navidrome sync scan is currently running", "warning")
    
    return redirect(url_for("dashboard"))


@app.route("/scan/stop-popularity", methods=["POST"])
def scan_stop_popularity():
    """Stop the popularity scan"""
    global scan_process_popularity
    
    with scan_lock:
        if scan_process_popularity and scan_process_popularity.poll() is None:
            scan_process_popularity.terminate()
            scan_process_popularity.wait(timeout=10)
            flash("Popularity scan stopped", "info")
        else:
            flash("No popularity scan is currently running", "warning")
    
    return redirect(url_for("dashboard"))


@app.route("/scan/stop-singles", methods=["POST"])
def scan_stop_singles():
    """Stop the single detection scan"""
    global scan_process_singles
    
    with scan_lock:
        if scan_process_singles and scan_process_singles.poll() is None:
            scan_process_singles.terminate()
            scan_process_singles.wait(timeout=10)
            flash("Single detection scan stopped", "info")
        else:
            flash("No single detection scan is currently running", "warning")
    
    return redirect(url_for("dashboard"))


@app.route("/scan/status")
def scan_status():
    """Get scan status (JSON)"""
    with scan_lock:
        web_ui_running = scan_process is not None and scan_process.poll() is None
    
    # Check if background scan from start.py is running
    lock_file_path = os.path.join(os.path.dirname(CONFIG_PATH), ".scan_lock")
    background_running = os.path.exists(lock_file_path)
    
    running = web_ui_running or background_running
    
    return jsonify({"running": running})


@app.route("/login", methods=["GET", "POST"])
def login():
    """Login page"""
    try:
        # If already logged in, redirect to dashboard
        if 'username' in session:
            return redirect(url_for('dashboard'))
        
        # If config doesn't exist, redirect to setup
        if not os.path.exists(CONFIG_PATH):
            return redirect(url_for('setup'))
        
        cfg, _ = _read_yaml(CONFIG_PATH)
        if _needs_setup(cfg):
            return redirect(url_for('setup'))
        
        if request.method == 'POST':
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '').strip()
            
            if _authenticate_navidrome(username, password):
                session.permanent = True
                session['username'] = username
                flash(f'Welcome back, {username}!', 'success')
                
                # Redirect to next URL or dashboard
                next_url = request.args.get('next')
                if next_url:
                    return redirect(next_url)
                return redirect(url_for('dashboard'))
            else:
                flash('Invalid username or password. Please use your Navidrome credentials.', 'danger')
    except Exception as e:
        logging.error(f"Login error: {e}")
        import traceback
        traceback.print_exc()
        flash(f"Login system error: {str(e)}", "danger")
    
    return render_template('login.html')


@app.route("/logout")
def logout():
    """Logout and clear session"""
    username = session.get('username', 'User')
    session.clear()
    flash(f'Goodbye, {username}!', 'info')
    return redirect(url_for('login'))


@app.route("/logs")
def logs():
    """View logs"""
    config_dir = os.path.dirname(CONFIG_PATH)
    log_files = {
        "main": LOG_PATH,
        "webui": os.path.join(config_dir, "webui.log"),
        "beets": os.path.join(config_dir, "beets_import.log"),
        "navidrome": os.path.join(config_dir, "app.log"),
        "popularity": os.path.join(config_dir, "popularity.log"),
        "downloads": os.path.join(config_dir, "downloads.log"),
    }
    return render_template("logs.html", log_path=LOG_PATH, log_files=log_files)


@app.route("/logs/stream")
def logs_stream():
    """Stream log file in real-time"""
    log_type = request.args.get("type", "main")
    config_dir = os.path.dirname(CONFIG_PATH)
    log_files = {
        "main": LOG_PATH,
        "webui": os.path.join(config_dir, "webui.log"),
        "beets": os.path.join(config_dir, "beets_import.log"),
        "navidrome": os.path.join(config_dir, "app.log"),
        "popularity": os.path.join(config_dir, "popularity.log"),
        "downloads": os.path.join(config_dir, "downloads.log"),
    }
    log_path = log_files.get(log_type, LOG_PATH)
    
    def generate():
        try:
            with open(log_path, "r") as f:
                # Seek to end
                f.seek(0, 2)
                while True:
                    line = f.readline()
                    if line:
                        yield f"data: {line}\n\n"
                    else:
                        time.sleep(0.5)
        except FileNotFoundError:
            yield f"data: Log file not found: {log_path}\n\n"
    
    return Response(generate(), mimetype="text/event-stream")


@app.route("/logs/view")
def logs_view():
    """View last N lines of log"""
    log_type = request.args.get("type", "main")
    lines = request.args.get("lines", 500, type=int)
    config_dir = os.path.dirname(CONFIG_PATH)
    log_files = {
        "main": LOG_PATH,
        "webui": os.path.join(config_dir, "webui.log"),
        "beets": os.path.join(config_dir, "beets_import.log"),
        "navidrome": os.path.join(config_dir, "app.log"),
        "popularity": os.path.join(config_dir, "popularity.log"),
        "downloads": os.path.join(config_dir, "downloads.log"),
    }
    log_path = log_files.get(log_type, LOG_PATH)
    try:
        with open(log_path, "r") as f:
            all_lines = f.readlines()
            recent_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
            return jsonify({"lines": recent_lines})
    except FileNotFoundError:
        return jsonify({"error": "Log file not found", "lines": []})


@app.route("/bookmarks")
def bookmarks():
    """View all bookmarks (favourites)"""
    try:
        filter_type = request.args.get('filter', None)
        
        conn = get_db()
        cursor = conn.cursor()
        
        if filter_type:
            cursor.execute("""
                SELECT id, type, name, artist, album, track_id, created_at
                FROM bookmarks
                WHERE type = ?
                ORDER BY created_at DESC
            """, (filter_type,))
        else:
            cursor.execute("""
                SELECT id, type, name, artist, album, track_id, created_at
                FROM bookmarks
                ORDER BY created_at DESC
            """)
        
        bookmarks_data = []
        for row in cursor.fetchall():
            bookmarks_data.append({
                'id': row[0],
                'type': row[1],
                'name': row[2],
                'artist': row[3],
                'album': row[4],
                'track_id': row[5],
                'created_at': row[6]
            })
        
        conn.close()
        
        return render_template("bookmarks.html", 
                             bookmarks=bookmarks_data,
                             filter_type=filter_type)
    except Exception as e:
        logging.error(f"Error loading bookmarks: {e}")
        return render_template("bookmarks.html", 
                             bookmarks=[], 
                             filter_type=None,
                             error=str(e))


@app.route("/api/bookmarks", methods=["GET", "POST"])
def api_bookmarks():
    """Get all bookmarks or add a new bookmark"""
    conn = get_db()
    cursor = conn.cursor()
    
    if request.method == "GET":
        try:
            cursor.execute("""
                SELECT id, type, name, artist, album, track_id, created_at
                FROM bookmarks
                ORDER BY created_at DESC
            """)
            
            bookmarks_data = []
            for row in cursor.fetchall():
                bookmarks_data.append({
                    'id': row[0],
                    'type': row[1],
                    'name': row[2],
                    'artist': row[3],
                    'album': row[4],
                    'track_id': row[5],
                    'created_at': row[6]
                })
            
            conn.close()
            return jsonify({"success": True, "bookmarks": bookmarks_data})
        except Exception as e:
            logging.error(f"Error fetching bookmarks: {e}")
            conn.close()
            return jsonify({"success": False, "error": str(e)}), 500
    
    elif request.method == "POST":
        try:
            data = request.get_json()
            bookmark_type = data.get('type')
            name = data.get('name')
            artist = data.get('artist')
            album = data.get('album')
            track_id = data.get('track_id')
            
            if not bookmark_type or not name:
                return jsonify({"success": False, "error": "Missing required fields"}), 400
            
            cursor.execute("""
                INSERT OR IGNORE INTO bookmarks (type, name, artist, album, track_id)
                VALUES (?, ?, ?, ?, ?)
            """, (bookmark_type, name, artist, album, track_id))
            
            conn.commit()
            bookmark_id = cursor.lastrowid
            conn.close()
            
            return jsonify({"success": True, "id": bookmark_id, "message": "Bookmark added"})
        except Exception as e:
            logging.error(f"Error adding bookmark: {e}")
            conn.close()
            return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/bookmarks/<int:bookmark_id>", methods=["DELETE"])
def api_delete_bookmark(bookmark_id):
    """Delete a bookmark"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("DELETE FROM bookmarks WHERE id = ?", (bookmark_id,))
        conn.commit()
        
        if cursor.rowcount == 0:
            conn.close()
            return jsonify({"success": False, "error": "Bookmark not found"}), 404
        
        conn.close()
        return jsonify({"success": True, "message": "Bookmark deleted"})
    except Exception as e:
        logging.error(f"Error deleting bookmark: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/config")
def config_editor():
    """View/edit config.yaml"""
    config, raw = _read_yaml(CONFIG_PATH)
    if not raw:
        raw = yaml.safe_dump(config, sort_keys=False, allow_unicode=False) if config else ""
    
    return render_template("config.html", config=config, config_raw=raw, CONFIG_PATH=CONFIG_PATH)



@app.route("/config/env", methods=["GET"])
def config_env_vars():
    """Return all relevant environment variables and their current values as JSON."""
    return jsonify(get_all_env_vars())

@app.route("/config/env", methods=["POST"])
def config_env_vars_post():
    """Update environment variables from config page."""
    try:
        data = request.get_json(force=True)
        changed = 0
        for var, value in data.items():
            if var in ALL_ENV_VARS:
                os.environ[var] = value
                changed += 1
        return jsonify({"success": True, "updated": changed})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400
    return redirect(url_for("config_editor"))


@app.route("/config/save-json", methods=["POST"])
def config_save_json():
    """Save config as JSON - converts to YAML and updates config.yaml"""
    try:
        # Get JSON data
        data = request.get_json()
        
        if data is None:
            return jsonify({"success": False, "error": "No JSON data provided"}), 400
        
        # Build YAML structure from JSON
        config_dict = {
            'navidrome_users': data.get('navidrome_users', []),
            'qbittorrent': data.get('qbittorrent', {}),
            'slskd': data.get('slskd', {}),
            'authentik': data.get('authentik', {}),
            'bookmarks': data.get('bookmarks', {}),
            'downloads': data.get('downloads', {}),
            'api_integrations': data.get('api_integrations', {}),
            'database': data.get('database', {}),
            'logging': data.get('logging', {}),
            'features': {},  # Preserve features section if it exists
            'weights': {}  # Preserve weights section if it exists
        }
        
        # Read existing config to preserve features and weights
        existing_config, _ = _read_yaml(CONFIG_PATH)
        if existing_config:
            if 'features' in existing_config:
                config_dict['features'] = existing_config['features']
            if 'weights' in existing_config:
                config_dict['weights'] = existing_config['weights']
            # Also preserve legacy navidrome config if it exists (for backward compatibility)
            if 'navidrome' in existing_config and not config_dict.get('navidrome_users'):
                config_dict['navidrome'] = existing_config['navidrome']
        
        # Convert to YAML
        yaml_content = yaml.safe_dump(config_dict, sort_keys=False, allow_unicode=True, default_flow_style=False)
        
        # Validate YAML before writing
        yaml.safe_load(yaml_content)
        
        # Write to file
        cfg_dir = os.path.dirname(CONFIG_PATH)
        if cfg_dir:
            os.makedirs(cfg_dir, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(yaml_content)
        
        return jsonify({"success": True, "message": "Configuration saved successfully"})
    
    except yaml.YAMLError as e:
        return jsonify({"success": False, "error": f"YAML error: {str(e)}"}), 400
    except IOError as e:
        return jsonify({"success": False, "error": f"File write error: {str(e)}"}), 400
    except Exception as e:
        import traceback
        error_msg = str(e)
        tb = traceback.format_exc()
        print(f"Config save error: {tb}")  # Log to console for debugging
        return jsonify({"success": False, "error": error_msg}), 400


@app.route("/api/stats")
def api_stats():
    """API endpoint for statistics"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(DISTINCT artist) FROM tracks")
    artist_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(DISTINCT album) FROM tracks")
    album_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM tracks")
    track_count = cursor.fetchone()[0]
    
    conn.close()
    
    return jsonify({
        "artists": artist_count,
        "albums": album_count,
        "tracks": track_count
    })


@app.route("/api/scan-status")
def api_scan_status():
    """API endpoint to get status of all scan types"""
    global scan_process, scan_process_mp3, scan_process_navidrome, scan_process_popularity, scan_process_singles, scan_process_missing_releases
    
    def is_process_running(proc):
        """Check if a process/thread is running, handling both dict and process objects."""
        if proc is None:
            return False
        if isinstance(proc, dict):
            # Handle dict format: {'thread': thread_obj, 'type': '...'}
            thread = proc.get('thread')
            if thread is None:
                return False
            # For threads, check is_alive()
            if hasattr(thread, 'is_alive'):
                return thread.is_alive()
            # For processes, check poll()
            if hasattr(thread, 'poll'):
                return thread.poll() is None
            return False
        # Direct process/thread object
        if hasattr(proc, 'is_alive'):
            return proc.is_alive()
        if hasattr(proc, 'poll'):
            return proc.poll() is None
        return False
    
    with scan_lock:
        return jsonify({
            "main_scan": {
                "name": "Main Rating Scan",
                "running": is_process_running(scan_process)
            },
            "mp3_scan": {
                "name": "File Path Scan",
                "running": is_process_running(scan_process_mp3)
            },
            "navidrome_scan": {
                "name": "Navidrome Sync",
                "running": is_process_running(scan_process_navidrome)
            },
            "popularity_scan": {
                "name": "Popularity Update",
                "running": is_process_running(scan_process_popularity)
            },
            "singles_scan": {
                "name": "Single Detection",
                "running": is_process_running(scan_process_singles)
            },
            "missing_releases_scan": {
                "name": "Missing Releases Scan",
                "running": is_process_running(scan_process_missing_releases)
            }
        })


@app.route("/api/recent-scans")
def api_recent_scans():
    """Return latest album scan events for dashboard refresh."""
    try:
        limit = request.args.get("limit", 10, type=int)
        from scan_history import get_recent_album_scans
        scans = get_recent_album_scans(limit=limit)
        return jsonify({"scans": scans})
    except Exception as e:
        logging.error(f"Error fetching recent scans: {e}")
        return jsonify({"scans": [], "error": str(e)}), 500


@app.route("/api/scan-progress")
def api_scan_progress():
    """API endpoint to get detailed scan progress"""
    try:
        from unified_scan import get_scan_progress
        progress = get_scan_progress()
        
        # If unified scan is not running, check for MP3, Navidrome, Popularity, and Singles scans
        if not progress.get("is_running", False):
            db_dir = os.path.dirname(DB_PATH)
            
            # Check MP3 scan progress
            mp3_progress_file = os.path.join(db_dir, "mp3_scan_progress.json")
            if os.path.exists(mp3_progress_file):
                try:
                    with open(mp3_progress_file, 'r') as f:
                        mp3_progress = json.load(f)
                        if mp3_progress.get("is_running", False):
                            return jsonify(mp3_progress)
                except:
                    pass
            
            # Check Navidrome scan progress
            nav_progress_file = os.path.join(db_dir, "navidrome_scan_progress.json")
            if os.path.exists(nav_progress_file):
                try:
                    with open(nav_progress_file, 'r') as f:
                        nav_progress = json.load(f)
                        if nav_progress.get("is_running", False):
                            return jsonify(nav_progress)
                except:
                    pass
            
            # Check Popularity scan progress
            popularity_progress_file = os.path.join(db_dir, "popularity_scan_progress.json")
            if os.path.exists(popularity_progress_file):
                try:
                    with open(popularity_progress_file, 'r') as f:
                        pop_progress = json.load(f)
                        if pop_progress.get("is_running", False):
                            return jsonify(pop_progress)
                except:
                    pass
            
            # Check Singles scan progress
            singles_progress_file = os.path.join(db_dir, "singles_scan_progress.json")
            if os.path.exists(singles_progress_file):
                try:
                    with open(singles_progress_file, 'r') as f:
                        singles_progress = json.load(f)
                        if singles_progress.get("is_running", False):
                            return jsonify(singles_progress)
                except:
                    pass
            
            # Check Missing Releases scan progress
            missing_releases_progress_file = os.path.join(db_dir, "missing_releases_scan_progress.json")
            if os.path.exists(missing_releases_progress_file):
                try:
                    with open(missing_releases_progress_file, 'r') as f:
                        missing_releases_progress = json.load(f)
                        if missing_releases_progress.get("is_running", False):
                            return jsonify(missing_releases_progress)
                except:
                    pass
        
        return jsonify(progress)
    except Exception as e:
        logging.error(f"Error getting scan progress: {e}")
        return jsonify({
            "is_running": False,
            "percent_complete": 0,
            "current_artist": None,
            "current_album": None,
            "error": str(e)
        })


@app.route("/api/scan-logs")
def api_scan_logs():
    """API endpoint to get last log entries for each scan type"""
    import re
    
    log_files = {
        "navidrome": LOG_PATH,  # Navidrome scans log to main app log
        "popularity": os.path.join(os.path.dirname(CONFIG_PATH), "popularity.log"),
        "singles": os.path.join(os.path.dirname(CONFIG_PATH), "singledetection.log"),
        # Beets auto-import now drives the file-path scan; read its log instead of the old mp3scanner log
        "file_paths": os.path.join(os.path.dirname(CONFIG_PATH), "beets_import.log")
    }
    
    def extract_meaningful_log(line):
        """Extract meaningful log message, removing timestamps and excessive details"""
        # Remove timestamp prefix (e.g., "2024-01-15 10:30:45,123 - ")
        line = re.sub(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ - ', '', line)
        # Remove log level prefix (e.g., "INFO - ", "DEBUG - ", "ERROR - ")
        line = re.sub(r'^(INFO|DEBUG|WARNING|ERROR|CRITICAL)\s*-?\s*', '', line)
        # Remove full file paths (keep just filename)
        line = re.sub(r'[A-Za-z]:\\[^\s]*\\', '', line)
        line = re.sub(r'/[^\s]*/([^\s/]*\.mp3)', r'\1', line)
        return line.strip()
    
    def is_meaningful_log(line):
        """Check if log line contains meaningful scan information"""
        line_lower = line.lower()
        # Keywords that indicate meaningful log entries
        meaningful_keywords = [
            'scanning', 'syncing', 'scanning album', 'scanning artist',
            'found', 'match', 'updated', 'importing', 'processing',
            'completed', 'finished', 'detected', 'checking', 'analyzing',
            'no match', 'error', 'failed', 'success', 'track', 'album',
            'artist', 'single', 'rating', 'score', 'popularity'
        ]
        # Skip debug lines that are too verbose
        skip_keywords = ['debug', 'checking match', 'checking for', 'found in']
        
        # Check if line contains skip keywords
        for skip in skip_keywords:
            if skip in line_lower:
                return False
        
        # Check if line contains meaningful keywords
        return any(keyword in line_lower for keyword in meaningful_keywords)
    
    result = {}
    for scan_type, log_path in log_files.items():
        lines = []
        if os.path.exists(log_path):
            try:
                with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                    all_lines = f.readlines()
                    # Get last meaningful non-empty lines
                    for line in reversed(all_lines):
                        line = line.strip()
                        if line and is_meaningful_log(line):
                            meaningful_line = extract_meaningful_log(line)
                            if meaningful_line and len(lines) < 3:
                                lines.append(meaningful_line)
                    lines.reverse()
            except Exception as e:
                lines = [f"Error reading log: {str(e)}"]
        result[scan_type] = lines
    
    return jsonify(result)


@app.route("/api/track-count")
def api_track_count():
    """API endpoint to get total track count for progress calculation"""
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM tracks")
            total_tracks = cursor.fetchone()[0]
            
            # Also get counts with different metadata filled in
            cursor.execute("SELECT COUNT(*) FROM tracks WHERE stars IS NOT NULL")
            navidrome_filled = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM tracks WHERE spotify_score IS NOT NULL")
            popularity_filled = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM tracks WHERE is_single IS NOT NULL")
            singles_filled = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM tracks WHERE file_path IS NOT NULL")
            filepath_filled = cursor.fetchone()[0]
            
            return jsonify({
                "total_tracks": total_tracks,
                "navidrome_filled": navidrome_filled,
                "popularity_filled": popularity_filled,
                "singles_filled": singles_filled,
                "filepath_filled": filepath_filled
            })
    except Exception as e:
        logging.error(f"Error getting track count: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/downloads")
def downloads():
    """Downloads page with qBittorrent and slskd search"""
    cfg, _ = _read_yaml(CONFIG_PATH)
    qbit_config = cfg.get("qbittorrent", {"enabled": False})
    slskd_config = cfg.get("slskd", {"enabled": False})
    
    return render_template("downloads.html", 
                         qbit_config=qbit_config,
                         slskd_config=slskd_config)


@app.route("/api/slskd/search", methods=["POST"])
def slskd_search():
    """Proxy endpoint for slskd search API - returns search ID for polling"""
    cfg, _ = _read_yaml(CONFIG_PATH)
    slskd_config = cfg.get("slskd", {})
    
    if not slskd_config.get("enabled"):
        return jsonify({"error": "slskd integration not enabled"}), 400
    
    query = request.json.get("query", "")
    if not query:
        return jsonify({"error": "Query parameter required"}), 400
    
    web_url = slskd_config.get("web_url", "http://localhost:5030")
    api_key = slskd_config.get("api_key", "")
    
    try:
        client = SlskdClient(web_url, api_key, enabled=True)
        search_id = client.start_search(query)
        
        if not search_id:
            return jsonify({"error": "Failed to start search"}), 500
        
        return jsonify({"searchId": search_id, "status": "searching"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/slskd/search/<search_id>", methods=["GET"])
def slskd_search_results(search_id):
    """Poll for Soulseek search results"""
    cfg, _ = _read_yaml(CONFIG_PATH)
    slskd_config = cfg.get("slskd", {})
    
    if not slskd_config.get("enabled"):
        return jsonify({"error": "slskd integration not enabled"}), 400
    
    web_url = slskd_config.get("web_url", "http://localhost:5030")
    api_key = slskd_config.get("api_key", "")
    
    try:
        client = SlskdClient(web_url, api_key, enabled=True)
        responses, state, is_complete = client.get_search_results(search_id)
        
        results = []
        for resp in responses:
            if hasattr(resp, 'files'):
                for file in resp.files:
                    results.append({
                        "username": resp.username,
                        "filename": file.filename,
                        "size": file.size,
                        "size_mb": f"{file.size_mb:.2f}",
                        "bitrate": file.bitrate,
                        "sample_rate": file.sample_rate,
                        "length": file.length,
                        "duration": file.duration_formatted,
                    })
        
        response_count = len(responses) if responses else 0
        logging.info(f"[SLSKD] search_id={search_id}, responses={response_count}, files={len(results)}, state={state}, complete={is_complete}")
        
        if response_count == 0:
            logging.warning(f"[SLSKD] Search {search_id} returned 0 responses - check if slskd service is reachable at {web_url}")
        elif len(results) == 0:
            logging.warning(f"[SLSKD] Search {search_id} got {response_count} responses but 0 files - peers may not have matching files")
        
        return jsonify({
            "results": results,
            "state": state,
            "responseCount": response_count,
            "fileCount": len(results),
            "isComplete": is_complete
        })
    except Exception as e:
        logging.error(f"[SLSKD] Error getting search results for {search_id}: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/scan/navidrome", methods=["POST"])
def scan_navidrome():
    """Run Navidrome import-only scan (no popularity/singles)."""
    global scan_process_navidrome
    
    with scan_lock:
        if scan_process_navidrome is not None:
            if isinstance(scan_process_navidrome, dict):
                thread = scan_process_navidrome.get('thread')
                if thread and thread.is_alive():
                    flash("Navidrome sync scan is already running", "warning")
                    return redirect(url_for("dashboard"))
            elif hasattr(scan_process_navidrome, 'is_alive') and scan_process_navidrome.is_alive():
                flash("Navidrome sync scan is already running", "warning")
                return redirect(url_for("dashboard"))
        
        try:
            db_dir = os.path.dirname(DB_PATH)
            nav_progress_file = os.path.join(db_dir, "navidrome_scan_progress.json")
            _write_progress_file(nav_progress_file, "navidrome_scan", True, {"status": "starting"})
            
            def run_navidrome_import_bg():
                try:
                    # Ensure singles/rating pipeline stays off during Navidrome metadata-only import
                    os.environ["SPTNR_SKIP_SINGLES"] = "1"

                    logging.info("Starting Navidrome import-only scan (no scoring/singles)")
                    artist_map = build_artist_index()
                    artists = list(artist_map.items())
                    total = len(artists)
                    for idx, (artist_name, info) in enumerate(artists, start=1):
                        scan_artist_to_db(artist_name, info.get("id"), verbose=False, force=False, processed_artists=idx, total_artists=total)
                    _write_progress_file(nav_progress_file, "navidrome_scan", False, {"status": "complete", "exit_code": 0})
                    logging.info("Navidrome import-only scan completed")
                except Exception as e:
                    logging.error(f"Error in Navidrome import-only scan: {e}", exc_info=True)
                    _write_progress_file(nav_progress_file, "navidrome_scan", False, {"status": "error", "error": str(e), "exit_code": 1})
                finally:
                    # Clear skip flag so popularity/singles scans run normally elsewhere
                    os.environ.pop("SPTNR_SKIP_SINGLES", None)
            
            scan_thread = threading.Thread(target=run_navidrome_import_bg, daemon=False)
            scan_thread.start()
            scan_process_navidrome = {'thread': scan_thread, 'type': 'navidrome'}
            flash("✅ Navidrome import started (metadata only)", "success")
        except Exception as e:
            logging.error(f"Error starting Navidrome import: {e}", exc_info=True)
            flash(f"❌ Error starting Navidrome import: {str(e)}", "danger")
    
    return redirect(url_for("dashboard"))


@app.route("/api/slskd/download", methods=["POST"])
def slskd_download():
    """Proxy endpoint to download from slskd"""
    cfg, _ = _read_yaml(CONFIG_PATH)
    slskd_config = cfg.get("slskd", {})
    
    if not slskd_config.get("enabled"):
        return jsonify({"error": "slskd integration not enabled"}), 400
    
    payload = request.json or {}
    files_payload = payload.get("files")
    username = payload.get("username", "")
    filename = payload.get("filename", "")
    size = payload.get("size", 0)

    # Batch mode: expect list of files
    if files_payload:
        if not isinstance(files_payload, list):
            return jsonify({"error": "files must be a list"}), 400
        normalized_files = []
        for entry in files_payload:
            u = entry.get("username")
            f = entry.get("filename")
            if not u or not f:
                return jsonify({"error": "Each file requires username and filename"}), 400
            normalized_files.append({
                "username": u,
                "filename": f,
                "size": int(entry.get("size") or 0)
            })

        web_url = slskd_config.get("web_url", "http://localhost:5030")
        api_key = slskd_config.get("api_key", "")
        try:
            client = SlskdClient(web_url, api_key, enabled=True)
            results = client.download_files(normalized_files)
            requested = sum(item.get("requested", 0) for item in results)
            successful_users = sum(1 for item in results if item.get("success"))
            overall_success = requested > 0 and successful_users > 0
            return jsonify({
                "success": overall_success,
                "requested": requested,
                "userBatches": results
            })
        except Exception as e:
            logging.error(f"[SLSKD] Batch download error: {str(e)}")
            return jsonify({"error": str(e)}), 500
    
    if not username or not filename:
        return jsonify({"error": "Username and filename required"}), 400
    
    web_url = slskd_config.get("web_url", "http://localhost:5030")
    api_key = slskd_config.get("api_key", "")
    
    try:
        client = SlskdClient(web_url, api_key, enabled=True)
        success = client.download_file(username, filename, size)
        
        if success:
            return jsonify({"success": True, "message": "Download enqueued"})
        else:
            return jsonify({"error": "Failed to enqueue download"}), 500
            
    except Exception as e:
        logging.error(f"[SLSKD] Download error: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/slskd/cancel", methods=["POST"])
def slskd_cancel():
    """Cancel a Soulseek download"""
    cfg, _ = _read_yaml(CONFIG_PATH)
    slskd_config = cfg.get("slskd", {})
    
    if not slskd_config.get("enabled"):
        return jsonify({"error": "slskd integration not enabled"}), 400
    
    username = request.json.get("username", "")
    filename = request.json.get("filename", "")
    
    if not username or not filename:
        return jsonify({"error": "Username and filename required"}), 400
    
    web_url = slskd_config.get("web_url", "http://localhost:5030")
    api_key = slskd_config.get("api_key", "")
    
    try:
        import requests as req
        
        headers = {"X-API-Key": api_key} if api_key else {}
        
        # Cancel download - DELETE request to the specific download
        cancel_url = f"{web_url}/api/v0/transfers/downloads/{username}/{filename}"
        
        resp = req.delete(cancel_url, headers=headers, timeout=10)
        
        if resp.status_code in [200, 204]:
            return jsonify({"success": True, "message": "Download cancelled successfully"})
        else:
            return jsonify({"error": f"Failed to cancel download: {resp.status_code}"}), 500
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/slskd/retry", methods=["POST"])
def slskd_retry():
    """Retry a failed Soulseek download"""
    cfg, _ = _read_yaml(CONFIG_PATH)
    slskd_config = cfg.get("slskd", {})
    
    if not slskd_config.get("enabled"):
        return jsonify({"error": "slskd integration not enabled"}), 400
    
    username = request.json.get("username", "")
    filename = request.json.get("filename", "")
    size = request.json.get("size", 0)
    
    if not username or not filename:
        return jsonify({"error": "Username and filename required"}), 400
    
    web_url = slskd_config.get("web_url", "http://localhost:5030")
    api_key = slskd_config.get("api_key", "")
    
    try:
        import requests as req_module
        
        # First cancel the existing download
        headers = {"X-API-Key": api_key} if api_key else {}
        cancel_url = f"{web_url}/api/v0/transfers/downloads/{username}/{filename}"
        req_module.delete(cancel_url, headers=headers, timeout=10)
        
        # Then re-queue it
        client = SlskdClient(web_url, api_key, enabled=True)
        success = client.download_file(username, filename, int(size))
        
        if success:
            return jsonify({"success": True, "message": f"Download retry queued for {filename}"})
        else:
            return jsonify({"error": "Failed to re-queue download"}), 500
            
    except Exception as e:
        logging.error(f"[SLSKD] Retry download error: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/slskd/search-again", methods=["POST"])
def slskd_search_again():
    """Search for a file again to find alternative sources"""
    cfg, _ = _read_yaml(CONFIG_PATH)
    slskd_config = cfg.get("slskd", {})
    
    if not slskd_config.get("enabled"):
        return jsonify({"error": "slskd integration not enabled"}), 400
    
    filename = request.json.get("filename", "")
    
    if not filename:
        return jsonify({"error": "Filename required"}), 400
    
    web_url = slskd_config.get("web_url", "http://localhost:5030")
    api_key = slskd_config.get("api_key", "")
    
    try:
        client = SlskdClient(web_url, api_key, enabled=True)
        search_id = client.start_search(filename)
        
        if search_id:
            return jsonify({
                "success": True,
                "message": f"Searching for '{filename}'",
                "search_id": search_id
            })
        else:
            return jsonify({"error": "Failed to start search"}), 500
            
    except Exception as e:
        logging.error(f"[SLSKD] Search again error: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/slskd/download-single", methods=["POST"])
def slskd_download_single():
    """Download a single track from Soulseek search results in playlist importer"""
    cfg, _ = _read_yaml(CONFIG_PATH)
    slskd_config = cfg.get("slskd", {})
    
    if not slskd_config.get("enabled"):
        return jsonify({"error": "slskd integration not enabled"}), 400
    
    username = request.json.get("username", "")
    filename = request.json.get("filename", "")
    size = request.json.get("size", 0)
    
    if not username or not filename:
        return jsonify({"error": "Username and filename required"}), 400
    
    web_url = slskd_config.get("web_url", "http://localhost:5030")
    api_key = slskd_config.get("api_key", "")
    
    try:
        client = SlskdClient(web_url, api_key, enabled=True)
        success = client.download_file(username, filename, int(size))
        
        if success:
            return jsonify({"success": True, "message": f"Download started for {filename}"})
        else:
            return jsonify({"error": "Failed to enqueue download"}), 500
            
    except Exception as e:
        logging.error(f"[SLSKD] Single file download error: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/musicbrainz/search", methods=["POST"])
def api_musicbrainz_search():
    """Search MusicBrainz for releases + local cached missing releases"""
    query = request.json.get("query", "").strip()
    if not query:
        return jsonify({"error": "Query parameter required"}), 400
    
    try:
        releases = []
        seen_ids = set()  # Track IDs to avoid duplicates
        
        # First, search local database for matching artists with missing releases
        try:
            conn = get_db()
            cursor = conn.cursor()
            
            # Search for artists matching the query
            query_pattern = f"%{query}%"
            cursor.execute("""
                SELECT DISTINCT artist, release_id, title, primary_type, first_release_date, cover_art_url, category
                FROM missing_releases
                WHERE artist LIKE ? OR title LIKE ?
                ORDER BY artist, first_release_date DESC
                LIMIT 50
            """, (query_pattern, query_pattern))
            
            local_results = cursor.fetchall()
            conn.close()
            
            # Add local results
            for row in local_results:
                artist, release_id, title, primary_type, first_release_date, cover_art_url, category = row
                
                # Create unique ID to check for duplicates
                result_id = f"{artist}_{release_id}"
                if result_id not in seen_ids:
                    seen_ids.add(result_id)
                    releases.append({
                        "id": release_id,
                        "title": title,
                        "artist": artist,
                        "artist-credit": [{"name": artist}],
                        "primary_type": primary_type,
                        "first_release_date": first_release_date,
                        "cover_art_url": cover_art_url,
                        "category": category,
                        "source": "local"
                    })
        except Exception as e:
            logging.warning(f"[MB_SEARCH] Error searching local database: {e}")
        
        # Then search MusicBrainz
        try:
            headers = {"User-Agent": "sptnr-web/1.0 (support@example.com)"}
            
            # Search for release groups
            url = "https://musicbrainz.org/ws/2/release-group"
            params = {
                "fmt": "json",
                "limit": 50,
                "query": query
            }
            
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            
            for rg in data.get("release-groups", []) or []:
                rg_id = rg.get("id", "")
                primary_type = rg.get("primary-type", "")
                artist_credit = rg.get("artist-credit", [])
                artist_name = artist_credit[0].get("name", "Unknown") if artist_credit else "Unknown"
                
                # Check if we already have this from local DB
                result_id = f"{artist_name}_{rg_id}"
                if result_id in seen_ids:
                    continue
                
                seen_ids.add(result_id)
                
                # Determine category
                category = primary_type
                if primary_type.lower() == "ep":
                    category = "EP"
                elif primary_type.lower() == "single":
                    category = "Single"
                elif primary_type.lower() == "album":
                    category = "Album"
                
                releases.append({
                    "id": rg_id,
                    "title": rg.get("title", ""),
                    "artist": artist_name,
                    "artist-credit": artist_credit,
                    "primary_type": primary_type,
                    "first_release_date": rg.get("first-release-date", ""),
                    "cover_art_url": f"https://coverartarchive.org/release-group/{rg_id}/front-500" if rg_id else "",
                    "category": category,
                    "source": "musicbrainz"
                })
        except requests.exceptions.Timeout:
            logging.warning("[MB_SEARCH] MusicBrainz request timed out")
        except Exception as e:
            logging.error(f"[MB_SEARCH] MusicBrainz search error: {e}")
        
        # Sort by artist and release date
        releases.sort(key=lambda x: (x.get("artist", "").lower(), x.get("first_release_date", "")), reverse=True)
        
        return jsonify({"releases": releases, "total": len(releases)})
        
    except Exception as e:
        logging.error(f"[MB_SEARCH] Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/musicbrainz/download", methods=["POST"])
def api_musicbrainz_download():
    """Initiate a managed download from MusicBrainz release"""
    data = request.json or {}
    release_id = data.get("release_id", "").strip()
    release_title = data.get("release_title", "").strip()
    artist = data.get("artist", "").strip()
    method = data.get("method", "").strip().lower()
    
    if not all([release_id, release_title, artist, method]):
        return jsonify({"error": "Missing required parameters"}), 400
    
    if method not in ["slskd", "qbittorrent"]:
        return jsonify({"error": "Invalid method. Use 'slskd' or 'qbittorrent'"}), 400
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Create search query
        download_query = f"{artist} {release_title}"
        
        # Insert into managed_downloads table
        cursor.execute("""
            INSERT INTO managed_downloads 
            (release_id, release_title, artist, method, status, download_query, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'queued', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """, (release_id, release_title, artist, method, download_query))
        
        tracking_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        # Immediately initiate the download in background thread with fresh connection
        if method == "slskd":
            thread = threading.Thread(target=_initiate_slskd_download_bg, args=(tracking_id, download_query), daemon=True)
            thread.start()
        elif method == "qbittorrent":
            thread = threading.Thread(target=_initiate_qbit_download_bg, args=(tracking_id, download_query), daemon=True)
            thread.start()
        
        return jsonify({
            "success": True,
            "tracking_id": tracking_id,
            "message": f"Download queued for {release_title}"
        })
        
    except Exception as e:
        logging.error(f"[MB_DOWNLOAD] Error: {e}")
        return jsonify({"error": str(e)}), 500


def _initiate_slskd_download_bg(tracking_id, query):
    """Background thread worker to initiate a Soulseek search and wait for user selection"""
    try:
        cfg, _ = _read_yaml(CONFIG_PATH)
        slskd_config = cfg.get("slskd", {})
        
        if not slskd_config.get("enabled"):
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE managed_downloads 
                SET status = 'error', error_message = 'Soulseek not enabled', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (tracking_id,))
            conn.commit()
            conn.close()
            return
        
        web_url = slskd_config.get("web_url", "http://localhost:5030")
        api_key = slskd_config.get("api_key", "")
        
        client = SlskdClient(web_url, api_key, enabled=True)
        search_id = client.search(query)
        
        if not search_id:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE managed_downloads 
                SET status = 'error', error_message = 'Failed to start search', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (tracking_id,))
            conn.commit()
            conn.close()
            return
        
        # Store search_id and update status
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE managed_downloads 
            SET status = 'searching', external_id = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (search_id, tracking_id))
        conn.commit()
        conn.close()
        
        # Start monitoring in a sub-thread
        def monitor_slskd_search():
            import time
            max_wait = 30  # Wait up to 30 seconds for results
            start_time = time.time()
            all_files = []  # Collect all results, not just the best
            
            while time.time() - start_time < max_wait:
                try:
                    responses, state, is_complete = client.get_search_results(search_id)
                    
                    if responses:
                        # Collect all matching files with scores
                        for response in responses:
                            if hasattr(response, 'files') and response.files:
                                for file_info in response.files:
                                    # Score the file based on how well it matches the query
                                    filename = file_info.get('filename', '').lower()
                                    query_lower = query.lower()
                                    
                                    # Simple scoring: count matching words
                                    query_words = query_lower.split()
                                    matches = sum(1 for word in query_words if word in filename)
                                    match_score = matches / len(query_words) if query_words else 0
                                    
                                    # Prefer files with audio extensions
                                    if any(filename.endswith(ext) for ext in ['.mp3', '.flac', '.m4a', '.aac', '.ogg']):
                                        match_score *= 1.2
                                    
                                    if match_score >= 0.3:  # Only include files with at least 30% match
                                        all_files.append({
                                            'username': response.username if hasattr(response, 'username') else 'Unknown',
                                            'filename': file_info.get('filename', ''),
                                            'size': file_info.get('size', 0),
                                            'match_score': match_score
                                        })
                    
                    if is_complete:
                        break
                    
                    time.sleep(1)
                    
                except Exception as e:
                    logging.error(f"[SLSKD_MONITOR] Error monitoring search {search_id}: {e}")
                    break
            
            # Save results and wait for user selection
            if all_files:
                try:
                    conn2 = get_db()
                    cursor2 = conn2.cursor()
                    
                    # Sort by match score (descending)
                    all_files.sort(key=lambda x: x['match_score'], reverse=True)
                    
                    # Insert all results into database
                    for file_result in all_files:
                        cursor2.execute("""
                            INSERT INTO slskd_search_results 
                            (download_id, username, filename, size, match_score)
                            VALUES (?, ?, ?, ?, ?)
                        """, (tracking_id, file_result['username'], file_result['filename'], 
                              file_result['size'], file_result['match_score']))
                    
                    # Update status to awaiting_selection
                    cursor2.execute("""
                        UPDATE managed_downloads 
                        SET status = 'awaiting_selection', updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """, (tracking_id,))
                    
                    conn2.commit()
                    conn2.close()
                    
                    logging.info(f"[SLSKD_MONITOR] Found {len(all_files)} results for download {tracking_id}, awaiting user selection")
                    
                except Exception as e:
                    logging.error(f"[SLSKD_MONITOR] Error saving results: {e}")
                    try:
                        conn2 = get_db()
                        cursor2 = conn2.cursor()
                        cursor2.execute("""
                            UPDATE managed_downloads 
                            SET status = 'error', error_message = ?, updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                        """, (str(e), tracking_id))
                        conn2.commit()
                        conn2.close()
                    except:
                        pass
            else:
                # No good matches found
                try:
                    conn2 = get_db()
                    cursor2 = conn2.cursor()
                    cursor2.execute("""
                        UPDATE managed_downloads 
                        SET status = 'error', error_message = 'No matching files found', updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """, (tracking_id,))
                    conn2.commit()
                    conn2.close()
                except:
                    pass
        
        # Start monitoring thread
        thread = threading.Thread(target=monitor_slskd_search, daemon=True)
        thread.start()
            
    except Exception as e:
        logging.error(f"[SLSKD_INIT] Error for tracking {tracking_id}: {e}")
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE managed_downloads 
                SET status = 'error', error_message = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (str(e), tracking_id))
            conn.commit()
            conn.close()
        except:
            pass


def _initiate_slskd_download(tracking_id, query, cursor, conn):
    """Helper to initiate a Soulseek download"""
    try:
        cfg, _ = _read_yaml(CONFIG_PATH)
        slskd_config = cfg.get("slskd", {})
        
        if not slskd_config.get("enabled"):
            cursor.execute("""
                UPDATE managed_downloads 
                SET status = 'error', error_message = 'Soulseek not enabled', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (tracking_id,))
            conn.commit()
            return
        
        web_url = slskd_config.get("web_url", "http://localhost:5030")
        api_key = slskd_config.get("api_key", "")
        
        client = SlskdClient(web_url, api_key, enabled=True)
        search_id = client.search(query)
        
        if not search_id:
            cursor.execute("""
                UPDATE managed_downloads 
                SET status = 'error', error_message = 'Failed to start search', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (tracking_id,))
            conn.commit()
            return
        
        # Store search_id and update status
        cursor.execute("""
            UPDATE managed_downloads 
            SET status = 'searching', external_id = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (search_id, tracking_id))
        conn.commit()
        
        # Start a thread to monitor the search and download when results are available
        def monitor_slskd_search():
            import time
            max_wait = 30  # Wait up to 30 seconds for results
            start_time = time.time()
            best_file = None
            best_match_score = 0
            
            while time.time() - start_time < max_wait:
                try:
                    responses, state, is_complete = client.get_search_results(search_id)
                    
                    if responses:
                        # Look through responses for best matching files
                        for response in responses:
                            if hasattr(response, 'files') and response.files:
                                for file_info in response.files:
                                    # Score the file based on how well it matches the query
                                    filename = file_info.get('filename', '').lower()
                                    query_lower = query.lower()
                                    
                                    # Simple scoring: count matching words
                                    query_words = query_lower.split()
                                    matches = sum(1 for word in query_words if word in filename)
                                    match_score = matches / len(query_words) if query_words else 0
                                    
                                    # Prefer files with audio extensions
                                    if any(filename.endswith(ext) for ext in ['.mp3', '.flac', '.m4a', '.aac', '.ogg']):
                                        match_score *= 1.2
                                    else:
                                        match_score *= 0.5  # Lower score for non-audio
                                    
                                    if match_score > best_match_score:
                                        best_match_score = match_score
                                        best_file = {
                                            'username': response.username if hasattr(response, 'username') else 'Unknown',
                                            'filename': file_info.get('filename', ''),
                                            'size': file_info.get('size', 0),
                                            'match_score': match_score
                                        }
                    
                    if is_complete:
                        break
                    
                    time.sleep(1)
                    
                except Exception as e:
                    logging.error(f"[SLSKD_MONITOR] Error monitoring search {search_id}: {e}")
                    break
            
            # Download the best file found
            if best_file and best_match_score >= 0.3:  # Minimum 30% match
                try:
                    conn2 = get_db()
                    cursor2 = conn2.cursor()
                    
                    # Start the download
                    success = client.download_file(
                        best_file['username'],
                        best_file['filename'],
                        best_file['size']
                    )
                    
                    if success:
                        cursor2.execute("""
                            UPDATE managed_downloads 
                            SET status = 'downloading', updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                        """, (tracking_id,))
                        logging.info(f"[SLSKD_MONITOR] Started download: {best_file['filename']} from {best_file['username']}")
                    else:
                        cursor2.execute("""
                            UPDATE managed_downloads 
                            SET status = 'error', error_message = 'Failed to start file download', updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                        """, (tracking_id,))
                    
                    conn2.commit()
                    conn2.close()
                    
                except Exception as e:
                    logging.error(f"[SLSKD_MONITOR] Error downloading file: {e}")
                    try:
                        conn2 = get_db()
                        cursor2 = conn2.cursor()
                        cursor2.execute("""
                            UPDATE managed_downloads 
                            SET status = 'error', error_message = ?, updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                        """, (str(e), tracking_id))
                        conn2.commit()
                        conn2.close()
                    except:
                        pass
            else:
                # No good matches found
                try:
                    conn2 = get_db()
                    cursor2 = conn2.cursor()
                    cursor2.execute("""
                        UPDATE managed_downloads 
                        SET status = 'error', error_message = 'No matching files found', updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """, (tracking_id,))
                    conn2.commit()
                    conn2.close()
                except:
                    pass
        
        # Start monitoring thread
        import threading
        thread = threading.Thread(target=monitor_slskd_search, daemon=True)
        thread.start()
            
    except Exception as e:
        logging.error(f"[SLSKD_INIT] Error for tracking {tracking_id}: {e}")
        cursor.execute("""
            UPDATE managed_downloads 
            SET status = 'error', error_message = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (str(e), tracking_id))
        conn.commit()


def _initiate_qbit_download_bg(tracking_id, query):
    """Background thread worker to initiate a qBittorrent download with fresh DB connection"""
    try:
        cfg, _ = _read_yaml(CONFIG_PATH)
        qbit_config = cfg.get("qbittorrent", {})
        
        if not qbit_config.get("enabled"):
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE managed_downloads 
                SET status = 'error', error_message = 'qBittorrent not enabled', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (tracking_id,))
            conn.commit()
            conn.close()
            return
        
        web_url = qbit_config.get("web_url", "http://localhost:8080")
        username = qbit_config.get("username", "")
        password = qbit_config.get("password", "")
        
        # Update status to searching
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE managed_downloads 
            SET status = 'searching', updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (tracking_id,))
        conn.commit()
        conn.close()
        
        # Start search in qBittorrent in a background thread
        def search_and_add_qbit():
            try:
                import requests as req
                import time
                
                session = req.Session()
                
                # Login if credentials provided
                if username and password:
                    login_url = f"{web_url}/api/v2/auth/login"
                    try:
                        session.post(login_url, data={"username": username, "password": password}, timeout=5)
                    except:
                        pass  # May not require login
                
                # Start search
                search_url = f"{web_url}/api/v2/search/start"
                resp = session.post(search_url, data={"pattern": query, "plugins": "all", "category": "music"}, timeout=10)
                
                if resp.status_code not in [200, 201]:
                    raise Exception(f"Search failed: {resp.status_code}")
                
                search_data = resp.json()
                search_id = search_data.get("id")
                
                if not search_id:
                    raise Exception("No search ID returned from qBittorrent")
                
                # Poll for results
                best_result = None
                for i in range(60):  # Poll for up to 30 seconds
                    time.sleep(0.5)
                    
                    status_url = f"{web_url}/api/v2/search/status"
                    status_resp = session.get(status_url, params={"id": search_id}, timeout=5)
                    
                    if status_resp.status_code == 200:
                        status_data = status_resp.json()
                        
                        if status_data and len(status_data) > 0:
                            # Get results
                            results_url = f"{web_url}/api/v2/search/results"
                            results_resp = session.get(results_url, params={"id": search_id, "limit": 100}, timeout=5)
                            
                            if results_resp.status_code == 200:
                                data = results_resp.json()
                                results = data.get("results", [])
                                
                                if results:
                                    # Pick the result with best seeders
                                    best_result = max(results, key=lambda x: x.get('nb_seeders', 0))
                            
                            # Check if search is done
                            search_status = status_data[0]
                            if search_status.get("status") == "Stopped":
                                break
                
                # Stop search
                try:
                    stop_url = f"{web_url}/api/v2/search/stop"
                    session.post(stop_url, data={"id": search_id}, timeout=5)
                except:
                    pass
                
                # Add the best torrent found
                if best_result:
                    conn2 = get_db()
                    cursor2 = conn2.cursor()
                    
                    try:
                        # Add magnet link if available
                        add_url = f"{web_url}/api/v2/torrents/add"
                        magnet = best_result.get('magnet_uri') or best_result.get('magnet')
                        torrent_url = best_result.get('torrent_url') or best_result.get('link')
                        
                        if magnet:
                            resp = session.post(add_url, data={"urls": magnet}, timeout=10)
                        elif torrent_url:
                            resp = session.post(add_url, data={"urls": torrent_url}, timeout=10)
                        else:
                            raise Exception("No magnet link or torrent URL found")
                        
                        if resp.status_code in [200, 403]:  # 403 might mean already added
                            cursor2.execute("""
                                UPDATE managed_downloads 
                                SET status = 'downloading', updated_at = CURRENT_TIMESTAMP
                                WHERE id = ?
                            """, (tracking_id,))
                            logging.info(f"[QBIT_MONITOR] Added torrent: {best_result.get('name', 'Unknown')}")
                        else:
                            cursor2.execute("""
                                UPDATE managed_downloads 
                                SET status = 'error', error_message = ?, updated_at = CURRENT_TIMESTAMP
                                WHERE id = ?
                            """, (f"qBittorrent returned {resp.status_code}", tracking_id))
                    except Exception as e:
                        cursor2.execute("""
                            UPDATE managed_downloads 
                            SET status = 'error', error_message = ?, updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                        """, (str(e), tracking_id))
                    
                    conn2.commit()
                    conn2.close()
                else:
                    conn2 = get_db()
                    cursor2 = conn2.cursor()
                    cursor2.execute("""
                        UPDATE managed_downloads 
                        SET status = 'error', error_message = 'No torrent results found', updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """, (tracking_id,))
                    conn2.commit()
                    conn2.close()
                    
            except Exception as e:
                logging.error(f"[QBIT_MONITOR] Error: {e}")
                try:
                    conn2 = get_db()
                    cursor2 = conn2.cursor()
                    cursor2.execute("""
                        UPDATE managed_downloads 
                        SET status = 'error', error_message = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """, (str(e), tracking_id))
                    conn2.commit()
                    conn2.close()
                except:
                    pass
        
        # Start qBit search in a background thread
        thread = threading.Thread(target=search_and_add_qbit, daemon=True)
        thread.start()
        
    except Exception as e:
        logging.error(f"[QBIT_INIT] Error for tracking {tracking_id}: {e}")
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE managed_downloads 
                SET status = 'error', error_message = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (str(e), tracking_id))
            conn.commit()
            conn.close()
        except:
            pass


def _initiate_qbit_download(tracking_id, query, cursor, conn):
    """Helper to initiate a qBittorrent download"""
    try:
        cfg, _ = _read_yaml(CONFIG_PATH)
        qbit_config = cfg.get("qbittorrent", {})
        
        if not qbit_config.get("enabled"):
            cursor.execute("""
                UPDATE managed_downloads 
                SET status = 'error', error_message = 'qBittorrent not enabled', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (tracking_id,))
            conn.commit()
            return
        
        web_url = qbit_config.get("web_url", "http://localhost:8080")
        username = qbit_config.get("username", "")
        password = qbit_config.get("password", "")
        
        # Update status to searching
        cursor.execute("""
            UPDATE managed_downloads 
            SET status = 'searching', updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (tracking_id,))
        conn.commit()
        
        # Start search in qBittorrent in a background thread
        def search_and_add_qbit():
            try:
                import requests as req
                import time
                
                session = req.Session()
                
                # Login if credentials provided
                if username and password:
                    login_url = f"{web_url}/api/v2/auth/login"
                    try:
                        session.post(login_url, data={"username": username, "password": password}, timeout=5)
                    except:
                        pass  # May not require login
                
                # Start search
                search_url = f"{web_url}/api/v2/search/start"
                resp = session.post(search_url, data={"pattern": query, "plugins": "all", "category": "music"}, timeout=10)
                
                if resp.status_code not in [200, 201]:
                    raise Exception(f"Search failed: {resp.status_code}")
                
                search_data = resp.json()
                search_id = search_data.get("id")
                
                if not search_id:
                    raise Exception("No search ID returned from qBittorrent")
                
                # Poll for results
                best_result = None
                for i in range(60):  # Poll for up to 30 seconds
                    time.sleep(0.5)
                    
                    status_url = f"{web_url}/api/v2/search/status"
                    status_resp = session.get(status_url, params={"id": search_id}, timeout=5)
                    
                    if status_resp.status_code == 200:
                        status_data = status_resp.json()
                        
                        if status_data and len(status_data) > 0:
                            # Get results
                            results_url = f"{web_url}/api/v2/search/results"
                            results_resp = session.get(results_url, params={"id": search_id, "limit": 100}, timeout=5)
                            
                            if results_resp.status_code == 200:
                                data = results_resp.json()
                                results = data.get("results", [])
                                
                                if results:
                                    # Pick the result with best seeders
                                    best_result = max(results, key=lambda x: x.get('nb_seeders', 0))
                            
                            # Check if search is done
                            search_status = status_data[0]
                            if search_status.get("status") == "Stopped":
                                break
                
                # Stop search
                try:
                    stop_url = f"{web_url}/api/v2/search/stop"
                    session.post(stop_url, data={"id": search_id}, timeout=5)
                except:
                    pass
                
                # Add the best torrent found
                if best_result:
                    conn2 = get_db()
                    cursor2 = conn2.cursor()
                    
                    try:
                        # Add magnet link if available
                        add_url = f"{web_url}/api/v2/torrents/add"
                        magnet = best_result.get('magnet_uri') or best_result.get('magnet')
                        torrent_url = best_result.get('torrent_url') or best_result.get('link')
                        
                        if magnet:
                            resp = session.post(add_url, data={"urls": magnet}, timeout=10)
                        elif torrent_url:
                            resp = session.post(add_url, data={"urls": torrent_url}, timeout=10)
                        else:
                            raise Exception("No magnet link or torrent URL found")
                        
                        if resp.status_code in [200, 403]:  # 403 might mean already added
                            cursor2.execute("""
                                UPDATE managed_downloads 
                                SET status = 'downloading', updated_at = CURRENT_TIMESTAMP
                                WHERE id = ?
                            """, (tracking_id,))
                            logging.info(f"[QBIT_MONITOR] Added torrent: {best_result.get('name', 'Unknown')}")
                        else:
                            cursor2.execute("""
                                UPDATE managed_downloads 
                                SET status = 'error', error_message = ?, updated_at = CURRENT_TIMESTAMP
                                WHERE id = ?
                            """, (f"qBittorrent returned {resp.status_code}", tracking_id))
                    except Exception as e:
                        cursor2.execute("""
                            UPDATE managed_downloads 
                            SET status = 'error', error_message = ?, updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                        """, (str(e), tracking_id))
                    
                    conn2.commit()
                    conn2.close()
                else:
                    conn2 = get_db()
                    cursor2 = conn2.cursor()
                    cursor2.execute("""
                        UPDATE managed_downloads 
                        SET status = 'error', error_message = 'No torrent results found', updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """, (tracking_id,))
                    conn2.commit()
                    conn2.close()
                    
            except Exception as e:
                logging.error(f"[QBIT_MONITOR] Error: {e}")
                try:
                    conn2 = get_db()
                    cursor2 = conn2.cursor()
                    cursor2.execute("""
                        UPDATE managed_downloads 
                        SET status = 'error', error_message = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """, (str(e), tracking_id))
                    conn2.commit()
                    conn2.close()
                except:
                    pass
        
        # Start qBit search in a background thread
        import threading
        thread = threading.Thread(target=search_and_add_qbit, daemon=True)
        thread.start()
        
    except Exception as e:
        logging.error(f"[QBIT_INIT] Error for tracking {tracking_id}: {e}")
        cursor.execute("""
            UPDATE managed_downloads 
            SET status = 'error', error_message = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (str(e), tracking_id))
        conn.commit()


@app.route("/api/musicbrainz/downloads", methods=["GET"])
def api_musicbrainz_downloads():
    """Get all managed downloads"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT id, release_id, release_title, artist, method, status, 
                   external_id, error_message, created_at, updated_at, completed_at
            FROM managed_downloads
            WHERE status != 'removed'
            ORDER BY created_at DESC
            LIMIT 100
        """)
        
        rows = cursor.fetchall()
        conn.close()
        
        downloads = []
        for row in rows:
            downloads.append({
                "id": row[0],
                "release_id": row[1],
                "release_title": row[2],
                "artist": row[3],
                "method": row[4],
                "status": row[5],
                "external_id": row[6],
                "error_message": row[7],
                "created_at": row[8],
                "updated_at": row[9],
                "completed_at": row[10]
            })
        
        return jsonify({"downloads": downloads})
        
    except Exception as e:
        logging.error(f"[MB_DOWNLOADS] Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/musicbrainz/download/<int:download_id>/retry", methods=["POST"])
def api_musicbrainz_retry(download_id):
    """Retry a failed download"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT release_id, release_title, artist, method, download_query
            FROM managed_downloads
            WHERE id = ?
        """, (download_id,))
        
        row = cursor.fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Download not found"}), 404
        
        _, _, _, method, download_query = row
        
        # Reset status to queued and clear error
        cursor.execute("""
            UPDATE managed_downloads 
            SET status = 'queued', error_message = NULL, external_id = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (download_id,))
        conn.commit()
        conn.close()
        
        # Reinitiate download in background thread with fresh connection
        if method == "slskd":
            thread = threading.Thread(target=_initiate_slskd_download_bg, args=(download_id, download_query), daemon=True)
            thread.start()
        elif method == "qbittorrent":
            thread = threading.Thread(target=_initiate_qbit_download_bg, args=(download_id, download_query), daemon=True)
            thread.start()
        
        return jsonify({"success": True, "message": "Download retry initiated"})
        
    except Exception as e:
        logging.error(f"[MB_RETRY] Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/musicbrainz/download/<int:download_id>", methods=["DELETE"])
def api_musicbrainz_remove(download_id):
    """Remove a download from the list"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("""
            UPDATE managed_downloads 
            SET status = 'removed', updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (download_id,))
        
        conn.commit()
        conn.close()
        
        return jsonify({"success": True})
        
    except Exception as e:
        logging.error(f"[MB_REMOVE] Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/slskd/search-results/<int:download_id>", methods=["GET"])
def api_slskd_search_results(download_id):
    """Get Soulseek search results for a download awaiting user selection"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Verify download exists and is awaiting selection
        cursor.execute("""
            SELECT status, method FROM managed_downloads WHERE id = ?
        """, (download_id,))
        
        download = cursor.fetchone()
        if not download:
            conn.close()
            return jsonify({"error": "Download not found"}), 404
        
        status, method = download
        if status != "awaiting_selection" or method != "slskd":
            conn.close()
            return jsonify({"error": "Download is not awaiting Soulseek selection"}), 400
        
        # Get all search results
        cursor.execute("""
            SELECT id, username, filename, size, match_score
            FROM slskd_search_results
            WHERE download_id = ?
            ORDER BY match_score DESC
        """, (download_id,))
        
        results = cursor.fetchall()
        conn.close()
        
        search_results = []
        for row in results:
            search_results.append({
                "result_id": row[0],
                "username": row[1],
                "filename": row[2],
                "size": row[3],
                "match_score": row[4]
            })
        
        return jsonify({"results": search_results})
        
    except Exception as e:
        logging.error(f"[SLSKD_RESULTS] Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/slskd/download-file", methods=["POST"])
def api_slskd_download_file():
    """User selects a file from search results and initiates download"""
    try:
        data = request.json or {}
        download_id = data.get("download_id")
        result_id = data.get("result_id")
        
        if not download_id or not result_id:
            return jsonify({"error": "Missing download_id or result_id"}), 400
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Get the selected result
        cursor.execute("""
            SELECT username, filename, size FROM slskd_search_results WHERE id = ? AND download_id = ?
        """, (result_id, download_id))
        
        result = cursor.fetchone()
        if not result:
            conn.close()
            return jsonify({"error": "Result not found"}), 404
        
        username, filename, size = result
        
        # Mark this result as selected
        cursor.execute("""
            UPDATE slskd_search_results SET selected = 1 WHERE id = ?
        """, (result_id,))
        
        # Update download status
        cursor.execute("""
            UPDATE managed_downloads SET status = 'initiating_download', updated_at = CURRENT_TIMESTAMP WHERE id = ?
        """, (download_id,))
        
        conn.commit()
        conn.close()
        
        # Initiate the download in a background thread
        def perform_slskd_download():
            try:
                cfg, _ = _read_yaml(CONFIG_PATH)
                slskd_config = cfg.get("slskd", {})
                web_url = slskd_config.get("web_url", "http://localhost:5030")
                api_key = slskd_config.get("api_key", "")
                
                client = SlskdClient(web_url, api_key, enabled=True)
                success = client.download_file(username, filename, size)
                
                conn2 = get_db()
                cursor2 = conn2.cursor()
                
                if success:
                    cursor2.execute("""
                        UPDATE managed_downloads 
                        SET status = 'downloading', updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """, (download_id,))
                    logging.info(f"[SLSKD_DOWNLOAD] Started download: {filename} from {username}")
                else:
                    cursor2.execute("""
                        UPDATE managed_downloads 
                        SET status = 'error', error_message = 'Failed to start file download', updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """, (download_id,))
                    logging.error(f"[SLSKD_DOWNLOAD] Failed to download: {filename} from {username}")
                
                conn2.commit()
                conn2.close()
                
            except Exception as e:
                logging.error(f"[SLSKD_DOWNLOAD] Error: {e}")
                try:
                    conn2 = get_db()
                    cursor2 = conn2.cursor()
                    cursor2.execute("""
                        UPDATE managed_downloads 
                        SET status = 'error', error_message = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """, (str(e), download_id))
                    conn2.commit()
                    conn2.close()
                except:
                    pass
        
        thread = threading.Thread(target=perform_slskd_download, daemon=True)
        thread.start()
        
        return jsonify({"success": True, "message": f"Download initiated for {filename}"})
        
    except Exception as e:
        logging.error(f"[SLSKD_DOWNLOAD_FILE] Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/slskd/search-again/<int:download_id>", methods=["POST"])
def api_slskd_search_again(download_id):
    """Retry search for a failed Soulseek download with a new query"""
    try:
        data = request.json or {}
        new_query = data.get("query", "").strip()
        
        if not new_query:
            return jsonify({"error": "Query parameter required"}), 400
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Get the original download
        cursor.execute("""
            SELECT method FROM managed_downloads WHERE id = ?
        """, (download_id,))
        
        download = cursor.fetchone()
        if not download or download[0] != "slskd":
            conn.close()
            return jsonify({"error": "Download not found or not a Soulseek download"}), 404
        
        # Clear previous search results
        cursor.execute("""
            DELETE FROM slskd_search_results WHERE download_id = ?
        """, (download_id,))
        
        # Reset status and clear error
        cursor.execute("""
            UPDATE managed_downloads 
            SET status = 'queued', error_message = NULL, external_id = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (download_id,))
        
        conn.commit()
        conn.close()
        
        # Reinitiate search with new query
        thread = threading.Thread(target=_initiate_slskd_download_bg, args=(download_id, new_query), daemon=True)
        thread.start()
        
        return jsonify({"success": True, "message": "New search initiated"})
        
    except Exception as e:
        logging.error(f"[SLSKD_SEARCH_AGAIN] Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/qbittorrent/search", methods=["POST"])
def qbit_search():
    """Proxy endpoint for qBittorrent search API"""
    cfg, _ = _read_yaml(CONFIG_PATH)
    qbit_config = cfg.get("qbittorrent", {})
    
    if not qbit_config.get("enabled"):
        return jsonify({"error": "qBittorrent integration not enabled"}), 400
    
    query = request.json.get("query", "")
    if not query:
        return jsonify({"error": "Query parameter required"}), 400
    
    web_url = qbit_config.get("web_url", "http://localhost:8080")
    username = qbit_config.get("username", "")
    password = qbit_config.get("password", "")
    
    try:
        import requests as req
        
        # Login if credentials provided
        session = req.Session()
        if username and password:
            login_url = f"{web_url}/api/v2/auth/login"
            session.post(login_url, data={"username": username, "password": password})
        
        # Start search with music category and all plugins
        search_url = f"{web_url}/api/v2/search/start"
        resp = session.post(search_url, data={"pattern": query, "plugins": "all", "category": "music"})
        
        if resp.status_code != 200:
            return jsonify({"error": f"Search failed: {resp.status_code}"}), 500
        
        search_data = resp.json()
        search_id = search_data.get("id")
        
        if not search_id:
            return jsonify({"error": "No search ID returned"}), 500
        
        # Poll for results (max 15 seconds with longer wait time for plugins to respond)
        import time
        results = []
        for i in range(30):
            time.sleep(0.5)
            status_url = f"{web_url}/api/v2/search/status"
            status_resp = session.get(status_url, params={"id": search_id})
            
            if status_resp.status_code == 200:
                status_data = status_resp.json()
                if status_data and len(status_data) > 0:
                    search_status = status_data[0]
                    # Get results even if still searching (partial results)
                    results_url = f"{web_url}/api/v2/search/results"
                    results_resp = session.get(results_url, params={"id": search_id, "limit": 100})
                    if results_resp.status_code == 200:
                        data = results_resp.json()
                        results = data.get("results", [])
                    
                    # If search is stopped, we're done
                    if search_status.get("status") == "Stopped":
                        break
        
        # Stop search
        stop_url = f"{web_url}/api/v2/search/stop"
        session.post(stop_url, data={"id": search_id})
        
        return jsonify({"results": results})
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/qbittorrent/add", methods=["POST"])
def qbit_add_torrent():
    """Proxy endpoint to add torrent to qBittorrent"""
    cfg, _ = _read_yaml(CONFIG_PATH)
    qbit_config = cfg.get("qbittorrent", {})
    
    if not qbit_config.get("enabled"):
        return jsonify({"error": "qBittorrent integration not enabled"}), 400
    
    torrent_url = request.json.get("url", "")
    if not torrent_url:
        return jsonify({"error": "URL parameter required"}), 400
    
    web_url = qbit_config.get("web_url", "http://localhost:8080")
    username = qbit_config.get("username", "")
    password = qbit_config.get("password", "")
    
    try:
        import requests as req
        
        session = req.Session()
        if username and password:
            login_url = f"{web_url}/api/v2/auth/login"
            session.post(login_url, data={"username": username, "password": password})
        
        # Add torrent with Music category
        add_url = f"{web_url}/api/v2/torrents/add"
        resp = session.post(add_url, data={"urls": torrent_url, "category": "Music"})
        
        if resp.status_code == 200:
            return jsonify({"success": True, "message": "Torrent added successfully to Music category"})
        else:
            return jsonify({"error": f"Failed to add torrent: {resp.status_code}"}), 500

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/qbittorrent/force-start", methods=["POST"])
def qbit_force_start():
    """Force-start or resume a stalled qBittorrent torrent"""
    cfg, _ = _read_yaml(CONFIG_PATH)
    qbit_config = cfg.get("qbittorrent", {})

    if not qbit_config.get("enabled"):
        return jsonify({"error": "qBittorrent integration not enabled"}), 400

    data = request.get_json(silent=True) or {}
    torrent_hash = data.get("hash", "").strip()
    if not torrent_hash:
        return jsonify({"error": "hash is required"}), 400

    web_url = qbit_config.get("web_url", "http://localhost:8080")
    username = qbit_config.get("username", "")
    password = qbit_config.get("password", "")

    try:
        import requests as req

        session = req.Session()
        login_url = f"{web_url}/api/v2/auth/login"
        login_resp = session.post(login_url, data={"username": username, "password": password}, timeout=10)

        if login_resp.text != "Ok.":
            return jsonify({"error": "Failed to login to qBittorrent"}), 500

        # Force start the torrent and resume if it was paused
        force_url = f"{web_url}/api/v2/torrents/setForceStart"
        force_resp = session.post(force_url, data={"hashes": torrent_hash, "value": "true"}, timeout=10)
        if force_resp.status_code != 200:
            return jsonify({"error": f"Failed to force start: {force_resp.status_code}"}), 500

        resume_url = f"{web_url}/api/v2/torrents/resume"
        session.post(resume_url, data={"hashes": torrent_hash}, timeout=10)

        return jsonify({"success": True})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/qbittorrent/stop", methods=["POST"])
def qbit_stop():
    """Pause/stop a qBittorrent torrent"""
    cfg, _ = _read_yaml(CONFIG_PATH)
    qbit_config = cfg.get("qbittorrent", {})

    if not qbit_config.get("enabled"):
        return jsonify({"error": "qBittorrent integration not enabled"}), 400

    data = request.get_json(silent=True) or {}
    torrent_hash = data.get("hash", "").strip()
    if not torrent_hash:
        return jsonify({"error": "hash is required"}), 400

    web_url = qbit_config.get("web_url", "http://localhost:8080")
    username = qbit_config.get("username", "")
    password = qbit_config.get("password", "")

    try:
        import requests as req

        session = req.Session()
        login_url = f"{web_url}/api/v2/auth/login"
        login_resp = session.post(login_url, data={"username": username, "password": password}, timeout=10)

        if login_resp.text != "Ok.":
            return jsonify({"error": "Failed to login to qBittorrent"}), 500

        # Pause the torrent
        pause_url = f"{web_url}/api/v2/torrents/pause"
        pause_resp = session.post(pause_url, data={"hashes": torrent_hash}, timeout=10)
        
        if pause_resp.status_code != 200:
            return jsonify({"error": f"Failed to pause: {pause_resp.status_code}"}), 500

        return jsonify({"success": True})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/metadata")
def api_metadata():
    """API endpoint for MP3 metadata lookup"""
    lookup_type = request.args.get("type", "track")
    identifier = request.args.get("id", "")
    
    metadata = {}
    
    try:
        if lookup_type == "track" and identifier:
            # Get track info from database
            track_info = get_track_metadata_from_db(identifier, DB_PATH)
            
            if track_info:
                # Try to find the file and read MP3 metadata
                artist = track_info.get("artist", "")
                album = track_info.get("album", "")
                title = track_info.get("title", "")
                stored_file_path = track_info.get("file_path", "")
                
                # Construct full path from stored path
                music_root = os.environ.get("MUSIC_ROOT", "/music")
                file_path = None
                
                # First try using stored file path from Navidrome
                if stored_file_path:
                    # Navidrome stores paths relative to music root
                    full_path = os.path.join(music_root, stored_file_path)
                    if os.path.exists(full_path):
                        file_path = full_path
                
                # Fallback to searching if stored path doesn't work
                if not file_path:
                    try:
                        # Use timeout to prevent hanging
                        file_path = find_track_file(artist, album, title, music_root, timeout_seconds=3)
                    except Exception as e:
                        # If file search fails, continue without file metadata
                        pass
                
                if file_path and os.path.exists(file_path):
                    try:
                        metadata = read_mp3_metadata(file_path)
                    except Exception as e:
                        # If MP3 read fails, use database info
                        metadata = {
                            "title": title,
                            "artist": artist,
                            "album": album,
                            "track_id": identifier,
                            "note": f"MP3 read error: {str(e)}"
                        }
                else:
                    # Return database info if file not found
                    metadata = {
                        "title": title,
                        "artist": artist,
                        "album": album,
                        "track_id": identifier,
                        "note": "MP3 file not found in /music; showing database info"
                    }
                
                # Add scoring metadata from DB
                if track_info.get("spotify_score"):
                    metadata["spotify_score"] = track_info.get("spotify_score")
                if track_info.get("lastfm_ratio"):
                    metadata["lastfm_ratio"] = track_info.get("lastfm_ratio")
                if track_info.get("listenbrainz_score"):
                    metadata["listenbrainz_score"] = track_info.get("listenbrainz_score")
                if track_info.get("final_score"):
                    metadata["final_score"] = track_info.get("final_score")
                if track_info.get("stars"):
                    metadata["stars"] = track_info.get("stars")
                if track_info.get("is_single"):
                    metadata["is_single"] = bool(track_info.get("is_single"))
                if track_info.get("single_confidence"):
                    metadata["single_confidence"] = track_info.get("single_confidence")
        
        elif lookup_type == "album" and identifier:
            # Album lookup: artist/album format
            parts = identifier.split("/")
            if len(parts) >= 2:
                artist = parts[0]
                album = "/".join(parts[1:])
                
                conn = get_db()
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT 
                        AVG(stars) as avg_stars,
                        COUNT(*) as track_count,
                        SUM(CASE WHEN is_single = 1 THEN 1 ELSE 0 END) as singles_count,
                        MAX(last_scanned) as last_scanned
                    FROM tracks
                    WHERE artist = ? AND album = ?
                """, (artist, album))
                result = cursor.fetchone()
                conn.close()
                
                if result:
                    metadata = {
                        "album": album,
                        "artist": artist,
                        "tracks": result[1] or 0,
                        "average_rating": round(result[0], 2) if result[0] else 0,
                        "singles_detected": result[2] or 0,
                        "last_scanned": result[3] or "Never"
                    }
        
        elif lookup_type == "artist" and identifier:
            # Artist metadata
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT 
                    COUNT(DISTINCT album) as album_count,
                    COUNT(*) as track_count,
                    AVG(stars) as avg_stars,
                    SUM(CASE WHEN stars = 5 THEN 1 ELSE 0 END) as five_star_count
                FROM tracks
                WHERE artist = ?
            """, (identifier,))
            result = cursor.fetchone()
            conn.close()
            
            if result:
                genres = aggregate_genres_from_tracks(identifier, DB_PATH)
                metadata = {
                    "artist": identifier,
                    "albums": result[0] or 0,
                    "tracks": result[1] or 0,
                    "average_rating": round(result[2], 2) if result[2] else 0,
                    "five_star_tracks": result[3] or 0,
                    "genres": ", ".join(genres) if genres else "Not detected"
                }
    
    except Exception as e:
        metadata = {"error": str(e)}
    
    return jsonify(metadata)


def _fetch_album_art_from_musicbrainz(artist_name: str, album_name: str) -> bytes | None:
    """
    Fetch album art from MusicBrainz Cover Art Archive.
    
    Args:
        artist_name: Artist name
        album_name: Album name
        
    Returns:
        Image bytes if found, None otherwise
    """
    try:
        # Try to get MBID from database
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT beets_album_mbid FROM tracks 
            WHERE artist = ? AND album = ? AND beets_album_mbid IS NOT NULL
            LIMIT 1
        """, (artist_name, album_name))
        result = cursor.fetchone()
        conn.close()
        
        album_mbid = result['beets_album_mbid'] if result else None
        
        # If we don't have MBID, try to search for it
        if not album_mbid:
            try:
                search_url = "https://musicbrainz.org/ws/2/release-group"
                params = {
                    "query": f'release:"{album_name}" AND artist:"{artist_name}"',
                    "fmt": "json",
                    "limit": 1
                }
                headers = {"User-Agent": "sptnr-web/1.0 (support@example.com)"}
                resp = requests.get(search_url, params=params, headers=headers, timeout=3)
                resp.raise_for_status()
                data = resp.json()
                rgs = data.get("release-groups", [])
                if rgs:
                    album_mbid = rgs[0].get("id")
            except Exception as e:
                logging.debug(f"MusicBrainz album search failed: {e}")
                return None
        
        if not album_mbid:
            return None
        
        # Fetch cover art from Cover Art Archive
        cover_url = f"https://coverartarchive.org/release-group/{album_mbid}/front-500"
        resp = requests.get(cover_url, timeout=3)
        if resp.status_code == 200:
            return resp.content
        
        return None
    except Exception as e:
        logging.debug(f"Failed to fetch album art from MusicBrainz: {e}")
        return None


def _fetch_album_art_from_discogs(artist_name: str, album_name: str) -> bytes | None:
    """
    Fetch album art from Discogs as fallback.
    
    Args:
        artist_name: Artist name
        album_name: Album name
        
    Returns:
        Image bytes if found, None otherwise
    """
    try:
        from api_clients.discogs import DiscogsClient
        discogs = DiscogsClient()
        album_data = discogs.search_album(artist_name, album_name)
        if album_data and album_data.get("cover_url"):
            resp = requests.get(album_data["cover_url"], timeout=3)
            if resp.status_code == 200:
                return resp.content
    except Exception as e:
        logging.debug(f"Failed to fetch album art from Discogs: {e}")
    
    return None


@app.route("/api/album-art/<path:artist>/<path:album>")
def api_album_art(artist, album):
    """Get album art from custom table, database, Navidrome, MusicBrainz, or Discogs"""
    try:
        from urllib.parse import unquote
        artist = unquote(artist)
        album = unquote(album)
        
        # 0. First, check custom album_art table
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT image_url FROM album_art 
                WHERE artist_name = ? AND album_name = ?
            """, (artist, album))
            result = cursor.fetchone()
            conn.close()
            
            if result and result[0]:
                custom_url = result[0]
                try:
                    resp = requests.get(custom_url, timeout=5)
                    if resp.status_code == 200:
                        return send_file(
                            io.BytesIO(resp.content),
                            mimetype='image/jpeg'
                        )
                except:
                    pass  # Fall through to other methods
        except:
            pass
        
        # 1. Check if we have cover_art_url in database
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT cover_art_url FROM tracks 
                WHERE artist = ? AND album = ? 
                AND cover_art_url IS NOT NULL 
                LIMIT 1
            """, (artist, album))
            result = cursor.fetchone()
            conn.close()
            
            if result and result[0]:
                cover_art_url = result[0]
                try:
                    resp = requests.get(cover_art_url, timeout=5)
                    if resp.status_code == 200:
                        return send_file(
                            io.BytesIO(resp.content),
                            mimetype='image/jpeg'
                        )
                except:
                    pass  # Fall through to other methods
        except:
            pass
        
        # 2. Try to get from Navidrome
        try:
            cfg, _ = _read_yaml(CONFIG_PATH)
            nav_users = cfg.get("navidrome_users", [])
            if not nav_users:
                nav = cfg.get("navidrome", {}) or {}
                if nav.get("base_url"):
                    nav_users = [nav]
            
            if nav_users:
                nav = nav_users[0]  # Use first Navidrome user
                base_url = nav.get("base_url", "").rstrip("/")
                username = nav.get("user", "")
                password = nav.get("pass", "")
                
                if base_url:
                    session = create_retry_session(retries=2, backoff=0.2, status_forcelist=(429, 500, 502, 503, 504))
                    search_url = f"{base_url}/rest/search3.view"
                    params = {
                        'u': username,
                        'p': password,
                        'c': 'sptnr',
                        'album': album,
                        'v': '1.12.0',
                        'f': 'json'
                    }
                    
                    resp = session.get(search_url, params=params, timeout=5)
                    if resp.status_code == 200:
                        data = resp.json()
                        albums = data.get('subsonic-response', {}).get('searchResult3', {}).get('album', [])
                        if albums:
                            album_id = albums[0].get('id')
                            if album_id:
                                # Get cover art
                                cover_url = f"{base_url}/rest/getCoverArt.view"
                                cover_params = {
                                    'u': username,
                                    'p': password,
                                    'c': 'sptnr',
                                    'id': album_id,
                                    'size': '300'
                                }
                                cover_resp = session.get(cover_url, params=cover_params, timeout=5)
                                if cover_resp.status_code == 200:
                                    return send_file(
                                        io.BytesIO(cover_resp.content),
                                        mimetype='image/jpeg'
                                    )
        except Exception as e:
            logging.debug(f"Navidrome cover art fetch failed: {e}")
        
        # 3. Try MusicBrainz
        art_bytes = _fetch_album_art_from_musicbrainz(artist, album)
        if art_bytes:
            return send_file(
                io.BytesIO(art_bytes),
                mimetype='image/jpeg'
            )
        
        # 4. Fallback to Discogs
        art_bytes = _fetch_album_art_from_discogs(artist, album)
        if art_bytes:
            return send_file(
                io.BytesIO(art_bytes),
                mimetype='image/jpeg'
            )
        
        # 5. No art found
        return Response(status=404)
    except Exception as e:
        logging.error(f"Error fetching album art for {artist} - {album}: {e}")
        return Response(status=404)


@app.route("/api/downloads/scan")
def api_downloads_scan():
    """Scan downloads folder and return pending files"""
    try:
        cfg, _ = _read_yaml(CONFIG_PATH)
        downloads_dir = cfg.get("downloads", {}).get("folder", os.environ.get("DOWNLOADS_DIR", "/downloads"))
        
        if not os.path.exists(downloads_dir):
            return jsonify({"error": "Downloads folder not found", "files": []})
        
        files = []
        for filename in os.listdir(downloads_dir):
            if not filename.lower().endswith('.mp3'):
                continue
            
            file_path = os.path.join(downloads_dir, filename)
            if not os.path.isfile(file_path):
                continue
            
            try:
                metadata = read_mp3_metadata(file_path)
                files.append({
                    'filename': filename,
                    'path': file_path,
                    'size': os.path.getsize(file_path),
                    'artist': metadata.get('artist', 'Unknown'),
                    'album': metadata.get('album', 'Unknown'),
                    'title': metadata.get('title', filename),
                    'year': metadata.get('year', metadata.get('date', '')),
                    'track': metadata.get('track', ''),
                    'genre': metadata.get('genre', ''),
                    'duration': metadata.get('duration', 0)
                })
            except Exception as e:
                files.append({
                    'filename': filename,
                    'path': file_path,
                    'size': os.path.getsize(file_path),
                    'error': str(e)
                })
        
        return jsonify({
            "count": len(files),
            "files": files
        })
    except Exception as e:
        return jsonify({"error": str(e), "files": []}), 400


@app.route("/api/downloads/process", methods=["POST"])
def api_downloads_process():
    """Process downloads folder - organize and move files to /Music"""
    try:
        from downloads_watcher import scan_downloads_folder
        
        results = scan_downloads_folder()
        
        successful = [r for r in results if r['status'] == 'success']
        failed = [r for r in results if r['status'] == 'error']
        
        return jsonify({
            "total": len(results),
            "successful": len(successful),
            "failed": len(failed),
            "results": results
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/downloads/process-one", methods=["POST"])
def api_downloads_process_one():
    """Process a single file from downloads folder"""
    try:
        data = request.get_json()
        file_path = data.get('path', '')
        
        if not file_path or not os.path.exists(file_path):
            return jsonify({"error": "File not found"}), 400
        
        from downloads_watcher import extract_mp3_metadata, organize_file, add_to_database
        
        # Extract metadata
        metadata = extract_mp3_metadata(file_path)
        
        # Organize file
        file_info = organize_file(file_path, metadata)
        
        if file_info.get('success'):
            # Add to database
            add_to_database(file_info, metadata)
            return jsonify({
                "success": True,
                "artist": file_info.get('artist'),
                "album": file_info.get('album'),
                "title": file_info.get('title'),
                "target_path": file_info.get('target_path')
            })
        else:
            return jsonify({
                "success": False,
                "error": file_info.get('error', 'Unknown error')
            }), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/lastfm/recommendations", methods=["GET"])
def api_lastfm_recommendations():
    """Get Last.fm recommendations"""
    try:
        cfg, _ = _read_yaml(CONFIG_PATH)
        lastfm_config = cfg.get("api_integrations", {}).get("lastfm", {})
        
        if not lastfm_config.get("enabled"):
            return jsonify({"error": "Last.fm not enabled"}), 400
        
        api_key = lastfm_config.get("api_key", "")
        if not api_key:
            return jsonify({"error": "Last.fm API key not configured"}), 400
        
        from api_clients.lastfm import get_lastfm_recommendations
        recommendations = get_lastfm_recommendations(api_key)
        
        return jsonify({"recommendations": recommendations})
    except Exception as e:
        logging.error(f"Last.fm recommendations error: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/downloads-manager")
def downloads_manager():
    """Downloads manager UI page"""
    cfg, _ = _read_yaml(CONFIG_PATH)
    
    # Get downloads folder from config, fall back to env var, then default
    downloads_dir = cfg.get("downloads", {}).get("folder", os.environ.get("DOWNLOADS_DIR", "/downloads"))
    
    return render_template("downloads_manager.html", 
                         downloads_dir=downloads_dir,
                         api_services=cfg.get('api_integrations', {}))


@app.route("/smart-playlists")
def smart_playlists():
    """Smart Playlist creation UI page"""
    return render_template("smart_playlists.html")


@app.route("/downloads-monitor")
def downloads_monitor():
    """Downloads monitoring UI page"""
    # Legacy route: redirect to unified downloads page (search + monitor)
    return redirect(url_for("downloads"))


@app.route("/api/qbittorrent/status", methods=["GET"])
def qbit_status():
    """Get qBittorrent download status"""
    cfg, _ = _read_yaml(CONFIG_PATH)
    qbit_config = cfg.get("qbittorrent", {})
    
    if not qbit_config.get("enabled"):
        return jsonify({"error": "qBittorrent integration not enabled"}), 400
    
    web_url = qbit_config.get("web_url", "http://localhost:8080")
    username = qbit_config.get("username", "")
    password = qbit_config.get("password", "")
    
    try:
        import requests as req
        
        # Login
        session = req.Session()
        login_url = f"{web_url}/api/v2/auth/login"
        login_resp = session.post(login_url, data={"username": username, "password": password}, timeout=10)
        
        if login_resp.text != "Ok.":
            return jsonify({"error": "Failed to login to qBittorrent"}), 500
        
        # Get torrents info
        torrents_url = f"{web_url}/api/v2/torrents/info"
        resp = session.get(torrents_url, timeout=10)
        
        if resp.status_code != 200:
            return jsonify({"error": f"Failed to get torrents: {resp.status_code}"}), 500
        
        torrents = resp.json()
        
        # Filter and format torrents - only show Music category
        active_torrents = []
        for torrent in torrents:
            # Only include torrents in Music category
            if torrent.get("category", "") == "Music":
                active_torrents.append({
                    "hash": torrent.get("hash", ""),
                    "name": torrent.get("name", ""),
                    "state": torrent.get("state", ""),
                    "progress": round(torrent.get("progress", 0) * 100, 2),
                    "dlspeed": torrent.get("dlspeed", 0),
                    "upspeed": torrent.get("upspeed", 0),
                    "downloaded": torrent.get("downloaded", 0),
                    "uploaded": torrent.get("uploaded", 0),
                    "size": torrent.get("size", 0),
                    "eta": torrent.get("eta", 0),
                    "num_seeds": torrent.get("num_seeds", 0),
                    "num_leechs": torrent.get("num_leechs", 0),
                    "category": torrent.get("category", ""),
                    "save_path": torrent.get("save_path", ""),
                    "added_on": torrent.get("added_on", 0)
                })
        
        # Sort by most recently added (added_on descending)
        active_torrents.sort(key=lambda x: x.get("added_on", 0), reverse=True)
        
        return jsonify({"torrents": active_torrents})
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/slskd/status", methods=["GET"])
def slskd_status():
    """Get slskd download status"""
    cfg, _ = _read_yaml(CONFIG_PATH)
    slskd_config = cfg.get("slskd", {})
    
    if not slskd_config.get("enabled"):
        return jsonify({"error": "slskd integration not enabled"}), 400
    
    web_url = slskd_config.get("web_url", "http://localhost:5030")
    api_key = slskd_config.get("api_key", "")
    
    try:
        import requests as req
        
        headers = {"X-API-Key": api_key} if api_key else {}
        
        # Get transfers
        transfers_url = f"{web_url}/api/v0/transfers/downloads"
        logging.debug(f"slskd_status: Fetching from {transfers_url}")
        resp = req.get(transfers_url, headers=headers, timeout=10)
        
        logging.debug(f"slskd_status: Response status {resp.status_code}")
        
        if resp.status_code != 200:
            logging.error(f"slskd_status: Failed to get transfers: {resp.status_code} - {resp.text[:500]}")
            return jsonify({"error": f"Failed to get transfers: {resp.status_code}"}), 500
        
        downloads_data = resp.json()
        logging.debug(f"slskd_status: Response is list: {isinstance(downloads_data, list)}, count: {len(downloads_data) if isinstance(downloads_data, list) else 'N/A'}")
        
        # Format downloads - slskd API returns array of UserResponse objects
        # Structure: [{ "username": "...", "directories": [{ "directory": "...", "files": [...] }] }]
        active_downloads = []
        
        if isinstance(downloads_data, list):
            # Correct format: array of user objects with nested directories and files
            for user_obj in downloads_data:
                if not isinstance(user_obj, dict):
                    continue
                
                username = user_obj.get("username", "Unknown")
                directories = user_obj.get("directories", [])
                
                if not isinstance(directories, list):
                    continue
                
                # Iterate through directories for this user
                for dir_obj in directories:
                    if not isinstance(dir_obj, dict):
                        continue
                    
                    files = dir_obj.get("files", [])
                    if not isinstance(files, list):
                        continue
                    
                    # Process each file
                    for file_obj in files:
                        if not isinstance(file_obj, dict):
                            continue
                        
                        filename = file_obj.get("filename", "Unknown")
                        size = int(file_obj.get("size", 0))
                        bytes_transferred = int(file_obj.get("bytesTransferred", 0))
                        percent_complete = int(file_obj.get("percentComplete", 0))
                        
                        # Normalize state
                        state_raw = file_obj.get("state", "")
                        state_lower = str(state_raw).lower()
                        
                        if "completed" in state_lower and "succeeded" in state_lower:
                            state = "Completed"
                        elif "completed" in state_lower and ("errored" in state_lower or "failed" in state_lower):
                            state = "Failed"
                        elif "completed" in state_lower and "cancelled" in state_lower:
                            state = "Cancelled"
                        elif "inprogress" in state_lower:
                            state = "Downloading"
                        elif "queued" in state_lower:
                            state = "Queued"
                        elif "initializing" in state_lower:
                            state = "Initializing"
                        else:
                            state = state_raw or "Unknown"
                        
                        average_speed = int(file_obj.get("averageSpeed", 0))
                        
                        logging.debug(f"slskd download: {username} -> {filename[:60]}, state={state}, progress={percent_complete}%, size={size}")
                        
                        active_downloads.append({
                            "username": username,
                            "filename": filename,
                            "state": state,
                            "progress": percent_complete,
                            "bytesTransferred": bytes_transferred,
                            "size": size,
                            "averageSpeed": average_speed,
                            "remoteToken": file_obj.get("id", ""),
                        })
        
        logging.info(f"slskd_status: Returning {len(active_downloads)} active downloads")
        return jsonify({"downloads": active_downloads})
        
    except Exception as e:
        logging.error(f"Error fetching slskd status: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
        
    except Exception as e:
        logging.error(f"Error fetching slskd status: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/smartplaylist/create", methods=["POST"])
def api_create_smart_playlist():
    """Create a new Smart Playlist (.nsp file)"""
    try:
        data = request.get_json()
        file_name = data.get('fileName', '').strip()
        playlist_data = data.get('playlist', {})
        
        if not file_name:
            return jsonify({"error": "File name is required"}), 400
        
        if not playlist_data.get('name'):
            return jsonify({"error": "Playlist name is required"}), 400
        
        # Sanitize file name
        file_name = ''.join(c for c in file_name if c.isalnum() or c in ('-', '_', ' '))
        if not file_name:
            return jsonify({"error": "Invalid file name"}), 400
        
        # Create playlists directory if it doesn't exist
        music_folder = os.environ.get("MUSIC_FOLDER", "/music")
        playlists_dir = os.path.join(music_folder, "Playlists")
        os.makedirs(playlists_dir, exist_ok=True)
        
        # Create file path
        file_path = os.path.join(playlists_dir, f"{file_name}.nsp")
        
        # Check if file already exists
        if os.path.exists(file_path):
            return jsonify({"error": f"Playlist file '{file_name}.nsp' already exists"}), 400
        
        # Write the playlist file
        try:
            with open(file_path, 'w') as f:
                json.dump(playlist_data, f, indent=2)
            
            return jsonify({
                "success": True,
                "message": f"Smart Playlist '{playlist_data.get('name')}' created successfully",
                "file_path": file_path,
                "file_name": f"{file_name}.nsp"
            }), 201
        
        except IOError as e:
            return jsonify({"error": f"Failed to write playlist file: {str(e)}"}), 500
    
    except Exception as e:
        logging.error(f"Error creating smart playlist: {str(e)}")
        return jsonify({"error": str(e)}), 500


# ============================================================================
# BEETS MUSIC TAGGING ROUTES
# ============================================================================

@app.route("/beets", methods=["GET"])
def beets_page():
    """Beets management page"""
    try:
        beets_client = _get_beets_client()
        status = beets_client.get_status()
        
        return render_template(
            "beets.html",
            status=status
        )
    except Exception as e:
        logging.error(f"Error loading beets page: {str(e)}")
        flash(f"Error loading beets page: {str(e)}", "danger")
        return redirect(url_for("dashboard"))


@app.route("/metadata-compare", methods=["GET"])
def metadata_compare():
    """Metadata comparison page - compare Navidrome vs Beets album data"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Get album mismatches (where Navidrome and Beets data differ)
        cursor.execute("""
            SELECT DISTINCT 
                album,
                artist,
                year,
                beets_year,
                navidrome_genres,
                musicbrainz_genres,
                COUNT(*) as track_count
            FROM tracks
            WHERE beets_album_mbid IS NOT NULL
            GROUP BY album, artist
            ORDER BY artist, album
        """)
        
        albums = cursor.fetchall()
        album_comparisons = []
        
        for album_row in albums:
            album = album_row[0]
            artist = album_row[1]
            nav_year = album_row[2]
            beets_year = album_row[3]
            nav_genres = album_row[4]
            beets_genres = album_row[5]
            track_count = album_row[6]
            
            # Check for mismatches
            has_mismatch = (
                (nav_year != beets_year) or
                (nav_genres != beets_genres)
            )
            
            if has_mismatch:
                album_comparisons.append({
                    "album": album,
                    "artist": artist,
                    "navidrome": {
                        "year": nav_year,
                        "genres": nav_genres.split(",") if nav_genres else []
                    },
                    "beets": {
                        "year": beets_year,
                        "genres": beets_genres.split(",") if beets_genres else []
                    },
                    "track_count": track_count
                })
        
        conn.close()
        
        return render_template(
            "metadata_compare.html",
            album_comparisons=album_comparisons
        )
    except Exception as e:
        logging.error(f"Error loading metadata comparison: {str(e)}")
        flash(f"Error loading metadata comparison: {str(e)}", "danger")
        return redirect(url_for("dashboard"))


@app.route("/api/metadata-compare/search-musicbrainz", methods=["POST"])
def search_musicbrainz_for_album():
    """Search MusicBrainz for album matches"""
    try:
        data = request.json or {}
        artist = data.get("artist", "")
        album = data.get("album", "")
        
        if not artist or not album:
            return jsonify({"error": "Artist and album name required"}), 400
        
        # Import MusicBrainz client
        from api_clients.musicbrainz import MusicBrainzClient
        mb_client = MusicBrainzClient()
        
        # Search for releases
        results = mb_client.search_releases(artist, album)
        
        return jsonify({
            "success": True,
            "results": results
        })
    except Exception as e:
        logging.error(f"MusicBrainz search error: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/metadata-compare/accept-navidrome", methods=["POST"])
def accept_navidrome_data():
    """Accept Navidrome data and lock it from being overwritten by Beets"""
    try:
        data = request.json or {}
        album = data.get("album", "")
        artist = data.get("artist", "")
        
        if not artist or not album:
            return jsonify({"error": "Artist and album name required"}), 400
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Mark all tracks in this album as locked from Beets overwrites
        cursor.execute("""
            UPDATE tracks 
            SET metadata_locked = 1
            WHERE artist = ? AND album = ?
        """, (artist, album))
        
        conn.commit()
        conn.close()
        
        return jsonify({
            "success": True,
            "message": f"Navidrome data locked for {artist} - {album}"
        })
    except Exception as e:
        logging.error(f"Error accepting Navidrome data: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/metadata-compare/apply-musicbrainz", methods=["POST"])
def apply_musicbrainz_data():
    """Apply MusicBrainz data to an album"""
    try:
        data = request.json or {}
        album = data.get("album", "")
        artist = data.get("artist", "")
        mb_data = data.get("mb_data", {})
        
        if not artist or not album or not mb_data:
            return jsonify({"error": "Artist, album, and MusicBrainz data required"}), 400
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Update tracks with MusicBrainz data
        cursor.execute("""
            UPDATE tracks 
            SET 
                year = ?,
                musicbrainz_genres = ?
            WHERE artist = ? AND album = ?
        """, (
            mb_data.get("year"),
            ",".join(mb_data.get("genres", [])),
            artist,
            album
        ))
        
        conn.commit()
        conn.close()
        
        return jsonify({
            "success": True,
            "message": f"Applied MusicBrainz data to {artist} - {album}"
        })
    except Exception as e:
        logging.error(f"Error applying MusicBrainz data: {str(e)}")
        return jsonify({"error": str(e)}), 500



def beets_status():
    """Get beets status and library info"""
    try:
        beets_client = _get_beets_client()
        status = beets_client.get_status()
        stats = beets_client.get_library_stats() if status.get("installed") else {}
        
        return jsonify({
            "status": status,
            "stats": stats
        })
    except Exception as e:
        logging.error(f"Error getting beets status: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/beets/import", methods=["POST"])
def beets_import_route():
    """Start a beets import operation"""
    try:
        data = request.json or {}
        source_path = data.get("source_path", "")
        move = data.get("move", True)
        
        if not source_path:
            return jsonify({"error": "source_path is required"}), 400
        
        if not os.path.exists(source_path):
            return jsonify({"error": f"Source path does not exist: {source_path}"}), 400
        
        beets_client = _get_beets_client()
        
        if not beets_client.enabled or not beets_client.is_installed():
            return jsonify({"error": "Beets is not installed or enabled"}), 400
        
        # Run import in background
        def run_import():
            logging.info(f"Starting beets import from {source_path}")
            result = beets_client.import_music(source_path, move=move)
            logging.info(f"Beets import result: {result}")
        
        import_thread = threading.Thread(target=run_import, daemon=True)
        import_thread.start()
        
        return jsonify({
            "success": True,
            "message": "Beets import started in background",
            "source_path": source_path
        }), 202
        
    except Exception as e:
        logging.error(f"Error starting beets import: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/beets/configure", methods=["POST"])
def beets_configure():
    """Configure beets settings"""
    try:
        beets_client = _get_beets_client()
        
        # Create default config if it doesn't exist
        success = beets_client.create_default_config()
        
        if success:
            return jsonify({
                "success": True,
                "message": "Beets configuration created"
            })
        else:
            return jsonify({
                "success": False,
                "error": "Failed to create beets configuration"
            }), 500
            
    except Exception as e:
        logging.error(f"Error configuring beets: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/beets/auto-import", methods=["POST"])
def beets_auto_import():
    """
    Run beets auto-import on entire library with metadata capture.
    Uses 'beet import -A' to auto-tag and stores MusicBrainz recommendations.
    """
    try:
        data = request.json or {}
        artist_path = data.get("artist_path")  # Optional: import specific artist only
        
        beets_client = _get_beets_client()
        
        if not beets_client.is_installed():
            return jsonify({"error": "Beets is not installed"}), 400
        
        # Run auto-import in background thread
        def run_auto_import():
            logging.info(f"Starting beets auto-import{' for ' + artist_path if artist_path else ' (full library)'}")
            skip_existing = data.get("skip_existing", False)
            result = beets_client.auto_import_library(artist_path=artist_path, skip_existing=skip_existing)
            logging.info(f"Beets auto-import result: {result}")
        
        import_thread = threading.Thread(target=run_auto_import, daemon=True)
        import_thread.start()
        
        return jsonify({
            "success": True,
            "message": "Beets auto-import started in background",
            "artist_path": artist_path or "Full library"
        }), 202
        
    except Exception as e:
        logging.error(f"Error starting beets auto-import: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/beets/sync-metadata", methods=["POST"])
def beets_sync_metadata():
    """Sync metadata from beets database to sptnr database."""
    try:
        beets_client = _get_beets_client()
        
        result = beets_client.sync_beets_metadata()
        
        if result.get("success"):
            return jsonify(result)
        else:
            return jsonify(result), 500
            
    except Exception as e:
        logging.error(f"Error syncing beets metadata: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/beets/track/<track_id>/recommendations", methods=["GET"])
def beets_track_recommendations(track_id):
    """Get beets/MusicBrainz recommendations for a specific track."""
    try:
        beets_client = _get_beets_client()
        
        result = beets_client.get_beets_recommendations(track_id=track_id)
        
        return jsonify(result)
        
    except Exception as e:
        logging.error(f"Error getting beets recommendations: {str(e)}")
        return jsonify({"error": str(e)}), 500


# ============================================================================
# SPOTIFY PLAYLIST IMPORT ROUTES
# ============================================================================

@app.route("/playlist/import")
def playlist_import():
    """Page to import Spotify playlists"""
    # Check if Spotify is configured
    config_data, _ = _read_yaml(CONFIG_PATH)
    spotify_enabled = config_data.get("api_integrations", {}).get("spotify", {}).get("enabled", False)
    slskd_enabled = config_data.get("slskd", {}).get("enabled", False)
    
    return render_template("playlist_importer.html", 
                         spotify_enabled=spotify_enabled,
                         slskd_enabled=slskd_enabled)


@app.route("/api/spotify/playlists", methods=["GET"])
def api_spotify_playlists():
    """API endpoint to list user's Spotify playlists"""
    try:
        config_data, _ = _read_yaml(CONFIG_PATH)
        spotify_config = config_data.get("api_integrations", {}).get("spotify", {})
        
        if not spotify_config.get("enabled"):
            return jsonify({"error": "Spotify not enabled"}), 400
        
        # Get Spotify credentials
        client_id = spotify_config.get("client_id", "")
        client_secret = spotify_config.get("client_secret", "")
        
        if not client_id or not client_secret:
            return jsonify({"error": "Spotify credentials not configured"}), 400
        
        try:
            from api_clients.spotify import get_spotify_user_playlists
            playlists = get_spotify_user_playlists(client_id, client_secret)
            return jsonify({"playlists": playlists})
        except Exception as e:
            logging.error(f"Failed to fetch Spotify playlists: {e}")
            return jsonify({"error": str(e)}), 500
    except Exception as e:
        logging.error(f"Playlist list error: {str(e)}")
        return jsonify({"error": "Failed to fetch playlists"}), 500


@app.route("/api/playlist/import", methods=["POST"])
def api_playlist_import():
    """API endpoint to import a Spotify playlist and match to Navidrome database"""
    try:
        data = request.get_json()
        spotify_url = data.get("spotify_url", "").strip()
        playlist_name = data.get("playlist_name", "").strip()
        playlist_description = data.get("playlist_description", "").strip()
        
        if not spotify_url or not playlist_name:
            return jsonify({"error": "Missing required fields"}), 400
        
        # Extract playlist ID from various URL formats
        playlist_id = extract_spotify_playlist_id(spotify_url)
        if not playlist_id:
            return jsonify({"error": "Invalid Spotify playlist URL or ID"}), 400
        
        # Get Spotify client and fetch playlist tracks
        try:
            spotify_tracks = get_spotify_playlist_tracks(playlist_id)
        except Exception as e:
            return jsonify({"error": f"Failed to fetch Spotify playlist: {str(e)}"}), 500
        
        if not spotify_tracks:
            return jsonify({"error": "Playlist is empty or could not be fetched"}), 400
        
        # Match tracks to Navidrome database with normalization + fuzzy scoring
        matched_tracks = []
        missing_tracks = []
        
        conn = get_db()
        cursor = conn.cursor()

        def _normalize(text: str) -> str:
            """Lowercase, strip accents, drop version/feat tags, collapse whitespace."""
            if not text:
                return ""
            # Strip accents
            text = unicodedata.normalize("NFKD", text)
            text = "".join(c for c in text if not unicodedata.combining(c))
            text = text.lower()
            # Remove bracketed suffixes (versions, mixes, remasters)
            text = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", text)
            # Remove common suffix words
            text = re.sub(r"(?i)\b(remaster(?:ed)?\s*\d{0,4}|remaster|deluxe|live|mono|stereo|edit|mix|version|bonus track)\b", " ", text)
            # Drop 'feat/ft'
            text = re.sub(r"(?i)\b(feat\.?|ft\.?)\b", " ", text)
            # Keep alnum only
            text = re.sub(r"[^a-z0-9]+", " ", text)
            return " ".join(text.split())

        def _similarity(a: str, b: str) -> float:
            if not a or not b:
                return 0.0
            return difflib.SequenceMatcher(None, a, b).ratio()

        def _find_best_match(track: dict) -> tuple[dict | None, float]:
            raw_title = (track.get("title") or "").strip()
            raw_artist = (track.get("artist") or "").strip()
            raw_album = (track.get("album") or "").strip()

            primary_artist = raw_artist.split(",")[0].strip()

            norm_title = _normalize(raw_title)
            norm_artist = _normalize(primary_artist)
            norm_album = _normalize(raw_album)

            if not norm_title or not norm_artist:
                return None, 0.0

            # Pull a candidate set using both raw and normalized tokens
            title_like = f"%{raw_title.lower()}%" if raw_title else "%"
            artist_like = f"%{primary_artist.lower()}%" if primary_artist else "%"

            cursor.execute("""
                SELECT id, title, artist, album, stars
                FROM tracks
                WHERE LOWER(title) LIKE ? OR LOWER(artist) LIKE ?
                LIMIT 250
            """, (title_like, artist_like))
            candidates = cursor.fetchall() or []

            # If no candidates, widen by first token of normalized title
            if not candidates and norm_title:
                seed = norm_title.split(" ")[0]
                cursor.execute("""
                    SELECT id, title, artist, album, stars
                    FROM tracks
                    WHERE LOWER(title) LIKE ?
                    LIMIT 250
                """, (f"%{seed}%",))
                candidates = cursor.fetchall() or []

            best_row = None
            best_score = 0.0

            for row in candidates:
                cand_title = _normalize(row["title"])
                cand_artist = _normalize(row["artist"])
                cand_album = _normalize(row["album"])

                title_score = _similarity(norm_title, cand_title)
                artist_score = _similarity(norm_artist, cand_artist)
                album_score = _similarity(norm_album, cand_album) if norm_album and cand_album else 0.0

                combined = (0.6 * title_score) + (0.35 * artist_score) + (0.05 * album_score)

                if combined > best_score:
                    best_score = combined
                    best_row = row

            # Accept only reasonably confident matches
            if best_row and best_score >= 0.72:
                return best_row, round(best_score, 3)
            return None, round(best_score, 3)

        for spotify_track in spotify_tracks:
            match_row, confidence = _find_best_match(spotify_track)
            if match_row:
                matched_tracks.append({
                    "id": match_row["id"],
                    "title": match_row["title"],
                    "artist": match_row["artist"],
                    "album": match_row["album"],
                    "stars": match_row["stars"],
                    "confidence": confidence
                })
            else:
                missing_tracks.append({
                    "title": spotify_track.get("title", ""),
                    "artist": spotify_track.get("artist", ""),
                    "album": spotify_track.get("album", ""),
                    "best_score": confidence
                })
        
        conn.close()
        
        # Check if slskd is enabled
        config_data, _ = _read_yaml(CONFIG_PATH)
        slskd_enabled = config_data.get("slskd", {}).get("enabled", False)
        
        return jsonify({
            "success": True,
            "playlist_name": playlist_name,
            "playlist_description": playlist_description,
            "matched_tracks": matched_tracks,
            "missing_tracks": missing_tracks,
            "slskd_enabled": slskd_enabled,
            "spotify_playlist_id": playlist_id,
            "message": f"Matched {len(matched_tracks)}/{len(spotify_tracks)} tracks"
        })
    
    except Exception as e:
        logging.error(f"Playlist import error: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/playlist/create", methods=["POST"])
def api_playlist_create():
    """API endpoint to create a Navidrome playlist from matched tracks"""
    try:
        data = request.get_json()
        playlist_name = data.get("playlist_name", "").strip()
        playlist_description = data.get("playlist_description", "").strip()
        matched_tracks = data.get("matched_tracks", [])
        
        if not playlist_name or not matched_tracks:
            return jsonify({"error": "Missing required fields"}), 400
        
        # Get track IDs from matched tracks
        track_ids = [track.get("id") for track in matched_tracks if track.get("id")]
        
        if not track_ids:
            return jsonify({"error": "No valid tracks to add to playlist"}), 400
        
        # Create NSP playlist file
        playlist_data = {
            "name": playlist_name,
            "comment": playlist_description or "Imported from Spotify",
            "all": []
        }
        
        # Add track IDs as a list
        playlist_data["trackIds"] = track_ids
        
        # Create playlists directory if it doesn't exist
        music_folder = os.environ.get("MUSIC_FOLDER", "/music")
        playlists_dir = os.path.join(music_folder, "Playlists")
        os.makedirs(playlists_dir, exist_ok=True)
        
        # Sanitize playlist name for filename
        file_name = "".join(c for c in playlist_name if c.isalnum() or c in ('-', '_', ' '))
        if not file_name:
            return jsonify({"error": "Invalid playlist name"}), 400
        
        file_path = os.path.join(playlists_dir, f"{file_name}.nsp")
        
        # Check if file already exists
        if os.path.exists(file_path):
            return jsonify({"error": f"Playlist file '{file_name}.nsp' already exists"}), 400
        
        # Write the playlist file
        try:
            with open(file_path, 'w') as f:
                json.dump(playlist_data, f, indent=2)
            
            logging.info(f"Created playlist: {playlist_name} with {len(track_ids)} tracks")
            
            return jsonify({
                "success": True,
                "message": f"Playlist '{playlist_name}' created successfully",
                "file_path": file_path,
                "file_name": f"{file_name}.nsp",
                "track_count": len(track_ids)
            }), 201
        
        except IOError as e:
            return jsonify({"error": f"Failed to write playlist file: {str(e)}"}), 500
    
    except Exception as e:
        logging.error(f"Playlist creation error: {str(e)}")
        return jsonify({"error": str(e)}), 500


def extract_spotify_playlist_id(url_or_id):
    """Extract Spotify playlist ID from URL or return the ID if already in correct format"""
    import re
    
    # If it's just an ID (32 characters of alphanumeric)
    if re.match(r"^[a-zA-Z0-9]{22}$", url_or_id):
        return url_or_id
    
    # Extract from various URL formats
    patterns = [
        r"spotify\.com/playlist/([a-zA-Z0-9]+)",  # https://open.spotify.com/playlist/...
        r"spotify:playlist:([a-zA-Z0-9]+)",       # spotify:playlist:...
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url_or_id)
        if match:
            return match.group(1)
    
    return None


def get_spotify_playlist_tracks(playlist_id):
    """Fetch tracks from a Spotify playlist"""
    try:
        from start import spotify_client
        if not spotify_client:
            raise Exception("Spotify client not configured")
        
        # Use SpotifyClient's get_playlist_tracks method
        tracks = spotify_client.get_playlist_tracks(playlist_id)
        return tracks
    
    except Exception as e:
        logging.error(f"Error fetching Spotify playlist: {str(e)}")
        raise


if __name__ == "__main__":
    # Auto-start scanner if configured for batchrate and perpetual mode
    try:
        cfg, _ = _read_yaml(CONFIG_PATH)
        features = cfg.get('features', {})
        
        print(f"Checking auto-start configuration...")
        print(f"  batchrate: {features.get('batchrate')}")
        print(f"  perpetual: {features.get('perpetual')}")
        
        if features.get('batchrate') and features.get('perpetual'):
            # Start the beets auto-import and scanner in background thread
            def start_scanner():
                import time as time_module
                time_module.sleep(2)  # Give Flask time to start
                try:
                    print("Auto-starting beets import and scanner with batchrate and perpetual mode...")
                    logger = logging.getLogger('sptnr')
                    logger.info("Auto-starting beets import and scanner with batchrate and perpetual mode...")
                    
                    # First, run beets auto-import to capture file paths and metadata
                    print("Step 1: Running beets auto-import...")
                    logger.info("Step 1: Running beets auto-import...")
                    from beets_auto_import import BeetsAutoImporter
                    importer = BeetsAutoImporter()
                    importer.import_and_capture(skip_existing=True)
                    
                    # Then run the standard scanner
                    print("Step 2: Running Navidrome sync and rating scan...")
                    logger.info("Step 2: Running Navidrome sync and rating scan...")
                    from start import run_scan
                    run_scan(scan_type='batchrate')
                except Exception as e:
                    import traceback
                    print(f"Error starting auto-import/scanner: {e}")
                    print(traceback.format_exc())
                    logger = logging.getLogger('sptnr')
                    logger.error(f"Error starting auto-import/scanner: {e}")
                    logger.error(traceback.format_exc())
            
            scanner_thread = threading.Thread(target=start_scanner, daemon=True)
            scanner_thread.start()
            print("Scanner thread started in background")
        else:
            print("Auto-start not enabled (both batchrate and perpetual must be true)")
    except Exception as e:
        import traceback
        print(f"Error in auto-start configuration: {e}")
        print(traceback.format_exc())
    
    # API endpoints for metadata lookups
    @app.route("/api/track/discogs", methods=["POST"])
    def api_track_discogs_lookup():
        """Lookup track on Discogs for better metadata and genres"""
        try:
            from api_clients.discogs import DiscogsClient
            import requests
            
            data = request.get_json()
            title = data.get("title", "")
            artist = data.get("artist", "")
            album = data.get("album", "")
            
            if not title or not artist:
                return jsonify({"error": "Missing title or artist"}), 400
            
            # Get Discogs token from config
            cfg, _ = _read_yaml(CONFIG_PATH)
            # Check both api_integrations.discogs and root discogs for backwards compatibility
            discogs_config = cfg.get("api_integrations", {}).get("discogs", {}) or cfg.get("discogs", {})
            token = discogs_config.get("token", "")
            
            if not token:
                return jsonify({"error": "Discogs token not configured. Please add your Discogs token in config.yaml under api_integrations.discogs.token"}), 400
            
            # Search Discogs API directly
            headers = {
                "Authorization": f"Discogs token={token}",
                "User-Agent": "Sptnr/1.0"
            }
            query = f"{artist} {album or title}"
            
            response = requests.get(
                "https://api.discogs.com/database/search",
                params={"q": query, "type": "release", "per_page": 5},
                headers=headers,
                timeout=10
            )
            
            if not response.ok:
                return jsonify({"error": f"Discogs API error: {response.status_code}"}), 500
            
            results_data = response.json().get("results", [])
            
            if not results_data:
                return jsonify({"results": [], "message": "No Discogs matches found"}), 200
            
            # Format results
            formatted_results = []
            for result in results_data[:5]:
                # Check if format includes "Single" to detect singles
                formats = result.get("format", [])
                is_single_release = "Single" in formats if formats else False
                
                formatted_results.append({
                    "title": result.get("title", "Unknown"),
                    "year": result.get("year", ""),
                    "genre": result.get("genre", []),
                    "style": result.get("style", []),
                    "format": formats,
                    "is_single": is_single_release,
                    "url": result.get("resource_url", ""),
                    "source": "discogs",
                    "discogs_id": result.get("id", "")
                })
            
            return jsonify({"results": formatted_results}), 200
        except Exception as e:
            logger = logging.getLogger('sptnr')
            logger.error(f"Discogs lookup error: {e}")
            return jsonify({"error": str(e)}), 500
            logger.error(f"Discogs lookup error: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/album/musicbrainz", methods=["POST"])
    def api_album_musicbrainz_lookup():
        """Lookup album on MusicBrainz for multiple matches (Picard-style) with retry logic"""
        import time
        logger = logging.getLogger('sptnr')
        try:
            data = request.get_json()
            album = data.get("album", "")
            artist = data.get("artist", "")
            
            if not album or not artist:
                return jsonify({"error": "Missing album or artist"}), 400
            
            # Search MusicBrainz for release groups with retry
            query = f'release:"{album}" AND artist:"{artist}"'
            headers = {"User-Agent": "sptnr-web/1.0 (support@example.com)"}
            
            max_retries = 3
            retry_delay = 1.0
            last_error = None
            
            for attempt in range(max_retries):
                try:
                    resp = requests.get(
                        "https://musicbrainz.org/ws/2/release-group",
                        params={"query": query, "fmt": "json", "limit": 10},
                        headers=headers,
                        timeout=5
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    release_groups = data.get("release-groups", []) or []
                    break  # Success, exit retry loop
                except (requests.Timeout, requests.exceptions.SSLError, requests.exceptions.ConnectionError) as e:
                    last_error = e
                    if attempt < max_retries - 1:
                        logger.warning(f"MusicBrainz album lookup attempt {attempt + 1} failed: {e}, retrying...")
                        time.sleep(retry_delay)
                        retry_delay *= 2  # Exponential backoff
                    else:
                        logger.error(f"MusicBrainz album lookup failed after {max_retries} retries: {e}")
                        return jsonify({
                            "error": f"MusicBrainz connection failed after {max_retries} retries. Try Discogs instead.",
                            "results": []
                        }), 503
                except requests.exceptions.RequestException as e:
                    logger.error(f"MusicBrainz request error: {e}")
                    return jsonify({"error": str(e), "results": []}), 500
            else:
                # Fell through without break - should not happen
                release_groups = []
            
            if not release_groups:
                return jsonify({"results": [], "message": "No MusicBrainz album matches found"}), 200
            
            # Format results with similarity scores
            import difflib
            results = []
            for rg in release_groups:
                rg_id = rg.get("id", "")
                rg_title = rg.get("title", "")
                primary_type = rg.get("primary-type", "Album")
                first_release = rg.get("first-release-date", "")
                
                # Get artist credit
                artist_credit = rg.get("artist-credit", [])
                rg_artist = artist_credit[0].get("name", "") if artist_credit else ""
                
                # Calculate similarity scores
                title_similarity = difflib.SequenceMatcher(None, album.lower(), rg_title.lower()).ratio()
                artist_similarity = difflib.SequenceMatcher(None, artist.lower(), rg_artist.lower()).ratio()
                overall_confidence = (title_similarity * 0.7 + artist_similarity * 0.3)
                
                # Get cover art URL
                cover_art_url = f"https://coverartarchive.org/release-group/{rg_id}/front-250" if rg_id else ""
                
                results.append({
                    "mbid": rg_id,
                    "title": rg_title,
                    "artist": rg_artist,
                    "primary_type": primary_type,
                    "first_release_date": first_release,
                    "cover_art_url": cover_art_url,
                    "confidence": round(overall_confidence, 3),
                    "title_similarity": round(title_similarity, 3),
                    "artist_similarity": round(artist_similarity, 3),
                    "source": "musicbrainz"
                })
            
            # Sort by confidence
            results.sort(key=lambda x: x["confidence"], reverse=True)
            
            return jsonify({"results": results[:10]}), 200
                
        except Exception as e:
            logger = logging.getLogger('sptnr')
            logger.error(f"MusicBrainz album lookup error: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/album/discogs", methods=["POST"])
    def api_album_discogs_lookup():
        """Lookup album on Discogs for better metadata and genres"""
        try:
            from singledetection import _discogs_search, _get_discogs_session
            import difflib
            
            data = request.get_json()
            album = data.get("album", "")
            artist = data.get("artist", "")
            
            if not album or not artist:
                return jsonify({"error": "Missing album or artist"}), 400
            
            # Get Discogs config for token
            config_data, _ = _read_yaml(CONFIG_PATH)
            discogs_config = config_data.get("api_integrations", {}).get("discogs", {})
            discogs_token = discogs_config.get("token", "")
            
            # Search Discogs with multiple query strategies
            session = _get_discogs_session()
            headers = {"User-Agent": "Sptnr/1.0"}
            if discogs_token:
                headers["Authorization"] = f"Discogs token={discogs_token}"
            
            # Try different query formats to improve match rate
            queries = [
                f"{artist} {album}",  # Full query
                f'artist:"{artist}" release:"{album}"',  # Structured query
                f'{artist} "{album}"',  # Quoted album
            ]
            
            results = []
            for query in queries:
                logger = logging.getLogger('sptnr')
                logger.debug(f"Discogs search attempt: {query}")
                results = _discogs_search(session, headers, query, kind="release", per_page=10)
                if results:
                    logger.debug(f"Discogs search found {len(results)} results")
                    break
                logger.debug(f"Discogs search with query '{query}' returned no results")
            
            if not results:
                return jsonify({"results": [], "message": "No Discogs album matches found"}), 200
            
            # Format results with similarity scoring
            formatted_results = []
            for result in results[:10]:
                result_title = result.get("title", "Unknown")
                # Calculate similarity to improve ordering
                title_sim = difflib.SequenceMatcher(None, album.lower(), result_title.lower()).ratio()
                artist_part = result_title.split("-")[0] if "-" in result_title else result_title
                artist_sim = difflib.SequenceMatcher(None, artist.lower(), artist_part.lower()).ratio()
                overall_conf = (title_sim * 0.7 + artist_sim * 0.3)
                
                formatted_results.append({
                    "title": result_title,
                    "year": result.get("year", ""),
                    "genre": result.get("genre", []),
                    "style": result.get("style", []),
                    "format": result.get("format", []),
                    "url": result.get("resource_url", ""),
                    "discogs_id": result.get("id", ""),
                    "confidence": round(overall_conf, 3),
                    "source": "discogs"
                })
            
            # Sort by confidence
            formatted_results.sort(key=lambda x: x["confidence"], reverse=True)
            
            return jsonify({"results": formatted_results}), 200
        except Exception as e:
            logger = logging.getLogger('sptnr')
            logger.error(f"Discogs lookup error: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/album/apply-mbid", methods=["POST"])
    def api_album_apply_mbid():
        """Apply MusicBrainz ID and cover art to all tracks in an album"""
        try:
            data = request.get_json()
            artist = data.get("artist", "")
            album = data.get("album", "")
            mbid = data.get("mbid", "")
            cover_art_url = data.get("cover_art_url", "")
            
            if not artist or not album:
                return jsonify({"error": "Missing artist or album"}), 400
            
            conn = get_db()
            cursor = conn.cursor()
            
            # Update all tracks in this album with MBID and cover art
            updates = []
            if mbid:
                updates.append("mbid = ?")
                updates.append("beets_album_mbid = ?")  # Also store as beets album MBID for display
            if cover_art_url:
                updates.append("cover_art_url = ?")
            
            if not updates:
                return jsonify({"error": "No data to update"}), 400
            
            query = f"UPDATE tracks SET {', '.join(updates)} WHERE artist = ? AND album = ?"
            params = []
            if mbid:
                params.append(mbid)
                params.append(mbid)  # Same ID for both fields
            if cover_art_url:
                params.append(cover_art_url)
            params.extend([artist, album])
            
            cursor.execute(query, params)
            rows_updated = cursor.rowcount
            conn.commit()
            conn.close()
            
            logging.info(f"Applied MBID {mbid} to {rows_updated} tracks in {artist} - {album}")
            
            return jsonify({
                "success": True,
                "message": f"Updated {rows_updated} tracks with MBID and cover art",
                "rows_updated": rows_updated
            }), 200
        except Exception as e:
            logger = logging.getLogger('sptnr')
            logger.error(f"Apply MBID error: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/album/apply-discogs-id", methods=["POST"])
    def api_album_apply_discogs_id():
        """Apply Discogs ID to all tracks in an album"""
        import time
        logger = logging.getLogger('sptnr')
        try:
            data = request.get_json()
            artist = data.get("artist", "")
            album = data.get("album", "")
            discogs_id = data.get("discogs_id", "")
            is_single = data.get("is_single", False)  # Check if Discogs marked this as Single
            
            if not artist or not album or not discogs_id:
                return jsonify({"error": "Missing required fields"}), 400
            
            # Retry logic for database lock
            max_retries = 3
            retry_delay = 0.5
            rows_updated = 0
            last_error = None
            
            for attempt in range(max_retries):
                try:
                    conn = get_db()
                    conn.isolation_level = None  # Autocommit mode
                    cursor = conn.cursor()
                    
                    # Update all tracks in this album with Discogs ID and is_single flag if detected
                    if is_single:
                        # If Discogs detected this as a Single, mark tracks as singles with high confidence
                        cursor.execute(
                            "UPDATE tracks SET discogs_album_id = ?, is_single = 1, single_confidence = 'high', single_sources = CASE WHEN single_sources IS NULL THEN 'discogs' ELSE single_sources || ',discogs' END WHERE artist = ? AND album = ?",
                            (discogs_id, artist, album)
                        )
                    else:
                        # Just update the Discogs ID
                        cursor.execute(
                            "UPDATE tracks SET discogs_album_id = ? WHERE artist = ? AND album = ?",
                            (discogs_id, artist, album)
                        )
                    
                    rows_updated = cursor.rowcount
                    conn.commit()
                    conn.close()
                    
                    if is_single:
                        logger.info(f"Updated {rows_updated} tracks with Discogs ID {discogs_id} and marked as single for {artist} - {album}")
                    else:
                        logger.info(f"Updated {rows_updated} tracks with Discogs ID {discogs_id} for {artist} - {album}")
                    
                    return jsonify({
                        "success": True,
                        "message": f"Updated {rows_updated} tracks with Discogs ID" + (" and marked as single" if is_single else ""),
                        "rows_updated": rows_updated
                    }), 200
                except sqlite3.OperationalError as e:
                    last_error = e
                    if "database is locked" in str(e):
                        if attempt < max_retries - 1:
                            logger.warning(f"Database locked on apply-discogs-id, retry {attempt + 1}/{max_retries}")
                            time.sleep(retry_delay)
                            retry_delay *= 2  # Exponential backoff
                        else:
                            logger.error(f"Database locked after {max_retries} retries: {e}")
                    else:
                        raise
                except Exception as inner_e:
                    last_error = inner_e
                    raise
            
            # If we get here, all retries failed
            logger.error(f"Apply Discogs ID failed after {max_retries} retries: {last_error}")
            return jsonify({"error": f"Database locked. Please try again."}), 503
        except Exception as e:
            logger = logging.getLogger('sptnr')
            logger.error(f"Apply Discogs ID error: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/track/musicbrainz", methods=["POST"])
    def api_track_musicbrainz_lookup():
        """Lookup track on MusicBrainz for multiple matches (Picard-style) with retry logic"""
        try:
            data = request.get_json()
            title = data.get("title", "")
            artist = data.get("artist", "")
            
            if not title or not artist:
                return jsonify({"error": "Missing title or artist"}), 400
            
            # Search MusicBrainz for recordings with retry
            query = f'recording:"{title}" AND artist:"{artist}"'
            headers = {"User-Agent": "sptnr-web/1.0 (support@example.com)"}
            
            max_retries = 2
            for attempt in range(max_retries):
                try:
                    resp = requests.get(
                        "https://musicbrainz.org/ws/2/recording",
                        params={"query": query, "fmt": "json", "limit": 10, "inc": "releases+artist-credits"},
                        headers=headers,
                        timeout=5
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    recordings = data.get("recordings", []) or []
                    
                    if not recordings:
                        return jsonify({"results": [], "message": "No MusicBrainz track matches found"}), 200
                    
                    # Format results with similarity scores
                    import difflib
                    results = []
                    for rec in recordings:
                        rec_id = rec.get("id", "")
                        rec_title = rec.get("title", "")
                        rec_length = rec.get("length", 0)  # in milliseconds
                        
                        # Get artist credit
                        artist_credit = rec.get("artist-credit", [])
                        rec_artist = artist_credit[0].get("name", "") if artist_credit else ""
                        
                        # Get releases (albums this appears on)
                        releases = rec.get("releases", []) or []
                        release_list = []
                        for rel in releases[:5]:
                            release_list.append({
                                "id": rel.get("id", ""),
                                "title": rel.get("title", "")
                            })
                        
                        # Calculate similarity scores
                        title_similarity = difflib.SequenceMatcher(None, title.lower(), rec_title.lower()).ratio()
                        artist_similarity = difflib.SequenceMatcher(None, artist.lower(), rec_artist.lower()).ratio()
                        overall_confidence = (title_similarity * 0.7 + artist_similarity * 0.3)
                        
                        results.append({
                            "mbid": rec_id,
                            "title": rec_title,
                            "artist": rec_artist,
                            "length": rec_length,
                            "releases": release_list,
                            "confidence": round(overall_confidence, 3),
                            "title_similarity": round(title_similarity, 3),
                            "artist_similarity": round(artist_similarity, 3),
                            "source": "musicbrainz"
                        })
                    
                    # Sort by confidence
                    results.sort(key=lambda x: x["confidence"], reverse=True)
                    
                    return jsonify({"results": results[:10]}), 200
                    
                except requests.exceptions.Timeout:
                    logging.debug(f"MusicBrainz timeout (attempt {attempt+1}/{max_retries}) for {title} by {artist}")
                    if attempt < max_retries - 1:
                        time.sleep(1 * (2 ** attempt))  # Exponential backoff
                except requests.exceptions.ConnectionError as e:
                    logging.debug(f"MusicBrainz connection error (attempt {attempt+1}/{max_retries}): {type(e).__name__}")
                    if attempt < max_retries - 1:
                        time.sleep(1 * (2 ** attempt))
                except Exception as e:
                    logging.debug(f"MusicBrainz lookup error: {e}")
                    if attempt == max_retries - 1:
                        return jsonify({"error": f"MusicBrainz lookup failed: {str(e)}"}), 500
                    time.sleep(1)
            
            return jsonify({"error": "MusicBrainz lookup failed after retries"}), 500
                
        except Exception as e:
            logging.error(f"MusicBrainz track lookup error: {e}")
            return jsonify({"error": str(e)}), 500

    # ==========================================================================
    # PLAYLIST MANAGER ROUTES
    # ==========================================================================

    @app.route("/playlist-manager")
    def playlist_manager():
        """Playlist manager page with downloader and custom creator"""
        cfg, _ = _read_yaml(CONFIG_PATH)
        navidrome_config = cfg.get("navidrome", {})
        navidrome_users = cfg.get("navidrome_users", [])
        
        # If navidrome_users not configured, use single user
        if not navidrome_users and navidrome_config.get("user"):
            navidrome_users = [{
                "base_url": navidrome_config.get("base_url"),
                "user": navidrome_config.get("user")
            }]
        
        return render_template('playlist_manager.html', navidrome_users=navidrome_users)

    @app.route("/api/playlist/list")
    def api_playlist_list():
        """List all playlists in Navidrome"""
        try:
            cfg, _ = _read_yaml(CONFIG_PATH)
            navidrome_config = cfg.get("navidrome", {})
            base_url = navidrome_config.get("base_url", "http://localhost:4533")
            user = navidrome_config.get("user", "admin")
            password = navidrome_config.get("pass", "")
            
            import requests as req
            
            # Get user token
            auth_response = req.post(
                f"{base_url}/rest/authenticate.view",
                params={
                    "u": user,
                    "p": password,
                    "c": "sptnr",
                    "f": "json"
                },
                timeout=10
            )
            
            if auth_response.status_code != 200:
                return jsonify({"error": "Failed to authenticate with Navidrome"}), 500
            
            auth_data = auth_response.json()
            if not auth_data.get("subsonic-response", {}).get("token"):
                return jsonify({"error": "Invalid Navidrome credentials"}), 500
            
            token = auth_data["subsonic-response"]["token"]
            
            # Get playlists
            playlists_response = req.get(
                f"{base_url}/rest/getPlaylists.view",
                params={"u": user, "t": token, "s": "salt", "c": "sptnr", "f": "json"},
                timeout=10
            )
            
            if playlists_response.status_code != 200:
                return jsonify({"error": "Failed to get playlists"}), 500
            
            playlists_data = playlists_response.json()
            playlists = []
            
            for playlist in playlists_data.get("subsonic-response", {}).get("playlists", {}).get("playlist", []):
                playlists.append({
                    "id": playlist.get("id"),
                    "name": playlist.get("name"),
                    "path": playlist.get("id")
                })
            
            return jsonify({"playlists": playlists}), 200
        except Exception as e:
            logging.error(f"Error listing playlists: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500

    @app.route("/api/playlist/load", methods=["POST"])
    def api_playlist_load():
        """Load playlist tracks"""
        try:
            data = request.get_json()
            playlist_id = data.get("playlist_path")
            
            if not playlist_id:
                return jsonify({"error": "Missing playlist_path"}), 400
            
            cfg, _ = _read_yaml(CONFIG_PATH)
            navidrome_config = cfg.get("navidrome", {})
            base_url = navidrome_config.get("base_url", "http://localhost:4533")
            user = navidrome_config.get("user", "admin")
            password = navidrome_config.get("pass", "")
            
            import requests as req
            
            # Authenticate
            auth_response = req.post(
                f"{base_url}/rest/authenticate.view",
                params={"u": user, "p": password, "c": "sptnr", "f": "json"},
                timeout=10
            )
            token = auth_response.json()["subsonic-response"]["token"]
            
            # Get playlist tracks
            playlist_response = req.get(
                f"{base_url}/rest/getPlaylist.view",
                params={"u": user, "t": token, "s": "salt", "c": "sptnr", "f": "json", "id": playlist_id},
                timeout=10
            )
            
            playlist_data = playlist_response.json().get("subsonic-response", {}).get("playlist", {})
            tracks = playlist_data.get("entry", [])
            
            if not isinstance(tracks, list):
                tracks = [tracks] if tracks else []
            
            songs = []
            matched_files = []
            
            for track in tracks:
                song = {
                    "id": track.get("id"),
                    "title": track.get("title", "Unknown"),
                    "artist": track.get("artist", "Unknown"),
                    "album": track.get("album", "Unknown"),
                    "detected": True
                }
                songs.append(song)
                matched_files.append({
                    "id": track.get("id"),
                    "title": song["title"],
                    "artist": song["artist"],
                    "filename": track.get("path", "")
                })
            
            return jsonify({
                "playlist_path": playlist_id,
                "songs": songs,
                "matched_files": matched_files,
                "total": len(songs),
                "matched": len(matched_files)
            }), 200
        except Exception as e:
            logging.error(f"Error loading playlist: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500

    @app.route("/api/playlist/search-songs", methods=["POST"])
    def api_playlist_search_songs():
        """Search for songs in library"""
        try:
            data = request.get_json() or {}
            raw_query = (data.get("query") or "").strip()
            artist = (data.get("artist") or "").strip()
            title = (data.get("title") or "").strip()
            album = (data.get("album") or "").strip()

            # Build a combined query string so Navidrome search remains happy
            search_terms = [t for t in [title, artist, album, raw_query] if t]
            query = " ".join(search_terms).strip()
            
            if not query or len(query) < 2:
                return jsonify({"error": "Query too short"}), 400
            
            cfg, _ = _read_yaml(CONFIG_PATH)
            nav_users = cfg.get("navidrome_users") or []
            if not nav_users:
                nav = cfg.get("navidrome", {}) or {}
                if nav.get("base_url"):
                    nav_users = [nav]

            base_url = None
            user = None
            password = None
            if nav_users:
                nd = nav_users[0]
                base_url = nd.get("base_url") or nd.get("url") or "http://localhost:4533"
                user = nd.get("user") or nd.get("username") or "admin"
                password = nd.get("pass") or nd.get("password") or ""
            else:
                base_url = "http://localhost:4533"
                user = "admin"
                password = ""

            import requests as req

            # Build token auth if password available
            params = {
                "u": user,
                "c": "sptnr",
                "f": "json",
                "v": "1.16.0",
                "query": query,
                "songCount": 50,
            }

            if password:
                salt = secrets.token_hex(8)
                token = hashlib.md5((password + salt).encode()).hexdigest()
                params.update({"t": token, "s": salt})
            else:
                params["p"] = ""  # empty password to satisfy API

            results = []

            try:
                search_response = req.get(
                    f"{base_url.rstrip('/')}/rest/search3.view",
                    params=params,
                    timeout=10,
                )

                response_data = search_response.json()
                if response_data.get("subsonic-response", {}).get("status") == "ok":
                    search_data = response_data.get("subsonic-response", {}).get("searchResult3", {})
                    songs = search_data.get("song", [])
                    if not isinstance(songs, list):
                        songs = [songs] if songs else []
                    for song in songs[:50]:
                        results.append({
                            "id": song.get("id"),
                            "title": song.get("title", "Unknown"),
                            "artist": song.get("artist", "Unknown"),
                            "album": song.get("album", "Unknown"),
                            "duration": song.get("duration", 0)
                        })
            except Exception as nav_err:
                logging.debug(f"Navidrome search failed, will fallback to local DB: {nav_err}")

            # Fallback: search local sptnr DB if Navidrome returned nothing
            if not results:
                try:
                    conn = get_db()
                    cursor = conn.cursor()

                    where_clauses = []
                    params = []

                    if title:
                        where_clauses.append("LOWER(title) LIKE ?")
                        params.append(f"%{title.lower()}%")
                    if artist:
                        where_clauses.append("LOWER(artist) LIKE ?")
                        params.append(f"%{artist.lower()}%")
                    if album:
                        where_clauses.append("LOWER(album) LIKE ?")
                        params.append(f"%{album.lower()}%")

                    if not where_clauses:
                        pattern = f"%{query.lower()}%"
                        where_clauses.append("(LOWER(title) LIKE ? OR LOWER(artist) LIKE ? OR LOWER(album) LIKE ?)")
                        params.extend([pattern, pattern, pattern])

                    where_sql = " AND ".join(where_clauses)

                    cursor.execute(
                        f"""
                        SELECT id, title, artist, album, duration
                        FROM tracks
                        WHERE {where_sql}
                        ORDER BY stars DESC NULLS LAST, title COLLATE NOCASE
                        LIMIT 50
                        """,
                        tuple(params),
                    )
                    for row in cursor.fetchall() or []:
                        results.append({
                            "id": row[0],
                            "title": row[1],
                            "artist": row[2],
                            "album": row[3],
                            "duration": row[4] or 0,
                        })
                    conn.close()
                except Exception as db_err:
                    logging.error(f"Local DB search failed: {db_err}")

            return jsonify({"songs": results}), 200
        except Exception as e:
            logging.error(f"Error searching songs: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500

    @app.route("/api/playlist/create-custom", methods=["POST"])
    def api_playlist_create_custom():
        """Create a custom playlist in Navidrome"""
        try:
            data = request.get_json()
            name = data.get("name", "").strip()
            description = data.get("description", "").strip()
            user_name = data.get("user", "admin")
            is_public = data.get("is_public", False)
            songs = data.get("songs", [])
            
            if not name:
                return jsonify({"error": "Playlist name is required"}), 400
            
            if not songs:
                return jsonify({"error": "Add at least one song"}), 400
            
            cfg, _ = _read_yaml(CONFIG_PATH)
            navidrome_config = cfg.get("navidrome", {})
            base_url = navidrome_config.get("base_url", "http://localhost:4533")
            user = navidrome_config.get("user", "admin")
            password = navidrome_config.get("pass", "")
            
            import requests as req
            
            # Authenticate
            auth_response = req.post(
                f"{base_url}/rest/authenticate.view",
                params={"u": user, "p": password, "c": "sptnr", "f": "json"},
                timeout=10
            )
            token = auth_response.json()["subsonic-response"]["token"]
            
            # Create playlist
            create_response = req.post(
                f"{base_url}/rest/createPlaylist.view",
                params={
                    "u": user,
                    "t": token,
                    "s": "salt",
                    "c": "sptnr",
                    "f": "json",
                    "name": name,
                    "comment": description,
                    "public": "true" if is_public else "false"
                },
                timeout=10
            )
            
            create_data = create_response.json()
            playlist_id = create_data.get("subsonic-response", {}).get("playlist", {}).get("id")
            
            if not playlist_id:
                return jsonify({"error": "Failed to create playlist"}), 500
            
            # Add songs to playlist
            for song in songs:
                req.post(
                    f"{base_url}/rest/updatePlaylist.view",
                    params={
                        "u": user,
                        "t": token,
                        "s": "salt",
                        "c": "sptnr",
                        "f": "json",
                        "playlistId": playlist_id,
                        "songIdToAdd": song.get("id")
                    },
                    timeout=10
                )
            
            logging.info(f"Created playlist '{name}' with {len(songs)} songs")
            return jsonify({
                "success": True,
                "playlist_id": playlist_id,
                "message": f"Playlist created with {len(songs)} songs"
            }), 201
        except Exception as e:
            logging.error(f"Error creating custom playlist: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500

    app.run(debug=False, host="0.0.0.0", port=5000)
