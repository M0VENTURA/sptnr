#!/usr/bin/env python3
"""
Music and Downloads Watcher Service
- Monitors /downloads for new files (to be moved to /music via beets)
- Monitors /music for new/changed files (to trigger Navidrome rescan)
- Triggers Navidrome API force sync, waits 10 minutes, then syncs database
- Auto-imports new songs from Navidrome and triggers popularity scan
"""
import os
import time
import logging
import threading
from pathlib import Path
from datetime import datetime
import hashlib
import shutil
import subprocess
import traceback
import requests
import sys

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("/config/music_watcher.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "/downloads/Music")
MUSIC_DIR = os.environ.get("MUSIC_ROOT", "/music")
BEETS_CONFIG = os.environ.get("BEETS_CONFIG", "/config/update_config.yaml")
NAVIDROME_BASE_URL = os.environ.get("NAVIDROME_BASE_URL", "http://localhost:4533")
NAVIDROME_USER = os.environ.get("NAVIDROME_USER", "admin")
NAVIDROME_PASS = os.environ.get("NAVIDROME_PASS", "password")

# Load watcher settings from config
try:
    from config_loader import load_config
    config = load_config()
    watcher_config = config.get("watcher", {})
    SCAN_INTERVAL = int(watcher_config.get("scan_interval", 30))  # seconds
    NAVIDROME_SYNC_WAIT = int(watcher_config.get("navidrome_sync_wait", 600))  # seconds
    AUTO_IMPORT_ENABLED = bool(watcher_config.get("auto_import_enabled", True))
    AUTO_POPULARITY_SCAN = bool(watcher_config.get("auto_popularity_scan", True))
    DOWNLOADS_WATCHER_ENABLED = bool(watcher_config.get("downloads_watcher_enabled", True))
except Exception as e:
    logger.warning(f"Could not load watcher config, using defaults: {e}")
    SCAN_INTERVAL = int(os.environ.get("WATCHER_SCAN_INTERVAL", "30"))
    NAVIDROME_SYNC_WAIT = 600
    AUTO_IMPORT_ENABLED = True
    AUTO_POPULARITY_SCAN = True
    DOWNLOADS_WATCHER_ENABLED = True

# File extensions to monitor
AUDIO_EXTENSIONS = {'.mp3', '.flac', '.m4a', '.ogg', '.opus', '.wma'}

# --- Utility: Snapshot file state ---
def get_file_snapshot(folder):
    """Return a dict of {filepath: mtime_hash} for all files in folder recursively."""
    snapshot = {}
    for root, _, files in os.walk(folder):
        for f in files:
            path = os.path.join(root, f)
            try:
                stat = os.stat(path)
                # Use mtime and size for change detection
                snapshot[path] = f"{stat.st_mtime}-{stat.st_size}"
            except Exception:
                continue
    return snapshot

# --- Downloads watcher: move new files to /music using beets ---
def process_new_downloads():
    """Process new audio files in downloads folder using beets."""
    logger.info("Checking /downloads for new audio files...")
    
    downloads_path = Path(DOWNLOADS_DIR)
    if not downloads_path.exists():
        logger.warning(f"Downloads folder not found: {DOWNLOADS_DIR}")
        return False
    
    # Find new audio files
    audio_files = []
    for file_path in downloads_path.rglob('*'):
        if file_path.is_file() and file_path.suffix.lower() in AUDIO_EXTENSIONS:
            audio_files.append(file_path)
    
    if not audio_files:
        logger.debug("No new audio files found")
        return False
    
    logger.info(f"Found {len(audio_files)} new audio file(s)")
    
    # Import using beets
    try:
        # Check if beets is installed
        result = subprocess.run(
            ["which", "beet"],
            capture_output=True,
            timeout=5
        )
        
        if result.returncode != 0:
            logger.warning("⚠️ Beets not installed, cannot process files")
            return False
        
        # Build beets import command for the entire downloads folder
        cmd = [
            "beet",
            "-c", str(BEETS_CONFIG),
            "import",
            "-m",  # Move files (not copy)
            "-q",  # Quiet mode
            str(DOWNLOADS_DIR)
        ]
        
        logger.info(f"Running beets import on {DOWNLOADS_DIR}")
        
        # Run beets import
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300  # 5 minute timeout
        )
        
        if result.returncode == 0:
            logger.info(f"✅ Beets import successful")
            if result.stdout:
                logger.debug(f"Beets output: {result.stdout}")
            return True
        else:
            error_msg = result.stderr or result.stdout or "Unknown error"
            logger.warning(f"⚠️ Beets import had issues: {error_msg[:200]}")
            return False
            
    except subprocess.TimeoutExpired:
        logger.error(f"❌ Beets import timed out")
        return False
    except Exception as e:
        logger.error(f"❌ Beets import error: {e}")
        return False

# --- Music watcher: trigger Navidrome rescan ---
def trigger_navidrome_sync():
    """Trigger Navidrome library scan and wait for completion."""
    logger.info("Triggering Navidrome API scan...")
    
    try:
        # Navidrome Subsonic API startScan endpoint
        # Note: MD5 is used here as required by Subsonic API specification
        # for the 'enc:' prefix authentication method. This is a limitation
        # of the Subsonic API, not a security choice.
        password_hash = hashlib.md5(NAVIDROME_PASS.encode()).hexdigest()
        
        url = f"{NAVIDROME_BASE_URL}/rest/startScan"
        params = {
            "u": NAVIDROME_USER,
            "p": f"enc:{password_hash}",  # Use enc: prefix for hashed password
            "v": "1.16.1",
            "c": "sptnr",
            "f": "json"
        }
        
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        
        result = response.json()
        
        # Check for Subsonic API success response
        if result.get("subsonic-response", {}).get("status") == "ok":
            logger.info("✅ Navidrome scan triggered successfully")
            
            # Wait for Navidrome to complete the scan
            logger.info(f"Waiting {NAVIDROME_SYNC_WAIT//60} minutes for Navidrome scan to complete...")
            time.sleep(NAVIDROME_SYNC_WAIT)
            
            logger.info("Navidrome scan should be complete")
            return True
        else:
            error = result.get("subsonic-response", {}).get("error", {})
            logger.warning(f"⚠️ Navidrome scan response: {error}")
            return False
            
    except requests.exceptions.RequestException as e:
        logger.warning(f"⚠️ Could not trigger Navidrome scan: {e}")
        logger.info("Navidrome may not be available or API endpoint not supported")
        return False
    except Exception as e:
        logger.error(f"❌ Error triggering Navidrome scan: {e}")
        return False

