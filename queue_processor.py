#!/usr/bin/env python3
"""
Download Queue Processor
Background worker that processes items in the download queue.
- Searches Soulseek for queued items
- Auto-downloads matching results
- Retries failed items with backoff
- Updates queue status and tracks file completion
"""

import os
import sys
import time
import sqlite3
import traceback
import re
import yaml
from datetime import datetime, timedelta
from pathlib import Path
from difflib import SequenceMatcher
from helpers.metadata_reader import read_mp3_metadata
try:
    from mutagen import File as MutagenFile
except ImportError:
    MutagenFile = None

# Use unified logging system - all logs go to debug.log
from helpers.logging_config import (
    setup_logging,
    log_unified,
    log_info,
    log_debug
)

# Set up logging with Queue Processor service name
setup_logging("QueueProcessor")

# Create logger reference for compatibility with existing code
import logging
logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")


def _is_postgres_connection(conn):
    """Return True when the active DB connection is PostgreSQL."""
    try:
        from app import _is_postgres_connection as app_is_postgres_connection
        return bool(app_is_postgres_connection(conn))
    except Exception:
        try:
            import psycopg2
            return isinstance(conn, psycopg2.extensions.connection)
        except Exception:
            return False


def _get_placeholder(conn):
    return "%s" if _is_postgres_connection(conn) else "?"


def resolve_downloads_dir():
    """Resolve downloads directory from env/config with safe fallback."""
    def _prefer_music_subfolder(path: str) -> str:
        if not path:
            return path
        normalized = os.path.normpath(path)
        if os.path.basename(normalized).lower() == "downloads":
            music_subdir = os.path.join(normalized, "Music")
            if os.path.isdir(music_subdir):
                return music_subdir
        return path

    env_dir = os.environ.get("DOWNLOADS_DIR")
    if env_dir:
        return _prefer_music_subfolder(env_dir)

    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
    try:
        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                cfg = yaml.safe_load(f) or {}
            configured = (cfg.get('downloads') or {}).get('folder')
            if configured:
                return _prefer_music_subfolder(configured)
    except Exception as e:
        logger.warning(f"Could not read downloads folder from config: {e}")

    return "/downloads/Music"


DOWNLOADS_DIR = resolve_downloads_dir()


def _normalize_match_text(value):
    """Normalize text for conservative filename/metadata matching."""
    if not value:
        return ""
    value = str(value).lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _tokenize_meaningful(value):
    """Tokenize and remove short/common words to reduce false positives."""
    stop_words = {"the", "and", "of", "a", "an", "to", "in", "on", "for", "with"}
    normalized = _normalize_match_text(value)
    return [t for t in normalized.split() if len(t) >= 3 and t not in stop_words]


def _extract_tag_value(tags, keys):
    """
    Extract the first non-empty string value from a mutagen tags dict.

    Handles Vorbis comments (list values), ID3 frames (.text attribute),
    and plain string values. Returns an empty string if nothing is found.
    """
    for key in keys:
        raw = tags.get(key)
        if not raw:
            continue
        if isinstance(raw, list):
            raw = raw[0] if raw else ''
        if hasattr(raw, 'text'):
            raw = raw.text[0] if raw.text else ''
        value = str(raw).strip()
        if value:
            return value
    return ''


def _score_soulseek_candidate(filename, queue_item):
    """
    Score a Soulseek candidate path/name against queue metadata.

    Returns float score in [0, 1]. Higher is better.
    """
    filename_norm = _normalize_match_text(filename)
    artist_norm = _normalize_match_text(queue_item.get('artist'))
    title_norm = _normalize_match_text(queue_item.get('title'))
    album_norm = _normalize_match_text(queue_item.get('album'))

    if not artist_norm or not title_norm or not filename_norm:
        return 0.0

    # Require both core fields to be reasonably represented in filename/path.
    artist_sim = SequenceMatcher(None, artist_norm, filename_norm).ratio()
    title_sim = SequenceMatcher(None, title_norm, filename_norm).ratio()
    if artist_sim < 0.12 or title_sim < 0.12:
        return 0.0

    score = (artist_sim * 0.45) + (title_sim * 0.55)

    # Strongly prefer explicit artist/title phrases when present.
    if artist_norm in filename_norm:
        score += 0.18
    if title_norm in filename_norm:
        score += 0.25

    # Album disambiguation: prevent "Power"-style partial collisions.
    if album_norm:
        album_tokens = _tokenize_meaningful(album_norm)
        if album_tokens:
            shared_album_tokens = sum(1 for tok in album_tokens if tok in filename_norm)
            token_ratio = shared_album_tokens / len(album_tokens)

            # When we have >=2 meaningful album tokens, require at least 2 matches.
            # This rejects near misses like "Sword of Power" for "Power of Metal".
            if len(album_tokens) >= 2 and shared_album_tokens < 2:
                return 0.0

            # Reward strong album evidence and penalize weak/partial album alignment.
            if album_norm in filename_norm:
                score += 0.30
            else:
                score += (0.20 * token_ratio)
                if token_ratio < 0.5:
                    score -= 0.10

    return max(0.0, min(1.0, score))


