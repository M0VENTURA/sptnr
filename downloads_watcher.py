#!/usr/bin/env python3
"""
Downloads Watcher - Monitors /downloads folder for new MP3 files,
extracts metadata, searches for better metadata online, and organizes
them into /Music with proper directory structure.
"""

import os
import shutil
import psycopg2.extras
import json
import time
import yaml
from datetime import datetime
from pathlib import Path
from helpers.metadata_reader import read_mp3_metadata
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("/config/downloads.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def resolve_downloads_dir():
    """Resolve downloads directory from config/env with safe fallback.
    Config file takes priority over environment variable."""
    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
    try:
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            configured = (cfg.get("downloads") or {}).get("folder")
            if configured and configured.strip():
                return os.path.normpath(configured.strip())
    except Exception as e:
        logger.warning(f"Could not read downloads folder from config: {e}")

    env_dir = os.environ.get("DOWNLOADS_DIR")
    if env_dir and env_dir.strip():
        return os.path.normpath(env_dir.strip())

    return "/downloads/Music"


def resolve_torrent_downloads_dir():
    """Resolve the qBittorrent-specific torrent downloads folder from config.

    Returns the configured ``qbittorrent.downloads_folder`` path when set,
    otherwise falls back to a ``torrents`` subdirectory of the main downloads
    folder so the watcher keeps working on existing deployments.
    """
    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
    try:
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            qbit_folder = cfg.get('qbittorrent', {}).get('downloads_folder', '').strip()
            if qbit_folder:
                return os.path.normpath(qbit_folder)
    except Exception as e:
        logger.warning(f"Could not read qbittorrent.downloads_folder from config: {e}")

    # Legacy fallback: torrents subfolder of the main downloads directory.
    return os.path.join(resolve_downloads_dir(), "torrents")


def get_downloads_dir():
    """Dynamically get downloads directory (re-evaluates on each call for config changes)."""
    return resolve_downloads_dir()


MUSIC_DIR = os.environ.get("MUSIC_ROOT", "/music")


def get_db():
    """Get PostgreSQL database connection."""
    try:
        from app import get_db as app_get_db
        return app_get_db()
    except ImportError:
        # Direct PostgreSQL connection if app not available
        conn = psycopg2.connect(
            host=os.environ.get("PG_HOST", "sptnr-postgres"),
            user=os.environ.get("PG_USER", "sptnr"),
            password=os.environ.get("PG_PASSWORD", ""),
            dbname=os.environ.get("PG_DATABASE", "sptnr"),
            port=int(os.environ.get("PG_PORT", "5432")),
            connect_timeout=10,
        )
        return conn


def _is_postgres_connection(conn):
    """Check if connection is PostgreSQL."""
    try:
        from app import _is_postgres_connection as app_is_postgres_connection
        return app_is_postgres_connection(conn)
    except ImportError:
        return True  # Default to PostgreSQL since this is now PostgreSQL-only

def sanitize_filename(filename):
    """Remove/replace invalid filename characters"""
    invalid_chars = '<>:"|?*'
    for char in invalid_chars:
        filename = filename.replace(char, '_')
    return filename

def extract_mp3_metadata(file_path):
    """Extract metadata from MP3 file"""
    try:
        return read_mp3_metadata(file_path)
    except Exception as e:
        logger.error(f"Error reading metadata from {file_path}: {e}")
        return {}

def determine_track_number(metadata):
    """Determine track number with disk prefix for multi-CD albums"""
    track_num = metadata.get('track', '0')
    disk_num = metadata.get('disk', '1')
    
    try:
        # Parse track number (may be "5/12" format)
        if isinstance(track_num, str) and '/' in track_num:
            track_num = track_num.split('/')[0]
        
        track_num = int(str(track_num).split('/')[0]) if track_num else 0
        disk_num = int(str(disk_num).split('/')[0]) if disk_num else 1
        
        if disk_num > 1:
            return f"{disk_num}{track_num:02d}"
        return f"{track_num:02d}"
    except:
        return "00"

def organize_file(file_path, metadata):
    """
    Organize file into /Music with structure:
    /Music/Artist Name/Release Year - Album Name/Track Number. Artist Name - Song Title.mp3
    """
    try:
        from download_queue_manager import (
            _normalize_album_artist_for_path,
            _read_track_file_name_format,
            _sanitize_path_component,
        )

        artist = metadata.get('artist', 'Unknown Artist').strip() or 'Unknown Artist'
        album_artist = metadata.get('album_artist', artist).strip() or artist
        album = metadata.get('album', 'Unknown Album').strip() or 'Unknown Album'
        title = metadata.get('title', Path(file_path).stem).strip() or Path(file_path).stem
        year = metadata.get('year', metadata.get('date', '')).strip()
        
        # Clean up year (just get first 4 digits if it's a date)
        if year and len(year) >= 4:
            year = year[:4]
        elif not year:
            year = 'Unknown'
        
        track_num = determine_track_number(metadata)
        
        file_name_format = _read_track_file_name_format()
        format_vars = {
            'track_number': track_num,
            'artist': _sanitize_path_component(artist) or 'Unknown Artist',
            'album_artist': _sanitize_path_component(_normalize_album_artist_for_path(album_artist)) or 'Unknown Artist',
            'title': _sanitize_path_component(title) or Path(file_path).stem,
            'album': _sanitize_path_component(album) or 'Unknown Album',
            'year': year or 'Unknown',
        }
        fallback_rel = (
            f"{format_vars['album_artist']}/{format_vars['year']} - {format_vars['album']}/"
            f"{format_vars['track_number']} - {format_vars['artist']} - {format_vars['title']}"
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
                f"{format_vars['track_number']} - {format_vars['artist']} - {format_vars['title']}",
            ]

        ext = os.path.splitext(file_path)[1].lower() or '.mp3'
        safe_parts[-1] = f"{safe_parts[-1]}{ext}"
        target_path = os.path.join(MUSIC_DIR, *safe_parts)
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
        
        # Move file
        shutil.move(file_path, target_path)
        logger.info(f"Moved: {file_path} -> {target_path}")
        
        return {
            'success': True,
            'target_path': target_path,
            'artist': artist,
            'album': album,
            'title': title,
            'year': year,
            'track_num': track_num
        }
    except Exception as e:
        logger.error(f"Error organizing file {file_path}: {e}")
        return {
            'success': False,
            'error': str(e)
        }

def track_exists_in_library(artist, album, title):
    """Check if a track with same artist/album/title exists in library (case-insensitive)"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT id FROM tracks 
            WHERE LOWER(COALESCE(album_artist, artist)) = LOWER(%s) 
            AND LOWER(album) = LOWER(%s)
            AND LOWER(title) = LOWER(%s)
            LIMIT 1
        """, (artist.strip(), album.strip(), title.strip()))
        
        exists = cursor.fetchone() is not None
        conn.close()
        return exists
    except Exception as e:
        logger.error(f"Error checking if track exists: {e}")
        return False


