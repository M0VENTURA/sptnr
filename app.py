#!/usr/bin/env python3
"""
Sptnr Web UI - Flask application for managing music ratings and scans
"""

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response
import sqlite3
import yaml
import os
import subprocess
import threading
import time
from datetime import datetime
import copy
import json
from check_db import update_schema
from metadata_reader import read_mp3_metadata, find_track_file, aggregate_genres_from_tracks, get_track_metadata_from_db

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-production")

# Paths
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/config.yaml")
DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")
LOG_PATH = os.environ.get("LOG_PATH", "/config/app.log")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(APP_DIR, "config", "config.yaml")

# Global scan process tracker
scan_process = None
scan_lock = threading.Lock()

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
            "refresh_playlists_on_start": False,
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


@app.before_request
def enforce_setup_wizard():
    exempt = {"setup", "static", "config_edit", "config_editor", "logs_stream", "logs_view"}
    if not request.endpoint or request.endpoint in exempt or request.endpoint.startswith("static"):
        return

    cfg, _ = _read_yaml(CONFIG_PATH)
    if _needs_setup(cfg):
        return redirect(url_for("setup"))


def get_db():
    """Get database connection with WAL mode for better concurrency"""
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    
    # Ensure schema is up-to-date
    try:
        update_schema(DB_PATH)
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
    
    cursor.execute("""
        SELECT artist, album, MAX(last_scanned) as last_scan
        FROM tracks
        WHERE last_scanned IS NOT NULL
        GROUP BY artist, album
        ORDER BY last_scan DESC
        LIMIT 10
    """)
    recent_scans = cursor.fetchall()
    
    conn.close()
    
    with scan_lock:
        scan_running = scan_process is not None and scan_process.poll() is None
    
    return render_template("dashboard.html",
                         artist_count=artist_count,
                         album_count=album_count,
                         track_count=track_count,
                         five_star_count=five_star_count,
                         singles_count=singles_count,
                         recent_scans=recent_scans,
                         scan_running=scan_running)


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


@app.route("/artist/<path:name>")
def artist_detail(name):
    """View artist details and albums"""
    conn = get_db()
    cursor = conn.cursor()
    
    # Get albums for this artist
    cursor.execute("""
        SELECT 
            album,
            COUNT(*) as track_count,
            AVG(stars) as avg_stars,
            SUM(CASE WHEN is_single = 1 THEN 1 ELSE 0 END) as singles_count,
            MAX(last_scanned) as last_updated
        FROM tracks
        WHERE artist = ?
        GROUP BY album
        ORDER BY album COLLATE NOCASE
    """, (name,))
    albums_data = cursor.fetchall()
    
    # Get artist stats
    cursor.execute("""
        SELECT 
            COUNT(*) as track_count,
            COUNT(DISTINCT album) as album_count,
            AVG(stars) as avg_stars,
            SUM(CASE WHEN stars = 5 THEN 1 ELSE 0 END) as five_star_count
        FROM tracks
        WHERE artist = ?
    """, (name,))
    artist_stats = cursor.fetchone()
    
    conn.close()
    
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


@app.route("/album/<path:artist>/<path:album>")
def album_detail(artist, album):
    """View album details and tracks"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT *
        FROM tracks
        WHERE artist = ? AND album = ?
        ORDER BY title COLLATE NOCASE
    """, (artist, album))
    tracks_data = cursor.fetchall()
    
    conn.close()
    
    return render_template("album.html",
                         artist_name=artist,
                         album_name=album,
                         tracks=tracks_data)