def _metadata_matches_queue_item(file_path, queue_item, threshold=0.68):
    """
    Validate file tags against queue artist/title.

    Returns:
        True: metadata exists and is a strong match
        False: metadata exists but mismatches queue item
        None: metadata unavailable; caller may fallback to filename matching
    """
    try:
        metadata = read_mp3_metadata(file_path) or {}
    except Exception:
        return None

    file_artist = (metadata.get('artist') or '').strip()
    file_title = (metadata.get('title') or '').strip()

    # read_mp3_metadata only handles MP3 ID3 tags. For FLAC, OGG, M4A and other
    # formats it returns an empty dict. Fall back to mutagen.File which supports
    # all common audio containers before giving up.
    if (not file_artist or not file_title) and MutagenFile is not None:
        try:
            audio = MutagenFile(file_path)
            if audio is not None and audio.tags:
                tags = audio.tags
                file_artist = file_artist or _extract_tag_value(
                    tags, ('artist', 'ARTIST', 'TPE1', '\xa9ART')
                )
                file_title = file_title or _extract_tag_value(
                    tags, ('title', 'TITLE', 'TIT2', '\xa9nam')
                )
        except Exception:
            pass

    if not file_artist or not file_title:
        return None

    queue_artist = (queue_item.get('artist') or '').strip()
    queue_title = (queue_item.get('title') or '').strip()
    if not queue_artist or not queue_title:
        return None

    artist_score = SequenceMatcher(
        None,
        _normalize_match_text(file_artist),
        _normalize_match_text(queue_artist),
    ).ratio()
    title_score = SequenceMatcher(
        None,
        _normalize_match_text(file_title),
        _normalize_match_text(queue_title),
    ).ratio()

    # Require both core fields to be reasonably close to avoid false-positive imports.
    if artist_score < 0.55 or title_score < 0.55:
        return False

    combined = (artist_score + title_score) / 2
    return combined >= threshold

def get_db():
    """Get database connection using app backend (PostgreSQL or SQLite)."""
    try:
        from app import get_db as app_get_db
        return app_get_db()
    except Exception:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

def get_slskd_client():
    """Get configured SlskdClient instance"""
    try:
        import yaml
        
        # Prefer explicit CONFIG_PATH, then try common defaults.
        config_path = os.environ.get("CONFIG_PATH", "").strip()
        if not config_path:
            config_path = "/config/config.yml"
            if not os.path.exists(config_path):
                config_path = "/config/config.yaml"
        
        if not os.path.exists(config_path):
            logger.error(f"Config file not found (tried config.yml and config.yaml)")
            return None
        
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f) or {}
        
        slskd_config = config.get("slskd", {})
        
        if not slskd_config.get("enabled"):
            logger.warning("Soulseek (slskd) is not enabled in config")
            return None
        
        from api_clients.slskd import SlskdClient
        
        web_url = slskd_config.get("web_url", "http://localhost:5030")
        api_key = slskd_config.get("api_key", "")
        
        return SlskdClient(web_url, api_key, enabled=True)
        
    except Exception as e:
        logger.error(f"Error getting SlskdClient: {e}")
        return None