# --- Navidrome database sync: import new songs and trigger popularity scan ---
def sync_navidrome_to_db():
    """Sync new songs from Navidrome to local database and trigger popularity scan."""
    if not AUTO_IMPORT_ENABLED:
        logger.info("Auto-import disabled, skipping Navidrome sync")
        return False
        
    logger.info("Starting Navidrome database sync...")
    
    try:
        # Get list of all artists from Navidrome before import
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from navidrome_import import scan_artist_to_db
        from popularity import popularity_scan
        from db_utils import get_db_connection
        
        # Get all artists from database
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT artist FROM tracks ORDER BY artist")
        artists = [row[0] for row in cursor.fetchall()]
        conn.close()
        
        if not artists:
            logger.info("No artists found in database")
            return False
        
        logger.info(f"Scanning {len(artists)} artists for new songs...")
        new_tracks_count = 0
        
        for artist in artists:
            try:
                # Get track count before import
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM tracks WHERE artist = ?", (artist,))
                before_count = cursor.fetchone()[0]
                conn.close()
                
                # Import from Navidrome
                logger.debug(f"Scanning artist: {artist}")
                scan_artist_to_db(artist)
                
                # Get track count after import
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM tracks WHERE artist = ?", (artist,))
                after_count = cursor.fetchone()[0]
                conn.close()
                
                # Calculate new tracks
                new_tracks = after_count - before_count
                if new_tracks > 0:
                    logger.info(f"✅ Found {new_tracks} new track(s) for {artist}")
                    new_tracks_count += new_tracks
                    
                    # Trigger popularity scan if enabled
                    if AUTO_POPULARITY_SCAN:
                        logger.info(f"Triggering popularity scan for {artist}...")
                        try:
                            popularity_scan(artist)
                            logger.info(f"✅ Popularity scan completed for {artist}")
                        except Exception as e:
                            logger.warning(f"⚠️ Popularity scan failed for {artist}: {e}")
                            
            except Exception as e:
                logger.warning(f"⚠️ Error syncing artist {artist}: {e}")
                continue
        
        if new_tracks_count > 0:
            logger.info(f"✅ Navidrome sync complete: {new_tracks_count} new track(s) imported")
            return True
        else:
            logger.info("No new tracks found")
            return False
            
    except Exception as e:
        logger.error(f"❌ Navidrome sync error: {e}")
        logger.debug(traceback.format_exc())
        return False

# --- Main watcher loop ---
def watcher_service():
    """Main watcher service loop."""
    last_downloads = get_file_snapshot(DOWNLOADS_DIR) if DOWNLOADS_WATCHER_ENABLED else {}
    last_music = get_file_snapshot(MUSIC_DIR)
    logger.info("Music/Downloads watcher started.")
    logger.info(f"Configuration: scan_interval={SCAN_INTERVAL}s, navidrome_sync_wait={NAVIDROME_SYNC_WAIT}s")
    logger.info(f"Auto-import: {AUTO_IMPORT_ENABLED}, Auto-popularity: {AUTO_POPULARITY_SCAN}, Downloads watcher: {DOWNLOADS_WATCHER_ENABLED}")

    # Initial setup phase: run Navidrome sync once
    logger.info("Initial setup: running Navidrome sync...")
    if trigger_navidrome_sync():
        logger.info("Navidrome sync complete. Running database import...")
        sync_navidrome_to_db()
    
    if DOWNLOADS_WATCHER_ENABLED:
        logger.info("Running initial Beets auto import...")
        process_new_downloads()
    
    logger.info("Initial setup complete.")

    # Main watcher loop
    while True:
        try:
            # Downloads watcher - check for new audio files
            if DOWNLOADS_WATCHER_ENABLED:
                current_downloads = get_file_snapshot(DOWNLOADS_DIR)
                if current_downloads != last_downloads:
                    logger.info("New files detected in /downloads.")
                    
                    # Process with beets
                    if process_new_downloads():
                        logger.info("Files imported successfully, triggering Navidrome sync...")
                        if trigger_navidrome_sync():
                            logger.info("Running database import for new files...")
                            sync_navidrome_to_db()
                    
                    last_downloads = current_downloads
            
            # Music watcher - check for changes in music library
            current_music = get_file_snapshot(MUSIC_DIR)
            if current_music != last_music:
                logger.info("New or changed files detected in /music.")
                logger.info("Triggering Navidrome sync...")
                if trigger_navidrome_sync():
                    logger.info("Running database import for new files...")
                    sync_navidrome_to_db()
                last_music = current_music
            
            time.sleep(SCAN_INTERVAL)
            
        except KeyboardInterrupt:
            logger.info("Watcher service stopped.")
            break
        except Exception as e:
            logger.error(f"Error in watcher loop: {e}")
            logger.debug(traceback.format_exc())
            time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    watcher_service()
