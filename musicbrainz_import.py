#!/usr/bin/env python3
"""
MusicBrainz Tag Importer - Extract raw MusicBrainz tags from MP3 files and import into database.
This allows viewing and mapping MusicBrainz metadata on album/artist pages.
Also supports writing tags back to MP3 files for persistence.
"""

import os
import sqlite3
from pathlib import Path
from helpers.metadata_reader import (
    read_musicbrainz_tags_from_mp3, 
    write_musicbrainz_tags_to_mp3,
    find_track_file
)

# Mapping of MP3 tag fields to database columns
MB_FIELD_MAPPING = {
    'musicbrainz_artistid': 'musicbrainz_track_artistid',         # Track artist ID
    'musicbrainz_albumartistid': 'musicbrainz_albumartistid',     # Album artist ID
    'musicbrainz_albumid': 'musicbrainz_albumid',                 # Album/Release ID
    'musicbrainz_trackid': 'musicbrainz_trackid',                 # Track/Recording ID
    'musicbrainz_releasegroupid': 'musicbrainz_releasegroupid',   # Release group ID
    'musicbrainz_releasetrackid': 'musicbrainz_releasetrackid',   # Release track ID
    'musicbrainz_workid': 'musicbrainz_workid',                   # Work ID
    'musicbrainz_releasestatus': 'musicbrainz_releasestatus',     # Release status
    'musicbrainz_releasetype': 'musicbrainz_releasetype',         # Release type
    'musicbrainz_releasecountry': 'musicbrainz_releasecountry',   # Release country
}

# Reverse mapping for writing to MP3
DB_TO_MP3_FIELD_MAPPING = {v: k for k, v in MB_FIELD_MAPPING.items()}


def import_musicbrainz_tags_for_track(artist, album, title, file_path=None, db_path="/database/sptnr.db"):
    """
    Import MusicBrainz tags from a track's MP3 file into the database.
    
    Args:
        artist: Artist name
        album: Album name
        title: Track title
        file_path: Optional pre-determined file path
        db_path: Path to database
        
    Returns:
        dict: Status {'success': bool, 'tags_found': int, 'message': str}
    """
    try:
        # Step 1: Find the MP3 file
        if not file_path:
            music_root = os.environ.get("MUSIC_ROOT", "/music")
            file_path = find_track_file(artist, album, title, music_root=music_root)
        
        if not file_path or not os.path.exists(file_path):
            return {
                'success': False,
                'tags_found': 0,
                'message': f"Could not find MP3 file for {artist} - {album} - {title}"
            }
        
        # Step 2: Extract MusicBrainz tags from MP3
        mb_tags = read_musicbrainz_tags_from_mp3(file_path)
        
        if not mb_tags:
            return {
                'success': True,
                'tags_found': 0,
                'message': f"No MusicBrainz tags found in {file_path}"
            }
        
        # Step 3: Update database with the tags
        conn = sqlite3.connect(db_path, timeout=120.0)
        cursor = conn.cursor()
        
        # Build the UPDATE query dynamically
        update_sets = []
        values = []
        
        for mp3_field, db_column in MB_FIELD_MAPPING.items():
            if mp3_field in mb_tags:
                update_sets.append(f"{db_column} = ?")
                values.append(mb_tags[mp3_field])
        
        if not update_sets:
            conn.close()
            return {
                'success': True,
                'tags_found': 0,
                'message': "No mappable MusicBrainz tags found"
            }
        
        # Add the WHERE clause
        values.append(artist)
        values.append(album)
        values.append(title)
        
        query = f"""
            UPDATE tracks 
            SET {', '.join(update_sets)}
            WHERE artist = ? AND album = ? AND title = ?
        """
        
        cursor.execute(query, values)
        conn.commit()
        
        tags_count = sum(1 for k in mb_tags if k in MB_FIELD_MAPPING)
        conn.close()
        
        return {
            'success': True,
            'tags_found': tags_count,
            'message': f"Imported {tags_count} MusicBrainz tags from {os.path.basename(file_path)}",
            'tags': mb_tags
        }
        
    except Exception as e:
        return {
            'success': False,
            'tags_found': 0,
            'message': f"Error importing MusicBrainz tags: {e}"
        }


