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

def _is_postgres_connection(conn):
    """Detect if connection is PostgreSQL."""
    try:
        import psycopg2
        return isinstance(conn, psycopg2.extensions.connection)
    except (ImportError, AttributeError):
        return False

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

# MusicBrainz-specific TXXX frame tags (ID3v2 user-defined text frames)
# These follow Navidrome's mapping conventions: https://github.com/navidrome/navidrome/blob/master/resources/mappings.yaml
# Comprehensive mapping of all MusicBrainz raw tags found in MP3 metadata
MB_TXXX_FIELDS = {
    # Artist IDs
    'musicbrainz_artistid': 'MUSICBRAINZ ARTIST ID',                           # Artist ID
    'musicbrainz_albumartistid': 'MUSICBRAINZ ALBUM ARTIST ID',                # Album artist ID
    
    # Album/Release IDs
    'musicbrainz_albumid': 'MUSICBRAINZ ALBUM ID',                             # Release ID
    'musicbrainz_releasegroupid': 'MUSICBRAINZ RELEASE GROUP ID',              # Release group ID
    
    # Track IDs
    'musicbrainz_trackid': 'MUSICBRAINZ TRACK ID',                             # Track/Recording ID
    'musicbrainz_releasetrackid': 'MUSICBRAINZ RELEASE TRACK ID',              # Track on specific release ID
    'musicbrainz_workid': 'MUSICBRAINZ WORK ID',                               # Musical work ID
    
    # Release metadata
    'musicbrainz_releasestatus': 'MUSICBRAINZ RELEASE STATUS',                 # Release status (Official, Promotion, Bootleg, etc.)
    'musicbrainz_releasetype': 'MUSICBRAINZ RELEASE TYPE',                     # Release type (Album, EP, Single, Compilation, etc.)
    'musicbrainz_releasecountry': 'MUSICBRAINZ RELEASE COUNTRY',               # Release country (2-letter code)
    
    # Legacy/Alternative field names (some tools use these)
    'musicbrainz_album_release_country': 'MUSICBRAINZ ALBUM RELEASE COUNTRY',  # Release country (2-letter code or full name)
    'musicbrainz_album_status': 'MUSICBRAINZ ALBUM STATUS',                    # Release status (Official, Promotion, Bootleg, etc.)
    'musicbrainz_album_type': 'MUSICBRAINZ ALBUM TYPE',                        # Release type (Album, EP, Single, etc.)
}


def extract_album_art_from_mp3(file_path):
    """
    Extract embedded album art from MP3 file.
    
    Args:
        file_path: Path to MP3 file
        
    Returns:
        bytes: Image data or None if no art found
    """
    if not file_path or not os.path.exists(file_path):
        return None
    
    try:
        audio = ID3(file_path)
        # Look for APIC (Attached Picture) frames
        for key in audio.keys():
            if key.startswith('APIC'):
                apic = audio[key]
                # Return the image data
                return apic.data
    except Exception as e:
        pass
    
    return None


def write_musicbrainz_tags_to_mp3(file_path, release_country=None, release_status=None, release_type=None):
    """
    Write MusicBrainz tags to MP3 file using TXXX (user-defined text frames).
    
    Args:
        file_path: Path to MP3 file
        release_country: Release country code or name (e.g., 'US', 'United States')
        release_status: Release status (e.g., 'Official', 'Promotion', 'Bootleg')
        release_type: Release type (e.g., 'Album', 'EP', 'Single')
        
    Returns:
        bool: True if successfully written, False otherwise
    """
    if not file_path or not os.path.exists(file_path):
        return False
    
    try:
        from mutagen.id3 import ID3, TXXX
        from mutagen.mp3 import MP3
        
        audio = MP3(file_path, ID3=ID3)
        
        # Create ID3 tags if they don't exist
        if audio.tags is None:
            audio.add_tags()
        
        # Write release country
        if release_country:
            audio.tags.add(TXXX(
                encoding=3,
                desc='MUSICBRAINZ ALBUM RELEASE COUNTRY',
                text=[release_country]
            ))
        
        # Write release status
        if release_status:
            audio.tags.add(TXXX(
                encoding=3,
                desc='MUSICBRAINZ ALBUM STATUS',
                text=[release_status]
            ))
        
        # Write release type
        if release_type:
            audio.tags.add(TXXX(
                encoding=3,
                desc='MUSICBRAINZ ALBUM TYPE',
                text=[release_type]
            ))
        
        # Save changes
        audio.save()
        return True
        
    except Exception as e:
        return False


