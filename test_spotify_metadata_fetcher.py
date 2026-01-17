#!/usr/bin/env python3
"""
Test script for Spotify metadata fetching functionality.
Validates that comprehensive metadata is correctly fetched and stored in the database.
"""

import os
import sys
import sqlite3
import tempfile
import json
import unittest
from unittest.mock import Mock, MagicMock, patch

# Set up test environment
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
test_db_path = os.environ["DB_PATH"]

print(f"Using test database: {test_db_path}")

# Import modules to test
from check_db import update_schema
from db_utils import get_db_connection
from spotify_metadata_fetcher import SpotifyMetadataFetcher


class TestSpotifyMetadataFetcher(unittest.TestCase):
    """Test cases for SpotifyMetadataFetcher class."""
    
    @classmethod
    def setUpClass(cls):
        """Set up test database schema once for all tests."""
        update_schema(test_db_path)
    
    def setUp(self):
        """Set up test fixtures for each test."""
        self.conn = get_db_connection()
        self.cursor = self.conn.cursor()
        
        # Create a mock Spotify client
        self.mock_client = Mock()
        
        # Insert a test track
        self.test_track_id = "test-track-1"
        self.cursor.execute("""
            INSERT OR REPLACE INTO tracks (id, artist, album, title, spotify_id)
            VALUES (?, ?, ?, ?, ?)
        """, (self.test_track_id, "Test Artist", "Test Album", "Test Track", "spotify-123"))
        self.conn.commit()
        
        # Create fetcher instance
        self.fetcher = SpotifyMetadataFetcher(self.mock_client, self.conn)
    
    def tearDown(self):
        """Clean up after each test."""
        # Clean up test data
        self.cursor.execute("DELETE FROM tracks WHERE id = ?", (self.test_track_id,))
        self.conn.commit()
        self.conn.close()
    
    def test_fetch_track_metadata_success(self):
        """Test successful fetching and storing of track metadata."""
        # Mock Spotify API responses
        self.mock_client.get_track_metadata.return_value = {
            "id": "spotify-123",
            "name": "Test Track",
            "popularity": 75,
            "explicit": False,
            "duration_ms": 240000,
            "album": {
                "id": "album-456",
                "name": "Test Album",
                "release_date": "2024-01-01",
                "album_type": "album",
                "total_tracks": 12
            },
            "artists": [
                {"id": "artist-789", "name": "Test Artist"}
            ],
            "external_ids": {
                "isrc": "USTEST1234567"
            }
        }
        
        self.mock_client.get_audio_features.return_value = {
            "tempo": 120.5,
            "energy": 0.8,
            "danceability": 0.7,
            "valence": 0.6,
            "acousticness": 0.3,
            "instrumentalness": 0.1,
            "liveness": 0.2,
            "speechiness": 0.05,
            "loudness": -5.5,
            "key": 5,
            "mode": 1,
            "time_signature": 4
        }
        
        self.mock_client.get_artist_metadata.return_value = {
            "id": "artist-789",
            "name": "Test Artist",
            "genres": ["rock", "alternative rock"],
            "popularity": 80
        }
        
        self.mock_client.get_album_metadata.return_value = {
            "id": "album-456",
            "label": "Test Records"
        }
        
        # Fetch metadata
        result = self.fetcher.fetch_and_store_track_metadata(
            track_id="spotify-123",
            db_track_id=self.test_track_id
        )
        
        # Verify result
        self.assertTrue(result)
        
        # Verify database was updated
        self.cursor.execute("SELECT * FROM tracks WHERE id = ?", (self.test_track_id,))
        row = self.cursor.fetchone()
        
        # Check core track metadata
        self.assertEqual(row["spotify_id"], "spotify-123")
        self.assertEqual(row["spotify_popularity"], 75)
        self.assertEqual(row["spotify_explicit"], 0)
        self.assertEqual(row["isrc"], "USTEST1234567")
        self.assertEqual(row["duration"], 240.0)  # Converted to seconds
        
        # Check audio features
        self.assertAlmostEqual(row["spotify_tempo"], 120.5)
        self.assertAlmostEqual(row["spotify_energy"], 0.8)
        self.assertAlmostEqual(row["spotify_danceability"], 0.7)
        self.assertAlmostEqual(row["spotify_valence"], 0.6)
        
        # Check artist metadata
        self.assertEqual(row["spotify_artist_id"], "artist-789")
        self.assertEqual(row["spotify_artist_popularity"], 80)
        artist_genres = json.loads(row["spotify_artist_genres"])
        self.assertIn("rock", artist_genres)
        
        # Check album metadata
        self.assertEqual(row["spotify_album_id"], "album-456")
        self.assertEqual(row["spotify_album_label"], "Test Records")
        
        # Check derived tags
        special_tags = json.loads(row["special_tags"]) if row["special_tags"] else []
        # This track should have no special tags
        self.assertEqual(len(special_tags), 0)
        
        # Check normalized genres
        normalized_genres = json.loads(row["normalized_genres"]) if row["normalized_genres"] else []
        self.assertIn("rock", normalized_genres)
    
    def test_fetch_metadata_with_special_tags(self):
        """Test that special tags are correctly detected and stored."""
        # Mock Spotify API responses for a Christmas track
        self.mock_client.get_track_metadata.return_value = {
            "id": "spotify-123",
            "name": "Jingle Bells (Live)",
            "popularity": 60,
            "explicit": False,
            "duration_ms": 180000,
            "album": {
                "id": "album-456",
                "name": "Christmas Concert Live",
                "release_date": "2023-12-01",
                "album_type": "album",
                "total_tracks": 15
            },
            "artists": [
                {"id": "artist-789", "name": "Test Artist"}
            ],
            "external_ids": {
                "isrc": "USTEST1234567"
            }
        }
        
        self.mock_client.get_audio_features.return_value = {
            "tempo": 140.0,
            "energy": 0.9,
            "danceability": 0.6,
            "valence": 0.8,
            "acousticness": 0.75,  # Above 0.7 threshold
            "instrumentalness": 0.0,
            "liveness": 0.85,
            "speechiness": 0.1,
            "loudness": -4.0,
            "key": 0,
            "mode": 1,
            "time_signature": 4
        }
        
        self.mock_client.get_artist_metadata.return_value = {
            "id": "artist-789",
            "name": "Test Artist",
            "genres": ["christmas", "holiday"],
            "popularity": 70
        }
        
        self.mock_client.get_album_metadata.return_value = {
            "id": "album-456",
            "label": "Holiday Records"
        }
        
        # Fetch metadata
        result = self.fetcher.fetch_and_store_track_metadata(
            track_id="spotify-123",
            db_track_id=self.test_track_id
        )
        
        # Verify result
        self.assertTrue(result)
        
        # Verify special tags were detected
        self.cursor.execute("SELECT special_tags FROM tracks WHERE id = ?", (self.test_track_id,))
        row = self.cursor.fetchone()
        special_tags = json.loads(row["special_tags"]) if row["special_tags"] else []
        
        # Should detect Christmas (from title, album, and genre), Live (from title and liveness), and Acoustic
        self.assertIn("Christmas", special_tags)
        self.assertIn("Live", special_tags)
        self.assertIn("Acoustic", special_tags)
    
    def test_fetch_metadata_no_track_id(self):
        """Test that fetcher handles missing track ID gracefully."""
        result = self.fetcher.fetch_and_store_track_metadata(
            track_id=None,
            db_track_id=self.test_track_id
        )
        self.assertFalse(result)
    
    def test_fetch_metadata_api_failure(self):
        """Test that fetcher handles API failures gracefully."""
        # Mock API to return None (simulating failure)
        self.mock_client.get_track_metadata.return_value = None
        
        result = self.fetcher.fetch_and_store_track_metadata(
            track_id="spotify-123",
            db_track_id=self.test_track_id
        )
        
        self.assertFalse(result)
    
    def test_should_refresh_metadata_no_previous_update(self):
        """Test that tracks without metadata_last_updated should be refreshed."""
        # Track has no metadata_last_updated
        should_refresh = self.fetcher._should_refresh_metadata(self.test_track_id)
        self.assertTrue(should_refresh)
    
    def test_should_refresh_metadata_recent_update(self):
        """Test that recently updated tracks should not be refreshed."""
        from datetime import datetime
        
        # Set metadata_last_updated to now
        self.cursor.execute(
            "UPDATE tracks SET metadata_last_updated = ? WHERE id = ?",
            (datetime.now().isoformat(), self.test_track_id)
        )
        self.conn.commit()
        
        should_refresh = self.fetcher._should_refresh_metadata(self.test_track_id)
        self.assertFalse(should_refresh)
    
    def test_should_refresh_metadata_old_update(self):
        """Test that tracks with old metadata should be refreshed."""
        from datetime import datetime, timedelta
        
        # Set metadata_last_updated to 60 days ago
        old_date = datetime.now() - timedelta(days=60)
        self.cursor.execute(
            "UPDATE tracks SET metadata_last_updated = ? WHERE id = ?",
            (old_date.isoformat(), self.test_track_id)
        )
        self.conn.commit()
        
        should_refresh = self.fetcher._should_refresh_metadata(self.test_track_id)
        self.assertTrue(should_refresh)


if __name__ == "__main__":
    print("Running Spotify Metadata Fetcher Tests...")
    print(f"Test database: {test_db_path}")
    unittest.main(verbosity=2)