def find_active_queue_item(artist, album, title):
    """Find a queue item matching artist/album/title with an active status."""
    try:
        conn = get_db()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT * FROM download_queue
            WHERE LOWER(COALESCE(artist, '')) = LOWER(%s)
              AND LOWER(COALESCE(album, '')) = LOWER(%s)
              AND LOWER(COALESCE(title, '')) = LOWER(%s)
              AND status IN ('queued', 'searching', 'downloading')
            ORDER BY updated_at DESC
            LIMIT 1
        """, (artist.strip(), album.strip(), title.strip()))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        logger.debug(f"Error finding active queue item: {e}")
        return None


def _seq_ratio(a: str, b: str) -> float:
    """Simple similarity ratio fallback."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    try:
        from difflib import SequenceMatcher
        return SequenceMatcher(None, a.lower(), b.lower()).ratio()
    except Exception:
        return 0.0


def verify_metadata_matches_queue(file_path, queue_item):
    """
    Verify that a torrent file's metadata matches the expected queue item.
    Returns (ok: bool, reason: str).
    """
    metadata = extract_mp3_metadata(file_path) or {}
    file_artist = (metadata.get('artist') or '').strip()
    file_title = (metadata.get('title') or '').strip()
    if not file_artist or not file_title:
        return False, "unreadable metadata"

    queue_artist = (queue_item.get('artist') or '').strip()
    queue_title = (queue_item.get('title') or '').strip()
    if not queue_artist or not queue_title:
        return False, "missing queue metadata"

    # Title must be a strong fuzzy match
    title_score = _seq_ratio(file_title, queue_title)
    if title_score < 0.70:
        # Allow version-stripped fallback
        import re
        _ft = re.sub(r'\s*[\(\[][^\)\]]*[\)\]]\s*', ' ', file_title).strip()
        _qt = re.sub(r'\s*[\(\[][^\)\]]*[\)\]]\s*', ' ', queue_title).strip()
        if _ft and _qt:
            title_score = max(title_score, _seq_ratio(_ft, _qt))
        if title_score < 0.70:
            return False, f"title mismatch (score={title_score:.2f})"

    # Artist must at least be included in the file artist (handles featured artists)
    if queue_artist.lower() not in file_artist.lower():
        # Also allow fuzzy match as fallback
        artist_score = _seq_ratio(file_artist, queue_artist)
        if artist_score < 0.55:
            return False, f"artist mismatch (score={artist_score:.2f})"

    return True, "ok"