def read_musicbrainz_tags_from_mp3(file_path):
    """
    Read ALL MusicBrainz TXXX tags from MP3 file.
    
    Args:
        file_path: Path to MP3 file
        
    Returns:
        dict: Dictionary with all available MusicBrainz tags found
        
    Example return:
        {
            'musicbrainz_artistid': 'xxxxx-xxxx-xxxx-xxxx-xxxxxx',
            'musicbrainz_trackid': 'xxxxx-xxxx-xxxx-xxxx-xxxxxx',
            'musicbrainz_releasetype': 'Album',
            'musicbrainz_releasestatus': 'Official',
            ...
        }
    """
    result = {}
    
    if not file_path or not os.path.exists(file_path):
        return result
    
    try:
        audio = ID3(file_path)
        
        # Look for all TXXX frames with MusicBrainz tags
        for key in audio.keys():
            if key.startswith('TXXX'):
                frame = audio[key]
                desc = frame.desc.upper() if hasattr(frame, 'desc') else ''
                text_value = frame.text[0] if frame.text else None
                
                # Map descriptors to field names using reverse lookup
                for field_name, field_desc in MB_TXXX_FIELDS.items():
                    if field_desc.upper() == desc and text_value:
                        result[field_name] = str(text_value)
                        break
    
    except Exception as e:
        pass
    
    return result


def _parse_number_tag(value):
    """
    Parse a track/disc number tag that may be in "X/Y" format (e.g. "7/11").
    Returns the integer track number (X), or None if value is empty or invalid.
    """
    if value is None or str(value).strip() == '':
        return None
    try:
        return int(str(value).split('/')[0].strip())
    except (ValueError, IndexError):
        return None


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
            if 'TPE1' in audio:  # Artist (track artist)
                metadata['artist'] = str(audio['TPE1'].text[0]) if audio['TPE1'].text else ''
            if 'TALB' in audio:  # Album
                metadata['album'] = str(audio['TALB'].text[0]) if audio['TALB'].text else ''
            if 'TPE2' in audio:  # Album Artist (preferred source for album artist)
                metadata['album_artist'] = str(audio['TPE2'].text[0]) if audio['TPE2'].text else ''
            if 'TCOM' in audio:  # Composer
                metadata['composer'] = str(audio['TCOM'].text[0]) if audio['TCOM'].text else ''
            if 'TDRC' in audio:  # Date/Year
                metadata['date'] = str(audio['TDRC'].text[0]) if audio['TDRC'].text else ''
            if 'TRCK' in audio:  # Track Number (may be "7/11" format)
                raw = str(audio['TRCK'].text[0]) if audio['TRCK'].text else ''
                metadata['track_number'] = _parse_number_tag(raw)
            if 'TPOS' in audio:  # Disc Number (may be "1/2" format)
                raw = str(audio['TPOS'].text[0]) if audio['TPOS'].text else ''
                metadata['disc_number'] = _parse_number_tag(raw)
            if 'TCON' in audio:  # Genre - can have multiple values
                # Handle both single TCON frame with multiple values and multiple TCON frames
                genre_list = []
                if audio['TCON'].text:
                    # TCON can contain multiple genres separated by null bytes or backslashes
                    for genre_item in audio['TCON'].text:
                        genre_str = str(genre_item)
                        # Split on common separators but keep individual genres
                        if '\\' in genre_str:
                            # Handle backslash-separated genres (ID3 format)
                            parts = [g.strip() for g in genre_str.split('\\') if g.strip()]
                            genre_list.extend(parts)
                        else:
                            genre_list.append(genre_str)
                
                # Join with double backslash for consistency
                metadata['genre'] = '\\'.join(genre_list) if genre_list else ''
            if 'TBPM' in audio:  # BPM
                metadata['bpm'] = str(audio['TBPM'].text[0]) if audio['TBPM'].text else ''
            if 'TLAN' in audio:  # Language
                metadata['language'] = str(audio['TLAN'].text[0]) if audio['TLAN'].text else ''
            if 'TCOP' in audio:  # Copyright
                metadata['copyright'] = str(audio['TCOP'].text[0]) if audio['TCOP'].text else ''
            if 'TPUB' in audio:  # Publisher
                metadata['publisher'] = str(audio['TPUB'].text[0]) if audio['TPUB'].text else ''
            
            # Extract TXXX (user-defined) frame for raw artists field (from Navidrome/beets)
            # This field contains featured artists, performers, and collaborators
            for key in audio.keys():
                if key.startswith('TXXX'):
                    frame = audio[key]
                    desc = frame.desc.upper() if hasattr(frame, 'desc') else ''
                    text_value = frame.text[0] if frame.text else None
                    
                    # Extract ARTISTS field (JSON array of artist names)
                    if 'ARTISTS' in desc and text_value:
                        metadata['artists_raw'] = str(text_value)
                    # Extract ALBUMARTIST field (raw from tags, may differ from TPE2)
                    elif 'ALBUMARTIST' in desc and text_value and 'album_artist' not in metadata:
                        metadata['album_artist'] = str(text_value)
                    # Extract PERFORMER field (could have multiple values)
                    elif 'PERFORMER' in desc and text_value:
                        metadata['performer_raw'] = str(text_value)
            
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
                        raw = values[0] if isinstance(values, list) and values else ''
                        if field in ('track_number', 'disc_number'):
                            metadata[field] = _parse_number_tag(raw)
                        else:
                            metadata[field] = raw
            except:
                pass
        
        # Add file info
        stat = os.stat(file_path)
        metadata['file_size'] = stat.st_size
        metadata['file_path'] = file_path
        
    except Exception as e:
        return metadata
    
    return metadata


