

import sqlite3
import psycopg2
import psycopg2.extras
from contextlib import closing
from datetime import datetime

DB_FILE = "sptnr.db"
DB_TIMEOUT = 120.0  # 2-minute timeout for database operations

def get_db_connection():
    """Get database connection (PostgreSQL if available, else SQLite)."""
    try:
        import os
        db_url = os.environ.get('DATABASE_URL') or os.environ.get('PG_DSN')
        if db_url:
            conn = psycopg2.connect(db_url)
            return conn
    except (ImportError, psycopg2.Error):
        pass
    
    return sqlite3.connect(DB_FILE, timeout=DB_TIMEOUT)

def is_postgres_connection(conn):
    """Check if connection is PostgreSQL."""
    return isinstance(conn, psycopg2.extensions.connection)

def init_db():
    conn = get_db_connection()
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
    conn.close()

def insert_artist(artist_id, name):
    conn = get_db_connection()
    cursor = conn.cursor()
    is_pg = is_postgres_connection(conn)
    
    if is_pg:
        cursor.execute("INSERT INTO artists (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING", (artist_id, name))
    else:
        cursor.execute("INSERT OR IGNORE INTO artists (id, name) VALUES (?, ?)", (artist_id, name))
    
    conn.commit()
    conn.close()

def insert_or_update_track(track_id, artist_id, album, title, genres, spotify_score,
                           lastfm_score, listenbrainz_score, age_score, final_score,
                           stars, is_single, single_confidence):
    genres_str = ", ".join(genres) if genres else ""
    timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    conn = get_db_connection()
    cursor = conn.cursor()
    is_pg = is_postgres_connection(conn)
    
    if is_pg:
        cursor.execute("""
        INSERT INTO tracks (id, artist_id, album, title, genres, spotify_score, lastfm_score,
                            listenbrainz_score, age_score, final_score, stars, is_single,
                            single_confidence, last_scanned)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(id) DO UPDATE SET
            genres=EXCLUDED.genres,
            spotify_score=EXCLUDED.spotify_score,
            lastfm_score=EXCLUDED.lastfm_score,
            listenbrainz_score=EXCLUDED.listenbrainz_score,
            age_score=EXCLUDED.age_score,
            final_score=EXCLUDED.final_score,
            stars=EXCLUDED.stars,
            is_single=EXCLUDED.is_single,
            single_confidence=EXCLUDED.single_confidence,
            last_scanned=EXCLUDED.last_scanned
        """, (track_id, artist_id, album, title, genres_str, spotify_score, lastfm_score,
              listenbrainz_score, age_score, final_score, stars, is_single, single_confidence, timestamp))
    else:
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
    conn.close()

def get_tracks_by_artist(artist_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    is_pg = is_postgres_connection(conn)
    placeholder = "%s" if is_pg else "?"
    cursor.execute(f"SELECT * FROM tracks WHERE artist_id = {placeholder}", (artist_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_top_tracks(limit=10):
    conn = get_db_connection()
    cursor = conn.cursor()
    is_pg = is_postgres_connection(conn)
    placeholder = "%s" if is_pg else "?"
    cursor.execute(f"SELECT title, final_score, stars FROM tracks ORDER BY final_score DESC LIMIT {placeholder}", (limit,))
    rows = cursor.fetchall()
    conn.close()
    return rows