def cleanup_stuck_searching_items():
    """Detect and mark as failed any items stuck in 'searching' for too long"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        placeholder = _get_placeholder(conn)
        
        # Items stuck in 'searching' for more than 90 seconds are likely hung
        stuck_threshold = (datetime.now() - timedelta(seconds=90)).isoformat()
        
        cursor.execute("""
            SELECT id, artist, title, updated_at FROM download_queue
            WHERE status = 'searching'
            AND updated_at < {placeholder}
        """.format(placeholder=placeholder), (stuck_threshold,))
        
        stuck_items = cursor.fetchall()
        
        if stuck_items:
            logger.warning(f"Found {len(stuck_items)} items stuck in 'searching' status, marking for retry...")
            
            for item in stuck_items:
                item_id = item['id']
                logger.warning(
                    f"Queue {item_id}: Detected stuck search ({item['artist']} - {item['title']}, "
                    f"updated at {item['updated_at']}), marking for retry..."
                )
                mark_failed(
                    item_id,
                    "Stuck in searching state (likely slskd unresponsive)",
                    schedule_retry=True,
                    retry_delay_minutes=15
                )
        
        conn.close()
        return len(stuck_items)
        
    except Exception as e:
        logger.error(f"Error cleaning up stuck searching items: {e}")
        return 0

def get_queued_items(limit=10):
    """Get items ready to process (queued or scheduled for retry)"""
    try:
        # First, clean up any items stuck in 'searching' state
        cleanup_stuck_searching_items()
        
        conn = get_db()
        cursor = conn.cursor()
        placeholder = _get_placeholder(conn)
        
        now = datetime.now().isoformat()
        
        # Get queued items and items scheduled for retry
        cursor.execute("""
            SELECT * FROM download_queue 
            WHERE status = 'queued'
            AND (next_retry_at IS NULL OR next_retry_at <= {placeholder})
            AND source = 'soulseek'
            ORDER BY priority ASC, retry_count ASC, next_retry_at ASC, created_at ASC
            LIMIT {placeholder}
        """.format(placeholder=placeholder), (now, limit))
        
        items = [dict(row) for row in cursor.fetchall()]
        conn.close()
        
        return items
        
    except Exception as e:
        logger.error(f"Error getting queued items: {e}")
        return []

def update_queue_status(queue_id, status, **kwargs):
    """Update queue item status"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        placeholder = _get_placeholder(conn)
        
        updates = [f"status = {placeholder}"]
        params = [status]
        
        # Add any additional fields to update
        for key, value in kwargs.items():
            if key in ['found_filename', 'file_path', 'failure_reason', 'retry_count', 
                       'last_failure_time', 'source_id']:
                updates.append(f"{key} = {placeholder}")
                params.append(value)
        
        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.append(queue_id)
        
        query = f"UPDATE download_queue SET {', '.join(updates)} WHERE id = {placeholder}"
        cursor.execute(query, params)
        conn.commit()
        conn.close()
        
        logger.info(f"Updated queue {queue_id} to status: {status}")
        return True
        
    except Exception as e:
        logger.error(f"Error updating queue status: {e}")
        return False

