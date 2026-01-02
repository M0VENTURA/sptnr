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
from datetime import datetime
from pathlib import Path
from metadata_reader import read_mp3_metadata, aggregate_genres_from_tracks
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

DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "/downloads")
MUSIC_DIR = os.environ.get("MUSIC_ROOT", "/music")
DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")

def get_db():
    """Get database connection"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

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

def add_to_database(file_info, metadata):
    """Add organized file to database"""
    try:
        if not file_info.get('success'):
            return False
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Generate track ID from path
        track_id = os.path.basename(file_info['target_path']).replace('.mp3', '')
        
        # Build genres string
        genres = metadata.get('genre', '')
        if isinstance(genres, list):
            genres = ', '.join(genres)
        
        # Insert/update track
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
    """Scan /downloads folder for MP3 files"""
    if not os.path.exists(DOWNLOADS_DIR):
        logger.warning(f"Downloads folder not found: {DOWNLOADS_DIR}")
        return []
    
    results = []
    
    for filename in os.listdir(DOWNLOADS_DIR):
        if not filename.lower().endswith('.mp3'):
            continue
        
        file_path = os.path.join(DOWNLOADS_DIR, filename)
        
        # Skip if not a file
        if not os.path.isfile(file_path):
            continue
        
        try:
            logger.info(f"Processing: {filename}")
            
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
    logger.info(f"Starting downloads watcher (interval: {interval}s)")
    
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
