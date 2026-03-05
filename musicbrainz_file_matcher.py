#!/usr/bin/env python3
"""
MusicBrainz File Matching System

Discovers downloaded files and matches them to MusicBrainz release tracks.
Moves matched files to monitoring folders and updates database with progress.

Matching Strategy (Priority Order):
1. ISRC code match (very high confidence: 100%)
2. ID3 Tag matching - Artist + Title exact/fuzzy (high: 95-99%)
3. Filename similarity (medium: 80-90%)
4. Manual verification (if low confidence)
"""

import sqlite3
import os
import logging
import shutil
from pathlib import Path
from difflib import SequenceMatcher
from mutagen import File as MutagenFile
from datetime import datetime
from contextlib import closing
from typing import Any
from database_abstraction import DatabaseQuery

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DOWNLOADS_MUSIC_DIR = "/downloads/Music"
DB_FILE = "sptnr.db"
DB_TIMEOUT = 120.0

# Supported audio formats
AUDIO_EXTENSIONS = {'.mp3', '.flac', '.m4a', '.ogg', '.wav', '.aac', '.wma'}


class MusicBrainzFileMatcher:
    """Matches downloaded files to MusicBrainz release tracks"""

    def __init__(self):
        self.downloads_dir = Path(DOWNLOADS_MUSIC_DIR)
        self.ensure_directory()
        self.ensure_schema()

    def ensure_directory(self):
        """Ensure downloads directory exists"""
        self.downloads_dir.mkdir(parents=True, exist_ok=True)

    def get_db(self):
        """Get database connection"""
        conn = sqlite3.connect(DB_FILE, timeout=DB_TIMEOUT)
        conn.row_factory = sqlite3.Row
        return conn

    def ensure_schema(self):
        """Ensure MusicBrainz release tables exist before background matching runs."""
        try:
            from musicbrainz_release_manager import MusicBrainzReleaseManager
            MusicBrainzReleaseManager().ensure_schema()
        except Exception as e:
            logger.warning(f"[FILE_MATCHER] Could not ensure MusicBrainz schema: {e}")

    def monitor_and_match(self):
        """
        Main matching loop:
        1. Find all unmatched files in /downloads/Music
        2. Match them to active releases
        3. Move to monitoring folders
        4. Update database
        """
        try:
            logger.info("[FILE_MATCHER] Starting file discovery and matching...")
            
            unmatched_files = self.find_unmatched_files()
            if not unmatched_files:
                logger.info("[FILE_MATCHER] No unmatched files found")
                return {"matched": 0, "files_processed": 0}

            active_releases = self.get_active_releases()
            if not active_releases:
                logger.info("[FILE_MATCHER] No active releases")
                return {"matched": 0, "files_processed": len(unmatched_files)}

            matched_count = 0
            
            # Try to match each unmatched file to a release track
            for file_path in unmatched_files:
                try:
                    for release in active_releases:
                        result = self.match_file_to_release(file_path, release)
                        if result and result['confidence'] >= 0.75:
                            # Move file to monitoring folder
                            self.move_to_monitoring_folder(
                                file_path, 
                                release['release_id'],
                                result['track_number'],
                                result['confidence']
                            )
                            matched_count += 1
                            break  # File matched, move to next file
                except Exception as e:
                    logger.error(f"[FILE_MATCHER] Error matching {file_path}: {e}")
                    continue
            
            logger.info(f"[FILE_MATCHER] Matched {matched_count}/{len(unmatched_files)} files")
            return {"matched": matched_count, "files_processed": len(unmatched_files)}
            
        except Exception as e:
            logger.error(f"[FILE_MATCHER] Error in monitor_and_match: {e}")
            raise

    def find_unmatched_files(self):
        """
        Find all audio files in /downloads/Music that aren't in monitoring folders
        
        Returns:
            List of Path objects for unmatched files
        """
        unmatched_files = []
        
        try:
            # Get all active monitoring folders
            conn = self.get_db()
            db_query = DatabaseQuery(conn)
            cursor = db_query.execute("""
                SELECT monitoring_folder_path FROM musicbrainz_releases 
                WHERE status = 'active'
            """)
            monitoring_folders = {Path(row['monitoring_folder_path']) for row in cursor.fetchall()}
            conn.close()
            
            # Scan downloads directory
            for item in self.downloads_dir.rglob('*'):
                # Skip directories and monitoring folders
                if not item.is_file():
                    continue
                    
                # Skip if in monitoring folder
                if any(folder in item.parents or folder == item.parent for folder in monitoring_folders):
                    continue
                
                # Check if audio file
                if item.suffix.lower() not in AUDIO_EXTENSIONS:
                    continue
                
                # Check file size (>=2 seconds roughly = >=50KB)
                if item.stat().st_size < 50000:
                    logger.debug(f"[FILE_MATCHER] Skipping small file: {item.name}")
                    continue
                
                unmatched_files.append(item)
            
            logger.info(f"[FILE_MATCHER] Found {len(unmatched_files)} unmatched files")
            return unmatched_files
            
        except Exception as e:
            logger.error(f"[FILE_MATCHER] Error finding unmatched files: {e}")
            return []

    def get_active_releases(self):
        """Get all active MusicBrainz releases"""
        try:
            conn = self.get_db()
            db_query = DatabaseQuery(conn)
            
            cursor = db_query.execute("""
                SELECT id, release_id, release_title, artist, release_year,
                       monitoring_folder_path, total_tracks, discovered_count
                FROM musicbrainz_releases
                WHERE status = 'active'
                ORDER BY created_at DESC
            """)
            
            releases = [dict(row) for row in cursor.fetchall()]
            conn.close()
            return releases
            
        except Exception as e:
            logger.error(f"[FILE_MATCHER] Error getting active releases: {e}")
            return []

    def match_file_to_track(self, file_path, release):
        """
        Match a file to a release track using multiple strategies
        
        Returns:
            dict with track_number, confidence, strategy used, or None if no match
        """
        try:
            # Get all tracks for this release
            conn = self.get_db()
            db_query = DatabaseQuery(conn)
            
            cursor = db_query.execute("""
                SELECT id, track_number, track_title, track_artist, isrc, duration
                FROM musicbrainz_release_tracks
                WHERE release_id = ? AND status = 'queued'
                ORDER BY track_number
            """, (release['release_id'],))
            
            tracks = [dict(row) for row in cursor.fetchall()]
            conn.close()
            
            if not tracks:
                return None
            
            file_metadata = self.extract_file_metadata(file_path)
            best_match = None
            best_confidence = 0.0
            best_strategy = None
            
            # Strategy 1: ISRC matching (if available)
            if file_metadata.get('isrc'):
                for track in tracks:
                    if track['isrc'] and file_metadata['isrc'].upper() == track['isrc'].upper():
                        logger.info(f"[FILE_MATCHER] ISRC match: {file_path.name} -> Track {track['track_number']}")
                        best_match = track
                        best_confidence = 1.0
                        best_strategy = "ISRC"
                        break
            
            # Strategy 2: ID3 Tag matching (Artist + Title)
            if not best_match and file_metadata.get('artist') and file_metadata.get('title'):
                for track in tracks:
                    # Exact match
                    artist_match = self.normalize_string(file_metadata['artist']).lower()
                    title_match = self.normalize_string(file_metadata['title']).lower()
                    track_artist = self.normalize_string(track['track_artist'] or '').lower()
                    track_title = self.normalize_string(track['track_title']).lower()
                    
                    if artist_match == track_artist and title_match == track_title:
                        best_match = track
                        best_confidence = 0.99
                        best_strategy = "ID3_exact"
                        break
                    
                    # Fuzzy match
                    artist_similarity = SequenceMatcher(None, artist_match, track_artist).ratio()
                    title_similarity = SequenceMatcher(None, title_match, track_title).ratio()
                    
                    # Weighted: title is more important than artist
                    combined_score = (title_similarity * 0.7) + (artist_similarity * 0.3)
                    
                    if combined_score > best_confidence and combined_score > 0.85:
                        best_match = track
                        best_confidence = combined_score
                        best_strategy = "ID3_fuzzy"
            
            # Strategy 3: Filename similarity
            if not best_match or best_confidence < 0.85:
                filename = file_path.stem.lower()
                for track in tracks:
                    track_name = self.normalize_string(
                        f"{track['track_title']} {track['track_artist']}".lower()
                    )
                    similarity = SequenceMatcher(None, filename, track_name).ratio()
                    
                    if similarity > best_confidence and similarity > 0.80:
                        best_match = track
                        best_confidence = similarity
                        best_strategy = "filename"
            
            if best_match and best_confidence >= 0.75:
                logger.info(
                    f"[FILE_MATCHER] Matched {file_path.name} -> "
                    f"Track {best_match['track_number']} ({best_strategy}: {best_confidence:.2%})"
                )
                return {
                    'track_number': best_match['track_number'],
                    'track_id': best_match['id'],
                    'confidence': best_confidence,
                    'strategy': best_strategy
                }
            
            return None
            
        except Exception as e:
            logger.error(f"[FILE_MATCHER] Error matching file to track: {e}")
            return None

    def match_file_to_release(self, file_path, release):
        """
        Match a file to the best track in a release
        """
        return self.match_file_to_track(file_path, release)

    def extract_file_metadata(self, file_path):
        """
        Extract metadata from audio file using Mutagen
        
        Returns:
            dict with artist, title, isrc, duration
        """
        try:
            metadata = {
                'artist': None,
                'title': None,
                'isrc': None,
                'duration': None
            }
            
            # Use appropriate loader based on file type
            audio_file = MutagenFile(str(file_path))
            
            if audio_file is None:
                logger.warning(f"[FILE_MATCHER] Could not read metadata from {file_path.name}")
                return metadata
            
            # Get duration
            if hasattr(audio_file.info, 'length'):
                metadata['duration'] = int(audio_file.info.length)
            
            # Handle different metadata formats
            if hasattr(audio_file, 'tags') and audio_file.tags:
                tags = audio_file.tags
                
                # Try common tag formats
                if 'TIT2' in tags:  # ID3v2.4
                    metadata['title'] = str(tags['TIT2'].text[0]) if tags['TIT2'].text else None
                elif 'TITLE' in tags:
                    metadata['title'] = str(tags['TITLE'][0]) if tags['TITLE'] else None
                
                if 'TPE1' in tags:  # ID3v2.4 Artist
                    metadata['artist'] = str(tags['TPE1'].text[0]) if tags['TPE1'].text else None
                elif 'ARTIST' in tags:
                    metadata['artist'] = str(tags['ARTIST'][0]) if tags['ARTIST'] else None
                
                if 'TSRC' in tags:  # ID3v2.4 ISRC
                    metadata['isrc'] = str(tags['TSRC'].text[0]) if tags['TSRC'].text else None
                elif 'ISRC' in tags:
                    metadata['isrc'] = str(tags['ISRC'][0]) if tags['ISRC'] else None
            
            # Fallback: extract from filename if tags missing
            if not metadata['title']:
                # Remove extension and try to parse
                filename = file_path.stem
                if ' - ' in filename:
                    parts = filename.split(' - ', 1)
                    metadata['artist'] = metadata['artist'] or parts[0].strip()
                    metadata['title'] = parts[1].strip()
                else:
                    metadata['title'] = filename
            
            return metadata
            
        except Exception as e:
            logger.error(f"[FILE_MATCHER] Error extracting metadata from {file_path.name}: {e}")
            return {
                'artist': None,
                'title': None,
                'isrc': None,
                'duration': None
            }

    def move_to_monitoring_folder(self, file_path, release_id, track_number, confidence):
        """
        Move matched file to monitoring folder
        
        Updates database with:
        - found_filename
        - file_path
        - status = 'discovered'
        """
        try:
            conn = self.get_db()
            db_query = DatabaseQuery(conn)
            
            # Get monitoring folder path
            cursor = db_query.execute("""
                SELECT monitoring_folder_path FROM musicbrainz_releases
                WHERE release_id = ?
            """, (release_id,))
            
            result = cursor.fetchone()
            if not result:
                logger.error(f"[FILE_MATCHER] Release not found: {release_id}")
                return False
            
            monitoring_folder = Path(result['monitoring_folder_path'])
            monitoring_folder.mkdir(parents=True, exist_ok=True)
            
            # Create destination path (preserve original filename)
            destination = monitoring_folder / file_path.name
            
            # Handle file collisions
            counter = 1
            original_stem = destination.stem
            while destination.exists():
                destination = monitoring_folder / f"{original_stem}_{counter}{file_path.suffix}"
                counter += 1
            
            # Move file
            shutil.move(str(file_path), str(destination))
            logger.info(f"[FILE_MATCHER] Moved {file_path.name} -> {destination.relative_to(self.downloads_dir)}")
            
            # Update database
            db_query.execute("""
                UPDATE musicbrainz_release_tracks
                SET status = 'discovered',
                    found_filename = ?,
                    file_path = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE release_id = ? AND track_number = ?
            """, (destination.name, str(destination), release_id, track_number))
            
            # Update release discovered_count
            db_query.execute("""
                UPDATE musicbrainz_releases
                SET discovered_count = (
                    SELECT COUNT(*) FROM musicbrainz_release_tracks
                    WHERE release_id = ? AND status = 'discovered'
                ),
                updated_at = CURRENT_TIMESTAMP
                WHERE release_id = ?
            """, (release_id, release_id))
            
            conn.commit()
            conn.close()
            
            logger.info(f"[FILE_MATCHER] Updated database for track {track_number} (confidence: {confidence:.2%})")
            return True
            
        except Exception as e:
            logger.error(f"[FILE_MATCHER] Error moving file to monitoring folder: {e}")
            return False

    @staticmethod
    def normalize_string(s):
        """Normalize string for comparison (remove special chars, extra spaces)"""
        if not s:
            return ""
        
        # Remove leading/trailing spaces
        s = s.strip()
        
        # Replace multiple spaces with single
        s = ' '.join(s.split())
        
        # Remove parentheses and contents (remix info, etc)
        import re
        s = re.sub(r'\s*\(.*?\)\s*', ' ', s)
        
        return s


def get_matcher():
    """Factory function for getting matcher instance"""
    return MusicBrainzFileMatcher()


if __name__ == '__main__':
    matcher = get_matcher()
    result = matcher.monitor_and_match()
    print(f"[RESULT] {result}")
