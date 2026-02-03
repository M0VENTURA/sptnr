#!/usr/bin/env python3
"""
Enhanced Downloads Watcher - Monitors /downloads folder for new MP3 and FLAC files.

Features:
- Monitors downloads folder for MP3 and FLAC files
- Uses beets to rename and move files to main music folder
- Triggers Navidrome API scan after successful import
- Supports configurable scan intervals

Usage:
    python enhanced_downloads_watcher.py
"""

import os
import time
import logging
import subprocess
from pathlib import Path
from datetime import datetime
from typing import List, Set
import requests

# Import centralized logging if available
try:
    from logging_config import setup_logging, log_info, log_debug
    setup_logging("downloads_watcher")
    
    def log_info_wrapper(msg, *args, **kwargs):
        log_info_wrapper(msg)
    def log_debug_wrapper(msg, *args, **kwargs):
        log_debug_wrapper(msg)
except (ImportError, PermissionError, OSError):
    # Fallback logging if centralized logging not available
    log_dir = os.environ.get("LOG_DIR", "/config")
    os.makedirs(log_dir, exist_ok=True) if os.access(os.path.dirname(log_dir) if log_dir != "/" else "/", os.W_OK) else None
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler()
        ]
    )
    
    def log_info_wrapper(msg, *args, **kwargs):
        logging.info(msg)
    def log_debug_wrapper(msg, *args, **kwargs):
        logging.debug(msg)

logger = logging.getLogger(__name__)

# Configuration from environment variables
DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "/downloads/Music")
MUSIC_DIR = os.environ.get("MUSIC_ROOT", "/music")
BEETS_CONFIG = os.environ.get("BEETS_CONFIG", "/config/update_config.yaml")
SCAN_INTERVAL = int(os.environ.get("WATCHER_SCAN_INTERVAL", "30"))  # seconds
NAVIDROME_BASE_URL = os.environ.get("NAVIDROME_BASE_URL", "http://localhost:4533")
NAVIDROME_USER = os.environ.get("NAVIDROME_USER", "admin")
NAVIDROME_PASS = os.environ.get("NAVIDROME_PASS", "password")

# File extensions to monitor
AUDIO_EXTENSIONS = {'.mp3', '.flac', '.m4a', '.ogg', '.opus', '.wma'}