def read_genres_from_mp3(file_path):
    """
    Read ALL genre tags from MP3 file, handling multiple TCON frames and multi-value frames.
    This reads raw ID3 tags, which may be more complete than what Navidrome returns.
    
    Args:
        file_path: Path to MP3 file
        
    Returns:
        str: Genre string with multiple genres separated by double backslash (e.g., "Genre1\\Genre2\\Genre3")
             Empty string if no genres found
    """
    if not file_path or not os.path.exists(file_path):
        return ""
    
    try:
        from mutagen.id3 import ID3
        audio = ID3(file_path)
        
        genre_list = []
        
        # Handle TCON (genre) frame - can have multiple frames with same ID
        if 'TCON' in audio:
            frame = audio['TCON']
            if hasattr(frame, 'text'):
                for text_item in frame.text:
                    genre_str = str(text_item).strip()
                    if genre_str:
                        # Split on backslashes if present (ID3v2 format stores multiple genres this way)
                        if '\\' in genre_str:
                            parts = [g.strip() for g in genre_str.split('\\') if g.strip()]
                            genre_list.extend(parts)
                        else:
                            genre_list.append(genre_str)
        
        # Return with double backslash separation (ID3 compatibility format)
        return '\\'.join(genre_list) if genre_list else ""
    
    except Exception as e:
        return ""


def find_track_file(artist, album, title, music_root="/music", timeout_seconds=5):
    """
    Attempt to locate an MP3 file in the music directory.
    Tries common path patterns with timeout protection.
    
    Args:
        artist: Artist name
        album: Album name
        title: Track title
        music_root: Root music directory
        timeout_seconds: Maximum time to search
        
    Returns:
        str: Path to MP3 file or None
    """
    import signal
    import time
    
    if not os.path.exists(music_root):
        return None
    
    start_time = time.time()
    
    # Try exact path first (fastest)
    exact_patterns = [
        f"{music_root}/{artist}/{album}/{title}.mp3",
        f"{music_root}/{artist} - {album}/{title}.mp3",
        f"{music_root}/{artist}/{album}/{artist} - {title}.mp3",
    ]
    
    for pattern in exact_patterns:
        if time.time() - start_time > timeout_seconds:
            return None
        if os.path.exists(pattern):
            return pattern
    
    # Try directory-based search (medium speed)
    try:
        album_dirs = [
            f"{music_root}/{artist}/{album}",
            f"{music_root}/{artist} - {album}",
            f"{music_root}/{artist}/{album.split(' - ')[-1] if ' - ' in album else album}",
        ]
        
        for album_dir in album_dirs:
            if time.time() - start_time > timeout_seconds:
                return None
            
            if os.path.isdir(album_dir):
                # List files in directory (limited)
                try:
                    files = os.listdir(album_dir)
                    for file in files[:100]:  # Limit to first 100 files
                        if time.time() - start_time > timeout_seconds:
                            return None
                        
                        if file.endswith('.mp3') and title.lower() in file.lower():
                            return os.path.join(album_dir, file)
                except:
                    pass
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
        is_pg = _is_postgres_connection(conn)
        placeholder = "%s" if is_pg else "?"
        cursor.execute(f"SELECT * FROM tracks WHERE id = {placeholder}", (track_id,))
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return dict(row)
    except:
        pass
    
    return {}


