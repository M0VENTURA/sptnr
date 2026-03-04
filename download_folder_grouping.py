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
            
            # Try to detect artist/album from folder name or track metadata
            detected_artist = first_track['artist']
            detected_album = first_track['album']
            detected_year = ''
            
            # Check if all tracks share same artist/album (strong indicator of album group)
            artists = set(t['artist'] for t in tracks)
            albums = set(t['album'] for t in tracks)
            
            is_consistent_group = len(artists) == 1 and len(albums) == 1
            
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


def match_folder_group_with_musicbrainz(folder_path, artist, album, mb_client=None):
    """
    Match a folder group with MusicBrainz and return candidate releases.
    
    Args:
        folder_path: Relative folder path from downloads root
        artist: Artist name to search for
        album: Album name to search for
        mb_client: MusicBrainzClient instance (optional, will be imported if not provided)
    
    Returns:
        Dict with MusicBrainz candidates and metadata
    """
    try:
        # Import MusicBrainz client if not provided
        if mb_client is None:
            try:
                from api_clients.musicbrainz import MusicBrainzClient
                mb_client = MusicBrainzClient()
            except Exception as import_err:
                logger.error(f"Could not import MusicBrainzClient: {import_err}")
                return {
                    'folder_path': folder_path,
                    'artist': artist,
                    'album': album,
                    'candidates': [],
                    'success': False,
                    'error': f"Could not import MusicBrainz client: {import_err}"
                }
        
        logger.info(f"Searching MusicBrainz for: {artist} - {album}")
        
        # Search for releases
        search_results = mb_client.search_releases(artist=artist, release=album)
        
        if not search_results:
            logger.warning(f"No MusicBrainz matches found for: {artist} - {album}")
            return {
                'folder_path': folder_path,
                'artist': artist,
                'album': album,
                'candidates': [],
                'success': False
            }
        
        # Format results
        candidates = []
        for result in search_results[:5]:  # Top 5 results
            candidates.append({
                'id': result.get('id'),
                'title': result.get('title'),
                'artist': result.get('artist-credit', [{}])[0].get('name', ''),
                'date': result.get('date', ''),
                'country': result.get('country', ''),
                'track_count': result.get('track-count', 0)
            })
        
        return {
            'folder_path': folder_path,
            'artist': artist,
            'album': album,
            'candidates': candidates,
            'success': len(candidates) > 0
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
