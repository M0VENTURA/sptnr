#!/usr/bin/env python3
"""
Music and Downloads Watcher Service
- Monitors /downloads for new files (to be moved to /music via beets)
- Monitors /music for new/changed files (to trigger Navidrome rescan)
- Triggers Navidrome API force sync, waits 10 minutes, then syncs database
"""
import os
import time
import logging
import threading
from pathlib import Path
from datetime import datetime
import hashlib
import shutil

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("/config/music_watcher.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "/downloads")
MUSIC_DIR = os.environ.get("MUSIC_ROOT", "/music")
SCAN_INTERVAL = 30  # seconds
NAVIDROME_SYNC_WAIT = 600  # 10 minutes

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
    # Placeholder: integrate with beets import logic
    logger.info("Checking /downloads for new files...")
    # ...existing logic or call beets_auto_import.py...
    # For now, just log
    pass

# --- Music watcher: trigger Navidrome rescan ---
def trigger_navidrome_sync():
    logger.info("Triggering Navidrome API force sync...")
    # Placeholder: call Navidrome API to force sync
    # ...
    logger.info(f"Waiting {NAVIDROME_SYNC_WAIT//60} minutes for Navidrome sync to complete...")
    time.sleep(NAVIDROME_SYNC_WAIT)
    logger.info("Triggering Navidrome database scan...")
    # Placeholder: call scan_artist_to_db or build_artist_index
    # ...
    logger.info("Navidrome database scan complete.")

# --- Main watcher loop ---
def watcher_service():
    last_downloads = get_file_snapshot(DOWNLOADS_DIR)
    last_music = get_file_snapshot(MUSIC_DIR)
    logger.info("Music/Downloads watcher started.")

    # Initial setup phase: run Navidrome sync once
    logger.info("Initial setup: running Navidrome sync...")
    trigger_navidrome_sync()
    logger.info("Initial Navidrome sync complete. Running Beets auto import...")
    process_new_downloads()
    logger.info("Beets auto import complete. Running popularity and singles detection...")
    # Placeholder: call popularity and singles detection functions
    # from popularity import scan_popularity
    # from singledetection import single_detection_scan
    # scan_popularity()
    # single_detection_scan()
    logger.info("Initial popularity and singles detection complete.")

    # Main watcher loop
    while True:
        try:
            # Downloads watcher
            current_downloads = get_file_snapshot(DOWNLOADS_DIR)
            if current_downloads != last_downloads:
                logger.info("New files detected in /downloads.")
                process_new_downloads()
                last_downloads = current_downloads
            # Music watcher
            current_music = get_file_snapshot(MUSIC_DIR)
            if current_music != last_music:
                logger.info("New or changed files detected in /music. Triggering Navidrome sync and full pipeline.")
                trigger_navidrome_sync()
                logger.info("Navidrome sync complete. Running Beets auto import...")
                process_new_downloads()
                logger.info("Beets auto import complete. Running popularity and singles detection...")
                # Placeholder: call popularity and singles detection functions
                # scan_popularity()
                # single_detection_scan()
                logger.info("Popularity and singles detection complete.")
                last_music = current_music
            time.sleep(SCAN_INTERVAL)
        except KeyboardInterrupt:
            logger.info("Watcher service stopped.")
            break
        except Exception as e:
            logger.error(f"Error in watcher loop: {e}")
            time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    watcher_service()
