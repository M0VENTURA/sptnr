#!/usr/bin/env python3
"""
Genre and Title Processing Module
==================================

Handles automatic genre tag and title updates based on album and track metadata.

Features:
1. Append (acoustic)/(live)/(unplugged) to titles based on genre tags
2. Add genre tags based on parenthetical tags in titles
3. Propagate album characteristics to track titles and genres
4. Update MP3/FLAC files with new metadata
"""

import re
import os
import logging
from typing import List, Tuple, Optional, Dict
from pathlib import Path

# Import centralized logging
from logging_config import log_debug, log_info

logger = logging.getLogger(__name__)


def normalize_for_comparison(text: str) -> str:
    """
    Normalize text for case-insensitive comparison.
    
    Args:
        text: Text to normalize
        
    Returns:
        Lowercase text
    """
    return text.lower().strip()


def has_parenthetical_tag(title: str, tag: str) -> bool:
    """
    Check if title has a specific tag in parentheses (case-insensitive).
    
    Args:
        title: Track title
        tag: Tag to check for (e.g., "live", "acoustic")
        
    Returns:
        True if tag is in title with parentheses
    """
    if not title or not tag:
        return False
    
    # Pattern matches (tag) with optional suffixes like (live version), (live at...), etc.
    # This matches:
    # - (live)
    # - (Live)
    # - (LIVE)
    # - (live version)
    # - (acoustic)
    # - (Acoustic Version)
    # etc.
    pattern = r'\(' + re.escape(tag) + r'[^\)]*\)'
    return bool(re.search(pattern, title, re.IGNORECASE))


def should_append_tag_to_title(title: str, genre_list: List[str], tag: str) -> bool:
    """
    Check if a tag should be appended to the title.
    
    Conditions:
    - Tag is in genre list (case-insensitive)
    - Tag is NOT already in title with parentheses (case-insensitive)
    
    Args:
        title: Track title
        genre_list: List of genres for the track
        tag: Tag to check (e.g., "acoustic", "live", "unplugged")
        
    Returns:
        True if tag should be appended
    """
    # Check if genre list contains the tag
    has_genre = any(tag.lower() in g.lower() for g in genre_list)
    
    # Check if title already has the tag
    already_in_title = has_parenthetical_tag(title, tag)
    
    return has_genre and not already_in_title


def extract_parenthetical_tags(title: str) -> List[str]:
    """
    Extract parenthetical tags from title that should be added as genres.
    
    Looks for: (Live), (Unplugged), (Acoustic), (Demo), (Remix)
    
    Args:
        title: Track title
        
    Returns:
        List of genre tags to add
    """
    tags_to_check = ['live', 'unplugged', 'acoustic', 'demo', 'remix']
    found_tags = []
    
    for tag in tags_to_check:
        if has_parenthetical_tag(title, tag):
            # Capitalize the tag properly for genre
            found_tags.append(tag.capitalize())
    
    return found_tags


def append_tag_to_title(title: str, tag: str) -> str:
    """
    Append a tag to the title in parentheses.
    
    Args:
        title: Track title
        tag: Tag to append (e.g., "acoustic", "live")
        
    Returns:
        Updated title with tag
    """
    # Capitalize first letter of tag
    formatted_tag = f" ({tag.lower()})"
    return title + formatted_tag


def check_album_for_tags(album_name: str) -> Dict[str, bool]:
    """
    Check album title for acoustic/unplugged/live tags.
    
    Args:
        album_name: Album title
        
    Returns:
        Dict with 'acoustic', 'unplugged', 'live' keys set to True/False
    """
    album_lower = normalize_for_comparison(album_name)
    
    return {
        'acoustic': 'acoustic' in album_lower,
        'unplugged': 'unplugged' in album_lower,
        'live': 'live' in album_lower
    }


