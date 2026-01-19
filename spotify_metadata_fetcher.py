#!/usr/bin/env python3
"""
Spotify Metadata Fetcher Module for SPTNR
Fetches comprehensive metadata from Spotify API including:
- Track metadata (ISRC, duration, popularity, explicit)
- Audio features (tempo, energy, danceability, etc.)
- Artist metadata (genres, popularity)
- Album metadata (label, total tracks, type)
"""

import json
import logging
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional, Any
from api_clients.spotify import SpotifyClient
from genre_detector import detect_special_tags, normalize_genres

logger = logging.getLogger(__name__)


class SpotifyMetadataFetcher:
    """Fetches and stores comprehensive Spotify metadata for tracks."""
    
    def __init__(self, spotify_client: SpotifyClient, db_connection: sqlite3.Connection):
        """
        Initialize metadata fetcher.
        
        Args:
            spotify_client: Configured SpotifyClient instance
            db_connection: SQLite database connection
        """
        self.client = spotify_client
        self.conn = db_connection
    
    def fetch_and_store_track_metadata(
        self, 
        track_id: str,
        db_track_id: str,
        track_name: str = None,
        artist_name: str = None,
        album_name: str = None,
        force_refresh: bool = False
    ) -> bool:
        """
        Fetch comprehensive Spotify metadata for a track and store in database.
        
        Args:
            track_id: Spotify track ID
            db_track_id: Database track ID (primary key)
            track_name: Track name (for fallback)
            artist_name: Artist name (for fallback)
            album_name: Album name (for fallback)
            force_refresh: Force refresh even if recently updated
            
        Returns:
            True if metadata was successfully fetched and stored
        """
        if not track_id:
            logger.debug(f"No Spotify track ID for db_track_id={db_track_id}")
            return False
        
        # Check if we need to refresh metadata
        if not force_refresh and not self._should_refresh_metadata(db_track_id):
            logger.debug(f"Skipping metadata refresh for track {track_id} (recently updated)")
            return True
        
        try:
            # Fetch track metadata
            track_meta = self.client.get_track_metadata(track_id)
            if not track_meta:
                logger.warning(f"Failed to fetch track metadata for {track_id}")
                return False
            
            # Extract basic track info
            album_id = track_meta.get("album", {}).get("id")
            artist_id = None
            artists = track_meta.get("artists", [])
            if artists:
                artist_id = artists[0].get("id")
            
            # Fetch audio features
            audio_features = self.client.get_audio_features(track_id)
            
            # Fetch artist metadata
            artist_metadata = None
            if artist_id:
                artist_metadata = self.client.get_artist_metadata(artist_id)
            
            # Fetch album metadata
            album_metadata = None
            if album_id:
                album_metadata = self.client.get_album_metadata(album_id)
            
            # Process and store metadata
            self._store_track_metadata(
                db_track_id=db_track_id,
                track_meta=track_meta,
                audio_features=audio_features,
                artist_metadata=artist_metadata,
                album_metadata=album_metadata
            )
            
            logger.info(f"✅ Fetched and stored comprehensive metadata for track {track_id}")
            return True
            
        except Exception as e:
            logger.error(f"❌ Error fetching metadata for track {track_id}: {e}")
            return False
    
    def fetch_and_store_batch(
        self,
        tracks: List[Dict[str, str]],
        force_refresh: bool = False
    ) -> int:
        """
        Fetch metadata for multiple tracks efficiently using batch requests where possible.
        
        Args:
            tracks: List of dicts with keys: db_track_id, spotify_track_id, track_name, 
                   artist_name, album_name
            force_refresh: Force refresh even if recently updated
            
        Returns:
            Number of tracks successfully processed
        """
        if not tracks:
            return 0
        
        success_count = 0
        
        # Filter tracks that need updating
        tracks_to_update = []
        for track in tracks:
            if force_refresh or self._should_refresh_metadata(track.get("db_track_id")):
                tracks_to_update.append(track)
        
        if not tracks_to_update:
            logger.info("All tracks have recent metadata, skipping batch fetch")
            return len(tracks)
        
        logger.info(f"Fetching metadata for {len(tracks_to_update)} tracks")
        
        # Batch fetch audio features (Spotify allows up to 100 per request)
        track_ids = [t.get("spotify_track_id") for t in tracks_to_update if t.get("spotify_track_id")]
        audio_features_map = {}
        
        # Process in chunks of 100
        for i in range(0, len(track_ids), 100):
            chunk = track_ids[i:i+100]
            features = self.client.get_audio_features_batch(chunk)
            audio_features_map.update(features)
        
        # Process each track individually (track, artist, album metadata)
        for track in tracks_to_update:
            try:
                spotify_track_id = track.get("spotify_track_id")
                if not spotify_track_id:
                    continue
                
                # Fetch track metadata
                track_meta = self.client.get_track_metadata(spotify_track_id)
                if not track_meta:
                    continue
                
                # Get pre-fetched audio features
                audio_features = audio_features_map.get(spotify_track_id)
                
                # Extract IDs
                album_id = track_meta.get("album", {}).get("id")
                artist_id = None
                artists = track_meta.get("artists", [])
                if artists:
                    artist_id = artists[0].get("id")
                
                # Fetch artist and album metadata
                artist_metadata = None
                if artist_id:
                    artist_metadata = self.client.get_artist_metadata(artist_id)
                
                album_metadata = None
                if album_id:
                    album_metadata = self.client.get_album_metadata(album_id)
                
                # Store metadata
                self._store_track_metadata(
                    db_track_id=track.get("db_track_id"),
                    track_meta=track_meta,
                    audio_features=audio_features,
                    artist_metadata=artist_metadata,
                    album_metadata=album_metadata
                )
                
                success_count += 1
                
            except Exception as e:
                logger.error(f"Error processing track {track.get('db_track_id')}: {e}")
                continue
        
        logger.info(f"✅ Successfully processed {success_count}/{len(tracks_to_update)} tracks")
        return success_count
    
    def _should_refresh_metadata(self, db_track_id: str) -> bool:
        """
        Check if track metadata should be refreshed.
        Skips if updated within last 30 days.
        
        Args:
            db_track_id: Database track ID
            
        Returns:
            True if metadata should be refreshed
        """
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT metadata_last_updated FROM tracks WHERE id = ?",
            (db_track_id,)
        )
        row = cursor.fetchone()
        
        if not row or not row[0]:
            return True
        
        # Check if last update was more than 30 days ago
        try:
            last_updated = datetime.fromisoformat(row[0])
            days_since_update = (datetime.now() - last_updated).days
            return days_since_update > 30
        except (ValueError, TypeError):
            return True
    
    def _store_track_metadata(
        self,
        db_track_id: str,
        track_meta: dict,
        audio_features: Optional[dict],
        artist_metadata: Optional[dict],
        album_metadata: Optional[dict]
    ):
        """
        Store comprehensive track metadata in database.
        
        Args:
            db_track_id: Database track ID (primary key)
            track_meta: Track metadata from Spotify
            audio_features: Audio features from Spotify
            artist_metadata: Artist metadata from Spotify
            album_metadata: Album metadata from Spotify
        """
        cursor = self.conn.cursor()
        
        # Extract core track metadata
        track_name = track_meta.get("name", "")
        album_name = track_meta.get("album", {}).get("name", "")
        artist_names = [a.get("name", "") for a in track_meta.get("artists", [])]
        artist_genres = []
        if artist_metadata:
            artist_genres = artist_metadata.get("genres", [])
        
        # Detect special tags
        special_tags = detect_special_tags(
            track_name=track_name,
            album_name=album_name,
            artist_genres=artist_genres,
            audio_features=audio_features
        )
        
        # Normalize genres
        normalized_genres = normalize_genres(artist_genres)
        
        # Build update query
        updates = {
            # Core track metadata
            "spotify_id": track_meta.get("id"),
            "isrc": track_meta.get("external_ids", {}).get("isrc"),
            "spotify_popularity": track_meta.get("popularity"),
            "spotify_explicit": 1 if track_meta.get("explicit") else 0,
            "duration": track_meta.get("duration_ms", 0) / 1000.0,  # Convert to seconds
            
            # Album metadata
            "spotify_album": album_name,
            "spotify_album_id": track_meta.get("album", {}).get("id"),
            "spotify_release_date": track_meta.get("album", {}).get("release_date"),
            "spotify_total_tracks": track_meta.get("album", {}).get("total_tracks"),
            "spotify_album_type": track_meta.get("album", {}).get("album_type"),
            
            # Album metadata from album endpoint
            "spotify_album_label": album_metadata.get("label") if album_metadata else None,
            
            # Artist metadata
            "spotify_artist": ", ".join(artist_names),
            "spotify_artist_id": track_meta.get("artists", [{}])[0].get("id") if track_meta.get("artists") else None,
            "spotify_artist_genres": json.dumps(artist_genres) if artist_genres else None,
            "spotify_artist_popularity": artist_metadata.get("popularity") if artist_metadata else None,
            
            # Audio features
            "spotify_tempo": audio_features.get("tempo") if audio_features else None,
            "spotify_energy": audio_features.get("energy") if audio_features else None,
            "spotify_danceability": audio_features.get("danceability") if audio_features else None,
            "spotify_valence": audio_features.get("valence") if audio_features else None,
            "spotify_acousticness": audio_features.get("acousticness") if audio_features else None,
            "spotify_instrumentalness": audio_features.get("instrumentalness") if audio_features else None,
            "spotify_liveness": audio_features.get("liveness") if audio_features else None,
            "spotify_speechiness": audio_features.get("speechiness") if audio_features else None,
            "spotify_loudness": audio_features.get("loudness") if audio_features else None,
            "spotify_key": audio_features.get("key") if audio_features else None,
            "spotify_mode": audio_features.get("mode") if audio_features else None,
            "spotify_time_signature": audio_features.get("time_signature") if audio_features else None,
            
            # Derived tags
            "special_tags": json.dumps(sorted(list(special_tags))) if special_tags else None,
            "normalized_genres": json.dumps(normalized_genres) if normalized_genres else None,
            
            # Metadata timestamp
            "metadata_last_updated": datetime.now().isoformat()
        }
        
        # Build SQL query
        set_clause = ", ".join([f"{k} = ?" for k in updates.keys()])
        values = list(updates.values()) + [db_track_id]
        
        sql = f"UPDATE tracks SET {set_clause} WHERE id = ?"
        
        cursor.execute(sql, values)
        self.conn.commit()
        
        logger.debug(f"Stored metadata for track {db_track_id}")


