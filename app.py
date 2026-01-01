
from flask import Flask, render_template, request, redirect, url_for
import sqlite3
import yaml
import subprocess
import os

DB_PATH = "/database/sptnr.db"
CONFIG_PATH = "/config/config.yaml"

app = Flask(__name__)

def get_artists():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT artist_name, album_count, track_count, last_updated FROM artist_stats ORDER BY artist_name")
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_tracks(artist):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT title, album, stars, mbid, suggested_mbid FROM tracks WHERE artist = ?", (artist,))
    rows = cursor.fetchall()
    conn.close()
    return rows

@app.route("/")
def dashboard():
    artists = get_artists()
    return render_template("dashboard.html", artists=artists)

@app.route("/artist/<name>")
def artist_detail(name):
    tracks = get_tracks(name)
    return render_template("artist.html", artist=name, tracks=tracks)

@app.route("/config", methods=["GET", "POST"])
def config_editor():
    if request.method == "POST":
        new_config = request.form.to_dict()
        with open(CONFIG_PATH, "w") as f:
            yaml.safe_dump(new_config, f)
        return redirect(url_for("dashboard"))
    else:
        with open(CONFIG_PATH) as f:
            config = yaml.safe_load(f)
        return render_template("config.html", config=config)

@app.route("/scan/<artist>")
def scan_artist(artist):
    # Trigger CLI scan in a subprocess
    subprocess.Popen(["python3", "sptnr.py", "--artist", artist, "--sync"])
    return f"Scan started for {artist}. Check logs for progress."

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
