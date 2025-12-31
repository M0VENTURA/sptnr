
#!/usr/bin/env python3
from flask import Flask, render_template, request, redirect, url_for
import sqlite3
import subprocess

app = Flask(__name__)
DB_PATH = "/database/sptnr.db"

# ✅ Get all artists from artist_stats
def get_artists():
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT artist_name, last_updated FROM artist_stats ORDER BY artist_name;")
        artists = [{"name": row[0], "last_updated": row[1]} for row in cursor.fetchall()]
        conn.close()
        return artists
    except Exception as e:
        print(f"❌ Error fetching artists: {e}")
        return []

# ✅ Get albums for a specific artist
def get_albums(artist):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT album FROM tracks WHERE artist=? ORDER BY album;", (artist,))
        albums = [row[0] for row in cursor.fetchall()]
        conn.close()
        return albums
    except Exception as e:
        print(f"❌ Error fetching albums: {e}")
        return []

# ✅ Get tracks for a specific album
def get_tracks(album):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT title, final_score, stars, genres FROM tracks WHERE album=? ORDER BY stars DESC;", (album,))
        tracks = [{"title": row[0], "score": row[1], "stars": row[2], "genres": row[3]} for row in cursor.fetchall()]
        conn.close()
        return tracks
    except Exception as e:
        print(f"❌ Error fetching tracks: {e}")
        return []

@app.route("/")
def index():
    artists = get_artists()
    return render_template("index.html", artists=artists)

@app.route("/albums", methods=["POST"])
def albums():
    artist = request.form.get("artist")
    albums = get_albums(artist)
    return render_template("albums.html", artist=artist, albums=albums)

@app.route("/tracks", methods=["POST"])
def tracks():
    album = request.form.get("album")
    tracks = get_tracks(album)
    return render_template("tracks.html", album=album, tracks=tracks)

# ✅ Route to trigger rating script
@app.route("/run-rating", methods=["POST"])
def run_rating():
    mode = request.form.get("mode")  # e.g., "batchrate" or "perpetual"
    cmd = ["python3", "/app/start.py"]
    if mode == "batchrate":
        cmd.append("--batchrate")
    elif mode == "perpetual":
        cmd.append("--perpetual")
    subprocess.Popen(cmd)  # Runs in background
    return redirect(url_for("index"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

