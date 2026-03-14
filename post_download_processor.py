#!/usr/bin/env python3
"""
Post-Download Processor
Automatically processes completed downloads from MusicBrainz/Discogs:
- Converts FLAC files to 320kbps MP3 (via ffmpeg)
- Updates file metadata (track number, artist, album artist, year, disc number, album art)
- Renames file to proper format: [track_number]. [artist] - [title].[ext]
- Moves file to proper folder: [album_artist]/[year] - [album]/
- Handles duplicates by moving to Duplicates/ subfolder

Requirements:
- ffmpeg: Required for FLAC to MP3 conversion (optional if only processing MP3 files)
- mutagen: Required for embedding album art and updating metadata
"""

import os
import shutil
import sqlite3
import logging
import io
import requests
from pathlib import Path
from datetime import datetime

# Setup logging with fallback for when /config doesn't exist (e.g., in tests)
log_handlers = [logging.StreamHandler()]
try:
    log_handlers.append(logging.FileHandler("/config/post_download.log"))
except (FileNotFoundError, PermissionError):
    pass  # Fallback to StreamHandler only

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [Post-Download] %(levelname)s - %(message)s',
    handlers=log_handlers
)
logger = logging.getLogger(__name__)

def get_db():
    """Get database connection"""
    db_path = os.environ.get("DB_PATH", "/database/sptnr.db")
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def get_music_dir():
    """Get music directory path"""
    return os.environ.get("MUSIC_ROOT", "/music")


def get_downloads_dir():
    """Get downloads directory path"""
    return os.environ.get("DOWNLOADS_DIR", "/downloads")


def sanitize_filename(filename):
    """Remove/replace invalid filename characters"""
    invalid_chars = '<>:"|?*\\/'
    for char in invalid_chars:
        filename = filename.replace(char, '_')
    # Also remove leading/trailing dots and spaces
    filename = filename.strip('. ')
    return filename


def fetch_writer_credits(title, artist):
    """
    Fetch composer/writer/lyricist credits from MusicBrainz API.
    
    Args:
        title: Track title
        artist: Track artist
    
    Returns:
        dict with keys: 'composers', 'writers', 'lyricists' (each is a de-duplicated list)
        or empty dict if fetch fails
    """
    try:
        from api_clients.musicbrainz import MusicBrainzClient
        
        mb_client = MusicBrainzClient()
        
        # Fetch composer/writer/lyricist credits
        credits = mb_client.get_composers_for_track(title, artist)
        
        if not credits:
            return {}
        
        # MusicBrainz client returns combined list, but let's store it under 'composers'
        # since that's the most common role. We could further categorize using relationship type
        # but for now we'll treat all credits uniformly.
        result = {
            'composers': credits,
            'writers': [],      # Could be populated from more detailed relationship parsing
            'lyricists': []     # Could be populated from more detailed relationship parsing
        }
        
        logger.debug(f"[WRITER_CREDITS] Fetched {len(credits)} composer(s) for '{title}' by '{artist}'")
        return result
        
    except Exception as e:
        logger.debug(f"Could not fetch writer credits for '{title}' by '{artist}': {e}")
        return {}


