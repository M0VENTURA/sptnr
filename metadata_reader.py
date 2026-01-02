#!/usr/bin/env python3
"""
MP3 Metadata Reader - Extract common MP3 tag fields from files.
Uses mutagen library to read ID3v2 tags.
Reference: https://docs.mp3tag.de/mapping/
"""

import os
import sqlite3
from pathlib import Path
from mutagen.id3 import ID3
from mutagen.easyid3 import EasyID3
from mutagen.mp3 import MP3

# Common MP3tag.de mapping fields
MP3_FIELDS = {
    'title': 'TIT2',
    'artist': 'TPE1',
    'album': 'TALB',
    'date': 'TDRC',
    'genre': 'TCON',
    'album_artist': 'TPE2',
    'composer': 'TCOM',
    'track_number': 'TRCK',
    'album_type': 'TOFN',
    'comment': 'COMM',
    'copyright': 'TCOP',
    'publisher': 'TPUB',
    'bpm': 'TBPM',
    'language': 'TLAN',
}


def read_mp3_metadata(file_path):
    """
    Read MP3 metadata from file using mutagen.
    Returns a dict with common fields.
    
    Args:
        file_path: Path to MP3 file
        
    Returns:
        dict: Metadata fields or empty dict if file not found/readable
    """
    metadata = {}
    
    if not file_path or not os.path.exists(file_path):
        return metadata
    
    try:
        # Try to read ID3 tags
        try:
            audio = ID3(file_path)
            
            # Extract common fields from ID3
            if 'TIT2' in audio:  # Title
                metadata['title'] = str(audio['TIT2'].text[0]) if audio['TIT2'].text else ''
            if 'TPE1' in audio:  # Artist
                metadata['artist'] = str(audio['TPE1'].text[0]) if audio['TPE1'].text else ''
            if 'TALB' in audio:  # Album
                metadata['album'] = str(audio['TALB'].text[0]) if audio['TALB'].text else ''
            if 'TPE2' in audio:  # Album Artist
                metadata['album_artist'] = str(audio['TPE2'].text[0]) if audio['TPE2'].text else ''
            if 'TCOM' in audio:  # Composer
                metadata['composer'] = str(audio['TCOM'].text[0]) if audio['TCOM'].text else ''
            if 'TDRC' in audio:  # Date/Year
                metadata['date'] = str(audio['TDRC'].text[0]) if audio['TDRC'].text else ''
            if 'TRCK' in audio:  # Track Number
                metadata['track_number'] = str(audio['TRCK'].text[0]) if audio['TRCK'].text else ''
            if 'TCON' in audio:  # Genre
                metadata['genre'] = str(audio['TCON'].text[0]) if audio['TCON'].text else ''
            if 'TBPM' in audio:  # BPM
                metadata['bpm'] = str(audio['TBPM'].text[0]) if audio['TBPM'].text else ''
            if 'TLAN' in audio:  # Language
                metadata['language'] = str(audio['TLAN'].text[0]) if audio['TLAN'].text else ''
            if 'TCOP' in audio:  # Copyright
                metadata['copyright'] = str(audio['TCOP'].text[0]) if audio['TCOP'].text else ''
            if 'TPUB' in audio:  # Publisher
                metadata['publisher'] = str(audio['TPUB'].text[0]) if audio['TPUB'].text else ''
            
            # Get audio properties (duration, bitrate, sample rate)
            try:
                mp3_audio = MP3(file_path)
                metadata['duration_ms'] = mp3_audio.info.length * 1000 if hasattr(mp3_audio.info, 'length') else None
                metadata['bitrate'] = mp3_audio.info.bitrate if hasattr(mp3_audio.info, 'bitrate') else None
                metadata['sample_rate'] = mp3_audio.info.sample_rate if hasattr(mp3_audio.info, 'sample_rate') else None
                metadata['channels'] = mp3_audio.info.channels if hasattr(mp3_audio.info, 'channels') else None
            except:
                pass
        except Exception as e:
            # Fallback to EasyID3 if ID3 fails
            try:
                audio = EasyID3(file_path)
                for field, id3_key in MP3_FIELDS.items():
                    if field in audio:
                        values = audio[field]
                        metadata[field] = values[0] if isinstance(values, list) and values else str(values)
            except:
                pass
        
        # Add file info
        stat = os.stat(file_path)
        metadata['file_size'] = stat.st_size
        metadata['file_path'] = file_path
        
    except Exception as e:
        return metadata
    
    return metadata


def find_track_file(artist, album, title, music_root="/music"):
    """
    Attempt to locate an MP3 file in the music directory.
    Tries common path patterns.
    
    Args:
        artist: Artist name
        album: Album name
        title: Track title
        music_root: Root music directory
        
    Returns:
        str: Path to MP3 file or None
    """
    if not os.path.exists(music_root):
        return None
    
    # Try common path patterns
    patterns = [
        f"{music_root}/{artist}/{album}/{title}.mp3",
        f"{music_root}/{artist}/{album}/*.mp3",
        f"{music_root}/{artist}/**/{title}.mp3",
    ]
    
    for pattern in patterns:
        try:
            path = Path(pattern.replace('/**/', '/**/').replace('*.mp3', '*'))
            if '**' in pattern:
                files = list(Path(music_root).rglob('*.mp3'))
                for f in files:
                    if title.lower() in f.stem.lower() and artist.lower() in str(f).lower():
                        return str(f)
            else:
                base_path = pattern.replace('*.mp3', '')
                if os.path.isdir(base_path):
                    for file in os.listdir(base_path):
                        if file.endswith('.mp3'):
                            return os.path.join(base_path, file)
        except:
            pass
    
    return None


def get_track_metadata_from_db(track_id, db_path="/database/sptnr.db"):
    """
    Get track file path from database.
    
    Args:
        track_id: Track ID
        db_path: Path to database
        
    Returns:
        dict: Track info with file path
    """
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tracks WHERE id = ?", (track_id,))
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return dict(row)
    except:
        pass
    
    return {}


def aggregate_genres_from_tracks(artist_name, db_path="/database/sptnr.db"):
    """
    Aggregate unique genres from all tracks by an artist.
    
    Args:
        artist_name: Artist name
        db_path: Path to database
        
    Returns:
        list: Sorted list of unique genres
    """
    genres = set()
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT genres FROM tracks WHERE artist = ?", (artist_name,))
        rows = cursor.fetchall()
        conn.close()
        
        for row in rows:
            if row[0]:
                # genres field may be JSON list or comma-separated
                genre_str = row[0]
                if isinstance(genre_str, str):
                    # Try parsing as JSON first
                    try:
                        import json
                        genre_list = json.loads(genre_str)
                        if isinstance(genre_list, list):
                            genres.update(genre_list)
                    except:
                        # Fall back to comma-separated
                        for g in genre_str.split(','):
                            g = g.strip().strip('"\'[]')
                            if g:
                                genres.add(g)
    except:
        pass
    
    return sorted(list(genres))
