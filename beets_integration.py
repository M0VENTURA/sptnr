#!/usr/bin/env python3
"""
Beets integration module for music library tagging and organization.
Provides wrapper around beets CLI for import and configuration operations.
"""

import os
import json
import subprocess
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


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
  copy: yes
  write: yes
  autotag: yes
  timid: no

plugins:
  - acousticbrainz
  - lyrics
  - missing
  - duplicates

acousticbrainz:
  auto: no

lyrics:
  sources: genius google
  fallback: ''
  force: no

missing:
  album_count: yes
"""
        
        try:
            with open(self.config_file, 'w') as f:
                f.write(default_config)
            logger.info(f"Created default beets config at {self.config_file}")
            return True
        except Exception as e:
            logger.error(f"Failed to create beets config: {e}")
            return False


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
