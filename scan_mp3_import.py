#!/usr/bin/env python3
"""
MP3 Metadata Import Scan
Scans local MP3/FLAC files and imports metadata into database.

This script:
1. Recursively scans configured MP3 directory
2. Extracts metadata from audio files using mutagen
3. Matches files to existing library tracks or creates new entries
4. Saves progress and logs to JSON for UI display
5. Handles both MP3 (ID3v2) and FLAC (Vorbis comments) formats

Usage:
    python scan_mp3_import.py [--directory /path/to/music] [--dry-run] [--verbose]
"""

import os
import sys
import json
import argparse
import logging
import psycopg2
import psycopg2.extras
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(__file__))

from helpers.metadata_reader import read_mp3_metadata, read_genres_from_mp3
from helpers.config_loader import load_config

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def get_db():
    """Create a PostgreSQL connection for MP3 import tasks."""
    return psycopg2.connect(
        host=os.environ.get("PG_HOST", "sptnr-postgres"),
        user=os.environ.get("PG_USER", "sptnr"),
        password=os.environ.get("PG_PASSWORD", ""),
        dbname=os.environ.get("PG_DATABASE", "sptnr"),
        port=int(os.environ.get("PG_PORT", "5432")),
        connect_timeout=10,
    )

# Constants
SUPPORTED_FORMATS = {'.mp3', '.flac', '.m4a', '.wav', '.ogg'}
PROGRESS_FILE = os.path.join(os.path.dirname(__file__), 'mp3_import_progress.json')