def copy_organized_file(file_path, metadata):
    """
    Compute target path and copy file there (leaving original intact).
    Returns dict like organize_file().
    """
    try:
        from download_queue_manager import (
            _normalize_album_artist_for_path,
            _read_track_file_name_format,
            _sanitize_path_component,
        )

        artist = metadata.get('artist', 'Unknown Artist').strip() or 'Unknown Artist'
        album_artist = metadata.get('album_artist', artist).strip() or artist
        album = metadata.get('album', 'Unknown Album').strip() or 'Unknown Album'
        title = metadata.get('title', Path(file_path).stem).strip() or Path(file_path).stem
        year = metadata.get('year', metadata.get('date', '')).strip()
        if year and len(year) >= 4:
            year = year[:4]
        elif not year:
            year = 'Unknown'
        track_num = determine_track_number(metadata)

        file_name_format = _read_track_file_name_format()
        format_vars = {
            'track_number': track_num,
            'artist': _sanitize_path_component(artist) or 'Unknown Artist',
            'album_artist': _sanitize_path_component(_normalize_album_artist_for_path(album_artist)) or 'Unknown Artist',
            'title': _sanitize_path_component(title) or Path(file_path).stem,
            'album': _sanitize_path_component(album) or 'Unknown Album',
            'year': year or 'Unknown',
        }
        fallback_rel = (
            f"{format_vars['album_artist']}/{format_vars['year']} - {format_vars['album']}/"
            f"{format_vars['track_number']} - {format_vars['artist']} - {format_vars['title']}"
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
                f"{format_vars['track_number']} - {format_vars['artist']} - {format_vars['title']}",
            ]

        ext = os.path.splitext(file_path)[1].lower() or '.mp3'
        safe_parts[-1] = f"{safe_parts[-1]}{ext}"
        target_path = os.path.join(MUSIC_DIR, *safe_parts)
        target_dir = os.path.dirname(target_path)
        os.makedirs(target_dir, exist_ok=True)

        if os.path.exists(target_path):
            filename = os.path.basename(target_path)
            base, ext = os.path.splitext(filename)
            counter = 1
            while os.path.exists(os.path.join(target_dir, f"{base}_{counter}{ext}")):
                counter += 1
            target_path = os.path.join(target_dir, f"{base}_{counter}{ext}")

        shutil.copy2(file_path, target_path)
        logger.info(f"Copied: {file_path} -> {target_path}")

        return {
            'success': True,
            'target_path': target_path,
            'artist': artist,
            'album': album,
            'title': title,
            'year': year,
            'track_num': track_num
        }
    except Exception as e:
        logger.error(f"Error copying organized file {file_path}: {e}")
        return {'success': False, 'error': str(e)}


