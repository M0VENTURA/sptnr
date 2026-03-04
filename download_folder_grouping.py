"""
Folder-based downloads grouping and MusicBrainz matching functionality.
These functions scan the downloads folder, group by folder path, and
enable MusicBrainz metadata matching before file organization.

This module is meant to be imported into download_queue_manager.py.
"""

import os
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def scan_downloads_grouped_by_folder(downloads_dir, read_mp3_metadata):
    """
    Scan /downloads folder and group audio files by their immediate parent folder.
    This creates natural album/release groupings for MusicBrainz matching.
    
    Args:
        downloads_dir: Path to downloads directory
        read_mp3_metadata: Function to extract MP3 metadata
    
    Returns:
        Dict with structure:
        {
            'folder_groups': [
                {
                    'folder_path': 'relative/path/to/folder',
                    'folder_name': 'folder name',
                    'track_count': int,
                    'artist': 'detected artist',
                    'album': 'detected album',
                    'year': 'detected year if available',
                    'is_consistent': bool,
                    'tracks': [...]
                }
            ],
            'total_folders': int,
            'total_files': int,
            'stats': {...}
        }
    """
    try:
        if not os.path.isdir(downloads_dir):
            logger.warning(f"Downloads folder not found: {downloads_dir}")
            return {
                'folder_groups': [],
                'total_folders': 0,
                'total_files': 0,
                'error': f"Downloads folder not found: {downloads_dir}"
            }
        
        logger.info(f"Scanning downloads folder for grouped files: {downloads_dir}")
        
        audio_extensions = {'.mp3', '.flac', '.m4a', '.ogg', '.wav'}
        folder_groups = {}  # key: folder path, value: list of file info dicts
        
        # Scan downloads folder recursively
        for root, dirs, files in os.walk(downloads_dir):
            audio_files_in_folder = []
            
            for filename in files:
                file_ext = os.path.splitext(filename)[1].lower()
                if file_ext in audio_extensions:
                    full_path = os.path.join(root, filename)
                    rel_path = os.path.relpath(full_path, downloads_dir)
                    
                    # Extract metadata
                    metadata = {}
                    try:
                        metadata = read_mp3_metadata(full_path)
                    except Exception as e:
                        logger.debug(f"Could not read metadata from {filename}: {e}")
                    
                    track_info = {
                        'file_path': full_path,
                        'filename': filename,
                        'rel_path': rel_path,
                        'artist': metadata.get('artist', 'Unknown Artist'),
                        'album': metadata.get('album', 'Unknown Album'),
                        'title': metadata.get('title', os.path.splitext(filename)[0]),
                        'track_number': metadata.get('track', ''),
                        'duration': metadata.get('duration', 0)
                    }
                    audio_files_in_folder.append(track_info)
            
            # Group files from this folder
            if audio_files_in_folder:
                # Use relative folder path as group key
                rel_folder = os.path.relpath(root, downloads_dir)
                if rel_folder == '.':
                    rel_folder = ''  # Root level
                
                folder_groups[rel_folder] = audio_files_in_folder
        
        # Convert folder_groups dict to structured output
        grouped_output = []
        
        for folder_path, tracks in sorted(folder_groups.items()):
            if not tracks:
                continue
            
            # Extract common metadata from group (use first track as reference)
            first_track = tracks[0]
            folder_name = os.path.basename(folder_path) if folder_path else 'Downloads Root'
            
            # Try to detect artist/album from folder name pattern (Artist - Album, Artist-Album, etc.)
            detected_artist = first_track['artist']
            detected_album = first_track['album']
            detected_year = ''
            
            # Parse folder name for better artist/album detection
            # Common patterns: "Artist - Album", "Artist-Album (Year)", "Album (Year)"
            import re
            if folder_name and folder_name != 'Downloads Root':
                # Try "Artist - Album" pattern
                match = re.match(r'^(.+?)\s*[-–—]\s*(.+?)(?:\s*\((\d{4})\))?$', folder_name)
                if match:
                    detected_artist = match.group(1).strip()
                    detected_album = match.group(2).strip()
                    if match.group(3):
                        detected_year = match.group(3)
                else:
                    # If no artist separator found, use folder name as album
                    year_match = re.search(r'\((\d{4})\)', folder_name)
                    if year_match:
                        detected_year = year_match.group(1)
                        detected_album = folder_name.replace(f'({detected_year})', '').strip()
                    else:
                        detected_album = folder_name
            
            # Check if all tracks share same artist/album (strong indicator of album group)
            artists = set(t['artist'] for t in tracks if t['artist'] != 'Unknown Artist')
            albums = set(t['album'] for t in tracks if t['album'] != 'Unknown Album')
            
            # Use track metadata if available and consistent
            if len(artists) == 1:
                detected_artist = list(artists)[0]
            if len(albums) == 1:
                detected_album = list(albums)[0]
            
            is_consistent_group = len(artists) <= 1 and len(albums) <= 1
            
            group_data = {
                'folder_path': folder_path,
                'folder_name': folder_name,
                'track_count': len(tracks),
                'artist': detected_artist,
                'album': detected_album,
                'year': detected_year,
                'is_consistent': is_consistent_group,
                'tracks': sorted(tracks, key=lambda t: (t['track_number'] or ''))
            }
            
            grouped_output.append(group_data)
        
        logger.info(f"Grouped {sum(len(t) for t in folder_groups.values())} files into {len(folder_groups)} folders")
        
        return {
            'folder_groups': grouped_output,
            'total_folders': len(folder_groups),
            'total_files': sum(len(t) for t in folder_groups.values()),
            'stats': {
                'folders_consistent': sum(1 for g in grouped_output if g['is_consistent']),
                'folders_mixed': sum(1 for g in grouped_output if not g['is_consistent'])
            }
        }
        
    except Exception as e:
        logger.error(f"Error scanning downloads folder: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {
            'folder_groups': [],
            'total_folders': 0,
            'total_files': 0,
            'error': str(e)
        }


