# --- Unified Log API ---
# Place all Flask route definitions after app = Flask(__name__)

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
from contextlib import closing
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response, send_file, session
from datetime import datetime
from functools import wraps
from scan_helpers import scan_artist_to_db
from start import rate_artist
from popularity import popularity_scan
from start import build_artist_index
from metadata_reader import aggregate_genres_from_tracks
from check_db import update_schema
from start import save_to_db
from scan_helpers import scan_artist_to_db
from beets_integration import _get_beets_client
from api_clients.slskd import SlskdClient
from metadata_reader import get_track_metadata_from_db, find_track_file, read_mp3_metadata
from helpers import create_retry_session


# Unified logging setup
LOG_PATH = os.environ.get("LOG_PATH", "/config/sptnr.log")
VERBOSE = (
    os.environ.get("SPTNR_VERBOSE_APP") or os.environ.get("SPTNR_VERBOSE") or "0"
) == "1"
SERVICE_PREFIX = "WebUI_"

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

def log_basic(msg):
    if VERBOSE:
        logging.info(msg)

def log_verbose(msg):
    if VERBOSE:
        logging.info(f"[VERBOSE] {msg}")

app = Flask(__name__)

# --- Unified Log API ---
@app.route("/api/unified-log")
def api_unified_log():
    lines = int(request.args.get("lines", 40))
    verbose = request.args.get("verbose", "0") == "1"
    unified_log_path = "/config/unified_scan.log"
    log_lines = []
    try:
        log_verbose(f"[api_unified_log] Reading {lines} lines from {unified_log_path}")
        if not os.path.exists(unified_log_path):
            log_verbose(f"[api_unified_log] Log file not found: {unified_log_path}")
            return jsonify({"error": f"Unified log file not found at {unified_log_path}", "lines": []}), 404
        with open(unified_log_path, "r", encoding="utf-8", errors="ignore") as f:
            log_lines = f.readlines()
    except Exception as e:
        log_verbose(f"[api_unified_log] Exception reading file: {e}")
        return jsonify({"error": str(e), "lines": []}), 500
    try:
        # Filter out HTTP request/response logs unless verbose is enabled
        if not verbose:
            import re
            http_log_pattern = re.compile(r'"(GET|POST|PUT|DELETE|PATCH) /api/.* HTTP/1\\.[01]" (200|201|204|400|401|403|404|500|502|503)')
            log_lines = [line for line in log_lines if not http_log_pattern.search(line)]
        # Only return the last N lines
        log_lines = log_lines[-lines:]
        log_verbose(f"[api_unified_log] Returning {len(log_lines)} log lines")
        return jsonify({"lines": [line.rstrip('\n') for line in log_lines]})
    except Exception as e:
        log_verbose(f"[api_unified_log] Exception processing log lines: {e}")
        return jsonify({"error": str(e), "lines": []}), 500
if VERBOSE:
    logging.basicConfig(level=logging.WARNING, handlers=[file_handler, stream_handler])
else:
    logging.basicConfig(level=logging.ERROR, handlers=[file_handler, stream_handler])

# --- Navidrome Playlists API ---
@app.route("/api/navidrome/playlists", methods=["GET"])
def api_navidrome_playlists():
    """Return all Navidrome playlists (id, name, type) grouped by type for dropdowns."""
    try:
        config_data, _ = _read_yaml(CONFIG_PATH)
        current_user = session.get("username")
        navidrome_users = config_data.get("navidrome_users", [])
        nav_cfg = None

        if navidrome_users and current_user:
            # Find the config for the logged-in user
            nav_cfg = next((u for u in navidrome_users if u.get("user") == current_user), None)
        if not nav_cfg:
            # Fallback to single-user config
            nav_cfg = config_data.get("navidrome", {})

        base_url = nav_cfg.get("base_url")
        username = nav_cfg.get("user")
        password = nav_cfg.get("pass")
        if not (base_url and username and password):
            logging.error(f"Navidrome not configured: base_url={base_url}, username={username}, password={'set' if password else 'unset'}")
            return jsonify({"error": "Navidrome not configured. Please check your config file and credentials."}), 400
        from api_clients.navidrome import NavidromeClient
        client = NavidromeClient(base_url, username, password)
        playlists = client.fetch_all_playlists()
        if playlists is None:
            logging.error("NavidromeClient returned None for playlists.")
            return jsonify({"error": "Failed to fetch playlists from Navidrome. See logs for details."}), 500
        if not playlists:
            logging.warning("No playlists returned from Navidrome. Check if any exist for the configured user.")
            return jsonify({"error": "No playlists found in Navidrome for the configured user."}), 200
        result = {"smart": [], "regular": []}
        for pl in playlists:
            entry = {"id": pl.get("id"), "name": pl.get("name")}
            if pl.get("type") == "smart":
                result["smart"].append(entry)
            else:
                result["regular"].append(entry)
        return jsonify(result)
    except Exception as e:
        logging.error(f"Failed to fetch Navidrome playlists: {e}", exc_info=True)
        return jsonify({"error": f"Exception occurred: {str(e)}"}), 500

