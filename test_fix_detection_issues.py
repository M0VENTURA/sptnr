#!/usr/bin/env python3
"""
Test cases for single detection fixes (PR fixing issue with "Life in Technicolor" vs "Life in Technicolor II")

These tests validate that the detection system correctly distinguishes between similar tracks:
1. "Life in Technicolor" (not a single) vs "Life in Technicolor II" (is a single)
2. "Lost!" (is a single) vs "Lost+" (not a traditional single)

The fixes address issues in:
- MusicBrainz matching: Now compares base titles in addition to version keywords
- Discogs single matching: Increased fuzzy matching threshold from 0.80 to 0.95
- Discogs video matching: Changed from substring to exact matching after cleaning
"""

import unittest
from api_clients.musicbrainz import _extract_version_info
from api_clients.discogs import DiscogsClient
import re


class TestVersionInfoExtraction(unittest.TestCase):
    """Test that version info extraction preserves important suffixes."""
    
    def test_roman_numeral_preservation(self):
        """Test that Roman numerals are preserved in base titles."""
        # Life in Technicolor (no suffix)
        base, versions = _extract_version_info("Life in Technicolor")
        self.assertEqual(base, "Life in Technicolor")
        self.assertEqual(versions, set())
        
        # Life in Technicolor II (with Roman numeral)
        base, versions = _extract_version_info("Life in Technicolor II")
        self.assertEqual(base.lower(), "life in technicolor ii")
        self.assertEqual(versions, set())
    
    def test_punctuation_suffix_preservation(self):
        """Test that punctuation suffixes are preserved in base titles."""
        # Lost! (with exclamation)
        base, versions = _extract_version_info("Lost!")
        self.assertEqual(base, "Lost!")
        self.assertEqual(versions, set())
        
        # Lost+ (with plus)
        base, versions = _extract_version_info("Lost+")
        self.assertEqual(base, "Lost+")
        self.assertEqual(versions, set())


class TestMusicBrainzMatching(unittest.TestCase):
    """Test that MusicBrainz matching compares both base titles and versions."""
    
    def test_title_differentiation(self):
        """Test that similar titles with different suffixes are distinguished."""
        # Simulate the matching logic from is_single()
        
        # Track: "Life in Technicolor"
        track_base, track_versions = _extract_version_info("Life in Technicolor")
        
        # Single: "Life in Technicolor II"
        single_base, single_versions = _extract_version_info("Life in Technicolor II")
        
        # Should NOT match because base titles differ
        matches = (track_base.lower() == single_base.lower() and 
                  track_versions == single_versions)
        self.assertFalse(matches, 
            "Life in Technicolor should NOT match Life in Technicolor II")
    
    def test_punctuation_differentiation(self):
        """Test that punctuation suffixes distinguish tracks."""
        # Track: "Lost!"
        track_base, track_versions = _extract_version_info("Lost!")
        
        # Single: "Lost+"
        single_base, single_versions = _extract_version_info("Lost+")
        
        # Should NOT match because punctuation differs
        matches = (track_base.lower() == single_base.lower() and 
                  track_versions == single_versions)
        self.assertFalse(matches, 
            "Lost! should NOT match Lost+")
    
    def test_exact_match_works(self):
        """Test that exact matches still work."""
        # Track: "Lost!"
        track_base, track_versions = _extract_version_info("Lost!")
        
        # Single: "Lost!"
        single_base, single_versions = _extract_version_info("Lost!")
        
        # Should match
        matches = (track_base.lower() == single_base.lower() and 
                  track_versions == single_versions)
        self.assertTrue(matches, 
            "Lost! should match Lost!")


