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
    level=logging.DEBUG,
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

def scan_popularity(verbose: bool = False):
    """
    Scan and update popularity scores from all available sources.
    Updates spotify_score, lastfm_ratio, listenbrainz_score
    """
    logging.info("=" * 60)
    logging.info("Popularity Scanner Started")
    logging.info("=" * 60)
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get all tracks that haven't had popularity data populated yet
        # Priority: tracks with NO popularity scores (zeros), then old ones (older than 7 days)
        cursor.execute("""
            SELECT DISTINCT artist, album, title, id, 
                   spotify_score, lastfm_ratio, listenbrainz_score, 
                   last_scanned, mbid
            FROM tracks
            WHERE (spotify_score = 0 AND lastfm_ratio = 0 AND listenbrainz_score = 0) OR
                  (last_scanned IS NOT NULL AND datetime(last_scanned) < datetime('now', '-7 days'))
            ORDER BY artist, album, title
            LIMIT 2000
        """)
        
        tracks = cursor.fetchall()
        conn.close()
        
        if not tracks:
            print("✅ All tracks have recent popularity data")
            logging.info("All tracks have recent popularity data")
            return
        
        logging.info(f"Scanning popularity for {len(tracks)} tracks")
        print(f"📊 Scanning popularity for {len(tracks)} tracks...")
        
        updated_count = 0
        for idx, track in enumerate(tracks, 1):
            if idx % 50 == 0:
                print(f"Progress: {idx}/{len(tracks)}")
                logging.info(f"Popularity scan progress: {idx}/{len(tracks)}")
            
            artist = track['artist']
            title = track['title']
            track_id = track['id']
            
            # Get Spotify score
            spotify_score = track['spotify_score'] or 0
            try:
                results = search_spotify_track(title, artist)
                if results and isinstance(results, list) and len(results) > 0:
                    # Select best match (highest popularity)
                    best_match = max(results, key=lambda r: r.get('popularity', 0))
                    spotify_score = best_match.get('popularity', 0)
                    if verbose:
                        logging.debug(f"Spotify popularity for {title}: {spotify_score}")
            except Exception as e:
                logging.debug(f"Spotify popularity lookup failed for {title}: {e}")
            
            # Get Last.fm ratio
            lastfm_ratio = track['lastfm_ratio'] or 0
            try:
                info = get_lastfm_track_info(artist, title)
                if info and info.get('playcount'):
                    lastfm_ratio = min(100, int(info['playcount']) / 10)
                    if verbose:
                        logging.debug(f"Last.fm ratio for {title}: {lastfm_ratio}")
            except Exception as e:
                logging.debug(f"Last.fm lookup failed for {title}: {e}")
            
            # Get ListenBrainz score
            listenbrainz_count = track['listenbrainz_score'] or 0
            try:
                mbid_value = track['mbid'] if 'mbid' in track.keys() else ''
                score = get_listenbrainz_score(mbid_value, artist, title)
                listenbrainz_count = score
                if verbose:
                    logging.debug(f"ListenBrainz count for {title}: {listenbrainz_count}")
            except Exception as e:
                logging.debug(f"ListenBrainz lookup failed for {title}: {e}")
            
            # Update database
            try:
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE tracks SET
                        spotify_score = ?,
                        lastfm_ratio = ?,
                        listenbrainz_score = ?
                    WHERE id = ?
                """, (spotify_score, lastfm_ratio, listenbrainz_count, track_id))
                conn.commit()
                conn.close()
                updated_count += 1
                
                if verbose:
                    print(f"  ✓ {title}: Spotify={spotify_score}, LastFM={lastfm_ratio:.1f}, LB={listenbrainz_count}")
            except Exception as e:
                logging.error(f"Failed to update track {track_id}: {e}")
        
        print(f"✅ Popularity scan complete: Updated {updated_count}/{len(tracks)} tracks")
        logging.info(f"Popularity scan complete: Updated {updated_count}/{len(tracks)} tracks")
    
    except Exception as e:
        logging.error(f"Popularity scan failed: {e}")
        print(f"❌ Popularity scan failed: {e}")
        raise
    
    finally:
        logging.info("=" * 60)

def popularity_scan(verbose: bool = False):
    """Detect track popularity from external sources (legacy function)"""
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
                    spotify_results = search_spotify_track(title, artist, track.get("album"))
                    if spotify_results and isinstance(spotify_results, list) and len(spotify_results) > 0:
                        # Select best match (highest popularity)
                        best_match = max(spotify_results, key=lambda r: r.get('popularity', 0))
                        spotify_score = best_match.get("popularity", 0)
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
