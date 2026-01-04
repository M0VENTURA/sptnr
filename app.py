#!/usr/bin/env python3
"""
Sptnr Web UI - Flask application for managing music ratings and scans
"""

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response, send_file, session
import sqlite3
from contextlib import closing
import yaml
import os
import sys
import subprocess
import threading
import time
import logging
from datetime import datetime
import copy
import json
import io
from functools import wraps
from check_db import update_schema
from metadata_reader import read_mp3_metadata, find_track_file, aggregate_genres_from_tracks, get_track_metadata_from_db
from start import create_retry_session, rate_artist, build_artist_index
from scan_helpers import scan_artist_to_db
from popularity import scan_popularity as run_popularity_scan
from api_clients.slskd import SlskdClient
from beets_integration import _get_beets_client

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

# Paths
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/config.yaml")
DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")
LOG_PATH = os.environ.get("LOG_PATH", "/config/app.log")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(APP_DIR, "config", "config.yaml")

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
scan_lock = threading.Lock()


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
    """Get database connection with WAL mode for better concurrency"""
    global _schema_updated
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    
    # Ensure schema is up-to-date (only once per session)
    if not _schema_updated:
        try:
            update_schema(DB_PATH)
            _schema_updated = True
        except Exception as e:
            print(f"⚠️ Database schema update warning: {e}")
    
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


