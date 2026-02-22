#!/usr/bin/env python3
"""
Compilation and Artist Credits Manager - Handle featured artists, performers, and compilation tracks.
Imports featured artists from MP3 tags and creates compilation track listings per artist.
"""

import os
import sqlite3
import json
from pathlib import Path
from helpers.metadata_reader import read_mp3_metadata, find_track_file


def parse_artists_field(artists_raw):
    """
    Parse the raw ARTISTS field from MP3 tags (usually JSON array).
    
    Args:
        artists_raw: Raw artists field value (JSON array or delimited string)
        
    Returns:
        list: List of artist names
    """
    if not artists_raw:
        return []
    
    try:
        # Try parsing as JSON array first
        if artists_raw.startswith('['):
            data = json.loads(artists_raw)
            if isinstance(data, list):
                return [str(artist).strip() for artist in data if artist]
        
        # Try splitting on common delimiters
        # Common separators: comma, semicolon, pipe, semicolon+space
        for sep in ['; ', ';', ' | ', '|', ', ', ',']:
            if sep in artists_raw:
                return [artist.strip() for artist in artists_raw.split(sep) if artist.strip()]
        
        # If no delimiters found, return as single-item list
        if artists_raw.strip():
            return [artists_raw.strip()]
    
    except Exception as e:
        pass
    
    return []


def import_featured_artists_for_track(artist, album, title, file_path=None, db_path="/database/sptnr.db", music_root="/music"):
    """
    Import featured artists and compilation info for a single track.
    
    Args:
        artist: Album artist name
        album: Album name
        title: Track title
        file_path: Optional pre-determined file path
        db_path: Path to database
        music_root: Root music directory
        
    Returns:
        dict: Status {'success': bool, 'featured_artists': list, 'message': str}
    """
    try:
        # Find the MP3 file
        if not file_path:
            file_path = find_track_file(artist, album, title, music_root=music_root)
        
        if not file_path or not os.path.exists(file_path):
            return {
                'success': False,
                'featured_artists': [],
                'message': f"Could not find MP3 file for {artist} - {album} - {title}"
            }
        
        # Read metadata from MP3
        metadata = read_mp3_metadata(file_path)
        
        # Get the featured artists
        featured_artists = []
        performers = []
        
        if 'artists_raw' in metadata:
            featured_artists = parse_artists_field(metadata['artists_raw'])
        
        if 'performer_raw' in metadata:
            performers = parse_artists_field(metadata['performer_raw'])
        
        # Determine album artist (prefer raw tag over standard field)
        album_artist = metadata.get('album_artist', artist)
        
        # Filter out the album artist from featured artists (to find actual collaborators)
        compilation_artists = [
            a for a in featured_artists + performers 
            if a.lower() != album_artist.lower()
        ]
        compilation_artists = list(set(compilation_artists))  # Remove duplicates
        
        is_compilation = len(compilation_artists) > 0
        
        # Update database
        conn = sqlite3.connect(db_path, timeout=120.0)
        cursor = conn.cursor()
        
        cursor.execute("""
            UPDATE tracks 
            SET 
                featured_artists = ?,
                performers = ?,
                is_compilation_track = ?,
                compilation_artists = ?
            WHERE artist = ? AND album = ? AND title = ?
        """, (
            json.dumps(featured_artists) if featured_artists else None,
            json.dumps(performers) if performers else None,
            1 if is_compilation else 0,
            json.dumps(compilation_artists) if compilation_artists else None,
            album_artist,  # Use album_artist from metadata
            album,
            title
        ))
        conn.commit()
        conn.close()
        
        return {
            'success': True,
            'featured_artists': featured_artists,
            'performers': performers,
            'compilation_artists': compilation_artists,
            'is_compilation': is_compilation,
            'album_artist': album_artist,
            'message': f"Imported {len(featured_artists)} featured artists" + (
                f" and {len(compilation_artists)} collaboration artists" if compilation_artists else ""
            )
        }
        
    except Exception as e:
        return {
            'success': False,
            'featured_artists': [],
            'message': f"Error importing featured artists: {e}"
        }


def import_featured_artists_for_album(artist, album, db_path="/database/sptnr.db", music_root="/music"):
    """
    Import featured artists for all tracks in an album.
    
    Args:
        artist: Artist name
        album: Album name
        db_path: Path to database
        music_root: Root music directory
        
    Returns:
        dict: Status with counts
    """
    try:
        conn = sqlite3.connect(db_path, timeout=120.0)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Get all tracks for this album
        cursor.execute("""
            SELECT id, artist, album, title, file_path, beets_path 
            FROM tracks 
            WHERE artist = ? AND album = ?
            ORDER BY track_number
        """, (artist, album))
        
        tracks = cursor.fetchall()
        conn.close()
        
        total = len(tracks)
        imported = 0
        
        for track in tracks:
            file_path = track['beets_path'] or track['file_path']
            result = import_featured_artists_for_track(
                track['artist'],
                track['album'],
                track['title'],
                file_path=file_path,
                db_path=db_path,
                music_root=music_root
            )
            
            if result['success']:
                imported += 1
        
        return {
            'success': True,
            'total': total,
            'imported': imported,
            'message': f"Imported featured artists for {imported}/{total} tracks in {artist} - {album}"
        }
        
    except Exception as e:
        return {
            'success': False,
            'total': 0,
            'imported': 0,
            'message': f"Error importing album: {e}"
        }