def import_musicbrainz_tags_for_album(artist, album, db_path="/database/sptnr.db", music_root="/music"):
    """
    Import MusicBrainz tags for all tracks in an album.
    
    Args:
        artist: Artist name
        album: Album name
        db_path: Path to database
        music_root: Root music directory
        
    Returns:
        dict: Status with counts {'success': bool, 'total': int, 'imported': int, 'skipped': int}
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
        skipped = 0
        
        for track in tracks:
            file_path = track['beets_path'] or track['file_path']
            result = import_musicbrainz_tags_for_track(
                track['artist'],
                track['album'],
                track['title'],
                file_path=file_path,
                db_path=db_path
            )
            
            if result['success'] and result['tags_found'] > 0:
                imported += 1
            else:
                skipped += 1
        
        return {
            'success': True,
            'total': total,
            'imported': imported,
            'skipped': skipped,
            'message': f"Imported MusicBrainz tags for {imported}/{total} tracks in {artist} - {album}"
        }
        
    except Exception as e:
        return {
            'success': False,
            'total': 0,
            'imported': 0,
            'skipped': 0,
            'message': f"Error importing album: {e}"
        }


def import_musicbrainz_tags_for_artist(artist, db_path="/database/sptnr.db", music_root="/music"):
    """
    Import MusicBrainz tags for all tracks by an artist.
    
    Args:
        artist: Artist name
        db_path: Path to database
        music_root: Root music directory
        
    Returns:
        dict: Status with counts
    """
    try:
        conn = sqlite3.connect(db_path, timeout=120.0)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Get all albums by artist
        cursor.execute("""
            SELECT DISTINCT album FROM tracks WHERE artist = ? ORDER BY album
        """, (artist,))
        
        albums = [row['album'] for row in cursor.fetchall()]
        conn.close()
        
        total_imported = 0
        total_tracks = 0
        
        for album in albums:
            result = import_musicbrainz_tags_for_album(artist, album, db_path, music_root)
            total_imported += result.get('imported', 0)
            total_tracks += result.get('total', 0)
        
        return {
            'success': True,
            'total': total_tracks,
            'imported': total_imported,
            'albums': len(albums),
            'message': f"Imported MusicBrainz tags for {total_imported}/{total_tracks} tracks by {artist}"
        }
        
    except Exception as e:
        return {
            'success': False,
            'message': f"Error importing artist: {e}"
        }


def get_musicbrainz_tags_for_track(artist, album, title, db_path="/database/sptnr.db"):
    """
    Get all MusicBrainz tags stored for a track in the database.
    
    Args:
        artist: Artist name
        album: Album name
        title: Track title
        db_path: Path to database
        
    Returns:
        dict: MusicBrainz tags or empty dict if not found
    """
    try:
        conn = sqlite3.connect(db_path, timeout=120.0)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                musicbrainz_track_artistid,
                musicbrainz_albumartistid,
                musicbrainz_albumid,
                musicbrainz_trackid,
                musicbrainz_releasegroupid,
                musicbrainz_releasetrackid,
                musicbrainz_workid,
                musicbrainz_releasestatus,
                musicbrainz_releasetype,
                musicbrainz_releasecountry,
                musicbrainz_albumstatus,
                musicbrainz_albumtype
            FROM tracks 
            WHERE artist = ? AND album = ? AND title = ?
        """, (artist, album, title))
        
        row = cursor.fetchone()
        conn.close()
        
        if not row:
            return {}
        
        # Convert to dict and filter out None values
        result = {}
        for key, value in dict(row).items():
            if value:
                result[key] = value
        
        return result
        
    except Exception as e:
        return {}


