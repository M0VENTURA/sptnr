#!/usr/bin/env python3
"""
MP3/FLAC Scanner - Scans /music folder for audio files and extracts metadata.
Matches files to Navidrome tracks in the database and stores absolute file paths.
"""

import os
import sqlite3
import logging
from pathlib import Path
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from difflib import SequenceMatcher
import json

# Setup logging
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/config/mp3scanner.log"),
        logging.StreamHandler()
    ]
)

MUSIC_ROOT = os.environ.get("MUSIC_ROOT", "/music")
DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")

def get_db_connection():
    """Get database connection with WAL mode"""
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn

def normalize_title(title):
    """Normalize title for comparison"""
    if not title:
        return ""
    return title.lower().strip()

def similarity(a, b):
    """Calculate string similarity ratio (0.0 to 1.0)"""
    return SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio()

def extract_mp3_metadata(file_path):
    """Extract metadata from MP3 file"""
    try:
        audio = ID3(file_path)
        return {
            "title": str(audio.get("TIT2", "")),
            "artist": str(audio.get("TPE1", "")),
            "album": str(audio.get("TALB", "")),
            "file_path": file_path
        }
    except Exception as e:
        logging.debug(f"Error reading MP3 {file_path}: {e}")
        return None

def extract_flac_metadata(file_path):
    """Extract metadata from FLAC file"""
    try:
        audio = FLAC(file_path)
        return {
            "title": audio.get("title", [""])[0] if audio.get("title") else "",
            "artist": audio.get("artist", [""])[0] if audio.get("artist") else "",
            "album": audio.get("album", [""])[0] if audio.get("album") else "",
            "file_path": file_path
        }
    except Exception as e:
        logging.debug(f"Error reading FLAC {file_path}: {e}")
        return None

def scan_music_folder():
    """Scan /music folder for audio files and extract metadata"""
    logging.info(f"Starting music folder scan at {MUSIC_ROOT}")
    
    if not os.path.exists(MUSIC_ROOT):
        logging.error(f"Music folder not found: {MUSIC_ROOT}")
        return {}
    
    audio_files = {}
    
    # Walk through music folder
    for root, dirs, files in os.walk(MUSIC_ROOT):
        for file in files:
            file_path = os.path.join(root, file)
            
            if file.lower().endswith(".mp3"):
                metadata = extract_mp3_metadata(file_path)
                if metadata:
                    key = f"{normalize_title(metadata['artist'])}|{normalize_title(metadata['album'])}|{normalize_title(metadata['title'])}"
                    audio_files[key] = metadata
                    
            elif file.lower().endswith(".flac"):
                metadata = extract_flac_metadata(file_path)
                if metadata:
                    key = f"{normalize_title(metadata['artist'])}|{normalize_title(metadata['album'])}|{normalize_title(metadata['title'])}"
                    audio_files[key] = metadata
    
    logging.info(f"Found {len(audio_files)} audio files with extractable metadata")
    return audio_files

def match_to_database(audio_files):
    """Match scanned files to database tracks and update file paths"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Get all tracks from database
    cursor.execute("""
        SELECT id, artist, album, title, file_path
        FROM tracks
        WHERE file_path IS NULL OR file_path = ''
        ORDER BY artist, album, title
    """)
    
    db_tracks = cursor.fetchall()
    logging.info(f"Found {len(db_tracks)} tracks in database without file paths")
    
    matched_count = 0
    updated_count = 0
    
    for track in db_tracks:
        track_id = track["id"]
        artist = track["artist"]
        album = track["album"]
        title = track["title"]
        
        # Create search key
        db_key = f"{normalize_title(artist)}|{normalize_title(album)}|{normalize_title(title)}"
        
        # Try exact match first
        if db_key in audio_files:
            file_path = audio_files[db_key]["file_path"]
            cursor.execute(
                "UPDATE tracks SET file_path = ? WHERE id = ?",
                (file_path, track_id)
            )
            matched_count += 1
            logging.debug(f"Exact match: {artist} - {title} -> {file_path}")
            continue
        
        # Try fuzzy matching if exact match fails
        best_match = None
        best_score = 0.7  # Minimum threshold
        
        for file_key, metadata in audio_files.items():
            # Split the key to get components
            parts = file_key.split("|")
            if len(parts) != 3:
                continue
            
            file_artist, file_album, file_title = parts
            
            # Score the match
            artist_score = similarity(artist, file_artist)
            album_score = similarity(album, file_album)
            title_score = similarity(title, file_title)
            
            # Average score
            avg_score = (artist_score + album_score + title_score) / 3.0
            
            # Update best match if this is better
            if avg_score > best_score:
                best_score = avg_score
                best_match = metadata
        
        # If fuzzy match found, update database
        if best_match:
            file_path = best_match["file_path"]
            cursor.execute(
                "UPDATE tracks SET file_path = ? WHERE id = ?",
                (file_path, track_id)
            )
            matched_count += 1
            logging.info(f"Fuzzy match ({best_score:.2%}): {artist} - {title} -> {file_path}")
    
    conn.commit()
    conn.close()
    
    logging.info(f"Matched {matched_count} tracks to files")
    return matched_count

def scan_all_tracks():
    """Full scan: scan folder, then match to database"""
    logging.info("=" * 60)
    logging.info("MP3/FLAC Scanner Started")
    logging.info("=" * 60)
    
    # Scan music folder
    audio_files = scan_music_folder()
    
    # Match to database
    if audio_files:
        matched = match_to_database(audio_files)
        logging.info(f"✅ Scan complete: {matched} tracks matched")
    else:
        logging.warning("⚠️ No audio files found to match")
    
    logging.info("=" * 60)

if __name__ == "__main__":
    scan_all_tracks()
