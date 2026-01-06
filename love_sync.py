#!/usr/bin/env python3
"""
Love Sync Module - Synchronize loved/starred tracks between Navidrome and ListenBrainz.

Features:
- Import starred tracks from Navidrome for each user
- Sync love status to ListenBrainz when user loves a track
- Import loved tracks from ListenBrainz feedback API
- Bi-directional sync between platforms
"""

import os
import sqlite3
import logging
from datetime import datetime
from typing import Optional, List, Dict
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")


class LoveSyncManager:
    """Manage love/star status across Navidrome and ListenBrainz."""
    
    def __init__(self, db_path: str = DB_PATH):
        """
        Initialize LoveSyncManager.
        
        Args:
            db_path: Path to sptnr database
        """
        self.db_path = db_path
    
    def get_db_connection(self):
        """Get database connection."""
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn
    
    def sync_navidrome_starred_tracks(self, user_id: int, starred_track_ids: List[str]) -> int:
        """
        Sync starred tracks from Navidrome for a specific user.
        
        Args:
            user_id: User ID from navidrome_users table
            starred_track_ids: List of Navidrome track IDs that are starred
            
        Returns:
            Number of tracks updated
        """
        conn = self.get_db_connection()
        cursor = conn.cursor()
        
        try:
            # First, unstar all tracks for this user (full sync approach)
            cursor.execute("""
                UPDATE user_loved_tracks
                SET is_loved = 0,
                    loved_at = NULL
                WHERE user_id = ?
            """, (user_id,))
            
            # Now mark starred tracks as loved
            updated_count = 0
            for track_id in starred_track_ids:
                cursor.execute("""
                    INSERT INTO user_loved_tracks (user_id, track_id, is_loved, loved_at)
                    VALUES (?, ?, 1, ?)
                    ON CONFLICT(user_id, track_id) DO UPDATE SET
                        is_loved = 1,
                        loved_at = ?
                """, (user_id, track_id, datetime.now().isoformat(), datetime.now().isoformat()))
                updated_count += 1
            
            conn.commit()
            logger.info(f"Synced {updated_count} starred tracks for user {user_id}")
            return updated_count
            
        except Exception as e:
            logger.error(f"Failed to sync Navidrome starred tracks: {e}")
            conn.rollback()
            return 0
        finally:
            conn.close()
    
    def sync_navidrome_starred_albums(self, user_id: int, starred_albums: List[Dict]) -> int:
        """
        Sync starred albums from Navidrome for a specific user.
        
        Args:
            user_id: User ID from navidrome_users table
            starred_albums: List of album dicts with 'artist' and 'album' keys
            
        Returns:
            Number of albums updated
        """
        conn = self.get_db_connection()
        cursor = conn.cursor()
        
        try:
            # Unstar all albums for this user
            cursor.execute("""
                UPDATE user_loved_albums
                SET is_loved = 0,
                    loved_at = NULL
                WHERE user_id = ?
            """, (user_id,))
            
            # Mark starred albums as loved
            updated_count = 0
            for album in starred_albums:
                artist = album.get("artist", "")
                album_name = album.get("name", "")
                if artist and album_name:
                    cursor.execute("""
                        INSERT INTO user_loved_albums (user_id, artist, album, is_loved, loved_at)
                        VALUES (?, ?, ?, 1, ?)
                        ON CONFLICT(user_id, artist, album) DO UPDATE SET
                            is_loved = 1,
                            loved_at = ?
                    """, (user_id, artist, album_name, datetime.now().isoformat(), datetime.now().isoformat()))
                    updated_count += 1
            
            conn.commit()
            logger.info(f"Synced {updated_count} starred albums for user {user_id}")
            return updated_count
            
        except Exception as e:
            logger.error(f"Failed to sync Navidrome starred albums: {e}")
            conn.rollback()
            return 0
        finally:
            conn.close()
    
    def sync_navidrome_starred_artists(self, user_id: int, starred_artists: List[str]) -> int:
        """
        Sync starred artists from Navidrome for a specific user.
        
        Args:
            user_id: User ID from navidrome_users table
            starred_artists: List of artist names that are starred
            
        Returns:
            Number of artists updated
        """
        conn = self.get_db_connection()
        cursor = conn.cursor()
        
        try:
            # Unstar all artists for this user
            cursor.execute("""
                UPDATE user_loved_artists
                SET is_loved = 0,
                    loved_at = NULL
                WHERE user_id = ?
            """, (user_id,))
            
            # Mark starred artists as loved
            updated_count = 0
            for artist in starred_artists:
                cursor.execute("""
                    INSERT INTO user_loved_artists (user_id, artist, is_loved, loved_at)
                    VALUES (?, ?, 1, ?)
                    ON CONFLICT(user_id, artist) DO UPDATE SET
                        is_loved = 1,
                        loved_at = ?
                """, (user_id, artist, datetime.now().isoformat(), datetime.now().isoformat()))
                updated_count += 1
            
            conn.commit()
            logger.info(f"Synced {updated_count} starred artists for user {user_id}")
            return updated_count
            
        except Exception as e:
            logger.error(f"Failed to sync Navidrome starred artists: {e}")
            conn.rollback()
            return 0
        finally:
            conn.close()
    
    def love_track(self, user_id: int, track_id: str, sync_to_listenbrainz: bool = True) -> bool:
        """
        Mark a track as loved for a user.
        
        Args:
            user_id: User ID
            track_id: Track ID
            sync_to_listenbrainz: If True and user has LB token, sync to ListenBrainz
            
        Returns:
            True if successful
        """
        conn = self.get_db_connection()
        cursor = conn.cursor()
        
        try:
            # Mark as loved in database
            cursor.execute("""
                INSERT INTO user_loved_tracks (user_id, track_id, is_loved, loved_at)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(user_id, track_id) DO UPDATE SET
                    is_loved = 1,
                    loved_at = ?
            """, (user_id, track_id, datetime.now().isoformat(), datetime.now().isoformat()))
            
            conn.commit()
            
            # Sync to ListenBrainz if requested
            if sync_to_listenbrainz:
                self._sync_track_to_listenbrainz(user_id, track_id, loved=True)
            
            logger.info(f"Loved track {track_id} for user {user_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to love track {track_id}: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()
    
    def unlove_track(self, user_id: int, track_id: str, sync_to_listenbrainz: bool = True) -> bool:
        """
        Remove love status from a track for a user.
        
        Args:
            user_id: User ID
            track_id: Track ID
            sync_to_listenbrainz: If True and user has LB token, sync to ListenBrainz
            
        Returns:
            True if successful
        """
        conn = self.get_db_connection()
        cursor = conn.cursor()
        
        try:
            # Remove love status
            cursor.execute("""
                UPDATE user_loved_tracks
                SET is_loved = 0,
                    loved_at = NULL
                WHERE user_id = ? AND track_id = ?
            """, (user_id, track_id))
            
            conn.commit()
            
            # Sync to ListenBrainz if requested
            if sync_to_listenbrainz:
                self._sync_track_to_listenbrainz(user_id, track_id, loved=False)
            
            logger.info(f"Unloved track {track_id} for user {user_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to unlove track {track_id}: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()
    
    def _sync_track_to_listenbrainz(self, user_id: int, track_id: str, loved: bool) -> bool:
        """
        Sync track love status to ListenBrainz.
        
        Args:
            user_id: User ID
            track_id: Track ID
            loved: True to love, False to unlove
            
        Returns:
            True if successful
        """
        conn = self.get_db_connection()
        cursor = conn.cursor()
        
        try:
            # Get user's ListenBrainz token
            cursor.execute("""
                SELECT listenbrainz_token FROM navidrome_users WHERE id = ?
            """, (user_id,))
            row = cursor.fetchone()
            
            if not row or not row['listenbrainz_token']:
                logger.debug(f"User {user_id} has no ListenBrainz token")
                return False
            
            lb_token = row['listenbrainz_token']
            
            # Get track MBID
            cursor.execute("""
                SELECT mbid, beets_mbid FROM tracks WHERE id = ?
            """, (track_id,))
            track_row = cursor.fetchone()
            
            if not track_row:
                logger.warning(f"Track {track_id} not found")
                return False
            
            mbid = track_row['mbid'] or track_row['beets_mbid']
            
            if not mbid:
                logger.warning(f"Track {track_id} has no MBID, cannot sync to ListenBrainz")
                return False
            
            # Sync to ListenBrainz
            from api_clients.audiodb_and_listenbrainz import ListenBrainzUserClient
            lb_client = ListenBrainzUserClient(lb_token)
            
            if loved:
                success = lb_client.love_track(mbid)
            else:
                success = lb_client.unlove_track(mbid)
            
            # Update sync status
            cursor.execute("""
                UPDATE user_loved_tracks
                SET synced_to_listenbrainz = ?,
                    last_sync_attempt = ?
                WHERE user_id = ? AND track_id = ?
            """, (1 if success else 0, datetime.now().isoformat(), user_id, track_id))
            
            conn.commit()
            return success
            
        except Exception as e:
            logger.error(f"Failed to sync track {track_id} to ListenBrainz: {e}")
            return False
        finally:
            conn.close()
    
    def get_user_loved_tracks(self, user_id: int) -> List[str]:
        """
        Get list of track IDs loved by a user.
        
        Args:
            user_id: User ID
            
        Returns:
            List of track IDs
        """
        conn = self.get_db_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute("""
                SELECT track_id FROM user_loved_tracks
                WHERE user_id = ? AND is_loved = 1
            """, (user_id,))
            
            return [row['track_id'] for row in cursor.fetchall()]
            
        except Exception as e:
            logger.error(f"Failed to get loved tracks for user {user_id}: {e}")
            return []
        finally:
            conn.close()
    
    def is_track_loved(self, user_id: int, track_id: str) -> bool:
        """
        Check if a track is loved by a user.
        
        Args:
            user_id: User ID
            track_id: Track ID
            
        Returns:
            True if loved
        """
        conn = self.get_db_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute("""
                SELECT is_loved FROM user_loved_tracks
                WHERE user_id = ? AND track_id = ?
            """, (user_id, track_id))
            
            row = cursor.fetchone()
            return bool(row and row['is_loved'])
            
        except Exception as e:
            logger.error(f"Failed to check if track {track_id} is loved: {e}")
            return False
        finally:
            conn.close()


# Convenience functions
def sync_all_users_from_navidrome():
    """
    Sync starred items from Navidrome for all active users.
    """
    from api_clients.navidrome import NavidromeClient
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    try:
        # Get all active users
        cursor.execute("""
            SELECT id, username, navidrome_base_url, navidrome_password
            FROM navidrome_users
            WHERE is_active = 1
        """)
        
        users = cursor.fetchall()
        conn.close()
        
        manager = LoveSyncManager()
        
        for user in users:
            user_id = user['id']
            username = user['username']
            base_url = user['navidrome_base_url']
            password = user['navidrome_password']
            
            if not base_url or not password:
                logger.warning(f"User {username} missing Navidrome credentials")
                continue
            
            logger.info(f"Syncing starred items for user {username}")
            
            # Fetch starred items from Navidrome
            client = NavidromeClient(base_url, username, password)
            starred = client.get_starred_items()
            
            # Sync tracks
            track_ids = [track['id'] for track in starred['tracks']]
            manager.sync_navidrome_starred_tracks(user_id, track_ids)
            
            # Sync albums
            albums = [{'artist': album.get('artist', ''), 'name': album.get('name', '')} 
                     for album in starred['albums']]
            manager.sync_navidrome_starred_albums(user_id, albums)
            
            # Sync artists
            artist_names = [artist['name'] for artist in starred['artists']]
            manager.sync_navidrome_starred_artists(user_id, artist_names)
        
        logger.info(f"Completed sync for {len(users)} users")
        
    except Exception as e:
        logger.error(f"Failed to sync users from Navidrome: {e}")
        if conn:
            conn.close()


if __name__ == "__main__":
    # Test sync
    logging.basicConfig(level=logging.INFO)
    sync_all_users_from_navidrome()