def match_folder_group_with_musicbrainz(folder_path, artist, album, mb_client=None, manual_query=None):
    """
    Match a folder group with MusicBrainz and return candidate releases.
    Falls back to Discogs if MusicBrainz returns no results.
    
    Args:
        folder_path: Relative folder path from downloads root
        artist: Artist name to search for
        album: Album name to search for
        mb_client: Not used (kept for compatibility)
        manual_query: Optional manual search query string
    
    Returns:
        Dict with MusicBrainz/Discogs candidates and metadata
    """
    import requests
    import time
    
    try:
        # Use manual query if provided, otherwise use artist/album
        if manual_query:
            search_artist = manual_query
            search_album = ''
            logger.info(f"Manual search for: {manual_query}")
        else:
            # Clean up artist and album names for better matching
            search_artist = artist.replace('Unknown Artist', '').strip()
            search_album = album.replace('Unknown Album', '').strip()
            
            # Remove common suffixes that might interfere with matching
            import re
            search_album = re.sub(r'\s*\((?:Deluxe|Limited|Special|Expanded|Remaster).*?\)\s*$', '', search_album, flags=re.IGNORECASE)
            search_album = re.sub(r'\s*\[(?:Deluxe|Limited|Special|Expanded|Remaster).*?\]\s*$', '', search_album, flags=re.IGNORECASE)
            
            logger.info(f"Searching MusicBrainz for: {search_artist} - {search_album}")
        
        # MusicBrainz API base URL
        base_url = "https://musicbrainz.org/ws/2/"
        headers = {
            "User-Agent": "sptnr/2.0.0 ( https://github.com/M0VENTURA/sptnr )",
            "Accept": "application/json"
        }
        
        # Respect MusicBrainz rate limit (1 request per second)
        time.sleep(1.0)
        
        # Build query - use manual query or artist+album
        if manual_query:
            query = manual_query
        elif search_artist and search_album:
            query = f'artist:"{search_artist}" AND release:"{search_album}"'
        elif search_artist:
            query = f'artist:"{search_artist}"'
        elif search_album:
            query = f'release:"{search_album}"'
        else:
            return {
                'folder_path': folder_path,
                'artist': artist,
                'album': album,
                'candidates': [],
                'success': False,
                'error': 'No search terms provided'
            }
        
        params = {
            "query": query,
            "fmt": "json",
            "limit": 10  # Get top 10 matches for better selection
        }
        
        logger.debug(f"MusicBrainz request: {base_url}release/ params={params}")
        
        response = requests.get(
            f"{base_url}release/",
            params=params,
            headers=headers,
            timeout=(5, 10)
        )
        
        response.raise_for_status()
        data = response.json()
        
        releases = data.get('releases', [])
        
        # If MusicBrainz returns no results, try Discogs as fallback
        if not releases and not manual_query:
            logger.info(f"No MusicBrainz matches, trying Discogs fallback for: {artist} - {album}")
            return try_discogs_match(folder_path, search_artist, search_album)
        
        if not releases:
            logger.warning(f"No matches found for: {query}")
            return {
                'folder_path': folder_path,
                'artist': artist,
                'album': album,
                'candidates': [],
                'success': False
            }
        
        # Format results
        candidates = []
        for release in releases:
            # Get artist name from artist-credit
            artist_name = ''
            if release.get('artist-credit'):
                artist_name = release['artist-credit'][0].get('name', '')
            
            candidates.append({
                'id': release.get('id'),
                'title': release.get('title', ''),
                'artist': artist_name,
                'date': release.get('date', ''),
                'country': release.get('country', ''),
                'track_count': release.get('track-count', 0),
                'source': 'musicbrainz'
            })
        
        logger.info(f"Found {len(candidates)} MusicBrainz candidates")
        
        return {
            'folder_path': folder_path,
            'artist': artist,
            'album': album,
            'candidates': candidates[:5],  # Return top 5
            'success': len(candidates) > 0
        }
        
    except requests.exceptions.RequestException as e:
        logger.error(f"MusicBrainz API request failed: {e}")
        # Try Discogs fallback on API error
        if not manual_query:
            logger.info("Trying Discogs fallback due to API error")
            return try_discogs_match(folder_path, artist, album)
        return {
            'folder_path': folder_path,
            'artist': artist,
            'album': album,
            'candidates': [],
            'success': False,
            'error': f"API request failed: {e}"
        }
    except Exception as e:
        logger.error(f"Error matching with MusicBrainz: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {
            'folder_path': folder_path,
            'artist': artist,
            'album': album,
            'candidates': [],
            'success': False,
            'error': str(e)
        }


