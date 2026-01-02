#!/usr/bin/env python3
"""
Popularity Scanner - Detects track popularity from external sources (Spotify, Last.fm, ListenBrainz).
Calculates popularity scores and updates database.
"""

import os
import sqlite3
import logging
from datetime import datetime
import sys

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/config/popularity.log"),
        logging.StreamHandler()
    ]
)

DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")

# Import from start.py
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from start import (
    get_spotify_artist_id,
    search_spotify_track,
    get_lastfm_track_info,
    get_listenbrainz_score,
    score_by_age,
)

def get_db_connection():
    """Get database connection with WAL mode"""
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn

def popularity_scan(verbose: bool = False):
    """Detect track popularity from external sources"""
    logging.info("=" * 60)
    logging.info("Popularity Scanner Started")
    logging.info("=" * 60)
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get all tracks that need popularity detection
        cursor.execute("""
            SELECT id, artist, title, album
            FROM tracks
            WHERE popularity_score IS NULL OR popularity_score = 0
            ORDER BY artist, title
        """)
        
        tracks = cursor.fetchall()
        logging.info(f"Found {len(tracks)} tracks to scan for popularity")
        
        scanned_count = 0
        
        for track in tracks:
            track_id = track["id"]
            artist = track["artist"]
            title = track["title"]
            
            if verbose:
                logging.info(f"Scanning: {artist} - {title}")
            
            # Try to get popularity from Spotify
            spotify_score = 0
            try:
                artist_id = get_spotify_artist_id(artist)
                if artist_id:
                    spotify_result = search_spotify_track(title, artist, track.get("album"))
                    if spotify_result:
                        spotify_score = spotify_result.get("popularity", 0)
            except Exception as e:
                if verbose:
                    logging.debug(f"Spotify lookup failed for {artist} - {title}: {e}")
            
            # Try to get popularity from Last.fm
            lastfm_score = 0
            try:
                lastfm_info = get_lastfm_track_info(artist, title)
                if lastfm_info and lastfm_info.get("playcount"):
                    lastfm_score = min(100, int(lastfm_info["playcount"]) // 100)
            except Exception as e:
                if verbose:
                    logging.debug(f"Last.fm lookup failed for {artist} - {title}: {e}")
            
            # Average the scores
            if spotify_score > 0 or lastfm_score > 0:
                popularity_score = (spotify_score + lastfm_score) / 2.0
                cursor.execute(
                    "UPDATE tracks SET popularity_score = ? WHERE id = ?",
                    (popularity_score, track_id)
                )
                scanned_count += 1
        
        conn.commit()
        conn.close()
        
        logging.info(f"✅ Popularity scan completed: {scanned_count} tracks updated")
        
    except Exception as e:
        logging.error(f"❌ Popularity scan failed: {str(e)}")
        raise
    
    finally:
        logging.info("=" * 60)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Detect track popularity from external sources")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output")
    
    args = parser.parse_args()
    popularity_scan(verbose=args.verbose)
