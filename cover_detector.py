#!/usr/bin/env python3
"""
Cover song detection module for automatic identification and attribution.

Detects cover songs by analyzing songwriter/composer data from MusicBrainz,
then attributes the original artist and updates track metadata accordingly.
"""

import logging
import json
import sqlite3
from typing import Optional, Dict, List, Tuple
from pathlib import Path

from api_clients.musicbrainz import _VERSION as MUSICBRAINZ_VERSION

logger = logging.getLogger(__name__)


class CoverDetector:
    """Detect and attribute cover songs using MusicBrainz writer/composer data."""
    
    @staticmethod
    def _is_postgres(conn):
        """Detect if connection is PostgreSQL."""
        try:
            import psycopg2
            return isinstance(conn, psycopg2.extensions.connection)
        except (ImportError, AttributeError):
            return False
    
    def __init__(self, musicbrainz_client, db_connection=None):
        """
        Initialize cover detector.
        
        Args:
            musicbrainz_client: MusicBrainzClient instance for API queries
            db_connection: SQLite database connection
        """
        self.mb_client = musicbrainz_client
        self.db_conn = db_connection
        self.is_pg = self._is_postgres(db_connection) if db_connection else False
        self.placeholder = "%s" if self.is_pg else "?"
        self._band_members_cache = {}  # Cache to avoid repeated API calls

    def _configure_musicbrainzngs(self):
        """Ensure musicbrainzngs identifies itself with the app user agent."""
        try:
            import musicbrainzngs as mb
            mb.set_useragent("sptnr", MUSICBRAINZ_VERSION, "https://github.com/M0VENTURA/sptnr")
            return mb
        except Exception as e:
            logger.debug(f"Failed to configure musicbrainzngs user agent: {e}")
            return None
    
    def detect_covers_for_album(self, album: str, artist: str, tracks: List[Dict]) -> List[Dict]:
        """
        Detect cover songs in an album by analyzing writer information.
        
        Logic:
        - For each track, check if the writer/composer is different from album artist
        - If a writer appears on only ONE track in the album, it's likely a cover
        - Look up the earliest recording by that writer on MusicBrainz
        - Return cover attribution information
        
        Args:
            album: Album name
            artist: Album artist
            tracks: List of track dicts with 'id', 'title', 'mbid', etc.
            
        Returns:
            List of dicts with cover detection results:
            {
                'track_id': str,
                'title': str,
                'is_cover': bool,
                'original_artist': str,
                'original_year': int,
                'writer': str,
                'confidence': str ('high'|'medium'|'low')
            }
        """
        logger.info(f"Starting cover detection for album '{album}' by '{artist}' ({len(tracks)} tracks)")
        
        # Step 1: Collect writer information for all tracks
        track_writers = {}
        for track in tracks:
            writers = self._get_track_writers(track)
            track_title = track.get('title', 'Unknown')
            if writers:
                track_writers[track['id']] = {
                    'title': track_title,
                    'writers': writers,
                    'mbid': track.get('mbid')
                }
                logger.debug(f"  Track '{track_title}': Found writers {writers}")
            else:
                logger.debug(f"  Track '{track_title}': No writer information in database")
        
        if not track_writers:
            logger.info(f"No writer information found for any tracks in album '{album}' - cover detection skipped")
            logger.info(f"  → To enable cover detection, ensure 'writer' field is populated from metadata sources during import")
            logger.info(f"  → Writer field should contain the original songwriter/composer name")
            return []
        
        # Step 2: Identify tracks with writers different from album artist (likely covers)
        # Any track whose writer/lyricist differs from the album artist is a candidate.
        cover_results = []
        seen_track_ids = set()  # Avoid processing same track twice
        for track_id, info in track_writers.items():
            if track_id in seen_track_ids:
                continue
            for writer in info['writers']:
                # Check if writer is different from album artist
                if not self._is_writer_same_as_artist(writer, artist):
                    logger.info(f"Potential cover: '{info['title']}' - lyricist/writer '{writer}' differs from artist '{artist}'")
                    
                    # Look up original recording by this writer
                    original = self._find_original_recording(info['title'], writer)
                    
                    if original:
                        result = {
                            'track_id': track_id,
                            'title': info['title'],
                            'is_cover': True,
                            'original_artist': original['artist'],
                            'original_year': original.get('year'),
                            'writer': writer,
                            'confidence': original.get('confidence', 'medium')
                        }
                        cover_results.append(result)
                        seen_track_ids.add(track_id)
                        logger.info(f"✓ Cover confirmed: '{info['title']}' originally by '{original['artist']}' ({original.get('year', 'unknown year')})")
                        
                        # Get file path from track info if available
                        track_data = next((t for t in tracks if t.get('id') == track_id), {})
                        file_path = track_data.get('file_path')
                        
                        # Update database and file metadata
                        self.update_cover_metadata(
                            track_id=track_id,
                            title=info['title'],
                            original_artist=original['artist'],
                            file_path=file_path
                        )
                        break  # Stop after first matching writer for this track
                    else:
                        logger.debug(f"No original recording found for '{info['title']}' by writer '{writer}'")
        
        logger.info(f"Cover detection complete: found {len(cover_results)} covers in '{album}'")
        return cover_results
    
    def _get_track_writers(self, track: Dict) -> List[str]:
        """
        Extract writer/composer information from track data.
        
        Checks:
        1. Database 'writer' field (JSON array - primary source for songwriter info)
        2. MusicBrainz API (if MBID available and no local data)
        
        Args:
            track: Track dict with potential 'writer', 'mbid' fields
            
        Returns:
            List of writer names
        """
        writers = []
        
        # Check database writer field (JSON array format)
        if 'writer' in track and track['writer']:
            try:
                if isinstance(track['writer'], str):
                    writers = json.loads(track['writer'])
                elif isinstance(track['writer'], list):
                    writers = track['writer']
            except json.JSONDecodeError:
                logger.debug(f"Could not parse writer field for track {track.get('title')}")
        
        # If no writers from DB, try MusicBrainz API
        if not writers and track.get('mbid'):
            writers = self._fetch_writers_from_musicbrainz(track['mbid'])
        
        return writers
    
    def _fetch_writers_from_musicbrainz(self, mbid: str) -> List[str]:
        """
        Fetch writer/composer credits from MusicBrainz recording.
        
        Args:
            mbid: MusicBrainz Recording ID
            
        Returns:
            List of writer names
        """
        try:
            mb = self._configure_musicbrainzngs()
            if mb is None:
                return []
            
            result = mb.get_recording_by_id(
                mbid,
                includes=['artist-rels', 'work-rels']
            )
            
            writers = []
            recording = result.get('recording', {})
            
            # Check work relationships for composer/lyricist
            work_rels = recording.get('work-relation-list', [])
            for rel in work_rels:
                work = rel.get('work', {})
                # Get artist relationships from the work
                artist_rels = work.get('artist-relation-list', [])
                for artist_rel in artist_rels:
                    rel_type = artist_rel.get('type', '')
                    if rel_type in ['composer', 'lyricist', 'writer']:
                        artist_name = artist_rel.get('artist', {}).get('name')
                        if artist_name and artist_name not in writers:
                            writers.append(artist_name)
            
            return writers
            
        except Exception as e:
            logger.debug(f"Failed to fetch writers from MusicBrainz for {mbid}: {e}")
            return []
    
    def _get_band_members(self, artist: str) -> List[str]:
        """
        Fetch band members for an artist from MusicBrainz.
        
        Caches results to avoid repeated API calls.
        
        Args:
            artist: Artist/band name
            
        Returns:
            List of band member names
        """
        # Check cache first
        if artist in self._band_members_cache:
            return self._band_members_cache[artist]

        try:
            members = []
            if self.mb_client and hasattr(self.mb_client, 'get_artist_member_names'):
                members = self.mb_client.get_artist_member_names(artist=artist)

            self._band_members_cache[artist] = members or []

            if members:
                logger.info(f"MusicBrainz found {len(members)} members for '{artist}': {', '.join(members)}")
            else:
                logger.debug(f"No band members found for '{artist}' in MusicBrainz")

            return self._band_members_cache[artist]
        except Exception as e:
            logger.debug(f"Failed to fetch band members for '{artist}' from MusicBrainz: {e}")
            self._band_members_cache[artist] = []
            return []
    
    def _is_writer_same_as_artist(self, writer: str, artist: str) -> bool:
        """
        Check if writer name matches the album artist (fuzzy matching).
        
        Also checks if the writer is a band member of the artist group.
        
        Args:
            writer: Writer/composer name
            artist: Album artist name
            
        Returns:
            True if they appear to be the same person/group or if writer is a band member
        """
        # Normalize both names
        writer_norm = writer.lower().strip()
        artist_norm = artist.lower().strip()
        
        # Exact match
        if writer_norm == artist_norm:
            return True
        
        # Check if one contains the other (handles "The Beatles" vs "Beatles")
        if writer_norm in artist_norm or artist_norm in writer_norm:
            return True
        
        # Check if writer is a band member
        band_members = self._get_band_members(artist)
        if band_members:
            for member in band_members:
                member_norm = member.lower().strip()
                if member_norm == writer_norm:
                    logger.debug(f"Writer '{writer}' identified as band member of '{artist}'")
                    return True
                # Also check partial matches (e.g., "Maynard Keenan" vs "Maynard James Keenan")
                if (member_norm in writer_norm or writer_norm in member_norm) and len(writer_norm) > 5:
                    logger.debug(f"Writer '{writer}' fuzzy-matched as band member '{member}' of '{artist}'")
                    return True
        
        return False
    
    def _find_original_recording(self, title: str, writer: str) -> Optional[Dict]:
        """
        Find the earliest/original recording of a song by the writer.
        
        Queries MusicBrainz for recordings with:
        - Matching title
        - Writer as the performing artist
        - Earliest release date
        
        Args:
            title: Track title to search for
            writer: Writer/composer name (likely original artist)
            
        Returns:
            Dict with {'artist': str, 'year': int, 'confidence': str} or None
        """
        try:
            mb = self._configure_musicbrainzngs()
            if mb is None:
                return None
            
            # Search for recordings by this artist with this title
            result = mb.search_recordings(
                recording=title,
                artist=writer,
                limit=20
            )
            
            recordings = result.get('recording-list', [])
            if not recordings:
                return None
            
            # Find earliest release
            earliest = None
            earliest_year = 9999
            
            for recording in recordings:
                # Check if artist name matches writer (case-insensitive)
                artist_credit = recording.get('artist-credit', [])
                if not artist_credit:
                    continue
                
                recording_artist = artist_credit[0].get('artist', {}).get('name', '')
                if writer.lower() not in recording_artist.lower():
                    continue
                
                # Get earliest release year
                releases = recording.get('release-list', [])
                for release in releases:
                    date = release.get('date', '')
                    if date:
                        try:
                            year = int(date[:4])
                            if year < earliest_year:
                                earliest_year = year
                                earliest = {
                                    'artist': recording_artist,
                                    'year': year,
                                    'confidence': 'high' if len(recordings) == 1 else 'medium'
                                }
                        except (ValueError, IndexError):
                            continue
            
            return earliest
            
        except Exception as e:
            logger.debug(f"Failed to find original recording for '{title}' by '{writer}': {e}")
            return None
    
    def update_cover_metadata(self, track_id: str, title: str, original_artist: str,
                            file_path: Optional[str] = None) -> bool:
        """
        Update track metadata to reflect cover attribution.
        
        Updates:
        1. Database: title → "Title (Original Artist Cover)" (only if not already present)
        2. Database: Add "Cover" to genres
        3. Database: Set is_cover, is_cover_reason, original_cover_artist
        4. File tags: Same updates to MP3/FLAC file
        
        Args:
            track_id: Track ID in database
            title: Current track title
            original_artist: Original artist name for attribution
            file_path: Optional path to audio file for tag updates
            
        Returns:
            True if successful
        """
        import re
        try:
            # Check if title already has a "(... Cover)" suffix to avoid duplication
            cover_suffix_pattern = re.compile(r'\s*\([^)]+\s+Cover\)\s*$', re.IGNORECASE)
            if cover_suffix_pattern.search(title):
                new_title = title  # Already has cover attribution
                logger.debug(f"Title '{title}' already has cover suffix, skipping title update")
            else:
                new_title = f"{title} ({original_artist} Cover)"
            
            # Update database
            if self.db_conn:
                cursor = self.db_conn.cursor()
                
                # Update title (only if changed)
                if new_title != title:
                    cursor.execute(
                        f"UPDATE tracks SET title = {self.placeholder} WHERE id = {self.placeholder}",
                        (new_title, track_id)
                    )
                
                # Add "Cover" to genres
                cursor.execute(
                    f"SELECT genres FROM tracks WHERE id = {self.placeholder}",
                    (track_id,)
                )
                result = cursor.fetchone()
                if result:
                    current_genres = (result['genres'] if self.is_pg else result[0]) or ""
                    genres_list = [g.strip() for g in current_genres.split(",")] if current_genres else []
                    if "Cover" not in genres_list:
                        genres_list.append("Cover")
                    new_genres = ", ".join(genres_list)
                    
                    cursor.execute(
                        f"UPDATE tracks SET genres = {self.placeholder} WHERE id = {self.placeholder}",
                        (new_genres, track_id)
                    )
                
                # Mark as cover and store original artist cleanly
                cursor.execute(
                    f"UPDATE tracks SET is_cover = 1, is_cover_reason = {self.placeholder}, original_cover_artist = {self.placeholder} WHERE id = {self.placeholder}",
                    (f"Writer-based detection: original by {original_artist}", original_artist, track_id)
                )
                
                self.db_conn.commit()
                logger.info(f"✓ Database updated: '{title}' → '{new_title}' (original: {original_artist})")
            
            # Update file metadata if path provided
            if file_path and Path(file_path).exists():
                self._update_file_metadata(file_path, new_title, ["Cover"])
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to update cover metadata for track {track_id}: {e}")
            return False
    
    def _update_file_metadata(self, file_path: str, title: str, additional_genres: List[str]) -> bool:
        """
        Update audio file tags with cover attribution.
        
        Args:
            file_path: Path to MP3/FLAC file
            title: New title with cover attribution
            additional_genres: Genres to add (e.g., ["Cover"])
            
        Returns:
            True if successful
        """
        try:
            from mutagen.mp3 import MP3
            from mutagen.flac import FLAC
            from mutagen.id3 import ID3, TIT2, TCON
            
            path = Path(file_path)
            
            if path.suffix.lower() == '.mp3':
                audio = MP3(file_path, ID3=ID3)
                
                # Update title
                audio.tags['TIT2'] = TIT2(encoding=3, text=title)
                
                # Update genres
                current_genres = []
                if 'TCON' in audio.tags:
                    current_genres = list(audio.tags['TCON'].text)
                
                for genre in additional_genres:
                    if genre not in current_genres:
                        current_genres.append(genre)
                
                audio.tags['TCON'] = TCON(encoding=3, text=current_genres)
                
                audio.save()
                logger.info(f"✓ MP3 file updated: {path.name}")
                
            elif path.suffix.lower() == '.flac':
                audio = FLAC(file_path)
                
                # Update title
                audio['title'] = title
                
                # Update genres
                current_genres = audio.get('genre', [])
                if isinstance(current_genres, str):
                    current_genres = [current_genres]
                
                for genre in additional_genres:
                    if genre not in current_genres:
                        current_genres.append(genre)
                
                audio['genre'] = current_genres
                
                audio.save()
                logger.info(f"✓ FLAC file updated: {path.name}")
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to update file metadata for {file_path}: {e}")
            return False
