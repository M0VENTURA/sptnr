"""
Enhanced folder matching system for improved auto-matching and duplicate detection.

Features:
1. Auto-matching with confidence scoring
2. Release track listing fetching
3. Per-track move capability with library checking
4. Duplicate detection (existing tracks in library)
5. Batch duplicate conflict viewer
"""

import os
import logging
import sqlite3
import requests
import time
from typing import Dict, List, Tuple, Optional, Any
from difflib import SequenceMatcher
from database_abstraction import DatabaseQuery
from api_clients.musicbrainz import _USER_AGENT as MUSICBRAINZ_USER_AGENT

logger = logging.getLogger(__name__)


def get_musicbrainz_release_tracks(release_id: str, source: str = 'musicbrainz') -> List[Dict]:
    """
    Fetch full track listing for a MusicBrainz or Discogs release.
    
    Args:
        release_id: MusicBrainz release ID or Discogs release ID
        source: 'musicbrainz' or 'discogs'
    
    Returns:
        List of track dicts with title, number, duration, artist
    """
    try:
        if source == 'musicbrainz':
            return _fetch_musicbrainz_tracks(release_id)
        elif source == 'discogs':
            return _fetch_discogs_tracks(release_id)
        else:
            logger.error(f"Unknown source: {source}")
            return []
    except Exception as e:
        logger.error(f"Error fetching {source} release tracks: {e}")
        return []


def _fetch_musicbrainz_tracks(release_id: str) -> List[Dict]:
    """Fetch tracks from MusicBrainz release."""
    try:
        base_url = "https://musicbrainz.org/ws/2/"
        headers = {
            "User-Agent": MUSICBRAINZ_USER_AGENT,
            "Accept": "application/json"
        }
        
        time.sleep(1.0)  # Respect rate limit
        
        response = requests.get(
            f"{base_url}release/{release_id}",
            params={"fmt": "json", "inc": "recordings"},
            headers=headers,
            timeout=(5, 10)
        )
        response.raise_for_status()
        data = response.json()
        
        tracks = []
        media_list = data.get('media', [])
        
        for media in media_list:
            for track_data in media.get('tracks', []):
                recording = track_data.get('recording', {})
                tracks.append({
                    'number': track_data.get('position', ''),
                    'title': recording.get('title', ''),
                    'artist': _extract_mb_artist(recording),
                    'duration': recording.get('length', 0),
                    'isrc': recording.get('isrc', '')
                })
        
        logger.info(f"Fetched {len(tracks)} tracks from MusicBrainz release {release_id}")
        return tracks
        
    except Exception as e:
        logger.error(f"Error fetching MusicBrainz tracks: {e}")
        return []


def _extract_mb_artist(recording: Dict) -> str:
    """Extract artist name from MusicBrainz recording data."""
    if recording.get('artist-credit'):
        return recording['artist-credit'][0].get('name', '')
    return ''


