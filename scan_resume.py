#!/usr/bin/env python3
"""
Scan Resume Module
==================

Handles auto-resume functionality for interrupted scans.

Features:
1. Detect interrupted scans using progress file and database state
2. Resume from last scanned artist/album
3. Clean up completed scan state
"""

import os
import json
import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
from pathlib import Path

# Import centralized logging with fallback
try:
    from logging_config import log_debug, log_info, log_unified
except ImportError:
    # Fallback if logging_config not available
    def log_debug(msg, **kwargs):
        logging.debug(msg)
    def log_info(msg, **kwargs):
        logging.info(msg)
    def log_unified(msg, **kwargs):
        logging.info(msg)

logger = logging.getLogger(__name__)

# Progress file paths
NAVIDROME_PROGRESS_FILE = os.environ.get("NAVIDROME_PROGRESS_FILE", "/database/navidrome_scan_progress.json")
POPULARITY_PROGRESS_FILE = os.environ.get("PROGRESS_FILE", "/database/scan_progress.json")


def load_scan_progress(scan_type: str = "navidrome") -> Optional[Dict]:
    """
    Load scan progress from file.
    
    Args:
        scan_type: Type of scan ('navidrome' or 'popularity')
        
    Returns:
        Progress dict or None if not found
    """
    # Get progress file path at runtime to support testing
    if scan_type == "navidrome":
        progress_file = os.environ.get("NAVIDROME_PROGRESS_FILE", "/database/navidrome_scan_progress.json")
    else:
        progress_file = os.environ.get("PROGRESS_FILE", "/database/scan_progress.json")
    
    if not os.path.exists(progress_file):
        log_debug(f"No progress file found at: {progress_file}")
        return None
    
    try:
        with open(progress_file, 'r') as f:
            progress = json.load(f)
        
        log_debug(f"Loaded progress from {progress_file}: {progress}")
        return progress
    except Exception as e:
        log_debug(f"Failed to load progress file {progress_file}: {e}")
        return None


def save_scan_progress(scan_type: str, progress_data: Dict) -> bool:
    """
    Save scan progress to file.
    
    Args:
        scan_type: Type of scan ('navidrome' or 'popularity')
        progress_data: Progress data to save
        
    Returns:
        True if successful, False otherwise
    """
    # Get progress file path at runtime to support testing
    if scan_type == "navidrome":
        progress_file = os.environ.get("NAVIDROME_PROGRESS_FILE", "/database/navidrome_scan_progress.json")
    else:
        progress_file = os.environ.get("PROGRESS_FILE", "/database/scan_progress.json")
    
    try:
        # Ensure directory exists
        progress_dir = os.path.dirname(progress_file)
        if progress_dir:  # Only create if there's a directory component
            os.makedirs(progress_dir, exist_ok=True)
        
        with open(progress_file, 'w') as f:
            json.dump(progress_data, f, indent=2)
        
        log_debug(f"Saved progress to {progress_file}: {progress_data.get('percent_complete', 0)}%")
        return True
    except Exception as e:
        log_debug(f"Failed to save progress file {progress_file}: {e}")
        return False


def clear_scan_progress(scan_type: str = "navidrome") -> bool:
    """
    Clear scan progress file (on completion).
    
    Args:
        scan_type: Type of scan ('navidrome' or 'popularity')
        
    Returns:
        True if successful, False otherwise
    """
    # Get progress file path at runtime
    if scan_type == "navidrome":
        progress_file = os.environ.get("NAVIDROME_PROGRESS_FILE", "/database/navidrome_scan_progress.json")
    else:
        progress_file = os.environ.get("PROGRESS_FILE", "/database/scan_progress.json")
    
    try:
        if os.path.exists(progress_file):
            # Update to mark as not running before deleting
            try:
                with open(progress_file, 'r') as f:
                    progress = json.load(f)
                progress['is_running'] = False
                progress['percent_complete'] = 100
                with open(progress_file, 'w') as f:
                    json.dump(progress, f, indent=2)
            except Exception:
                pass
            
            os.remove(progress_file)
            log_info(f"Cleared progress file: {progress_file}")
            return True
    except Exception as e:
        log_debug(f"Failed to clear progress file {progress_file}: {e}")
        return False