class MP3ImportScanner:
    """Scans and imports MP3 metadata into database."""
    
    def __init__(self, directory: str = None, dry_run: bool = False, verbose: bool = False, mode: str = "database"):
        """Initialize scanner.
        
        Args:
            directory: Directory to scan (for directory mode only)
            dry_run: If True, don't modify database
            verbose: If True, log detailed information
            mode: "database" (read from DB tracks) or "directory" (scan directory)
        """
        self.config = load_config()
        self.directory = directory or self.config.get('music_library_path', '/Music')
        self.dry_run = dry_run
        self.verbose = verbose
        self.mode = mode
        self.db_conn = None
        
        # Scan statistics
        self.total_files = 0
        self.processed = 0
        self.imported = 0
        self.matched = 0
        self.skipped = 0
        self.errors = 0
        self.start_time = None
        self.current_file = ""
        
    def _write_progress(self):
        """Write progress to JSON file."""
        progress = {
            "is_running": True,
            "scan_type": "mp3_import",
            "status": "scanning",
            "current_file": self.current_file,
            "processed": self.processed,
            "total": self.total_files,
            "imported": self.imported,
            "matched": self.matched,
            "skipped": self.skipped,
            "errors": self.errors,
            "percent": int((self.processed / self.total_files * 100) if self.total_files > 0 else 0),
            "timestamp": datetime.now().isoformat()
        }
        
        try:
            os.makedirs(os.path.dirname(PROGRESS_FILE), exist_ok=True)
            with open(PROGRESS_FILE, 'w', encoding='utf-8') as f:
                json.dump(progress, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to write progress: {e}")
    
    def _normalize_string(self, s: str) -> str:
        """Normalize string for comparison."""
        if not s:
            return ""
        return s.lower().strip()
    
    def _match_track_in_db(self, title: str, artist: str, album: str) -> Optional[str]:
        """
        Find matching track in database.
        
        Returns:
            Track ID if found, None otherwise
        """
        try:
            cursor = self.db_conn.cursor()
            
            # Try exact match first
            query = """
                SELECT track_id FROM tracks 
                WHERE LOWER(title) = LOWER(%s) AND LOWER(artist) = LOWER(%s) AND LOWER(album) = LOWER(%s)
                LIMIT 1
            """
            cursor.execute(query, (title, artist, album))
            result = cursor.fetchone()
            
            if result:
                return result[0]
            
            # Try fuzzy match (title + artist)
            query = """
                SELECT track_id FROM tracks 
                WHERE LOWER(title) = LOWER(%s) AND LOWER(artist) = LOWER(%s)
                LIMIT 1
            """
            cursor.execute(query, (title, artist))
            result = cursor.fetchone()
            
            return result[0] if result else None
        except Exception as e:
            logger.debug(f"Error matching track: {e}")
            try:
                self.db_conn.rollback()
            except:
                pass
            return None
    
    def _import_track(self, file_path: str, metadata: Dict) -> Tuple[bool, str]:
        """
        Import or update track in database.
        
        Returns:
            Tuple of (success, message)
        """
        title = metadata.get('title', 'Unknown Title')
        artist = metadata.get('artist', 'Unknown Artist')
        album = metadata.get('album', 'Unknown Album')
        
        try:
            cursor = self.db_conn.cursor()
            existing_id = self._match_track_in_db(title, artist, album)
            
            if existing_id:
                # Update existing track
                if not self.dry_run:
                    update_query = """
                        UPDATE tracks 
                        SET 
                            path = %s,
                            album_artist = %s,
                            track_number = %s,
                            disc_number = %s,
                            year = %s,
                            genres = %s,
                            comment = %s,
                            mood = %s,
                            titlesort = %s,
                            albumsort = %s,
                            artistsort = %s,
                            albumartistsort = %s,
                            composersort = %s,
                            conductor = %s,
                            remixer = %s,
                            grouping = %s,
                            subtitle = %s,
                            key = %s,
                            encodedby = %s,
                            encodersettings = %s,
                            releasedate = %s,
                            originaldate = %s,
                            discsubtitle = %s,
                            lyricist = %s,
                            compilation = %s,
                            tracktotal = %s,
                            disctotal = %s,
                            website = %s,
                            catalognumber = %s,
                            barcode = %s,
                            asin = %s,
                            script = %s,
                            license = %s,
                            director = %s,
                            djmixer = %s,
                            engineer = %s,
                            albumversion = %s,
                            movementname = %s,
                            movementtotal = %s,
                            movement = %s,
                            albumartists = %s,
                            artistssort = %s,
                            albumartistssort = %s,
                            lyricistsort = %s,
                            replaygain_track_gain = %s,
                            replaygain_track_peak = %s,
                            replaygain_album_gain = %s,
                            replaygain_album_peak = %s,
                            r128_track_gain = %s,
                            r128_album_gain = %s,
                            explicitstatus = %s,
                            lyrics = %s,
                            bpm = %s,
                            composer = %s,
                            writer = %s,
                            arranger = %s,
                            mixer = %s,
                            producer = %s,
                            performer = %s,
                            language = %s,
                            copyright = %s,
                            recordlabel = %s,
                            label = %s,
                            releasetype = %s,
                            releasestatus = %s,
                            releasecountry = %s,
                            media = %s,
                            isrc = %s,
                            work = %s,
                            musicbrainz_trackid = %s,
                            musicbrainz_albumid = %s,
                            musicbrainz_album_mbid = %s,
                            musicbrainz_artistid = %s,
                            musicbrainz_albumartistid = %s,
                            musicbrainz_releasegroupid = %s,
                            musicbrainz_releasetrackid = %s,
                            musicbrainz_workid = %s,
                            last_metadata_update = now()
                        WHERE track_id = %s
                    """
                    cursor.execute(update_query, (
                        file_path,
                        metadata.get('album_artist'),
                        metadata.get('track_number'),
                        metadata.get('disc_number'),
                        metadata.get('date'),
                        metadata.get('genre', ''),
                        metadata.get('comment', ''),
                        metadata.get('mood'),
                        metadata.get('titlesort'),
                        metadata.get('albumsort'),
                        metadata.get('artistsort'),
                        metadata.get('albumartistsort'),
                        metadata.get('composersort'),
                        metadata.get('conductor'),
                        metadata.get('remixer'),
                        metadata.get('grouping'),
                        metadata.get('subtitle'),
                        metadata.get('key'),
                        metadata.get('encodedby'),
                        metadata.get('encodersettings'),
                        metadata.get('releasedate'),
                        metadata.get('originaldate'),
                        metadata.get('discsubtitle'),
                        metadata.get('lyricist'),
                        metadata.get('compilation'),
                        metadata.get('tracktotal'),
                        metadata.get('disctotal'),
                        metadata.get('website'),
                        metadata.get('catalognumber'),
                        metadata.get('barcode'),
                        metadata.get('asin'),
                        metadata.get('script'),
                        metadata.get('license'),
                        metadata.get('director'),
                        metadata.get('djmixer'),
                        metadata.get('engineer'),
                        metadata.get('albumversion'),
                        metadata.get('movementname'),
                        metadata.get('movementtotal'),
                        metadata.get('movement'),
                        metadata.get('albumartists'),
                        metadata.get('artistssort'),
                        metadata.get('albumartistssort'),
                        metadata.get('lyricistsort'),
                        metadata.get('replaygain_track_gain'),
                        metadata.get('replaygain_track_peak'),
                        metadata.get('replaygain_album_gain'),
                        metadata.get('replaygain_album_peak'),
                        metadata.get('r128_track_gain'),
                        metadata.get('r128_album_gain'),
                        metadata.get('explicitstatus'),
                        metadata.get('lyrics'),
                        metadata.get('bpm'),
                        metadata.get('composer'),
                        metadata.get('writer'),
                        metadata.get('arranger'),
                        metadata.get('mixer'),
                        metadata.get('producer'),
                        metadata.get('performer') or metadata.get('performer_raw'),
                        metadata.get('language'),
                        metadata.get('copyright'),
                        metadata.get('recordlabel') or metadata.get('publisher'),
                        metadata.get('recordlabel') or metadata.get('publisher'),
                        metadata.get('releasetype') or metadata.get('musicbrainz_releasetype'),
                        metadata.get('releasestatus') or metadata.get('musicbrainz_releasestatus'),
                        metadata.get('releasecountry') or metadata.get('musicbrainz_releasecountry'),
                        metadata.get('media'),
                        metadata.get('isrc'),
                        metadata.get('work'),
                        metadata.get('musicbrainz_trackid'),
                        metadata.get('musicbrainz_albumid'),
                        metadata.get('musicbrainz_albumid'),  # musicbrainz_album_mbid mirrors musicbrainz_albumid
                        metadata.get('musicbrainz_artistid'),
                        metadata.get('musicbrainz_albumartistid'),
                        metadata.get('musicbrainz_releasegroupid'),
                        metadata.get('musicbrainz_releasetrackid'),
                        metadata.get('musicbrainz_workid'),
                        existing_id
                    ))
                    self.db_conn.commit()
                
                self.matched += 1
                return True, f"Matched existing: {artist} - {title}"
            else:
                # Create new track
                if not self.dry_run:
                    insert_query = """
                        INSERT INTO tracks 
                        (artist, title, album, album_artist, track_number, disc_number,
                         path, year, genres, comment,
                         mood, titlesort, albumsort, artistsort, albumartistsort,
                         composersort, conductor, remixer, grouping, subtitle, key,
                         encodedby, encodersettings, releasedate, originaldate,
                         discsubtitle, lyricist, compilation, tracktotal, disctotal,
                         website, catalognumber, barcode, asin, script, license,
                         director, djmixer, engineer, albumversion, movementname,
                         movementtotal, movement, albumartists, artistssort,
                         albumartistssort, lyricistsort, replaygain_track_gain,
                         replaygain_track_peak, replaygain_album_gain,
                         replaygain_album_peak, r128_track_gain, r128_album_gain,
                         explicitstatus, lyrics, bpm, composer, writer, arranger,
                         mixer, producer, performer, language, copyright,
                         recordlabel, label, releasetype, releasestatus,
                         releasecountry, media, isrc, work,
                         musicbrainz_trackid, musicbrainz_albumid, musicbrainz_album_mbid,
                         musicbrainz_artistid, musicbrainz_albumartistid,
                         musicbrainz_releasegroupid, musicbrainz_releasetrackid,
                         musicbrainz_workid)
                        VALUES (%s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s,
                                %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s,
                                %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s,
                                %s, %s, %s, %s,
                                %s, %s, %s,
                                %s, %s,
                                %s, %s, %s,
                                %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s,
                                %s, %s, %s, %s,
                                %s, %s, %s, %s,
                                %s, %s, %s,
                                %s, %s, %s, %s,
                                %s)
                    """
                    cursor.execute(insert_query, (
                        artist,
                        title,
                        album,
                        metadata.get('album_artist'),
                        metadata.get('track_number'),
                        metadata.get('disc_number'),
                        file_path,
                        metadata.get('date'),
                        metadata.get('genre', ''),
                        metadata.get('comment', ''),
                        metadata.get('mood'),
                        metadata.get('titlesort'),
                        metadata.get('albumsort'),
                        metadata.get('artistsort'),
                        metadata.get('albumartistsort'),
                        metadata.get('composersort'),
                        metadata.get('conductor'),
                        metadata.get('remixer'),
                        metadata.get('grouping'),
                        metadata.get('subtitle'),
                        metadata.get('key'),
                        metadata.get('encodedby'),
                        metadata.get('encodersettings'),
                        metadata.get('releasedate'),
                        metadata.get('originaldate'),
                        metadata.get('discsubtitle'),
                        metadata.get('lyricist'),
                        metadata.get('compilation'),
                        metadata.get('tracktotal'),
                        metadata.get('disctotal'),
                        metadata.get('website'),
                        metadata.get('catalognumber'),
                        metadata.get('barcode'),
                        metadata.get('asin'),
                        metadata.get('script'),
                        metadata.get('license'),
                        metadata.get('director'),
                        metadata.get('djmixer'),
                        metadata.get('engineer'),
                        metadata.get('albumversion'),
                        metadata.get('movementname'),
                        metadata.get('movementtotal'),
                        metadata.get('movement'),
                        metadata.get('albumartists'),
                        metadata.get('artistssort'),
                        metadata.get('albumartistssort'),
                        metadata.get('lyricistsort'),
                        metadata.get('replaygain_track_gain'),
                        metadata.get('replaygain_track_peak'),
                        metadata.get('replaygain_album_gain'),
                        metadata.get('replaygain_album_peak'),
                        metadata.get('r128_track_gain'),
                        metadata.get('r128_album_gain'),
                        metadata.get('explicitstatus'),
                        metadata.get('lyrics'),
                        metadata.get('bpm'),
                        metadata.get('composer'),
                        metadata.get('writer'),
                        metadata.get('arranger'),
                        metadata.get('mixer'),
                        metadata.get('producer'),
                        metadata.get('performer') or metadata.get('performer_raw'),
                        metadata.get('language'),
                        metadata.get('copyright'),
                        metadata.get('recordlabel') or metadata.get('publisher'),
                        metadata.get('recordlabel') or metadata.get('publisher'),
                        metadata.get('releasetype') or metadata.get('musicbrainz_releasetype'),
                        metadata.get('releasestatus') or metadata.get('musicbrainz_releasestatus'),
                        metadata.get('releasecountry') or metadata.get('musicbrainz_releasecountry'),
                        metadata.get('media'),
                        metadata.get('isrc'),
                        metadata.get('work'),
                        metadata.get('musicbrainz_trackid'),
                        metadata.get('musicbrainz_albumid'),
                        metadata.get('musicbrainz_albumid'),  # musicbrainz_album_mbid mirrors musicbrainz_albumid
                        metadata.get('musicbrainz_artistid'),
                        metadata.get('musicbrainz_albumartistid'),
                        metadata.get('musicbrainz_releasegroupid'),
                        metadata.get('musicbrainz_releasetrackid'),
                        metadata.get('musicbrainz_workid'),
                    ))
                    self.db_conn.commit()
                
                self.imported += 1
                return True, f"Imported: {artist} - {title}"
                
        except Exception as e:
            self.errors += 1
            if not self.dry_run:
                try:
                    self.db_conn.rollback()
                except:
                    pass
            return False, f"Error: {str(e)}"
    
    def _scan_file(self, file_path: str) -> bool:
        """
        Scan and process a single audio file.
        
        Returns:
            True if successful, False otherwise
        """
        try:
            self.current_file = os.path.basename(file_path)
            
            if self.verbose:
                logger.info(f"Processing: {file_path}")
            
            # Extract metadata based on file type
            file_ext = os.path.splitext(file_path)[1].lower()
            metadata = {}
            
            if file_ext in ('.mp3', '.flac', '.m4a', '.ogg', '.opus'):
                metadata = read_mp3_metadata(file_path)
            else:
                self.skipped += 1
                return True
            
            if not metadata or not metadata.get('title'):
                self.skipped += 1
                if self.verbose:
                    logger.warning(f"Skipped {file_path}: no title found")
                return True
            
            # Try to import/match track
            success, message = self._import_track(file_path, metadata)
            
            if self.verbose:
                level = logging.INFO if success else logging.WARNING
                logger.log(level, message)
            
            self.processed += 1
            self._write_progress()
            
            return success
            
        except Exception as e:
            self.errors += 1
            if self.verbose:
                logger.error(f"Error processing {file_path}: {e}")
            return False
    
    def scan(self) -> Dict:
        """
        Scan and import metadata.
        
        Mode can be:
        - "database": Read file paths from tracks table and update metadata from files
        - "directory": Scan directory for audio files and import
        
        Returns:
            Dictionary with scan results
        """
        self.start_time = datetime.now()
        try:
            self.db_conn = get_db()
        except Exception as e:
            logger.error(f"Failed to connect to database: {e}")
            return self._get_results(f"Database connection failed: {str(e)}", error=True)
        
        if self.mode == "database":
            return self._scan_from_database()
        else:
            return self._scan_directory()
    
    def _scan_from_database(self) -> Dict:
        """Scan by reading file paths from database tracks table."""
        logger.info("Starting MP3 metadata import from database tracks")
        
        try:
            cursor = self.db_conn.cursor()
            
            # Get all tracks with file paths from database
            cursor.execute("""
                SELECT id, file_path, artist, title, album
                FROM tracks
                WHERE file_path IS NOT NULL
                  AND file_path != ''
                  AND (file_path LIKE '%.mp3' OR file_path LIKE '%.flac' OR file_path LIKE '%.m4a' OR file_path LIKE '%.wav' OR file_path LIKE '%.ogg')
                ORDER BY artist, album
            """)
            
            tracks = cursor.fetchall()
            self.total_files = len(tracks)
            
            logger.info(f"Found {self.total_files} tracks in database with file paths")
            
            if self.total_files == 0:
                return self._get_results("No tracks with file paths found in database")
            
            # Process each track
            for i, (track_id, file_path, db_artist, db_title, db_album) in enumerate(tracks, 1):
                if self._update_track_from_file(track_id, file_path, db_artist, db_title, db_album):
                    pass
                
                if i % 50 == 0:
                    logger.info(f"Progress: {i}/{self.total_files}")
            
            elapsed = (datetime.now() - self.start_time).total_seconds() if self.start_time else 0
            summary = f"Scan completed: {self.matched} matched, {self.imported} updated, {self.errors} errors"
            return self._get_results(summary, elapsed)
            
        except Exception as e:
            logger.error(f"Database scan failed: {e}", exc_info=True)
            elapsed = (datetime.now() - self.start_time).total_seconds() if self.start_time else 0
            return self._get_results(f"Scan failed: {str(e)}", elapsed, error=True)
    
    def _scan_directory(self) -> Dict:
        """Scan directory for audio files."""
        logger.info(f"Starting MP3 import scan: {self.directory}")
        
        try:
            # Count total files
            if not os.path.exists(self.directory):
                return self._get_results(f"Directory not found: {self.directory}", error=True)
                
            all_files = list(Path(self.directory).rglob('*'))
            audio_files = [f for f in all_files if f.suffix.lower() in SUPPORTED_FORMATS]
            self.total_files = len(audio_files)
            
            logger.info(f"Found {self.total_files} audio files to scan")
            
            if self.total_files == 0:
                return self._get_results("No audio files found")
            
            # Process each file
            for i, file_path in enumerate(audio_files, 1):
                if self._scan_file(str(file_path)):
                    pass
                
                if i % 10 == 0:
                    logger.info(f"Progress: {i}/{self.total_files}")
            
            elapsed = (datetime.now() - self.start_time).total_seconds() if self.start_time else 0
            return self._get_results(f"Scan completed in {elapsed:.1f}s", elapsed)
            
        except Exception as e:
            logger.error(f"Scan failed: {e}", exc_info=True)
            elapsed = (datetime.now() - self.start_time).total_seconds() if self.start_time else 0
            return self._get_results(f"Scan failed: {str(e)}", elapsed, error=True)
        finally:
            if self.db_conn:
                try:
                    self.db_conn.close()
                except:
                    pass
    
    def _update_track_from_file(self, track_id: int, file_path: str, db_artist: str, db_title: str, db_album: str) -> bool:
        """Read metadata from file and update database track."""
        try:
            self.current_file = os.path.basename(file_path)
            
            if not os.path.exists(file_path):
                if self.verbose:
                    logger.warning(f"File not found: {file_path}")
                self.skipped += 1
                self.processed += 1
                self._write_progress()
                return True
            
            # Extract metadata from file
            file_ext = os.path.splitext(file_path)[1].lower()
            metadata = {}
            
            if file_ext in ('.mp3', '.flac', '.m4a', '.ogg', '.opus'):
                metadata = read_mp3_metadata(file_path)
            else:
                self.skipped += 1
                self.processed += 1
                self._write_progress()
                return True
            
            if not metadata:
                metadata = {}
            
            # Update database with metadata from file
            if not self.dry_run:
                cursor = self.db_conn.cursor()
                
                update_query = """
                    UPDATE tracks 
                    SET 
                        artist = COALESCE(%s, artist),
                        album_artist = COALESCE(%s, album_artist),
                        title = COALESCE(%s, title),
                        album = COALESCE(%s, album),
                        year = COALESCE(%s, year),
                        track_number = COALESCE(%s, track_number),
                        disc_number = COALESCE(%s, disc_number),
                        mood = %s,
                        titlesort = %s,
                        albumsort = %s,
                        artistsort = %s,
                        albumartistsort = %s,
                        composersort = %s,
                        conductor = %s,
                        remixer = %s,
                        grouping = %s,
                        subtitle = %s,
                        key = %s,
                        encodedby = %s,
                        encodersettings = %s,
                        releasedate = %s,
                        originaldate = %s,
                        discsubtitle = %s,
                        lyricist = %s,
                        compilation = %s,
                        tracktotal = %s,
                        disctotal = %s,
                        website = %s,
                        catalognumber = %s,
                        barcode = %s,
                        asin = %s,
                        script = %s,
                        license = %s,
                        director = %s,
                        djmixer = %s,
                        engineer = %s,
                        albumversion = %s,
                        movementname = %s,
                        movementtotal = %s,
                        movement = %s,
                        albumartists = %s,
                        artistssort = %s,
                        albumartistssort = %s,
                        lyricistsort = %s,
                        replaygain_track_gain = %s,
                        replaygain_track_peak = %s,
                        replaygain_album_gain = %s,
                        replaygain_album_peak = %s,
                        r128_track_gain = %s,
                        r128_album_gain = %s,
                        explicitstatus = %s,
                        lyrics = %s,
                        bpm = %s,
                        genres = %s,
                        composer = %s,
                        comment = %s,
                        language = %s,
                        copyright = %s,
                        recordlabel = %s
                    WHERE id = %s
                """
                
                cursor.execute(update_query, (
                    metadata.get('artist') or db_artist,
                    metadata.get('album_artist'),
                    metadata.get('title') or db_title,
                    metadata.get('album') or db_album,
                    metadata.get('date'),
                    metadata.get('track_number'),
                    metadata.get('disc_number'),
                    metadata.get('mood'),
                    metadata.get('titlesort'),
                    metadata.get('albumsort'),
                    metadata.get('artistsort'),
                    metadata.get('albumartistsort'),
                    metadata.get('composersort'),
                    metadata.get('conductor'),
                    metadata.get('remixer'),
                    metadata.get('grouping'),
                    metadata.get('subtitle'),
                    metadata.get('key'),
                    metadata.get('encodedby'),
                    metadata.get('encodersettings'),
                    metadata.get('releasedate'),
                    metadata.get('originaldate'),
                    metadata.get('discsubtitle'),
                    metadata.get('lyricist'),
                    metadata.get('compilation'),
                    metadata.get('tracktotal'),
                    metadata.get('disctotal'),
                    metadata.get('website'),
                    metadata.get('catalognumber'),
                    metadata.get('barcode'),
                    metadata.get('asin'),
                    metadata.get('script'),
                    metadata.get('license'),
                    metadata.get('director'),
                    metadata.get('djmixer'),
                    metadata.get('engineer'),
                    metadata.get('albumversion'),
                    metadata.get('movementname'),
                    metadata.get('movementtotal'),
                    metadata.get('movement'),
                    metadata.get('albumartists'),
                    metadata.get('artistssort'),
                    metadata.get('albumartistssort'),
                    metadata.get('lyricistsort'),
                    metadata.get('replaygain_track_gain'),
                    metadata.get('replaygain_track_peak'),
                    metadata.get('replaygain_album_gain'),
                    metadata.get('replaygain_album_peak'),
                    metadata.get('r128_track_gain'),
                    metadata.get('r128_album_gain'),
                    metadata.get('explicitstatus'),
                    metadata.get('lyrics'),
                    metadata.get('bpm'),
                    metadata.get('genre') or metadata.get('genres'),
                    metadata.get('composer'),
                    metadata.get('comment'),
                    metadata.get('language'),
                    metadata.get('copyright'),
                    metadata.get('recordlabel') or metadata.get('publisher'),
                    track_id
                ))
                self.db_conn.commit()
            
            self.matched += 1
            if self.verbose:
                logger.info(f"Updated: {db_artist} - {db_title}")
            
            self.processed += 1
            self._write_progress()
            return True
            
        except Exception as e:
            self.errors += 1
            if self.verbose:
                logger.error(f"Error processing {file_path}: {e}")
            self.processed += 1
            try:
                self.db_conn.rollback()
            except:
                pass
            return False
    
    def _get_results(self, message: str = "", elapsed: float = 0, error: bool = False) -> Dict:
        """Format scan results."""
        return {
            "success": not error,
            "message": message,
            "scan_type": "mp3_import",
            "total_files": self.total_files,
            "processed": self.processed,
            "imported": self.imported,
            "matched": self.matched,
            "skipped": self.skipped,
            "errors": self.errors,
            "elapsed_seconds": elapsed,
            "dry_run": self.dry_run,
            "timestamp": datetime.now().isoformat()
        }


def main():
    """Entry point for CLI."""
    parser = argparse.ArgumentParser(description='MP3 Metadata Import Scan')
    parser.add_argument('--directory', help='Directory to scan (default: music library path)')
    parser.add_argument('--dry-run', action='store_true', help='Preview only, don\'t modify database')
    parser.add_argument('--verbose', action='store_true', help='Verbose output')
    
    args = parser.parse_args()
    
    scanner = MP3ImportScanner(
        directory=args.directory,
        dry_run=args.dry_run,
        verbose=args.verbose
    )
    
    results = scanner.scan()
    
    # Write final results
    print(json.dumps(results, indent=2))
    
    return 0 if results['success'] else 1


if __name__ == '__main__':
    sys.exit(main())