def write_genre_to_mp3(file_path, genres):
    """
    Write or append genre tag to MP3 file.
    
    Args:
        file_path: Path to MP3 file
        genres: Genre string or list of genres. Can be:
                - Comma-separated string: "Pop, Christmas"
                - Double-backslash separated string: "Pop\\\\Christmas" (ID3v2 format)
                - List of genres: ["Pop", "Christmas"]
        
    Returns:
        bool: True if successful, False otherwise
    """
    if not file_path or not os.path.exists(file_path):
        return False
    
    try:
        from mutagen.id3 import ID3, TCON
        from mutagen.mp3 import MP3
        
        # Convert genres to list, handling different input formats
        if isinstance(genres, str):
            # Check if it's double-backslash separated (ID3 format)
            if '\\\\' in genres:
                # Keep the double backslash format for ID3 tags
                genre_str = genres
            else:
                # Split on comma and reconstruct with double backslash
                genre_list = [g.strip() for g in genres.split(',') if g.strip()]
                genre_str = '\\'.join(genre_list)
        else:
            # It's a list, join with double backslash
            genre_str = '\\'.join(str(g).strip() for g in genres if g)
        
        # Load the MP3 file
        audio = MP3(file_path, ID3=ID3)
        
        # Create ID3 tags if they don't exist
        if audio.tags is None:
            audio.add_tags()
        
        # Set Genre tag (TCON frame in ID3v2)
        # Use the double backslash format for consistency with Navidrome/Subsonic
        audio.tags['TCON'] = TCON(encoding=3, text=[genre_str])
        
        # Save changes
        audio.save()
        return True
        
    except Exception as e:
        return False


def write_genre_to_flac(file_path, genres):
    """
    Write or append genre tag to FLAC file.
    
    Args:
        file_path: Path to FLAC file
        genres: Genre string or list of genres (e.g., "Pop, Christmas" or ["Pop", "Christmas"])
        
    Returns:
        bool: True if successful, False otherwise
    """
    if not file_path or not os.path.exists(file_path):
        return False
    
    try:
        from mutagen.flac import FLAC
        
        # Convert genres to list if string
        if isinstance(genres, str):
            genre_list = [g.strip() for g in genres.split(',') if g.strip()]
        else:
            genre_list = genres if isinstance(genres, list) else [genres]
        
        # Load the FLAC file
        audio = FLAC(file_path)
        
        # Set Genre tag (Vorbis comment)
        audio['genre'] = genre_list
        
        # Save changes
        audio.save()
        return True
        
    except Exception as e:
        return False


def write_genre_to_audio_file(file_path, genres):
    """
    Write genre tag to audio file (MP3 or FLAC).
    
    Args:
        file_path: Path to audio file
        genres: Genre string or list of genres (e.g., "Pop, Christmas" or ["Pop", "Christmas"])
        
    Returns:
        bool: True if successful, False otherwise
    """
    if not file_path:
        return False
    
    file_ext = Path(file_path).suffix.lower()
    
    if file_ext == '.mp3':
        return write_genre_to_mp3(file_path, genres)
    elif file_ext in ['.flac', '.fla']:
        return write_genre_to_flac(file_path, genres)
    else:
        return False