def increment_retry_count(queue_id, retry_delay_minutes=30):
    """Increment retry count and schedule next retry"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        placeholder = _get_placeholder(conn)
        
        # Get current retry count
        cursor.execute(f"""
            SELECT retry_count FROM download_queue WHERE id = {placeholder}
        """, (queue_id,))
        
        row = cursor.fetchone()
        if not row:
            conn.close()
            return False
        
        retry_count = (row['retry_count'] or 0) + 1
        
        next_retry = datetime.now() + timedelta(minutes=retry_delay_minutes)
        
        cursor.execute(f"""
            UPDATE download_queue 
            SET retry_count = {placeholder}, next_retry_at = {placeholder}, updated_at = CURRENT_TIMESTAMP
            WHERE id = {placeholder}
        """, (retry_count, next_retry.isoformat(), queue_id))
        
        conn.commit()
        conn.close()
        
        logger.info(f"Queue {queue_id}: retry count now {retry_count}, next retry at {next_retry}")
        return True
        
    except Exception as e:
        logger.error(f"Error incrementing retry count: {e}")
        return False

def mark_failed(queue_id, reason, schedule_retry=True, retry_delay_minutes=30):
    """Mark queue item as failed, optionally scheduling retry"""
    try:
        # Try app's get_db first (PostgreSQL-aware)
        is_pg = False
        try:
            from app import get_db as app_get_db, _is_postgres_connection as app_is_postgres_connection
            conn = app_get_db()
            is_pg = bool(app_is_postgres_connection(conn))
        except Exception:
            conn = get_db()
        
        cursor = conn.cursor()
        placeholder = "%s" if is_pg else "?"
        
        # Get current retry count
        cursor.execute(f"SELECT retry_count FROM download_queue WHERE id = {placeholder}", (queue_id,))
        row = cursor.fetchone()
        
        if not row:
            conn.close()
            return False
        
        retry_count = (row['retry_count'] or 0) + 1
        
        # Always schedule retry if requested - no max retry limit for Soulseek searches
        if schedule_retry:
            next_retry = datetime.now() + timedelta(minutes=retry_delay_minutes)
            new_status = 'queued'
            logger.warning(f"Queue {queue_id}: Failed ({reason}), scheduling retry #{retry_count} at {next_retry}")
        else:
            next_retry = None
            new_status = 'failed'
            logger.error(f"Queue {queue_id}: Failed permanently ({reason}) - retry not requested")
        
        cursor.execute(f"""
            UPDATE download_queue 
            SET status = {placeholder}, retry_count = {placeholder}, failure_reason = {placeholder}, last_failure_time = CURRENT_TIMESTAMP,
                next_retry_at = {placeholder}, updated_at = CURRENT_TIMESTAMP
            WHERE id = {placeholder}
        """, (new_status, retry_count, reason, next_retry.isoformat() if next_retry else None, queue_id))
        
        conn.commit()
        conn.close()
        
        return schedule_retry  # Return whether retry was scheduled
        
    except Exception as e:
        logger.error(f"Error marking queue item as failed: {e}")
        return False

def search_and_download(queue_id, queue_item, client):
    """Search Soulseek for queue item and download top result"""
    try:
        search_query = queue_item['search_query']
        
        logger.info(f"Queue {queue_id}: Searching for '{search_query}'...")
        update_queue_status(queue_id, 'searching')
        
        # Start search
        search_id = client.start_search(search_query)
        if not search_id:
            logger.warning(f"Queue {queue_id}: Failed to start search")
            mark_failed(queue_id, "Failed to start Soulseek search", schedule_retry=True)
            return False
        
        # Poll for results (up to MAX_POLL_ATTEMPTS seconds with 1 second intervals)
        # Increased timeout to 45 seconds to handle slow Soulseek peer responses
        MAX_POLL_ATTEMPTS = 45
        best_result = None
        best_score = 0.0
        poll_start_time = datetime.now()
        
        for poll_attempt in range(MAX_POLL_ATTEMPTS):
            time.sleep(1)
            
            try:
                responses, state, is_complete = client.get_search_results(search_id)
                
                logger.debug(f"Queue {queue_id}: Poll {poll_attempt+1}/{MAX_POLL_ATTEMPTS} - Got {len(responses)} responses, state={state}")
                
                if responses:
                    # Score all available files and choose the strongest semantic match.
                    for resp_idx, resp in enumerate(responses):
                        if not (hasattr(resp, 'files') and resp.files and len(resp.files) > 0):
                            logger.debug(
                                f"Queue {queue_id}: Response {resp_idx} from "
                                f"{getattr(resp, 'username', 'unknown')} has no files or empty files list"
                            )
                            continue

                        logger.debug(f"Queue {queue_id}: Response {resp_idx} from {resp.username} has {len(resp.files)} files")
                        for file_info in resp.files:
                            filename = (
                                getattr(file_info, 'filename', file_info.get('filename', ''))
                                if isinstance(file_info, dict)
                                else getattr(file_info, 'filename', '')
                            )
                            size = (
                                getattr(file_info, 'size', file_info.get('size', 0))
                                if isinstance(file_info, dict)
                                else getattr(file_info, 'size', 0)
                            )

                            candidate_score = _score_soulseek_candidate(filename, queue_item)
                            if candidate_score > best_score:
                                best_score = candidate_score
                                best_result = {
                                    "username": resp.username,
                                    "filename": filename,
                                    "size": size,
                                    "score": candidate_score,
                                }

                    # If we already have a strong candidate, no need to keep polling.
                    if best_result and best_score >= 0.72:
                        logger.info(
                            f"Queue {queue_id}: ✓ Found high-confidence match after {poll_attempt+1}s "
                            f"(score={best_score:.2f})"
                        )
                        break
                
                # Exit early if search is complete and we have results
                if is_complete and best_result:
                    logger.info(f"Queue {queue_id}: Search complete with results, stopping polling")
                    break
                    
            except Exception as e:
                logger.warning(f"Queue {queue_id}: Error polling results (attempt {poll_attempt+1}): {e}")
                logger.debug(traceback.format_exc())
        
        if not best_result:
            elapsed = (datetime.now() - poll_start_time).total_seconds()
            logger.warning(f"Queue {queue_id}: ✗ No results found after {elapsed:.0f}s of polling")
            mark_failed(queue_id, f"No results found for '{search_query}'", schedule_retry=True, retry_delay_minutes=60)
            return False

        if best_score < 0.45:
            elapsed = (datetime.now() - poll_start_time).total_seconds()
            logger.warning(
                f"Queue {queue_id}: ✗ Results found but no safe match for '{search_query}' "
                f"(best_score={best_score:.2f}, elapsed={elapsed:.0f}s)"
            )
            mark_failed(
                queue_id,
                f"No safe Soulseek match for '{search_query}' (best_score={best_score:.2f})",
                schedule_retry=True,
                retry_delay_minutes=60,
            )
            return False
        
        # Download the result
        logger.info(
            f"Queue {queue_id}: Downloading '{best_result['filename']}' from "
            f"{best_result['username']} (score={best_score:.2f})..."
        )
        update_queue_status(queue_id, 'downloading', found_filename=best_result['filename'])
        
        success = client.download_file(best_result['username'], best_result['filename'], best_result['size'])
        
        if success:
            logger.info(f"Queue {queue_id}: Download queued successfully in slskd")
            logger.info(f"Queue {queue_id}: File will appear in {DOWNLOADS_DIR} when download completes")
            # Status already set to 'downloading' above
            return True
        else:
            logger.error(f"Queue {queue_id}: Failed to queue download in slskd")
            mark_failed(queue_id, "Failed to queue Soulseek download", schedule_retry=True, retry_delay_minutes=15)
            return False
            
    except Exception as e:
        logger.error(f"Queue {queue_id}: Error in search_and_download: {e}")
        logger.debug(traceback.format_exc())
        mark_failed(queue_id, f"Search error: {str(e)}", schedule_retry=True)
        return False

def check_completed_downloads():
    """Check /downloads folder for completed files and match to queue items"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        if not os.path.isdir(DOWNLOADS_DIR):
            logger.warning(f"Downloads directory does not exist: {DOWNLOADS_DIR}")
            return
        
        # Get all downloading queue items (select all columns for metadata needed to move)
        cursor.execute("""
            SELECT * FROM download_queue
            WHERE status = 'downloading'
        """)
        
        downloading = [dict(row) for row in cursor.fetchall()]
        
        if downloading:
            logger.debug(f"Checking {len(downloading)} items in 'downloading' status")
        
        # Get audio files in downloads folder recursively
        try:
            files = []
            for root, _, root_files in os.walk(DOWNLOADS_DIR):
                for f in root_files:
                    if f.lower().endswith(('.mp3', '.flac', '.m4a')):
                        files.append(os.path.relpath(os.path.join(root, f), DOWNLOADS_DIR))
            if files:
                logger.debug(f"Found {len(files)} audio files in {DOWNLOADS_DIR}")
        except Exception as e:
            logger.error(f"Error scanning downloads folder: {e}")
            conn.close()
            return
        
        # Try to match files to queue items
        newly_completed = []
        for item in downloading:
            match_found = None
            match_meta_state = None
            
            # Try exact filename match first
            if item['found_filename']:
                for rel_file in files:
                    rel_file_norm = rel_file.replace('\\', '/')
                    found_norm = str(item['found_filename']).replace('\\', '/')
                    if rel_file_norm == found_norm or os.path.basename(rel_file_norm) == os.path.basename(found_norm):
                        file_path = os.path.join(DOWNLOADS_DIR, rel_file)
                        meta_state = _metadata_matches_queue_item(file_path, item)
                        if meta_state is False:
                            logger.info(
                                f"Queue {item['id']}: rejecting exact filename match due to metadata mismatch: {rel_file}"
                            )
                            continue
                        match_found = rel_file
                        match_meta_state = meta_state
                        break

            if match_found:
                logger.debug(f"Queue {item['id']}: Exact filename match found")
            else:
                # Try fuzzy matching
                for filename in files:
                    file_path = os.path.join(DOWNLOADS_DIR, filename)
                    meta_state = _metadata_matches_queue_item(file_path, item)

                    # If tags are present and disagree, do not allow filename/path fallback.
                    if meta_state is False:
                        continue

                    # Prefer metadata-backed matches. Otherwise use stricter path scoring fallback.
                    if meta_state is True or matches_queue_item(filename, item):
                        match_found = filename
                        match_meta_state = meta_state
                        logger.debug(f"Queue {item['id']}: Fuzzy match found: {filename}")
                        break
            
            if match_found:
                file_path = os.path.join(DOWNLOADS_DIR, match_found)
                if match_meta_state is True:
                    logger.info(
                        f"Queue {item['id']}: Matched file '{match_found}' by metadata artist/title - marking as completed"
                    )
                else:
                    logger.info(
                        f"Queue {item['id']}: Matched file '{match_found}' by filename/path fallback - marking as completed"
                    )
                update_queue_status(item['id'], 'completed', file_path=file_path, found_filename=match_found)

                # Immediately move the file to /music
                try:
                    from download_queue_manager import move_single_track_to_music_dir, update_queue_item
                    from download_file_verification import verify_file_in_music, mark_queue_item_moved
                    
                    # Build a minimal item dict with the metadata needed for folder determination
                    item_for_move = dict(item)
                    item_for_move['file_path'] = file_path
                    move_result = move_single_track_to_music_dir(item_for_move)
                    if move_result['success']:
                        target_path = move_result['target_path']
                        
                        # Verify file exists at new location before marking as imported
                        verify_result = verify_file_in_music(item['id'], target_path)
                        
                        if verify_result['success']:
                            # File verified - mark as moved and imported
                            mark_queue_item_moved(item['id'], target_path)
                            update_queue_item(
                                item['id'],
                                status='imported',
                                file_path=target_path,
                                copied_individually=1,
                                copied_individually_at=datetime.now().isoformat()
                            )
                            logger.info(f"[AUTO_MOVE] Queue {item['id']}: verified and imported to {target_path}")
                        else:
                            # Verification failed - mark back to completed for retry
                            logger.warning(
                                f"[AUTO_MOVE] Queue {item['id']}: verification FAILED ({verify_result.get('error')}), "
                                f"marking back to 'completed' for retry"
                            )
                            update_queue_item(
                                item['id'],
                                status='completed',
                                file_path=file_path
                            )
                    else:
                        logger.warning(
                            f"[AUTO_MOVE] Queue {item['id']}: could not move "
                            f"({move_result.get('error')}), keeping as 'completed'"
                        )
                except Exception as move_err:
                    logger.warning(f"[AUTO_MOVE] Queue {item['id']}: move error: {move_err}")

                newly_completed.append(item)

        conn.close()

        # After matching files, check whether any album is now fully complete
        # and auto-move all its remaining tracks to the music library.
        for item in newly_completed:
            try:
                from download_queue_manager import auto_move_completed_album
                result = auto_move_completed_album(
                    release_id=item.get('release_id'),
                    artist=item.get('artist'),
                    album=item.get('album')
                )
                if result.get('album_complete'):
                    logger.info(
                        f"[AUTO_MOVE] Album complete after download: "
                        f"{item.get('artist')} – {item.get('album')} | "
                        f"moved={result['moved']}, already_copied={result['already_copied']}"
                    )
            except Exception as auto_err:
                logger.warning(f"[AUTO_MOVE] Error triggering auto-move for queue {item['id']}: {auto_err}")

    except Exception as e:
        logger.error(f"Error checking completed downloads: {e}")

