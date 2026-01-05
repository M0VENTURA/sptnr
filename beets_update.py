#!/usr/bin/env python3
"""
Beets Album Update Module - Update tags and organize files for specific albums.

This module provides functions to update album metadata and organization using beets.
Uses the update_config.yml which has write=true and file reorganization enabled.
"""

import subprocess
import logging
from pathlib import Path
from typing import Optional, Dict
import sqlite3

logger = logging.getLogger(__name__)

DB_PATH = "/database/sptnr.db"
CONFIG_PATH = "/config"
UPDATE_CONFIG = Path(CONFIG_PATH) / "update_config.yaml"


def update_album_with_beets(album_folder: str) -> Dict[str, any]:
    """
    Update an album folder with beets using the update config.
    
    This will:
    1. Fetch latest metadata from MusicBrainz
    2. Write tags to files
    3. Reorganize files based on metadata
    
    Args:
        album_folder: Full path to the album folder (e.g., /music/Artist Name/Album Name)
        
    Returns:
        Dict with success status and details
    """
    if not UPDATE_CONFIG.exists():
        return {
            "success": False,
            "error": f"Update config not found at {UPDATE_CONFIG}"
        }
    
    album_path = Path(album_folder)
    
    if not album_path.exists():
        return {
            "success": False,
            "error": f"Album folder not found: {album_folder}"
        }
    
    try:
        # Build beets command to move/update files for this album path
        cmd = [
            "beet",
            "-c", str(UPDATE_CONFIG),
            "move",
            f"path:{album_folder}"
        ]
        
        logger.info(f"Running beets update: {' '.join(cmd)}")
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300  # 5 minute timeout
        )
        
        if result.returncode == 0:
            logger.info(f"Successfully updated album: {album_folder}")
            return {
                "success": True,
                "message": f"Album updated: {album_folder}",
                "output": result.stdout,
                "folder": album_folder
            }
        else:
            error_msg = result.stderr or result.stdout or "Unknown error"
            logger.error(f"Beets update failed for {album_folder}: {error_msg}")
            return {
                "success": False,
                "error": error_msg[:500],
                "folder": album_folder
            }
    
    except subprocess.TimeoutExpired:
        logger.error(f"Beets update timed out for {album_folder}")
        return {
            "success": False,
            "error": "Update timed out (>5 minutes)",
            "folder": album_folder
        }
    except Exception as e:
        logger.error(f"Beets update failed for {album_folder}: {e}")
        return {
            "success": False,
            "error": str(e)[:500],
            "folder": album_folder
        }


def get_album_folder_for_track(track_id: str) -> Optional[str]:
    """
    Get the album folder path for a track from the database.
    
    Args:
        track_id: Track ID from sptnr database
        
    Returns:
        Album folder path or None if not found
    """
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30.0)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("SELECT album_folder FROM tracks WHERE id = ?", (track_id,))
        row = cursor.fetchone()
        conn.close()
        
        if row and row['album_folder']:
            return row['album_folder']
        
        return None
    
    except Exception as e:
        logger.error(f"Failed to get album folder for track {track_id}: {e}")
        return None


def get_album_folder_for_artist_album(artist: str, album: str) -> Optional[str]:
    """
    Get the album folder path for an artist/album combo.
    
    Args:
        artist: Artist name
        album: Album name
        
    Returns:
        Album folder path or None if not found
    """
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30.0)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Get the first track's album folder for this album
        cursor.execute("""
            SELECT DISTINCT album_folder FROM tracks
            WHERE artist = ? AND album = ? AND album_folder IS NOT NULL
            LIMIT 1
        """, (artist, album))
        
        row = cursor.fetchone()
        conn.close()
        
        if row and row['album_folder']:
            return row['album_folder']
        
        return None
    
    except Exception as e:
        logger.error(f"Failed to get album folder for {artist} - {album}: {e}")
        return None


def get_all_album_folders_for_artist(artist: str) -> list:
    """
    Get all album folder paths for an artist.
    
    Args:
        artist: Artist name
        
    Returns:
        List of unique album folder paths
    """
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30.0)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT DISTINCT artist, album, album_folder
            FROM tracks
            WHERE artist = ? AND album_folder IS NOT NULL
            ORDER BY album
        """, (artist,))
        
        folders = [row['album_folder'] for row in cursor.fetchall()]
        conn.close()
        
        return folders
    
    except Exception as e:
        logger.error(f"Failed to get album folders for artist {artist}: {e}")
        return []


if __name__ == "__main__":
    import sys
    
    logging.basicConfig(level=logging.INFO)
    
    if len(sys.argv) < 2:
        print("Usage: python beets_update.py <album_folder_path>")
        sys.exit(1)
    
    folder = sys.argv[1]
    result = update_album_with_beets(folder)
    
    if result['success']:
        print(f"✅ {result['message']}")
        sys.exit(0)
    else:
        print(f"❌ {result['error']}")
        sys.exit(1)
