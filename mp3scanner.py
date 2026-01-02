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
        from mutagen.mp3 import MP3
        audio = ID3(file_path)
        mp3 = MP3(file_path)
        
        metadata = {
            "title": str(audio.get("TIT2", "")),
            "artist": str(audio.get("TPE1", "")),
            "album": str(audio.get("TALB", "")),
            "file_path": file_path,
            "duration": mp3.info.length if hasattr(mp3.info, 'length') else None,
            "bitrate": mp3.info.bitrate // 1000 if hasattr(mp3.info, 'bitrate') else None,
            "sample_rate": mp3.info.sample_rate if hasattr(mp3.info, 'sample_rate') else None
        }
        
        # Optional fields
        if "TRCK" in audio:  # Track number
            try:
                metadata["track_number"] = int(str(audio["TRCK"]).split('/')[0])
            except:
                pass
        
        if "TPOS" in audio:  # Disc number
            try:
                metadata["disc_number"] = int(str(audio["TPOS"]).split('/')[0])
            except:
                pass
        
        if "TDRC" in audio or "TYER" in audio:  # Year
            try:
                year_str = str(audio.get("TDRC", audio.get("TYER", "")))
                metadata["year"] = int(year_str[:4]) if len(year_str) >= 4 else None
            except:
                pass
        
        if "TPE2" in audio:  # Album artist
            metadata["album_artist"] = str(audio["TPE2"])
        
        if "TBPM" in audio:  # BPM
            try:
                metadata["bpm"] = int(str(audio["TBPM"]))
            except:
                pass
        
        if "TSRC" in audio:  # ISRC
            metadata["isrc"] = str(audio["TSRC"])
        
        if "TCOM" in audio:  # Composer
            metadata["composer"] = str(audio["TCOM"])
        
        if "COMM" in audio:  # Comment
            metadata["comment"] = str(audio["COMM"])
        
        if "USLT" in audio:  # Lyrics
            metadata["lyrics"] = str(audio["USLT"])
        
        return metadata
    except Exception as e:
        logging.debug(f"Error reading MP3 {file_path}: {e}")
        return None

def extract_flac_metadata(file_path):
    """Extract metadata from FLAC file"""
    try:
        audio = FLAC(file_path)
        
        metadata = {
            "title": audio.get("title", [""])[0] if audio.get("title") else "",
            "artist": audio.get("artist", [""])[0] if audio.get("artist") else "",
            "album": audio.get("album", [""])[0] if audio.get("album") else "",
            "file_path": file_path,
            "duration": audio.info.length if hasattr(audio.info, 'length') else None,
            "bitrate": audio.info.bitrate // 1000 if hasattr(audio.info, 'bitrate') else None,
            "sample_rate": audio.info.sample_rate if hasattr(audio.info, 'sample_rate') else None
        }
        
        # Optional fields
        if "tracknumber" in audio:
            try:
                metadata["track_number"] = int(audio["tracknumber"][0].split('/')[0])
            except:
                pass
        
        if "discnumber" in audio:
            try:
                metadata["disc_number"] = int(audio["discnumber"][0].split('/')[0])
            except:
                pass
        
        if "date" in audio:
            try:
                year_str = audio["date"][0]
                metadata["year"] = int(year_str[:4]) if len(year_str) >= 4 else None
            except:
                pass
        
        if "albumartist" in audio:
            metadata["album_artist"] = audio["albumartist"][0]
        
        if "bpm" in audio:
            try:
                metadata["bpm"] = int(audio["bpm"][0])
            except:
                pass
        
        if "isrc" in audio:
            metadata["isrc"] = audio["isrc"][0]
        
        if "composer" in audio:
            metadata["composer"] = audio["composer"][0]
        
        if "comment" in audio:
            metadata["comment"] = audio["comment"][0]
        
        if "lyrics" in audio:
            metadata["lyrics"] = audio["lyrics"][0]
        
        return metadata
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
    file_count = 0
    
    # Walk through music folder
    for root, dirs, files in os.walk(MUSIC_ROOT):
        for file in files:
            file_path = os.path.join(root, file)
            
            if file.lower().endswith(".mp3"):
                file_count += 1
                if file_count % 100 == 0:
                    logging.info(f"Scanned {file_count} files so far...")
                metadata = extract_mp3_metadata(file_path)
                if metadata:
                    key = f"{normalize_title(metadata['artist'])}|{normalize_title(metadata['album'])}|{normalize_title(metadata['title'])}"
                    audio_files[key] = metadata
                    logging.debug(f"Found MP3: {metadata['artist']} - {metadata['title']}")
                    
            elif file.lower().endswith(".flac"):
                file_count += 1
                if file_count % 100 == 0:
                    logging.info(f"Scanned {file_count} files so far...")
                metadata = extract_flac_metadata(file_path)
                if metadata:
                    key = f"{normalize_title(metadata['artist'])}|{normalize_title(metadata['album'])}|{normalize_title(metadata['title'])}"
                    audio_files[key] = metadata
                    logging.debug(f"Found FLAC: {metadata['artist']} - {metadata['title']}")
    
    logging.info(f"Scan complete: Found {len(audio_files)} audio files with extractable metadata from {file_count} total files")
    return audio_files