def detect_interrupted_scan(scan_type: str = "navidrome") -> Optional[Dict]:
    """
    Detect if a scan was interrupted and can be resumed.
    
    Checks:
    1. Progress file exists
    2. is_running flag is True
    3. Last update was recent (within 24 hours)
    
    Args:
        scan_type: Type of scan ('navidrome' or 'popularity')
        
    Returns:
        Progress dict if interrupted scan detected, None otherwise
    """
    progress = load_scan_progress(scan_type)
    
    if not progress:
        return None
    
    # Check if scan was running
    is_running = progress.get('is_running', False)
    if not is_running:
        log_debug(f"Progress file exists but scan was not running")
        return None
    
    # Check if progress is recent (within 24 hours)
    # This prevents resuming very old interrupted scans
    try:
        if 'last_updated' in progress:
            last_updated = datetime.fromisoformat(progress['last_updated'])
            age = datetime.now() - last_updated
            if age > timedelta(hours=24):
                log_debug(f"Progress file is too old ({age}), not resuming")
                return None
    except Exception as e:
        log_debug(f"Failed to check progress age: {e}")
    
    log_info(f"Detected interrupted {scan_type} scan at {progress.get('percent_complete', 0)}%")
    log_info(f"Last scanned: {progress.get('current_artist', 'unknown')}")
    
    return progress


def get_artists_to_scan(all_artists: List[str], resume_from: Optional[str] = None) -> List[str]:
    """
    Get list of artists to scan, optionally resuming from a specific artist.
    
    Args:
        all_artists: Complete list of artists
        resume_from: Artist name to resume from (exclusive - this artist was already scanned)
        
    Returns:
        List of artists to scan
    """
    if not resume_from:
        return all_artists
    
    # Find the index of the resume artist
    try:
        resume_index = all_artists.index(resume_from)
        # Start from the next artist (resume_from was already scanned)
        artists_to_scan = all_artists[resume_index + 1:]
        log_info(f"Resuming scan from artist index {resume_index + 1}/{len(all_artists)}")
        log_info(f"Skipping {resume_index + 1} already scanned artists")
        return artists_to_scan
    except ValueError:
        log_debug(f"Resume artist '{resume_from}' not found in artist list, starting from beginning")
        return all_artists


def get_last_scanned_artist_from_db(db_path: str = "/database/sptnr.db") -> Optional[str]:
    """
    Get the last scanned artist from the database based on last_scanned timestamp.
    
    Args:
        db_path: Path to database
        
    Returns:
        Artist name or None
    """
    try:
        conn = sqlite3.connect(db_path, timeout=120)
        cursor = conn.cursor()
        
        # Get the most recently scanned track's artist
        cursor.execute("""
            SELECT artist, MAX(last_scanned) as latest
            FROM tracks
            WHERE last_scanned IS NOT NULL
            GROUP BY artist
            ORDER BY latest DESC
            LIMIT 1
        """)
        
        row = cursor.fetchone()
        conn.close()
        
        if row:
            artist = row[0]
            log_debug(f"Last scanned artist from DB: {artist} at {row[1]}")
            return artist
        
    except Exception as e:
        log_debug(f"Failed to get last scanned artist from DB: {e}")
    
    return None


def should_resume_scan(scan_type: str = "navidrome") -> Tuple[bool, Optional[str]]:
    """
    Check if scan should be resumed.
    
    Returns:
        Tuple of (should_resume: bool, resume_from_artist: Optional[str])
    """
    progress = detect_interrupted_scan(scan_type)
    
    if not progress:
        return False, None
    
    current_artist = progress.get('current_artist')
    
    if not current_artist:
        log_debug("No current artist in progress file")
        return False, None
    
    log_unified(f"Auto-Resume: Resuming {scan_type} scan from {current_artist}")
    log_info(f"Resuming {scan_type} scan from artist: {current_artist}")
    
    return True, current_artist


def mark_scan_completed(scan_type: str = "navidrome") -> bool:
    """
    Mark scan as completed and clear progress.
    
    Args:
        scan_type: Type of scan ('navidrome' or 'popularity')
        
    Returns:
        True if successful
    """
    log_info(f"Marking {scan_type} scan as completed")
    return clear_scan_progress(scan_type)