def matches_queue_item(filename, queue_item):
    """Conservative filename/path fallback matcher when metadata is unavailable."""
    try:
        score = _score_soulseek_candidate(filename, queue_item)
        return score >= 0.60
        
    except Exception as e:
        logger.error(f"Error matching filename: {e}")
        return False

def process_queue(client):
    """Process one batch of queued items"""
    try:
        items = get_queued_items(limit=5)
        
        if not items:
            logger.debug("No queued items to process")
        else:
            logger.info(f"Processing {len(items)} queue items...")
        
        processed = 0
        for item in items:
            if not client:
                logger.error("SlskdClient not available, skipping")
                break
            
            try:
                if search_and_download(item['id'], item, client):
                    processed += 1
            except Exception as e:
                logger.error(f"Error processing queue {item['id']}: {e}")
                mark_failed(item['id'], f"Processing error: {str(e)}", schedule_retry=True)
        
        # Always check for completed downloads, even if no new items were processed
        # This ensures downloads that complete between processing cycles are detected
        check_completed_downloads()
        
        # Process completed downloads with MusicBrainz/Discogs metadata
        try:
            from post_download_processor import process_pending_completed_items
            post_stats = process_pending_completed_items(limit=5)
            if post_stats.get('processed', 0) > 0:
                logger.info(f"Post-download processing: {post_stats['processed']} items organized")
        except Exception as e:
            logger.error(f"Error in post-download processing: {e}")
        
        return processed
        
    except Exception as e:
        logger.error(f"Error in process_queue: {e}")
        return 0


