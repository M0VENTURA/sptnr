#!/usr/bin/env python3
"""
Beets auto-import with metadata capture.

This script:
1. Runs 'beet import' (with config to auto-tag without prompts) on the music library
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
import time
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List
import yaml

try:
    from scan_history import log_album_scan
except ImportError as e:
    # Fallback if scan_history module not available
    logger = logging.getLogger(__name__)
    logger.warning(f"scan_history module import failed: {e}, using fallback")
    def log_album_scan(*args, **kwargs):
        logger.warning(f"log_album_scan called but module not available: args={args}, kwargs={kwargs}")

BEETS_LOG_PATH = os.environ.get("BEETS_LOG_PATH", "/config/beets_import.log")

# Configure module logger to write to beets_import.log (and keep console output)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    _fmt = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    try:
        os.makedirs(os.path.dirname(BEETS_LOG_PATH), exist_ok=True)
        fh = logging.FileHandler(BEETS_LOG_PATH)
        fh.setFormatter(_fmt)
        logger.addHandler(fh)
    except Exception:
        # Fallback: ignore file handler errors
        pass

    sh = logging.StreamHandler()
    sh.setFormatter(_fmt)
    logger.addHandler(sh)

# Avoid propagating to root to prevent duplicate lines in other logs
logger.propagate = False

# Database connection
DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")
BEETS_DB_PATH = "/config/beets/musiclibrary.db"
CONFIG_PATH = "/config"
MUSIC_PATH = "/music"
MP3_PROGRESS_FILE = os.environ.get("MP3_PROGRESS_FILE", str(Path(DB_PATH).parent / "mp3_scan_progress.json"))


def save_beets_progress(processed: int, total: int, *, status: str = "running", is_running: bool = True, current: Optional[str] = None):
    """Save beets import progress to JSON file for dashboard polling."""
    try:
        progress_data = {
            "is_running": is_running,
            "scan_type": "mp3_scan",
            "status": status,
            "processed_files": processed,
            "total_files": total,
            "percent_complete": int((processed / total * 100)) if total > 0 else 0,
        }

        if current:
            progress_data["current_item"] = current

        if not is_running:
            progress_data["completed_at"] = datetime.now().isoformat()

        with open(MP3_PROGRESS_FILE, 'w') as f:
            json.dump(progress_data, f)
    except Exception as e:
        logger.error(f"Error saving beets progress: {e}")


class BeetsAutoImporter:
    """Automated beets import with metadata capture."""
    
    def __init__(self, music_path: str = MUSIC_PATH, config_path: str = CONFIG_PATH):
        self.music_path = Path(music_path)
        self.config_path = Path(config_path)
        self.beets_config_readonly = self.config_path / "read_config.yaml"  # Read-only import config
        self.beets_config_update = self.config_path / "update_config.yaml"  # Update/write config
        self.beets_config = self.beets_config_readonly  # Default to read-only
        self.beets_db = Path(BEETS_DB_PATH)
        self.sptnr_db = Path(DB_PATH)
        
        # Ensure beets database directory exists
        self.beets_db.parent.mkdir(parents=True, exist_ok=True)
        
        # Ensure beets configs exist with safe defaults
        self._create_default_configs_if_missing()
    
    def _create_default_configs_if_missing(self):
        """Create default beets config files if they don't exist."""
        # Read-only config for importing
        self._create_config_file(self.beets_config_readonly, readonly=True)
        # Update config for writing tags and organizing files
        self._create_config_file(self.beets_config_update, readonly=False)
    
    def _create_config_file(self, config_path: Path, readonly: bool = True):
        """Create a beets config file if it doesn't exist or is empty."""
        try:
            # Check if config file exists and has content
            if config_path.exists() and config_path.stat().st_size > 0:
                with open(config_path, 'r') as f:
                    content = f.read().strip()
                    if content and content != '{}':
                        # Check if existing config has invalid resume value and fix it
                        try:
                            existing_config = yaml.safe_load(content)
                            if existing_config and 'import' in existing_config:
                                resume_val = existing_config['import'].get('resume')
                                if resume_val == 'no':
                                    # Fix invalid resume value
                                    logger.info(f"Fixing invalid resume='no' in {config_path}")
                                    existing_config['import']['resume'] = True if not readonly else False
                                    with open(config_path, 'w') as fw:
                                        yaml.dump(existing_config, fw, default_flow_style=False, sort_keys=False)
                                    logger.info(f"Updated resume value in {config_path}")
                                    return
                        except Exception as e:
                            logger.warning(f"Could not check/fix existing config: {e}")
                        # File has valid content, skip
                        return
            
            logger.info(f"Creating beets config at {config_path} (readonly={readonly})")
            
            if readonly:
                config = {
                    "directory": str(self.music_path),
                    "library": str(self.beets_db),
                    "import": {
                        "autotag": True,
                        "copy": False,
                        "write": False,
                        "incremental": True,
                        "resume": False,
                        "quiet": True  # Suppress beets own logging, we log output to our logger
                    },
                    "musicbrainz": {
                        "enabled": True
                    },
                    "plugins": ["duplicates", "info"]
                }
            else:
                config = {
                    "directory": str(self.music_path),
                    "library": str(self.beets_db),
                    "import": {
                        "autotag": True,
                        "copy": False,
                        "write": True,
                        "incremental": True,
                        "resume": True,
                        "quiet": False
                    },
                    "musicbrainz": {
                        "enabled": True
                    },
                    "item_fields": {
                        "disc_and_track": "u'%d%02d' % (disc, track)"
                    },
                    "paths": {
                        "default": "$albumartist/$year - $album/$disc_and_track. $artist - $title",
                        "comp": "Various Artists/$year - $album/$disc_and_track. $artist - $title",
                        "albumtype:soundtrack": "Soundtrack/$year - $album/$disc_and_track. $artist - $title",
                        "singleton": "$artist/$year - $title/$track. $artist - $title"
                    },
                    "convert": {
                        "auto": False,
                        "copy": True,
                        "format": "mp3",
                        "bitrate": 320,
                        "threads": 2,
                        "dest": "/music/mp3",
                        "never_convert_lossy": True
                    },
                    "plugins": ["duplicates", "info", "convert"]
                }
            
            with open(config_path, 'w') as f:
                yaml.dump(config, f, default_flow_style=False, sort_keys=False)
            
            logger.info(f"Config created successfully at {config_path}")
        
        except Exception as e:
            logger.error(f"Failed to create beets config at {config_path}: {e}")
    
    def _create_default_config_if_missing(self):
        """Create a default beets config file if it doesn't exist or is empty."""
        try:
            # Check if config file exists and has content
            if self.beets_config.exists() and self.beets_config.stat().st_size > 0:
                # File exists and has content, try to parse it to verify it's valid
                with open(self.beets_config, 'r') as f:
                    content = f.read().strip()
                    if content and content != '{}':
                        # File has valid content, skip
                        return
            
            # File doesn't exist, is empty, or is invalid - create default config
            logger.info(f"Creating default beets config at {self.beets_config}")
            
            default_config = {
                "directory": str(self.music_path),
                "library": str(self.beets_db),
                "import": {
                    "copy": False,  # Don't copy, files are already in /music
                    "write": False,  # Don't modify files (-A mode)
                    "autotag": False,  # Don't auto-tag, just import metadata
                    "resume": True,  # Allow resuming interrupted imports
                    "incremental": True,  # Only import new/modified files
                    "log": str(self.config_path / "beets_import.log")
                },
                "musicbrainz": {
                    "enabled": False  # Disable MusicBrainz lookup when just importing
                },
                "plugins": ["duplicates", "info"]
            }
            
            with open(self.beets_config, 'w') as f:
                yaml.dump(default_config, f, default_flow_style=False, sort_keys=False)
            
            logger.info(f"Default beets config created successfully")
        
        except Exception as e:
            logger.error(f"Failed to create default beets config: {e}")
    
    def ensure_beets_config(self, use_update: bool = False):
        """
        Ensure beets configuration is up-to-date.
        
        Args:
            use_update: If True, use update_config.yaml (write mode); otherwise use read_config.yaml
        """
        config_file = self.beets_config_update if use_update else self.beets_config_readonly
        self.beets_config = config_file
        
        if not config_file.exists():
            self._create_config_file(config_file, readonly=not use_update)
        
        logger.info(f"Using beets config: {config_file} (readonly={not use_update})")
    
    def get_artists_in_beets(self) -> set:
        """
        Get set of artist names already in beets database.
        
        Returns:
            Set of artist names
        """
        try:
            beets_conn = sqlite3.connect(str(self.beets_db))
            beets_cursor = beets_conn.cursor()
            
            beets_cursor.execute("""
                SELECT DISTINCT items.artist 
                FROM items
                ORDER BY items.artist
            """)
            
            artists = {row[0] for row in beets_cursor.fetchall() if row[0]}
            beets_conn.close()
            
            logger.info(f"Found {len(artists)} artists in beets database")
            return artists
        except Exception as e:
            logger.error(f"Failed to get artists from beets: {e}")
            return set()

    def run_import(self, artist_path: Optional[str] = None, skip_existing: bool = False) -> subprocess.Popen:
        """
        Run beets import with auto-tagging.
        
        Args:
            artist_path: Optional specific artist folder to import
            skip_existing: If True, skip artists already in beets database
            
        Returns:
            Subprocess handle
        """
        # Check if beet command exists
        try:
            beet_check = subprocess.run(['which', 'beet'], capture_output=True, text=True)
            if beet_check.returncode != 0:
                logger.error("beet command not found! Is beets installed?")
                raise FileNotFoundError("beet command not found")
                logger.info(f"Found beet at: {beet_check.stdout.strip()}")
        except Exception as e:
                logger.error(f"Error checking for beet command: {e}")
        
        # Ensure beets database directory exists
        self.beets_db.parent.mkdir(parents=True, exist_ok=True)
        logger.info(f"Beets database directory: {self.beets_db.parent}")
        
        # Ensure beets config is up-to-date (use read-only config for import by default)
        self.ensure_beets_config(use_update=False)
        
        # Determine import path
        if artist_path:
            import_path = Path(artist_path)
        else:
            import_path = self.music_path
        
        # Filter out existing artists if requested
        if skip_existing and not artist_path:
            logger.info("Skip-existing enabled: filtering out artists already in beets...")
            existing_artists = self.get_artists_in_beets()
            
            if existing_artists:
                # Get subdirectories in music_path (artist folders)
                artist_folders = [d for d in import_path.iterdir() if d.is_dir()]
                filtered_folders = [d for d in artist_folders if d.name not in existing_artists]
                
                skipped = len(artist_folders) - len(filtered_folders)
                logger.info(f"Found {len(existing_artists)} existing artists")
                logger.info(f"Skipping {skipped} artist(s) already in beets")
                logger.info(f"Will import {len(filtered_folders)} new/updated artist folder(s)")
                
                # Create temporary list of folders to import
                if not filtered_folders:
                    logger.warning("\n" + "="*80)
                    logger.warning("All artists already in beets database. Nothing new to import.")
                    logger.warning(f"Total artists in beets: {len(existing_artists)}")
                    logger.warning(f"Total folders in /music: {len(artist_folders)}")
                    logger.warning("="*80 + "\n")
                    # Return a dummy process that does nothing
                    process = subprocess.Popen(['echo', 'No new artists to import'], 
                                              stdout=subprocess.PIPE, 
                                              stderr=subprocess.STDOUT,
                                              text=True)
                    return process
                
                # Import all filtered folders in one batch instead of one-by-one
                # This allows beets to process them all, not just the last one
                if filtered_folders:
                    logger.info(f"Importing {len(filtered_folders)} new artist(s)")
                    for folder in filtered_folders:
                        logger.info(f"  - {folder.name}")
                    
                    # Build single import command with all folders
                    cmd = [
                        "beet", "import",
                        "-c", str(self.beets_config),  # Use our config
                        "--library", str(self.beets_db),
                    ]
                    # Add all filtered folders as arguments
                    cmd.extend([str(folder) for folder in filtered_folders])
                    
                    logger.info(f"Running: {' '.join(cmd)}")
                    logger.info(f"Non-interactive mode enabled via config (autotag=True, write=False)")
                    
                    # Run as single process with all new artists
                    process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                        universal_newlines=True,
                        stdin=subprocess.DEVNULL  # Ensure no stdin available to prevent hanging
                    )
                    return process
                else:
                    # No new artists to import
                    logger.warning("All artists already in beets database. Nothing to import.")
                    process = subprocess.Popen(['echo', 'No new artists to import'], 
                                              stdout=subprocess.PIPE, 
                                              stderr=subprocess.STDOUT,
                                              text=True)
                    return process
        
        logger.info(f"Import path: {import_path}")
        logger.info(f"Import path exists: {import_path.exists()}")
        if import_path.exists():
            file_count = sum(1 for _ in import_path.rglob('*.mp3'))
            logger.info(f"Found {file_count} .mp3 files in import path")
        
        # Check beets database to see how many tracks are already imported
        try:
            import sqlite3
            beets_conn = sqlite3.connect(str(self.beets_db))
            beets_cursor = beets_conn.cursor()
            beets_cursor.execute("SELECT COUNT(*) FROM items")
            existing_count = beets_cursor.fetchone()[0]
            beets_conn.close()
            logger.info(f"Beets database currently has {existing_count} tracks")
        except Exception as e:
            logger.warning(f"Could not check beets database: {e}")
        
        cmd = [
            "beet",
            "-c", str(self.beets_config),  # Use our config
            "import",
            str(import_path)
        ]
        
        logger.info(f"Running: {' '.join(cmd)}")
        logger.info(f"With incremental mode enabled, beets will skip files already in database")
        logger.info(f"Non-interactive mode enabled via config (autotag=True, write=False)")
        
        # Run with live output capture
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True,
            stdin=subprocess.DEVNULL  # Ensure no stdin available to prevent hanging
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
            logger.warning(f"Beets database not found: {self.beets_db}")
            return
        
        logger.info("Syncing beets metadata to sptnr database...")
        
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
            # NOTE: Use albums.mb_albumid for Release Group ID (album concept)
            # NOT items.mb_albumid which is Release ID (specific pressing)
            beets_cursor.execute("""
                SELECT 
                    items.id,
                    items.title,
                    items.artist,
                    items.album,
                    items.albumartist,
                    items.mb_trackid,
                    albums.mb_albumid as album_release_group_id,
                    items.mb_artistid,
                    items.path,
                    items.year,
                    items.added,
                    albums.albumartist as album_artist_credit
                FROM items
                LEFT JOIN albums ON items.album_id = albums.id
                ORDER BY items.albumartist, items.album, items.title
            """)
            
            beets_tracks = beets_cursor.fetchall()
            total_beets = len(beets_tracks)
            logger.info(f"Found {total_beets} tracks in beets database")

            # Mark progress start for the dashboard
            save_beets_progress(0, total_beets, status="running", is_running=True)
            
            updated_count = 0
            processed_tracks = 0
            current_album = None
            album_tracks = 0
            
            for idx, track in enumerate(beets_tracks, 1):
                processed_tracks = idx

                # Progress reporting every 50 tracks
                if idx % 50 == 0:
                    save_beets_progress(idx, total_beets, status="running", is_running=True, current=track['album'])
                    logger.info(f"Beets sync progress: {idx}/{total_beets}")
                
                # Track album changes for scan history logging
                album_artist = track['album_artist_credit'] or track['albumartist']
                if current_album != (album_artist, track['album']):
                    # Log the previous album if we were processing one
                    if current_album is not None and album_tracks > 0:
                        try:
                            log_album_scan(current_album[0], current_album[1], 'beets', album_tracks, 'completed')
                            logger.info(f"Logged beets scan for {current_album[0]} - {current_album[1]} ({album_tracks} tracks) to scan_history")
                        except Exception as e:
                            logger.error(f"Failed to log album scan for {current_album[0]} - {current_album[1]}: {e}", exc_info=True)
                    
                    current_album = (album_artist, track['album'])
                    album_tracks = 0
                
                # Decode path if it's bytes for matching
                track_path = track['path']
                if isinstance(track_path, bytes):
                    track_path = track_path.decode('utf-8', errors='replace')
                
                # Try to match by path first, then by title+artist+album
                sptnr_cursor.execute("""
                    SELECT id FROM tracks 
                    WHERE file_path = ? 
                    OR (title = ? AND artist = ? AND album = ?)
                    LIMIT 1
                """, (
                    track_path,
                    track['title'],
                    track['artist'],
                    track['album']
                ))
                
                match = sptnr_cursor.fetchone()
                if match:
                    # Decode path if it's bytes
                    beets_path = track['path']
                    if isinstance(beets_path, bytes):
                        beets_path = beets_path.decode('utf-8', errors='replace')
                    
                    # Extract album folder path (parent directory of the track file)
                    # This is used for selective updates via beets
                    album_folder = str(Path(beets_path).parent)
                    
                    # Update sptnr track with beets metadata
                    # Use album_release_group_id from albums table (not items.mb_albumid)
                    sptnr_cursor.execute("""
                        UPDATE tracks SET
                            beets_mbid = ?,
                            beets_album_mbid = ?,
                            beets_artist_mbid = ?,
                            beets_album_artist = ?,
                            beets_year = ?,
                            beets_import_date = ?,
                            beets_path = ?,
                            album_folder = ?
                        WHERE id = ?
                    """, (
                        track['mb_trackid'],
                        track['album_release_group_id'],  # Use release group ID from albums table
                        track['mb_artistid'],
                        track['album_artist_credit'] or track['albumartist'],
                        track['year'],
                        datetime.fromtimestamp(track['added']).strftime('%Y-%m-%dT%H:%M:%S') if track['added'] else None,
                        beets_path,
                        album_folder,
                        match[0]
                    ))
                    updated_count += 1
                    album_tracks += 1
            
            # Log the final album after the loop completes
            if current_album is not None and album_tracks > 0:
                try:
                    log_album_scan(current_album[0], current_album[1], 'beets', album_tracks, 'completed')
                    logger.info(f"Logged final beets scan for {current_album[0]} - {current_album[1]} ({album_tracks} tracks) to scan_history")
                except Exception as e:
                    logger.error(f"Failed to log final album scan for {current_album[0]} - {current_album[1]}: {e}", exc_info=True)
            
            sptnr_conn.commit()
            logger.info(f"Updated {updated_count} tracks with beets metadata")

            # Mark completion for dashboard
            save_beets_progress(total_beets, total_beets, status="complete", is_running=False)
            
            beets_conn.close()
            sptnr_conn.close()
            
        except Exception as e:
            logger.error(f"Failed to sync beets metadata: {e}")
            try:
                save_beets_progress(processed_tracks, total_beets if 'total_beets' in locals() else 0, status="error", is_running=False)
            except Exception:
                pass
    
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
                    logger.info(f"Added column: {col_name}")
                except Exception as e:
                    logger.warning(f"Could not add column {col_name}: {e}")
    
    def import_and_capture(self, artist_path: Optional[str] = None, skip_existing: bool = False):
        """
        Run full import with live output capture and metadata sync.
        
        Args:
            artist_path: Optional specific artist folder to import
            skip_existing: If True, skip artists already in beets database
        """
        self.ensure_beets_config()
        
        logger.info("\n" + "="*80)
        logger.info("BEETS AUTO-IMPORT SESSION STARTED")
        logger.info("="*80)
        logger.info(f"Music path: {self.music_path}")
        logger.info(f"Config path: {self.config_path}")
        logger.info(f"Beets config: {self.beets_config}")
        logger.info(f"Beets DB: {self.beets_db}")
        logger.info(f"Skip existing artists: {skip_existing}")
        
        # Count folders and files
        if self.music_path.exists():
            artist_folders = [d for d in self.music_path.iterdir() if d.is_dir()]
            logger.info(f"Artist folders in /music: {len(artist_folders)}")
        
        total_files = 0
        if self.music_path.exists():
            total_files = sum(1 for _ in self.music_path.rglob('*.mp3'))
        logger.info(f"Total .mp3 files in /music: {total_files}")
        logger.info("="*80 + "\n")
        
        save_beets_progress(0, total_files, status="starting", is_running=True)

        try:
            process = self.run_import(artist_path, skip_existing=skip_existing)
            logger.info(f"Beets import process started with PID {process.pid}")
        except Exception as e:
            logger.error(f"Failed to start beets import process: {e}")
            import traceback
            logger.error(traceback.format_exc())
            save_beets_progress(0, total_files, status="error", is_running=False)
            return False

        # Capture output in real-time with timeout handling
        import_metadata: list[dict] = []
        current_item: dict = {}
        line_count = 0
        last_output_time = time.time()
        output_timeout = 300  # 5 minutes without output = timeout

        try:
            while True:
                # Check if process is still running
                poll_result = process.poll()
                
                try:
                    # Non-blocking read with timeout
                    line = process.stdout.readline()
                    
                    if line:
                        last_output_time = time.time()  # Reset timeout on new output
                        line = line.strip()
                        if line:  # Skip empty lines
                            line_count += 1
                            print(line)  # Echo to console
                            logger.info(f"BEETS: {line}")

                            metadata = self.parse_import_output(line)
                            if metadata:
                                if current_item:
                                    import_metadata.append(current_item)
                                current_item = metadata
                            elif current_item and line:
                                current_item.setdefault('output_lines', []).append(line)
                    elif poll_result is not None:
                        # Process ended and no more output
                        break
                    else:
                        # No output but process still running - check for timeout
                        elapsed = time.time() - last_output_time
                        if elapsed > output_timeout:
                            logger.warning(f"Beets import timeout: no output for {output_timeout} seconds")
                            logger.warning("Attempting to terminate beets process...")
                            process.terminate()
                            try:
                                process.wait(timeout=10)
                            except subprocess.TimeoutExpired:
                                logger.error("Force killing beets process")
                                process.kill()
                            break
                        else:
                            time.sleep(0.1)  # Small sleep to avoid busy-waiting
                
                except Exception as e:
                    logger.error(f"Error reading beets output: {e}")
                    break

        except Exception as e:
            logger.error(f"Unexpected error during beets import: {e}")
            import traceback
            logger.error(traceback.format_exc())
            try:
                process.terminate()
                process.wait(timeout=5)
            except:
                process.kill()

        if current_item:
            import_metadata.append(current_item)

        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            logger.error("Process did not terminate gracefully, killing...")
            process.kill()
            process.wait()

        logger.info(f"Beets process completed with return code: {process.returncode}")
        logger.info(f"Captured {line_count} output lines from beets")
        
        # Check how many tracks are now in beets database
        try:
            import sqlite3
            beets_conn = sqlite3.connect(str(self.beets_db))
            beets_cursor = beets_conn.cursor()
            beets_cursor.execute("SELECT COUNT(*) FROM items")
            final_count = beets_cursor.fetchone()[0]
            beets_conn.close()
            logger.info(f"Beets database now has {final_count} tracks")
        except Exception as e:
            logger.warning(f"Could not check final beets database count: {e}")

        if process.returncode is not None and process.returncode not in (0, -15):  # -15 is SIGTERM
            logger.error(f"Beets import failed with return code {process.returncode}")
            save_beets_progress(0, total_files, status="error", is_running=False)
            # Don't return False immediately - still try to sync what was imported
        
        # Save captured metadata
        if import_metadata:
            metadata_file = self.config_path / "beets_import_metadata.json"
            try:
                with open(metadata_file, 'w') as f:
                    json.dump(import_metadata, f, indent=2)
                logger.info(f"Saved {len(import_metadata)} import records to {metadata_file}")
            except Exception as e:
                logger.error(f"Could not save import metadata: {e}")
        
        if line_count == 0:
            logger.warning("No output captured from beets - this may indicate an issue")
            logger.warning("Proceeding to sync any data that was imported...")

        # Sync beets database to sptnr even if import had issues
        logger.info("Syncing beets metadata to sptnr database...")
        try:
            self.sync_beets_to_sptnr()
            logger.info("Beets metadata sync completed successfully")
        except Exception as e:
            logger.error(f"Error syncing beets to sptnr: {e}")
            import traceback
            logger.error(traceback.format_exc())
            save_beets_progress(total_files, total_files, status="error", is_running=False)
            return False

        logger.info("Beets auto-import complete!")
        save_beets_progress(total_files, total_files, status="complete", is_running=False)
        return True  # Consider it successful if we synced the data


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
        logger.info("Sync-only mode: updating sptnr from beets database")
        importer.sync_beets_to_sptnr()
    else:
        success = importer.import_and_capture(artist_path=args.artist)
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
