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
import sqlite3
import argparse
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(__file__))

from helpers.metadata_reader import extract_metadata_from_mp3, extract_metadata_from_flac
from database_abstraction import get_db, close_db
from helpers.config_loader import load_config

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Constants
SUPPORTED_FORMATS = {'.mp3', '.flac', '.m4a', '.wav', '.ogg'}
PROGRESS_FILE = os.path.join(os.path.dirname(__file__), '..' if os.name == 'nt' else '.', 'mp3_import_progress.json')

class MP3ImportScanner:
    """Scans and imports MP3 metadata into database."""
    
    def __init__(self, directory: str = None, dry_run: bool = False, verbose: bool = False):
        """Initialize scanner."""
        self.config = load_config()
        self.directory = directory or self.config.get('music_library_path', os.path.expanduser('~/Music'))
        self.dry_run = dry_run
        self.verbose = verbose
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
        cursor = self.db_conn.cursor()
        
        # Try exact match first
        query = """
            SELECT track_id, artist, title, album 
            FROM tracks 
            WHERE LOWER(title) = ? AND LOWER(artist) = ? AND LOWER(album) = ?
            LIMIT 1
        """
        cursor.execute(query, (self._normalize_string(title), self._normalize_string(artist), self._normalize_string(album)))
        result = cursor.fetchone()
        
        if result:
            return result[0]
        
        # Try fuzzy match (title + artist)
        query = """
            SELECT track_id, artist, title, album 
            FROM tracks 
            WHERE LOWER(title) = ? AND LOWER(artist) = ?
            LIMIT 1
        """
        cursor.execute(query, (self._normalize_string(title), self._normalize_string(artist)))
        result = cursor.fetchone()
        
        return result[0] if result else None
    
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
            
            # Check if track already exists
            existing_id = self._match_track_in_db(title, artist, album)
            
            if existing_id:
                # Update existing track
                if not self.dry_run:
                    update_query = """
                        UPDATE tracks 
                        SET 
                            path = ?,
                            year = ?,
                            genres = ?,
                            mbid = ?,
                            comment = ?,
                            imp_from_file = 1,
                            file_metadata_updated = datetime('now')
                        WHERE track_id = ?
                    """
                    cursor.execute(update_query, (
                        file_path,
                        metadata.get('year'),
                        metadata.get('genres_str', ''),
                        metadata.get('mbid'),
                        metadata.get('comment'),
                        existing_id
                    ))
                    self.db_conn.commit()
                
                self.matched += 1
                return True, f"Matched existing track: {artist} - {title}"
            else:
                # Create new track
                if not self.dry_run:
                    insert_query = """
                        INSERT INTO tracks 
                        (artist, title, album, path, year, genres, mbid, comment, imp_from_file)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """
                    cursor.execute(insert_query, (
                        artist,
                        title,
                        album,
                        file_path,
                        metadata.get('year'),
                        metadata.get('genres_str', ''),
                        metadata.get('mbid'),
                        metadata.get('comment')
                    ))
                    self.db_conn.commit()
                
                self.imported += 1
                return True, f"Imported new track: {artist} - {title}"
                
        except Exception as e:
            self.errors += 1
            return False, f"Error importing {title}: {str(e)}"
    
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
            
            if file_ext == '.mp3':
                metadata = extract_metadata_from_mp3(file_path)
            elif file_ext == '.flac':
                metadata = extract_metadata_from_flac(file_path)
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
        Scan directory and import metadata.
        
        Returns:
            Dictionary with scan results
        """
        logger.info(f"Starting MP3 import scan: {self.directory}")
        self.start_time = datetime.now()
        self.db_conn = get_db()
        
        try:
            # Count total files
            all_files = list(Path(self.directory).rglob('*'))
            audio_files = [f for f in all_files if f.suffix.lower() in SUPPORTED_FORMATS]
            self.total_files = len(audio_files)
            
            logger.info(f"Found {self.total_files} audio files to scan")
            
            if self.total_files == 0:
                return self._get_results("No audio files found")
            
            # Process each file
            for i, file_path in enumerate(audio_files, 1):
                self._scan_file(str(file_path))
                
                if i % 10 == 0:
                    logger.info(f"Progress: {i}/{self.total_files}")
            
            elapsed = (datetime.now() - self.start_time).total_seconds()
            return self._get_results(f"Scan completed in {elapsed:.1f}s", elapsed)
            
        except Exception as e:
            logger.error(f"Scan failed: {e}", exc_info=True)
            return self._get_results(f"Scan failed: {str(e)}", error=True)
        finally:
            if self.db_conn:
                close_db(self.db_conn)
    
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
