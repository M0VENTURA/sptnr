#!/usr/bin/env python3
"""
Unified Scan Module - Coordinates popularity detection and single detection
Processes artist-by-artist, then album-by-album in a sequential pipeline.
"""

import os
import sqlite3
import logging
import json
import time
from datetime import datetime
from typing import Dict, Optional, Callable

# Import centralized logging
from logging_config import setup_logging, log_unified, log_info, log_debug

# Set up logging for unified_scan service
setup_logging("unified_scan")

VERBOSE = (
    os.environ.get("SPTNR_VERBOSE_UNIFIEDSCAN") or os.environ.get("SPTNR_VERBOSE") or "0"
) == "1"

def log_verbose(msg):
    """Legacy verbose logging - redirects to debug"""
    if VERBOSE:
        log_debug(msg)

DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")
PROGRESS_FILE = os.environ.get("PROGRESS_FILE", "/database/scan_progress.json")
SCAN_HISTORY_PATH = os.environ.get("SCAN_HISTORY_PATH", "/database/scan_history.json")


def count_music_files(music_folder: str = "/music") -> int:
    """
    Count total music files in folder.
    
    Args:
        music_folder: Root music folder path
        
    Returns:
        Total count of audio files (.mp3, .flac, .ogg, .m4a, etc.)
    """
    audio_extensions = {'.mp3', '.flac', '.ogg', '.m4a', '.wav', '.aac', '.wma', '.opus'}
    total = 0
    
    try:
        if not os.path.isdir(music_folder):
            logging.warning(f"Music folder not found: {music_folder}")
            return 0
        
        for root, dirs, files in os.walk(music_folder):
            for file in files:
                if os.path.splitext(file)[1].lower() in audio_extensions:
                    total += 1
    except Exception as e:
        logging.error(f"Error counting music files: {e}")
    
    return total


class ScanProgress:
    """Manages scan progress state"""
    
    def __init__(self):
        self.current_artist = None
        self.current_album = None
        self.total_artists = 0
        self.processed_artists = 0
        self.total_albums = 0
        self.processed_albums = 0
        self.total_tracks = 0
        self.processed_tracks = 0
        self.total_files = 0  # Files in music folder
        self.processed_files = 0  # Files processed
        self.scan_type = None
        self.start_time = None
        self.is_running = False
        self.current_phase = None  # "popularity", "singles", "navidrome_update"
        
    def to_dict(self) -> Dict:
        """Convert progress to dictionary"""
        elapsed = 0
        if self.start_time:
            elapsed = int(time.time() - self.start_time)
        
        return {
            "current_artist": self.current_artist,
            "current_album": self.current_album,
            "total_artists": self.total_artists,
            "processed_artists": self.processed_artists,
            "total_albums": self.total_albums,
            "processed_albums": self.processed_albums,
            "total_tracks": self.total_tracks,
            "processed_tracks": self.processed_tracks,
            "total_files": self.total_files,
            "processed_files": self.processed_files,
            "scan_type": self.scan_type,
            "is_running": self.is_running,
            "current_phase": self.current_phase,
            "elapsed_seconds": elapsed,
            "percent_complete": self._calculate_percent()
        }
    
    def _calculate_percent(self) -> float:
        """Calculate overall completion percentage based on files processed"""
        # If we have file count, use that (more accurate)
        if self.total_files > 0:
            return min(100.0, (self.processed_files / self.total_files) * 100)
        
        # Fallback: use track count if no file count available
        if self.total_tracks > 0:
            return min(100.0, (self.processed_tracks / self.total_tracks) * 100)
        
        return 0.0
    
    def save(self):
        """Save progress to file"""
        try:
            with open(PROGRESS_FILE, 'w') as f:
                json.dump(self.to_dict(), f, indent=2)
        except Exception as e:
            logging.error(f"Failed to save progress: {e}")
    
    @classmethod
    def load(cls) -> 'ScanProgress':
        """Load progress from file"""
        progress = cls()
        try:
            if os.path.exists(PROGRESS_FILE):
                with open(PROGRESS_FILE, 'r') as f:
                    data = json.load(f)
                    progress.current_artist = data.get("current_artist")
                    progress.current_album = data.get("current_album")
                    progress.total_artists = data.get("total_artists", 0)
                    progress.processed_artists = data.get("processed_artists", 0)
                    progress.total_albums = data.get("total_albums", 0)
                    progress.processed_albums = data.get("processed_albums", 0)
                    progress.total_tracks = data.get("total_tracks", 0)
                    progress.processed_tracks = data.get("processed_tracks", 0)
                    progress.scan_type = data.get("scan_type")
                    progress.is_running = data.get("is_running", False)
                    progress.current_phase = data.get("current_phase")
        except Exception as e:
            logging.error(f"Failed to load progress: {e}")
        return progress


