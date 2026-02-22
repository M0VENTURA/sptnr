#!/usr/bin/env python3
"""
Test script for Spotify genre endpoint logic.
Verifies that the genre extraction logic works correctly.
"""

import json
import unittest


class TestSpotifyGenreLogic(unittest.TestCase):
    """Test cases for Spotify genre extraction logic."""
    
    def test_genre_extraction_from_json(self):
        """Test genre extraction from JSON strings."""
        # Simulate database rows with JSON genre data
        genre_rows = [
            (json.dumps(["rock", "alternative rock", "indie"]),),
            (json.dumps(["rock", "indie"]),),
        ]
        
        # Extract unique genres
        genres = set()
        for row in genre_rows:
            try:
                genre_value = row[0] if row else None
                if genre_value:
                    genre_list = json.loads(genre_value)
                    if isinstance(genre_list, list):
                        genres.update(genre_list)
            except (json.JSONDecodeError, IndexError, TypeError):
                continue
        
        # Verify results
        self.assertEqual(len(genres), 3)
        self.assertIn("rock", genres)
        self.assertIn("alternative rock", genres)
        self.assertIn("indie", genres)
    
    def test_genre_extraction_empty_list(self):
        """Test genre extraction with no genres."""
        genre_rows = []
        
        genres = set()
        for row in genre_rows:
            try:
                genre_value = row[0] if row else None
                if genre_value:
                    genre_list = json.loads(genre_value)
                    if isinstance(genre_list, list):
                        genres.update(genre_list)
            except (json.JSONDecodeError, IndexError, TypeError):
                continue
        
        self.assertEqual(len(genres), 0)
    
    def test_genre_extraction_invalid_json(self):
        """Test genre extraction handles invalid JSON gracefully."""
        genre_rows = [
            ("not a json string",),
            (json.dumps(["valid genre"]),),
        ]
        
        genres = set()
        for row in genre_rows:
            try:
                genre_value = row[0] if row else None
                if genre_value:
                    genre_list = json.loads(genre_value)
                    if isinstance(genre_list, list):
                        genres.update(genre_list)
            except (json.JSONDecodeError, IndexError, TypeError):
                continue
        
        # Should only have the valid genre
        self.assertEqual(len(genres), 1)
        self.assertIn("valid genre", genres)
    
    def test_genre_sorting(self):
        """Test that genres are returned sorted."""
        genres = {"rock", "jazz", "alternative", "blues"}
        sorted_genres = sorted(list(genres))
        
        self.assertEqual(sorted_genres[0], "alternative")
        self.assertEqual(sorted_genres[-1], "rock")


if __name__ == '__main__':
    print("Testing Spotify genre extraction logic...")
    unittest.main(verbosity=2)
