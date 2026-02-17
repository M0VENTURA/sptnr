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
import yaml
from pathlib import Path

# Import centralized logging
from logging_config import setup_logging, log_unified, log_info, log_debug

# Set up logging for beets service
setup_logging("beets")

# Keep standard logger for backward compatibility
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Import auto-importer
try:
    from beets_auto_import import BeetsAutoImporter
except ImportError:
    BeetsAutoImporter = None
    logger.warning("beets_auto_import module not available")


class BeetsClient:
    """Wrapper for beets music tagger CLI."""
    
    def __init__(self, config_path: str = os.environ.get("CONFIG_PATH", "/config"), enabled: bool = True):
        """
        Initialize Beets client.
        
        Args:
            config_path: Path to config directory
            enabled: Whether beets is enabled
        """
        self.enabled = enabled
        self.config_path = Path(config_path)
        self.main_config_file = self.config_path / "config.yaml"
        self.config_file = self.config_path / "beetsconfig.yaml"
        self.beets_dir = self.config_path / "beets"
        self.library_db = self.beets_dir / "musiclibrary.db"
        self.beets_config = self._load_beets_config()
        
        # Ensure beets directory exists
        if self.enabled:
            self.beets_dir.mkdir(parents=True, exist_ok=True)
            # Generate beets config file from main config
            self._generate_beets_config()
    
    def _load_beets_config(self) -> dict:
        """
        Load beets configuration from main config.yaml.
        
        Returns:
            Dict with beets configuration
        """
        try:
            if self.main_config_file.exists():
                with open(self.main_config_file, 'r') as f:
                    config = yaml.safe_load(f)
                    return config.get('beets', {})
            else:
                logger.warning(f"Main config file not found: {self.main_config_file}")
                return {}
        except Exception as e:
            logger.error(f"Failed to load beets config from {self.main_config_file}: {e}")
            return {}
    
    def _generate_beets_config(self, mode: str = "import_downloads"):
        """
        Generate beets configuration file from main config.yaml settings.
        
        Args:
            mode: Either "import_downloads" or "scan_collection" to select config profile
        """
        if not self.beets_config:
            logger.warning("No beets configuration found in config.yaml, using defaults")
            return
        
        # Get the mode-specific configuration
        mode_config = self.beets_config.get(mode, {})
        
        # Build beets config structure
        beets_yaml_config = {
            'directory': self.beets_config.get('directory', '/music'),
            'library': self.beets_config.get('library', '/config/beets/musiclibrary.db'),
            'import': {
                'copy': mode_config.get('copy', False),
                'write': mode_config.get('write', True),
                'autotag': mode_config.get('autotag', True),
                'resume': mode_config.get('resume', True),
                'incremental': mode_config.get('incremental', True),
                'log': mode_config.get('log', '/config/beets_import.log'),
            }
        }
        
        # Add optional import settings
        if 'quiet_fallback' in mode_config:
            beets_yaml_config['import']['quiet_fallback'] = mode_config['quiet_fallback']
        if 'timid' in mode_config:
            beets_yaml_config['import']['timid'] = mode_config['timid']
        if 'detail' in mode_config:
            beets_yaml_config['import']['detail'] = mode_config['detail']
        
        # Add match configuration
        if 'match' in self.beets_config:
            beets_yaml_config['match'] = self.beets_config['match']
        
        # Add paths configuration
        if 'paths' in self.beets_config:
            beets_yaml_config['paths'] = self.beets_config['paths']
        
        # Add MusicBrainz configuration
        mb_config = self.beets_config.get('musicbrainz', {})
        beets_yaml_config['musicbrainz'] = {
            'enabled': mb_config.get('enabled', True),
        }
        if 'rate_limit' in mb_config:
            beets_yaml_config['musicbrainz']['ratelimit'] = mb_config['rate_limit']
        
        # Add plugins
        if 'plugins' in self.beets_config:
            beets_yaml_config['plugins'] = self.beets_config['plugins']
        
        # Write the config file
        try:
            with open(self.config_file, 'w') as f:
                yaml.dump(beets_yaml_config, f, default_flow_style=False, sort_keys=False)
            logger.debug(f"Generated beets config at {self.config_file} for mode: {mode}")
        except Exception as e:
            logger.error(f"Failed to generate beets config: {e}")
    
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
    
    def import_music(self, source_path: str, move: bool = True, autotag: bool = True, mode: str = "import_downloads") -> dict:
        """
        Import music files using beets.
        
        Args:
            source_path: Path to music files to import
            move: Whether to move files (vs. copy)
            autotag: Whether to auto-tag files
            mode: Config mode to use - "import_downloads" or "scan_collection"
            
        Returns:
            Dict with import results
        """
        if not self.enabled or not self.is_installed():
            return {"success": False, "error": "Beets not available"}
        
        try:
            # Regenerate config for the appropriate mode
            self._generate_beets_config(mode=mode)
            
            # Build beets import command
            cmd = ["beet", "import"]
            
            if move:
                cmd.append("-m")
            else:
                cmd.append("-c")
            
            if not autotag:
                cmd.append("-s")  # Skip automatic tagging
            
            # Set library database and config file
            cmd.extend(["--library", str(self.library_db)])
            cmd.extend(["--config", str(self.config_file)])
            
            # Add source path
            cmd.append(source_path)
            
            logger.info(f"Running beets import with mode '{mode}': {' '.join(cmd)}")
            
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
    
    def auto_import_library(self, artist_path: str = None, skip_existing: bool = False) -> dict:
        """
        Run auto-import on entire library or specific artist.
        
        Args:
            artist_path: Optional path to specific artist folder
            skip_existing: If True, skip artists already in beets database
            
        Returns:
            Dict with import results
        """
        if not BeetsAutoImporter:
            return {"success": False, "error": "Auto-importer not available"}
        
        try:
            importer = BeetsAutoImporter(config_path=str(self.config_path))
            success = importer.import_and_capture(artist_path=artist_path, skip_existing=skip_existing)
            
            return {
                "success": success,
                "message": "Auto-import completed" if success else "Auto-import failed",
                "skip_existing_enabled": skip_existing
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
                    musicbrainz_artist_id,
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
                    "artist_mbid": row['musicbrainz_artist_id'],
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


def update_track_metadata_with_beets(track_id: str, metadata: dict, db_path: str = None) -> bool:
    """
    Update track metadata in MP3/FLAC file using direct mutagen-based updates.
    
    This function replaces the previous beets-based approach that required files to be 
    imported into beets' database. Instead, it directly updates the audio file tags using
    mutagen, which is more reliable and doesn't have the dependency on beets import.
    
    Args:
        track_id: Track ID in the database
        metadata: Dict of metadata fields to update. Supported fields:
                 - title: Track title
                 - artist: Track artist
                 - album: Album name
                 - albumartist: Album artist
                 - genre: Genre (can be string or list)
                 - year: Release year
                 - composer: Composer name
                 - track: Track number
                 - disc: Disc number
                 - comments: Comments field
                 - mb_trackid: MusicBrainz track ID
                 - mb_albumid: MusicBrainz album ID
        db_path: Path to the database
        
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        # Get the track file path from the database
        if not db_path:
            db_path = os.environ.get("DB_PATH", "/config/sptnr.db")
        
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("SELECT beets_path, file_path, artist, album, title FROM tracks WHERE id = ?", (track_id,))
        row = cursor.fetchone()
        conn.close()
        
        if not row:
            logger.error(f"Track {track_id} not found in database")
            return False
        
        file_path = row['beets_path'] if row['beets_path'] else row['file_path']
        
        if not file_path:
            logger.error(f"No file path found for track {track_id}")
            return False
        
        # Construct the full file path using MUSIC_ROOT if the path is relative
        music_root = os.environ.get("MUSIC_ROOT", "/music")
        
        # Normalize music_root to not have trailing slash for consistency
        music_root = music_root.rstrip('/')
        
        if not os.path.isabs(file_path):
            # Relative path - prepend MUSIC_ROOT
            full_file_path = os.path.join(music_root, file_path)
        else:
            # Absolute path - check if it exists as-is first
            if os.path.exists(file_path):
                full_file_path = file_path
            else:
                # Try to detect if path has a prefix that should be replaced with MUSIC_ROOT
                # Common pattern: database has "/music/Artist/Album/track.mp3" but MUSIC_ROOT is "/mnt/music"
                # We need to strip the database prefix and use MUSIC_ROOT
                potential_prefix = "/music"
                if file_path.startswith(potential_prefix + "/"):
                    rel_path = file_path[len(potential_prefix) + 1:]  # +1 for the slash
                    full_file_path = os.path.join(music_root, rel_path)
                else:
                    # No known prefix, use as-is
                    full_file_path = file_path
        
        if not os.path.exists(full_file_path):
            logger.error(f"File not found for track {track_id}: {full_file_path} (original: {file_path})")
            return False
        
        # Determine file type
        file_ext = Path(full_file_path).suffix.lower()
        
        # Update metadata using mutagen based on file type
        if file_ext == '.mp3':
            success = _update_mp3_metadata(full_file_path, metadata)
        elif file_ext in ['.flac', '.fla']:
            success = _update_flac_metadata(full_file_path, metadata)
        else:
            logger.warning(f"Unsupported file format for metadata update: {file_ext}")
            return False
        
        if success:
            logger.info(f"Successfully updated metadata for track {track_id} at {full_file_path}")
        else:
            logger.error(f"Failed to update metadata for track {track_id}")
        
        return success
        
    except Exception as e:
        logger.error(f"Error updating track metadata: {e}")
        return False


def _update_mp3_metadata(file_path: str, metadata: dict) -> bool:
    """
    Update MP3 file metadata using mutagen.
    
    Args:
        file_path: Path to MP3 file
        metadata: Dict of metadata fields to update
        
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        from mutagen.id3 import ID3, TIT2, TPE1, TALB, TPE2, TCON, TDRC, TCOM, TRCK, TPOS, COMM, TXXX
        from mutagen.mp3 import MP3
        
        # Load the MP3 file
        audio = MP3(file_path, ID3=ID3)
        
        # Create ID3 tags if they don't exist
        if audio.tags is None:
            audio.add_tags()
        
        # Map metadata fields to ID3 tags
        if 'title' in metadata and metadata['title']:
            audio.tags['TIT2'] = TIT2(encoding=3, text=metadata['title'])
        
        if 'artist' in metadata and metadata['artist']:
            audio.tags['TPE1'] = TPE1(encoding=3, text=metadata['artist'])
        
        if 'album' in metadata and metadata['album']:
            audio.tags['TALB'] = TALB(encoding=3, text=metadata['album'])
        
        if 'albumartist' in metadata and metadata['albumartist']:
            audio.tags['TPE2'] = TPE2(encoding=3, text=metadata['albumartist'])
        
        if 'genre' in metadata and metadata['genre']:
            # Handle genre - can be string or list
            genre_value = metadata['genre']
            if isinstance(genre_value, str):
                # Check if it's comma-separated or double-backslash separated
                if '\\\\' in genre_value:
                    genre_str = genre_value
                else:
                    # Split on comma and reconstruct with double backslash for ID3 format
                    genre_list = [g.strip() for g in genre_value.split(',') if g.strip()]
                    genre_str = '\\\\'.join(genre_list)
            else:
                # It's a list, join with double backslash for ID3 format
                genre_str = '\\\\'.join(str(g).strip() for g in genre_value if g)
            
            audio.tags['TCON'] = TCON(encoding=3, text=[genre_str])
        
        if 'year' in metadata and metadata['year']:
            audio.tags['TDRC'] = TDRC(encoding=3, text=str(metadata['year']))
        
        if 'composer' in metadata and metadata['composer']:
            audio.tags['TCOM'] = TCOM(encoding=3, text=metadata['composer'])
        
        if 'track' in metadata and metadata['track']:
            audio.tags['TRCK'] = TRCK(encoding=3, text=str(metadata['track']))
        
        if 'disc' in metadata and metadata['disc']:
            audio.tags['TPOS'] = TPOS(encoding=3, text=str(metadata['disc']))
        
        if 'comments' in metadata and metadata['comments']:
            audio.tags['COMM'] = COMM(encoding=3, lang='eng', desc='', text=metadata['comments'])
        
        # Handle MusicBrainz IDs using TXXX frames
        # Remove existing TXXX frames to prevent duplicates, then add new ones
        if 'mb_trackid' in metadata and metadata['mb_trackid']:
            # Remove existing MUSICBRAINZ TRACK ID frames
            audio.tags.delall('TXXX:MUSICBRAINZ TRACK ID')
            audio.tags.add(TXXX(
                encoding=3,
                desc='MUSICBRAINZ TRACK ID',
                text=[metadata['mb_trackid']]
            ))
        
        if 'mb_albumid' in metadata and metadata['mb_albumid']:
            # Remove existing MUSICBRAINZ ALBUM ID frames
            audio.tags.delall('TXXX:MUSICBRAINZ ALBUM ID')
            audio.tags.add(TXXX(
                encoding=3,
                desc='MUSICBRAINZ ALBUM ID',
                text=[metadata['mb_albumid']]
            ))
        
        # Save changes
        audio.save()
        return True
        
    except Exception as e:
        logger.error(f"Failed to update MP3 metadata for {file_path}: {e}")
        return False


def _update_flac_metadata(file_path: str, metadata: dict) -> bool:
    """
    Update FLAC file metadata using mutagen.
    
    Args:
        file_path: Path to FLAC file
        metadata: Dict of metadata fields to update
        
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        from mutagen.flac import FLAC
        
        # Load the FLAC file
        audio = FLAC(file_path)
        
        # Map metadata fields to FLAC Vorbis comments
        if 'title' in metadata and metadata['title']:
            audio['title'] = metadata['title']
        
        if 'artist' in metadata and metadata['artist']:
            audio['artist'] = metadata['artist']
        
        if 'album' in metadata and metadata['album']:
            audio['album'] = metadata['album']
        
        if 'albumartist' in metadata and metadata['albumartist']:
            audio['albumartist'] = metadata['albumartist']
        
        if 'genre' in metadata and metadata['genre']:
            # Handle genre - can be string or list
            genre_value = metadata['genre']
            if isinstance(genre_value, str):
                genre_list = [g.strip() for g in genre_value.split(',') if g.strip()]
            else:
                genre_list = genre_value if isinstance(genre_value, list) else [genre_value]
            audio['genre'] = genre_list
        
        if 'year' in metadata and metadata['year']:
            audio['date'] = str(metadata['year'])
        
        if 'composer' in metadata and metadata['composer']:
            audio['composer'] = metadata['composer']
        
        if 'track' in metadata and metadata['track']:
            audio['tracknumber'] = str(metadata['track'])
        
        if 'disc' in metadata and metadata['disc']:
            audio['discnumber'] = str(metadata['disc'])
        
        if 'comments' in metadata and metadata['comments']:
            audio['comment'] = metadata['comments']
        
        # Handle MusicBrainz IDs
        if 'mb_trackid' in metadata and metadata['mb_trackid']:
            audio['musicbrainz_trackid'] = metadata['mb_trackid']
        
        if 'mb_albumid' in metadata and metadata['mb_albumid']:
            audio['musicbrainz_albumid'] = metadata['mb_albumid']
        
        # Save changes
        audio.save()
        return True
        
    except Exception as e:
        logger.error(f"Failed to update FLAC metadata for {file_path}: {e}")
        return False
