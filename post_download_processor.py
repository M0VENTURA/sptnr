#!/usr/bin/env python3
"""
Post-Download Processor
Automatically processes completed downloads from MusicBrainz/Discogs:
- Updates file metadata (track number, artist, album artist, year)
- Renames file to proper format: [track_number]. [artist] - [title].[ext]
- Moves file to proper folder: [album_artist]/[year] - [album]/
"""

import os
import shutil
import sqlite3
import logging
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


def update_file_metadata(file_path, metadata):
    """
    Update file metadata tags using mutagen
    
    Args:
        file_path: Path to audio file
        metadata: Dict with keys: track_number, artist, album_artist, album, year, title
    
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TDRC, TRCK
        from mutagen.mp3 import MP3
        from mutagen.flac import FLAC
        
        ext = os.path.splitext(file_path)[1].lower()
        
        if ext == '.mp3':
            # Update MP3 tags
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
            
            audio.save()
            logger.info(f"Updated MP3 metadata: {file_path}")
            return True
            
        elif ext == '.flac':
            # Update FLAC tags
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
                audio['date'] = [str(metadata['year'])]
            
            if metadata.get('track_number'):
                audio['tracknumber'] = [str(metadata['track_number'])]
            
            audio.save()
            logger.info(f"Updated FLAC metadata: {file_path}")
            return True
            
        else:
            logger.warning(f"Unsupported file format for metadata update: {ext}")
            return False
            
    except Exception as e:
        logger.error(f"Error updating file metadata for {file_path}: {e}")
        return False


def organize_via_beets(file_path):
    """
    Organize a file using beets (update mode).
    
    After post-download processor updates file metadata tags, 
    use beets to move the file to its final location based on 
    the metadata tags that were just written.
    
    Args:
        file_path: Path to audio file with updated tags
    
    Returns:
        dict: {'success': bool, 'target_path': str, 'error': str}
    """
    try:
        import subprocess
        from pathlib import Path
        
        # Get the directory containing the file
        file_dir = str(Path(file_path).parent)
        
        try:
            # Try to import BeetsAutoImporter
            from beets_auto_import import BeetsAutoImporter
            
            importer = BeetsAutoImporter()
            
            # Ensure we're using the update config for file moves
            importer.ensure_beets_config(use_update=True)
            
            logger.info(f"Running beets import (update mode) on {file_dir}")
            
            # Run beets with update config to organize the file
            cmd = [
                "beet",
                "-c", str(importer.beets_config),
                "import",
                file_dir
            ]
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode == 0:
                logger.info(f"Beets successfully organized file from {file_path}")
                
                # Query beets database to find the new location
                try:
                    import sqlite3
                    beets_conn = sqlite3.connect(str(importer.beets_db))
                    beets_cursor = beets_conn.cursor()
                    
                    # Find the file in beets database by path (before organization)
                    # Beets renames files, so we need to find the organized path
                    filename = Path(file_path).name
                    beets_cursor.execute("""
                        SELECT path FROM items WHERE path LIKE ?
                    """, (f"%{filename}%",))
                    
                    organized_row = beets_cursor.fetchone()
                    organized_path = organized_row[0] if organized_row else None
                    
                    beets_conn.close()
                    
                    if organized_path:
                        logger.info(f"File organized to: {organized_path}")
                        return {
                            'success': True,
                            'target_path': organized_path,
                            'error': None
                        }
                except Exception as e:
                    logger.warning(f"Could not determine organized path from beets DB: {e}")
                    # Still consider it success since beets completed
                    return {
                        'success': True,
                        'target_path': None,  # We don't have the exact path, but beets moved it
                        'error': None
                    }
            else:
                logger.error(f"Beets import failed: {result.stderr}")
                return {
                    'success': False,
                    'target_path': None,
                    'error': f"Beets failed: {result.stderr}"
                }
                
        except ImportError:
            logger.error("beets_auto_import module not available, cannot organize via beets")
            return {
                'success': False,
                'target_path': None,
                'error': "Beets integration not available"
            }
            
    except Exception as e:
        logger.error(f"Error organizing file via beets {file_path}: {e}")
        return {
            'success': False,
            'target_path': None,
            'error': str(e)
        }


def rename_and_move_file(file_path, metadata):
    """
    Fallback: Rename file and move to proper folder structure
    (used if beets is unavailable)
    
    Args:
        file_path: Current path to audio file
        metadata: Dict with keys: track_number, artist, album_artist, album, year, title
    
    Returns:
        dict: {'success': bool, 'target_path': str, 'error': str}
    """
    try:
        music_dir = get_music_dir()
        
        # Extract metadata with fallbacks - ensure proper string conversions
        track_number = str(metadata.get('track_number') or '00').zfill(2)
        artist = metadata.get('artist', 'Unknown Artist').strip()
        album_artist = metadata.get('album_artist', artist).strip()
        album = metadata.get('album', 'Unknown Album').strip()
        title = metadata.get('title', Path(file_path).stem).strip()
        year = str(metadata.get('year') or 'Unknown').strip()
        
        # Get file extension
        ext = os.path.splitext(file_path)[1]
        
        # Build directory structure: [album_artist]/[year] - [album]/
        artist_dir = os.path.join(music_dir, sanitize_filename(album_artist))
        album_dir = os.path.join(artist_dir, sanitize_filename(f"{year} - {album}"))
        
        # Create directories
        os.makedirs(album_dir, exist_ok=True)
        
        # Build filename: [track_number]. [artist] - [title].[ext]
        filename = sanitize_filename(f"{track_number}. {artist} - {title}{ext}")
        target_path = os.path.join(album_dir, filename)
        
        # Handle duplicate filenames
        if os.path.exists(target_path) and os.path.abspath(file_path) != os.path.abspath(target_path):
            base, extension = os.path.splitext(filename)
            counter = 1
            while os.path.exists(os.path.join(album_dir, f"{base}_{counter}{extension}")):
                counter += 1
            filename = f"{base}_{counter}{extension}"
            target_path = os.path.join(album_dir, filename)
        
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
        
        # Step 1: Update file metadata tags
        metadata_updated = update_file_metadata(file_path, metadata)
        if not metadata_updated:
            logger.warning(f"Queue {queue_id}: Failed to update file metadata")
            # Don't abort, still try to organize via beets with what we have
        
        # Step 2: Organize file via beets (using update mode to move files)
        result = organize_via_beets(file_path)
        
        if result['success']:
            target_path = result['target_path'] or file_path  # Use either organized path or original
            logger.info(f"Queue {queue_id}: Successfully processed - {target_path}")
            return {
                'success': True,
                'message': 'Successfully processed and organized via beets',
                'target_path': target_path
            }
        else:
            # Fallback to manual organization if beets unavailable
            logger.warning(f"Queue {queue_id}: Beets organization failed, falling back to manual move")
            result_fallback = rename_and_move_file(file_path, metadata)
            if result_fallback['success']:
                logger.info(f"Queue {queue_id}: Successfully organized via fallback - {result_fallback['target_path']}")
                return {
                    'success': True,
                    'message': 'Successfully processed (fallback organization)',
                    'target_path': result_fallback['target_path']
                }
            else:
                logger.error(f"Queue {queue_id}: Both beets and fallback failed - {result_fallback['error']}")
                return {
                    'success': False,
                    'message': result_fallback['error'],
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