def fetch_metadata_for_artist(
    artist_name: str,
    spotify_client: SpotifyClient,
    db_connection: sqlite3.Connection,
    force_refresh: bool = False
) -> int:
    """
    Fetch and store Spotify metadata for all tracks by an artist.
    
    Args:
        artist_name: Artist name to fetch metadata for
        spotify_client: Configured SpotifyClient instance
        db_connection: SQLite database connection
        force_refresh: Force refresh even if recently updated
        
    Returns:
        Number of tracks successfully processed
    """
    cursor = db_connection.cursor()
    
    # Get all tracks by artist with Spotify IDs
    cursor.execute("""
        SELECT id as db_track_id, spotify_id as spotify_track_id, 
               title as track_name, artist as artist_name, album as album_name
        FROM tracks
        WHERE artist = ? AND spotify_id IS NOT NULL AND spotify_id != ''
    """, (artist_name,))
    
    tracks = [dict(row) for row in cursor.fetchall()]
    
    if not tracks:
        logger.info(f"No tracks with Spotify IDs found for artist: {artist_name}")
        return 0
    
    logger.info(f"Fetching metadata for {len(tracks)} tracks by {artist_name}")
    
    # Create fetcher and process batch
    fetcher = SpotifyMetadataFetcher(spotify_client, db_connection)
    return fetcher.fetch_and_store_batch(tracks, force_refresh=force_refresh)


