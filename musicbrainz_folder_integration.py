#!/usr/bin/env python3
"""
MusicBrainz Release Folder Integration

Integrates MusicBrainz releases into the existing folder-based monitoring system.
Displays releases as "green" folders with progress tracking and file discovery.
"""

import os
import logging
from pathlib import Path
from datetime import datetime
from helpers.db_utils import get_db_connection

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_FILE = "sptnr.db"
DB_TIMEOUT = 120.0
DOWNLOADS_MUSIC_DIR = "/downloads/Music"


def get_files_in_folder(folder_path):
    """
    Get all audio files in a folder
    
    Args:
        folder_path: Path to monitoring folder
        
    Returns:
        list of file dicts with name, size, extension, type
    """
    files = []
    supported_formats = {'.mp3', '.flac', '.m4a', '.ogg', '.wav', '.aac', '.wma'}
    
    try:
        folder = Path(folder_path)
        if not folder.exists():
            return []
        
        for file_path in folder.iterdir():
            if file_path.is_file() and file_path.suffix.lower() in supported_formats:
                files.append({
                    "name": file_path.name,
                    "size": file_path.stat().st_size,
                    "extension": file_path.suffix.lower(),
                    "type": "file",
                    "modified": datetime.fromtimestamp(file_path.stat().st_mtime).isoformat()
                })
        
        # Sort by modification time (newest first)
        files.sort(key=lambda x: x['modified'], reverse=True)
        return files
        
    except Exception as e:
        logger.error(f"Error getting files in folder {folder_path}: {e}")
        return []


def get_folder_groups_with_musicbrainz():
    """
    Get combined view of folder groups including MusicBrainz releases
    
    Returns:
        dict with folder_groups list and metadata
    """
    from musicbrainz_release_manager import MusicBrainzReleaseManager
    
    manager = MusicBrainzReleaseManager()
    mb_releases = manager.get_active_releases()
    
    folder_groups = []
    
    # Add MusicBrainz releases as special folder groups
    for release in mb_releases:
        monitor_folder = release.get('monitoring_folder')
        
        folder_group = {
            "type": "musicbrainz",  # Mark as MB release for green styling
            "name": monitor_folder,
            "display_name": f"{release['release_title']} ({release['artist']} - {release['release_year']})",
            "release_id": release["release_id"],
            "total_tracks": release["total_tracks"],
            "discovered_count": release["discovered_count"],
            "organized_count": release.get("organized_count", 0),
            "finalized_count": release.get("finalized_count", 0),
            "progress_percent": release["progress_percent"],
            "status": release.get("status", "active"),
            "files": get_files_in_folder(monitor_folder),  # Get actual files in folder
            "metadata": {
                "artist": release['artist'],
                "album": release['release_title'],
                "year": release['release_year'],
                "source": "musicbrainz",
                "created_at": release.get("created_at"),
                "updated_at": release.get("updated_at")
            }
        }
        
        folder_groups.append(folder_group)
    
    return {
        "success": True,
        "count": len(folder_groups),
        "folder_groups": folder_groups
    }


def get_folder_group_details(folder_path):
    """
    Get detailed info for a specific folder group
    
    Args:
        folder_path: Path to the folder
        
    Returns:
        dict with folder details and all files
    """
    try:
        folder = Path(folder_path)
        
        if not folder.exists():
            return {"error": f"Folder not found: {folder_path}"}
        
        # Get all files (including non-audio for completeness)
        all_files = []
        for file_path in folder.iterdir():
            if file_path.is_file():
                all_files.append({
                    "name": file_path.name,
                    "size": file_path.stat().st_size,
                    "extension": file_path.suffix.lower(),
                    "modified": datetime.fromtimestamp(file_path.stat().st_mtime).isoformat(),
                    "is_audio": file_path.suffix.lower() in {'.mp3', '.flac', '.m4a', '.ogg', '.wav', '.aac', '.wma'}
                })
        
        # Sort by modification time
        all_files.sort(key=lambda x: x['modified'], reverse=True)
        
        return {
            "success": True,
            "folder": folder_path,
            "name": folder.name,
            "file_count": len(all_files),
            "audio_files": len([f for f in all_files if f['is_audio']]),
            "files": all_files,
            "created": datetime.fromtimestamp(folder.stat().st_ctime).isoformat(),
            "modified": datetime.fromtimestamp(folder.stat().st_mtime).isoformat()
        }
        
    except Exception as e:
        logger.error(f"Error getting folder details: {e}")
        return {"error": str(e)}