def fetch_musicbrainz_release_metadata(release_id):
    """
    Fetch complete release metadata from MusicBrainz including disc numbers and cover art.
    
    Args:
        release_id: MusicBrainz release ID (UUID format)
    
    Returns:
        dict with keys:
            - release_title: Album title
            - release_year: Release year
            - artist: Album artist
            - disc_count: Total number of discs
            - cover_art: Binary image data or None
            - tracks: List of dicts with track info including disc/track numbers
    """
    try:
        from api_clients.musicbrainz import _USER_AGENT
        
        # Fetch release details with recordings and artist-credits
        mb_url = f"https://musicbrainz.org/ws/2/release/{release_id}?inc=recordings+artist-credits&fmt=json"
        
        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "application/json"
        }
        
        response = requests.get(mb_url, headers=headers, timeout=10)
        response.raise_for_status()
        mb_data = response.json()
        
        release_info = {
            'release_title': mb_data.get('title', 'Unknown Album'),
            'release_year': str(mb_data.get('date', ''))[:4] if mb_data.get('date') else '',
            'artist': '',
            'disc_count': len(mb_data.get('media', [])),
            'cover_art': None,
            'tracks': []
        }
        
        # Get album artist
        if mb_data.get('artist-credit'):
            artist_parts = []
            for credit in mb_data['artist-credit']:
                if isinstance(credit, dict):
                    artist_parts.append(credit.get('name', ''))
                else:
                    artist_parts.append(str(credit))
            release_info['artist'] = ' '.join(artist_parts).strip()
        
        # Extract track info from all media (discs)
        for media_idx, media in enumerate(mb_data.get('media', []), 1):
            disc_number = media_idx
            for track in media.get('tracks', []):
                recording = track.get('recording', {})
                # MusicBrainz returns duration in milliseconds; store in ms for consistency
                duration_ms = recording.get('length') or track.get('length')
                track_info = {
                    'disc_number': disc_number,
                    'track_number': track.get('position', 0),
                    'title': recording.get('title', 'Unknown'),
                    'artist': '',
                    'duration': int(duration_ms) if duration_ms is not None else None,
                }
                
                # Get track artist if different
                if recording.get('artist-credit'):
                    artist_parts = []
                    for credit in recording['artist-credit']:
                        if isinstance(credit, dict):
                            artist_parts.append(credit.get('name', ''))
                        else:
                            artist_parts.append(str(credit))
                    track_info['artist'] = ' '.join(artist_parts).strip()
                
                release_info['tracks'].append(track_info)
        
        # Try to fetch cover art from MusicBrainz
        try:
            cover_url = f"https://coverartarchive.org/release/{release_id}/front-500"
            cover_response = requests.get(cover_url, timeout=5)
            if cover_response.status_code == 200:
                release_info['cover_art'] = cover_response.content
                logger.debug(f"[MB_METADATA] Fetched cover art for release {release_id}")
        except Exception as e:
            logger.debug(f"Could not fetch cover art for release {release_id}: {e}")
        
        logger.info(f"[MB_METADATA] Fetched metadata for release {release_id}: "
                   f"{release_info['release_title']} by {release_info['artist']}")
        return release_info
        
    except Exception as e:
        logger.error(f"Error fetching MusicBrainz metadata for release {release_id}: {e}")
        return None


