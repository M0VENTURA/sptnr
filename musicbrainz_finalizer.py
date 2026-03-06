#!/usr/bin/env python3
"""
MusicBrainz Release Finalization System

Handles the finalization of MusicBrainz releases when all tracks are discovered:
1. Detects releases with all tracks found (discovered_count == total_tracks)
2. Creates final directory structure in /music/ARTIST/YEAR - ALBUM/
3. Moves and renames files with track numbers: "01. Artist - Title.ext"
4. Updates database status from 'active' to 'finalized'
5. Cleans up empty monitoring folders

This system integrates with the background queue processor (runs every 60 seconds).
"""

import sqlite3
import shutil
import logging
from pathlib import Path
from datetime import datetime
from contextlib import closing
from typing import Any
from database_abstraction import DatabaseQuery, is_postgres_connection

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DOWNLOADS_MUSIC_DIR = "/downloads/Music"
MUSIC_LIBRARY_DIR = "/music"
DB_FILE = "sptnr.db"
DB_TIMEOUT = 120.0


class MusicBrainzFinalizer:
    """Finalizes MusicBrainz releases when all tracks are discovered"""

    def __init__(self):
        self.downloads_dir = Path(DOWNLOADS_MUSIC_DIR)
        self.music_dir = Path(MUSIC_LIBRARY_DIR)
        self.ensure_directories()
        self.ensure_schema()

    def ensure_directories(self):
        """Ensure all required directories exist"""
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.music_dir.mkdir(parents=True, exist_ok=True)

    def get_db(self):
        """Get database connection"""
        conn = sqlite3.connect(DB_FILE, timeout=DB_TIMEOUT)
        conn.row_factory = sqlite3.Row
        return conn

    def ensure_schema(self):
        """Ensure MusicBrainz release tables exist before finalization checks run."""
        try:
            from musicbrainz_release_manager import MusicBrainzReleaseManager
            MusicBrainzReleaseManager().ensure_schema()
        except Exception as e:
            logger.warning(f"[FINALIZER] Could not ensure MusicBrainz schema: {e}")

    def check_and_finalize_releases(self):
        """
        Main loop:
        1. Find all active releases with all tracks discovered
        2. Finalize each one
        3. Return count finalized
        """
        try:
            logger.info("[FINALIZER] Checking for releases ready to finalize...")
            
            ready_releases = self.find_ready_releases()
            if not ready_releases:
                logger.debug("[FINALIZER] No releases ready for finalization")
                return {"finalized": 0, "checked": 0}

            finalized_count = 0
            
            for release in ready_releases:
                try:
                    if self.finalize_release(release['id'], release['release_id']):
                        finalized_count += 1
                except Exception as e:
                    logger.error(f"[FINALIZER] Error finalizing release {release['release_id']}: {e}")
                    continue
            
            logger.info(f"[FINALIZER] Finalized {finalized_count}/{len(ready_releases)} releases")
            return {"finalized": finalized_count, "checked": len(ready_releases)}
            
        except Exception as e:
            logger.error(f"[FINALIZER] Error in check_and_finalize_releases: {e}")
            raise

    def find_ready_releases(self):
        """
        Find all active releases where discovered_count >= total_tracks
        
        Returns:
            List of dicts with release info
        """
        try:
            conn = self.get_db()
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT id, release_id, release_title, artist, release_year,
                       monitoring_folder_path, total_tracks, discovered_count,
                       created_at
                FROM musicbrainz_releases
                WHERE status = 'active'
                AND discovered_count >= total_tracks
                ORDER BY created_at ASC
            """)
            
            releases = [dict(row) for row in cursor.fetchall()]
            conn.close()
            
            if releases:
                logger.info(f"[FINALIZER] Found {len(releases)} releases ready for finalization")
            
            return releases
            
        except Exception as e:
            logger.error(f"[FINALIZER] Error finding ready releases: {e}")
            return []

    def finalize_release(self, release_db_id, release_id):
        """
        Finalize a single release:
        1. Get all files from monitoring folder
        2. Get track info from database
        3. Move and rename files to final location
        4. Update database status
        5. Cleanup monitoring folder
        
        Returns:
            bool indicating success
        """
        conn = None
        try:
            logger.info(f"[FINALIZER] Finalizing release {release_id}...")
            
            conn = self.get_db()
            cursor = conn.cursor()
            
            # Step 1: Get release info
            from app import _is_postgres_connection as app_is_postgres_connection
            is_pg = bool(app_is_postgres_connection(conn))
            placeholder = "%s" if is_pg else "?"
            cursor.execute(f"""
                SELECT release_title, artist, release_year, monitoring_folder_path,
                       total_tracks, discovered_count
                FROM musicbrainz_releases
                WHERE id = {placeholder}
            """, (release_db_id,))
            
            release = cursor.fetchone()
            if not release:
                logger.error(f"[FINALIZER] Release not found in database: {release_db_id}")
                return False
            
            monitoring_folder = Path(release['monitoring_folder_path'])
            
            # Verify folder exists and has files
            if not monitoring_folder.exists():
                logger.error(f"[FINALIZER] Monitoring folder not found: {monitoring_folder}")
                return False
            
            files = [f for f in monitoring_folder.glob('*') if f.is_file()]
            if not files:
                logger.warning(f"[FINALIZER] No files in monitoring folder: {monitoring_folder}")
                # Still mark as finalized since all tracks discovered (must be in DB already)
            
            # Step 2: Create final directory
            final_dir = self.create_final_directory(release)
            if not final_dir:
                logger.error(f"[FINALIZER] Failed to create final directory for {release_id}")
                return False
            
            logger.info(f"[FINALIZER] Created final directory: {final_dir}")
            
            # Step 3: Move and rename files
            moved_count = 0
            for file_path in files:
                if self.move_and_rename_file(file_path, release_db_id, release_id, final_dir, conn):
                    moved_count += 1
            
            logger.info(f"[FINALIZER] Moved {moved_count}/{len(files)} files to final location")
            
            # Step 4: Update release status
            cursor.execute("""
                UPDATE musicbrainz_releases
                SET status = 'finalized',
                    finalized_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (release_db_id,))
            
            # Step 5: Cleanup monitoring folder
            self.cleanup_monitoring_folder(monitoring_folder)
            
            conn.commit()
            conn.close()
            
            logger.info(f"[FINALIZER] Successfully finalized release {release_id}")
            return True
            
        except Exception as e:
            logger.error(f"[FINALIZER] Error finalizing release: {e}")
            if conn:
                try:
                    conn.close()
                except:
                    pass
            return False

    def create_final_directory(self, release):
        """
        Create final directory structure: /music/ARTIST/YEAR - ALBUM/
        
        Returns:
            Path object for the directory or None if error
        """
        try:
            artist = str(release['artist']).replace('/', '_').replace('\\', '_')[:100]
            album = str(release['release_title']).replace('/', '_').replace('\\', '_')[:100]
            year = str(release['release_year'])
            
            # Format: /music/ARTIST/YEAR - ALBUM/
            final_dir = self.music_dir / artist / f"{year} - {album}"
            final_dir.mkdir(parents=True, exist_ok=True)
            
            logger.debug(f"[FINALIZER] Created directory: {final_dir}")
            return final_dir
            
        except Exception as e:
            logger.error(f"[FINALIZER] Error creating final directory: {e}")
            return None

    def move_and_rename_file(self, file_path, release_db_id, release_id, final_dir, conn):
        """
        Move file from monitoring folder to final location with new name
        
        New name format: "01. Artist - Title.ext"
        
        Returns:
            bool indicating success
        """
        try:
            cursor = conn.cursor()
            
            # Find matching track in database
            from app import _is_postgres_connection as app_is_postgres_connection
            is_pg = bool(app_is_postgres_connection(conn))
            placeholder = "%s" if is_pg else "?"
            cursor.execute(f"""
                SELECT track_number, track_title, track_artist
                FROM musicbrainz_release_tracks
                WHERE release_id = {placeholder} AND (found_filename = {placeholder} OR file_path LIKE {placeholder})
            """, (release_id, file_path.name, f"%{file_path.name}%"))
            
            track = cursor.fetchone()
            if not track:
                logger.warning(f"[FINALIZER] No database match for file: {file_path.name}")
                # Try to extract artist, title, and track number from original filename
                import re
                filename_noext = file_path.stem
                extension = file_path.suffix
                
                # Try pattern: "Artist - NN - Title" or "Artist - Title (with track in parens)"
                # First try: extract track number from filename if it exists
                track_match = re.search(r'\b(\d{1,2})\s*-', filename_noext)
                track_number = track_match.group(1) if track_match else "00"
                
                # Try to extract artist and title by splitting on " - "
                parts = filename_noext.split(" - ")
                if len(parts) >= 2:
                    artist = parts[0].strip()
                    # Join the rest in case there are multiple " - " separators
                    title = " - ".join(parts[1:]).strip()
                else:
                    # Fallback if no " - " found
                    artist = "Unknown Artist"
                    title = filename_noext.strip()
                
                # Clean up title if it has a track number prefix
                title = re.sub(r'^\d{1,2}\s*-\s*', '', title).strip()
                
                # Format: "01. Artist - Title.ext"
                new_name = f"{int(track_number):02d}. {artist} - {title}{extension}"
            else:
                track_number = track['track_number']
                artist = str(track['track_artist']).strip()
                title = str(track['track_title']).strip()
                extension = file_path.suffix
                
                # Format: "01. Artist - Title.ext"
                new_name = f"{track_number:02d}. {artist} - {title}{extension}"
            
            destination = final_dir / new_name
            
            # Handle filename collisions
            if destination.exists():
                logger.warning(f"[FINALIZER] File already exists: {destination.name}, overwriting")
                destination.unlink()
            
            # Move file
            shutil.move(str(file_path), str(destination))
            logger.debug(f"[FINALIZER] Moved {file_path.name} → {destination.name}")
            
            # Update database with finalized info
            cursor.execute(f"""
                UPDATE musicbrainz_release_tracks
                SET status = 'finalized',
                    file_path = {placeholder},
                    updated_at = CURRENT_TIMESTAMP
                WHERE release_id = {placeholder} AND track_number = {placeholder}
            """, (str(destination), release_id, track_number))
            
            return True
            
        except Exception as e:
            logger.error(f"[FINALIZER] Error moving file {file_path.name}: {e}")
            return False

    def cleanup_monitoring_folder(self, folder_path):
        """
        Remove empty monitoring folder
        
        Tries to remove folder, silently fails if not empty (other files may remain)
        """
        try:
            # Try to remove folder if empty
            folder_path.rmdir()
            logger.info(f"[FINALIZER] Removed empty monitoring folder: {folder_path}")
            
        except FileNotFoundError:
            logger.warning(f"[FINALIZER] Monitoring folder already gone: {folder_path}")
        except OSError as e:
            # Folder not empty - that's okay, leave it
            logger.debug(f"[FINALIZER] Monitoring folder not empty (expected): {folder_path.name}")

    def get_finalization_progress(self, release_id):
        """
        Get progress info for a specific release
        
        Returns:
            dict with release info and progress
        """
        try:
            conn = self.get_db()
            cursor = conn.cursor()
            
            from app import _is_postgres_connection as app_is_postgres_connection
            is_pg = bool(app_is_postgres_connection(conn))
            placeholder = "%s" if is_pg else "?"
            
            cursor.execute(f"""
                SELECT id, release_title, artist, release_year,
                       total_tracks, discovered_count, status,
                       finalized_at
                FROM musicbrainz_releases
                WHERE release_id = {placeholder}
            """, (release_id,))
            
            release = cursor.fetchone()
            conn.close()
            
            if not release:
                return None
            
            is_ready = release['discovered_count'] >= release['total_tracks']
            
            return {
                'release_id': release_id,
                'title': release['release_title'],
                'artist': release['artist'],
                'year': release['release_year'],
                'total_tracks': release['total_tracks'],
                'discovered_count': release['discovered_count'],
                'status': release['status'],
                'ready_to_finalize': is_ready,
                'finalized_at': release['finalized_at']
            }
            
        except Exception as e:
            logger.error(f"[FINALIZER] Error getting progress: {e}")
            return None


def get_finalizer():
    """Factory function for getting finalizer instance"""
    return MusicBrainzFinalizer()


if __name__ == '__main__':
    finalizer = get_finalizer()
    result = finalizer.check_and_finalize_releases()
    print(f"[RESULT] {result}")
