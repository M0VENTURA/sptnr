#!/usr/bin/env python3
"""
Tests for Genre and Title Processing Module
============================================

Tests the automatic genre tag and title updates based on album and track metadata.
"""

import os
import sys
import unittest

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from genre_title_processor import (
    has_parenthetical_tag,
    should_append_tag_to_title,
    extract_parenthetical_tags,
    append_tag_to_title,
    check_album_for_tags,
    process_track_genres_and_title
)


class TestParentheticalTagDetection(unittest.TestCase):
    """Test detection of parenthetical tags in titles"""
    
    def test_has_live_tag(self):
        """Test detection of (live) tag"""
        self.assertTrue(has_parenthetical_tag("Song Name (live)", "live"))
        self.assertTrue(has_parenthetical_tag("Song Name (Live)", "live"))
        self.assertTrue(has_parenthetical_tag("Song Name (LIVE)", "live"))
        self.assertTrue(has_parenthetical_tag("Song Name (live version)", "live"))
        self.assertTrue(has_parenthetical_tag("Song Name (Live at Wembley)", "live"))
    
    def test_has_acoustic_tag(self):
        """Test detection of (acoustic) tag"""
        self.assertTrue(has_parenthetical_tag("Song Name (acoustic)", "acoustic"))
        self.assertTrue(has_parenthetical_tag("Song Name (Acoustic)", "acoustic"))
        self.assertTrue(has_parenthetical_tag("Song Name (Acoustic Version)", "acoustic"))
    
    def test_no_tag(self):
        """Test when tag is not present"""
        self.assertFalse(has_parenthetical_tag("Song Name", "live"))
        self.assertFalse(has_parenthetical_tag("Live Song Name", "live"))
        self.assertFalse(has_parenthetical_tag("Song live Name", "live"))


class TestTagExtraction(unittest.TestCase):
    """Test extraction of tags from titles"""
    
    def test_extract_live_tag(self):
        """Test extraction of (Live) tag"""
        tags = extract_parenthetical_tags("Song Name (Live)")
        self.assertIn("Live", tags)
    
    def test_extract_multiple_tags(self):
        """Test extraction of multiple tags"""
        tags = extract_parenthetical_tags("Song Name (Live) (Acoustic)")
        self.assertIn("Live", tags)
        self.assertIn("Acoustic", tags)
    
    def test_extract_remix_tag(self):
        """Test extraction of (Remix) tag"""
        tags = extract_parenthetical_tags("Song Name (Remix)")
        self.assertIn("Remix", tags)
    
    def test_extract_demo_tag(self):
        """Test extraction of (Demo) tag"""
        tags = extract_parenthetical_tags("Song Name (Demo)")
        self.assertIn("Demo", tags)


class TestAlbumTagDetection(unittest.TestCase):
    """Test detection of tags in album names"""
    
    def test_acoustic_album(self):
        """Test detection of acoustic in album name"""
        result = check_album_for_tags("The Acoustic Sessions")
        self.assertTrue(result['acoustic'])
        self.assertFalse(result['unplugged'])
        self.assertFalse(result['live'])
    
    def test_unplugged_album(self):
        """Test detection of unplugged in album name"""
        result = check_album_for_tags("MTV Unplugged")
        self.assertTrue(result['unplugged'])
        self.assertFalse(result['acoustic'])
    
    def test_live_album(self):
        """Test detection of live in album name"""
        result = check_album_for_tags("Live at Madison Square Garden")
        self.assertTrue(result['live'])
        self.assertFalse(result['acoustic'])


class TestTitleGenreProcessing(unittest.TestCase):
    """Test the main title and genre processing logic"""
    
    def test_add_acoustic_from_genre_to_title(self):
        """Test appending (acoustic) to title when genre contains acoustic"""
        title, genres = process_track_genres_and_title(
            "Song Name",
            "Regular Album",
            ["Rock", "Acoustic"]
        )
        self.assertEqual(title, "Song Name (acoustic)")
        self.assertIn("Acoustic", genres)
    
    def test_dont_add_if_already_present(self):
        """Test not appending tag if already in title"""
        title, genres = process_track_genres_and_title(
            "Song Name (Acoustic)",
            "Regular Album",
            ["Rock", "Acoustic"]
        )
        self.assertEqual(title, "Song Name (Acoustic)")
    
    def test_add_live_from_genre_to_title(self):
        """Test appending (live) to title when genre contains live"""
        title, genres = process_track_genres_and_title(
            "Song Name",
            "Regular Album",
            ["Rock", "Live"]
        )
        self.assertEqual(title, "Song Name (live)")
    
    def test_extract_genre_from_title_tag(self):
        """Test adding genre from title parenthetical tag"""
        title, genres = process_track_genres_and_title(
            "Song Name (Demo)",
            "Regular Album",
            ["Rock"]
        )
        # Title should stay the same
        self.assertEqual(title, "Song Name (Demo)")
        # Demo should be added to genres
        self.assertIn("Demo", genres)
        self.assertIn("Rock", genres)
    
    def test_propagate_from_album_title(self):
        """Test propagating acoustic from album title to track"""
        title, genres = process_track_genres_and_title(
            "Song Name",
            "The Acoustic Album",
            ["Rock"]
        )
        # Should add (acoustic) to title
        self.assertEqual(title, "Song Name (acoustic)")
        # Should add Acoustic to genres
        self.assertIn("Acoustic", genres)
    
    def test_propagate_unplugged_from_album(self):
        """Test propagating unplugged from album title"""
        title, genres = process_track_genres_and_title(
            "Song Name",
            "MTV Unplugged",
            ["Rock"]
        )
        # Should add (unplugged) to title
        self.assertEqual(title, "Song Name (unplugged)")
        # Should add Unplugged to genres
        self.assertIn("Unplugged", genres)
    
    def test_complex_scenario(self):
        """Test complex scenario with multiple tags"""
        title, genres = process_track_genres_and_title(
            "Song Name (Live)",
            "Acoustic Sessions",
            ["Rock"]
        )
        # Should have (Live) from original title
        self.assertIn("(Live)", title)
        # Should have (acoustic) from album
        self.assertIn("(acoustic)", title)
        # Should have both Live and Acoustic in genres
        self.assertIn("Live", genres)
        self.assertIn("Acoustic", genres)
        self.assertIn("Rock", genres)


class TestTitleAppending(unittest.TestCase):
    """Test the title tag appending logic"""
    
    def test_append_tag(self):
        """Test basic tag appending"""
        result = append_tag_to_title("Song Name", "acoustic")
        self.assertEqual(result, "Song Name (acoustic)")
    
    def test_append_live(self):
        """Test appending live tag"""
        result = append_tag_to_title("Song Name", "live")
        self.assertEqual(result, "Song Name (live)")


if __name__ == "__main__":
    unittest.main()
