#!/usr/bin/env python3
"""
Metadata Tag Manager - Handles reading, updating, and writing metadata tags to files.

This module manages all Navidrome-sourced metadata tags:
- Reading tags from MP3/FLAC files
- Updating tags in the database
- Writing tags back to MP3/FLAC files
- Bulk updates for albums
- Conflict detection between album_artist and albumartist fields
"""

import os
import json
import logging
from typing import Optional, Dict, List, Any
from pathlib import Path

# Metadata reading library (supports multiple formats)
try:
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, TPE2, TCON, TRCK, TPOS, TDRC, TCOM, COMM, TXXX, APIC
    _MUTAGEN_AVAILABLE = True
except ImportError:
    _MUTAGEN_AVAILABLE = False

try:
    from mutagen.flac import FLAC
    _FLAC_AVAILABLE = True
except ImportError:
    _FLAC_AVAILABLE = False

from .db_utils import get_db_connection, _is_postgres_connection


logger = logging.getLogger(__name__)

# Fields that can be edited
EDITABLE_FIELDS = {
    # Basic metadata
    "album", "artist", "title", "album_artist", "albumartist", 
    "albumartistsort", "artistsort",
    # Credits
    "arranger", "composer", "mixer", "producer", "writer", "performer",
    # Release info
    "label", "releasecountry", "releasestatus", "releasetype",
    "media", "barcode", "catalognumber", "asin",
    # Dates
    "year", "originalyear", "originaldate", "date",
    # Numbering
    "track_number", "tracktotal", "disc_number", "totaldiscs",
    # Content
    "genres", "work",
    # Technical
    "bpm", "isrc", "script",
    # MusicBrainz IDs
    "musicbrainz_albumartistid", "musicbrainz_albumid", "musicbrainz_albumtype",
    "musicbrainz_albumstatus", "musicbrainz_releasegroupid", "musicbrainz_releasetrackid",
    "musicbrainz_workid", "mbid",
}

# Fields that store JSON arrays
JSON_ARRAY_FIELDS = {
    "artists", "performer", "producer", "writer",
}

# Fields that are album-level (not per-track but same across album)
ALBUM_LEVEL_FIELDS = {
    "album", "label", "releasecountry", "releasestatus", "releasetype",
    "media", "barcode", "catalognumber", "asin", "year", "originalyear",
    "originaldate", "totaldiscs", "musicbrainz_albumid", "musicbrainz_albumtype",
    "musicbrainz_albumstatus", "musicbrainz_releasegroupid",
}

# Fields that can conflict and should be highlighted
CONFLICT_PRONE_FIELDS = {
    "album_artist": "albumartist",  # These can differ and indicate metadata issues
    "artist": "album_artist",  # Track artist vs album artist
}


def get_track_tags(track_id: str) -> Dict[str, Any]:
    """
    Get all editable metadata tags for a track from the database.
    
    Args:
        track_id: Track ID to retrieve
        
    Returns:
        Dictionary of tag fields and values
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        is_pg = _is_postgres_connection(conn)
        placeholder = "%s" if is_pg else "?"
        
        # Select editable fields
        fields = ", ".join(EDITABLE_FIELDS)
        cursor.execute(f"SELECT {fields} FROM tracks WHERE id = {placeholder}", (track_id,))
        result = cursor.fetchone()
        conn.close()
        
        if not result:
            return {}
        
        # Build dictionary
        tags = {}
        for field in EDITABLE_FIELDS:
            try:
                idx = list(EDITABLE_FIELDS).index(field)
                value = result[idx]
                
                # Parse JSON arrays
                if field in JSON_ARRAY_FIELDS and isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except (json.JSONDecodeError, TypeError):
                        value = []
                
                tags[field] = value
            except (IndexError, TypeError):
                tags[field] = None
        
        return tags
    except Exception as e:
        logger.error(f"Failed to get tags for track {track_id}: {e}")
        return {}


def get_album_tags(album: str, artist: str) -> Dict[str, Any]:
    """
    Get album-level metadata tags from the database.
    
    Retrieves album-level fields that are the same across all tracks in the album.
    
    Args:
        album: Album name
        artist: Artist name
        
    Returns:
        Dictionary of album-level tag fields and values
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get album-level fields (use first track as reference)
        fields = ", ".join(["COUNT(*) as track_count"] + list(ALBUM_LEVEL_FIELDS))
        cursor.execute(
            f"SELECT {fields} FROM tracks WHERE album = ? AND artist = ? LIMIT 1",
            (album, artist)
        )
        result = cursor.fetchone()
        conn.close()
        
        if not result:
            return {}
        
        tags = {"track_count": result[0]}
        for field in ALBUM_LEVEL_FIELDS:
            try:
                idx = list(ALBUM_LEVEL_FIELDS).index(field) + 1  # +1 for track_count
                value = result[idx]
                
                # Parse JSON arrays
                if field in JSON_ARRAY_FIELDS and isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except (json.JSONDecodeError, TypeError):
                        value = []
                
                tags[field] = value
            except (IndexError, TypeError):
                tags[field] = None
        
        return tags
    except Exception as e:
        logger.error(f"Failed to get album tags for {artist} - {album}: {e}")
        return {}


