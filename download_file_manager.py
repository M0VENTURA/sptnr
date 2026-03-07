#!/usr/bin/env python3
"""
Download File Manager
Handles individual file matching, metadata updates, and copying for downloaded tracks.
Integrates with MusicBrainz data stored in the queue to update file metadata before copying.
"""

import os
import shutil
import sqlite3
import logging
from pathlib import Path
from datetime import datetime
from difflib import SequenceMatcher
from helpers.metadata_reader import read_mp3_metadata

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [Download File Manager] %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("/config/download_file_manager.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def sanitize_filename(filename):
    """Remove/replace invalid filename characters"""
    invalid_chars = '<>:"|?*\\'
    for char in invalid_chars:
        filename = filename.replace(char, '_')
    # Also remove leading/trailing dots and spaces
    filename = filename.strip('. ')
    return filename


def get_audio_files_in_downloads(downloads_dir):
    """
    Recursively scan downloads folder for audio files.
    Returns list of tuples: (filename, full_path, relative_path)
    """
    audio_files = []
    if not os.path.isdir(downloads_dir):
        return audio_files
    
    try:
        for root, dirs, files in os.walk(downloads_dir):
            for f in files:
                if f.lower().endswith(('.mp3', '.flac', '.m4a', '.ogg', '.wav')):
                    full_path = os.path.join(root, f)
                    rel_path = os.path.relpath(full_path, downloads_dir)
                    audio_files.append({
                        'filename': f,
                        'full_path': full_path,
                        'rel_path': rel_path
                    })
    except Exception as e:
        logger.error(f"Error scanning downloads folder: {e}")
    
    return audio_files


def fuzzy_match_filename(filename, queue_item):
    """
    Fuzzy match a filename against queue item data.
    Returns match score (0.0-1.0)
    """
    try:
        artist = (queue_item.get('artist') or '').lower()
        title = (queue_item.get('title') or '').lower()
        filename_lower = filename.lower()
        
        # Check if both artist and title appear in filename
        if artist and title:
            # Full match: both artist and title in filename
            if artist in filename_lower and title in filename_lower:
                # Calculate how much of the filename matches
                matches = filename_lower.count(artist) + filename_lower.count(title)
                return min(1.0, 0.9 + (0.1 * min(matches / 2, 1.0)))
            
            # Partial match: use SequenceMatcher for fuzzy matching
            combined = f"{artist} {title}"
            ratio = SequenceMatcher(None, combined, filename_lower).ratio()
            return ratio if ratio > 0.5 else 0.0
        
        elif artist or title:
            # Only artist or title available
            search_term = artist or title
            ratio = SequenceMatcher(None, search_term, filename_lower).ratio()
            return ratio if ratio > 0.5 else 0.0
        
        return 0.0
    except Exception as e:
        logger.error(f"Error in fuzzy_match_filename: {e}")
        return 0.0


def find_file_for_queue_item(queue_item, audio_files):
    """
    Find the best matching audio file for a queue item.
    Returns (file_info, match_score) or (None, 0.0)
    """
    if not queue_item:
        return None, 0.0
    
    # Exact match by found_filename
    if queue_item.get('found_filename'):
        for file_info in audio_files:
            if (file_info['filename'] == queue_item['found_filename'] or
                file_info['rel_path'] == queue_item['found_filename'] or
                file_info['full_path'] == queue_item['found_filename']):
                return file_info, 1.0
    
    # Fuzzy match by artist/title
    best_match = None
    best_score = 0.0
    
    for file_info in audio_files:
        score = fuzzy_match_filename(file_info['filename'], queue_item)
        if score > best_score:
            best_score = score
            best_match = file_info
    
    return best_match, best_score


def update_file_metadata(file_path, metadata):
    """
    Update file metadata tags using mutagen.
    
    Args:
        file_path: Path to audio file (MP3, FLAC, etc)
        metadata: Dict with keys: track_number, artist, album_artist, album, year, title, disc_number
    
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        ext = os.path.splitext(file_path)[1].lower()
        
        if ext == '.mp3':
            from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TDRC, TRCK, TPOS
            from mutagen.mp3 import MP3
            
            audio = MP3(file_path, ID3=ID3)
            if audio.tags is None:
                audio.add_tags()
            
            if metadata.get('title'):
                audio.tags['TIT2'] = TIT2(encoding=3, text=[metadata['title']])
            
            if metadata.get('artist'):
                audio.tags['TPE1'] = TPE1(encoding=3, text=[metadata['artist']])
            
            if metadata.get('album_artist'):
                audio.tags['TPE2'] = TPE2(encoding=3, text=[metadata['album_artist']])
            
            if metadata.get('album'):
                audio.tags['TALB'] = TALB(encoding=3, text=[metadata['album']])
            
            if metadata.get('year'):
                audio.tags['TDRC'] = TDRC(encoding=3, text=[metadata['year']])
            
            if metadata.get('track_number'):
                audio.tags['TRCK'] = TRCK(encoding=3, text=[str(metadata['track_number'])])
            
            if metadata.get('disc_number'):
                audio.tags['TPOS'] = TPOS(encoding=3, text=[str(metadata['disc_number'])])
            
            audio.save()
            logger.info(f"✅ Updated MP3 metadata: {file_path}")
            return True
        
        elif ext == '.flac':
            from mutagen.flac import FLAC
            
            audio = FLAC(file_path)
            
            if metadata.get('title'):
                audio['title'] = [metadata['title']]
            
            if metadata.get('artist'):
                audio['artist'] = [metadata['artist']]
            
            if metadata.get('album_artist'):
                audio['albumartist'] = [metadata['album_artist']]
            
            if metadata.get('album'):
                audio['album'] = [metadata['album']]
            
            if metadata.get('year'):
                audio['date'] = [metadata['year']]
            
            if metadata.get('track_number'):
                audio['tracknumber'] = [str(metadata['track_number'])]
            
            if metadata.get('disc_number'):
                audio['discnumber'] = [str(metadata['disc_number'])]
            
            audio.save()
            logger.info(f"✅ Updated FLAC metadata: {file_path}")
            return True
        
        else:
            logger.warning(f"Unsupported file format for metadata update: {ext}")
            return False
    
    except Exception as e:
        logger.error(f"Error updating metadata for {file_path}: {e}")
        return False


def prepare_filename_and_path(music_dir, metadata):
    """
    Prepare filename and directory path based on metadata.
    Uses naming convention: [track_number]. [artist] - [title]
    Directory structure: [album_artist]/[year] - [album]/
    
    Returns: (target_path, directory_created) or (None, False)
    """
    try:
        artist = (metadata.get('artist') or 'Unknown Artist').strip() or 'Unknown Artist'
        album_artist = (metadata.get('album_artist') or artist).strip() or artist
        album = (metadata.get('album') or 'Unknown Album').strip() or 'Unknown Album'
        title = (metadata.get('title') or 'Unknown Title').strip() or 'Unknown Title'
        year = (metadata.get('year') or 'Unknown').strip()
        
        # Clean up year (just get first 4 digits if it's a date)
        if year and len(year) >= 4:
            year = year[:4]
        elif not year:
            year = 'Unknown'
        
        # Format track number with disc prefix if needed
        track_num = metadata.get('track_number', '00')
        disc_num = metadata.get('disc_number', 1)
        
        try:
            track_num = int(str(track_num).split('/')[0]) if track_num else 0
            disc_num = int(str(disc_num).split('/')[0]) if disc_num else 1
            
            if disc_num > 1:
                track_num = f"{disc_num}{track_num:02d}"
            else:
                track_num = f"{track_num:02d}"
        except:
            track_num = "00"
        
        # Build directory structure
        artist_dir = os.path.join(music_dir, sanitize_filename(album_artist))
        album_dir = os.path.join(artist_dir, sanitize_filename(f"{year} - {album}"))
        
        # Create directories
        os.makedirs(album_dir, exist_ok=True)
        
        # Build filename
        ext = metadata.get('ext', '.mp3')
        filename = sanitize_filename(f"{track_num}. {artist} - {title}{ext}")
        target_path = os.path.join(album_dir, filename)
        
        # Handle duplicate filenames
        if os.path.exists(target_path):
            base, ext = os.path.splitext(filename)
            counter = 1
            while os.path.exists(os.path.join(album_dir, f"{base}_{counter}{ext}")):
                counter += 1
            target_path = os.path.join(album_dir, f"{base}_{counter}{ext}")
        
        return target_path, True
    
    except Exception as e:
        logger.error(f"Error preparing filename and path: {e}")
        return None, False


def copy_file_to_music(source_file_path, queue_item, music_dir):
    """
    Copy a file from downloads to music directory with proper metadata and naming.
    
    Args:
        source_file_path: Full path to source file
        queue_item: Queue item dict with metadata
        music_dir: Destination music directory
    
    Returns:
        dict: {
            'success': bool,
            'target_path': str or None,
            'error': str or None,
            'metadata_updated': bool
        }
    """
    try:
        if not os.path.exists(source_file_path):
            return {
                'success': False,
                'target_path': None,
                'error': f"Source file not found: {source_file_path}",
                'metadata_updated': False
            }
        
        # Prepare metadata for update
        metadata = {
            'title': queue_item.get('title', 'Unknown'),
            'artist': queue_item.get('artist', 'Unknown Artist'),
            'album_artist': queue_item.get('album_artist') or queue_item.get('artist', 'Unknown Artist'),
            'album': queue_item.get('album', 'Unknown Album'),
            'year': queue_item.get('year', 'Unknown'),
            'track_number': queue_item.get('track_number'),
            'disc_number': queue_item.get('disc_number'),
            'ext': os.path.splitext(source_file_path)[1].lower()
        }
        
        # Update file metadata
        metadata_updated = update_file_metadata(source_file_path, metadata)
        
        if not metadata_updated:
            logger.warning(f"Could not update metadata for {source_file_path}, but continuing with copy")
        
        # Prepare target path
        target_path, _ = prepare_filename_and_path(music_dir, metadata)
        
        if not target_path:
            return {
                'success': False,
                'target_path': None,
                'error': 'Could not prepare target path',
                'metadata_updated': metadata_updated
            }
        
        # Copy file
        shutil.copy2(source_file_path, target_path)
        logger.info(f"✅ Copied file: {source_file_path} → {target_path}")
        
        return {
            'success': True,
            'target_path': target_path,
            'error': None,
            'metadata_updated': metadata_updated
        }
    
    except Exception as e:
        logger.error(f"Error copying file {source_file_path}: {e}")
        return {
            'success': False,
            'target_path': None,
            'error': str(e),
            'metadata_updated': False
        }


def get_available_files_for_album(album_artist, album_name, downloads_dir):
    """
    Get all available audio files in downloads that match album artist/name.
    Useful for showing user which files can be copied for an album.
    
    Args:
        album_artist: Album artist name
        album_name: Album name
        downloads_dir: Downloads directory path
    
    Returns:
        List of file_info dicts matching the album
    """
    audio_files = get_audio_files_in_downloads(downloads_dir)
    matching_files = []
    
    try:
        artist_lower = (album_artist or '').lower()
        album_lower = (album_name or '').lower()
        
        for file_info in audio_files:
            filename_lower = file_info['filename'].lower()
            # Fuzzy match on filename
            if artist_lower and album_lower:
                if artist_lower in filename_lower and album_lower in filename_lower:
                    matching_files.append(file_info)
                elif artist_lower in filename_lower:
                    matching_files.append(file_info)
            elif artist_lower:
                if artist_lower in filename_lower:
                    matching_files.append(file_info)
            elif album_lower:
                if album_lower in filename_lower:
                    matching_files.append(file_info)
    
    except Exception as e:
        logger.error(f"Error finding files for album: {e}")
    
    return matching_files
