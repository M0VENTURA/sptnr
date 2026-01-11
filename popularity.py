#!/usr/bin/env python3
"""
Popularity Scanner - Detects track popularity from external sources (Spotify, Last.fm, ListenBrainz).
Calculates popularity scores and updates database.
"""

import os
import sqlite3
import logging
import json
import math
from datetime import datetime

# Import single detection
from single_detector import rate_track_single_detection, WEIGHTS as singles_config

# --- Dual Logger Setup: sptnr.log and unified_scan.log ---
import logging
LOG_PATH = os.environ.get("LOG_PATH", "/config/sptnr.log")
UNIFIED_LOG_PATH = os.environ.get("UNIFIED_SCAN_LOG_PATH", "/config/unified_scan.log")
VERBOSE = (
    os.environ.get("SPTNR_VERBOSE_POPULARITY") or os.environ.get("SPTNR_VERBOSE") or "0"
) == "1"
SERVICE_PREFIX = "popularity_"

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

# Dedicated logger for unified_scan.log
unified_logger = logging.getLogger("unified_scan")
unified_file_handler = logging.FileHandler(UNIFIED_LOG_PATH)
unified_file_handler.setFormatter(formatter)
unified_logger.setLevel(logging.INFO)
# Always add the file handler (even if handlers exist)
unified_logger.addHandler(unified_file_handler)
unified_logger.propagate = False

def log_basic(msg):
    logging.info(msg)

def log_unified(msg):
    unified_logger.info(msg)

def log_verbose(msg):
    if VERBOSE:
        logging.info(msg)

DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")
POPULARITY_PROGRESS_FILE = os.environ.get("POPULARITY_PROGRESS_FILE", "/database/popularity_scan_progress.json")
NAVIDROME_PROGRESS_FILE = os.environ.get("NAVIDROME_PROGRESS_FILE", "/database/navidrome_scan_progress.json")
from popularity_helpers import (
    get_spotify_artist_id,
    search_spotify_track,
    get_lastfm_track_info,
    get_listenbrainz_score,
    score_by_age,
    SPOTIFY_WEIGHT,
    LASTFM_WEIGHT,
    LISTENBRAINZ_WEIGHT,
    AGE_WEIGHT,
)

# Import scan history tracker
try:
    from scan_history import log_album_scan
except ImportError:
    def log_album_scan(*args, **kwargs):
        pass  # Fallback if scan_history not available

def get_db_connection():
    """Get database connection with WAL mode"""
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn

