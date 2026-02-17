#!/usr/bin/env python3
"""
Track Tag Editor - Update MP3/FLAC metadata and rename files
"""

import os
import re
import sqlite3
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

def update_track_file_tags(track_id: str, db_path: str, changes: dict) -> dict:
    """
    Update track file tags (title, artist, genres) and rename file if title changes.
    
    Args:
        track_id: Database track ID
        db_path: Path to SQLite database
        changes: Dict with keys 'title', 'artist', 'genres' (optional)
                 changes should already be validated
    
    Returns:
        Dict with success/error status and details
    """
    try:
        # Get current track info
        conn = sqlite3.connect(db_path, timeout=120.0)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("SELECT id, title, artist, genres, file_path, beets_path FROM tracks WHERE id = ?", (track_id,))
        row = cursor.fetchone()
        
        if not row:
            return {"success": False, "error": "Track not found"}
        
        track = dict(row)
        old_title = track['title']
        old_artist = track['artist']
        old_genres = track['genres']
        file_path = track['file_path'] or track['beets_path']
        
        if not file_path or not os.path.exists(file_path):
            return {"success": False, "error": f"File not found: {file_path}"}
        
        new_title = changes.get('title', old_title)
        new_artist = changes.get('artist', old_artist)
        new_genres = changes.get('genres', old_genres)
        
        # Update file tags
        tag_result = _update_file_tags(file_path, {
            'title': new_title,
            'artist': new_artist,
            'genres': new_genres
        })
        
        if not tag_result['success']:
            return tag_result
        
        # Rename file if title changed
        new_file_path = file_path
        if 'title' in changes and new_title != old_title:
            rename_result = _rename_track_file(file_path, old_title, new_title)
            if rename_result['success']:
                new_file_path = rename_result['new_path']
        
        # Update database
        cursor.execute("""
            UPDATE tracks SET 
            title = ?, artist = ?, genres = ?, file_path = ?
            WHERE id = ?
        """, (new_title, new_artist, new_genres, new_file_path, track_id))
        
        conn.commit()
        conn.close()
        
        return {
            "success": True,
            "track_id": track_id,
            "changes": {
                "title": new_title if 'title' in changes else None,
                "artist": new_artist if 'artist' in changes else None,
                "genres": new_genres if 'genres' in changes else None,
                "file_renamed": new_file_path != file_path
            }
        }
        
    except Exception as e:
        logger.error(f"Error updating track tags: {e}")
        return {"success": False, "error": str(e)}


def _update_file_tags(file_path: str, changes: dict) -> dict:
    """Update mutagen tags in MP3/FLAC file"""
    try:
        ext = os.path.splitext(file_path)[1].lower()
        
        if ext in ['.mp3']:
            return _update_mp3_tags(file_path, changes)
        elif ext in ['.flac']:
            return _update_flac_tags(file_path, changes)
        else:
            return {"success": False, "error": f"Unsupported file format: {ext}"}
            
    except Exception as e:
        logger.error(f"Error updating file tags: {e}")
        return {"success": False, "error": str(e)}


def _update_mp3_tags(file_path: str, changes: dict) -> dict:
    """Update ID3 tags in MP3 file"""
    try:
        from mutagen.id3 import ID3, TIT2, TPE1, TCON
        from mutagen.mp3 import MP3
        
        audio = MP3(file_path, ID3=ID3)
        if audio.tags is None:
            audio.add_tags()
        
        if 'title' in changes:
            audio.tags['TIT2'] = TIT2(encoding=3, text=[changes['title']])
        
        if 'artist' in changes:
            audio.tags['TPE1'] = TPE1(encoding=3, text=[changes['artist']])
        
        if 'genres' in changes:
            genre_str = changes['genres']
            audio.tags['TCON'] = TCON(encoding=3, text=[genre_str])
        
        audio.save()
        return {"success": True}
        
    except Exception as e:
        logger.error(f"Error updating MP3 tags: {e}")
        return {"success": False, "error": str(e)}


def _update_flac_tags(file_path: str, changes: dict) -> dict:
    """Update Vorbis tags in FLAC file"""
    try:
        from mutagen.flac import FLAC
        
        audio = FLAC(file_path)
        
        if 'title' in changes:
            audio['title'] = [changes['title']]
        
        if 'artist' in changes:
            audio['artist'] = [changes['artist']]
        
        if 'genres' in changes:
            # FLAC uses semicolon-separated genres in single tag
            genre_list = [g.strip() for g in changes['genres'].split('\\') if g.strip()]
            audio['genre'] = genre_list
        
        audio.save()
        return {"success": True}
        
    except Exception as e:
        logger.error(f"Error updating FLAC tags: {e}")
        return {"success": False, "error": str(e)}


def _rename_track_file(old_path: str, old_title: str, new_title: str) -> dict:
    """Rename track file based on title change"""
    try:
        directory = os.path.dirname(old_path)
        filename = os.path.basename(old_path)
        ext = os.path.splitext(filename)[1]
        
        # Replace old title with new title in filename
        # Try to find and replace the title in the filename
        new_filename = filename
        
        # Common patterns: "01 - Title.mp3", "Track Title.mp3", "01. Title - Artist.mp3"
        # Remove leading track numbers and clean up
        title_pattern = re.escape(old_title)
        new_filename = re.sub(title_pattern, new_title, filename, flags=re.IGNORECASE)
        
        # If no change (title not found in filename), use new title with number
        if new_filename == filename:
            # Extract track number if present
            match = re.match(r'^(\d+\s*[-.]?\s*)?', filename)
            if match:
                prefix = match.group(1) or ''
                new_filename = f"{prefix}{new_title}{ext}"
            else:
                new_filename = f"{new_title}{ext}"
        
        new_path = os.path.join(directory, new_filename)
        
        # Only rename if new name is different and doesn't exist
        if new_path != old_path and not os.path.exists(new_path):
            os.rename(old_path, new_path)
            return {"success": True, "new_path": new_path}
        elif os.path.exists(new_path):
            return {"success": False, "error": f"Target filename already exists: {new_filename}"}
        else:
            return {"success": True, "new_path": old_path}  # No change needed
            
    except Exception as e:
        logger.error(f"Error renaming track file: {e}")
        return {"success": False, "error": str(e)}


def remove_genre_from_track_title(title: str) -> str:
    """Remove (live) suffix from track title if genre 'live' is being removed"""
    if not title:
        return title
    
    # Remove patterns like " (live)" or " [live]" from end of title
    cleaned = re.sub(r'\s*[\(\[]live[\)\]]\s*$', '', title, flags=re.IGNORECASE)
    return cleaned.strip()
