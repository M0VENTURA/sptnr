#!/usr/bin/env python3
"""
Beets auto-import with metadata capture.

This script:
1. Runs 'beet import -A' (auto-tag without prompts) on the music library
2. Captures autotagger output (MusicBrainz matches, similarity scores)
3. Stores beets recommendations in the sptnr database
4. Updates existing tracks with beets metadata
5. Cross-references beets database with sptnr database
"""

import os
import sys
import re
import json
import sqlite3
import subprocess
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List
import yaml

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Database connection
DB_PATH = "/database/sptnr.db"
BEETS_DB_PATH = "/config/beets/musiclibrary.db"
CONFIG_PATH = "/config"
MUSIC_PATH = "/music"


class BeetsAutoImporter:
    """Automated beets import with metadata capture."""
    
    def __init__(self, music_path: str = MUSIC_PATH, config_path: str = CONFIG_PATH):
        self.music_path = Path(music_path)
        self.config_path = Path(config_path)
        self.beets_config = self.config_path / "beetsconfig.yaml"
        self.beets_db = Path(BEETS_DB_PATH)
        self.sptnr_db = Path(DB_PATH)
        
        # Ensure beets database directory exists
        self.beets_db.parent.mkdir(parents=True, exist_ok=True)
    
    def ensure_beets_config(self):
        """Create or update beets configuration for auto-import."""
        config = {
            "directory": str(self.music_path),
            "library": str(self.beets_db),
            "import": {
                "copy": False,  # Don't copy, files are already in /music
                "write": True,  # Write tags to files
                "autotag": True,  # Enable auto-tagging
                "timid": False,  # Don't prompt for confirmation
                "resume": True,  # Resume interrupted imports
                "quiet_fallback": "skip",  # Skip items with no strong match
                "detail": True,  # Show detailed match info
                "log": str(self.config_path / "beets_import.log")
            },
            "match": {
                "strong_rec_thresh": 0.04,  # Threshold for strong recommendation
                "medium_rec_thresh": 0.25,  # Threshold for medium recommendation
                "ignored": ["EP", "live", "remix"]  # Patterns to ignore in matching
            },
            "musicbrainz": {
                "enabled": True
            },
            "plugins": ["duplicates", "missing", "info"]
        }
        
        with open(self.beets_config, 'w') as f:
            yaml.dump(config, f, default_flow_style=False)
        
        logging.info(f"Created beets config at {self.beets_config}")
    
    def run_import(self, artist_path: Optional[str] = None) -> subprocess.Popen:
        """
        Run beets import with auto-tagging.
        
        Args:
            artist_path: Optional specific artist folder to import
            
        Returns:
            Subprocess handle
        """
        # Ensure beets database directory exists
        self.beets_db.parent.mkdir(parents=True, exist_ok=True)
        
        import_path = Path(artist_path) if artist_path else self.music_path
        
        cmd = [
            "beet", "import",
            "-A",  # Auto-tag without prompts
            "-c", str(self.beets_config),  # Use our config
            "--library", str(self.beets_db),
            str(import_path)
        ]
        
        logging.info(f"Running: {' '.join(cmd)}")
        
        # Run with live output capture
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True
        )
        
        return process
    
    def parse_import_output(self, line: str) -> Optional[Dict]:
        """
        Parse beets import output line for metadata.
        
        Example output:
        "Looking up: Artist Name - Album Name"
        "Tagging: Artist - Album (Similarity: 98.5%)"
        "  Artist Name - Album Name (2020)"
        "    (Similarity: 98.5%, MBID: abc123...)"
        
        Returns:
            Dict with parsed metadata or None
        """
        metadata = {}
        
        # Match: "Tagging: Artist - Album"
        tagging_match = re.search(r'Tagging:\s+(.+?)\s+-\s+(.+?)(?:\s+\(|$)', line)
        if tagging_match:
            metadata['artist'] = tagging_match.group(1).strip()
            metadata['album'] = tagging_match.group(2).strip()
        
        # Match similarity score
        sim_match = re.search(r'Similarity:\s+([\d.]+)%', line)
        if sim_match:
            metadata['similarity'] = float(sim_match.group(1))
        
        # Match MBID
        mbid_match = re.search(r'MBID:\s+([a-f0-9-]{36})', line, re.IGNORECASE)
        if mbid_match:
            metadata['mbid'] = mbid_match.group(1)
        
        # Match album MBID
        album_mbid_match = re.search(r'album[_-]?mbid:\s+([a-f0-9-]{36})', line, re.IGNORECASE)
        if album_mbid_match:
            metadata['album_mbid'] = album_mbid_match.group(1)
        
        # Match year
        year_match = re.search(r'\((\d{4})\)', line)
        if year_match:
            metadata['year'] = int(year_match.group(1))
        
        return metadata if metadata else None
    
    def sync_beets_to_sptnr(self):
        """
        Sync metadata from beets database to sptnr database.
        
        Reads beets SQLite database and updates sptnr tracks with:
        - beets_mbid (MusicBrainz recording ID)
        - beets_album_mbid (MusicBrainz release ID)
        - beets_similarity (match confidence)
        - beets_album_artist (official album artist from MusicBrainz)
        - beets_import_date
        """
        if not self.beets_db.exists():
            logging.warning(f"Beets database not found: {self.beets_db}")
            return
        
        logging.info("Syncing beets metadata to sptnr database...")
        
        try:
            # Connect to both databases
            beets_conn = sqlite3.connect(self.beets_db)
            beets_conn.row_factory = sqlite3.Row
            beets_cursor = beets_conn.cursor()
            
            sptnr_conn = sqlite3.connect(self.sptnr_db)
            sptnr_cursor = sptnr_conn.cursor()
            
            # First, ensure sptnr has beets columns
            self._ensure_beets_columns(sptnr_cursor)
            
            # Get all items from beets
            beets_cursor.execute("""
                SELECT 
                    items.id,
                    items.title,
                    items.artist,
                    items.album,
                    items.albumartist,
                    items.mb_trackid,
                    items.mb_albumid,
                    items.mb_artistid,
                    items.path,
                    items.year,
                    items.added,
                    albums.albumartist as album_artist_credit
                FROM items
                LEFT JOIN albums ON items.album_id = albums.id
            """)
            
            beets_tracks = beets_cursor.fetchall()
            logging.info(f"Found {len(beets_tracks)} tracks in beets database")
            
            updated_count = 0
            for track in beets_tracks:
                # Try to match by path first, then by title+artist+album
                sptnr_cursor.execute("""
                    SELECT id FROM tracks 
                    WHERE file_path = ? 
                    OR (title = ? AND artist = ? AND album = ?)
                    LIMIT 1
                """, (
                    track['path'],
                    track['title'],
                    track['artist'],
                    track['album']
                ))
                
                match = sptnr_cursor.fetchone()
                if match:
                    # Update sptnr track with beets metadata
                    sptnr_cursor.execute("""
                        UPDATE tracks SET
                            beets_mbid = ?,
                            beets_album_mbid = ?,
                            beets_artist_mbid = ?,
                            beets_album_artist = ?,
                            beets_year = ?,
                            beets_import_date = ?,
                            beets_path = ?
                        WHERE id = ?
                    """, (
                        track['mb_trackid'],
                        track['mb_albumid'],
                        track['mb_artistid'],
                        track['album_artist_credit'] or track['albumartist'],
                        track['year'],
                        datetime.fromtimestamp(track['added']).strftime('%Y-%m-%dT%H:%M:%S') if track['added'] else None,
                        track['path'],
                        match[0]
                    ))
                    updated_count += 1
            
            sptnr_conn.commit()
            logging.info(f"Updated {updated_count} tracks with beets metadata")
            
            beets_conn.close()
            sptnr_conn.close()
            
        except Exception as e:
            logging.error(f"Failed to sync beets metadata: {e}")
    
    def _ensure_beets_columns(self, cursor):
        """Ensure sptnr database has beets metadata columns."""
        columns_to_add = [
            ("beets_mbid", "TEXT"),  # MusicBrainz recording ID
            ("beets_album_mbid", "TEXT"),  # MusicBrainz release ID
            ("beets_artist_mbid", "TEXT"),  # MusicBrainz artist ID
            ("beets_similarity", "REAL"),  # Match confidence (0-100)
            ("beets_album_artist", "TEXT"),  # Official album artist
            ("beets_year", "INTEGER"),  # Release year from MusicBrainz
            ("beets_import_date", "TEXT"),  # When imported by beets
            ("beets_path", "TEXT")  # Path in beets database
        ]
        
        # Get existing columns
        cursor.execute("PRAGMA table_info(tracks)")
        existing_columns = {row[1] for row in cursor.fetchall()}
        
        # Add missing columns
        for col_name, col_type in columns_to_add:
            if col_name not in existing_columns:
                try:
                    cursor.execute(f"ALTER TABLE tracks ADD COLUMN {col_name} {col_type}")
                    logging.info(f"Added column: {col_name}")
                except Exception as e:
                    logging.warning(f"Could not add column {col_name}: {e}")
    
    def import_and_capture(self, artist_path: Optional[str] = None):
        """
        Run full import with live output capture and metadata sync.
        
        Args:
            artist_path: Optional specific artist folder to import
        """
        self.ensure_beets_config()
        
        logging.info("Starting beets auto-import...")
        process = self.run_import(artist_path)
        
        # Capture output in real-time
        import_metadata = []
        current_item = {}
        
        for line in iter(process.stdout.readline, ''):
            if not line:
                break
            
            line = line.strip()
            print(line)  # Echo to console
            
            # Parse metadata from output
            metadata = self.parse_import_output(line)
            if metadata:
                if current_item:
                    import_metadata.append(current_item)
                current_item = metadata
            elif current_item and line:
                # Accumulate additional info to current item
                current_item.setdefault('output_lines', []).append(line)
        
        if current_item:
            import_metadata.append(current_item)
        
        process.wait()
        
        # Save captured metadata
        if import_metadata:
            metadata_file = self.config_path / "beets_import_metadata.json"
            with open(metadata_file, 'w') as f:
                json.dump(import_metadata, f, indent=2)
            logging.info(f"Saved {len(import_metadata)} import records to {metadata_file}")
        
        # Sync beets database to sptnr
        logging.info("Syncing beets metadata to sptnr database...")
        self.sync_beets_to_sptnr()
        
        logging.info("Import complete!")
        return process.returncode == 0


def main():
    """Run beets auto-import."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Beets auto-import with metadata capture")
    parser.add_argument("--artist", help="Import specific artist folder only")
    parser.add_argument("--sync-only", action="store_true", help="Only sync existing beets DB to sptnr, don't import")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    importer = BeetsAutoImporter()
    
    if args.sync_only:
        logging.info("Sync-only mode: updating sptnr from beets database")
        importer.sync_beets_to_sptnr()
    else:
        success = importer.import_and_capture(artist_path=args.artist)
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
