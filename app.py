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

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-production")

# Paths
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/config.yaml")
DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")
LOG_PATH = os.environ.get("LOG_PATH", "/config/app.log")

# Global scan process tracker
scan_process = None
scan_lock = threading.Lock()


def get_db():
    """Get database connection with WAL mode for better concurrency"""
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


@app.route("/")
def index():
    """Redirect to dashboard"""
    return redirect(url_for("dashboard"))


@app.route("/dashboard")
def dashboard():
    """Main dashboard with statistics"""
    conn = get_db()
    cursor = conn.cursor()
    
    # Get statistics
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
    
    # Get recent scans
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
    
    # Check if scan is running
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
    
    return render_template("artist.html", 
                         artist_name=name,
                         albums=albums_data,
                         stats=artist_stats)


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
    try:
        with open(CONFIG_PATH) as f:
            config_content = f.read()
            config = yaml.safe_load(config_content) or {}
    except FileNotFoundError:
        config_content = "# Config file not found"
        config = {}
    
    return render_template("config.html", config=config, config_raw=config_content)


@app.route("/config/edit", methods=["POST"])
def config_edit():
    """Save config.yaml"""
    config_content = request.form.get("config_content")
    
    try:
        # Validate YAML
        yaml.safe_load(config_content)
        
        # Save to file
        with open(CONFIG_PATH, "w") as f:
            f.write(config_content)
        
        flash("Configuration saved successfully", "success")
    except yaml.YAMLError as e:
        flash(f"Invalid YAML: {e}", "error")
    except Exception as e:
        flash(f"Error saving config: {e}", "error")
    
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