def _navidrome_scan_running() -> bool:
    """Return True if Navidrome scan progress file says a scan is running."""
    try:
        if os.path.exists(NAVIDROME_PROGRESS_FILE):
            with open(NAVIDROME_PROGRESS_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
                return bool(state.get("is_running"))
    except Exception as e:
        log_verbose(f"Could not read Navidrome progress file: {e}")
    return False

def save_popularity_progress(processed_artists: int, total_artists: int):
    """Save popularity scan progress to file"""
    try:
        progress_data = {
            "is_running": True,
            "scan_type": "popularity_scan",
            "processed_artists": processed_artists,
            "total_artists": total_artists,
            "percent_complete": int((processed_artists / total_artists * 100)) if total_artists > 0 else 0
        }
        with open(POPULARITY_PROGRESS_FILE, 'w') as f:
            json.dump(progress_data, f)
    except Exception as e:
        log_basic(f"Error saving popularity progress: {e}")

def popularity_scan(verbose: bool = False):
    """Detect track popularity from external sources (legacy function)"""
    log_unified("=" * 60)
    log_unified("Popularity Scanner Started")
    log_unified("=" * 60)
    log_unified(f"🟢 Popularity scan started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # Initialize popularity helpers to configure Spotify client
    from popularity_helpers import configure_popularity_helpers
    configure_popularity_helpers()
    log_unified("✅ Spotify client configured")

    try:
        log_verbose("Connecting to database for popularity scan...")
        conn = get_db_connection()
        cursor = conn.cursor()

        # Get all tracks that need popularity detection
        sql = ("""
            SELECT id, artist, title, album
            FROM tracks
            WHERE popularity_score IS NULL OR popularity_score = 0
            ORDER BY artist, title
        """)
        log_verbose(f"Executing SQL: {sql.strip()}")
        cursor.execute(sql)

        tracks = cursor.fetchall()
        log_unified(f"Found {len(tracks)} tracks to scan for popularity")
        log_verbose(f"Fetched {len(tracks)} tracks from database.")

        if not tracks:
            log_unified("No tracks found for popularity scan. Exiting.")
        else:
            scanned_count = 0
            album_map = {}
            for idx, track in enumerate(tracks, 1):
                track_id = track["id"]
                artist = track["artist"]
                title = track["title"]
                album = track["album"]

                if verbose:
                    log_verbose(f"Scanning: {artist} - {title} (Track ID: {track_id})")

                # Progress log every 10 tracks
                if idx % 10 == 0:
                    log_unified(f"Progress: scanned {idx} of {len(tracks)} tracks...")

                # Try to get popularity from Spotify
                spotify_score = 0
                try:
                    log_verbose(f"Calling get_spotify_artist_id for artist: {artist}")
                    artist_id = get_spotify_artist_id(artist)
                    log_verbose(f"Spotify artist_id: {artist_id}")
                    if artist_id:
                        log_verbose(f"Calling search_spotify_track for title: {title}, artist: {artist}, album: {album}")
                        spotify_results = search_spotify_track(title, artist, album)
                        log_verbose(f"Spotify results: {spotify_results}")
                        if spotify_results and isinstance(spotify_results, list) and len(spotify_results) > 0:
                            # Select best match (highest popularity)
                            best_match = max(spotify_results, key=lambda r: r.get('popularity', 0))
                            log_verbose(f"Best Spotify match: {best_match}")
                            spotify_score = best_match.get("popularity", 0)
                except Exception as e:
                    log_verbose(f"Spotify lookup failed for {artist} - {title}: {e}")

                # Try to get popularity from Last.fm
                lastfm_score = 0
                try:
                    log_verbose(f"Calling get_lastfm_track_info for artist: {artist}, title: {title}")
                    lastfm_info = get_lastfm_track_info(artist, title)
                    log_verbose(f"Last.fm info: {lastfm_info}")
                    if lastfm_info and lastfm_info.get("track_play"):
                        lastfm_score = min(100, int(lastfm_info["track_play"]) // 100)
                except Exception as e:
                    log_verbose(f"Last.fm lookup failed for {artist} - {title}: {e}")

                # Average the scores
                if spotify_score > 0 or lastfm_score > 0:
                    popularity_score = (spotify_score + lastfm_score) / 2.0
                    log_verbose(f"Updating popularity_score={popularity_score} for track_id={track_id}")
                    cursor.execute(
                        "UPDATE tracks SET popularity_score = ? WHERE id = ?",
                        (popularity_score, track_id)
                    )
                    scanned_count += 1
                    # Track processed albums for logging
                    key = (artist, album)
                    if key not in album_map:
                        album_map[key] = 0
                    album_map[key] += 1
                else:
                    log_verbose(f"No popularity score found for {artist} - {title}")

            log_verbose("Committing changes to database.")
            conn.commit()
            conn.close()

            # Log each album scan to scan_history
            for (artist, album), tracks_processed in album_map.items():
                log_verbose(f"Logging album scan: {artist} - {album}, tracks_processed={tracks_processed}")
                log_album_scan(artist, album, 'popularity', tracks_processed, 'completed')

            log_unified(f"✅ Popularity scan completed: {scanned_count} tracks updated")
            log_verbose(f"Popularity scan completed: {scanned_count} tracks updated")

    except Exception as e:
        log_unified(f"❌ Popularity scan failed: {str(e)}")
        raise

    finally:
        log_unified("=" * 60)
        log_unified(f"✅ Popularity scan complete at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Detect track popularity from external sources")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output")
    
    args = parser.parse_args()
    popularity_scan(verbose=args.verbose)