def match_to_database(audio_files):
    """Match scanned files to database tracks and update file paths"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Build ISRC lookup for files (for better matching)
    isrc_lookup = {}
    for key, metadata in audio_files.items():
        if metadata.get("isrc"):
            isrc_lookup[metadata["isrc"]] = metadata
    
    logging.info(f"Built ISRC index with {len(isrc_lookup)} unique ISRCs")
    
    # Get all tracks from database
    cursor.execute("""
        SELECT id, artist, album, title, file_path, isrc
        FROM tracks
        WHERE file_path IS NULL OR file_path = ''
        ORDER BY artist, album, title
    """)
    
    db_tracks = cursor.fetchall()
    logging.info(f"Found {len(db_tracks)} tracks in database without file paths")
    
    matched_count = 0
    isrc_matched = 0
    exact_matched = 0
    fuzzy_matched = 0
    processed_count = 0
    
    for track in db_tracks:
        track_id = track["id"]
        artist = track["artist"]
        album = track["album"]
        title = track["title"]
        db_isrc = track["isrc"]
        
        processed_count += 1
        if processed_count % 50 == 0:
            logging.info(f"Processing track {processed_count}/{len(db_tracks)} - {matched_count} matches so far (ISRC: {isrc_matched}, Exact: {exact_matched}, Fuzzy: {fuzzy_matched})")
        
        matched_metadata = None
        match_type = None
        
        # Priority 1: Try ISRC match (most reliable)
        if db_isrc and db_isrc in isrc_lookup:
            matched_metadata = isrc_lookup[db_isrc]
            match_type = "ISRC"
            isrc_matched += 1
        
        # Priority 2: Try exact string match
        if not matched_metadata:
            db_key = f"{normalize_title(artist)}|{normalize_title(album)}|{normalize_title(title)}"
            if db_key in audio_files:
                matched_metadata = audio_files[db_key]
                match_type = "Exact"
                exact_matched += 1
        
        # Priority 3: Try fuzzy matching
        if not matched_metadata:
            best_score = 0.7  # Minimum threshold
            for file_key, metadata in audio_files.items():
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
                    matched_metadata = metadata
                    match_type = f"Fuzzy ({best_score:.0%})"
            
            if matched_metadata:
                fuzzy_matched += 1
        
        # Update database if we found a match
        if matched_metadata:
            file_path = matched_metadata["file_path"]
            
            # Update with all metadata fields
            cursor.execute("""
                UPDATE tracks SET 
                    file_path = ?, 
                    duration = ?,
                    track_number = ?,
                    disc_number = ?,
                    year = ?,
                    album_artist = ?,
                    bpm = ?,
                    bitrate = ?,
                    sample_rate = ?,
                    isrc = ?,
                    composer = ?,
                    comment = ?,
                    lyrics = ?
                WHERE id = ?
            """, (
                file_path,
                matched_metadata.get("duration"),
                matched_metadata.get("track_number"),
                matched_metadata.get("disc_number"),
                matched_metadata.get("year"),
                matched_metadata.get("album_artist"),
                matched_metadata.get("bpm"),
                matched_metadata.get("bitrate"),
                matched_metadata.get("sample_rate"),
                matched_metadata.get("isrc"),
                matched_metadata.get("composer"),
                matched_metadata.get("comment"),
                matched_metadata.get("lyrics"),
                track_id
            ))
            matched_count += 1
            logging.info(f"{match_type} match: {artist} - {title} -> {file_path}")
        else:
            logging.debug(f"No match found for: {artist} - {title}")
    
    conn.commit()
    conn.close()
    
    logging.info(f"=" * 60)
    logging.info(f"Matching Statistics:")
    logging.info(f"  Total tracks processed: {len(db_tracks)}")
    logging.info(f"  Total matches: {matched_count}")
    logging.info(f"  - ISRC matches: {isrc_matched}")
    logging.info(f"  - Exact matches: {exact_matched}")
    logging.info(f"  - Fuzzy matches: {fuzzy_matched}")
    logging.info(f"  Unmatched: {len(db_tracks) - matched_count}")
    logging.info(f"=" * 60)
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