@app.route("/setup", methods=["GET", "POST"])
def setup():
    cfg, _ = _read_yaml(CONFIG_PATH)
    baseline = copy.deepcopy(_baseline_config())

    if request.method == "POST":
        # Get lists of user credentials (arrays from form)
        nav_base_urls = request.form.getlist("nav_base_url[]")
        nav_users = request.form.getlist("nav_user[]")
        nav_passes = request.form.getlist("nav_pass[]")
        
        spotify_client_id = request.form.get("spotify_client_id", "").strip()
        spotify_client_secret = request.form.get("spotify_client_secret", "").strip()
        discogs_token = request.form.get("discogs_token", "").strip()
        lastfm_api_key = request.form.get("lastfm_api_key", "").strip()

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
        
        return render_template("dashboard.html",
                             artist_count=artist_count,
                             album_count=album_count,
                             track_count=track_count,
                             five_star_count=five_star_count,
                             singles_count=singles_count,
                             recent_scans=recent_scans,
                             scan_running=scan_running)
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
            MAX(last_scanned) as last_updated
        FROM tracks
        GROUP BY artist
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
            ORDER BY album_year DESC, album COLLATE NOCASE
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
                    MAX(beets_album_mbid) as beets_album_mbid
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
                    NULL as beets_album_mbid
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
        
        # Group tracks by disc number
        tracks_by_disc = {}
        for track in tracks_data:
            try:
                # Handle both dict and Row objects
                track_dict = dict(track) if hasattr(track, 'keys') else track
                disc_num = track_dict.get('disc_number') if isinstance(track_dict, dict) else (track['disc_number'] if hasattr(track, '__getitem__') else 1)
                disc_num = disc_num or 1
                
                if disc_num not in tracks_by_disc:
                    tracks_by_disc[disc_num] = []
                tracks_by_disc[disc_num].append(track_dict)
            except Exception as e:
                logging.debug(f"Error processing track for disc grouping: {e}")
                # Fallback to disc 1
                if 1 not in tracks_by_disc:
                    tracks_by_disc[1] = []
                tracks_by_disc[1].append(dict(track) if hasattr(track, 'keys') else track)
        
        conn.close()
        
        return render_template("album.html",
                             artist_name=artist,
                             album_name=album,
                             tracks=tracks_data,
                             tracks_by_disc=tracks_by_disc,
                             album_data=album_data,
                             album_genres=sorted(list(album_genres)))
    except Exception as e:
        import traceback
        logging.error(f"Error loading album {artist}/{album}: {e}")
        logging.error(traceback.format_exc())
        return render_template("album.html",
                             artist_name=artist,
                             album_name=album,
                             tracks=[],
                             tracks_by_disc={},
                             album_data=None,
                             album_genres=[],
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
            run_popularity_scan(verbose=True, artist=artist_name)

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
            run_popularity_scan(verbose=True, artist=artist_name)

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
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM tracks WHERE id = ?", (track_id,))
    track = cursor.fetchone()
    
    # Get recommended genres from other tracks with similar titles or artists
    recommended_genres = []
    if track:
        artist_name = track['artist']
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
    
    if not track:
        flash("Track not found", "error")
        return redirect(url_for("dashboard"))
    
    return render_template("track.html", track=track, recommended_genres=recommended_genres, track_id=track_id)


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
                    flash("Beets auto-import is already running", "warning")
                    return redirect(url_for("dashboard"))
            elif hasattr(scan_process_mp3, 'is_alive') and scan_process_mp3.is_alive():
                flash("Beets auto-import is already running", "warning")
                return redirect(url_for("dashboard"))
        
        try:
            db_dir = os.path.dirname(DB_PATH)
            mp3_progress_file = os.path.join(db_dir, "mp3_scan_progress.json")
            _write_progress_file(mp3_progress_file, "mp3_scan", True, {"status": "starting"})
            
            # Run beets import in background thread instead of subprocess
            def run_beets_scan_bg():
                try:
                    from beets_auto_import import BeetsAutoImporter
                    logging.info("Starting Beets auto-import scan in background")
                    importer = BeetsAutoImporter()
                    importer.import_music()
                    _write_progress_file(mp3_progress_file, "mp3_scan", False, {"status": "complete", "exit_code": 0})
                    logging.info("Beets scan completed successfully")
                except Exception as e:
                    logging.error(f"Error in Beets scan: {e}", exc_info=True)
                    _write_progress_file(mp3_progress_file, "mp3_scan", False, {"status": "error", "error": str(e), "exit_code": 1})
            
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


@app.route("/scan/navidrome", methods=["POST"])
def scan_navidrome():
    """Run Navidrome library scan in background thread"""
    global scan_process_navidrome
    
    with scan_lock:
        # Check if scan is already running
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
            
            # Run scan in background thread instead of subprocess to avoid initialization overhead
            def run_navidrome_scan_bg():
                try:
                    from start import run_scan
                    logging.info("Starting Navidrome batch rating scan in background")
                    run_scan(scan_type='batchrate', verbose=False, force=False, dry_run=False)
                    _write_progress_file(nav_progress_file, "navidrome_scan", False, {"status": "complete", "exit_code": 0})
                    logging.info("Navidrome scan completed successfully")
                except Exception as e:
                    logging.error(f"Error in Navidrome scan: {e}", exc_info=True)
                    _write_progress_file(nav_progress_file, "navidrome_scan", False, {"status": "error", "error": str(e), "exit_code": 1})
            
            scan_thread = threading.Thread(target=run_navidrome_scan_bg, daemon=False)
            scan_thread.start()
            
            # Store thread reference for tracking
            scan_process_navidrome = {'thread': scan_thread, 'type': 'navidrome'}
            
            flash("✅ Navidrome sync scan started", "success")
            logging.info("Navidrome scan thread started successfully")
        except Exception as e:
            logging.error(f"Error starting Navidrome scan: {e}", exc_info=True)
            flash(f"❌ Error starting Navidrome scan: {str(e)}", "danger")
    
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
        
        try:
            db_dir = os.path.dirname(DB_PATH)
            popularity_progress_file = os.path.join(db_dir, "popularity_scan_progress.json")
            _write_progress_file(popularity_progress_file, "popularity_scan", True, {"status": "starting"})

            # Run popularity scan in background thread instead of subprocess
            def run_popularity_scan_bg():
                try:
                    logging.info("Starting popularity score scan in background")
                    run_popularity_scan(verbose=False)
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
    """View all bookmarks"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
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
        
        return render_template("bookmarks.html", bookmarks=bookmarks_data)
    except Exception as e:
        logging.error(f"Error loading bookmarks: {e}")
        return render_template("bookmarks.html", bookmarks=[], error=str(e))


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


@app.route("/config/edit", methods=["POST"])
def config_edit():
    """Save config.yaml"""
    config_content = request.form.get("config_content", "")

    try:
        yaml.safe_load(config_content)
        cfg_dir = os.path.dirname(CONFIG_PATH)
        if cfg_dir:
            os.makedirs(cfg_dir, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(config_content)
        flash("Configuration saved successfully", "success")
    except yaml.YAMLError as e:
        flash(f"Invalid YAML: {e}", "danger")
    except Exception as e:
        flash(f"Error saving config: {e}", "danger")

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
            'navidrome': data.get('navidrome', {}),
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
    global scan_process, scan_process_mp3, scan_process_navidrome, scan_process_popularity, scan_process_singles
    
    with scan_lock:
        return jsonify({
            "main_scan": {
                "name": "Main Rating Scan",
                "running": scan_process is not None and scan_process.poll() is None
            },
            "mp3_scan": {
                "name": "File Path Scan",
                "running": scan_process_mp3 is not None and scan_process_mp3.poll() is None
            },
            "navidrome_scan": {
                "name": "Navidrome Sync",
                "running": scan_process_navidrome is not None and scan_process_navidrome.poll() is None
            },
            "popularity_scan": {
                "name": "Popularity Update",
                "running": scan_process_popularity is not None and scan_process_popularity.poll() is None
            },
            "singles_scan": {
                "name": "Single Detection",
                "running": scan_process_singles is not None and scan_process_singles.poll() is None
            }
        })


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
        "navidrome": os.path.join(os.path.dirname(CONFIG_PATH), "sptnr.log"),
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
        
        # Return search ID immediately for client-side polling
        return jsonify({
            "searchId": search_id,
            "status": "searching"
        })
        
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
        
        # Flatten file results from all responses
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
        
        # Log all results
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


@app.route("/api/album-art/<path:artist>/<path:album>")
def api_album_art(artist, album):
    """Get album art from Navidrome or MP3 files"""
    try:
        # First try to get from Navidrome
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
                try:
                    session = create_retry_session(retries=3, backoff=0.3, status_forcelist=(429, 500, 502, 503, 504))
                    # Try to get album cover via Navidrome REST API
                    # Format: /rest/getCoverArt.view?u=user&p=pass&c=client&id=album_id
                    
                    # First, search for the album
                    search_url = f"{base_url}/rest/search3.view"
                    params = {
                        'u': username,
                        'p': password,
                        'c': 'sptnr',
                        'album': album,
                        'v': '1.12.0',
                        'f': 'json'
                    }
                    
                    resp = session.get(search_url, params=params, timeout=10)
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
                                cover_resp = session.get(cover_url, params=cover_params, timeout=10)
                                if cover_resp.status_code == 200:
                                    return send_file(
                                        io.BytesIO(cover_resp.content),
                                        mimetype='image/jpeg'
                                    )
                except Exception as e:
                    pass  # Fall through to MP3 extraction
        
        # Try to extract from MP3 files
        music_root = os.environ.get("MUSIC_ROOT", "/music")
        try:
            file_path = find_track_file(artist, album, "", music_root, timeout_seconds=3)
        except:
            file_path = None
        
        if file_path and os.path.exists(file_path):
            try:
                from mutagen.id3 import ID3
                audio = ID3(file_path)
                # APIC frame contains album art
                for frame in audio.values():
                    if frame.FrameID == 'APIC':
                        return send_file(
                            io.BytesIO(frame.data),
                            mimetype=frame.mime
                        )
            except:
                pass
        
        # Default placeholder if no art found
        return send_file(
            io.BytesIO(b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82'),
            mimetype='image/png'
        )
    except Exception as e:
        return {"error": str(e)}, 400


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
        logging.debug(f"slskd_status: Raw response type: {type(downloads_data)}, content preview: {str(downloads_data)[:200]}")
        
        # Format downloads - handle multiple possible slskd API formats
        active_downloads = []
        
        def extract_file(file_obj, username="Unknown"):
            """Extract file info from various possible slskd response formats"""
            if not isinstance(file_obj, dict):
                return None
            
            filename = file_obj.get("filename") or file_obj.get("name") or "Unknown"
            state = file_obj.get("state") or file_obj.get("status") or ""
            bytes_transferred = file_obj.get("bytesTransferred") or file_obj.get("bytesReceived") or 0
            size = file_obj.get("size") or 0
            progress = (bytes_transferred / size * 100) if size > 0 else 0
            
            logging.debug(f"extract_file: {username} -> {filename[:50]}, state={state}, progress={progress:.1f}%")
            
            return {
                "username": username,
                "filename": filename,
                "state": state,
                "progress": round(progress, 2),
                "bytesTransferred": bytes_transferred,
                "size": size,
                "averageSpeed": file_obj.get("averageSpeed") or 0,
                "remoteToken": file_obj.get("remoteToken") or ""
            }
        
        if isinstance(downloads_data, dict):
            # Format 1: {username: {folderId: {files: []}}}
            for username, folders_or_files in downloads_data.items():
                logging.debug(f"Processing username: {username}, type: {type(folders_or_files)}")
                if isinstance(folders_or_files, dict):
                    for key, value in folders_or_files.items():
                        logging.debug(f"  Processing key: {key}, type: {type(value)}")
                        # Check if this is a folder structure or direct file
                        if isinstance(value, dict) and "files" in value:
                            # Folder structure: extract files
                            files_list = value.get("files", [])
                            logging.debug(f"    Found folder with {len(files_list)} files")
                            for file_obj in files_list:
                                extracted = extract_file(file_obj, username)
                                if extracted:
                                    active_downloads.append(extracted)
                        elif isinstance(value, dict):
                            # Try direct extraction (might be a file object)
                            logging.debug(f"    Trying direct extraction of dict")
                            extracted = extract_file(value, username)
                            if extracted:
                                active_downloads.append(extracted)
                        elif isinstance(value, list):
                            # Format 2: {username: [files]}
                            logging.debug(f"    Found list with {len(value)} items")
                            for file_obj in value:
                                extracted = extract_file(file_obj, username)
                                if extracted:
                                    active_downloads.append(extracted)
        elif isinstance(downloads_data, list):
            # Format 3: [files] with username field
            logging.debug(f"Processing list with {len(downloads_data)} items")
            for file_obj in downloads_data:
                username = file_obj.get("username", "Unknown")
                extracted = extract_file(file_obj, username)
                if extracted:
                    active_downloads.append(extracted)
        
        logging.info(f"slskd_status: Returning {len(active_downloads)} active downloads")
        return jsonify({"downloads": active_downloads})
        
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


@app.route("/api/beets/status", methods=["GET"])
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
            result = beets_client.auto_import_library(artist_path=artist_path)
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
        
        # Match tracks to Navidrome database
        matched_tracks = []
        missing_tracks = []
        
        conn = get_db()
        cursor = conn.cursor()
        
        for spotify_track in spotify_tracks:
            title = spotify_track.get("title", "").lower().strip()
            artist = spotify_track.get("artist", "").lower().strip()
            
            if not title or not artist:
                continue
            
            # Try to find exact match first
            cursor.execute("""
                SELECT id, title, artist, album, stars FROM tracks
                WHERE LOWER(title) = ? AND LOWER(artist) = ?
                LIMIT 1
            """, (title, artist))
            
            result = cursor.fetchone()
            
            if result:
                matched_tracks.append({
                    "id": result[0],
                    "title": result[1],
                    "artist": result[2],
                    "album": result[3],
                    "stars": result[4]
                })
            else:
                # Try fuzzy match - require BOTH title AND artist to partially match
                cursor.execute("""
                    SELECT id, title, artist, album, stars FROM tracks
                    WHERE LOWER(title) LIKE ? AND LOWER(artist) LIKE ?
                    ORDER BY stars DESC
                    LIMIT 1
                """, (f"%{title}%", f"%{artist}%"))
                
                result = cursor.fetchone()
                if result:
                    matched_tracks.append({
                        "id": result[0],
                        "title": result[1],
                        "artist": result[2],
                        "album": result[3],
                        "stars": result[4]
                    })
                else:
                    missing_tracks.append({
                        "title": spotify_track.get("title", ""),
                        "artist": spotify_track.get("artist", ""),
                        "album": spotify_track.get("album", "")
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
                    importer.import_and_capture()
                    
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
    @app.route("/api/track/musicbrainz", methods=["POST"])
    def api_track_musicbrainz_lookup():
        """Lookup track on MusicBrainz for better metadata"""
        try:
            from api_clients.musicbrainz import MusicBrainzClient
            
            data = request.get_json()
            title = data.get("title", "")
            artist = data.get("artist", "")
            
            if not title or not artist:
                return jsonify({"error": "Missing title or artist"}), 400
            
            # Get suggested MBID using the client
            mb_client = MusicBrainzClient(enabled=True)
            mbid, confidence = mb_client.get_suggested_mbid(title, artist, limit=5)
            
            if not mbid:
                return jsonify({"results": [], "message": "No MusicBrainz matches found"}), 200
            
            # Return MBID and confidence
            return jsonify({
                "results": [{
                    "mbid": mbid,
                    "confidence": confidence,
                    "source": "musicbrainz"
                }]
            }), 200
        except Exception as e:
            logger = logging.getLogger('sptnr')
            logger.error(f"MusicBrainz lookup error: {e}")
            return jsonify({"error": str(e)}), 500

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
                formatted_results.append({
                    "title": result.get("title", "Unknown"),
                    "year": result.get("year", ""),
                    "genre": result.get("genre", []),
                    "style": result.get("style", []),
                    "format": result.get("format", []),
                    "url": result.get("resource_url", ""),
                    "source": "discogs"
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
        """Lookup album on MusicBrainz for better metadata"""
        try:
            from api_clients.musicbrainz import MusicBrainzClient
            
            data = request.get_json()
            album = data.get("album", "")
            artist = data.get("artist", "")
            
            if not album or not artist:
                return jsonify({"error": "Missing album or artist"}), 400
            
            # Get suggested MBID using the client
            mb_client = MusicBrainzClient(enabled=True)
            mbid, confidence = mb_client.get_suggested_mbid(album, artist, limit=5)
            
            if not mbid:
                return jsonify({"results": [], "message": "No MusicBrainz album matches found"}), 200
            
            return jsonify({
                "results": [{
                    "mbid": mbid,
                    "confidence": confidence,
                    "source": "musicbrainz"
                }]
            }), 200
        except Exception as e:
            logger = logging.getLogger('sptnr')
            logger.error(f"MusicBrainz album lookup error: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/album/discogs", methods=["POST"])
    def api_album_discogs_lookup():
        """Lookup album on Discogs for better metadata and genres"""
        try:
            from singledetection import _discogs_search, _get_discogs_session
            
            data = request.get_json()
            album = data.get("album", "")
            artist = data.get("artist", "")
            
            if not album or not artist:
                return jsonify({"error": "Missing album or artist"}), 400
            
            # Search Discogs
            session = _get_discogs_session()
            headers = {"User-Agent": "Sptnr/1.0"}
            query = f"{artist} {album}"
            
            results = _discogs_search(session, headers, query, kind="release", per_page=5)
            
            if not results:
                return jsonify({"results": [], "message": "No Discogs album matches found"}), 200
            
            # Format results
            formatted_results = []
            for result in results[:5]:
                formatted_results.append({
                    "title": result.get("title", "Unknown"),
                    "year": result.get("year", ""),
                    "genre": result.get("genre", []),
                    "style": result.get("style", []),
                    "format": result.get("format", []),
                    "url": result.get("resource_url", ""),
                    "source": "discogs"
                })
            
            return jsonify({"results": formatted_results}), 200
        except Exception as e:
            logger = logging.getLogger('sptnr')
            logger.error(f"Discogs lookup error: {e}")
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
            data = request.get_json()
            query = data.get("query", "").strip()
            
            if not query or len(query) < 2:
                return jsonify({"error": "Query too short"}), 400
            
            cfg, _ = _read_yaml(CONFIG_PATH)
            navidrome_config = cfg.get("navidrome", {})
            base_url = navidrome_config.get("base_url", "http://localhost:4533")
            user = navidrome_config.get("user", "admin")
            password = navidrome_config.get("pass", "")
            
            import requests as req
            
            # Search using password directly (simpler than token auth for search)
            search_response = req.get(
                f"{base_url}/rest/search3.view",
                params={
                    "u": user, 
                    "p": password, 
                    "c": "sptnr", 
                    "f": "json",
                    "v": "1.16.0",
                    "query": query,
                    "songCount": 50
                },
                timeout=10
            )
            
            response_data = search_response.json()
            if response_data.get("subsonic-response", {}).get("status") != "ok":
                error_msg = response_data.get("subsonic-response", {}).get("error", {}).get("message", "Unknown error")
                return jsonify({"error": f"Navidrome API error: {error_msg}"}), 500
            
            search_data = response_data.get("subsonic-response", {}).get("searchResult3", {})
            songs = search_data.get("song", [])
            
            if not isinstance(songs, list):
                songs = [songs] if songs else []
            
            results = []
            for song in songs[:50]:
                results.append({
                    "id": song.get("id"),
                    "title": song.get("title", "Unknown"),
                    "artist": song.get("artist", "Unknown"),
                    "album": song.get("album", "Unknown"),
                    "duration": song.get("duration", 0)
                })
            
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