def try_discogs_match(folder_path, artist, album):
    """
    Try to match using Discogs as a fallback.
    
    Args:
        folder_path: Relative folder path
        artist: Artist name
        album: Album name
    
    Returns:
        Dict with Discogs candidates
    """
    try:
        from api_clients.discogs import DiscogsClient
        
        discogs = DiscogsClient()
        
        # Search Discogs for the release
        query = f"{artist} {album}".strip()
        logger.info(f"Searching Discogs for: {query}")
        
        results = discogs.search_releases(query, limit=5)
        
        if not results:
            logger.warning(f"No Discogs matches found for: {query}")
            return {
                'folder_path': folder_path,
                'artist': artist,
                'album': album,
                'candidates': [],
                'success': False
            }
        
        # Format Discogs results to match MusicBrainz format
        candidates = []
        for result in results:
            candidates.append({
                'id': str(result.get('id', '')),
                'title': result.get('title', ''),
                'artist': result.get('artist', artist),
                'date': str(result.get('year', '')),
                'country': result.get('country', ''),
                'track_count': 0,  # Discogs search doesn't return track count
                'source': 'discogs'
            })
        
        logger.info(f"Found {len(candidates)} Discogs candidates")
        
        return {
            'folder_path': folder_path,
            'artist': artist,
            'album': album,
            'candidates': candidates,
            'success': len(candidates) > 0
        }
        
    except Exception as e:
        logger.error(f"Discogs fallback failed: {e}")
        return {
            'folder_path': folder_path,
            'artist': artist,
            'album': album,
            'candidates': [],
            'success': False,
            'error': f"Discogs search failed: {e}"
        }


def apply_matched_metadata_to_folder(folder_path, tracks, matched_metadata):
    """
    Apply MusicBrainz matched metadata to a folder group's tracks.
    
    Args:
        folder_path: Relative folder path
        tracks: List of track dicts for the folder
        matched_metadata: MusicBrainz release metadata
    
    Returns:
        List of tracks with updated metadata
    """
    try:
        updated_tracks = []
        
        # Get track listing from matched metadata if available
        mb_tracks = matched_metadata.get('tracks', [])
        
        for i, track in enumerate(tracks):
            updated_track = track.copy()
            
            # Try to match with MusicBrainz track number order
            if i < len(mb_tracks):
                mb_track = mb_tracks[i]
                updated_track['mb_title'] = mb_track.get('title')
                updated_track['mb_track_number'] = mb_track.get('position')
                updated_track['mb_duration'] = mb_track.get('length')
            
            # Apply release-level metadata
            updated_track['matched_artist'] = matched_metadata.get('artist', track['artist'])
            updated_track['matched_album'] = matched_metadata.get('title', track['album'])
            updated_track['matched_date'] = matched_metadata.get('date')
            updated_track['mb_release_id'] = matched_metadata.get('id')
            
            updated_tracks.append(updated_track)
        
        return updated_tracks
        
    except Exception as e:
        logger.error(f"Error applying matched metadata: {e}")
        return tracks


