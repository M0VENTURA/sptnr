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
from typing import Any
from api_clients.musicbrainz import _USER_AGENT as MUSICBRAINZ_USER_AGENT
from database_abstraction import DatabaseQuery, is_postgres_connection

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
        """Get active app database connection (PostgreSQL or SQLite fallback)."""
        try:
            from app import get_db as app_get_db
            return app_get_db()
        except Exception:
            conn = sqlite3.connect(DB_FILE, timeout=DB_TIMEOUT)
            conn.row_factory = sqlite3.Row
            return conn

    @staticmethod
    def _row_get(row, key, index=0, default=None):
        """Read value from dict/Row/tuple results safely."""
        if row is None:
            return default
        if isinstance(row, dict):
            return row.get(key, default)
        try:
            if hasattr(row, "keys") and key in row.keys():
                return row[key]
        except Exception:
            pass
        try:
            return row[index]
        except Exception:
            return default

    def ensure_schema(self):
        """Create required MusicBrainz release tables if they are missing."""
        conn = self.get_db()
        is_pg = is_postgres_connection(conn)
        db_query = DatabaseQuery(conn)

        try:
            if is_pg:
                db_query.execute("""
                    CREATE TABLE IF NOT EXISTS musicbrainz_releases (
                        id BIGSERIAL PRIMARY KEY,
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
                
                # Ensure sequence exists for id column (handles migration cases)
                try:
                    db_query.execute("""
                        CREATE SEQUENCE IF NOT EXISTS musicbrainz_releases_id_seq
                        AS BIGINT START WITH 1 INCREMENT BY 1
                    """)
                    db_query.execute("""
                        ALTER TABLE musicbrainz_releases 
                        ALTER COLUMN id SET DEFAULT nextval('musicbrainz_releases_id_seq')
                    """)
                    db_query.execute("""
                        SELECT setval('musicbrainz_releases_id_seq', 
                                     COALESCE((SELECT MAX(id) FROM musicbrainz_releases), 0) + 1)
                    """)
                except Exception as seq_error:
                    logger.debug(f"[SCHEMA] Note: Could not ensure musicbrainz_releases sequence (may already exist): {seq_error}")
                
                # Ensure download_queue sequence exists (required for foreign key from musicbrainz_release_tracks)
                try:
                    db_query.execute("""
                        CREATE SEQUENCE IF NOT EXISTS download_queue_id_seq
                        AS BIGINT START WITH 1 INCREMENT BY 1
                    """)
                    db_query.execute("""
                        ALTER TABLE download_queue 
                        ALTER COLUMN id SET DEFAULT nextval('download_queue_id_seq')
                    """)
                    db_query.execute("""
                        SELECT setval('download_queue_id_seq', 
                                     COALESCE((SELECT MAX(id) FROM download_queue), 0) + 1)
                    """)
                except Exception as seq_error:
                    logger.debug(f"[SCHEMA] Note: Could not ensure download_queue sequence (may already exist): {seq_error}")

                db_query.execute("""
                    CREATE TABLE IF NOT EXISTS musicbrainz_release_tracks (
                        id BIGSERIAL PRIMARY KEY,
                        release_id TEXT NOT NULL,
                        queue_id BIGINT,
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
                
                # Ensure musicbrainz_release_tracks sequence exists
                try:
                    db_query.execute("""
                        CREATE SEQUENCE IF NOT EXISTS musicbrainz_release_tracks_id_seq
                        AS BIGINT START WITH 1 INCREMENT BY 1
                    """)
                    db_query.execute("""
                        ALTER TABLE musicbrainz_release_tracks 
                        ALTER COLUMN id SET DEFAULT nextval('musicbrainz_release_tracks_id_seq')
                    """)
                    db_query.execute("""
                        SELECT setval('musicbrainz_release_tracks_id_seq', 
                                     COALESCE((SELECT MAX(id) FROM musicbrainz_release_tracks), 0) + 1)
                    """)
                except Exception as seq_error:
                    logger.debug(f"[SCHEMA] Note: Could not ensure musicbrainz_release_tracks sequence (may already exist): {seq_error}")
            else:
                db_query.execute("""
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

                db_query.execute("""
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

            db_query.execute("""
                CREATE INDEX IF NOT EXISTS idx_mb_releases_status
                ON musicbrainz_releases(status)
            """)
            db_query.execute("""
                CREATE INDEX IF NOT EXISTS idx_mb_releases_created
                ON musicbrainz_releases(created_at DESC)
            """)
            db_query.execute("""
                CREATE INDEX IF NOT EXISTS idx_mb_release_tracks_release
                ON musicbrainz_release_tracks(release_id)
            """)
            db_query.execute("""
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
        placeholder = "%s" if is_postgres_connection(conn) else "?"
        is_pg = is_postgres_connection(conn)
        
        try:
            # Check if release already exists
            cursor.execute(f"""
                SELECT id FROM musicbrainz_releases 
                WHERE release_id = {placeholder}
            """, (release_id,))
            
            existing = cursor.fetchone()
            if existing:
                release_db_id = self._row_get(existing, 'id', 0, 0)
                cursor.execute(f"""
                    UPDATE musicbrainz_releases
                    SET status = 'active', updated_at = CURRENT_TIMESTAMP
                    WHERE id = {placeholder}
                """, (release_db_id,))
                logger.info(f"[RELEASE_ENTRY] Updated existing release entry {release_db_id}")
            else:
                if is_pg:
                    # PostgreSQL: Use RETURNING to get the auto-generated ID
                    try:
                        cursor.execute(f"""
                            INSERT INTO musicbrainz_releases
                            (release_id, release_title, artist, release_year, total_tracks,
                             monitoring_folder_path, status, method, created_at, updated_at)
                            VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, 'active', {placeholder}, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                            RETURNING id
                        """, (release_id, release_title, artist, release_year, total_tracks,
                              str(monitoring_folder_path), method))
                        inserted = cursor.fetchone()
                        if inserted:
                            release_db_id = inserted[0] if isinstance(inserted, tuple) else self._row_get(inserted, 'id', 0, None)
                        else:
                            raise ValueError("Failed to retrieve inserted release ID from PostgreSQL RETURNING clause")
                    except Exception as pg_error:
                        # Fallback: PostgreSQL RETURNING failed, try alternative approach
                        # Check if it's due to missing id sequence and try to get the max id
                        if 'null' in str(pg_error).lower() and 'id' in str(pg_error).lower():
                            logger.warning(f"[RELEASE_ENTRY] PostgreSQL RETURNING failed: {pg_error}, attempting fallback")
                            # Re-insert without error handling this time to see if it succeeds
                            cursor.execute(f"""
                                SELECT COALESCE(MAX(id), 0) + 1 FROM musicbrainz_releases
                            """)
                            next_id_result = cursor.fetchone()
                            next_id = next_id_result[0] if next_id_result else 1
                            
                            cursor.execute(f"""
                                INSERT INTO musicbrainz_releases
                                (id, release_id, release_title, artist, release_year, total_tracks,
                                 monitoring_folder_path, status, method, created_at, updated_at)
                                VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, 'active', {placeholder}, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                                RETURNING id
                            """, (next_id, release_id, release_title, artist, release_year, total_tracks,
                                  str(monitoring_folder_path), method))
                            inserted = cursor.fetchone()
                            release_db_id = inserted[0] if inserted else next_id
                        else:
                            raise
                else:
                    # SQLite: Use lastrowid to get the auto-generated ID
                    cursor.execute("""
                        INSERT INTO musicbrainz_releases
                        (release_id, release_title, artist, release_year, total_tracks,
                         monitoring_folder_path, status, method, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, 'active', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """, (release_id, release_title, artist, release_year, total_tracks,
                          str(monitoring_folder_path), method))
                    release_db_id = cursor.lastrowid
                
                if not release_db_id:
                    raise ValueError(f"Failed to get ID for new release: {release_id}")
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
        is_pg = is_postgres_connection(conn)
        placeholder = "%s" if is_pg else "?"
        
        try:
            # Get release database ID
            cursor.execute(f"SELECT id FROM musicbrainz_releases WHERE release_id = {placeholder}", (release_id,))
            release_row = cursor.fetchone()
            if not release_row:
                logger.error(f"[QUEUE_ADD] Release entry not found for {release_id}")
                return []
            
            mb_release_db_id = self._row_get(release_row, 'id', 0, 0)
            
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
                        
                        # Create search query (artist - title format, no album)
                        search_query = f"{track_artist} - {track_title}".strip()
                        
                        # Add to download_queue
                        if is_pg:
                            cursor.execute(f"""
                                INSERT INTO download_queue
                                (artist, album, title, search_query, source, status,
                                 release_id, track_number, mb_release_download_id,
                                 created_at, updated_at)
                                VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, 'soulseek', 'queued', {placeholder}, {placeholder}, {placeholder},
                                       CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                                RETURNING id
                            """, (track_artist, album, track_title, search_query,
                                  release_id, track_number, mb_release_db_id))
                            queue_row = cursor.fetchone()
                            queue_id = self._row_get(queue_row, 'id', 0, 0)
                        else:
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
                        cursor.execute(f"""
                            INSERT INTO musicbrainz_release_tracks
                            (release_id, queue_id, track_number, track_title, 
                             track_artist, duration, isrc, status, 
                             created_at, updated_at)
                            VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, 'queued', 
                                   CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        """, (release_id, queue_id, track_number, track_title,
                              track_artist, duration, isrc))
                        
                        # Also add to tracks table with 'downloading' status
                        # This allows the track to appear on artist/album pages as "Downloading"
                        track_id = f"{track_artist}|{album}|{track_title}"
                        if is_pg:
                            cursor.execute(f"""
                                INSERT INTO tracks
                                (id, artist, album, title, track_number, duration, isrc,
                                 download_status, created_at, updated_at)
                                VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, 'downloading',
                                       CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                                ON CONFLICT (id) DO UPDATE SET
                                    artist = EXCLUDED.artist,
                                    album = EXCLUDED.album,
                                    title = EXCLUDED.title,
                                    track_number = EXCLUDED.track_number,
                                    duration = EXCLUDED.duration,
                                    isrc = EXCLUDED.isrc,
                                    download_status = EXCLUDED.download_status,
                                    updated_at = CURRENT_TIMESTAMP
                            """, (track_id, track_artist, album, track_title,
                                  track_number, duration, isrc))
                        else:
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
                total_tracks = self._row_get(row, 'total_tracks', 5, 0) or 0
                discovered_count = self._row_get(row, 'discovered_count', 6, 0) or 0
                releases.append({
                    "id": self._row_get(row, 'id', 0, None),
                    "release_id": self._row_get(row, 'release_id', 1, None),
                    "release_title": self._row_get(row, 'release_title', 2, None),
                    "artist": self._row_get(row, 'artist', 3, None),
                    "release_year": self._row_get(row, 'release_year', 4, None),
                    "total_tracks": total_tracks,
                    "discovered_count": discovered_count,
                    "organized_count": self._row_get(row, 'organized_count', 7, 0),
                    "finalized_count": self._row_get(row, 'finalized_count', 8, 0),
                    "status": self._row_get(row, 'status', 9, None),
                    "monitoring_folder": self._row_get(row, 'monitoring_folder_path', 10, None),
                    "created_at": self._row_get(row, 'created_at', 11, None),
                    "updated_at": self._row_get(row, 'updated_at', 12, None),
                    "progress_percent": int((discovered_count / total_tracks * 100) if total_tracks > 0 else 0)
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
        placeholder = "%s" if is_postgres_connection(conn) else "?"
        
        try:
            cursor.execute(f"""
                SELECT id, track_number, track_title, track_artist, 
                       status, file_path, found_filename
                FROM musicbrainz_release_tracks
                WHERE release_id = {placeholder}
                ORDER BY track_number
            """, (release_id,))
            
            tracks = []
            for row in cursor.fetchall():
                tracks.append({
                    "id": self._row_get(row, 'id', 0, None),
                    "track_number": self._row_get(row, 'track_number', 1, None),
                    "title": self._row_get(row, 'track_title', 2, None),
                    "artist": self._row_get(row, 'track_artist', 3, None),
                    "status": self._row_get(row, 'status', 4, None),
                    "file_path": self._row_get(row, 'file_path', 5, None),
                    "found_filename": self._row_get(row, 'found_filename', 6, None)
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