def fetch_metadata_for_all_tracks(
    spotify_client: SpotifyClient,
    db_connection: sqlite3.Connection,
    force_refresh: bool = False,
    artist_filter: Optional[str] = None
) -> int:
    """
    Fetch and store Spotify metadata for all tracks in the database.
    
    Args:
        spotify_client: Configured SpotifyClient instance
        db_connection: SQLite database connection
        force_refresh: Force refresh even if recently updated
        artist_filter: Optional artist name to filter by
        
    Returns:
        Number of tracks successfully processed
    """
    cursor = db_connection.cursor()
    
    # Build query
    if artist_filter:
        sql = """
            SELECT id as db_track_id, spotify_id as spotify_track_id,
                   title as track_name, artist as artist_name, album as album_name
            FROM tracks
            WHERE artist = ? AND spotify_id IS NOT NULL AND spotify_id != ''
        """
        cursor.execute(sql, (artist_filter,))
    else:
        sql = """
            SELECT id as db_track_id, spotify_id as spotify_track_id,
                   title as track_name, artist as artist_name, album as album_name
            FROM tracks
            WHERE spotify_id IS NOT NULL AND spotify_id != ''
        """
        cursor.execute(sql)
    
    tracks = [dict(row) for row in cursor.fetchall()]
    
    if not tracks:
        logger.info("No tracks with Spotify IDs found")
        return 0
    
    logger.info(f"Fetching metadata for {len(tracks)} tracks")
    
    # Create fetcher and process batch
    fetcher = SpotifyMetadataFetcher(spotify_client, db_connection)
    return fetcher.fetch_and_store_batch(tracks, force_refresh=force_refresh)
