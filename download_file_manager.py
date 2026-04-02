#!/usr/bin/env python3
"""
Download File Manager
Handles individual file matching, metadata updates, and copying for downloaded tracks.
Integrates with MusicBrainz data stored in the queue to update file metadata before copying.
"""

import os
import shutil
import logging
import re
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

_UUID_RE = re.compile(
    r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
)


def _is_uuid_like(value):
    if not value:
        return False
    return bool(_UUID_RE.match(str(value).strip()))


def _resolve_release_mbid_for_copy(queue_item, metadata):
    """Best-effort MBID fallback so per-track copies keep album identity cohesive."""
    existing = (metadata.get('release_mbid') or '').strip()
    if _is_uuid_like(existing):
        return existing

    release_source = str(queue_item.get('release_source') or '').strip().lower()
    release_id = (queue_item.get('release_id') or '').strip()
    if release_source in {'musicbrainz', 'mb'} and _is_uuid_like(release_id):
        return release_id

    conn = None
    try:
        from helpers.db_utils import get_db_connection

        conn = get_db_connection()
        cursor = conn.cursor()
        placeholder = '%s'

        import_group = (queue_item.get('import_group') or '').strip()
        if import_group:
            cursor.execute(
                f"""
                SELECT COALESCE(NULLIF(TRIM(CAST(release_mbid AS TEXT)), ''), NULLIF(TRIM(CAST(release_id AS TEXT)), '')) AS mbid
                FROM download_queue
                WHERE import_group = {placeholder}
                  AND COALESCE(NULLIF(TRIM(CAST(release_mbid AS TEXT)), ''), NULLIF(TRIM(CAST(release_id AS TEXT)), '')) IS NOT NULL
                ORDER BY id DESC
                LIMIT 25
                """,
                (import_group,),
            )
            for row in cursor.fetchall() or []:
                mbid = (row[0] if isinstance(row, (tuple, list)) else row.get('mbid')) if row else None
                mbid = str(mbid or '').strip()
                if _is_uuid_like(mbid):
                    return mbid

        artist = (metadata.get('album_artist') or metadata.get('artist') or '').strip()
        album = (metadata.get('album') or '').strip()
        if artist and album:
            cursor.execute(
                f"""
                SELECT
                    COALESCE(
                        MAX(NULLIF(TRIM(CAST(musicbrainz_album_mbid AS TEXT)), '')),
                        MAX(NULLIF(TRIM(CAST(musicbrainz_albumid AS TEXT)), ''))
                    ) AS mbid
                FROM tracks
                WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER({placeholder})
                  AND LOWER(COALESCE(album, '')) = LOWER({placeholder})
                """,
                (artist, album),
            )
            row = cursor.fetchone()
            if row:
                mbid = (row[0] if isinstance(row, (tuple, list)) else row.get('mbid'))
                mbid = str(mbid or '').strip()
                if _is_uuid_like(mbid):
                    return mbid
    except Exception as e:
        logger.debug(f"Could not resolve fallback release MBID for copy: {e}")
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass

    return ''


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
        
        cover_art_data = None
        cover_art_mime = 'image/jpeg'
        cover_art_url = metadata.get('cover_art_url')
        if cover_art_url:
            try:
                import requests
                art_resp = requests.get(cover_art_url, timeout=10)
                if art_resp.status_code == 200 and art_resp.content:
                    cover_art_data = art_resp.content
                    cover_art_mime = art_resp.headers.get('Content-Type', cover_art_mime) or cover_art_mime
            except Exception as art_err:
                logger.debug(f"Could not fetch cover art for metadata update: {art_err}")

        if ext == '.mp3':
            from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TDRC, TRCK, TPOS, TCON, TCOM, TSRC, APIC, TXXX
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

            if metadata.get('genre'):
                audio.tags['TCON'] = TCON(encoding=3, text=[str(metadata['genre'])])

            if metadata.get('composer'):
                audio.tags['TCOM'] = TCOM(encoding=3, text=[str(metadata['composer'])])

            if metadata.get('isrc'):
                audio.tags['TSRC'] = TSRC(encoding=3, text=[str(metadata['isrc'])])

            _release_mbid = (metadata.get('release_mbid') or '').strip()
            if _release_mbid:
                _del = [k for k in list(audio.tags.keys())
                        if k.startswith('TXXX:') and k[5:].lower().replace(' ', '').replace('_', '') == 'musicbrainzalbumid']
                for _k in _del:
                    audio.tags.delall(_k)
                audio.tags.add(TXXX(encoding=3, desc='MUSICBRAINZ ALBUM ID', text=[_release_mbid]))
            _recording_mbid = (metadata.get('recording_mbid') or '').strip()
            if _recording_mbid:
                _del = [k for k in list(audio.tags.keys())
                        if k.startswith('TXXX:') and k[5:].lower().replace(' ', '').replace('_', '') == 'musicbrainztrackid']
                for _k in _del:
                    audio.tags.delall(_k)
                audio.tags.add(TXXX(encoding=3, desc='MUSICBRAINZ TRACK ID', text=[_recording_mbid]))

            if cover_art_data:
                audio.tags['APIC'] = APIC(
                    encoding=3,
                    mime=cover_art_mime,
                    type=3,
                    desc='Cover',
                    data=cover_art_data,
                )
            
            audio.save()
            logger.info(f"✅ Updated MP3 metadata: {file_path}")
            return True
        
        elif ext == '.flac':
            from mutagen.flac import FLAC, Picture
            
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

            if metadata.get('genre'):
                audio['genre'] = [str(metadata['genre'])]

            if metadata.get('composer'):
                audio['composer'] = [str(metadata['composer'])]

            if metadata.get('isrc'):
                audio['isrc'] = [str(metadata['isrc'])]

            _release_mbid = (metadata.get('release_mbid') or '').strip()
            if _release_mbid:
                audio['musicbrainz_albumid'] = [_release_mbid]
            _recording_mbid = (metadata.get('recording_mbid') or '').strip()
            if _recording_mbid:
                audio['musicbrainz_trackid'] = [_recording_mbid]

            if cover_art_data:
                picture = Picture()
                picture.type = 3
                picture.mime = cover_art_mime
                picture.desc = 'Cover'
                picture.data = cover_art_data
                audio.clear_pictures()
                audio.add_picture(picture)
            
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
        from download_queue_manager import (
            _normalize_album_artist_for_path,
            _read_track_file_name_format,
            _sanitize_path_component,
        )

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

        ext = metadata.get('ext', '.mp3')
        if not ext or not ext.startswith('.'):
            orig_path = metadata.get('source_file_path')
            if orig_path:
                ext = os.path.splitext(orig_path)[1].lower() or '.mp3'
            else:
                ext = '.mp3'
            logger.warning(f"[FILENAME] Missing or invalid extension, defaulting to {ext}")

        file_name_format = _read_track_file_name_format()
        format_vars = {
            'track_number': track_num,
            'artist': _sanitize_path_component(artist) or 'Unknown Artist',
            'album_artist': _sanitize_path_component(_normalize_album_artist_for_path(album_artist)) or 'Unknown Artist',
            'title': _sanitize_path_component(title) or 'Unknown Title',
            'album': _sanitize_path_component(album) or 'Unknown Album',
            'year': year or 'Unknown',
        }
        fallback_rel = (
            f"{format_vars['album_artist']}/{format_vars['year']} - {format_vars['album']}/"
            f"{format_vars['track_number']}. {format_vars['artist']} - {format_vars['title']}"
        )

        try:
            relative_path = file_name_format.format(**format_vars)
        except Exception:
            relative_path = fallback_rel

        if not isinstance(relative_path, str) or not relative_path.strip():
            relative_path = fallback_rel

        relative_path = relative_path.strip().replace('\\', '/').lstrip('/')
        safe_parts = []
        for part in relative_path.split('/'):
            clean = _sanitize_path_component(part)
            if clean and clean not in ('.', '..'):
                safe_parts.append(clean)

        if not safe_parts:
            safe_parts = [
                format_vars['album_artist'],
                f"{format_vars['year']} - {format_vars['album']}",
                f"{format_vars['track_number']}. {format_vars['artist']} - {format_vars['title']}",
            ]

        safe_parts[-1] = f"{safe_parts[-1]}{ext}"
        target_path = os.path.join(music_dir, *safe_parts)

        target_dir = os.path.dirname(target_path)
        os.makedirs(target_dir, exist_ok=True)
        
        # Handle duplicate filenames
        if os.path.exists(target_path):
            filename = os.path.basename(target_path)
            base, ext = os.path.splitext(filename)
            counter = 1
            while os.path.exists(os.path.join(target_dir, f"{base}_{counter}{ext}")):
                counter += 1
            target_path = os.path.join(target_dir, f"{base}_{counter}{ext}")
        
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
        
        source_ext = os.path.splitext(source_file_path)[1].lower()

        # Keep conversion behavior aligned with the queue move/import flow so
        # manual per-track copies do not bypass FLAC -> MP3 settings.
        convert_requested = False
        queue_id = queue_item.get('id')
        transfer_download_to_music = None
        _apply_release_year_mtime = None
        try:
            from download_queue_manager import (
                _apply_release_year_mtime,
                _read_download_conversion_settings,
                transfer_download_to_music,
            )
            conversion_settings = _read_download_conversion_settings()
            convert_requested = bool(
                conversion_settings.get('enabled')
                and conversion_settings.get('mode') == 'flac_to_mp3'
                and source_ext == '.flac'
            )
        except Exception as conv_cfg_err:
            logger.debug(f"Could not load download conversion settings; falling back to direct copy: {conv_cfg_err}")

        target_ext = '.mp3' if convert_requested else source_ext

        # Prepare metadata for update
        metadata = {
            'title': queue_item.get('title', 'Unknown'),
            'artist': queue_item.get('artist', 'Unknown Artist'),
            'album_artist': queue_item.get('album_artist') or queue_item.get('artist', 'Unknown Artist'),
            'album': queue_item.get('album', 'Unknown Album'),
            'year': queue_item.get('year', 'Unknown'),
            'track_number': queue_item.get('track_number'),
            'disc_number': queue_item.get('disc_number'),
            'genre': queue_item.get('genres') or queue_item.get('genre'),
            'composer': queue_item.get('composer'),
            'isrc': queue_item.get('isrc'),
            'cover_art_url': queue_item.get('cover_art_url'),
            'release_mbid': queue_item.get('release_mbid') or queue_item.get('release_id'),
            'recording_mbid': queue_item.get('recording_mbid'),
            'ext': target_ext,
            'source_file_path': source_file_path
        }

        # Ensure album MBID is present so Navidrome groups all copied tracks as one release.
        resolved_release_mbid = _resolve_release_mbid_for_copy(queue_item, metadata)
        if resolved_release_mbid:
            metadata['release_mbid'] = resolved_release_mbid
        
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
        
        # Import file using the same conversion-aware transfer helper as the
        # queue processor. This converts FLACs to MP3 when configured instead
        # of blindly copying the original FLAC into /music.
        if convert_requested and transfer_download_to_music is not None:
            transfer_result = transfer_download_to_music(source_file_path, target_path, queue_id=queue_id)
            if not transfer_result.get('success'):
                return {
                    'success': False,
                    'target_path': None,
                    'error': transfer_result.get('error') or 'FLAC conversion failed',
                    'metadata_updated': metadata_updated
                }

            target_path = transfer_result.get('target_path') or target_path

            # Re-apply tags to the converted MP3 to ensure all fields, artwork,
            # and writer data are present on the final library file.
            metadata['ext'] = os.path.splitext(target_path)[1].lower() or '.mp3'
            final_metadata_updated = update_file_metadata(target_path, metadata)
            metadata_updated = metadata_updated or final_metadata_updated
            logger.info(f"✅ Converted/imported file: {source_file_path} → {target_path}")
        else:
            shutil.copy2(source_file_path, target_path)
            logger.info(f"✅ Copied file: {source_file_path} → {target_path}")

        # Set file mtime to the release year from MusicBrainz so the library
        # reflects the original release date rather than the copy date.
        try:
            if _apply_release_year_mtime is None:
                from download_queue_manager import _apply_release_year_mtime
            _apply_release_year_mtime(target_path, metadata.get('year'), queue_id=queue_id)
        except Exception:
            pass
        
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
