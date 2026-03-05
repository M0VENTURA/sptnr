#!/usr/bin/env python3
"""
MusicBrainz Release Download Manager

Handles the complete flow of downloading entire releases track-by-track:
1. Fetch release details from MusicBrainz
2. Create monitoring folders
3. Add individual tracks to download queue
4. Track file discovery and movement
5. Finalize releases when complete
"""

import sqlite3
import requests
import os
import json
import logging
from datetime import datetime
from contextlib import closing
from pathlib import Path
from api_clients.musicbrainz import _USER_AGENT as MUSICBRAINZ_USER_AGENT

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DOWNLOADS_MUSIC_DIR = "/downloads/Music"
MUSIC_LIBRARY_DIR = "/music"
MB_API_URL = "https://musicbrainz.org/ws/2"
DB_FILE = "sptnr.db"
DB_TIMEOUT = 120.0


class MusicBrainzReleaseManager:
    """Manages the complete MusicBrainz release download workflow"""

    def __init__(self):
        self.downloads_dir = Path(DOWNLOADS_MUSIC_DIR)
        self.music_dir = Path(MUSIC_LIBRARY_DIR)
        self.ensure_directories()
        self.ensure_schema()

    def ensure_directories(self):
        """Create all required directories"""
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.music_dir.mkdir(parents=True, exist_ok=True)

    def get_db(self):
        """Get database connection"""
        return sqlite3.connect(DB_FILE, timeout=DB_TIMEOUT)

    def ensure_schema(self):
        """Create required MusicBrainz release tables if they are missing."""
        conn = self.get_db()
        cursor = conn.cursor()

        try:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS musicbrainz_releases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    release_id TEXT NOT NULL UNIQUE,
                    release_title TEXT NOT NULL,
                    artist TEXT NOT NULL,
                    release_year INTEGER,
                    total_tracks INTEGER,
                    monitoring_folder_path TEXT,
                    final_folder_path TEXT,
                    status TEXT DEFAULT 'active',
                    method TEXT,
                    discovered_count INTEGER DEFAULT 0,
                    organized_count INTEGER DEFAULT 0,
                    finalized_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    finalized_at TIMESTAMP
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS musicbrainz_release_tracks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    release_id TEXT NOT NULL,
                    queue_id INTEGER,
                    track_number INTEGER,
                    track_title TEXT,
                    track_artist TEXT,
                    duration INTEGER,
                    isrc TEXT,
                    found_filename TEXT,
                    file_path TEXT,
                    status TEXT DEFAULT 'queued',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (release_id) REFERENCES musicbrainz_releases(release_id),
                    FOREIGN KEY (queue_id) REFERENCES download_queue(id)
                )
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_mb_releases_status
                ON musicbrainz_releases(status)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_mb_releases_created
                ON musicbrainz_releases(created_at DESC)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_mb_release_tracks_release
                ON musicbrainz_release_tracks(release_id)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_mb_release_tracks_status
                ON musicbrainz_release_tracks(release_id, status)
            """)

            conn.commit()
        except Exception as e:
            logger.error(f"[SCHEMA] Error ensuring MusicBrainz release schema: {e}")
            raise
        finally:
            conn.close()

    def fetch_release_from_musicbrainz(self, release_id):
        """
        Fetch release details from MusicBrainz API
        
        Returns:
            dict with release info including tracks
        """
        try:
            headers = {"User-Agent": MUSICBRAINZ_USER_AGENT}
            url = f"{MB_API_URL}/release/{release_id}"
            params = {
                "fmt": "json",
                "inc": "recordings"
            }
            
            response = requests.get(url, headers=headers, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            logger.info(f"[MB_FETCH] Retrieved release: {data.get('title')} by {data.get('artist-credit', [{}])[0].get('name', 'Unknown')}")
            return data
            
        except Exception as e:
            logger.error(f"[MB_FETCH] Error fetching release {release_id}: {e}")
            raise

    def create_monitoring_folder(self, artist, album, year):
        """
        Create monitoring folder with format: YEAR - ARTIST - ALBUM
        
        Returns:
            Path object for the folder
        """
        folder_name = f"{year} - {artist} - {album}".replace('/', '_').replace('\\', '_')[:200]
        folder_path = self.downloads_dir / folder_name
        folder_path.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"[MONITOR_FOLDER] Created: {folder_path}")
        return folder_path

    def create_release_entry(self, release_id, release_title, artist, release_year, 
                            total_tracks, monitoring_folder_path, method='slskd'):
        """
        Create or update release entry in database
        
        Returns:
            int: Database ID of the release entry
        """
        conn = self.get_db()
        cursor = conn.cursor()
        
        try:
            # Check if release already exists
            cursor.execute("""
                SELECT id FROM musicbrainz_releases 
                WHERE release_id = ?
            """, (release_id,))
            
            existing = cursor.fetchone()
            if existing:
                release_db_id = existing[0]
                cursor.execute("""
                    UPDATE musicbrainz_releases
                    SET status = 'active', updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (release_db_id,))
                logger.info(f"[RELEASE_ENTRY] Updated existing release entry {release_db_id}")
            else:
                cursor.execute("""
                    INSERT INTO musicbrainz_releases
                    (release_id, release_title, artist, release_year, total_tracks,
                     monitoring_folder_path, status, method, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'active', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """, (release_id, release_title, artist, release_year, total_tracks,
                      str(monitoring_folder_path), method))
                
                release_db_id = cursor.lastrowid
                logger.info(f"[RELEASE_ENTRY] Created new release entry {release_db_id}")
            
            conn.commit()
            return release_db_id
            
        except Exception as e:
            logger.error(f"[RELEASE_ENTRY] Error creating release: {e}")
            raise
        finally:
            conn.close()

    def add_release_tracks_to_queue(self, release_id, mb_release_data, artist, album, album_artist=None):
        """
        Add all tracks from a release to the download queue
        
        Args:
            release_id: MusicBrainz release ID
            mb_release_data: Release data from MusicBrainz API
            artist: Artist name for search queries
            album: Album name for queue items
            album_artist: Potentially different artist for file organization
        
        Returns:
            list of queue item IDs created
        """
        queue_ids = []
        conn = self.get_db()
        cursor = conn.cursor()
        
        try:
            # Get release database ID
            cursor.execute("SELECT id FROM musicbrainz_releases WHERE release_id = ?", (release_id,))
            release_row = cursor.fetchone()
            if not release_row:
                logger.error(f"[QUEUE_ADD] Release entry not found for {release_id}")
                return []
            
            mb_release_db_id = release_row[0]
            
            releases = mb_release_data.get('releases', [])
            if not releases:
                releases = [mb_release_data]
            
            for release in releases:
                media = release.get('media', [])
                track_count = 0
                
                for medium in media:
                    tracks = medium.get('tracks', [])
                    
                    for track in tracks:
                        track_count += 1
                        recording = track.get('recording', {})
                        
                        track_number = track.get('number', track_count)
                        track_title = recording.get('title', 'Unknown Track')
                        track_artist = artist  # Use main artist for search
                        duration = recording.get('length', 0) // 1000 if recording.get('length') else 0  # Convert ms to seconds
                        isrc = None
                        
                        # Try to get ISRC from isrcs
                        isrcs = recording.get('isrcs', [])
                        if isrcs:
                            isrc = isrcs[0]
                        
                        # Create search query
                        search_query = f"{track_artist} {track_title}".strip()
                        
                        # Add to download_queue
                        cursor.execute("""
                            INSERT INTO download_queue
                            (artist, album, title, search_query, source, status, 
                             release_id, track_number, mb_release_download_id, 
                             created_at, updated_at)
                            VALUES (?, ?, ?, ?, 'soulseek', 'queued', ?, ?, ?, 
                                   CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        """, (track_artist, album, track_title, search_query, 
                              release_id, track_number, mb_release_db_id))
                        
                        queue_id = cursor.lastrowid
                        queue_ids.append(queue_id)
                        
                        # Add to musicbrainz_release_tracks
                        cursor.execute("""
                            INSERT INTO musicbrainz_release_tracks
                            (release_id, queue_id, track_number, track_title, 
                             track_artist, duration, isrc, status, 
                             created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', 
                                   CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        """, (release_id, queue_id, track_number, track_title,
                              track_artist, duration, isrc))
                        
                        # Also add to tracks table with 'downloading' status
                        # This allows the track to appear on artist/album pages as "Downloading"
                        track_id = f"{track_artist}|{album}|{track_title}"
                        cursor.execute("""
                            INSERT OR REPLACE INTO tracks
                            (id, artist, album, title, track_number, duration, isrc, 
                             download_status, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, 'downloading', 
                                   CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        """, (track_id, track_artist, album, track_title, 
                              track_number, duration, isrc))
                        
                        logger.info(f"[QUEUE_ADD] Added track {track_number}: {track_title} (Queue ID: {queue_id})")
                
                logger.info(f"[QUEUE_ADD] Added {len(queue_ids)} tracks to queue for release {release_id}")
            
            conn.commit()
            return queue_ids
            
        except Exception as e:
            logger.error(f"[QUEUE_ADD] Error adding tracks to queue: {e}")
            raise
        finally:
            conn.close()

    def start_release_download(self, release_id, release_title, artist, method='slskd'):
        """
        Start downloading a complete release
        
        Process:
        1. Fetch release data from MusicBrainz
        2. Create monitoring folder
        3. Create release entry
        4. Add tracks to queue
        
        Returns:
            dict with release info and queue items created
        """
        try:
            logger.info(f"[START_DOWNLOAD] Starting download for release {release_id}")
            
            # Fetch release data
            mb_data = self.fetch_release_from_musicbrainz(release_id)
            
            # Extract release year
            release_year = None
            if 'releases' in mb_data and mb_data['releases']:
                date_str = mb_data['releases'][0].get('first-release-date', '')
                release_year = int(date_str.split('-')[0]) if date_str else None
            
            if not release_year:
                release_year = datetime.now().year
            
            # Create monitoring folder
            monitoring_folder = self.create_monitoring_folder(artist, release_title, release_year)
            
            # Get total tracks
            total_tracks = 0
            if 'releases' in mb_data:
                for release in mb_data['releases']:
                    for medium in release.get('media', []):
                        total_tracks += len(medium.get('tracks', []))
            
            # Create release entry
            mb_release_db_id = self.create_release_entry(
                release_id, release_title, artist, release_year, 
                total_tracks, monitoring_folder, method
            )
            
            # Add tracks to queue
            queue_ids = self.add_release_tracks_to_queue(
                release_id, mb_data, artist, release_title
            )
            
            return {
                "success": True,
                "mb_release_db_id": mb_release_db_id,
                "release_id": release_id,
                "release_title": release_title,
                "artist": artist,
                "release_year": release_year,
                "total_tracks": total_tracks,
                "queue_items_created": len(queue_ids),
                "queue_ids": queue_ids,
                "monitoring_folder": str(monitoring_folder)
            }
            
        except Exception as e:
            logger.error(f"[START_DOWNLOAD] Failed to start download: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    def get_active_releases(self):
        """
        Get all active releases with progress information
        
        Returns:
            list of release dictionaries with current progress
        """
        conn = self.get_db()
        cursor = conn.cursor()
        
        try:
            cursor.execute("""
                SELECT id, release_id, release_title, artist, release_year, 
                       total_tracks, discovered_count, organized_count, 
                       finalized_count, status, monitoring_folder_path, 
                       created_at, updated_at
                FROM musicbrainz_releases
                WHERE status IN ('active', 'finalizing')
                ORDER BY created_at DESC
            """)
            
            releases = []
            for row in cursor.fetchall():
                releases.append({
                    "id": row[0],
                    "release_id": row[1],
                    "release_title": row[2],
                    "artist": row[3],
                    "release_year": row[4],
                    "total_tracks": row[5],
                    "discovered_count": row[6],
                    "organized_count": row[7],
                    "finalized_count": row[8],
                    "status": row[9],
                    "monitoring_folder": row[10],
                    "created_at": row[11],
                    "updated_at": row[12],
                    "progress_percent": int((row[6] / row[5] * 100) if row[5] > 0 else 0)
                })
            
            return releases
            
        except Exception as e:
            logger.error(f"[GET_ACTIVE] Error fetching active releases: {e}")
            return []
        finally:
            conn.close()

    def get_release_tracks(self, release_id):
        """
        Get all tracks for a release with current status
        
        Returns:
            list of track dictionaries
        """
        conn = self.get_db()
        cursor = conn.cursor()
        
        try:
            cursor.execute("""
                SELECT id, track_number, track_title, track_artist, 
                       status, file_path, found_filename
                FROM musicbrainz_release_tracks
                WHERE release_id = ?
                ORDER BY track_number
            """, (release_id,))
            
            tracks = []
            for row in cursor.fetchall():
                tracks.append({
                    "id": row[0],
                    "track_number": row[1],
                    "title": row[2],
                    "artist": row[3],
                    "status": row[4],
                    "file_path": row[5],
                    "found_filename": row[6]
                })
            
            return tracks
            
        except Exception as e:
            logger.error(f"[GET_TRACKS] Error fetching tracks: {e}")
            return []
        finally:
            conn.close()


# Singleton instance
_manager = None

def get_manager():
    """Get or create the MusicBrainz release manager"""
    global _manager
    if _manager is None:
        _manager = MusicBrainzReleaseManager()
    return _manager