def _load_auto_discovery_settings():
    """Load persistent auto-discovery settings from config/env with safe defaults."""
    enabled = True
    interval_seconds = 60

    # Optional env overrides for quick control.
    env_enabled = os.environ.get("DOWNLOADS_AUTO_DISCOVER_ENABLED")
    env_interval = os.environ.get("DOWNLOADS_AUTO_DISCOVER_INTERVAL_SECONDS")

    if env_enabled is not None:
        enabled = str(env_enabled).strip().lower() in {"1", "true", "yes", "on"}

    if env_interval:
        try:
            interval_seconds = int(env_interval)
        except ValueError:
            logger.warning("Invalid DOWNLOADS_AUTO_DISCOVER_INTERVAL_SECONDS='%s'", env_interval)

    # Config file settings override defaults when present.
    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
    try:
        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                cfg = yaml.safe_load(f) or {}

            features = cfg.get('features') or {}
            discovery_cfg = features.get('downloads_auto_discover') or {}

            if 'enabled' in discovery_cfg:
                enabled = bool(discovery_cfg.get('enabled'))
            if 'interval_seconds' in discovery_cfg:
                interval_seconds = int(discovery_cfg.get('interval_seconds') or interval_seconds)
    except Exception as e:
        logger.warning(f"Could not read auto-discovery settings: {e}")

    if interval_seconds < 15:
        interval_seconds = 15

    return enabled, interval_seconds


