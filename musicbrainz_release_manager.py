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

import requests
import os
import json
import logging
import re
import threading
from contextlib import closing
from pathlib import Path
from typing import Any
from api_clients.musicbrainz import _USER_AGENT as MUSICBRAINZ_USER_AGENT
from database_abstraction import DatabaseQuery, is_postgres_connection
from helpers.db_utils import get_db_connection
from queue_status_constants import ACTIVE_QUEUE_STATUS_SQL as _ACTIVE_QUEUE_STATUS_SQL
try:
    import psycopg2
except ImportError:
    psycopg2 = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DOWNLOADS_MUSIC_DIR = "/downloads/Music"


def _build_artist_credit_string(artist_credit):
    """Build a display string from a MusicBrainz artist-credit array."""
    result = ''
    for credit in artist_credit:
        if isinstance(credit, dict):
            result += credit.get('name', '')
            result += credit.get('joinphrase', '')
        else:
            result += str(credit)
    return result.strip()


def _coerce_position_to_int(value, default):
    """Convert MusicBrainz position strings (e.g. 'A1', '1/12') into an integer."""
    raw = str(value or '').strip()
    if not raw:
        return default
    if raw.isdigit():
        return int(raw)
    match = re.search(r"\d+", raw)
    if match:
        return int(match.group(0))
    return default


MUSIC_LIBRARY_DIR = "/music"
MB_API_URL = "https://musicbrainz.org/ws/2"
DB_FILE = "sptnr.db"
DB_TIMEOUT = 120.0


