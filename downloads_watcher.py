#!/usr/bin/env python3
"""
Downloads Watcher - Monitors /downloads folder for new MP3 files,
extracts metadata, searches for better metadata online, and organizes
them into /Music with proper directory structure.
"""

import os
import shutil
import sqlite3
import json
import time
import yaml
from datetime import datetime
from pathlib import Path
from helpers.metadata_reader import read_mp3_metadata
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("/config/downloads.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def resolve_downloads_dir():
    """Resolve downloads directory from env/config with safe fallback."""
    def _prefer_music_subfolder(path: str) -> str:
        if not path:
            return path
        normalized = os.path.normpath(path)
        if os.path.basename(normalized).lower() == "downloads":
            music_subdir = os.path.join(normalized, "Music")
            if os.path.isdir(music_subdir):
                return music_subdir
        return path

    env_dir = os.environ.get("DOWNLOADS_DIR")
    if env_dir:
        return _prefer_music_subfolder(env_dir)

    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
    try:
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            configured = (cfg.get("downloads") or {}).get("folder")
            if configured:
                return _prefer_music_subfolder(configured)
    except Exception as e:
        logger.warning(f"Could not read downloads folder from config: {e}")

    return "/downloads/Music"


DOWNLOADS_DIR = resolve_downloads_dir()
MUSIC_DIR = os.environ.get("MUSIC_ROOT", "/music")
DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")

def get_db():
    """Get database connection (PostgreSQL if configured, else SQLite).
    Uses app's get_db() if available for consistency across the application."""
    try:
        from app import get_db as app_get_db
        return app_get_db()
    except ImportError:
        # Fallback if app module not available
        conn = sqlite3.connect(DB_PATH, timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

def _is_postgres_connection(conn):
    """Check if connection is PostgreSQL."""
    try:
        from app import _is_postgres_connection as app_is_postgres_connection
        return app_is_postgres_connection(conn)
    except ImportError:
        return False

def sanitize_filename(filename):
    """Remove/replace invalid filename characters"""
    invalid_chars = '<>:"|?*'
    for char in invalid_chars:
        filename = filename.replace(char, '_')
    return filename

def extract_mp3_metadata(file_path):
    """Extract metadata from MP3 file"""
    try:
        return read_mp3_metadata(file_path)
    except Exception as e:
        logger.error(f"Error reading metadata from {file_path}: {e}")
        return {}

def determine_track_number(metadata):
    """Determine track number with disk prefix for multi-CD albums"""
    track_num = metadata.get('track', '0')
    disk_num = metadata.get('disk', '1')
    
    try:
        # Parse track number (may be "5/12" format)
        if isinstance(track_num, str) and '/' in track_num:
            track_num = track_num.split('/')[0]
        
        track_num = int(str(track_num).split('/')[0]) if track_num else 0
        disk_num = int(str(disk_num).split('/')[0]) if disk_num else 1
        
        if disk_num > 1:
            return f"{disk_num}{track_num:02d}"
        return f"{track_num:02d}"
    except:
        return "00"

def organize_file(file_path, metadata):
    """
    Organize file into /Music with structure:
    /Music/Artist Name/Release Year - Album Name/Track Number. Artist Name - Song Title.mp3
    """
    try:
        artist = metadata.get('artist', 'Unknown Artist').strip() or 'Unknown Artist'
        album = metadata.get('album', 'Unknown Album').strip() or 'Unknown Album'
        title = metadata.get('title', Path(file_path).stem).strip() or Path(file_path).stem
        year = metadata.get('year', metadata.get('date', '')).strip()
        
        # Clean up year (just get first 4 digits if it's a date)
        if year and len(year) >= 4:
            year = year[:4]
        elif not year:
            year = 'Unknown'
        
        track_num = determine_track_number(metadata)
        
        # Build directory structure
        artist_dir = os.path.join(MUSIC_DIR, sanitize_filename(artist))
        album_dir = os.path.join(artist_dir, sanitize_filename(f"{year} - {album}"))
        
        # Create directories
        os.makedirs(album_dir, exist_ok=True)
        
        # Build filename: TrackNumber. Artist Name - Song Title.mp3
        filename = sanitize_filename(f"{track_num}. {artist} - {title}.mp3")
        target_path = os.path.join(album_dir, filename)
        
        # Handle duplicate filenames
        if os.path.exists(target_path):
            base, ext = os.path.splitext(filename)
            counter = 1
            while os.path.exists(os.path.join(album_dir, f"{base}_{counter}{ext}")):
                counter += 1
            target_path = os.path.join(album_dir, f"{base}_{counter}{ext}")
        
        # Move file
        shutil.move(file_path, target_path)
        logger.info(f"Moved: {file_path} -> {target_path}")
        
        return {
            'success': True,
            'target_path': target_path,
            'artist': artist,
            'album': album,
            'title': title,
            'year': year,
            'track_num': track_num
        }
    except Exception as e:
        logger.error(f"Error organizing file {file_path}: {e}")
        return {
            'success': False,
            'error': str(e)
        }

def track_exists_in_library(artist, album, title):
    """Check if a track with same artist/album/title exists in library (case-insensitive)"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        is_pg = _is_postgres_connection(conn)
        
        if is_pg:
            cursor.execute("""
                SELECT id FROM tracks 
                WHERE LOWER(COALESCE(album_artist, artist)) = LOWER(%s) 
                AND LOWER(album) = LOWER(%s)
                AND LOWER(title) = LOWER(%s)
                LIMIT 1
            """, (artist.strip(), album.strip(), title.strip()))
        else:
            cursor.execute("""
                SELECT id FROM tracks 
                WHERE LOWER(COALESCE(album_artist, artist)) = LOWER(?) 
                AND LOWER(album) = LOWER(?)
                AND LOWER(title) = LOWER(?)
                LIMIT 1
            """, (artist.strip(), album.strip(), title.strip()))
        
        exists = cursor.fetchone() is not None
        conn.close()
        return exists
    except Exception as e:
        logger.error(f"Error checking if track exists: {e}")
        return False

def queue_incomplete_download(file_path, metadata):
    """Queue an incomplete download for retry"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        is_pg = _is_postgres_connection(conn)
        
        artist = metadata.get('artist', 'Unknown')
        album = metadata.get('album', 'Unknown')
        title = metadata.get('title', os.path.basename(file_path))
        
        # Check if already exists in library
        exists_in_library = 1 if track_exists_in_library(artist, album, title) else 0
        
        # Use database-agnostic INSERT OR REPLACE approach
        if is_pg:
            cursor.execute("""
                INSERT INTO download_queue (
                    file_path, found_filename, artist, album, title, duration,
                    status, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(file_path) DO UPDATE SET
                    found_filename = EXCLUDED.found_filename,
                    artist = EXCLUDED.artist,
                    album = EXCLUDED.album,
                    title = EXCLUDED.title,
                    duration = EXCLUDED.duration,
                    status = EXCLUDED.status,
                    updated_at = CURRENT_TIMESTAMP
            """, (
                file_path,
                os.path.basename(file_path),
                artist,
                album,
                title,
                metadata.get('duration', 0),
                'discovered' if exists_in_library else 'discovered'
            ))
            conn.commit()
        else:
            cursor.execute("""
                INSERT OR REPLACE INTO download_queue (
                    file_path, found_filename, artist, album, title, duration,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                file_path,
                os.path.basename(file_path),
                artist,
                album,
                title,
                metadata.get('duration', 0),
                'discovered' if exists_in_library else 'discovered',
                datetime.now().isoformat(),
                datetime.now().isoformat()
            ))
            conn.commit()
        
        conn.close()
        logger.info(f"Queued incomplete download: {artist} - {title} (exists_in_library: {exists_in_library})")
        return True
    except Exception as e:
        if "already in queue" not in str(e):
            logger.error(f"Error queuing download: {e}")
        else:
            logger.info(f"File {file_path} already in queue")
        return False

def get_download_queue(status=None, limit=50):
    """Get files from download queue"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        is_pg = _is_postgres_connection(conn)
        
        if status:
            if is_pg:
                cursor.execute("""
                    SELECT * FROM download_queue 
                    WHERE status = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                """, (status, limit))
            else:
                cursor.execute("""
                    SELECT * FROM download_queue 
                    WHERE status = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                """, (status, limit))
        else:
            if is_pg:
                cursor.execute("""
                    SELECT * FROM download_queue 
                    ORDER BY
                        CASE
                            WHEN status IN ('queued', 'searching', 'downloading') THEN 0
                            ELSE 1
                        END,
                        created_at DESC
                    LIMIT %s
                """, (limit,))
            else:
                cursor.execute("""
                    SELECT * FROM download_queue 
                    ORDER BY
                        CASE
                            WHEN status IN ('queued', 'searching', 'downloading') THEN 0
                            ELSE 1
                        END,
                        created_at DESC
                    LIMIT ?
                """, (limit,))
        
        rows = cursor.fetchall()
        conn.close()
        
        return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"Error getting download queue: {e}")
        return []

def get_retry_queue(limit=50):
    """Get files queued for retry"""
    try:
        from datetime import datetime as dt, timedelta
        conn = get_db()
        cursor = conn.cursor()
        is_pg = _is_postgres_connection(conn)
        
        now = dt.now().isoformat()
        
        if is_pg:
            cursor.execute("""
                SELECT * FROM download_queue 
                WHERE status = 'incomplete'
                AND (next_retry_at IS NULL OR next_retry_at <= %s)
                AND retry_count < max_retries
                ORDER BY next_retry_at ASC, created_at ASC
                LIMIT %s
            """, (now, limit))
        else:
            cursor.execute("""
                SELECT * FROM download_queue 
                WHERE status = 'incomplete'
                AND (next_retry_at IS NULL OR next_retry_at <= ?)
                AND retry_count < max_retries
                ORDER BY next_retry_at ASC, created_at ASC
                LIMIT ?
            """, (now, limit))
        
        rows = cursor.fetchall()
        conn.close()
        
        return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"Error getting retry queue: {e}")
        return []

def get_download_queue_grouped(status=None, limit=50):
    """
    Get download queue items grouped by album for smart matching.
    
    Groups songs from the same album together, allowing UI to display them
    as album entries with expandable track lists instead of individual songs.
    
    Args:
        status: Filter by status (e.g., 'completed')
        limit: Maximum number of groups to return
        
    Returns:
        List of album groups with track counts and album metadata
    """
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Get queue items
        if status:
            cursor.execute("""
                SELECT * FROM download_queue 
                WHERE status = ?
                ORDER BY artist ASC, album ASC, created_at ASC
            """, (status,))
        else:
            cursor.execute("""
                SELECT * FROM download_queue 
                ORDER BY artist ASC, album ASC, created_at ASC
            """)
        
        rows = cursor.fetchall()
        items = [dict(row) for row in rows]
        conn.close()
        
        # Filter out items where the file no longer exists
        valid_items = []
        for item in items:
            filepath = item.get('filepath') or item.get('file_path')
            if filepath and os.path.exists(filepath):
                valid_items.append(item)
            else:
                # Log stale entry for debugging
                logger.debug(f"Skipping non-existent file in queue: {filepath}")
        
        items = valid_items
        
        # Group by import_group first (if set), then by artist/album
        from collections import defaultdict
        groups = defaultdict(list)
        
        for item in items:
            import_group = item.get('import_group') or f"{item.get('artist', 'Unknown')}_{item.get('album', 'Unknown')}"
            groups[import_group].append(item)
        
        # Build album group results
        album_groups = []
        for group_key, tracks in groups.items():
            if not tracks:
                continue
            
            # Use first track's metadata for the group
            first_track = tracks[0]
            artist = first_track.get('artist', 'Unknown')
            album = first_track.get('album', 'Unknown')
            
            album_groups.append({
                'group_id': group_key,
                'artist': artist,
                'album': album,
                'track_count': len(tracks),
                'status': first_track.get('status'),
                'created_at': first_track.get('created_at'),
                'updated_at': first_track.get('updated_at'),
                'tracks': tracks
            })
        
        # Sort by artist, album, then creation date
        album_groups.sort(key=lambda x: (x['artist'].lower(), x['album'].lower(), x['created_at']))
        
        # Apply limit to groups
        return album_groups[:limit]
        
    except Exception as e:
        logger.error(f"Error getting grouped download queue: {e}")
        return []

def mark_download_as_failed(file_path, failure_reason):
    """Mark a download as failed and schedule retry"""
    try:
        from datetime import datetime as dt, timedelta
        conn = get_db()
        cursor = conn.cursor()
        is_pg = _is_postgres_connection(conn)
        
        next_retry = (dt.now() + timedelta(minutes=10)).isoformat()  # Retry in 10 minutes
        
        if is_pg:
            cursor.execute("""
                UPDATE download_queue 
                SET status = 'incomplete',
                    retry_count = retry_count + 1,
                    failure_reason = %s,
                    last_retry_at = %s,
                    next_retry_at = %s,
                    updated_at = %s
                WHERE file_path = %s
            """, (
                failure_reason,
                dt.now().isoformat(),
                next_retry,
                dt.now().isoformat(),
                file_path
            ))
        else:
            cursor.execute("""
                UPDATE download_queue 
                SET status = 'incomplete',
                    retry_count = retry_count + 1,
                    failure_reason = ?,
                    last_retry_at = ?,
                    next_retry_at = ?,
                    updated_at = ?
                WHERE file_path = ?
            """, (
                failure_reason,
                dt.now().isoformat(),
                next_retry,
                dt.now().isoformat(),
                file_path
            ))
        
        conn.commit()
        conn.close()
        logger.info(f"Marked as failed with retry scheduled: {file_path}")
        return True
    except Exception as e:
        logger.error(f"Error marking download as failed: {e}")
        return False

def mark_download_as_successful(file_path):
    """Mark a download as successfully processed and remove from queue"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        is_pg = _is_postgres_connection(conn)
        
        if is_pg:
            cursor.execute("""
                DELETE FROM download_queue WHERE file_path = %s
            """, (file_path,))
        else:
            cursor.execute("""
                DELETE FROM download_queue WHERE file_path = ?
            """, (file_path,))
        
        conn.commit()
        conn.close()
        logger.info(f"Removed from queue (successfully processed): {file_path}")
        return True
    except Exception as e:
        logger.error(f"Error marking download as successful: {e}")
        return False

def mark_download_exists_in_library(file_path):
    """Mark a download as already existing in library"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        is_pg = _is_postgres_connection(conn)
        
        if is_pg:
            cursor.execute("""
                UPDATE download_queue 
                SET status = 'discovered',
                    updated_at = %s
                WHERE file_path = %s
            """, (datetime.now().isoformat(), file_path))
        else:
            cursor.execute("""
                UPDATE download_queue 
                SET status = 'discovered',
                    updated_at = ?
                WHERE file_path = ?
            """, (datetime.now().isoformat(), file_path))
        
        conn.commit()
        conn.close()
        logger.info(f"Marked as exists_in_library: {file_path}")
        return True
    except Exception as e:
        logger.error(f"Error marking as exists in library: {e}")
        return False

def add_to_database(file_info, metadata):
    """Add organized file to database"""
    try:
        if not file_info.get('success'):
            return False
        
        conn = get_db()
        cursor = conn.cursor()
        is_pg = _is_postgres_connection(conn)
        
        # Generate track ID from path
        track_id = os.path.basename(file_info['target_path']).replace('.mp3', '')
        
        # Build genres string
        genres = metadata.get('genre', '')
        if isinstance(genres, list):
            genres = ', '.join(genres)
        
        # Insert/update track with database-agnostic approach
        if is_pg:
            cursor.execute("""
                INSERT INTO tracks (
                    id, artist, album, title, genres, file_path, last_scanned
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(id) DO UPDATE SET
                    artist = EXCLUDED.artist,
                    album = EXCLUDED.album,
                    title = EXCLUDED.title,
                    genres = EXCLUDED.genres,
                    file_path = EXCLUDED.file_path,
                    last_scanned = EXCLUDED.last_scanned
            """, (
                track_id,
                file_info['artist'],
                file_info['album'],
                file_info['title'],
                genres,
                file_info['target_path'],
                datetime.now().isoformat()
            ))
        else:
            cursor.execute("""
                INSERT OR REPLACE INTO tracks (
                    id, artist, album, title, genres, file_path, last_scanned
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                track_id,
                file_info['artist'],
                file_info['album'],
                file_info['title'],
                genres,
                file_info['target_path'],
                datetime.now().isoformat()
            ))
        
        conn.commit()
        conn.close()
        logger.info(f"Added to database: {track_id}")
        return True
    except Exception as e:
        logger.error(f"Error adding to database: {e}")
        return False

def scan_downloads_folder():
    """Scan downloads folder recursively for MP3/FLAC files."""
    if not os.path.exists(DOWNLOADS_DIR):
        logger.warning(f"Downloads folder not found: {DOWNLOADS_DIR}")
        return []
    
    results = []

    for root, _, files in os.walk(DOWNLOADS_DIR):
        for filename in files:
            if not filename.lower().endswith((".mp3", ".flac")):
                continue

            file_path = os.path.join(root, filename)
        
            # Skip if not a regular file
            if not os.path.isfile(file_path):
                continue

            try:
                logger.info(f"Processing: {os.path.relpath(file_path, DOWNLOADS_DIR)}")

                # Extract metadata
                metadata = extract_mp3_metadata(file_path)
                logger.info(f"Extracted metadata: {metadata}")

                # Organize file
                file_info = organize_file(file_path, metadata)

                if file_info.get('success'):
                    # Add to database
                    add_to_database(file_info, metadata)

                    results.append({
                        'status': 'success',
                        'filename': filename,
                        'artist': file_info.get('artist'),
                        'album': file_info.get('album'),
                        'title': file_info.get('title'),
                        'target_path': file_info.get('target_path')
                    })
                else:
                    results.append({
                        'status': 'error',
                        'filename': filename,
                        'error': file_info.get('error', 'Unknown error')
                    })
            except Exception as e:
                logger.error(f"Error processing {filename}: {e}")
                results.append({
                    'status': 'error',
                    'filename': filename,
                    'error': str(e)
                })
    
    return results

def watch_downloads_folder(interval=10):
    """Watch downloads folder for new files (runs continuously)"""
    logger.info(f"Starting downloads watcher for '{DOWNLOADS_DIR}' (interval: {interval}s)")
    
    while True:
        try:
            results = scan_downloads_folder()
            
            if results:
                logger.info(f"Scan complete. Results: {len(results)}")
                for result in results:
                    if result['status'] == 'success':
                        logger.info(f"✓ {result['filename']} -> {result['artist']}/{result['album']}/{result['title']}")
                    else:
                        logger.error(f"✗ {result['filename']}: {result.get('error', 'Unknown error')}")
            
            time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("Downloads watcher stopped")
            break
        except Exception as e:
            logger.error(f"Error in watch loop: {e}")
            time.sleep(interval)

if __name__ == "__main__":
    watch_downloads_folder(interval=30)