def maybe_auto_discover_files(now_ts, last_run_ts):
    """Run background auto-discovery on interval and return updated last-run timestamp."""
    enabled, interval_seconds = _load_auto_discovery_settings()
    if not enabled:
        return last_run_ts

    if last_run_ts is not None and (now_ts - last_run_ts) < interval_seconds:
        return last_run_ts

    try:
        from download_queue_manager import auto_discover_and_queue_files

        stats = auto_discover_and_queue_files()
        queued = int(stats.get('queued', 0) or 0)
        scanned = int(stats.get('scanned', 0) or 0)
        if queued > 0:
            logger.info(
                "[AUTO-DISCOVER] Added %s new files to queue (scanned=%s)",
                queued,
                scanned,
            )
        else:
            logger.debug("[AUTO-DISCOVER] No new files found (scanned=%s)", scanned)
    except Exception as e:
        logger.error(f"[AUTO-DISCOVER] Error during background discovery: {e}")

    return now_ts


def maybe_check_musicbrainz_files(now_ts, last_run_ts, interval_seconds=30):
    """
    Run MusicBrainz file matching on interval and return updated last-run timestamp.
    Checks for new files matching active releases every 30 seconds.
    """
    if last_run_ts is not None and (now_ts - last_run_ts) < interval_seconds:
        return last_run_ts

    try:
        from musicbrainz_file_matcher import get_matcher
        
        matcher = get_matcher()
        result = matcher.monitor_and_match()
        matched = result.get("matched", 0)
        
        if matched > 0:
            logger.info(f"[MB_FILE_MATCHER] Matched {matched} files to releases")
        else:
            logger.debug("[MB_FILE_MATCHER] No new matches found")
            
    except Exception as e:
        logger.error(f"[MB_FILE_MATCHER] Error during file matching: {e}")

    return now_ts


