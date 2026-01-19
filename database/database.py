

import sqlite3
from contextlib import closing
from datetime import datetime

DB_FILE = "sptnr.db"

def init_db():
    with closing(sqlite3.connect(DB_FILE)) as conn:
        cursor = conn.cursor()
        # Artists table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS artists (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL
        )
        """)
        # Tracks table (basic structure; columns added dynamically by check_db.py)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS tracks (
            id TEXT PRIMARY KEY
        )
        """)
        
        # Artist stats table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS artist_stats (
            artist_id TEXT PRIMARY KEY,
            artist_name TEXT NOT NULL,
            album_count INTEGER,
            track_count INTEGER,
            last_updated TEXT
        )
        """)
        conn.commit()

def insert_artist(artist_id, name):
    with closing(sqlite3.connect(DB_FILE)) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO artists (id, name) VALUES (?, ?)", (artist_id, name))
        conn.commit()

def insert_or_update_track(track_id, artist_id, album, title, genres, spotify_score,
                           lastfm_score, listenbrainz_score, age_score, final_score,
                           stars, is_single, single_confidence):
    genres_str = ", ".join(genres) if genres else ""
    timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    with closing(sqlite3.connect(DB_FILE)) as conn:
        cursor = conn.cursor()
        cursor.execute("""
        INSERT INTO tracks (id, artist_id, album, title, genres, spotify_score, lastfm_score,
                            listenbrainz_score, age_score, final_score, stars, is_single,
                            single_confidence, last_scanned)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            genres=excluded.genres,
            spotify_score=excluded.spotify_score,
            lastfm_score=excluded.lastfm_score,
            listenbrainz_score=excluded.listenbrainz_score,
            age_score=excluded.age_score,
            final_score=excluded.final_score,
            stars=excluded.stars,
            is_single=excluded.is_single,
            single_confidence=excluded.single_confidence,
            last_scanned=excluded.last_scanned
        """, (track_id, artist_id, album, title, genres_str, spotify_score, lastfm_score,
              listenbrainz_score, age_score, final_score, stars, is_single, single_confidence, timestamp))
        conn.commit()

def get_tracks_by_artist(artist_id):
    with closing(sqlite3.connect(DB_FILE)) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tracks WHERE artist_id = ?", (artist_id,))
        return cursor.fetchall()

def get_top_tracks(limit=10):
    with closing(sqlite3.connect(DB_FILE)) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT title, final_score, stars FROM tracks ORDER BY final_score DESC LIMIT ?", (limit,))
        return cursor.fetchall()