@app.route("/api/navidrome/playlist/<playlist_id>", methods=["GET"])
def api_navidrome_playlist_detail(playlist_id):
    """Return full details for a single Navidrome playlist by ID."""
    try:
        config_data, _ = _read_yaml(CONFIG_PATH)
        nav_cfg = config_data.get("api_integrations", {}).get("navidrome", {})
        base_url = nav_cfg.get("base_url") or config_data.get("nav_base_url")
        username = nav_cfg.get("username") or config_data.get("nav_user")
        password = nav_cfg.get("password") or config_data.get("nav_pass")
        if not (base_url and username and password):
            logging.error(f"Navidrome not configured: base_url={base_url}, username={username}, password={'set' if password else 'unset'}")
            return jsonify({"error": "Navidrome not configured. Please check your config file and credentials."}), 400
        from api_clients.navidrome import NavidromeClient
        client = NavidromeClient(base_url, username, password)
        playlists = client.fetch_all_playlists()
        if playlists is None:
            logging.error("NavidromeClient returned None for playlists.")
            return jsonify({"error": "Failed to fetch playlists from Navidrome. See logs for details."}), 500
        for pl in playlists:
            if str(pl.get("id")) == str(playlist_id):
                return jsonify(pl)
        logging.warning(f"Playlist ID {playlist_id} not found in Navidrome playlists.")
        return jsonify({"error": f"Playlist {playlist_id} not found in Navidrome."}), 404
    except Exception as e:
        logging.error(f"Failed to fetch Navidrome playlist detail: {e}", exc_info=True)
        return jsonify({"error": f"Exception occurred: {str(e)}"}), 500
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-production")
app.config['PERMANENT_SESSION_LIFETIME'] = 86400  # 24 hours


@app.route("/setup", methods=["GET", "POST"])
def setup():
    import yaml
    try:
        if request.method == "POST":
            # Build full config from form data
            nav_base_urls = request.form.getlist("nav_base_url[]")
            nav_users = request.form.getlist("nav_user[]")
            nav_passes = request.form.getlist("nav_pass[]")
            # Optional per-user fields (future: add more as needed)
            # For now, only first user gets Spotify keys from main form
            users = []
            for i in range(len(nav_base_urls)):
                user = {
                    "base_url": nav_base_urls[i],
                    "user": nav_users[i],
                    "pass": nav_passes[i],
                }
                if i == 0:
                    user["spotify_client_id"] = request.form.get("spotify_client_id", "")
                    user["spotify_client_secret"] = request.form.get("spotify_client_secret", "")
                    user["lastfm_api_key"] = request.form.get("lastfm_api_key", "")
                    user["discogs_token"] = request.form.get("discogs_token", "")
                users.append(user)

            # Always include features and weights at the bottom
            features = {
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
            }
            weights = {"spotify": 0.4, "lastfm": 0.3, "listenbrainz": 0.2, "age": 0.1}

            config = {
                "navidrome_users": users,
                "api_integrations": {
                    "spotify": {
                        "enabled": True,
                        "client_id": request.form.get("spotify_client_id", ""),
                        "client_secret": request.form.get("spotify_client_secret", "")
                    },
                    "lastfm": {
                        "enabled": True,
                        "api_key": request.form.get("lastfm_api_key", "")
                    },
                    "listenbrainz": {"enabled": True},
                    "discogs": {
                        "enabled": True,
                        "token": request.form.get("discogs_token", "")
                    },
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
                    "enabled": True,
                    "web_url": "http://localhost:5030",
                    "api_key": ""
                },
                "downloads": {"folder": "/downloads/Music"},
                "weights": weights,
                "database": {"path": "/database/sptnr.db", "vacuum_on_start": False},
                "logging": {"level": "INFO", "file": "/config/app.log", "console": True},
                "features": features,
            }
            # Always set main navidrome section to first user for compatibility
            if users and len(users) > 0:
                config["navidrome"] = {
                    "base_url": users[0].get("base_url", ""),
                    "user": users[0].get("user", ""),
                    "pass": users[0].get("pass", ""),
                }
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                yaml.safe_dump(config, f, sort_keys=False, allow_unicode=True)
            flash("Setup updated!", "success")
            return redirect(url_for("setup"))

        # For GET, load the full config (all sections) and pass to template
        config = {}
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
        except Exception:
            config = {}
        # Extract user and integration fields for template
        nav_users = config.get("navidrome_users", [])
        # Map legacy/alternate keys to expected keys for UI
        for user in nav_users:
            if "navidrome_base_url" in user:
                user["base_url"] = user["navidrome_base_url"]
            if "navidrome_password" in user:
                user["pass"] = user["navidrome_password"]
            if "username" in user:
                user["user"] = user["username"]
        spotify_client_id = config.get("api_integrations", {}).get("spotify", {}).get("client_id", "")
        spotify_client_secret = config.get("api_integrations", {}).get("spotify", {}).get("client_secret", "")
        discogs_token = config.get("api_integrations", {}).get("discogs", {}).get("token", "")
        lastfm_api_key = config.get("api_integrations", {}).get("lastfm", {}).get("api_key", "")
        return render_template(
            "setup.html",
            nav_users=nav_users,
            spotify_client_id=spotify_client_id,
            spotify_client_secret=spotify_client_secret,
            discogs_token=discogs_token,
            lastfm_api_key=lastfm_api_key
        )
    except Exception as e:
        import logging
        logging.error(f"Error loading setup page: {e}")
        return "Setup page error", 500



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