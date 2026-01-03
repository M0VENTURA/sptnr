#!/usr/bin/env python3
"""
Beets integration module for music library tagging and organization.
Provides wrapper around beets CLI for import and configuration operations.
"""

import os
import json
import subprocess
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

# Import auto-importer
try:
    from beets_auto_import import BeetsAutoImporter
except ImportError:
    BeetsAutoImporter = None
    logger.warning("beets_auto_import module not available")


class BeetsClient:
    """Wrapper for beets music tagger CLI."""
    
    def __init__(self, config_path: str = "/config", enabled: bool = True):
        """
        Initialize Beets client.
        
        Args:
            config_path: Path to beets config directory
            enabled: Whether beets is enabled
        """
        self.enabled = enabled
        self.config_path = Path(config_path)
        self.config_file = self.config_path / "beetsconfig.yaml"
        self.beets_dir = self.config_path / "beets"
        self.library_db = self.beets_dir / "musiclibrary.db"
    
    def is_installed(self) -> bool:
        """Check if beets is installed and available."""
        try:
            result = subprocess.run(
                ["beet", "--version"],
                capture_output=True,
                timeout=5
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
    
    def get_status(self) -> dict:
        """
        Get current beets status and configuration.
        
        Returns:
            Dict with status info
        """
        if not self.enabled:
            return {"enabled": False, "installed": False}
        
        installed = self.is_installed()
        config_exists = self.config_file.exists()
        
        return {
            "enabled": True,
            "installed": installed,
            "config_exists": config_exists,
            "library_db_exists": self.library_db.exists(),
            "config_path": str(self.config_file),
            "library_path": str(self.library_db)
        }
    
    def import_music(self, source_path: str, move: bool = True, autotag: bool = True) -> dict:
        """
        Import music files using beets.
        
        Args:
            source_path: Path to music files to import
            move: Whether to move files (vs. copy)
            autotag: Whether to auto-tag files
            
        Returns:
            Dict with import results
        """
        if not self.enabled or not self.is_installed():
            return {"success": False, "error": "Beets not available"}
        
        try:
            # Build beets import command
            cmd = ["beet", "import"]
            
            if move:
                cmd.append("-m")
            else:
                cmd.append("-c")
            
            if not autotag:
                cmd.append("-s")  # Skip automatic tagging
            
            # Set library database
            cmd.extend(["--library", str(self.library_db)])
            
            # Add source path
            cmd.append(source_path)
            
            logger.info(f"Running beets import: {' '.join(cmd)}")
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300  # 5 minute timeout for imports
            )
            
            if result.returncode == 0:
                logger.info(f"Beets import completed successfully from {source_path}")
                return {
                    "success": True,
                    "output": result.stdout,
                    "message": "Import completed"
                }
            else:
                error_msg = result.stderr or result.stdout or "Unknown error"
                logger.error(f"Beets import failed: {error_msg}")
                return {
                    "success": False,
                    "error": error_msg[:500]  # Truncate error
                }
                
        except subprocess.TimeoutExpired:
            logger.error("Beets import timed out")
            return {"success": False, "error": "Import timed out (>5 minutes)"}
        except Exception as e:
            logger.error(f"Beets import failed: {e}")
            return {"success": False, "error": str(e)[:500]}
    
    def get_library_stats(self) -> dict:
        """
        Get statistics about the beets library.
        
        Returns:
            Dict with library stats
        """
        if not self.enabled or not self.is_installed():
            return {"error": "Beets not available"}
        
        try:
            # Use beets list command to get stats
            cmd = ["beet", "list", "--library", str(self.library_db), "-f", "count"]
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode == 0:
                # Parse output to get counts
                output = result.stdout.strip()
                # beet list returns one item per line
                track_count = len(output.split('\n')) if output else 0
                
                return {
                    "success": True,
                    "track_count": track_count,
                    "library_path": str(self.library_db)
                }
            else:
                return {
                    "success": False,
                    "error": result.stderr[:200]
                }
                
        except Exception as e:
            logger.error(f"Failed to get library stats: {e}")
            return {"success": False, "error": str(e)[:200]}
    
    def create_default_config(self) -> bool:
        """
        Create a default beets configuration file.
        
        Returns:
            True if successful
        """
        if not self.config_path.exists():
            self.config_path.mkdir(parents=True, exist_ok=True)
        
        if not self.beets_dir.exists():
            self.beets_dir.mkdir(parents=True, exist_ok=True)
        
        default_config = """
directory: /music
library: /config/beets/musiclibrary.db

import:
  copy: no
  write: yes
  autotag: yes
  timid: no
  resume: yes
  quiet_fallback: skip
  detail: yes
  log: /config/beets_import.log

match:
  strong_rec_thresh: 0.04
  medium_rec_thresh: 0.25

musicbrainz:
  enabled: yes

plugins:
  - duplicates
  - missing
  - info
"""
        
        try:
            with open(self.config_file, 'w') as f:
                f.write(default_config)
            logger.info(f"Created default beets config at {self.config_file}")
            return True
        except Exception as e:
            logger.error(f"Failed to create beets config: {e}")
            return False
    
    def auto_import_library(self, artist_path: str = None) -> dict:
        """
        Run auto-import on entire library or specific artist.
        
        Args:
            artist_path: Optional path to specific artist folder
            
        Returns:
            Dict with import results
        """
        if not BeetsAutoImporter:
            return {"success": False, "error": "Auto-importer not available"}
        
        try:
            importer = BeetsAutoImporter(config_path=str(self.config_path))
            success = importer.import_and_capture(artist_path=artist_path)
            
            return {
                "success": success,
                "message": "Auto-import completed" if success else "Auto-import failed"
            }
        except Exception as e:
            logger.error(f"Auto-import failed: {e}")
            return {"success": False, "error": str(e)}
    
    def sync_beets_metadata(self) -> dict:
        """
        Sync metadata from beets database to sptnr database.
        
        Returns:
            Dict with sync results
        """
        if not BeetsAutoImporter:
            return {"success": False, "error": "Auto-importer not available"}
        
        try:
            importer = BeetsAutoImporter(config_path=str(self.config_path))
            importer.sync_beets_to_sptnr()
            
            return {
                "success": True,
                "message": "Beets metadata synced to sptnr database"
            }
        except Exception as e:
            logger.error(f"Metadata sync failed: {e}")
            return {"success": False, "error": str(e)}
    
    def get_beets_recommendations(self, track_id: str = None) -> dict:
        """
        Get beets/MusicBrainz recommendations for a track.
        
        Args:
            track_id: Sptnr track ID
            
        Returns:
            Dict with beets metadata
        """
        try:
            sptnr_db = Path("/database/sptnr.db")
            if not sptnr_db.exists():
                return {"success": False, "error": "Sptnr database not found"}
            
            conn = sqlite3.connect(sptnr_db)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT 
                    beets_mbid,
                    beets_album_mbid,
                    beets_artist_mbid,
                    beets_similarity,
                    beets_album_artist,
                    beets_year,
                    beets_import_date,
                    beets_path
                FROM tracks
                WHERE id = ?
            """, (track_id,))
            
            row = cursor.fetchone()
            conn.close()
            
            if row:
                return {
                    "success": True,
                    "mbid": row['beets_mbid'],
                    "album_mbid": row['beets_album_mbid'],
                    "artist_mbid": row['beets_artist_mbid'],
                    "similarity": row['beets_similarity'],
                    "album_artist": row['beets_album_artist'],
                    "year": row['beets_year'],
                    "import_date": row['beets_import_date'],
                    "path": row['beets_path']
                }
            else:
                return {"success": False, "error": "No beets data for this track"}
                
        except Exception as e:
            logger.error(f"Failed to get beets recommendations: {e}")
            return {"success": False, "error": str(e)}


# Backward-compatible module functions
_beets_client = None

def _get_beets_client(config_path: str = "/config", enabled: bool = True):
    """Get or create singleton beets client."""
    global _beets_client
    if _beets_client is None:
        _beets_client = BeetsClient(config_path, enabled=enabled)
    return _beets_client

def get_beets_status(config_path: str = "/config", enabled: bool = True) -> dict:
    """Backward-compatible wrapper."""
    client = _get_beets_client(config_path, enabled)
    return client.get_status()

def beets_import(source_path: str, move: bool = True, config_path: str = "/config", enabled: bool = True) -> dict:
    """Backward-compatible wrapper."""
    client = _get_beets_client(config_path, enabled)
    return client.import_music(source_path, move=move)

def get_beets_stats(config_path: str = "/config", enabled: bool = True) -> dict:
    """Backward-compatible wrapper."""
    client = _get_beets_client(config_path, enabled)
    return client.get_library_stats()
