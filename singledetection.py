#!/usr/bin/env python3
"""
Single Detection Scanner - Detects which tracks are singles vs album tracks.
Uses Discogs, Last.fm, MusicBrainz and other sources to determine if a track is a single.
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
        logging.FileHandler("/config/singledetection.log"),
        logging.StreamHandler()
    ]
)

DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")

# Import from start.py
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from start import (
    is_discogs_single,
    is_lastfm_single,
    is_musicbrainz_single,
    secondary_single_lookup,
    infer_album_context,
)

def get_db_connection():
    """Get database connection with WAL mode"""
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn

def single_detection_scan(verbose: bool = False):
    """Detect which tracks are singles"""
    logging.info("=" * 60)
    logging.info("Single Detection Scanner Started")
    logging.info("=" * 60)
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get all tracks
        cursor.execute("""
            SELECT id, artist, title, album
            FROM tracks
            ORDER BY artist, title
        """)
        
        tracks = cursor.fetchall()
        logging.info(f"Found {len(tracks)} tracks to scan for single detection")
        
        scanned_count = 0
        
        for track in tracks:
            track_id = track["id"]
            artist = track["artist"]
            title = track["title"]
            album = track["album"]
            
            if verbose:
                logging.info(f"Checking: {artist} - {title}")
            
            is_single = False
            single_source = None
            confidence = "low"
            
            # Try Discogs first
            try:
                if is_discogs_single(title, artist, album_context=infer_album_context(album)):
                    is_single = True
                    single_source = "discogs"
                    confidence = "high"
                    if verbose:
                        logging.debug(f"  -> Single (Discogs)")
            except Exception as e:
                if verbose:
                    logging.debug(f"Discogs check failed: {e}")
            
            # Try Last.fm if not already marked as single
            if not is_single:
                try:
                    if is_lastfm_single(title, artist):
                        is_single = True
                        single_source = "lastfm"
                        confidence = "medium"
                        if verbose:
                            logging.debug(f"  -> Single (Last.fm)")
                except Exception as e:
                    if verbose:
                        logging.debug(f"Last.fm check failed: {e}")
            
            # Try MusicBrainz if not already marked as single
            if not is_single:
                try:
                    if is_musicbrainz_single(title, artist):
                        is_single = True
                        single_source = "musicbrainz"
                        confidence = "medium"
                        if verbose:
                            logging.debug(f"  -> Single (MusicBrainz)")
                except Exception as e:
                    if verbose:
                        logging.debug(f"MusicBrainz check failed: {e}")
            
            # Try secondary lookup for additional validation
            if is_single or not is_single:
                try:
                    secondary = secondary_single_lookup(
                        {"title": title, "artist": artist},
                        artist,
                        infer_album_context(album) if album else None
                    )
                    if secondary.get("is_single"):
                        is_single = True
                        single_source = secondary.get("source", "secondary")
                        confidence = "high"
                except Exception as e:
                    if verbose:
                        logging.debug(f"Secondary lookup failed: {e}")
            
            # Update database
            cursor.execute(
                """UPDATE tracks 
                   SET is_single = ?, single_source = ?, single_confidence = ?
                   WHERE id = ?""",
                (1 if is_single else 0, single_source or "none", confidence, track_id)
            )
            scanned_count += 1
        
        conn.commit()
        conn.close()
        
        logging.info(f"✅ Single detection scan completed: {scanned_count} tracks scanned")
        
    except Exception as e:
        logging.error(f"❌ Single detection scan failed: {str(e)}")
        raise
    
    finally:
        logging.info("=" * 60)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Detect which tracks are singles")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output")
    
    args = parser.parse_args()
    single_detection_scan(verbose=args.verbose)