def retry_matching_for_release(release_id):
    """
    Retry file matching for a specific release
    Useful when auto-matching failed and user wants to retry
    
    Args:
        release_id: MusicBrainz release ID
        
    Returns:
        dict with results of retry attempt
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Get release info
        cursor.execute(f"""
            SELECT id, monitoring_folder_path, total_tracks, discovered_count
            FROM musicbrainz_releases
            WHERE release_id = %s
        """, (release_id,))
        
        release_row = cursor.fetchone()
        if not release_row:
            return {"error": "Release not found"}
        
        release_db_id = release_row['id'] if hasattr(release_row, 'get') else release_row[0]
        monitor_folder = release_row['monitoring_folder_path'] if hasattr(release_row, 'get') else release_row[1]
        total_tracks = release_row['total_tracks'] if hasattr(release_row, 'get') else release_row[2]
        discovered_count = release_row['discovered_count'] if hasattr(release_row, 'get') else release_row[3]
        
        # Get all unmatched tracks for this release
        cursor.execute(f"""
            SELECT track_number, track_title, track_artist
            FROM musicbrainz_release_tracks
            WHERE release_id = %s AND status != 'discovered' AND status != 'finalized'
        """, (release_id,))
        
        unmatched_tracks = cursor.fetchall()
        
        # Note: Actual matching logic would be in Phase 5
        # For now, this just returns info about what needs matching
        
        return {
            "success": True,
            "release_id": release_id,
            "monitoring_folder": monitor_folder,
            "total_tracks": total_tracks,
            "discovered_count": discovered_count,
            "unmatched_tracks": [
                {
                    "track_number": row['track_number'] if hasattr(row, 'get') else row[0],
                    "title": row['track_title'] if hasattr(row, 'get') else row[1],
                    "artist": row['track_artist'] if hasattr(row, 'get') else row[2]
                }
                for row in unmatched_tracks
            ],
            "message": f"Retry would match {len(unmatched_tracks)} remaining tracks"
        }
        
    except Exception as e:
        logger.error(f"Error retrying matching: {e}")
        return {"error": str(e)}
    finally:
        conn.close()


def cancel_folder_downloads(folder_path):
    """
    Cancel all downloads for a folder (MusicBrainz release or regular folder)
    
    Args:
        folder_path: Path to the folder
        
    Returns:
        dict with cancellation results
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Check if this is a MusicBrainz release folder
        cursor.execute(f"""
            SELECT id, release_id FROM musicbrainz_releases
            WHERE monitoring_folder_path = %s
        """, (folder_path,))
        
        mb_result = cursor.fetchone()
        
        if mb_result:
            release_db_id = mb_result['id'] if hasattr(mb_result, 'get') else mb_result[0]
            release_id = mb_result['release_id'] if hasattr(mb_result, 'get') else mb_result[1]
            
            # Remove from queue
            cursor.execute(f"""
                DELETE FROM download_queue
                WHERE mb_release_download_id = %s
            """, (release_db_id,))
            
            # Mark release as cancelled
            cursor.execute(f"""
                UPDATE musicbrainz_releases
                SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (release_db_id,))
            
            conn.commit()
            return {
                "success": True,
                "type": "musicbrainz",
                "release_id": release_id,
                "folder": folder_path,
                "message": "Release cancelled and removed from queue"
            }
        
        # TODO: Handle regular folder cancellation
        
        return {
            "success": False,
            "error": "Folder not recognized as MusicBrainz release"
        }
        
    except Exception as e:
        logger.error(f"Error cancelling folder: {e}")
        return {"error": str(e)}
    finally:
        conn.close()
