#!/usr/bin/env python3
"""
Test script for genre detection logic.
Verifies that special genre tags are correctly detected from track metadata and audio features.
"""

import sys
import unittest
from genre_detector import GenreDetector, detect_special_tags, normalize_genres


class TestGenreDetector(unittest.TestCase):
    """Test cases for GenreDetector class."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.detector = GenreDetector()
    
    def test_christmas_detection_by_title(self):
        """Test Christmas detection from track title."""
        tags = self.detector.detect_special_tags(
            track_name="Jingle Bells",
            album_name="Regular Album",
            artist_genres=[]
        )
        self.assertIn("Christmas", tags)
        
        tags = self.detector.detect_special_tags(
            track_name="Silent Night",
            album_name="Regular Album",
            artist_genres=[]
        )
        self.assertIn("Christmas", tags)
    
    def test_christmas_detection_by_album(self):
        """Test Christmas detection from album name."""
        tags = self.detector.detect_special_tags(
            track_name="Regular Track",
            album_name="Christmas Carols Collection",
            artist_genres=[]
        )
        self.assertIn("Christmas", tags)
    
    def test_christmas_detection_by_genre(self):
        """Test Christmas detection from artist genres."""
        tags = self.detector.detect_special_tags(
            track_name="Regular Track",
            album_name="Regular Album",
            artist_genres=["christmas", "holiday music"]
        )
        self.assertIn("Christmas", tags)
    
    def test_cover_detection_by_title(self):
        """Test Cover detection from track title."""
        tags = self.detector.detect_special_tags(
            track_name="Song Name (Cover)",
            album_name="Regular Album",
            artist_genres=[]
        )
        self.assertIn("Cover", tags)
        
        tags = self.detector.detect_special_tags(
            track_name="Song Name (Tribute)",
            album_name="Regular Album",
            artist_genres=[]
        )
        self.assertIn("Cover", tags)
    
    def test_cover_detection_by_album(self):
        """Test Cover detection from album name."""
        tags = self.detector.detect_special_tags(
            track_name="Regular Track",
            album_name="Tribute to The Beatles",
            artist_genres=[]
        )
        self.assertIn("Cover", tags)
    
    def test_live_detection_by_title(self):
        """Test Live detection from track title."""
        tags = self.detector.detect_special_tags(
            track_name="Song Name (Live)",
            album_name="Regular Album",
            artist_genres=[]
        )
        self.assertIn("Live", tags)
        
        tags = self.detector.detect_special_tags(
            track_name="Live at Madison Square Garden",
            album_name="Regular Album",
            artist_genres=[]
        )
        self.assertIn("Live", tags)
    
    def test_live_detection_by_album(self):
        """Test Live detection from album name."""
        tags = self.detector.detect_special_tags(
            track_name="Regular Track",
            album_name="Live from Wembley",
            artist_genres=[]
        )
        self.assertIn("Live", tags)
    
    def test_live_detection_by_audio_features(self):
        """Test Live detection from liveness audio feature."""
        tags = self.detector.detect_special_tags(
            track_name="Regular Track",
            album_name="Regular Album",
            artist_genres=[],
            audio_features={"liveness": 0.85}
        )
        self.assertIn("Live", tags)
    
    def test_acoustic_detection_by_title(self):
        """Test Acoustic detection from track title."""
        tags = self.detector.detect_special_tags(
            track_name="Song Name (Acoustic)",
            album_name="Regular Album",
            artist_genres=[]
        )
        self.assertIn("Acoustic", tags)
    
    def test_acoustic_detection_by_audio_features(self):
        """Test Acoustic detection from acousticness audio feature."""
        tags = self.detector.detect_special_tags(
            track_name="Regular Track",
            album_name="Regular Album",
            artist_genres=[],
            audio_features={"acousticness": 0.75}
        )
        self.assertIn("Acoustic", tags)
    
    def test_instrumental_detection(self):
        """Test Instrumental detection from audio features."""
        tags = self.detector.detect_special_tags(
            track_name="Regular Track",
            album_name="Regular Album",
            artist_genres=[],
            audio_features={"instrumentalness": 0.85}
        )
        self.assertIn("Instrumental", tags)
    
    def test_orchestral_detection_by_title(self):
        """Test Orchestral detection from track title."""
        tags = self.detector.detect_special_tags(
            track_name="Symphonic Version",
            album_name="Regular Album",
            artist_genres=[]
        )
        self.assertIn("Orchestral", tags)
    
    def test_orchestral_detection_by_audio_features(self):
        """Test Orchestral detection from audio features."""
        tags = self.detector.detect_special_tags(
            track_name="Regular Track",
            album_name="Regular Album",
            artist_genres=[],
            audio_features={
                "instrumentalness": 0.85,
                "acousticness": 0.6
            }
        )
        self.assertIn("Orchestral", tags)
        self.assertIn("Instrumental", tags)
    
    def test_multiple_tags_detection(self):
        """Test detection of multiple special tags."""
        tags = self.detector.detect_special_tags(
            track_name="Jingle Bells (Live Acoustic)",
            album_name="Christmas Concert",
            artist_genres=[],
            audio_features={
                "liveness": 0.85,
                "acousticness": 0.8
            }
        )
        self.assertIn("Christmas", tags)
        self.assertIn("Live", tags)
        self.assertIn("Acoustic", tags)
    
    def test_no_tags_detection(self):
        """Test that regular tracks don't get false positive tags."""
        tags = self.detector.detect_special_tags(
            track_name="Regular Song",
            album_name="Regular Album",
            artist_genres=["rock", "alternative"],
            audio_features={
                "liveness": 0.2,
                "acousticness": 0.3,
                "instrumentalness": 0.1
            }
        )
        self.assertEqual(len(tags), 0)
    
    def test_normalize_genres_rock(self):
        """Test genre normalization for rock genres."""
        genres = normalize_genres(["alternative rock", "indie rock", "rock"])
        self.assertIn("rock", genres)
    
    def test_normalize_genres_metal(self):
        """Test genre normalization for metal genres."""
        genres = normalize_genres(["death metal", "black metal", "metal"])
        self.assertIn("metal", genres)
    
    def test_normalize_genres_electronic(self):
        """Test genre normalization for electronic genres."""
        genres = normalize_genres(["house", "techno", "edm", "electronic"])
        self.assertIn("electronic", genres)
    
    def test_normalize_genres_mixed(self):
        """Test genre normalization for mixed genres."""
        genres = normalize_genres([
            "indie rock", 
            "electronic", 
            "hip hop",
            "folk rock"
        ])
        # Should normalize indie rock and folk rock to "rock"
        self.assertIn("rock", genres)
        self.assertIn("electronic", genres)
        self.assertIn("hip hop", genres)
        self.assertIn("folk", genres)
    
    def test_normalize_genres_empty(self):
        """Test genre normalization with empty input."""
        genres = normalize_genres([])
        self.assertEqual(genres, [])
    
    def test_convenience_functions(self):
        """Test convenience functions work correctly."""
        tags = detect_special_tags(
            track_name="Christmas Song (Live)",
            album_name="Holiday Album",
            artist_genres=[]
        )
        self.assertIn("Christmas", tags)
        self.assertIn("Live", tags)
        
        genres = normalize_genres(["rock", "metal"])
        self.assertIsInstance(genres, list)


if __name__ == "__main__":
    print("Running Genre Detection Tests...")
    unittest.main(verbosity=2)