def organize_folder_to_music(folder_path, tracks, release_metadata, music_dir="/music"):
    """
    Organize tracks from a folder into the /music directory structure.
    Format: /music/Album Artist/Year - Album/Track Number. Artist - Track Title.ext
    
    Args:
        folder_path: Relative folder path from downloads
        tracks: List of track dicts
        release_metadata: Matched release metadata (album, artist, date, etc.)
        music_dir: Base music directory path
    
    Returns:
        Dict with success status and organized file paths
    """
    import shutil
    import re
    
    try:
        organized_files = []
        errors = []
        
        # Extract metadata
        album_artist = release_metadata.get('artist', 'Unknown Artist')
        album_title = release_metadata.get('title', 'Unknown Album')
        release_date = release_metadata.get('date', '')
        release_year = release_date.split('-')[0] if release_date else ''
        
        # Sanitize names for filesystem
        def sanitize(name):
            # Remove invalid filesystem characters
            name = re.sub(r'[<>:"/\\|?*]', '', name)
            # Remove leading/trailing dots and spaces
            name = name.strip('. ')
            return name
        
        album_artist_clean = sanitize(album_artist)
        album_title_clean = sanitize(album_title)
        
        # Create album folder: "Year - Album" or just "Album" if no year
        if release_year:
            album_folder_name = f"{release_year} - {album_title_clean}"
        else:
            album_folder_name = album_title_clean
        
        # Full album path: /music/Album Artist/Year - Album/
        album_path = os.path.join(music_dir, album_artist_clean, album_folder_name)
        
        # Create album directory if it doesn't exist
        os.makedirs(album_path, exist_ok=True)
        logger.info(f"Created album directory: {album_path}")
        
        # Organize each track
        for track in tracks:
            try:
                source_file = track.get('file_path')
                if not source_file or not os.path.exists(source_file):
                    errors.append(f"Source file not found: {source_file}")
                    continue
                
                # Get track metadata
                track_artist = track.get('artist', album_artist)
                track_title = track.get('title', 'Unknown Track')
                track_number = track.get('track_number', '')
                
                # Get file extension
                _, ext = os.path.splitext(source_file)
                
                # Format track number with leading zero
                if track_number:
                    try:
                        track_num_str = f"{int(track_number):02d}"
                    except (ValueError, TypeError):
                        track_num_str = str(track_number).zfill(2)
                else:
                    track_num_str = "00"
                
                # Sanitize track metadata
                track_artist_clean = sanitize(track_artist)
                track_title_clean = sanitize(track_title)
                
                # Create filename: "01. Artist - Track Title.ext"
                new_filename = f"{track_num_str}. {track_artist_clean} - {track_title_clean}{ext}"
                dest_file = os.path.join(album_path, new_filename)
                
                # Move file
                shutil.move(source_file, dest_file)
                logger.info(f"Moved: {source_file} -> {dest_file}")
                
                organized_files.append({
                    'source': source_file,
                    'destination': dest_file,
                    'track_number': track_num_str,
                    'title': track_title
                })
                
            except Exception as track_error:
                error_msg = f"Failed to organize {track.get('filename', 'unknown')}: {track_error}"
                logger.error(error_msg)
                errors.append(error_msg)
        
        # Try to remove empty source folder
        try:
            source_folder = os.path.join(os.path.dirname(tracks[0]['file_path']))
            if os.path.exists(source_folder) and not os.listdir(source_folder):
                os.rmdir(source_folder)
                logger.info(f"Removed empty source folder: {source_folder}")
        except Exception as e:
            logger.debug(f"Could not remove source folder: {e}")
        
        return {
            'success': len(organized_files) > 0,
            'organized_count': len(organized_files),
            'total_tracks': len(tracks),
            'album_path': album_path,
            'organized_files': organized_files,
            'errors': errors
        }
        
    except Exception as e:
        logger.error(f"Error organizing folder to music: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {
            'success': False,
            'error': str(e),
            'organized_count': 0,
            'total_tracks': len(tracks)
        }