class MusicBrainzReleaseManager:
    """Manages the complete MusicBrainz release download workflow"""

    _schema_initialized = False
    _schema_lock = threading.Lock()

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
        """Get database connection from shared backend helper."""
        return get_db_connection()

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
        if MusicBrainzReleaseManager._schema_initialized:
            return

        with MusicBrainzReleaseManager._schema_lock:
            if MusicBrainzReleaseManager._schema_initialized:
                return

            conn = self.get_db()
            db_query = DatabaseQuery(conn)

            try:
                if is_postgres_connection(conn):
                    db_query.execute("SELECT pg_advisory_lock(hashtext(%s))", ("sptnr_mb_schema_init_v1",))

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

                db_query.execute("""
                    CREATE TABLE IF NOT EXISTS musicbrainz_release_tracks (
                        id BIGSERIAL PRIMARY KEY,
                        release_id TEXT NOT NULL,
                        queue_id BIGINT,
                        disc_number INTEGER,
                        track_number INTEGER,
                        track_title TEXT,
                        track_artist TEXT,
                        duration INTEGER,
                        isrc TEXT,
                        recording_title TEXT,
                        recording_mbid TEXT,
                        found_filename TEXT,
                        file_path TEXT,
                        status TEXT DEFAULT 'queued',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (release_id) REFERENCES musicbrainz_releases(release_id),
                        FOREIGN KEY (queue_id) REFERENCES download_queue(id)
                    )
                """)

                # Ensure release_year column exists on pre-existing musicbrainz_releases
                # tables that were created before the column was added to the schema.
                try:
                    db_query.execute("""
                        ALTER TABLE musicbrainz_releases
                        ADD COLUMN IF NOT EXISTS release_year INTEGER
                    """)
                except Exception as alter_err:
                    logger.debug(f"[SCHEMA] musicbrainz_releases.release_year column add: {alter_err}")

                # Ensure disc_number column exists on pre-existing tables that were
                # created before the column was added to the schema definition.
                try:
                    db_query.execute("""
                        ALTER TABLE musicbrainz_release_tracks
                        ADD COLUMN IF NOT EXISTS disc_number INTEGER
                    """)
                except Exception as alter_err:
                    logger.debug(f"[SCHEMA] disc_number column check: {alter_err}")

                # Ensure recording_title and recording_mbid columns exist on
                # pre-existing tables created before these columns were added.
                try:
                    db_query.execute("""
                        ALTER TABLE musicbrainz_release_tracks
                        ADD COLUMN IF NOT EXISTS recording_title TEXT
                    """)
                except Exception as alter_err:
                    logger.debug(f"[SCHEMA] recording_title column check: {alter_err}")

                try:
                    db_query.execute("""
                        ALTER TABLE musicbrainz_release_tracks
                        ADD COLUMN IF NOT EXISTS recording_mbid TEXT
                    """)
                except Exception as alter_err:
                    logger.debug(f"[SCHEMA] recording_mbid column check: {alter_err}")

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
                MusicBrainzReleaseManager._schema_initialized = True
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                logger.error(f"[SCHEMA] Error ensuring MusicBrainz release schema: {e}")
                raise
            finally:
                try:
                    if is_postgres_connection(conn):
                        db_query.execute("SELECT pg_advisory_unlock(hashtext(%s))", ("sptnr_mb_schema_init_v1",))
                except Exception:
                    pass
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
                "inc": "recordings+artist-credits"
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
        year_text = str(year).strip() if year not in (None, '') else ''
        year_match = re.search(r"(19|20)\d{2}", year_text)
        folder_year = year_match.group(0) if year_match else 'Unknown'
        folder_name = f"{folder_year} - {artist} - {album}".replace('/', '_').replace('\\', '_')[:200]
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
        placeholder = "%s"
        
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
                    if 'null' in str(pg_error).lower() and 'id' in str(pg_error).lower():
                        logger.warning(f"[RELEASE_ENTRY] PostgreSQL RETURNING failed: {pg_error}, attempting fallback")
                        cursor.execute("""
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

    def add_release_tracks_to_queue(self, release_id, mb_release_data, artist, album, album_artist=None, queue_source='soulseek'):
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
        placeholder = "%s"
        
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
            
            normalized_queue_source = (queue_source or 'soulseek').strip().lower()
            if normalized_queue_source == 'slskd':
                normalized_queue_source = 'soulseek'
            if normalized_queue_source not in ('soulseek', 'qbittorrent'):
                normalized_queue_source = 'soulseek'

            for release in releases:
                # Extract release year from date field (format: YYYY-MM-DD or YYYY)
                release_date = release.get('date', '')
                release_year = None
                if release_date:
                    try:
                        release_year = release_date[:4]  # Extract first 4 characters (year)
                    except Exception:
                        release_year = None
                
                # Derive release-level album artist from artist-credit
                rel_credits = release.get('artist-credit', [])
                if rel_credits:
                    rel_album_artist = _build_artist_credit_string(rel_credits) or album_artist or artist
                else:
                    rel_album_artist = album_artist or artist

                media = release.get('media', [])
                track_count = 0
                
                for medium in media:
                    tracks = medium.get('tracks', [])
                    disc_number = _coerce_position_to_int(medium.get('position', 1), 1)
                    
                    for track in tracks:
                        track_count += 1
                        recording = track.get('recording', {})
                        recording_mbid = recording.get('id')
                        
                        raw_track_number = track.get('number') or track.get('position') or track_count
                        track_number = _coerce_position_to_int(raw_track_number, track_count)
                        track_title = recording.get('title', 'Unknown Track')
                        # Use per-track artist from recording's artist-credit when available;
                        # fall back to the album artist for non-VA releases.
                        rec_credits = recording.get('artist-credit') or track.get('artist-credit') or []
                        if rec_credits:
                            track_artist = _build_artist_credit_string(rec_credits) or artist
                        else:
                            track_artist = artist
                        # MusicBrainz returns duration in milliseconds.
                        # musicbrainz_release_tracks.duration stores ms (consistent with
                        # post_download_processor / folder_matching_enhancements caching).
                        # tracks.duration stores seconds (consistent with mp3scanner).
                        duration_ms = recording.get('length') or track.get('length') or 0
                        duration_sec = duration_ms // 1000 if duration_ms else 0
                        isrc = None
                        
                        # Try to get ISRC from isrcs
                        isrcs = recording.get('isrcs', [])
                        if isrcs:
                            isrc = isrcs[0]
                        
                        # Create search query (artist - title format, no album)
                        search_query = f"{track_artist} - {track_title}".strip()

                        # Duplicate check: skip insert if an active queue entry already
                        # exists for the same (artist, album, title, source) combination.
                        # This mirrors the pre-check in add_to_queue() and prevents
                        # "duplicate key violates uq_download_queue_active_identity" errors
                        # when a release is re-queued while some tracks are still active.
                        cursor.execute(
                            f"""
                            SELECT id FROM download_queue
                            WHERE LOWER(artist) = LOWER({placeholder})
                              AND LOWER(COALESCE(album, '')) = LOWER(COALESCE({placeholder}, ''))
                              AND LOWER(title) = LOWER({placeholder})
                              AND source = {placeholder}
                              AND status IN ({_ACTIVE_QUEUE_STATUS_SQL})
                            ORDER BY created_at ASC
                            LIMIT 1
                            """,
                            (track_artist, album, track_title, normalized_queue_source),
                        )
                        existing_row = cursor.fetchone()
                        if existing_row:
                            queue_id = self._row_get(existing_row, 'id', 0, 0)
                            queue_ids.append(queue_id)
                            logger.info(
                                f"[QUEUE_ADD] Duplicate skipped (track already active): "
                                f"{track_artist} - {track_title} (Queue ID: {queue_id})"
                            )
                            continue

                        # Add to download_queue with release year, album_artist, and MB IDs
                        try:
                            cursor.execute(f"""
                                INSERT INTO download_queue
                                (artist, album, title, search_query, source, status,
                                 release_id, track_number, disc_number, mb_release_download_id,
                                 year, album_artist, recording_mbid,
                                 created_at, updated_at)
                                VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, 'queued',
                                        {placeholder}, {placeholder}, {placeholder}, {placeholder},
                                        {placeholder}, {placeholder}, {placeholder},
                                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                                RETURNING id
                            """, (track_artist, album, track_title, search_query, normalized_queue_source,
                                  release_id, track_number, disc_number, mb_release_db_id,
                                  release_year, rel_album_artist, recording_mbid))
                            queue_row = cursor.fetchone()
                            queue_id = self._row_get(queue_row, 'id', 0, 0)
                        except Exception as insert_err:
                            # Handle concurrent duplicate-key race condition: another worker
                            # inserted the same track between our pre-check and this INSERT.
                            # Roll back the failed statement and look up the winning row.
                            is_integrity_error = (
                                psycopg2 is not None and isinstance(insert_err, psycopg2.IntegrityError)
                            ) or "duplicate key" in str(insert_err).lower()
                            if is_integrity_error:
                                logger.warning(
                                    f"[QUEUE_ADD] Duplicate key race on insert for "
                                    f"{track_artist!r} - {track_title!r}: {insert_err}"
                                )
                                try:
                                    conn.rollback()
                                except Exception as rb_err:
                                    logger.warning(f"[QUEUE_ADD] Rollback failed after duplicate key: {rb_err}")
                                cursor.execute(
                                    f"""
                                    SELECT id FROM download_queue
                                    WHERE LOWER(artist) = LOWER({placeholder})
                                      AND LOWER(COALESCE(album, '')) = LOWER(COALESCE({placeholder}, ''))
                                      AND LOWER(title) = LOWER({placeholder})
                                      AND source = {placeholder}
                                      AND status IN ({_ACTIVE_QUEUE_STATUS_SQL})
                                    ORDER BY created_at ASC
                                    LIMIT 1
                                    """,
                                    (track_artist, album, track_title, normalized_queue_source),
                                )
                                fallback_row = cursor.fetchone()
                                if fallback_row:
                                    queue_id = self._row_get(fallback_row, 'id', 0, 0)
                                else:
                                    logger.warning(
                                        f"[QUEUE_ADD] Could not resolve duplicate-key race for "
                                        f"{track_artist!r} - {track_title!r}: no active row found after rollback"
                                    )
                                    continue
                            else:
                                raise
                        queue_ids.append(queue_id)
                        
                        # Add to musicbrainz_release_tracks (include disc_number so
                        # post-processing can determine disc count from the local DB
                        # without making an external MusicBrainz API call)
                        cursor.execute(f"""
                            INSERT INTO musicbrainz_release_tracks
                            (release_id, queue_id, disc_number, track_number, track_title, 
                             track_artist, duration, isrc, status, 
                             created_at, updated_at)
                            VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, 'queued', 
                                   CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        """, (release_id, queue_id, disc_number, track_number, track_title,
                              track_artist, duration_ms, isrc))
                        
                        # Also add to tracks table with 'downloading' status
                        # This allows the track to appear on artist/album pages as "Downloading"
                        track_id = f"{track_artist}|{album}|{track_title}"
                        cursor.execute(f"""
                            INSERT INTO tracks
                            (id, artist, album, title, track_number, duration, isrc,
                             download_status)
                            VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, 'downloading')
                            ON CONFLICT (id) DO UPDATE SET
                                artist = EXCLUDED.artist,
                                album = EXCLUDED.album,
                                title = EXCLUDED.title,
                                track_number = EXCLUDED.track_number,
                                duration = EXCLUDED.duration,
                                isrc = EXCLUDED.isrc,
                                download_status = EXCLUDED.download_status
                        """, (track_id, track_artist, album, track_title,
                              track_number, duration_sec, isrc))
                        
                        logger.info(f"[QUEUE_ADD] Added track {track_number}: {track_title} (Queue ID: {queue_id})")
                
                logger.info(
                    f"[QUEUE_ADD] Added {len(queue_ids)} tracks to queue for release {release_id} "
                    f"(source={normalized_queue_source})"
                )
            
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
            
            # Extract release year from MusicBrainz payload.
            # For /ws/2/release/{id}, the canonical field is top-level `date`.
            release_year = None
            date_candidates = []

            top_level_date = mb_data.get('date')
            if top_level_date:
                date_candidates.append(str(top_level_date))

            nested_releases = mb_data.get('releases') if isinstance(mb_data.get('releases'), list) else []
            for rel in nested_releases:
                if not isinstance(rel, dict):
                    continue
                rel_date = rel.get('date') or rel.get('first-release-date')
                if rel_date:
                    date_candidates.append(str(rel_date))

            for candidate in date_candidates:
                match = re.search(r"(19|20)\d{2}", candidate)
                if match:
                    release_year = int(match.group(0))
                    break
            
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
            
            # Derive album artist from release artist-credit (e.g. "Various Artists" for compilations)
            release_album_artist = artist
            if 'releases' in mb_data and mb_data['releases']:
                rel_credits = mb_data['releases'][0].get('artist-credit', [])
                if rel_credits:
                    release_album_artist = _build_artist_credit_string(rel_credits) or artist

            # Add tracks to queue
            queue_source = 'soulseek' if str(method).strip().lower() == 'slskd' else 'qbittorrent'

            queue_ids = self.add_release_tracks_to_queue(
                release_id, mb_data, artist, release_title,
                album_artist=release_album_artist,
                queue_source=queue_source,
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
        placeholder = "%s"
        
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
