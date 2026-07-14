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
    from mutagen.id3 import (
        ID3, TIT2, TPE1, TALB, TPE2, TCON, TRCK, TPOS, TDRC, TCOM, TBPM, COMM, TXXX, APIC,
        TMOO, TSOT, TSOA, TSOC, TSST, TPE3, TPE4, TIT1, TIT3, TKEY, TLAN, TPUB,
        TCOP, TENC, TSSE, TDRL, WOAR, USLT, TIPL, TSRC,
        TEXT as TTEXT,
    )
    _MUTAGEN_AVAILABLE = True
except ImportError:
    _MUTAGEN_AVAILABLE = False

try:
    from mutagen.flac import FLAC, Picture as FLACPicture
    _FLAC_AVAILABLE = True
except ImportError:
    _FLAC_AVAILABLE = False

from .db_utils import get_db_connection, _is_postgres_connection
from .metadata_reader import find_track_file


logger = logging.getLogger(__name__)

# Fields that can be edited
EDITABLE_FIELDS = {
    # Basic metadata
    "album", "artist", "title", "album_artist", "albumartist",
    "albumartistsort", "artistsort",
    # Sort keys
    "titlesort", "albumsort", "composersort", "lyricistsort",
    "artistssort", "albumartistssort",
    # Multi-value artist fields
    "artists", "albumartists",
    # Credits
    "arranger", "composer", "mixer", "producer", "writer", "performer",
    "conductor", "director", "djmixer", "engineer", "remixer", "lyricist",
    # Release info
    "label", "releasecountry", "releasestatus", "releasetype",
    "media", "barcode", "catalognumber", "asin",
    "recordlabel", "copyright", "releasedate",
    # Dates
    "year", "originalyear", "originaldate", "date",
    # Numbering
    "track_number", "tracktotal", "disc_number", "totaldiscs",
    # Content
    "genres", "work", "mood", "lyrics",
    "subtitle", "discsubtitle", "albumversion",
    "grouping", "movement", "movementname", "movementtotal",
    # Classical/Work
    "key", "language", "script",
    # Technical
    "bpm", "danceability", "isrc",
    "encodedby", "encodersettings", "website",
    "license", "explicitstatus",
    # ReplayGain / R128
    "replaygain_track_gain", "replaygain_track_peak",
    "replaygain_album_gain", "replaygain_album_peak",
    "r128_track_gain", "r128_album_gain",
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
        placeholder = "%s"
        
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
            f"SELECT {fields} FROM tracks WHERE album = %s AND artist = %s LIMIT 1",
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
            "SELECT DISTINCT album_artist, albumartist FROM tracks WHERE album = %s AND artist = %s ",
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
                f"SELECT DISTINCT {field} FROM tracks WHERE album = %s AND artist = %s AND {field} IS NOT NULL AND {field} != ''",
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
        placeholder = "%s"
        
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
        placeholder = "%s"
        
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
            try:
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute(query, query_values)
                updated_count = cursor.rowcount
                conn.commit()
                conn.close()
                
                logger.info(f"Updated {updated_count} tracks in album {artist} - {album}")
                return updated_count
                
            except Exception as e:
                if "database is locked" in str(e).lower():
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
    """Write ID3 tags to MP3 file.

    Some MP3 files trigger mutagen's "can't sync to MPEG frame" error when
    loaded via ``mutagen.mp3.MP3`` because the MPEG frame scanner can't locate
    a valid sync word (e.g. non-standard encoders, large padding blocks, or
    files whose MPEG audio starts unusually late).  The ID3 tag layer is still
    perfectly valid in those files, so we fall back to ``mutagen.id3.ID3``
    which skips MPEG frame validation and only reads/writes the ID3 header.
    """
    if not _MUTAGEN_AVAILABLE:
        logger.error("Mutagen library not available for ID3 tag writing")
        return False

    try:
        # --- load tag object -------------------------------------------------
        # Try the full MP3 loader first (validates MPEG framing).  On failure
        # fall back to raw ID3 access so that tag writes still succeed on files
        # with non-standard MPEG structures.
        tag_obj = None      # the ID3 tag dict we operate on
        _save_fn = None     # callable that persists changes to disk

        try:
            audio = MP3(file_path, ID3=ID3)  # type: ignore[name-defined]
            if audio.tags is None:
                audio.add_tags()
            tag_obj = audio.tags

            def _save_mp3():
                audio.save(v2_version=3)
            _save_fn = _save_mp3
        except Exception as _mp3_load_err:
            logger.debug(
                f"MP3 MPEG-frame load failed for {file_path} "
                f"({_mp3_load_err}); falling back to raw ID3 access"
            )
            try:
                tag_obj = ID3(file_path)  # type: ignore[name-defined]
            except Exception:
                # File has no ID3 tags yet — create an empty tag set.
                tag_obj = ID3()  # type: ignore[name-defined]

            _fp = file_path  # capture for the closure

            def _save_id3():
                tag_obj.save(_fp, v2_version=3)
            _save_fn = _save_id3

        # --- helper closures that operate on tag_obj -------------------------
        def _set_text_frame(frame_id: str, frame_cls, value: Any):
            if value is None or value == "":
                tag_obj.delall(frame_id)
                return
            text = str(value)
            tag_obj.delall(frame_id)
            tag_obj.add(frame_cls(encoding=3, text=[text]))

        def _norm_txxx_desc(desc: str) -> str:
            """Normalise a TXXX desc for comparison: lowercase, strip spaces/underscores/hyphens."""
            return desc.lower().replace(' ', '').replace('_', '').replace('-', '')

        def _clear_txxx_variants(normalized_target: str) -> None:
            """Remove every TXXX frame whose normalised desc matches *normalized_target*.

            This catches any capitalisation or separator variant written by Picard, beets,
            older versions of this code, or third-party tools before we write the canonical
            frame, ensuring exactly one value ends up in the file.
            """
            to_delete = [
                key for key in list(tag_obj.keys())
                if key.startswith('TXXX:') and _norm_txxx_desc(key[5:]) == normalized_target
            ]
            for key in to_delete:
                tag_obj.delall(key)

        # --- apply requested tag changes -------------------------------------
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
                tag_obj.delall(frame_key)
                if value is not None and str(value).strip():
                    tag_obj.add(TXXX(encoding=3, desc="WRITER", text=[str(value)]))
            elif field == "track_number":
                _set_text_frame("TRCK", TRCK, value)
            elif field == "disc_number":
                _set_text_frame("TPOS", TPOS, value)
            elif field in ("year", "date"):
                _set_text_frame("TDRC", TDRC, value)
            elif field in ("genre", "genres"):
                if value is None or value == "":
                    tag_obj.delall("TCON")
                else:
                    if isinstance(value, list):
                        genre_values = [str(v).strip() for v in value if str(v).strip()]
                    else:
                        import re
                        genre_values = [g.strip() for g in re.split(r'[\\,;/]+', str(value)) if g.strip()]
                    tag_obj.delall("TCON")
                    if genre_values:
                        tag_obj.add(TCON(encoding=3, text=genre_values))
            elif field == "mood":
                # Fix: use standard TMOO frame (Navidrome reads tmoo alias, not TXXX:MOOD)
                tag_obj.delall("TXXX:MOOD")
                tag_obj.delall("TMOO")
                if value is not None and str(value).strip():
                    tag_obj.add(TMOO(encoding=3, text=[str(value)]))
            elif field == "bpm":
                _set_text_frame("TBPM", TBPM, value)
            elif field == "danceability":
                frame_key = "TXXX:DANCEABILITY"
                tag_obj.delall(frame_key)
                if value is not None and str(value).strip():
                    tag_obj.add(TXXX(encoding=3, desc="DANCEABILITY", text=[str(value)]))
            elif field == "comment":
                tag_obj.delall("COMM")
                if value is not None and str(value).strip():
                    tag_obj.add(COMM(encoding=3, lang="eng", desc="", text=[str(value)]))
            elif field in ("mbid", "musicbrainz_trackid"):
                # Both field names refer to the recording UUID and map to the same TXXX frame.
                _clear_txxx_variants('musicbrainztrackid')
                if value is not None and str(value).strip():
                    tag_obj.add(TXXX(encoding=3, desc="MUSICBRAINZ TRACK ID", text=[str(value)]))
            elif field in ("musicbrainz_album_mbid", "musicbrainz_albumid"):
                _clear_txxx_variants('musicbrainzalbumid')
                if value is not None and str(value).strip():
                    tag_obj.add(TXXX(encoding=3, desc="MUSICBRAINZ ALBUM ID", text=[str(value)]))
            elif field == "musicbrainz_artistid":
                _clear_txxx_variants('musicbrainzartistid')
                if value is not None and str(value).strip():
                    tag_obj.add(TXXX(encoding=3, desc="MUSICBRAINZ ARTIST ID", text=[str(value)]))
            elif field == "musicbrainz_albumartistid":
                _clear_txxx_variants('musicbrainzalbumartistid')
                if value is not None and str(value).strip():
                    tag_obj.add(TXXX(encoding=3, desc="MUSICBRAINZ ALBUM ARTIST ID", text=[str(value)]))
            elif field == "musicbrainz_releasegroupid":
                _clear_txxx_variants('musicbrainzreleasegroupid')
                if value is not None and str(value).strip():
                    tag_obj.add(TXXX(encoding=3, desc="MUSICBRAINZ RELEASE GROUP ID", text=[str(value)]))
            elif field == "musicbrainz_releasetrackid":
                _clear_txxx_variants('musicbrainzreleasetrackid')
                if value is not None and str(value).strip():
                    tag_obj.add(TXXX(encoding=3, desc="MUSICBRAINZ RELEASE TRACK ID", text=[str(value)]))
            elif field == "musicbrainz_workid":
                _clear_txxx_variants('musicbrainzworkid')
                if value is not None and str(value).strip():
                    tag_obj.add(TXXX(encoding=3, desc="MUSICBRAINZ WORK ID", text=[str(value)]))
            elif field == "titlesort":
                _set_text_frame("TSOT", TSOT, value)
            elif field == "albumsort":
                _set_text_frame("TSOA", TSOA, value)
            elif field == "albumartistsort":
                # TSO2 is not a standard ID3v2.3 frame; use TXXX for broader compatibility
                frame_key = "TXXX:ALBUMARTISTSORT"
                tag_obj.delall(frame_key)
                if value is not None and str(value).strip():
                    tag_obj.add(TXXX(encoding=3, desc="ALBUMARTISTSORT", text=[str(value)]))
            elif field == "composersort":
                _set_text_frame("TSOC", TSOC, value)
            elif field == "lyricistsort":
                frame_key = "TXXX:lyricistsort"
                tag_obj.delall(frame_key)
                if value is not None and str(value).strip():
                    tag_obj.add(TXXX(encoding=3, desc="lyricistsort", text=[str(value)]))
            elif field == "artists":
                frame_key = "TXXX:ARTISTS"
                tag_obj.delall(frame_key)
                if value is not None and str(value).strip():
                    tag_obj.add(TXXX(encoding=3, desc="ARTISTS", text=[str(value)]))
            elif field == "artistssort":
                frame_key = "TXXX:artistssort"
                tag_obj.delall(frame_key)
                if value is not None and str(value).strip():
                    tag_obj.add(TXXX(encoding=3, desc="artistssort", text=[str(value)]))
            elif field == "albumartists":
                frame_key = "TXXX:ALBUM ARTISTS"
                tag_obj.delall(frame_key)
                if value is not None and str(value).strip():
                    tag_obj.add(TXXX(encoding=3, desc="ALBUM ARTISTS", text=[str(value)]))
            elif field == "albumartistssort":
                frame_key = "TXXX:albumartistssort"
                tag_obj.delall(frame_key)
                if value is not None and str(value).strip():
                    tag_obj.add(TXXX(encoding=3, desc="albumartistssort", text=[str(value)]))
            elif field == "conductor":
                _set_text_frame("TPE3", TPE3, value)
            elif field == "director":
                frame_key = "TXXX:DIRECTOR"
                tag_obj.delall(frame_key)
                if value is not None and str(value).strip():
                    tag_obj.add(TXXX(encoding=3, desc="DIRECTOR", text=[str(value)]))
            elif field == "djmixer":
                frame_key = "TXXX:DJMIXER"
                tag_obj.delall(frame_key)
                if value is not None and str(value).strip():
                    tag_obj.add(TXXX(encoding=3, desc="DJMIXER", text=[str(value)]))
            elif field == "engineer":
                frame_key = "TXXX:ENGINEER"
                tag_obj.delall(frame_key)
                if value is not None and str(value).strip():
                    tag_obj.add(TXXX(encoding=3, desc="ENGINEER", text=[str(value)]))
            elif field == "remixer":
                _set_text_frame("TPE4", TPE4, value)
            elif field == "lyricist":
                _set_text_frame("TEXT", TTEXT, value)
            elif field == "albumversion":
                frame_key = "TXXX:ALBUMVERSION"
                tag_obj.delall(frame_key)
                if value is not None and str(value).strip():
                    tag_obj.add(TXXX(encoding=3, desc="ALBUMVERSION", text=[str(value)]))
            elif field == "discsubtitle":
                _set_text_frame("TSST", TSST, value)
            elif field == "lyrics":
                tag_obj.delall("USLT")
                if value is not None and str(value).strip():
                    tag_obj.add(USLT(encoding=3, lang="eng", desc="", text=str(value)))
            elif field == "releasedate":
                _set_text_frame("TDRL", TDRL, value)
            elif field == "r128_album_gain":
                frame_key = "TXXX:R128_ALBUM_GAIN"
                tag_obj.delall(frame_key)
                if value is not None and str(value).strip():
                    tag_obj.add(TXXX(encoding=3, desc="R128_ALBUM_GAIN", text=[str(value)]))
            elif field == "r128_track_gain":
                frame_key = "TXXX:R128_TRACK_GAIN"
                tag_obj.delall(frame_key)
                if value is not None and str(value).strip():
                    tag_obj.add(TXXX(encoding=3, desc="R128_TRACK_GAIN", text=[str(value)]))
            elif field == "explicitstatus":
                frame_key = "TXXX:ITUNESADVISORY"
                tag_obj.delall(frame_key)
                if value is not None and str(value).strip():
                    tag_obj.add(TXXX(encoding=3, desc="ITUNESADVISORY", text=[str(value)]))
            elif field == "copyright":
                _set_text_frame("TCOP", TCOP, value)
            elif field == "encodedby":
                _set_text_frame("TENC", TENC, value)
            elif field == "encodersettings":
                _set_text_frame("TSSE", TSSE, value)
            elif field == "grouping":
                _set_text_frame("TIT1", TIT1, value)
            elif field == "key":
                _set_text_frame("TKEY", TKEY, value)
            elif field == "language":
                _set_text_frame("TLAN", TLAN, value)
            elif field == "license":
                frame_key = "TXXX:LICENSE"
                tag_obj.delall(frame_key)
                if value is not None and str(value).strip():
                    tag_obj.add(TXXX(encoding=3, desc="LICENSE", text=[str(value)]))
            elif field == "movementname":
                frame_key = "TXXX:MOVEMENTNAME"
                tag_obj.delall(frame_key)
                if value is not None and str(value).strip():
                    tag_obj.add(TXXX(encoding=3, desc="MOVEMENTNAME", text=[str(value)]))
            elif field == "movementtotal":
                frame_key = "TXXX:MOVEMENTTOTAL"
                tag_obj.delall(frame_key)
                if value is not None and str(value).strip():
                    tag_obj.add(TXXX(encoding=3, desc="MOVEMENTTOTAL", text=[str(value)]))
            elif field == "movement":
                frame_key = "TXXX:MOVEMENT"
                tag_obj.delall(frame_key)
                if value is not None and str(value).strip():
                    tag_obj.add(TXXX(encoding=3, desc="MOVEMENT", text=[str(value)]))
            elif field == "subtitle":
                _set_text_frame("TIT3", TIT3, value)
            elif field == "website":
                tag_obj.delall("WOAR")
                if value is not None and str(value).strip():
                    tag_obj.add(WOAR(url=str(value)))
            elif field == "recordlabel":
                _set_text_frame("TPUB", TPUB, value)
            elif field == "isrc":
                _set_text_frame("TSRC", TSRC, value)
            elif field == "replaygain_track_gain":
                frame_key = "TXXX:replaygain_track_gain"
                tag_obj.delall(frame_key)
                if value is not None and str(value).strip():
                    tag_obj.add(TXXX(encoding=3, desc="replaygain_track_gain", text=[str(value)]))
            elif field == "replaygain_track_peak":
                frame_key = "TXXX:replaygain_track_peak"
                tag_obj.delall(frame_key)
                if value is not None and str(value).strip():
                    tag_obj.add(TXXX(encoding=3, desc="replaygain_track_peak", text=[str(value)]))
            elif field == "replaygain_album_gain":
                frame_key = "TXXX:replaygain_album_gain"
                tag_obj.delall(frame_key)
                if value is not None and str(value).strip():
                    tag_obj.add(TXXX(encoding=3, desc="replaygain_album_gain", text=[str(value)]))
            elif field == "replaygain_album_peak":
                frame_key = "TXXX:replaygain_album_peak"
                tag_obj.delall(frame_key)
                if value is not None and str(value).strip():
                    tag_obj.add(TXXX(encoding=3, desc="replaygain_album_peak", text=[str(value)]))
            elif field == "cover_art_data":
                # value should be raw image bytes; cover_art_mime is read from the same tags dict
                if value is not None and isinstance(value, (bytes, bytearray)) and len(value) > 0:
                    mime = tags.get("cover_art_mime", "image/jpeg")
                    tag_obj.delall("APIC")
                    tag_obj.add(APIC(
                        encoding=3,
                        mime=mime,
                        type=3,  # 3 = Cover (front)
                        desc="Cover",
                        data=bytes(value),
                    ))
            elif field == "cover_art_mime":
                pass  # handled alongside cover_art_data above
            else:
                logger.debug(f"Skipping unmapped field for ID3: {field}")

        _save_fn()
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
            "bpm": "bpm",
            "danceability": "danceability",
            "genre": "genre",
            "genres": "genre",
            "mood": "mood",
            "comment": "comment",
            "mbid": "musicbrainz_trackid",
            "musicbrainz_trackid": "musicbrainz_trackid",
            "musicbrainz_album_mbid": "musicbrainz_albumid",
            # Both musicbrainz_album_mbid and musicbrainz_albumid are column names used
            # in different parts of the codebase for the same MBID; both map to the
            # canonical Vorbis comment key so only one value is written to the file.
            "musicbrainz_albumid": "musicbrainz_albumid",
            "musicbrainz_artistid": "musicbrainz_artistid",
            "musicbrainz_albumartistid": "musicbrainz_albumartistid",
            "musicbrainz_releasegroupid": "musicbrainz_releasegroupid",
            "musicbrainz_releasetrackid": "musicbrainz_releasetrackid",
            "musicbrainz_workid": "musicbrainz_workid",
            # Sort keys
            "titlesort": "titlesort",
            "albumsort": "albumsort",
            "artistsort": "artistsort",
            "composersort": "composersort",
            "albumartistsort": "albumartistsort",
            "lyricistsort": "lyricistsort",
            "artistssort": "artistssort",
            "albumartistssort": "albumartistssort",
            # Multi-value artist fields
            "artists": "artists",
            "albumartists": "albumartists",
            # Credits
            "conductor": "conductor",
            "director": "director",
            "djmixer": "djmixer",
            "engineer": "engineer",
            "remixer": "remixer",
            "lyricist": "lyricist",
            "performer": "performer",
            "arranger": "arranger",
            "mixer": "mixer",
            "producer": "producer",
            # Release info
            "label": "label",
            "releasecountry": "releasecountry",
            "releasestatus": "releasestatus",
            "releasetype": "releasetype",
            "media": "media",
            "barcode": "barcode",
            "catalognumber": "catalognumber",
            "asin": "asin",
            "recordlabel": "publisher",
            "copyright": "copyright",
            "releasedate": "releasedate",
            # Dates
            "originalyear": "originalyear",
            "originaldate": "originaldate",
            # Numbering
            "tracktotal": "tracktotal",
            "totaldiscs": "totaldiscs",
            # Content
            "work": "work",
            "lyrics": "lyrics",
            "subtitle": "subtitle",
            "discsubtitle": "discsubtitle",
            "albumversion": "albumversion",
            "grouping": "grouping",
            "movement": "movement",
            "movementname": "movementname",
            "movementtotal": "movementtotal",
            # Technical
            "isrc": "isrc",
            "key": "key",
            "language": "language",
            "script": "script",
            "encodedby": "encodedby",
            "encodersettings": "encodersettings",
            "website": "website",
            "license": "license",
            "explicitstatus": "itunesadvisory",
            # ReplayGain
            "replaygain_track_gain": "replaygain_track_gain",
            "replaygain_track_peak": "replaygain_track_peak",
            "replaygain_album_gain": "replaygain_album_gain",
            "replaygain_album_peak": "replaygain_album_peak",
            "r128_track_gain": "r128_track_gain",
            "r128_album_gain": "r128_album_gain",
        }

        for field, value in tags.items():
            # Handle cover art separately (not a Vorbis comment field)
            if field == "cover_art_data":
                if value is not None and isinstance(value, (bytes, bytearray)) and len(value) > 0:
                    mime = tags.get("cover_art_mime", "image/jpeg")
                    pic = FLACPicture()
                    pic.type = 3  # 3 = Cover (front)
                    pic.mime = mime
                    pic.desc = "Cover"
                    pic.data = bytes(value)
                    audio.clear_pictures()
                    audio.add_picture(pic)
                continue
            if field == "cover_art_mime":
                continue  # handled alongside cover_art_data above

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