class TestDiscogsVideoMatching(unittest.TestCase):
    """Test that Discogs video matching uses exact matching after cleaning."""
    
    def clean_video_title(self, video_title):
        """Helper to simulate the cleaning logic."""
        # Remove video suffixes
        cleaned = re.sub(
            r'\s*[\(\[]?(official|music)?\s*(video|music video|mv|hd|4k|lyric video)[\)\]]?\s*$',
            '', video_title, flags=re.IGNORECASE
        ).strip()
        
        # Remove artist prefix
        if ' - ' in cleaned:
            parts = cleaned.split(' - ', 1)
            if len(parts) == 2:
                cleaned = parts[1].strip()
        
        return cleaned
    
    def test_video_title_cleaning(self):
        """Test that video titles are cleaned correctly."""
        # Test cleaning removes "official video" suffix
        cleaned = self.clean_video_title("life in technicolor official video")
        self.assertEqual(cleaned, "life in technicolor")
        
        # Test cleaning removes artist prefix
        cleaned = self.clean_video_title("coldplay - viva la vida official video")
        self.assertEqual(cleaned, "viva la vida")
    
    def test_exact_matching_prevents_false_positives(self):
        """Test that exact matching prevents false positives."""
        # "Life in Technicolor" should NOT match "Life in Technicolor II" video
        track = "life in technicolor"
        video = "life in technicolor ii official video"
        cleaned = self.clean_video_title(video)
        
        self.assertNotEqual(track, cleaned,
            "Life in Technicolor should NOT match Life in Technicolor II video")
        
        # "Lost!" should NOT match "Lost+" video
        track = "lost!"
        video = "lost+ official video"
        cleaned = self.clean_video_title(video)
        
        self.assertNotEqual(track, cleaned,
            "Lost! should NOT match Lost+ video")
    
    def test_exact_matching_allows_correct_matches(self):
        """Test that exact matching still allows correct matches."""
        # "Life in Technicolor II" should match its video
        track = "life in technicolor ii"
        video = "life in technicolor ii official video"
        cleaned = self.clean_video_title(video)
        
        self.assertEqual(track, cleaned,
            "Life in Technicolor II should match its own video")
        
        # "Lost!" should match its video
        track = "lost!"
        video = "lost! official video"
        cleaned = self.clean_video_title(video)
        
        self.assertEqual(track, cleaned,
            "Lost! should match its own video")


class TestDiscogsSingleMatching(unittest.TestCase):
    """Test that Discogs single matching uses stricter threshold."""
    
    def test_fuzzy_threshold(self):
        """Test that the new threshold (0.95) prevents false positives."""
        import difflib
        
        # "Life in Technicolor" vs "Life in Technicolor II"
        ratio = difflib.SequenceMatcher(
            None, 
            "life in technicolor", 
            "life in technicolor ii"
        ).ratio()
        
        # Old threshold of 0.80 would match (ratio is 0.927)
        self.assertGreater(ratio, 0.80, "Ratio should exceed old threshold")
        
        # New threshold of 0.95 should NOT match
        self.assertLess(ratio, 0.95, "Ratio should be below new threshold")
        
        # "Lost!" vs "Lost+"
        ratio = difflib.SequenceMatcher(
            None, 
            "lost!", 
            "lost+"
        ).ratio()
        
        # Old threshold of 0.80 would match (ratio is 0.80)
        self.assertGreaterEqual(ratio, 0.80, "Ratio should meet old threshold")
        
        # New threshold of 0.95 should NOT match
        self.assertLess(ratio, 0.95, "Ratio should be below new threshold")


class TestColdplayAlbumScenario(unittest.TestCase):
    """Integration tests for the specific Coldplay album scenario."""
    
    def test_life_in_technicolor_differentiation(self):
        """
        Test the real-world scenario from the issue:
        - "Life in Technicolor" (Disc 1, Track 1) - NOT a single
        - "Life in Technicolor II" (Disc 2, Track 1) - IS a single
        """
        # Extract version info for both tracks
        track1_base, track1_versions = _extract_version_info("Life in Technicolor")
        track2_base, track2_versions = _extract_version_info("Life in Technicolor II")
        
        # They should have different base titles
        self.assertNotEqual(
            track1_base.lower(), 
            track2_base.lower(),
            "The two tracks should have different base titles"
        )
        
        # Both should have no version keywords
        self.assertEqual(track1_versions, set())
        self.assertEqual(track2_versions, set())
    
    def test_lost_variants_differentiation(self):
        """
        Test the real-world scenario from the issue:
        - "Lost!" (Disc 1, Track 3) - IS a single
        - "Lost+" (Disc 2, Track 6, featuring Jay-Z) - NOT a traditional single
        """
        # Extract version info for both tracks
        track1_base, track1_versions = _extract_version_info("Lost!")
        track2_base, track2_versions = _extract_version_info("Lost+")
        
        # They should have different base titles due to punctuation
        self.assertNotEqual(
            track1_base, 
            track2_base,
            "Lost! and Lost+ should have different base titles"
        )
        
        # Both should have no version keywords
        self.assertEqual(track1_versions, set())
        self.assertEqual(track2_versions, set())


if __name__ == "__main__":
    unittest.main()