def check_field_conflicts(album: str, artist: str) -> Dict[str, List[str]]:
    """
    Check for conflicting metadata values within an album.
    
    Identifies fields that have different values for different tracks in the album,
    which indicates potential metadata issues.
    
    Args:
        album: Album name
        artist: Artist name
        
    Returns:
        Dictionary mapping field names to lists of different values found
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        conflicts = {}
        
        # Check album_artist vs albumartist
        cursor.execute(
            "SELECT DISTINCT album_artist, albumartist FROM tracks WHERE album = ? AND artist = ? ",
            (album, artist)
        )
        results = cursor.fetchall()
        
        if len(results) > 1:
            album_artists = {r[0] for r in results if r[0]}
            albumartists = {r[1] for r in results if r[1]}
            
            if len(album_artists) > 1:
                conflicts["album_artist"] = list(album_artists)
            if len(albumartists) > 1:
                conflicts["albumartist"] = list(albumartists)
            if album_artists and albumartists and album_artists != albumartists:
                conflicts["album_artist_vs_albumartist"] = {
                    "album_artist": list(album_artists),
                    "albumartist": list(albumartists)
                }
        
        # Check other album-level fields for conflicts
        for field in ["label", "releasecountry", "releasetype"]:
            cursor.execute(
                f"SELECT DISTINCT {field} FROM tracks WHERE album = ? AND artist = ? AND {field} IS NOT NULL AND {field} != ''",
                (album, artist)
            )
            distinct_values = [row[0] for row in cursor.fetchall()]
            if len(distinct_values) > 1:
                conflicts[field] = distinct_values
        
        conn.close()
        return conflicts
    except Exception as e:
        logger.error(f"Failed to check field conflicts for {artist} - {album}: {e}")
        return {}


def update_track_tags(track_id: str, tag_updates: Dict[str, Any]) -> bool:
    """
    Update metadata tags for a single track in the database.
    
    Args:
        track_id: Track ID to update
        tag_updates: Dictionary of field -> new value
        
    Returns:
        True if successful, False otherwise
    """
    try:
        # Validate and clean updates
        validated = {}
        for field, value in tag_updates.items():
            if field not in EDITABLE_FIELDS:
                logger.warning(f"Ignoring non-editable field: {field}")
                continue
            
            # Convert JSON arrays
            if field in JSON_ARRAY_FIELDS:
                if isinstance(value, list):
                    value = json.dumps(value)
                elif isinstance(value, str):
                    try:
                        json.loads(value)  # Validate it's valid JSON
                    except json.JSONDecodeError:
                        value = json.dumps([value])
            
            validated[field] = value
        
        if not validated:
            logger.warning(f"No valid fields to update for track {track_id}")
            return False
        
        # Build UPDATE statement with DB-aware placeholders
        from database_abstraction import is_postgres_connection
        conn = get_db_connection()
        is_pg = is_postgres_connection(conn)
        placeholder = "%s" if is_pg else "?"
        
        set_clause = ", ".join([f"{field} = {placeholder}" for field in validated.keys()])
        query = f"UPDATE tracks SET {set_clause} WHERE id = {placeholder}"
        
        cursor = conn.cursor()
        cursor.execute(query, list(validated.values()) + [track_id])
        conn.commit()
        conn.close()
        
        logger.info(f"Updated {len(validated)} fields for track {track_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to update tags for track {track_id}: {e}")
        return False


def update_album_tags(album: str, artist: str, tag_updates: Dict[str, Any], selected_tracks: Optional[List[str]] = None) -> int:
    """
    Update metadata tags for all tracks in an album with retry logic for database locks.
    
    Args:
        album: Album name
        artist: Artist name
        tag_updates: Dictionary of field -> new value
        selected_tracks: List of specific track IDs to update (None = all tracks in album)
        
    Returns:
        Number of tracks updated
    """
    import time
    import sqlite3
    
    try:
        # Validate and clean updates
        validated = {}
        for field, value in tag_updates.items():
            if field not in EDITABLE_FIELDS:
                logger.warning(f"Ignoring non-editable field: {field}")
                continue
            
            # Convert JSON arrays
            if field in JSON_ARRAY_FIELDS:
                if isinstance(value, list):
                    value = json.dumps(value)
                elif isinstance(value, str):
                    try:
                        json.loads(value)
                    except json.JSONDecodeError:
                        value = json.dumps([value])
            
            validated[field] = value
        
        if not validated:
            logger.warning(f"No valid fields to update for album {artist} - {album}")
            return 0
        
        # Build UPDATE statement with DB-aware placeholders
        from database_abstraction import is_postgres_connection
        conn = get_db_connection()
        is_pg = is_postgres_connection(conn)
        placeholder = "%s" if is_pg else "?"
        
        set_clause = ", ".join([f"{field} = {placeholder}" for field in validated.keys()])
        query_values = list(validated.values()) + [album, artist]
        
        if selected_tracks:
            # Update only selected tracks
            track_placeholders = ", ".join([placeholder for _ in selected_tracks])
            query = f"UPDATE tracks SET {set_clause} WHERE album = {placeholder} AND artist = {placeholder} AND id IN ({track_placeholders})"
            query_values.extend(selected_tracks)
        else:
            # Update all tracks in album
            query = f"UPDATE tracks SET {set_clause} WHERE album = {placeholder} AND artist = {placeholder}"
        
        # Retry logic for database locked errors
        max_retries = 3
        retry_delay = 0.5  # Start with 500ms
        
        for attempt in range(max_retries):
            conn = None
            try:
                cursor = conn.cursor()
                cursor.execute(query, query_values)
                updated_count = cursor.rowcount
                conn.commit()
                conn.close()
                
                logger.info(f"Updated {updated_count} tracks in album {artist} - {album}")
                return updated_count
                
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e):
                    if attempt < max_retries - 1:
                        logger.debug(f"Database locked while updating album tags, retrying ({attempt + 1}/{max_retries})...")
                        time.sleep(retry_delay)
                        retry_delay *= 2  # Exponential backoff: 0.5s, 1s, 2s
                    else:
                        logger.error(f"Failed to update album tags after {max_retries} attempts: database is locked")
                        raise
                else:
                    logger.error(f"Database error updating album tags: {e}")
                    raise
            except Exception as e:
                logger.error(f"Failed to update album tags for {artist} - {album}: {e}")
                raise
            finally:
                if conn:
                    try:
                        conn.close()
                    except:
                        pass
        
        return 0
    except Exception as e:
        logger.error(f"Failed to update album tags for {artist} - {album}: {e}")
        return 0


def write_tags_to_file(file_path: str, tags: Dict[str, Any]) -> bool:
    """
    Write metadata tags to an MP3 or FLAC file.
    
    Args:
        file_path: Path to audio file
        tags: Dictionary of tag names and values
        
    Returns:
        True if successful, False otherwise
    """
    if not os.path.exists(file_path):
        logger.error(f"File not found: {file_path}")
        return False
    
    file_ext = Path(file_path).suffix.lower()
    
    try:
        if file_ext in [".mp3"]:
            return _write_id3_tags(file_path, tags)
        elif file_ext in [".flac"]:
            return _write_flac_tags(file_path, tags)
        else:
            logger.warning(f"Unsupported file format: {file_ext}")
            return False
    except Exception as e:
        logger.error(f"Failed to write tags to {file_path}: {e}")
        return False


def _write_id3_tags(file_path: str, tags: Dict[str, Any]) -> bool:
    """Write ID3 tags to MP3 file."""
    if not _MUTAGEN_AVAILABLE:
        logger.error("Mutagen library not available for ID3 tag writing")
        return False
    
    try:
        audio = MP3(file_path, ID3=ID3)  # type: ignore[name-defined]
        if audio.tags is None:
            audio.add_tags()

        def _set_text_frame(frame_id: str, frame_cls, value: Any):
            if value is None or value == "":
                audio.tags.delall(frame_id)
                return
            text = str(value)
            audio.tags.delall(frame_id)
            audio.tags.add(frame_cls(encoding=3, text=[text]))

        for field, value in tags.items():
            if field == "title":
                _set_text_frame("TIT2", TIT2, value)
            elif field == "artist":
                _set_text_frame("TPE1", TPE1, value)
            elif field == "album":
                _set_text_frame("TALB", TALB, value)
            elif field == "album_artist":
                _set_text_frame("TPE2", TPE2, value)
            elif field == "composer":
                _set_text_frame("TCOM", TCOM, value)
            elif field == "writer":
                frame_key = "TXXX:WRITER"
                audio.tags.delall(frame_key)
                if value is not None and str(value).strip():
                    audio.tags.add(TXXX(encoding=3, desc="WRITER", text=[str(value)]))
            elif field == "track_number":
                _set_text_frame("TRCK", TRCK, value)
            elif field == "disc_number":
                _set_text_frame("TPOS", TPOS, value)
            elif field in ("year", "date"):
                _set_text_frame("TDRC", TDRC, value)
            elif field in ("genre", "genres"):
                if value is None or value == "":
                    audio.tags.delall("TCON")
                else:
                    if isinstance(value, list):
                        genre_values = [str(v).strip() for v in value if str(v).strip()]
                    else:
                        import re
                        genre_values = [g.strip() for g in re.split(r'[\\,;/]+', str(value)) if g.strip()]
                    audio.tags.delall("TCON")
                    if genre_values:
                        audio.tags.add(TCON(encoding=3, text=genre_values))
            elif field == "comment":
                audio.tags.delall("COMM")
                if value is not None and str(value).strip():
                    audio.tags.add(COMM(encoding=3, lang="eng", desc="", text=[str(value)]))
            elif field == "mbid":
                frame_key = "TXXX:MUSICBRAINZ TRACK ID"
                audio.tags.delall(frame_key)
                if value is not None and str(value).strip():
                    audio.tags.add(TXXX(encoding=3, desc="MUSICBRAINZ TRACK ID", text=[str(value)]))
            else:
                logger.debug(f"Skipping unmapped field for ID3: {field}")

        audio.save(v2_version=3)
        logger.info(f"Wrote ID3 tags to {file_path}")
        return True
    except Exception as e:
        logger.error(f"Failed to write ID3 tags to {file_path}: {e}")
        return False


def _write_flac_tags(file_path: str, tags: Dict[str, Any]) -> bool:
    """Write Vorbis tags to FLAC file."""
    if not _FLAC_AVAILABLE:
        logger.error("Mutagen library not available for FLAC tag writing")
        return False
    
    try:
        audio = FLAC(file_path)  # type: ignore[name-defined]
        
        # FLAC uses Vorbis comments; map common DB field names to canonical Vorbis keys.
        flac_field_map = {
            "title": "title",
            "artist": "artist",
            "album": "album",
            "album_artist": "albumartist",
            "composer": "composer",
            "writer": "lyricist",
            "track_number": "tracknumber",
            "disc_number": "discnumber",
            "year": "date",
            "date": "date",
            "genre": "genre",
            "genres": "genre",
            "comment": "comment",
            "mbid": "musicbrainz_trackid",
        }

        for field, value in tags.items():
            target_field = flac_field_map.get(field)
            if not target_field:
                logger.debug(f"Skipping unmapped field for FLAC: {field}")
                continue

            if value is None:
                if target_field in audio:
                    del audio[target_field]
                continue

            # Genres can be multi-valued in FLAC.
            if target_field == "genre":
                if isinstance(value, list):
                    genre_values = [str(v).strip() for v in value if str(v).strip()]
                else:
                    import re
                    genre_values = [g.strip() for g in re.split(r'[\\,;/]+', str(value)) if g.strip()]
                if genre_values:
                    audio[target_field] = genre_values
                elif target_field in audio:
                    del audio[target_field]
            else:
                audio[target_field] = [str(value)]
        
        audio.save()
        logger.info(f"Wrote FLAC tags to {file_path}")
        return True
    except Exception as e:
        logger.error(f"Failed to write FLAC tags to {file_path}: {e}")
        return False


def sync_track_tags_to_file(track_id: str) -> bool:
    """
    Sync database tags back to the audio file.
    
    Reads the track from database and writes all tags to the audio file.
    
    Args:
        track_id: Track ID to sync
        
    Returns:
        True if successful, False otherwise
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get file path and all tags in one query
        cursor.execute("""
            SELECT id, title, album, artist, album_artist, albumartist, composer, 
                   year, originalyear, track_number, disc_number, genres, 
                   comment, mbid, file_path
            FROM tracks 
            WHERE id = ?
        """, (track_id,))
        result = cursor.fetchone()
        conn.close()
        
        if not result:
            logger.warning(f"Track not found: {track_id}")
            return False
        
        # Extract file path
        file_path = result['file_path'] if isinstance(result, dict) else result[-1]
        
        if not file_path:
            logger.warning(f"No file path found for track {track_id}")
            return False
        
        # Handle relative paths from Navidrome - convert to absolute
        if file_path and not os.path.isabs(file_path):
            music_folder = os.environ.get("MUSIC_FOLDER", "/music")
            absolute_path = os.path.join(music_folder, file_path)
            if os.path.exists(absolute_path):
                file_path = absolute_path
                logger.debug(f"Converted relative path to absolute: {file_path}")
        
        # Check if file exists
        if not os.path.exists(file_path):
            logger.error(f"Audio file not found: {file_path}")
            return False
        
        # Prepare tags from database
        tags = {}
        field_mapping = {
            'title': 'title',
            'artist': 'artist',
            'album': 'album',
            'album_artist': 'album_artist',
            'albumartist': 'albumartist',
            'composer': 'composer',
            'year': 'year',
            'originalyear': 'originalyear',
            'track_number': 'track_number',
            'disc_number': 'disc_number',
            'genres': 'genres',
            'comment': 'comment',
            'mbid': 'mbid'
        }
        
        # Extract values from result
        for idx, field in enumerate(['id', 'title', 'album', 'artist', 'album_artist', 'albumartist', 
                                      'composer', 'year', 'originalyear', 'track_number', 'disc_number', 
                                      'genres', 'comment', 'mbid', 'file_path']):
            if field in field_mapping and idx < len(result):
                value = result[idx] if not isinstance(result, dict) else result[field]
                if value is not None and str(value).strip():
                    tags[field_mapping[field]] = value
        
        # If no tags were extracted, return False
        if not tags:
            logger.warning(f"No editable tags found for track {track_id}")
            return False
        
        # Write to file
        success = write_tags_to_file(file_path, tags)
        if success:
            logger.info(f"Successfully synced tags to file: {file_path}")
        else:
            logger.error(f"Failed to write tags to file: {file_path}")
        return success
        
    except Exception as e:
        logger.error(f"Failed to sync tags for track {track_id}: {e}")
        return False