def update_file_metadata_with_albumart(file_path, metadata, cover_art_data=None, clear_existing_tags=True):
    """
    Update file metadata tags using mutagen, including album art and composer/writer/lyricist credits.
    
    For FLAC files: metadata is updated then file is converted to MP3 320kbps in rename_and_move_file()
    For MP3 files: metadata is updated and file is moved to destination
    
    Args:
        file_path: Path to audio file (MP3 or FLAC)
        metadata: Dict with keys: track_number, artist, album_artist, album, year, title, disc_number,
                                  composers (list), writers (list), lyricists (list)
        cover_art_data: Binary image data (JPG/PNG) to embed as album art
    
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TDRC, TRCK, TPOS, APIC, TXXX
        from mutagen.mp3 import MP3
        from mutagen.flac import FLAC
        from mutagen.flac import Picture
        
        ext = os.path.splitext(file_path)[1].lower()
        
        if ext == '.mp3':
            # Update MP3 tags
            audio = MP3(file_path, ID3=ID3)
            # Start from a clean tag slate so stale source tags do not leak through.
            if clear_existing_tags:
                try:
                    audio.delete(file_path)
                except Exception:
                    pass
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
                audio.tags['TDRC'] = TDRC(encoding=3, text=[str(metadata['year'])])
            
            if metadata.get('track_number'):
                audio.tags['TRCK'] = TRCK(encoding=3, text=[str(metadata['track_number'])])
            
            if metadata.get('disc_number'):
                audio.tags['TPOS'] = TPOS(encoding=3, text=[str(metadata['disc_number'])])
            
            # Add composer/writer/lyricist credits as TXXX frames
            composers = metadata.get('composers', [])
            writers = metadata.get('writers', [])
            lyricists = metadata.get('lyricists', [])
            
            if composers:
                composer_str = '; '.join(composers) if isinstance(composers, list) else str(composers)
                audio.tags['TXXX:Composer'] = TXXX(encoding=3, desc='Composer', text=[composer_str])
                logger.debug(f"[METADATA] Embedded {len(composers)} composer(s) in MP3: {file_path}")
            
            if writers:
                writer_str = '; '.join(writers) if isinstance(writers, list) else str(writers)
                audio.tags['TXXX:Writer'] = TXXX(encoding=3, desc='Writer', text=[writer_str])
                logger.debug(f"[METADATA] Embedded {len(writers)} writer(s) in MP3: {file_path}")
            
            if lyricists:
                lyricist_str = '; '.join(lyricists) if isinstance(lyricists, list) else str(lyricists)
                audio.tags['TXXX:Lyricist'] = TXXX(encoding=3, desc='Lyricist', text=[lyricist_str])
                logger.debug(f"[METADATA] Embedded {len(lyricists)} lyricist(s) in MP3: {file_path}")
            
            # Add album art if provided
            if cover_art_data:
                try:
                    audio.tags['APIC'] = APIC(
                        encoding=3,
                        mime='image/jpeg',
                        type=3,  # 3 = Cover front
                        desc=u'Cover',
                        data=cover_art_data
                    )
                    logger.debug(f"[METADATA] Embedded album art in MP3: {file_path}")
                except Exception as art_err:
                    logger.warning(f"[METADATA] Could not embed album art in MP3: {art_err}")
            
            audio.save()
            logger.info(f"Updated MP3 metadata: {file_path}")
            return True
            
        elif ext == '.flac':
            # Update FLAC tags
            audio = FLAC(file_path)

            # Remove existing Vorbis comments and embedded pictures first.
            if clear_existing_tags:
                try:
                    audio.clear()
                    audio.clear_pictures()
                except Exception:
                    pass
            
            if metadata.get('title'):
                audio['title'] = [metadata['title']]
            
            if metadata.get('artist'):
                audio['artist'] = [metadata['artist']]
            
            if metadata.get('album_artist'):
                audio['albumartist'] = [metadata['album_artist']]
            
            if metadata.get('album'):
                audio['album'] = [metadata['album']]
            
            if metadata.get('year'):
                audio['date'] = [str(metadata['year'])]
            
            if metadata.get('track_number'):
                audio['tracknumber'] = [str(metadata['track_number'])]
            
            if metadata.get('disc_number'):
                audio['discnumber'] = [str(metadata['disc_number'])]
            
            # Add composer/writer/lyricist credits as Vorbis comments
            composers = metadata.get('composers', [])
            writers = metadata.get('writers', [])
            lyricists = metadata.get('lyricists', [])
            
            if composers:
                # For FLAC, store as multi-value field
                audio['composer'] = composers if isinstance(composers, list) else [str(composers)]
                logger.debug(f"[METADATA] Embedded {len(composers)} composer(s) in FLAC: {file_path}")
            
            if writers:
                audio['writer'] = writers if isinstance(writers, list) else [str(writers)]
                logger.debug(f"[METADATA] Embedded {len(writers)} writer(s) in FLAC: {file_path}")
            
            if lyricists:
                audio['lyricist'] = lyricists if isinstance(lyricists, list) else [str(lyricists)]
                logger.debug(f"[METADATA] Embedded {len(lyricists)} lyricist(s) in FLAC: {file_path}")
            
            # Legacy: also store as single-value fields for compatibility with players
            if composers:
                audio['©cmp'] = ['; '.join(composers) if isinstance(composers, list) else str(composers)]
            if writers:
                audio['©wrt'] = ['; '.join(writers) if isinstance(writers, list) else str(writers)]
            
            # Add album art if provided
            if cover_art_data:
                try:
                    picture = Picture()
                    picture.data = cover_art_data
                    picture.mime = 'image/jpeg'
                    picture.type = 3  # 3 = Cover front
                    audio.add_picture(picture)
                    logger.debug(f"[METADATA] Embedded album art in FLAC: {file_path}")
                except Exception as art_err:
                    logger.warning(f"[METADATA] Could not embed album art in FLAC: {art_err}")
            
            audio.save()
            logger.info(f"Updated FLAC metadata: {file_path}")
            return True
            
        else:
            logger.warning(f"Unsupported file format for metadata update: {ext}")
            return False
            
    except Exception as e:
        logger.error(f"Error updating file metadata for {file_path}: {e}")
        return False


def update_file_metadata(file_path, metadata, clear_existing_tags=True):
    """
    Update file metadata tags using mutagen (backward compatibility wrapper).
    
    This is a wrapper around update_file_metadata_with_albumart that maintains
    backward compatibility for code that doesn't use album art.
    
    Args:
        file_path: Path to audio file (MP3 or FLAC)
        metadata: Dict with keys: track_number, artist, album_artist, album, year, title, disc_number
    
    Returns:
        bool: True if successful, False otherwise
    """
    return update_file_metadata_with_albumart(
        file_path,
        metadata,
        cover_art_data=None,
        clear_existing_tags=clear_existing_tags,
    )


def rename_and_move_file(file_path, metadata):
    """
    Rename file and move to proper folder structure
    FLAC files are automatically converted to 320kbps MP3
    Uses manual file operations without external dependencies
    
    Args:
        file_path: Current path to audio file
        metadata: Dict with keys: track_number, artist, album_artist, album, year, title, disc_number
    
    Returns:
        dict: {'success': bool, 'target_path': str, 'error': str}
    """
    try:
        music_dir = get_music_dir()
        
        # Get file extension
        ext = os.path.splitext(file_path)[1].lower()
        
        # Convert FLAC to MP3 320kbps if needed
        if ext == '.flac':
            logger.info(f"Converting FLAC to 320kbps MP3: {file_path}")
            converted_path = convert_flac_to_mp3(file_path)
            if not converted_path:
                return {
                    'success': False,
                    'target_path': None,
                    'error': 'FLAC to MP3 conversion failed'
                }
            file_path = converted_path
            ext = '.mp3'
            logger.info(f"Conversion complete: {file_path}")
        
        # Extract metadata with fallbacks - ensure proper string conversions
        track_number = str(metadata.get('track_number') or '00').zfill(2)
        artist = metadata.get('artist', 'Unknown Artist').strip()
        album_artist = metadata.get('album_artist', artist).strip()
        album = metadata.get('album', 'Unknown Album').strip()
        title = metadata.get('title', Path(file_path).stem).strip()
        year = str(metadata.get('year') or 'Unknown').strip()
        
        # Build directory structure: [album_artist]/[year] - [album]/
        artist_dir = os.path.join(music_dir, sanitize_filename(album_artist))
        album_dir = os.path.join(artist_dir, sanitize_filename(f"{year} - {album}"))
        
        # Create directories
        os.makedirs(album_dir, exist_ok=True)
        
        # Build filename: [track_number]. [artist] - [title].[ext]
        filename = sanitize_filename(f"{track_number}. {artist} - {title}{ext}")
        target_path = os.path.join(album_dir, filename)
        
        # Handle duplicate filenames - move to Duplicates subfolder
        if os.path.exists(target_path) and os.path.abspath(file_path) != os.path.abspath(target_path):
            # Create Duplicates subfolder within the album
            duplicates_dir = os.path.join(album_dir, "Duplicates")
            os.makedirs(duplicates_dir, exist_ok=True)
            
            # Find next available duplicate number
            base, extension = os.path.splitext(filename)
            counter = 1
            target_path = os.path.join(duplicates_dir, f"{base}_{counter}{extension}")
            while os.path.exists(target_path):
                counter += 1
                target_path = os.path.join(duplicates_dir, f"{base}_{counter}{extension}")
            
            filename = f"{base}_{counter}{extension}"
            logger.info(f"Duplicate detected - will save to Duplicates subfolder: {filename}")
        
        # Move file
        if os.path.abspath(file_path) != os.path.abspath(target_path):
            shutil.move(file_path, target_path)
            logger.info(f"Moved: {file_path} -> {target_path}")
        else:
            logger.info(f"File already in correct location: {target_path}")
        
        return {
            'success': True,
            'target_path': target_path,
            'error': None
        }
        
    except Exception as e:
        logger.error(f"Error renaming/moving file {file_path}: {e}")
        return {
            'success': False,
            'target_path': None,
            'error': str(e)
        }


def convert_flac_to_mp3(flac_path, bitrate='320k'):
    """
    Convert FLAC file to MP3 using ffmpeg
    
    Args:
        flac_path: Path to FLAC file
        bitrate: Target bitrate (default: 320k for 320kbps)
    
    Returns:
        str: Path to converted MP3 file, or None if conversion failed
    """
    try:
        import subprocess
        
        if not os.path.exists(flac_path):
            logger.error(f"FLAC file not found: {flac_path}")
            return None
        
        # Create output path (same directory, .mp3 extension)
        mp3_path = os.path.splitext(flac_path)[0] + '.mp3'
        
        # Check if ffmpeg is available
        try:
            subprocess.run(['ffmpeg', '-version'], capture_output=True, timeout=5, check=True)
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            logger.error("ffmpeg not found or not working - cannot convert FLAC to MP3")
            return None
        
        # Use ffmpeg to convert: FLAC -> MP3 at 320kbps
        # -i: input file
        # -b:a: audio bitrate
        # -q:a: quality (0 is best when using explicit bitrate)
        # -y: overwrite output file without asking
        cmd = [
            'ffmpeg',
            '-i', flac_path,
            '-b:a', bitrate,
            '-q:a', '0',
            '-v', 'error',  # Only show errors
            '-y',  # Overwrite without asking
            mp3_path
        ]
        
        logger.info(f"Running ffmpeg conversion: FLAC -> MP3 (bitrate: {bitrate})")
        result = subprocess.run(cmd, capture_output=True, timeout=300, text=True)
        
        if result.returncode != 0:
            logger.error(f"ffmpeg conversion failed: {result.stderr}")
            return None
        
        if not os.path.exists(mp3_path):
            logger.error(f"Conversion succeeded but output file not found: {mp3_path}")
            return None
        
        # Verify output file has reasonable size (at least 100KB)
        file_size = os.path.getsize(mp3_path)
        if file_size < 102400:  # 100KB
            logger.warning(f"Converted MP3 seems too small ({file_size} bytes), possible conversion issue")
        
        logger.info(f"✓ FLAC converted to MP3: {mp3_path} ({file_size / 1024 / 1024:.1f} MB)")
        
        # Delete original FLAC file
        try:
            os.remove(flac_path)
            logger.info(f"✓ Deleted original FLAC: {flac_path}")
        except Exception as e:
            logger.warning(f"Could not delete original FLAC file: {e}")
        
        return mp3_path
        
    except subprocess.TimeoutExpired:
        logger.error(f"FLAC to MP3 conversion timed out (>300s): {flac_path}")
        return None
    except Exception as e:
        logger.error(f"Error converting FLAC to MP3: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None


def process_completed_queue_item(queue_item):
    """
    Process a completed queue item with MusicBrainz/Discogs metadata
    
    Args:
        queue_item: Dict representing download_queue row
    
    Returns:
        dict: {'success': bool, 'message': str, 'target_path': str}
    """
    try:
        queue_id = queue_item['id']
        file_path = queue_item.get('file_path')
        
        # Check if file exists
        if not file_path or not os.path.exists(file_path):
            logger.warning(f"Queue {queue_id}: File not found at {file_path}")
            return {
                'success': False,
                'message': 'File not found',
                'target_path': None
            }
        
        # Check if we have metadata to process
        if not queue_item.get('release_source'):
            logger.debug(f"Queue {queue_id}: No release metadata, skipping post-processing")
            return {
                'success': False,
                'message': 'No metadata available',
                'target_path': None
            }
        
        # Build metadata dict
        metadata = {
            'track_number': queue_item.get('track_number'),
            'artist': queue_item.get('artist'),
            'album_artist': queue_item.get('album_artist') or queue_item.get('artist'),
            'album': queue_item.get('album'),
            'year': queue_item.get('year'),
            'title': queue_item.get('title')
        }
        
        logger.info(f"Queue {queue_id}: Processing with metadata from {queue_item.get('release_source')}")
        
        # Step 1: Update file metadata tags using mutagen
        metadata_updated = update_file_metadata(file_path, metadata)
        if not metadata_updated:
            logger.warning(f"Queue {queue_id}: Failed to update file metadata")
            # Don't abort, still try to organize the file
        
        # Step 2: Organize file using mutagen + manual rename/move
        result = rename_and_move_file(file_path, metadata)
        
        if result['success']:
            target_path = result['target_path']
            logger.info(f"Queue {queue_id}: Successfully processed and organized - {target_path}")
            return {
                'success': True,
                'message': 'Successfully processed and organized',
                'target_path': target_path
            }
        else:
            logger.error(f"Queue {queue_id}: Failed to organize file - {result['error']}")
            return {
                'success': False,
                'message': result['error'],
                'target_path': None
            }
            
    except Exception as e:
        logger.error(f"Error processing queue item {queue_item.get('id')}: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {
            'success': False,
            'message': str(e),
            'target_path': None
        }


def process_pending_completed_items(limit=10):
    """
    Find completed queue items with metadata and process them
    
    Args:
        limit: Max number of items to process in one batch
    
    Returns:
        dict: Statistics about processing
    """
    stats = {
        'processed': 0,
        'failed': 0,
        'skipped': 0,
        'errors': []
    }
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Find completed items with release metadata that haven't been organized yet
        cursor.execute("""
            SELECT * FROM download_queue
            WHERE status = 'completed'
            AND file_path IS NOT NULL
            AND release_source IS NOT NULL
            AND (imported_at IS NULL OR imported_at = '')
            ORDER BY updated_at ASC
            LIMIT ?
        """, (limit,))
        
        items = [dict(row) for row in cursor.fetchall()]
        
        if not items:
            logger.debug("No completed items with metadata to process")
            return stats
        
        logger.info(f"Found {len(items)} completed items with metadata to process")
        
        for item in items:
            queue_id = item['id']
            
            try:
                result = process_completed_queue_item(item)
                
                if result['success']:
                    # Update queue item status
                    cursor.execute("""
                        UPDATE download_queue
                        SET status = 'imported',
                            file_path = ?,
                            imported_at = CURRENT_TIMESTAMP,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """, (result['target_path'], queue_id))
                    conn.commit()
                    
                    stats['processed'] += 1
                    logger.info(f"Queue {queue_id}: Marked as imported")
                    
                elif result['message'] == 'No metadata available':
                    # Skip items without metadata
                    stats['skipped'] += 1
                    
                else:
                    # Failed to process
                    stats['failed'] += 1
                    stats['errors'].append(f"Queue {queue_id}: {result['message']}")
                    
            except Exception as e:
                logger.error(f"Error processing queue {queue_id}: {e}")
                stats['failed'] += 1
                stats['errors'].append(f"Queue {queue_id}: {str(e)}")
        
        conn.close()
        
        if stats['processed'] > 0:
            logger.info(f"Post-download processing complete: {stats['processed']} processed, {stats['failed']} failed, {stats['skipped']} skipped")
        
        return stats
        
    except Exception as e:
        logger.error(f"Error in process_pending_completed_items: {e}")
        import traceback
        logger.error(traceback.format_exc())
        stats['errors'].append(str(e))
        return stats


if __name__ == "__main__":
    logger.info("Running post-download processor...")
    stats = process_pending_completed_items()
    logger.info(f"Results: {stats}")
