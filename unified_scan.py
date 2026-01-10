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

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/config/unified_scan.log"),
        logging.StreamHandler()
    ]
)

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
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
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
    from popularity import scan_popularity
    from start import rate_artist, build_artist_index
    from scan_history import log_album_scan
    
    logging.info("\n🟢 ==================== UNIFIED SCAN PIPELINE STARTED ==================== 🟢")
    logging.info(f"🕒 Start Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logging.info("=" * 70)
    
    # Initialize progress
    progress = ScanProgress()
    progress.scan_type = "unified"
    progress.start_time = time.time()
    progress.is_running = True
    progress.save()
    
    try:
        # Get list of artists
        conn = get_db_connection()
        cursor = conn.cursor()
        
        if artist_filter:
            cursor.execute("""
                SELECT DISTINCT artist FROM tracks WHERE artist = ?
                ORDER BY artist COLLATE NOCASE
            """, (artist_filter,))
        else:
            cursor.execute("""
                SELECT DISTINCT artist FROM tracks
                ORDER BY artist COLLATE NOCASE
            """)
        
        artists = [row['artist'] for row in cursor.fetchall()]
        progress.total_artists = len(artists)
        
        # Count total tracks for progress
        cursor.execute("""
            SELECT COUNT(*) as count FROM tracks
            WHERE 1=1 {}
        """.format("AND artist = ?" if artist_filter else ""),
        (artist_filter,) if artist_filter else ())
        progress.total_tracks = cursor.fetchone()['count']
        
        # Count actual music files in folder for accurate progress
        music_folder = os.environ.get("MUSIC_FOLDER", "/music")
        progress.total_files = count_music_files(music_folder)
        
        conn.close()
        
        logging.info(f"🎼 Total Artists: {progress.total_artists} | 🎵 Total Tracks: {progress.total_tracks} | 📂 Files: {progress.total_files}")
        progress.save()
        
        # Build artist index for ID lookups
        artist_index = build_artist_index()
        
        # Process each artist
        for idx, artist_name in enumerate(artists, 1):
            progress.current_artist = artist_name
            progress.processed_artists = idx - 1
            logging.info("")
            logging.info(f"🎤 [Artist {idx}/{progress.total_artists}] {artist_name}")
            # Get artist ID
            artist_id = artist_index.get(artist_name)
            if not artist_id:
                logging.warning(f"⚠️ No artist ID found for '{artist_name}', skipping")
                continue
            # Get albums for this artist
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT DISTINCT album FROM tracks
                WHERE artist = ?
                ORDER BY album COLLATE NOCASE
            """, (artist_name,))
            albums = [row['album'] for row in cursor.fetchall()]
            conn.close()
            for album_idx, album_name in enumerate(albums, 1):
                progress.current_album = album_name
                logging.info(f"   💿 [Album {album_idx}/{len(albums)}] {album_name}")
                # Phase 1: Popularity Detection
                progress.current_phase = "popularity"
                progress.save()
                if progress_callback:
                    progress_callback(progress)
                logging.info(f"      → Phase: Popularity detection")
                try:
                    scan_popularity(verbose=verbose, artist=artist_name)
                except Exception as e:
                    logging.error(f"      ✗ Popularity scan failed: {e}")
                # Phase 2: Single Detection & Rating
                progress.current_phase = "singles"
                progress.save()
                if progress_callback:
                    progress_callback(progress)
                logging.info(f"      → Phase: Single detection & rating")
                try:
                    rate_artist(artist_id, artist_name, verbose=verbose, force=force)
                    # Log singles detection scan
                    log_album_scan(artist_name, album_name, 'singles', album_track_count, 'completed')
                except Exception as e:
                    logging.error(f"      ✗ Rating failed: {e}")
                # Log unified scan for this album
                log_album_scan(artist_name, album_name, 'unified', album_track_count, 'completed')
                # Update track count
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT COUNT(*) as count FROM tracks
                    WHERE artist = ? AND album = ?
                """, (artist_name, album_name))
                album_track_count = cursor.fetchone()['count']
                conn.close()
                progress.processed_tracks += album_track_count
                if progress.total_tracks > 0:
                    progress.processed_files = int((progress.processed_tracks / progress.total_tracks) * progress.total_files)
                progress.processed_albums += 1
                progress.save()
                if progress_callback:
                    progress_callback(progress)
                logging.info(f"      ✓ Album complete: {album_name} ({album_track_count} tracks)")
            # --- Essential Artist Smart Playlist Creation ---
            try:
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("SELECT id, title, stars, final_score FROM tracks WHERE artist = ?", (artist_name,))
                tracks = cursor.fetchall()
                conn.close()
                five_star_tracks = [t for t in tracks if t['stars'] == 5]
                total_tracks = len(tracks)
                playlist_tracks = []
                playlist_type = None
                if len(five_star_tracks) > 10:
                    playlist_tracks = five_star_tracks
                    playlist_type = '5star'
                elif total_tracks > 100:
                    sorted_tracks = sorted(tracks, key=lambda t: t['final_score'], reverse=True)
                    top_n = max(1, int(total_tracks * 0.10))
                    playlist_tracks = sorted_tracks[:top_n]
                    playlist_type = 'top10pct'
                if playlist_tracks:
                    playlist_name = f"Essential {artist_name}"
                    playlist_comment = "Auto-generated by SPTNR"
                    playlist_json = {
                        "name": playlist_name,
                        "comment": playlist_comment,
                        "all": [
                            {"is": {"artist": artist_name}}
                        ],
                        "sort": "random"
                    }
                    if playlist_type == '5star':
                        playlist_json['all'].append({"is": {"rating": 5}})
                    elif playlist_type == 'top10pct':
                        # For top 10%, add explicit track IDs
                        playlist_json['any'] = [{"is": {"id": t['id']}} for t in playlist_tracks]
                    playlists_dir = os.path.join(music_folder, "Playlists")
                    os.makedirs(playlists_dir, exist_ok=True)
                    playlist_path = os.path.join(playlists_dir, f"Essential {artist_name}.nsp")
                    with open(playlist_path, "w", encoding="utf-8") as pf:
                        json.dump(playlist_json, pf, indent=2)
                    logging.info(f"✓ Essential Artist playlist created: {playlist_path}")
            except Exception as e:
                logging.error(f"Failed to create Essential Artist playlist for {artist_name}: {e}")
            # --- End Essential Artist Smart Playlist Creation ---
            progress.processed_artists += 1
            progress.save()
            if progress_callback:
                progress_callback(progress)
            time.sleep(1)
        
        logging.info("")
        logging.info("🟢 ==================== UNIFIED SCAN COMPLETE ==================== 🟢")
        logging.info(f"🏁 End Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logging.info(f"✅ Processed: {progress.processed_artists} artists, {progress.processed_tracks} tracks")
        logging.info("=" * 70)
        
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