def get_musicbrainz_tags_for_album(artist, album, db_path="/database/sptnr.db"):
    """
    Get MusicBrainz tags for all tracks in an album.
    
    Args:
        artist: Artist name
        album: Album name
        db_path: Path to database
        
    Returns:
        list: List of dicts with track info and MusicBrainz tags
    """
    try:
        conn = sqlite3.connect(db_path, timeout=120.0)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                title, track_number,
                musicbrainz_track_artistid,
                musicbrainz_albumartistid,
                musicbrainz_albumid,
                musicbrainz_trackid,
                musicbrainz_releasegroupid,
                musicbrainz_releasetrackid,
                musicbrainz_workid,
                musicbrainz_releasestatus,
                musicbrainz_releasetype,
                musicbrainz_releasecountry
            FROM tracks 
            WHERE artist = ? AND album = ?
            ORDER BY track_number
        """, (artist, album))
        
        rows = cursor.fetchall()
        conn.close()
        
        result = []
        for row in rows:
            track_data = dict(row)
            # Filter out None values
            track_data = {k: v for k, v in track_data.items() if v is not None}
            result.append(track_data)
        
        return result
        
    except Exception as e:
        return []


def update_musicbrainz_tag_in_db(artist, album, title, field_name, field_value, db_path="/database/sptnr.db"):
    """
    Update a single MusicBrainz tag in the database.
    
    Args:
        artist: Artist name
        album: Album name
        title: Track title
        field_name: Database field name (e.g., 'musicbrainz_trackid')
        field_value: New value for the field
        db_path: Path to database
        
    Returns:
        dict: Status {'success': bool, 'message': str}
    """
    try:
        # Validate field name is in our mapping
        if field_name not in MB_FIELD_MAPPING.values() and field_name not in ['musicbrainz_albumstatus', 'musicbrainz_albumtype']:
            return {
                'success': False,
                'message': f"Field '{field_name}' is not a valid MusicBrainz field"
            }
        
        conn = sqlite3.connect(db_path, timeout=120.0)
        cursor = conn.cursor()
        
        # Update the field
        query = f"UPDATE tracks SET {field_name} = ? WHERE artist = ? AND album = ? AND title = ?"
        cursor.execute(query, (field_value, artist, album, title))
        conn.commit()
        
        affected = cursor.rowcount
        conn.close()
        
        if affected > 0:
            return {
                'success': True,
                'message': f"Updated {field_name} for {artist} - {album} - {title}"
            }
        else:
            return {
                'success': False,
                'message': f"Track not found: {artist} - {album} - {title}"
            }
        
    except Exception as e:
        return {
            'success': False,
            'message': f"Error updating database: {e}"
        }


def write_musicbrainz_tag_to_mp3_file(file_path, **kwargs):
    """
    Generic function to write MusicBrainz tags to MP3 file.
    
    Args:
        file_path: Path to MP3 file
        **kwargs: MusicBrainz field name and value pairs (e.g., musicbrainz_trackid='xxx', musicbrainz_releasetype='Album')
        
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        from mutagen.id3 import ID3, TXXX
        from mutagen.mp3 import MP3
        
        if not os.path.exists(file_path):
            return False
        
        audio = MP3(file_path, ID3=ID3)
        
        # Create ID3 tags if they don't exist
        if audio.tags is None:
            audio.add_tags()
        
        # Map of field names to TXXX descriptors
        field_to_descriptor = {
            'musicbrainz_artistid': 'MUSICBRAINZ ARTIST ID',
            'musicbrainz_albumartistid': 'MUSICBRAINZ ALBUM ARTIST ID',
            'musicbrainz_albumid': 'MUSICBRAINZ ALBUM ID',
            'musicbrainz_trackid': 'MUSICBRAINZ TRACK ID',
            'musicbrainz_releasegroupid': 'MUSICBRAINZ RELEASE GROUP ID',
            'musicbrainz_releasetrackid': 'MUSICBRAINZ RELEASE TRACK ID',
            'musicbrainz_workid': 'MUSICBRAINZ WORK ID',
            'musicbrainz_releasestatus': 'MUSICBRAINZ RELEASE STATUS',
            'musicbrainz_releasetype': 'MUSICBRAINZ RELEASE TYPE',
            'musicbrainz_releasecountry': 'MUSICBRAINZ RELEASE COUNTRY',
        }
        
        # Write each field
        for field_name, field_value in kwargs.items():
            if field_name not in field_to_descriptor:
                continue
            
            descriptor = field_to_descriptor[field_name]
            
            # Remove existing TXXX frame with this descriptor if present
            for key in list(audio.tags.keys()):
                if key.startswith('TXXX'):
                    frame = audio.tags[key]
                    if hasattr(frame, 'desc') and frame.desc.upper() == descriptor.upper():
                        del audio.tags[key]
                        break
            
            # Add new TXXX frame
            audio.tags.add(TXXX(
                encoding=3,
                desc=descriptor,
                text=[str(field_value)]
            ))
        
        # Save changes
        audio.save()
        return True
        
    except Exception as e:
        return False


def write_musicbrainz_tag_to_mp3(artist, album, title, field_name, field_value, db_path="/database/sptnr.db", music_root="/music"):
    """
    Write a MusicBrainz tag to an MP3 file.
    
    Args:
        artist: Artist name
        album: Album name
        title: Track title
        field_name: Database field name
        field_value: Value to write
        db_path: Path to database
        music_root: Root music directory
        
    Returns:
        dict: Status {'success': bool, 'message': str}
    """
    try:
        # Find the MP3 file
        file_path = find_track_file(artist, album, title, music_root=music_root)
        
        if not file_path or not os.path.exists(file_path):
            return {
                'success': False,
                'message': f"Could not find MP3 file for {artist} - {album} - {title}"
            }
        
        # Map database field name to MP3 tag field name
        mp3_field_name = DB_TO_MP3_FIELD_MAPPING.get(field_name)
        
        if not mp3_field_name:
            return {
                'success': False,
                'message': f"Cannot map database field '{field_name}' to MP3 tag"
            }
        
        # Build tag dict with only this field
        tag_kwargs = {mp3_field_name: field_value} if mp3_field_name else {}
        
        if not tag_kwargs:
            return {
                'success': False,
                'message': f"No MP3 tag mapping for {field_name}"
            }
        
        # Call the write function (handles field-specific writing)
        success = write_musicbrainz_tag_to_mp3_file(file_path, **tag_kwargs)
        
        if success:
            return {
                'success': True,
                'message': f"Wrote {field_name} to {os.path.basename(file_path)}"
            }
        else:
            return {
                'success': False,
                'message': f"Failed to write {field_name} to MP3 file"
            }
        
    except Exception as e:
        return {
            'success': False,
            'message': f"Error writing to MP3: {e}"
        }


if __name__ == "__main__":
    import sys
    import json
    
    if len(sys.argv) < 3:
        print("Usage: python musicbrainz_import.py <artist> <album> [action]")
        print("Actions: import (default), view")
        sys.exit(1)
    
    artist = sys.argv[1]
    album = sys.argv[2]
    action = sys.argv[3] if len(sys.argv) > 3 else "import"
    
    if action == "import":
        result = import_musicbrainz_tags_for_album(artist, album)
        print(json.dumps(result, indent=2))
    elif action == "view":
        tags = get_musicbrainz_tags_for_album(artist, album)
        print(json.dumps(tags, indent=2))
    else:
        print(f"Unknown action: {action}")