def mark_queue_item_matched_from_torrent(queue_id, target_path):
    """Update a queue item to reflect that its torrent file has been matched and copied."""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE download_queue
            SET status = 'completed',
                file_path = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (target_path, queue_id))
        conn.commit()
        conn.close()
        logger.info(f"Queue item {queue_id} marked as completed from torrent match")
        return True
    except Exception as e:
        logger.error(f"Error marking queue item {queue_id} as matched: {e}")
        return False

def queue_incomplete_download(file_path, metadata):
    """Queue an incomplete download for retry"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        artist = metadata.get('artist', 'Unknown')
        album = metadata.get('album', 'Unknown')
        title = metadata.get('title', os.path.basename(file_path))
        
        # Check if already exists in library
        exists_in_library = 1 if track_exists_in_library(artist, album, title) else 0
        
        # Use PostgreSQL upsert
        cursor.execute("""
            INSERT INTO download_queue (
                file_path, found_filename, artist, album, title, duration,
                status, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(file_path) DO UPDATE SET
                found_filename = EXCLUDED.found_filename,
                artist = EXCLUDED.artist,
                album = EXCLUDED.album,
                title = EXCLUDED.title,
                duration = EXCLUDED.duration,
                status = EXCLUDED.status,
                updated_at = CURRENT_TIMESTAMP
        """, (
            file_path,
            os.path.basename(file_path),
            artist,
            album,
            title,
            metadata.get('duration', 0),
            'in_collection' if exists_in_library else 'unmatched'
        ))
        conn.commit()
        
        conn.close()
        logger.info(f"Queued incomplete download: {artist} - {title} (exists_in_library: {exists_in_library})")
        return True
    except Exception as e:
        if "already in queue" not in str(e):
            logger.error(f"Error queuing download: {e}")
        else:
            logger.info(f"File {file_path} already in queue")
        return False

def get_download_queue(status=None, limit=50, offset=0):
    """Get files from download queue with summary metadata."""
    try:
        conn = get_db()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        limit = max(int(limit), 0)
        offset = max(int(offset), 0)

        # Compute status counts (and total_count) efficiently.
        # When filtering by a specific status a simple COUNT(*) is cleaner than
        # a redundant GROUP BY.  When listing all statuses the GROUP BY is used
        # so total_count can be derived without an extra query.
        if status:
            cursor.execute("""
                SELECT COUNT(*) AS total
                FROM download_queue
                WHERE status = %s
            """, (status,))
            total_count = int((cursor.fetchone() or {}).get('total', 0))
            status_counts = {status: total_count}
        else:
            cursor.execute("""
                SELECT status, COUNT(*) AS count
                FROM download_queue
                GROUP BY status
            """)
            status_counts = {
                row['status']: int(row['count'])
                for row in cursor.fetchall()
                if row and row.get('status')
            }
            total_count = sum(status_counts.values())

        if status:
            cursor.execute("""
                SELECT * FROM download_queue 
                WHERE status = %s
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
            """, (status, limit, offset))
        else:
            cursor.execute("""
                SELECT * FROM download_queue 
                ORDER BY
                    CASE
                        WHEN status IN ('queued', 'searching', 'downloading') THEN 0
                        ELSE 1
                    END,
                    created_at DESC
                LIMIT %s OFFSET %s
            """, (limit, offset))
        
        rows = cursor.fetchall()
        conn.close()
        
        return {
            'queue': list(rows),
            'total_count': total_count,
            'status_counts': status_counts,
            'limit': limit,
            'offset': offset,
            'has_more': (offset + len(rows)) < total_count,
        }
    except Exception as e:
        logger.error(f"Error getting download queue: {e}")
        return {
            'queue': [],
            'total_count': 0,
            'status_counts': {},
            'limit': limit,
            'offset': offset,
            'has_more': False,
        }

def get_retry_queue(limit=50):
    """Get files queued for retry"""
    try:
        from datetime import datetime as dt, timedelta
        conn = get_db()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        now = dt.now().isoformat()
        
        cursor.execute("""
            SELECT * FROM download_queue 
            WHERE status = 'failed'
            AND (next_retry_at IS NULL OR next_retry_at <= %s)
            AND retry_count < max_retries
            ORDER BY next_retry_at ASC, created_at ASC
            LIMIT %s
        """, (now, limit))
        
        rows = cursor.fetchall()
        conn.close()
        
        # RealDictCursor already returns dict-like rows
        return list(rows)
    except Exception as e:
        logger.error(f"Error getting retry queue: {e}")
        return []

def get_download_queue_grouped(status=None, limit=50):
    """
    Get download queue items grouped by album for smart matching.
    
    Groups songs from the same album together, allowing UI to display them
    as album entries with expandable track lists instead of individual songs.
    
    Args:
        status: Filter by status (e.g., 'completed')
        limit: Maximum number of groups to return
        
    Returns:
        List of album groups with track counts and album metadata
    """
    try:
        conn = get_db()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # Get queue items
        if status:
            cursor.execute("""
                SELECT * FROM download_queue 
                WHERE status = %s
                ORDER BY artist ASC, album ASC, created_at ASC
            """, (status,))
        else:
            cursor.execute("""
                SELECT * FROM download_queue 
                ORDER BY artist ASC, album ASC, created_at ASC
            """)
        
        rows = cursor.fetchall()
        items = list(rows)
        conn.close()

        # Group by import_group first (if set), then by artist/album
        from collections import defaultdict
        groups = defaultdict(list)
        
        for item in items:
            import_group = item.get('import_group') or f"{item.get('artist', 'Unknown')}_{item.get('album', 'Unknown')}"
            groups[import_group].append(item)
        
        # Build album group results
        album_groups = []
        for group_key, tracks in groups.items():
            if not tracks:
                continue
            
            # Use first track's metadata for the group
            first_track = tracks[0]
            artist = first_track.get('artist', 'Unknown')
            album = first_track.get('album', 'Unknown')
            
            album_groups.append({
                'group_id': group_key,
                'artist': artist,
                'album': album,
                'track_count': len(tracks),
                'status': first_track.get('status'),
                'created_at': first_track.get('created_at'),
                'updated_at': first_track.get('updated_at'),
                'tracks': tracks
            })
        
        # Sort by artist, album, then creation date
        album_groups.sort(key=lambda x: (x['artist'].lower(), x['album'].lower(), x['created_at']))
        
        # Apply limit to groups
        return album_groups[:limit]
        
    except Exception as e:
        logger.error(f"Error getting grouped download queue: {e}")
        return []

def mark_download_as_failed(file_path, failure_reason):
    """Mark a download as failed and schedule retry"""
    try:
        from datetime import datetime as dt, timedelta
        conn = get_db()
        cursor = conn.cursor()
        
        next_retry = (dt.now() + timedelta(minutes=10)).isoformat()  # Retry in 10 minutes
        
        cursor.execute("""
            UPDATE download_queue 
            SET status = 'failed',
                retry_count = retry_count + 1,
                failure_reason = %s,
                last_failure_time = %s,
                next_retry_at = %s,
                updated_at = %s
            WHERE file_path = %s
        """, (
            failure_reason,
            dt.now().isoformat(),
            next_retry,
            dt.now().isoformat(),
            file_path
        ))
        
        conn.commit()
        conn.close()
        logger.info(f"Marked as failed with retry scheduled: {file_path}")
        return True
    except Exception as e:
        logger.error(f"Error marking download as failed: {e}")
        return False

def mark_download_as_successful(file_path):
    """Mark a download as successfully processed and remove from queue"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("""
            DELETE FROM download_queue WHERE file_path = %s
        """, (file_path,))
        
        conn.commit()
        conn.close()
        logger.info(f"Removed from queue (successfully processed): {file_path}")
        return True
    except Exception as e:
        logger.error(f"Error marking download as successful: {e}")
        return False

def mark_download_exists_in_library(file_path):
    """Mark a download as already existing in library"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("""
            UPDATE download_queue 
            SET status = 'discovered',
                updated_at = %s
            WHERE file_path = %s
        """, (datetime.now().isoformat(), file_path))
        
        conn.commit()
        conn.close()
        logger.info(f"Marked as exists_in_library: {file_path}")
        return True
    except Exception as e:
        logger.error(f"Error marking as exists in library: {e}")
        return False

def add_to_database(file_info, metadata, source_file_path=None):
    """Add organized file to database"""
    try:
        if not file_info.get('success'):
            return False
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Generate track ID from path
        track_id = os.path.basename(file_info['target_path']).replace('.mp3', '')
        
        # Build genres string
        genres = metadata.get('genre', '')
        if isinstance(genres, list):
            genres = ', '.join(genres)
        
        # Insert/update track using PostgreSQL upsert
        cursor.execute("""
            INSERT INTO tracks (
                id, artist, album, title, genres, file_path, last_scanned
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(id) DO UPDATE SET
                artist = EXCLUDED.artist,
                album = EXCLUDED.album,
                title = EXCLUDED.title,
                genres = EXCLUDED.genres,
                file_path = EXCLUDED.file_path,
                last_scanned = EXCLUDED.last_scanned
        """, (
            track_id,
            file_info['artist'],
            file_info['album'],
            file_info['title'],
            genres,
            file_info['target_path'],
            datetime.now().isoformat()
        ))
        
        # If we know the original queue file path, bring over MBIDs from queue metadata.
        if source_file_path:
            try:
                cursor.execute(
                    """
                    SELECT release_mbid, recording_mbid
                    FROM download_queue
                    WHERE file_path = %s
                    LIMIT 1
                    """,
                    (source_file_path,),
                )

                queue_row = cursor.fetchone()
                if queue_row:
                    release_mbid = queue_row['release_mbid'] if isinstance(queue_row, dict) else queue_row[0]
                    recording_mbid = queue_row['recording_mbid'] if isinstance(queue_row, dict) else queue_row[1]

                    # Store recording MBID and album MBID on the track row so tag sync can write them.
                    cursor.execute(
                        """
                        UPDATE tracks
                        SET mbid = %s,
                            suggested_mbid = %s,
                            musicbrainz_album_mbid = %s,
                            last_scanned = %s
                        WHERE id = %s
                        """,
                        (
                            recording_mbid or None,
                            release_mbid or None,
                            release_mbid or None,
                            datetime.now().isoformat(),
                            track_id,
                        ),
                    )
            except Exception as e:
                logger.warning(f"Could not transfer queue MBIDs for {track_id}: {e}")

        conn.commit()
        conn.close()

        # Sync tags to file after DB write so MBIDs and album metadata are embedded in the file.
        try:
            from helpers.tag_manager import sync_track_tags_to_file

            sync_track_tags_to_file(track_id)
        except Exception as e:
            logger.warning(f"Tag sync skipped for {track_id}: {e}")

        logger.info(f"Added to database: {track_id}")
        return True
    except Exception as e:
        logger.error(f"Error adding to database: {e}")
        return False

def scan_downloads_folder():
    """Scan the configured torrent downloads folder recursively for MP3/FLAC files."""
    torrents_dir = resolve_torrent_downloads_dir()
    downloads_dir = get_downloads_dir()

    if not os.path.exists(torrents_dir):
        logger.info(f"Torrent downloads folder not found: {torrents_dir} - skipping scan")
        return []

    results = []

    for root, _, files in os.walk(torrents_dir):
        for filename in files:
            if not filename.lower().endswith((".mp3", ".flac")):
                continue

            file_path = os.path.join(root, filename)
        
            # Skip if not a regular file
            if not os.path.isfile(file_path):
                continue

            try:
                logger.info(f"Processing: {os.path.relpath(file_path, downloads_dir)}")

                # Extract metadata
                metadata = extract_mp3_metadata(file_path)
                logger.info(f"Extracted metadata: {metadata}")

                # Try to match against an active queue item
                queue_item = find_active_queue_item(
                    metadata.get('artist', ''),
                    metadata.get('album', ''),
                    metadata.get('title', '')
                )

                if queue_item:
                    logger.info(
                        f"Torrent file matched queue item {queue_item['id']}: "
                        f"{queue_item.get('artist')} - {queue_item.get('title')}"
                    )
                    # Verify metadata before copying
                    meta_ok, meta_reason = verify_metadata_matches_queue(file_path, queue_item)
                    if not meta_ok:
                        logger.warning(
                            f"Metadata verification failed for queue item {queue_item['id']}: {meta_reason}. "
                            f"Deleting file."
                        )
                        try:
                            os.remove(file_path)
                        except Exception as del_err:
                            logger.error(f"Could not delete mismatched torrent file {file_path}: {del_err}")
                        results.append({
                            'status': 'error',
                            'filename': filename,
                            'error': f"Metadata verification failed: {meta_reason}"
                        })
                        continue

                    file_info = copy_organized_file(file_path, metadata)
                    if file_info.get('success'):
                        add_to_database(file_info, metadata, source_file_path=file_path)
                        mark_queue_item_matched_from_torrent(
                            queue_item['id'], file_info['target_path']
                        )
                        try:
                            os.remove(file_path)
                        except Exception as del_err:
                            logger.warning(f"Could not remove original torrent file {file_path}: {del_err}")
                        results.append({
                            'status': 'success',
                            'filename': filename,
                            'artist': file_info.get('artist'),
                            'album': file_info.get('album'),
                            'title': file_info.get('title'),
                            'target_path': file_info.get('target_path')
                        })
                    else:
                        results.append({
                            'status': 'error',
                            'filename': filename,
                            'error': file_info.get('error', 'Copy failed')
                        })
                    continue

                # Standard path for torrents that do not match a queue item
                file_info = organize_file(file_path, metadata)

                if file_info.get('success'):
                    # Add to database
                    add_to_database(file_info, metadata, source_file_path=file_path)

                    results.append({
                        'status': 'success',
                        'filename': filename,
                        'artist': file_info.get('artist'),
                        'album': file_info.get('album'),
                        'title': file_info.get('title'),
                        'target_path': file_info.get('target_path')
                    })
                else:
                    results.append({
                        'status': 'error',
                        'filename': filename,
                        'error': file_info.get('error', 'Unknown error')
                    })
            except Exception as e:
                logger.error(f"Error processing {filename}: {e}")
                results.append({
                    'status': 'error',
                    'filename': filename,
                    'error': str(e)
                })
    
    return results

def watch_downloads_folder(interval=10):
    """Watch downloads folder for new files (runs continuously)"""
    logger.info(f"Starting downloads watcher for '{get_downloads_dir()}' (interval: {interval}s)")
    
    while True:
        try:
            results = scan_downloads_folder()
            
            if results:
                logger.info(f"Scan complete. Results: {len(results)}")
                for result in results:
                    if result['status'] == 'success':
                        logger.info(f"✓ {result['filename']} -> {result['artist']}/{result['album']}/{result['title']}")
                    else:
                        logger.error(f"✗ {result['filename']}: {result.get('error', 'Unknown error')}")
            
            time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("Downloads watcher stopped")
            break
        except Exception as e:
            logger.error(f"Error in watch loop: {e}")
            time.sleep(interval)

if __name__ == "__main__":
    watch_downloads_folder(interval=30)