def get_compilations_for_artist(artist, db_path="/database/sptnr.db"):
    """
    Get all compilation tracks (songs where this artist is featured but not the album artist).
    
    Args:
        artist: Artist name
        db_path: Path to database
        
    Returns:
        list: List of compilation tracks
    """
    try:
        conn = sqlite3.connect(db_path, timeout=120.0)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Find tracks where this artist appears in compilation_artists (featured artist)
        # but is NOT the album artist
        cursor.execute("""
            SELECT 
                id, title, album, artist as album_artist,
                featured_artists, compilation_artists,
                score, stars, navidrome_rating
            FROM tracks 
            WHERE is_compilation_track = 1
            AND compilation_artists LIKE ?
            AND LOWER(artist) != LOWER(?)
            ORDER BY album, track_number
        """, (f'%"{artist}"%', artist))
        
        rows = cursor.fetchall()
        conn.close()
        
        result = []
        for row in rows:
            track_data = dict(row)
            # Parse JSON arrays
            try:
                track_data['featured_artists'] = json.loads(track_data['featured_artists']) if track_data['featured_artists'] else []
                track_data['compilation_artists'] = json.loads(track_data['compilation_artists']) if track_data['compilation_artists'] else []
            except:
                track_data['featured_artists'] = []
                track_data['compilation_artists'] = []
            
            result.append(track_data)
        
        return result
        
    except Exception as e:
        return []


def get_main_tracks_for_artist(artist, db_path="/database/sptnr.db"):
    """
    Get all main tracks (where this artist is the album artist).
    
    Args:
        artist: Artist name
        db_path: Path to database
        
    Returns:
        list: List of album tracks grouped by album
    """
    try:
        conn = sqlite3.connect(db_path, timeout=120.0)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Get all tracks where artist is the album artist
        cursor.execute("""
            SELECT 
                id, title, album, track_number,
                featured_artists, compilation_artists,
                score, stars, navidrome_rating
            FROM tracks 
            WHERE LOWER(artist) = LOWER(?)
            ORDER BY album, track_number
        """, (artist,))
        
        rows = cursor.fetchall()
        conn.close()
        
        result = []
        for row in rows:
            track_data = dict(row)
            # Parse JSON arrays
            try:
                track_data['featured_artists'] = json.loads(track_data['featured_artists']) if track_data['featured_artists'] else []
                track_data['compilation_artists'] = json.loads(track_data['compilation_artists']) if track_data['compilation_artists'] else []
            except:
                track_data['featured_artists'] = []
                track_data['compilation_artists'] = []
            
            result.append(track_data)
        
        return result
        
    except Exception as e:
        return []


def get_artist_stats(artist, db_path="/database/sptnr.db"):
    """
    Get statistics for an artist (main tracks, compilation appearances, collaborations).
    
    Args:
        artist: Artist name
        db_path: Path to database
        
    Returns:
        dict: Artist statistics
    """
    try:
        main_tracks = get_main_tracks_for_artist(artist, db_path)
        compilation_tracks = get_compilations_for_artist(artist, db_path)
        
        # Count collaborators on main tracks
        collaborators = set()
        for track in main_tracks:
            if track.get('collaboration_artists'):
                collaborators.update(track['collaboration_artists'])
        
        # Count featured appearances
        featured_count = 0
        for track in compilation_tracks:
            if artist in track.get('featured_artists', []):
                featured_count += 1
        
        return {
            'artist': artist,
            'main_albums_count': len(set(t['album'] for t in main_tracks)),
            'main_tracks_count': len(main_tracks),
            'compilation_appearances': len(compilation_tracks),
            'featured_appearances': featured_count,
            'collaborators_count': len(collaborators),
            'collaborators': sorted(list(collaborators))
        }
        
    except Exception as e:
        return {
            'artist': artist,
            'error': str(e)
        }


if __name__ == "__main__":
    import sys
    import json
    
    if len(sys.argv) < 2:
        print("Usage: python compilation_manager.py <artist> [action]")
        print("Actions: main (default), compilations, stats")
        sys.exit(1)
    
    artist = sys.argv[1]
    action = sys.argv[2] if len(sys.argv) > 2 else "main"
    
    if action == "main":
        tracks = get_main_tracks_for_artist(artist)
        print(json.dumps(tracks, indent=2))
    elif action == "compilations":
        tracks = get_compilations_for_artist(artist)
        print(json.dumps(tracks, indent=2))
    elif action == "stats":
        stats = get_artist_stats(artist)
        print(json.dumps(stats, indent=2))
    else:
        print(f"Unknown action: {action}")