def update_file_tags(file_path: str, tag_updates: Dict[str, Any]) -> bool:
    """
    Update a subset of metadata tags in an audio file.

    Validates inputs and delegates to write_tags_to_file.

    Args:
        file_path: Path to audio file
        tag_updates: Dictionary of tag names and updated values

    Returns:
        True if successful, False otherwise
    """
    if not file_path:
        logger.error("update_file_tags called with empty file_path")
        return False
    if not tag_updates:
        logger.warning(f"No tag updates provided for {file_path}")
        return False
    logger.debug(f"Updating tags for {file_path}: {list(tag_updates.keys())}")
    return write_tags_to_file(file_path, tag_updates)


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
        placeholder = "%s"
        
        # Get file path and all tags in one query
        cursor.execute(f"""
             SELECT id, title, album, artist, album_artist, albumartist, composer,
                   year, originalyear, track_number, disc_number, genres,
                 mood, bpm, danceability, comment, mbid, musicbrainz_album_mbid,
                 musicbrainz_albumid, musicbrainz_artistid, musicbrainz_albumartistid,
                 musicbrainz_releasegroupid, musicbrainz_releasetrackid, musicbrainz_workid,
                 file_path
            FROM tracks 
            WHERE id = {placeholder}
        """, (track_id,))
        result = cursor.fetchone()
        conn.close()
        
        if not result:
            logger.warning(f"Track not found: {track_id}")
            return False
        
        result_fields = [
            'id', 'title', 'album', 'artist', 'album_artist', 'albumartist',
            'composer', 'year', 'originalyear', 'track_number', 'disc_number',
            'genres', 'mood', 'bpm', 'danceability', 'comment', 'mbid', 'musicbrainz_album_mbid',
            'musicbrainz_albumid', 'musicbrainz_artistid', 'musicbrainz_albumartistid',
            'musicbrainz_releasegroupid', 'musicbrainz_releasetrackid', 'musicbrainz_workid',
            'file_path',
        ]

        # Helper to read from dict-like and tuple-like cursor rows.
        def _row_get(row: Any, field_name: str, index: int) -> Any:
            if isinstance(row, dict):
                return row.get(field_name)
            if hasattr(row, 'keys'):
                try:
                    return row[field_name]
                except Exception:
                    pass
            if index < len(row):
                return row[index]
            return None

        # Extract file path
        file_path = _row_get(result, 'file_path', len(result_fields) - 1)
        
        # Use metadata-based search when path is missing (e.g., legacy rows).
        if not file_path:
            title = _row_get(result, 'title', 1)
            album = _row_get(result, 'album', 2)
            artist = _row_get(result, 'artist', 3)
            album_artist = _row_get(result, 'album_artist', 4) or _row_get(result, 'albumartist', 5)
            music_folder = os.environ.get("MUSIC_FOLDER", "/music")

            # Try album artist first for compilation/Various Artists folder layouts.
            if album_artist and album and title:
                file_path = find_track_file(album_artist, album, title, music_root=music_folder, timeout_seconds=5)
            if not file_path and artist and album and title:
                file_path = find_track_file(artist, album, title, music_root=music_folder, timeout_seconds=5)

            if file_path:
                logger.debug(f"Resolved missing file path for track {track_id}: {file_path}")
            else:
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
            title = _row_get(result, 'title', 1)
            album = _row_get(result, 'album', 2)
            artist = _row_get(result, 'artist', 3)
            album_artist = _row_get(result, 'album_artist', 4) or _row_get(result, 'albumartist', 5)
            music_folder = os.environ.get("MUSIC_FOLDER", "/music")

            fallback_path = None
            if album_artist and album and title:
                fallback_path = find_track_file(album_artist, album, title, music_root=music_folder, timeout_seconds=5)
            if not fallback_path and artist and album and title:
                fallback_path = find_track_file(artist, album, title, music_root=music_folder, timeout_seconds=5)

            if fallback_path and os.path.exists(fallback_path):
                file_path = fallback_path
                logger.debug(f"Resolved moved file path for track {track_id}: {file_path}")
            else:
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
            'mood': 'mood',
            'bpm': 'bpm',
            'danceability': 'danceability',
            'comment': 'comment',
            'mbid': 'mbid',
            'musicbrainz_album_mbid': 'musicbrainz_album_mbid',
            'musicbrainz_albumid': 'musicbrainz_albumid',
            'musicbrainz_artistid': 'musicbrainz_artistid',
            'musicbrainz_albumartistid': 'musicbrainz_albumartistid',
            'musicbrainz_releasegroupid': 'musicbrainz_releasegroupid',
            'musicbrainz_releasetrackid': 'musicbrainz_releasetrackid',
            'musicbrainz_workid': 'musicbrainz_workid',
        }
        
        # Extract values from result
        for idx, field in enumerate(result_fields):
            if field not in field_mapping:
                continue
            value = _row_get(result, field, idx)
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