@app.route("/track/<track_id>")
def track_detail(track_id):
    """View and edit track details"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM tracks WHERE id = ?", (track_id,))
    track = cursor.fetchone()
    
    conn.close()
    
    if not track:
        flash("Track not found", "error")
        return redirect(url_for("dashboard"))
    
    return render_template("track.html", track=track)


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
    
    # Update database
    cursor.execute("""
        UPDATE tracks
        SET title = ?, artist = ?, album = ?, stars = ?, is_single = ?, single_confidence = ?
        WHERE id = ?
    """, (title, artist, album, stars, is_single, single_confidence, track_id))
    
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


@app.route("/scan/stop", methods=["POST"])
def scan_stop():
    """Stop the running scan"""
    global scan_process
    
    with scan_lock:
        if scan_process and scan_process.poll() is None:
            scan_process.terminate()
            scan_process.wait(timeout=10)
            flash("Scan stopped", "info")
        else:
            flash("No scan is currently running", "warning")
    
    return redirect(url_for("dashboard"))


@app.route("/scan/status")
def scan_status():
    """Get scan status (JSON)"""
    with scan_lock:
        running = scan_process is not None and scan_process.poll() is None
    
    return jsonify({"running": running})


@app.route("/logs")
def logs():
    """View logs"""
    return render_template("logs.html", log_path=LOG_PATH)


@app.route("/logs/stream")
def logs_stream():
    """Stream log file in real-time"""
    def generate():
        try:
            with open(LOG_PATH, "r") as f:
                # Seek to end
                f.seek(0, 2)
                while True:
                    line = f.readline()
                    if line:
                        yield f"data: {line}\n\n"
                    else:
                        time.sleep(0.5)
        except FileNotFoundError:
            yield f"data: Log file not found: {LOG_PATH}\n\n"
    
    return Response(generate(), mimetype="text/event-stream")


@app.route("/logs/view")
def logs_view():
    """View last N lines of log"""
    lines = request.args.get("lines", 500, type=int)
    try:
        with open(LOG_PATH, "r") as f:
            all_lines = f.readlines()
            recent_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
            return jsonify({"lines": recent_lines})
    except FileNotFoundError:
        return jsonify({"error": "Log file not found", "lines": []})


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
    """Proxy endpoint for slskd search API"""
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
        import requests as req
        
        headers = {"X-API-Key": api_key} if api_key else {}
        
        # Start search
        search_url = f"{web_url}/api/v0/searches"
        search_data = {"searchText": query}
        resp = req.post(search_url, json=search_data, headers=headers, timeout=10)
        
        if resp.status_code != 201:
            return jsonify({"error": f"Search failed: {resp.status_code}"}), 500
        
        search_response = resp.json()
        search_id = search_response.get("id")
        
        if not search_id:
            return jsonify({"error": "No search ID returned"}), 500
        
        # Poll for results (max 15 seconds)
        import time
        results = []
        for _ in range(30):
            time.sleep(0.5)
            
            # Get search status/results
            status_url = f"{web_url}/api/v0/searches/{search_id}"
            status_resp = req.get(status_url, headers=headers, timeout=10)
            
            if status_resp.status_code == 200:
                search_data = status_resp.json()
                
                # Check if search is complete or has results
                if search_data.get("state") in ["Completed", "Cancelled"]:
                    # Get all responses
                    responses = search_data.get("responses", [])
                    
                    # Flatten file results from all responses
                    for response in responses:
                        username = response.get("username", "Unknown")
                        files = response.get("files", [])
                        
                        for file in files[:50]:  # Limit per user
                            results.append({
                                "username": username,
                                "filename": file.get("filename", ""),
                                "size": file.get("size", 0),
                                "bitrate": file.get("bitRate", 0),
                                "length": file.get("length", 0),
                                "fileId": file.get("code", "")
                            })
                    
                    break
                
                # If still searching but we have some results, we can return partial
                if len(search_data.get("responses", [])) > 0:
                    responses = search_data.get("responses", [])
                    for response in responses:
                        username = response.get("username", "Unknown")
                        files = response.get("files", [])
                        for file in files[:50]:
                            results.append({
                                "username": username,
                                "filename": file.get("filename", ""),
                                "size": file.get("size", 0),
                                "bitrate": file.get("bitRate", 0),
                                "length": file.get("length", 0),
                                "fileId": file.get("code", "")
                            })
        
        return jsonify({"results": results, "searchId": search_id})
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/slskd/download", methods=["POST"])
def slskd_download():
    """Proxy endpoint to download from slskd"""
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
        
        # Enqueue download
        download_url = f"{web_url}/api/v0/transfers/downloads/{username}"
        download_data = {"filename": filename}
        
        resp = req.post(download_url, json=download_data, headers=headers, timeout=10)
        
        if resp.status_code in [200, 201]:
            return jsonify({"success": True, "message": "Download added successfully"})
        else:
            return jsonify({"error": f"Failed to add download: {resp.status_code}"}), 500
            
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
        
        # Start search
        search_url = f"{web_url}/api/v2/search/start"
        resp = session.post(search_url, data={"pattern": query, "plugins": "enabled", "category": "all"})
        
        if resp.status_code != 200:
            return jsonify({"error": f"Search failed: {resp.status_code}"}), 500
        
        search_data = resp.json()
        search_id = search_data.get("id")
        
        if not search_id:
            return jsonify({"error": "No search ID returned"}), 500
        
        # Poll for results (max 10 seconds)
        import time
        results = []
        for _ in range(20):
            time.sleep(0.5)
            status_url = f"{web_url}/api/v2/search/status"
            status_resp = session.get(status_url, params={"id": search_id})
            
            if status_resp.status_code == 200:
                status_data = status_resp.json()
                if status_data and len(status_data) > 0:
                    search_status = status_data[0]
                    if search_status.get("status") == "Stopped":
                        # Get results
                        results_url = f"{web_url}/api/v2/search/results"
                        results_resp = session.get(results_url, params={"id": search_id, "limit": 50})
                        if results_resp.status_code == 200:
                            data = results_resp.json()
                            results = data.get("results", [])
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
        
        # Add torrent
        add_url = f"{web_url}/api/v2/torrents/add"
        resp = session.post(add_url, data={"urls": torrent_url})
        
        if resp.status_code == 200:
            return jsonify({"success": True, "message": "Torrent added successfully"})
        else:
            return jsonify({"error": f"Failed to add torrent: {resp.status_code}"}), 500
            
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
                
                # Try to find the file in /music directory with timeout
                music_root = os.environ.get("MUSIC_ROOT", "/music")
                file_path = None
                
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


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