class DownloadsWatcher:
    """Monitors downloads folder and processes new audio files."""
    
    def __init__(self):
        """Initialize the downloads watcher."""
        self.downloads_dir = Path(DOWNLOADS_DIR)
        self.music_dir = Path(MUSIC_DIR)
        self.beets_config = Path(BEETS_CONFIG)
        self.scan_interval = SCAN_INTERVAL
        self.processed_files: Set[str] = set()
        
        # Ensure directories exist
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.music_dir.mkdir(parents=True, exist_ok=True)
        
        log_info_wrapper(f"Downloads Watcher initialized")
        log_info_wrapper(f"  Downloads folder: {self.downloads_dir}")
        log_info_wrapper(f"  Music folder: {self.music_dir}")
        log_info_wrapper(f"  Beets config: {self.beets_config}")
        log_info_wrapper(f"  Scan interval: {self.scan_interval}s")
    
    def get_audio_files(self) -> List[Path]:
        """
        Get list of audio files in downloads folder.
        
        Returns:
            List of Path objects for audio files
        """
        audio_files = []
        
        if not self.downloads_dir.exists():
            log_debug_wrapper(f"Downloads folder does not exist: {self.downloads_dir}")
            return audio_files
        
        for file_path in self.downloads_dir.rglob('*'):
            if file_path.is_file() and file_path.suffix.lower() in AUDIO_EXTENSIONS:
                # Skip already processed files
                if str(file_path) not in self.processed_files:
                    audio_files.append(file_path)
        
        return audio_files
    
    def import_with_beets(self, file_or_folder: Path) -> bool:
        """
        Import audio file(s) using beets.
        
        Args:
            file_or_folder: Path to file or folder to import
            
        Returns:
            True if import was successful
        """
        try:
            # Check if beets is installed
            result = subprocess.run(
                ["which", "beet"],
                capture_output=True,
                timeout=5
            )
            
            if result.returncode != 0:
                log_info_wrapper("⚠️ Beets not installed, skipping import")
                return False
            
            # Build beets import command
            cmd = [
                "beet",
                "-c", str(self.beets_config),
                "import",
                "-m",  # Move files (not copy)
                "-q",  # Quiet mode
                str(file_or_folder)
            ]
            
            log_info_wrapper(f"Running beets import: {' '.join(cmd)}")
            
            # Run beets import
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300  # 5 minute timeout
            )
            
            if result.returncode == 0:
                log_info_wrapper(f"✅ Beets import successful: {file_or_folder}")
                if result.stdout:
                    log_debug_wrapper(f"Beets output: {result.stdout}")
                return True
            else:
                error_msg = result.stderr or result.stdout or "Unknown error"
                log_info_wrapper(f"❌ Beets import failed: {error_msg[:200]}")
                return False
                
        except subprocess.TimeoutExpired:
            log_info_wrapper(f"❌ Beets import timed out for {file_or_folder}")
            return False
        except Exception as e:
            log_info_wrapper(f"❌ Beets import error: {e}")
            return False
    
    def trigger_navidrome_scan(self) -> bool:
        """
        Trigger Navidrome library scan via API.
        
        Returns:
            True if scan was triggered successfully
        """
        try:
            # Navidrome Subsonic API startScan endpoint
            url = f"{NAVIDROME_BASE_URL}/rest/startScan"
            params = {
                "u": NAVIDROME_USER,
                "p": NAVIDROME_PASS,
                "v": "1.16.1",
                "c": "sptnr",
                "f": "json"
            }
            
            log_info_wrapper("Triggering Navidrome library scan...")
            
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            
            result = response.json()
            
            # Check for Subsonic API success response
            if result.get("subsonic-response", {}).get("status") == "ok":
                log_info_wrapper("✅ Navidrome scan triggered successfully")
                return True
            else:
                error = result.get("subsonic-response", {}).get("error", {})
                log_info_wrapper(f"⚠️ Navidrome scan response: {error}")
                return False
                
        except requests.exceptions.RequestException as e:
            log_info_wrapper(f"⚠️ Could not trigger Navidrome scan: {e}")
            return False
        except Exception as e:
            log_info_wrapper(f"⚠️ Error triggering Navidrome scan: {e}")
            return False
    
    def process_new_files(self) -> int:
        """
        Process any new audio files found in downloads folder.
        
        Returns:
            Number of files successfully imported
        """
        new_files = self.get_audio_files()
        
        if not new_files:
            return 0
        
        log_info_wrapper(f"Found {len(new_files)} new audio file(s)")
        
        imported_count = 0
        
        for file_path in new_files:
            log_info_wrapper(f"Processing: {file_path.name}")
            
            # Import with beets
            success = self.import_with_beets(file_path)
            
            if success:
                imported_count += 1
                # Mark as processed even if file was moved
                self.processed_files.add(str(file_path))
            else:
                log_info_wrapper(f"⚠️ Failed to import {file_path.name}")
        
        # Trigger Navidrome scan if any files were imported
        if imported_count > 0:
            log_info_wrapper(f"Successfully imported {imported_count} file(s)")
            self.trigger_navidrome_scan()
        
        return imported_count
    
    def run(self):
        """Run the watcher in continuous loop."""
        log_info_wrapper("=" * 60)
        log_info_wrapper("Enhanced Downloads Watcher Started")
        log_info_wrapper("=" * 60)
        log_info_wrapper(f"Monitoring: {self.downloads_dir}")
        log_info_wrapper(f"Extensions: {', '.join(AUDIO_EXTENSIONS)}")
        log_info_wrapper(f"Scan interval: {self.scan_interval}s")
        log_info_wrapper("=" * 60)
        
        while True:
            try:
                # Process new files
                imported = self.process_new_files()
                
                if imported > 0:
                    log_info_wrapper(f"Imported {imported} file(s) in this scan")
                
                # Wait for next scan
                time.sleep(self.scan_interval)
                
            except KeyboardInterrupt:
                log_info_wrapper("Downloads watcher stopped by user")
                break
            except Exception as e:
                log_info_wrapper(f"❌ Error in watcher loop: {e}")
                import traceback
                log_debug_wrapper(traceback.format_exc())
                time.sleep(self.scan_interval)


def main():
    """Main entry point."""
    watcher = DownloadsWatcher()
    watcher.run()


if __name__ == "__main__":
    main()