def process_track_genres_and_title(
    track_title: str,
    album_name: str,
    genre_list: List[str]
) -> Tuple[str, List[str]]:
    """
    Process track title and genres based on requirements.
    
    Requirements:
    1. If genre has 'acoustic' or 'live', append to title if not present
    2. If title has (Live)/(Acoustic)/(Demo)/(Remix)/(Unplugged), add to genres
    3. If album has 'acoustic'/'unplugged', add to title and genres
    
    Args:
        track_title: Original track title
        album_name: Album name
        genre_list: List of current genres
        
    Returns:
        Tuple of (updated_title, updated_genre_list)
    """
    updated_title = track_title
    updated_genres = list(genre_list)  # Make a copy
    
    # Step 1: Extract tags from title and add to genres
    title_tags = extract_parenthetical_tags(track_title)
    for tag in title_tags:
        if not any(tag.lower() in g.lower() for g in updated_genres):
            updated_genres.append(tag)
            log_debug(f"Added genre '{tag}' from title tag: {track_title}")
    
    # Step 2: Check album for acoustic/unplugged and propagate
    album_tags = check_album_for_tags(album_name)
    
    for tag_key, has_tag in album_tags.items():
        if has_tag:
            tag_capitalized = tag_key.capitalize()
            
            # Add to genres if not present
            if not any(tag_key.lower() in g.lower() for g in updated_genres):
                updated_genres.append(tag_capitalized)
                log_debug(f"Added genre '{tag_capitalized}' from album: {album_name}")
            
            # Add to title if not present (only for acoustic/unplugged from album)
            if tag_key in ['acoustic', 'unplugged']:
                if not has_parenthetical_tag(updated_title, tag_key):
                    updated_title = append_tag_to_title(updated_title, tag_key)
                    log_debug(f"Appended '{tag_key}' to title from album: {track_title} -> {updated_title}")
    
    # Step 3: Check if genre has acoustic/live and add to title
    tags_to_append = ['acoustic', 'live', 'unplugged']
    
    for tag in tags_to_append:
        if should_append_tag_to_title(updated_title, updated_genres, tag):
            updated_title = append_tag_to_title(updated_title, tag)
            log_debug(f"Appended '{tag}' to title from genre: {track_title} -> {updated_title}")
    
    return updated_title, updated_genres


def write_title_to_mp3(file_path: str, title: str) -> bool:
    """
    Write title tag to MP3 file.
    
    Args:
        file_path: Path to MP3 file
        title: Title to write
        
    Returns:
        True if successful, False otherwise
    """
    if not file_path or not os.path.exists(file_path):
        return False
    
    try:
        from mutagen.id3 import ID3, TIT2
        from mutagen.mp3 import MP3
        
        # Load the MP3 file
        audio = MP3(file_path, ID3=ID3)
        
        # Create ID3 tags if they don't exist
        if audio.tags is None:
            audio.add_tags()
        
        # Set Title tag (TIT2 frame in ID3v2)
        audio.tags['TIT2'] = TIT2(encoding=3, text=title)
        
        # Save changes
        audio.save()
        return True
        
    except Exception as e:
        log_debug(f"Failed to write title to MP3 {file_path}: {e}")
        return False


def write_title_to_flac(file_path: str, title: str) -> bool:
    """
    Write title tag to FLAC file.
    
    Args:
        file_path: Path to FLAC file
        title: Title to write
        
    Returns:
        True if successful, False otherwise
    """
    if not file_path or not os.path.exists(file_path):
        return False
    
    try:
        from mutagen.flac import FLAC
        
        # Load the FLAC file
        audio = FLAC(file_path)
        
        # Set Title tag (Vorbis comment)
        audio['title'] = title
        
        # Save changes
        audio.save()
        return True
        
    except Exception as e:
        log_debug(f"Failed to write title to FLAC {file_path}: {e}")
        return False


def write_title_to_audio_file(file_path: str, title: str) -> bool:
    """
    Write title tag to audio file (MP3 or FLAC).
    
    Args:
        file_path: Path to audio file
        title: Title to write
        
    Returns:
        True if successful, False otherwise
    """
    if not file_path:
        return False
    
    file_ext = Path(file_path).suffix.lower()
    
    if file_ext == '.mp3':
        return write_title_to_mp3(file_path, title)
    elif file_ext in ['.flac', '.fla']:
        return write_title_to_flac(file_path, title)
    else:
        log_debug(f"Unsupported file format for title update: {file_ext}")
        return False


def update_track_metadata_file(file_path: str, title: str, genres: List[str]) -> bool:
    """
    Update both title and genre in audio file.
    
    Args:
        file_path: Path to audio file
        title: New title
        genres: List of genres
        
    Returns:
        True if both updates successful, False otherwise
    """
    if not file_path or not os.path.exists(file_path):
        return False
    
    # Import the genre writer from metadata_reader
    try:
        from metadata_reader import write_genre_to_audio_file
    except ImportError:
        log_debug("Cannot import write_genre_to_audio_file from metadata_reader")
        return False
    
    # Update title
    title_success = write_title_to_audio_file(file_path, title)
    
    # Update genres
    genre_success = write_genre_to_audio_file(file_path, genres)
    
    if title_success and genre_success:
        log_info(f"Updated file: {file_path} - Title: {title}, Genres: {genres}")
        return True
    elif title_success:
        log_debug(f"Updated title but genre update failed for: {file_path}")
        return False
    elif genre_success:
        log_debug(f"Updated genre but title update failed for: {file_path}")
        return False
    else:
        log_debug(f"Failed to update file: {file_path}")
        return False