def _fetch_discogs_tracks(release_id: str) -> List[Dict]:
    """Fetch tracks from Discogs release."""
    try:
        import os
        from api_clients.discogs import DiscogsClient
        
        # Get Discogs token from environment or config
        discogs_token = os.environ.get("DISCOGS_TOKEN", "")
        if not discogs_token:
            try:
                from app import get_config
                cfg = get_config()
                discogs_token = cfg.get("api_integrations", {}).get("discogs", {}).get("token", "")
            except Exception:
                pass
        
        if not discogs_token:
            logger.warning("Discogs token not configured, skipping Discogs release track fetch")
            return []
        
        logger.debug(f"[_fetch_discogs_tracks] Fetching release {release_id}")
        discogs = DiscogsClient(discogs_token)
        release = discogs.get_release(release_id)
        
        logger.debug(f"[_fetch_discogs_tracks] Release response type: {type(release)}")
        if not release:
            logger.warning(f"[_fetch_discogs_tracks] No release data returned for {release_id}")
            return []
        
        logger.debug(f"[_fetch_discogs_tracks] Release keys: {list(release.keys()) if isinstance(release, dict) else 'not a dict'}")
        
        tracks = []
        tracklist = release.get('tracklist', [])
        logger.debug(f"[_fetch_discogs_tracks] Tracklist count: {len(tracklist)}")
        
        for track in tracklist:
            parsed_duration = _parse_discogs_duration(track.get('duration', ''))
            track_dict = {
                'number': track.get('position', ''),
                'title': track.get('title', ''),
                'artist': track.get('artist', ''),
                'duration': parsed_duration,
                'isrc': ''
            }
            logger.debug(f"[_fetch_discogs_tracks] Track: {track_dict}")
            tracks.append(track_dict)
        
        logger.info(f"Fetched {len(tracks)} tracks from Discogs release {release_id}")
        return tracks
        
    except Exception as e:
        logger.error(f"Error fetching Discogs tracks: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return []


def _parse_discogs_duration(duration_str: str) -> int:
    """Convert Discogs duration string (MM:SS) to milliseconds."""
    try:
        parts = duration_str.split(':')
        if len(parts) == 2:
            minutes = int(parts[0])
            seconds = int(parts[1])
            return (minutes * 60 + seconds) * 1000
        return 0
    except Exception:
        return 0


def score_folder_match(folder_tracks: List[Dict], release_tracks: List[Dict]) -> Tuple[float, Dict]:
    """
    Score how well a folder's tracks match a release.
    
    Uses multiple heuristics:
    - Track count match (exact or close)
    - Title similarity (using SequenceMatcher)
    - Track order consistency
    
    Args:
        folder_tracks: Tracks found in folder
        release_tracks: Tracks from release metadata
    
    Returns:
        (confidence_score, match_details_dict)
        confidence_score: 0.0-1.0 where 1.0 is perfect match
    """
    if not release_tracks:
        return 0.0, {}
    
    scores = []
    details = {}
    
    # Score 1: Track count match (weight: 30%)
    folder_count = len(folder_tracks)
    release_count = len(release_tracks)
    
    if folder_count == release_count:
        count_score = 1.0
    elif folder_count > 0 and release_count > 0:
        # Partial credit if counts are close (within 20%)
        ratio = min(folder_count, release_count) / max(folder_count, release_count)
        count_score = 0.5 if ratio >= 0.8 else 0.0
    else:
        count_score = 0.0
    
    scores.append(('count_match', count_score, 0.3))
    details['track_count_match'] = {
        'folder': folder_count,
        'release': release_count,
        'score': count_score
    }
    
    # Score 2: Title similarity (weight: 50%)
    # Match folder titles against release titles and average the results
    if folder_tracks and release_tracks:
        title_matches = []
        
        for i, folder_track in enumerate(folder_tracks):
            folder_title = folder_track.get('title', '').lower()
            best_sim = 0.0
            best_match_idx = -1
            
            # Find best match in release tracks
            for j, release_track in enumerate(release_tracks):
                release_title = release_track.get('title', '').lower()
                
                # Use sequence matching for similarity
                sim = SequenceMatcher(None, folder_title, release_title).ratio()
                
                if sim > best_sim:
                    best_sim = sim
                    best_match_idx = j
            
            title_matches.append({
                'folder_idx': i,
                'folder_title': folder_track.get('title', 'Unknown'),
                'best_match_idx': best_match_idx,
                'release_title': release_tracks[best_match_idx].get('title', '') if best_match_idx >= 0 else '',
                'similarity': best_sim
            })
        
        if title_matches:
            avg_title_score = sum(m['similarity'] for m in title_matches) / len(title_matches)
            # Require minimum 60% similarity to count as match
            title_score = avg_title_score if avg_title_score >= 0.6 else 0.0
        else:
            title_score = 0.0
        
        scores.append(('title_similarity', title_score, 0.5))
        details['title_matches'] = title_matches
    else:
        scores.append(('title_similarity', 0.0, 0.5))
    
    # Score 3: Track order consistency (weight: 20%)
    # If track numbers are present in folder, check if they match expected order
    order_score = 1.0  # Default perfect
    has_track_numbers = any(t.get('track_number') for t in folder_tracks)
    
    if has_track_numbers and release_tracks:
        mismatches = 0
        for i, folder_track in enumerate(folder_tracks):
            track_num = folder_track.get('track_number')
            if track_num:
                try:
                    expected_idx = int(track_num) - 1
                    if expected_idx < len(release_tracks):
                        expected_title = release_tracks[expected_idx].get('title', '')
                        actual_title = folder_track.get('title', '')
                        
                        if expected_title and actual_title:
                            sim = SequenceMatcher(None, actual_title.lower(), expected_title.lower()).ratio()
                            if sim < 0.6:
                                mismatches += 1
                except (ValueError, IndexError):
                    pass
        
        if mismatches > 0:
            order_score = max(0.3, 1.0 - (mismatches / len(folder_tracks)))
        
        scores.append(('track_order', order_score, 0.2))
        details['track_order'] = {'mismatches': mismatches, 'score': order_score}
    else:
        scores.append(('track_order', 1.0, 0.2))  # No track numbers doesn't penalize
    
    # Calculate weighted average
    total_score = sum(score * weight for _, score, weight in scores)
    
    return total_score, details


def suggest_auto_match(folder_artist: str, folder_album: str, candidates: List[Dict], folder_tracks: List[Dict]) -> Optional[Dict]:
    """
    Automatically suggest the best match from candidates if confidence is high enough.
    
    Args:
        folder_artist: Detected artist from folder
        folder_album: Detected album from folder
        candidates: List of candidate releases from API
        folder_tracks: List of tracks in folder
    
    Returns:
        Best candidate with confidence score if score >= 0.75, else None
    """
    if not candidates or not folder_tracks:
        return None
    
    best_candidate = None
    best_score = 0.0
    best_details = {}
    
    for candidate in candidates:
        # Fetch release tracks for this candidate
        release_tracks = get_musicbrainz_release_tracks(
            candidate.get('id'),
            candidate.get('source', 'musicbrainz')
        )
        
        if not release_tracks:
            continue
        
        # Score this candidate
        score, details = score_folder_match(folder_tracks, release_tracks)
        
        if score > best_score:
            best_score = score
            best_candidate = candidate.copy()
            best_details = details
    
    # Only auto-suggest if confidence is high enough
    if best_score >= 0.75 and best_candidate:
        best_candidate['auto_match_confidence'] = best_score
        best_candidate['match_details'] = best_details
        logger.info(f"Auto-match suggested: {best_candidate['title']} (confidence: {best_score:.2%})")
        return best_candidate
    
    logger.info(f"No high-confidence auto-match found (best: {best_score:.2%})")
    return None


def detect_library_duplicates(conn: Any, tracks: List[Dict], artist: str, album: str) -> Dict:
    """
    Check if tracks/album already exist in the music library.
    
    Args:
        conn: Database connection (SQLite or PostgreSQL)
        tracks: List of tracks from folder
        artist: Album artist
        album: Album name
    
    Returns:
        Dict with duplicate detection results and conflict info
    """
    try:
        db_query = DatabaseQuery(conn)
        
        duplicates = {
            'has_duplicates': False,
            'duplicate_type': None,  # 'exact_album', 'partial_tracks', 'album_exists'
            'conflict_tracks': [],
            'existing_album_path': None,
            'conflicting_files': []
        }
        
        # Check 1: Exact album match
        cursor = db_query.execute("""
            SELECT id, file_path FROM tracks 
            WHERE LOWER(artist) = LOWER(?) AND LOWER(album) = LOWER(?)
            LIMIT 1
        """, (artist, album))
        
        existing_album = cursor.fetchone()
        if existing_album:
            duplicates['has_duplicates'] = True
            duplicates['duplicate_type'] = 'exact_album'
            duplicates['existing_album_path'] = existing_album[1] if existing_album[1] else None
            logger.warning(f"Album already exists: {artist} - {album}")
            return duplicates
        
        # Check 2: Partial track matches
        conflict_tracks = []
        for track in tracks:
            cursor = db_query.execute("""
                SELECT id, file_path, title FROM tracks
                WHERE LOWER(artist) = LOWER(?) AND LOWER(title) = LOWER(?)
                LIMIT 1
            """, (artist, track.get('title', '')))
            
            existing_track = cursor.fetchone()
            if existing_track:
                conflict_tracks.append({
                    'local_title': track.get('title'),
                    'existing_id': existing_track[0],
                    'existing_path': existing_track[1],
                    'existing_title': existing_track[2]
                })
        
        if conflict_tracks:
            duplicates['has_duplicates'] = True
            duplicates['duplicate_type'] = 'partial_tracks'
            duplicates['conflict_tracks'] = conflict_tracks
            logger.warning(f"Found {len(conflict_tracks)} duplicate tracks for {artist}")
            return duplicates
        
        # Check 3: Album exists (different artist same album)
        cursor = db_query.execute("""
            SELECT DISTINCT file_path FROM tracks
            WHERE LOWER(album) = LOWER(?)
            LIMIT 1
        """, (album,))
        
        existing_by_album = cursor.fetchone()
        if existing_by_album:
            duplicates['duplicate_type'] = 'album_exists'
            duplicates['existing_album_path'] = existing_by_album[0]
            logger.info(f"Album title exists for different artist: {album}")
        
        return duplicates
        
    except Exception as e:
        logger.error(f"Error detecting library duplicates: {e}")
        return {
            'has_duplicates': False,
            'error': str(e)
        }


def organize_individual_track(
    track: Dict,
    release_metadata: Dict,
    music_dir: str = "/music",
    db_conn: Optional[Any] = None,
    check_duplicates: bool = True
) -> Dict:
    """
    Move a single track to the music library.
    
    Args:
        track: Track dict with file_path, title, artist
        release_metadata: Release info (album, artist, date)
        music_dir: Base music directory
        db_conn: Database connection for duplicate checking
        check_duplicates: Whether to check for existing library copies
    
    Returns:
        Dict with success status and file path
    """
    import shutil
    import re
    
    try:
        source_file = track.get('file_path')
        if not source_file or not os.path.exists(source_file):
            return {
                'success': False,
                'error': f"Source file not found: {source_file}"
            }
        
        # Check for duplicates if requested
        if check_duplicates and db_conn:
            dup_result = detect_library_duplicates(
                db_conn,
                [track],
                track.get('artist', release_metadata.get('artist', 'Unknown')),
                track.get('album', release_metadata.get('title', 'Unknown'))
            )
            
            if dup_result.get('has_duplicates'):
                return {
                    'success': False,
                    'duplicate_detected': True,
                    'duplicate_info': dup_result,
                    'error': f"Track/album already in library"
                }
        
        # Build destination path
        def sanitize(name):
            name = re.sub(r'[<>:"/\\|?*]', '', name)
            name = name.strip('. ')
            return name
        
        album_artist = release_metadata.get('artist', 'Unknown Artist')
        album_title = release_metadata.get('title', 'Unknown Album')
        release_date = release_metadata.get('date', '')
        release_year = release_date.split('-')[0] if release_date else ''
        
        album_artist_clean = sanitize(album_artist)
        album_title_clean = sanitize(album_title)
        
        if release_year:
            album_folder = f"{release_year} - {album_title_clean}"
        else:
            album_folder = album_title_clean
        
        album_path = os.path.join(music_dir, album_artist_clean, album_folder)
        os.makedirs(album_path, exist_ok=True)
        
        # Build filename
        # Use release artist for track if track artist is unknown or generic
        track_artist_from_metadata = track.get('artist', '').strip()
        is_unknown_artist = (
            track_artist_from_metadata == 'Unknown Artist' or
            track_artist_from_metadata == 'Unknown' or
            track_artist_from_metadata == ''
        )
        
        # If track artist is unknown/missing, use the matched release artist
        if is_unknown_artist:
            track_artist = album_artist  # Use the matched release artist
        else:
            track_artist = track_artist_from_metadata
        
        track_title = track.get('title', 'Unknown')
        track_number = track.get('track_number', '00')
        
        if track_number and track_number != '':
            try:
                track_num_str = f"{int(track_number):02d}"
            except (ValueError, TypeError):
                track_num_str = str(track_number).zfill(2)
        else:
            track_num_str = "00"
        
        _, ext = os.path.splitext(source_file)
        track_artist_clean = sanitize(track_artist)
        track_title_clean = sanitize(track_title)
        
        new_filename = f"{track_num_str}. {track_artist_clean} - {track_title_clean}{ext}"
        dest_file = os.path.join(album_path, new_filename)
        
        # Move file
        shutil.move(source_file, dest_file)
        logger.info(f"Moved track: {source_file} -> {dest_file}")
        
        return {
            'success': True,
            'source_file': source_file,
            'destination_file': dest_file,
            'album_path': album_path,
            'track_number': track_num_str,
            'title': track_title
        }
        
    except Exception as e:
        logger.error(f"Error organizing individual track: {e}")
        return {
            'success': False,
            'error': str(e)
        }


def get_folder_duplicates_batch(conn: Any) -> List[Dict]:
    """
    Find all folders that have potential duplicate conflicts with existing library.
    
    Returns list of folders with conflicts for batch review.
    """
    try:
        db_query = DatabaseQuery(conn)
        
        # This would require a folder tracking table in the database
        # For now, return empty list - user can populate based on their needs
        cursor = db_query.execute("""
            SELECT DISTINCT 
                folder_path,
                COUNT(*) as conflict_count
            FROM folder_duplicates
            WHERE resolved = 0
            GROUP BY folder_path
            ORDER BY conflict_count DESC
        """)
        
        results = cursor.fetchall()
        
        batch = []
        for row in results:
            batch.append({
                'folder_path': row[0],
                'conflict_count': row[1]
            })
        
        return batch
        
    except Exception as e:
        logger.error(f"Error getting duplicate batch: {e}")
        return []