def get_db_connection():
    """Get database connection with WAL mode"""
    conn = sqlite3.connect(DB_PATH, timeout=120.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def unified_scan_pipeline(
    verbose: bool = False,
    force: bool = False,
    artist_filter: Optional[str] = None,
    progress_callback: Optional[Callable[[ScanProgress], None]] = None
):
    """
    Unified scan pipeline that processes:
    1. Popularity detection (Spotify, Last.fm, ListenBrainz)
    2. Single detection (Discogs, MusicBrainz, etc.)
    3. Star rating calculation and Navidrome sync
    
    Processes artist-by-artist, then album-by-album within each artist.
    
    Args:
        verbose: Enable verbose logging
        force: Force re-scan of all tracks
        artist_filter: Optional artist name to filter by
        progress_callback: Optional callback function for progress updates
    """
    from popularity import popularity_scan
    from start import build_artist_index
    from scan_history import log_album_scan
    
    log_unified("=" * 80)
    log_unified("🔄 UNIFIED SCAN PIPELINE STARTED")
    log_unified("=" * 80)
    log_unified(f"Start time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log_unified(f"Verbose: {verbose}, Force: {force}")
    log_unified("")
    if verbose:
        log_verbose("Unified scan pipeline started.")
    
    # Initialize progress
    progress = ScanProgress()
    progress.scan_type = "unified"
    progress.start_time = time.time()
    progress.is_running = True
    progress.save()
    
    try:
        if verbose:
            log_verbose("Getting list of artists from database...")
        # Get list of artists
        conn = get_db_connection()
        cursor = conn.cursor()

        if artist_filter:
            sql = """
                SELECT DISTINCT artist FROM tracks WHERE artist = ?
                ORDER BY artist COLLATE NOCASE
            """
            log_verbose(f"Executing SQL: {sql.strip()} with artist_filter={artist_filter}")
            cursor.execute(sql, (artist_filter,))
        else:
            sql = """
                SELECT DISTINCT artist FROM tracks
                ORDER BY artist COLLATE NOCASE
            """
            log_verbose(f"Executing SQL: {sql.strip()}")
            cursor.execute(sql)

        artists = [row['artist'] for row in cursor.fetchall()]
        progress.total_artists = len(artists)
        log_verbose(f"Found {progress.total_artists} artists.")

        # Count total tracks for progress
        sql = """
            SELECT COUNT(*) as count FROM tracks
            WHERE 1=1 {}
        """.format("AND artist = ?" if artist_filter else "")
        log_verbose(f"Executing SQL: {sql.strip()} with artist_filter={artist_filter}")
        cursor.execute(sql, (artist_filter,) if artist_filter else ())
        progress.total_tracks = cursor.fetchone()['count']
        log_verbose(f"Total tracks: {progress.total_tracks}")

        # Count actual music files in folder for accurate progress
        music_folder = os.environ.get("MUSIC_FOLDER", "/music")
        log_verbose(f"Counting music files in folder: {music_folder}")
        progress.total_files = count_music_files(music_folder)

        conn.close()

        logging.info(f"🎼 Total Artists: {progress.total_artists} | 🎵 Total Tracks: {progress.total_tracks} | 📂 Files: {progress.total_files}")
        progress.save()

        # Build artist index for ID lookups
        log_verbose("Building artist index...")
        artist_index = build_artist_index()

        # Run popularity scan ONCE for all tracks before processing artists
        # This ensures artist IDs are looked up only once per artist and cached in the database
        log_unified("⭐ Popularity Detection")
        log_unified("-" * 80)
        log_unified("Detecting track popularity and singles...")
        logging.info("📊 Running popularity scan for all tracks...")
        try:
            # Pass artist_filter if specified, and skip_header to avoid duplicate headers
            popularity_scan(
                verbose=verbose, 
                artist_filter=artist_filter,
                skip_header=True,
                force=force
            )
            log_unified("✅ Popularity detection complete")
            log_unified("")
            logging.info("✅ Popularity scan completed for all tracks")
        except Exception as e:
            logging.error(f"❌ Popularity detection failed: {e}")
            log_unified(f"❌ Popularity detection failed: {e}")
            # Continue with singles detection even if popularity scan fails
        
        # Note: Phase 2 (Singles Detection & Star Rating) has been removed as it was redundant.
        # All functionality (popularity scoring, singles detection, star rating, Navidrome sync,
        # and essential playlist creation) is now handled by popularity_scan() in Phase 1.
        
        # Update progress to mark scan as complete
        progress.processed_artists = progress.total_artists
        progress.processed_tracks = progress.total_tracks
        progress.processed_files = progress.total_files
        progress.save()
        if progress_callback:
            progress_callback(progress)
        
        # Pipeline complete
        log_unified("=" * 80)
        log_unified("✅ UNIFIED SCAN PIPELINE COMPLETE")
        log_unified("=" * 80)
        log_unified(f"End time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
    except Exception as e:
        logging.error(f"Unified scan failed: {e}")
        raise
    finally:
        progress.is_running = False
        progress.current_phase = None
        progress.save()


def get_scan_progress() -> Dict:
    """Get current scan progress"""
    progress = ScanProgress.load()
    return progress.to_dict()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Unified scan pipeline for popularity and single detection")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output")
    parser.add_argument("--force", action="store_true", help="Force re-scan of all tracks")
    parser.add_argument("--artist", type=str, help="Filter by specific artist")
    
    args = parser.parse_args()
    unified_scan_pipeline(verbose=args.verbose, force=args.force, artist_filter=args.artist)