def maybe_finalize_musicbrainz_releases(now_ts, last_run_ts, interval_seconds=60):
    """
    Run MusicBrainz release finalization on interval and return updated last-run timestamp.
    Finalizes releases when all tracks are discovered (every 60 seconds).
    """
    if last_run_ts is not None and (now_ts - last_run_ts) < interval_seconds:
        return last_run_ts

    try:
        from musicbrainz_finalizer import get_finalizer
        
        finalizer = get_finalizer()
        result = finalizer.check_and_finalize_releases()
        finalized = result.get("finalized", 0)
        
        if finalized > 0:
            logger.info(f"[MB_FINALIZER] Finalized {finalized} releases")
        else:
            logger.debug("[MB_FINALIZER] No releases ready for finalization")
            
    except Exception as e:
        logger.error(f"[MB_FINALIZER] Error during release finalization: {e}")

    return now_ts


def maybe_check_missing_moved_files(now_ts, last_run_ts, interval_seconds=300):
    """
    Periodically check for files that were moved to /music but have since disappeared.
    Requeues them for retry. Runs every 5 minutes by default.
    
    Args:
        now_ts: Current timestamp
        last_run_ts: Timestamp of last run
        interval_seconds: Interval between checks (default 300 seconds = 5 minutes)
    
    Returns:
        Updated last-run timestamp
    """
    if last_run_ts is not None and (now_ts - last_run_ts) < interval_seconds:
        return last_run_ts

    try:
        from download_file_verification import check_missing_moved_files
        
        result = check_missing_moved_files(minutes_old=30)
        checked = result.get('checked', 0)
        found_missing = result.get('found_missing', 0)
        requeued = result.get('requeued', 0)
        
        if found_missing > 0:
            logger.warning(
                f"[FILE_VERIFY] File verification: checked {checked}, "
                f"found {found_missing} missing, requeued {requeued}"
            )
        else:
            logger.debug(f"[FILE_VERIFY] File verification: checked {checked}, all present")
            
    except Exception as e:
        logger.error(f"[FILE_VERIFY] Error during file verification check: {e}")

    return now_ts

def run_processor(interval=30):
    """Run queue processor loop"""
    logger.info("=== Queue Processor Started ===")
    logger.info(f"Processing interval: {interval}s")
    
    client = get_slskd_client()
    if not client:
        logger.error("Cannot initialize SlskdClient - exiting")
        sys.exit(1)
    
    loop_count = 0
    last_auto_discover_ts = None
    last_mb_check_ts = None
    last_mb_finalize_ts = None
    last_verify_ts = None
    
    try:
        while True:
            try:
                loop_count += 1
                logger.debug(f"--- Loop {loop_count} ---")

                now_ts = time.time()
                last_auto_discover_ts = maybe_auto_discover_files(now_ts, last_auto_discover_ts)
                last_mb_check_ts = maybe_check_musicbrainz_files(now_ts, last_mb_check_ts)
                last_mb_finalize_ts = maybe_finalize_musicbrainz_releases(now_ts, last_mb_finalize_ts)
                last_verify_ts = maybe_check_missing_moved_files(now_ts, last_verify_ts)
                
                processed = process_queue(client)
                
                if processed > 0:
                    logger.info(f"Processed {processed} queue items")
                
                time.sleep(interval)
                
            except KeyboardInterrupt:
                logger.info("Queue processor stopped by user")
                break
            except Exception as e:
                logger.error(f"Error in processor loop: {e}")
                logger.error(traceback.format_exc())
                time.sleep(interval)
                
    except KeyboardInterrupt:
        logger.info("Queue processor interrupted")
    finally:
        logger.info("=== Queue Processor Stopped ===")

if __name__ == "__main__":
    # Default interval is 30 seconds
    interval = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    run_processor(interval)
