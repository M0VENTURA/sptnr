#!/usr/bin/env python3
"""
Tests for Album Matching Enhancements
======================================

Tests the enhanced album matching logic for special cases.
"""

import os
import sys
import unittest

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from album_matching_enhancements import (
    normalize_album_for_fallback,
    is_special_album_type,
    extract_year_from_date,
    is_within_time_window,
    should_apply_time_window_restriction,
    match_album_with_fallback
)


class TestAlbumNormalization(unittest.TestCase):
    """Test album name normalization for fallback matching"""
    
    def test_remove_deluxe_edition(self):
        """Test removal of (Deluxe Edition)"""
        result = normalize_album_for_fallback("Album Name (Deluxe Edition)")
        self.assertEqual(result, "Album Name")
    
    def test_remove_remastered(self):
        """Test removal of (Remastered)"""
        result = normalize_album_for_fallback("Album Name (Remastered)")
        self.assertEqual(result, "Album Name")
    
    def test_remove_rereleased(self):
        """Test removal of (Rereleased)"""
        result = normalize_album_for_fallback("Album Name (Rereleased)")
        self.assertEqual(result, "Album Name")
    
    def test_remove_dash_deluxe(self):
        """Test removal of - Deluxe Edition"""
        result = normalize_album_for_fallback("Album Name - Deluxe Edition")
        self.assertEqual(result, "Album Name")
    
    def test_preserve_regular_album(self):
        """Test that regular album names are preserved"""
        result = normalize_album_for_fallback("Regular Album Name")
        self.assertEqual(result, "Regular Album Name")
    
    def test_clean_empty_parentheses(self):
        """Test that empty parentheses are cleaned up"""
        # This could happen after removing edition markers
        result = normalize_album_for_fallback("Album Name ()")
        self.assertEqual(result, "Album Name")


class TestSpecialAlbumDetection(unittest.TestCase):
    """Test detection of special album types"""
    
    def test_detect_live_album(self):
        """Test detection of live albums"""
        self.assertTrue(is_special_album_type("Live at Madison Square Garden"))
        self.assertTrue(is_special_album_type("The LIVE Album"))
    
    def test_detect_symphony_album(self):
        """Test detection of symphony/symphonic albums"""
        self.assertTrue(is_special_album_type("Symphony No. 5"))
        self.assertTrue(is_special_album_type("Symphonic Rock"))
    
    def test_detect_acoustic_album(self):
        """Test detection of acoustic albums"""
        self.assertTrue(is_special_album_type("Acoustic Sessions"))
        self.assertTrue(is_special_album_type("The Acoustic Album"))
    
    def test_detect_unplugged_album(self):
        """Test detection of unplugged albums"""
        self.assertTrue(is_special_album_type("MTV Unplugged"))
        self.assertTrue(is_special_album_type("Unplugged in New York"))
    
    def test_regular_album(self):
        """Test that regular albums are not detected as special"""
        self.assertFalse(is_special_album_type("Regular Studio Album"))
        self.assertFalse(is_special_album_type("Greatest Hits"))


class TestYearExtraction(unittest.TestCase):
    """Test year extraction from date strings"""
    
    def test_extract_from_full_date(self):
        """Test extracting year from YYYY-MM-DD"""
        self.assertEqual(extract_year_from_date("2020-05-15"), 2020)
    
    def test_extract_from_year_month(self):
        """Test extracting year from YYYY-MM"""
        self.assertEqual(extract_year_from_date("2020-05"), 2020)
    
    def test_extract_from_year_only(self):
        """Test extracting year from YYYY"""
        self.assertEqual(extract_year_from_date("2020"), 2020)
    
    def test_extract_from_none(self):
        """Test extracting year from None"""
        self.assertIsNone(extract_year_from_date(None))
    
    def test_extract_from_empty_string(self):
        """Test extracting year from empty string"""
        self.assertIsNone(extract_year_from_date(""))


class TestTimeWindowValidation(unittest.TestCase):
    """Test time window validation logic"""
    
    def test_within_same_year(self):
        """Test tracks from same year as album"""
        self.assertTrue(is_within_time_window(2020, 2020, window_years=1))
    
    def test_within_one_year_before(self):
        """Test tracks from one year before album"""
        self.assertTrue(is_within_time_window(2019, 2020, window_years=1))
    
    def test_within_one_year_after(self):
        """Test tracks from one year after album"""
        self.assertTrue(is_within_time_window(2021, 2020, window_years=1))
    
    def test_outside_two_years(self):
        """Test tracks from two years away"""
        self.assertFalse(is_within_time_window(2018, 2020, window_years=1))
        self.assertFalse(is_within_time_window(2022, 2020, window_years=1))
    
    def test_unknown_year_allows_match(self):
        """Test that unknown years allow the match"""
        self.assertTrue(is_within_time_window(None, 2020, window_years=1))
        self.assertTrue(is_within_time_window(2020, None, window_years=1))
        self.assertTrue(is_within_time_window(None, None, window_years=1))


class TestTimeWindowRestriction(unittest.TestCase):
    """Test time window restriction application"""
    
    def test_apply_to_live_album(self):
        """Test that restriction is applied to live albums"""
        should_restrict, is_within = should_apply_time_window_restriction(
            "Live at Wembley",
            "2020-05-15",
            "2019-06-01"
        )
        self.assertTrue(should_restrict)
        self.assertTrue(is_within)  # Within 1 year
    
    def test_apply_to_acoustic_album(self):
        """Test that restriction is applied to acoustic albums"""
        should_restrict, is_within = should_apply_time_window_restriction(
            "Acoustic Sessions",
            "2020-05-15",
            "2019-06-01"
        )
        self.assertTrue(should_restrict)
        self.assertTrue(is_within)
    
    def test_no_restriction_for_regular_album(self):
        """Test that restriction is not applied to regular albums"""
        should_restrict, is_within = should_apply_time_window_restriction(
            "Regular Album",
            "2020-05-15",
            "2015-06-01"
        )
        self.assertFalse(should_restrict)
        self.assertTrue(is_within)  # Should be True since no restriction
    
    def test_outside_window_for_live_album(self):
        """Test that restriction rejects tracks outside window"""
        should_restrict, is_within = should_apply_time_window_restriction(
            "Live Album",
            "2020-05-15",
            "2017-06-01"
        )
        self.assertTrue(should_restrict)
        self.assertFalse(is_within)  # Outside 1 year window


class TestAlbumMatchingWithFallback(unittest.TestCase):
    """Test album matching with fallback to original versions"""
    
    def test_exact_match(self):
        """Test exact album match"""
        candidates = ["Album One", "Album Two", "Album Three"]
        result = match_album_with_fallback("Album Two", candidates)
        self.assertEqual(result, "Album Two")
    
    def test_fallback_deluxe_to_original(self):
        """Test matching Deluxe edition to original"""
        candidates = ["Album Name", "Other Album"]
        result = match_album_with_fallback("Album Name (Deluxe Edition)", candidates)
        self.assertEqual(result, "Album Name")
    
    def test_fallback_remastered_to_original(self):
        """Test matching Remastered edition to original"""
        candidates = ["Classic Album", "Other Album"]
        result = match_album_with_fallback("Classic Album (Remastered)", candidates)
        self.assertEqual(result, "Classic Album")
    
    def test_no_match_found(self):
        """Test when no match is found"""
        candidates = ["Album One", "Album Two"]
        result = match_album_with_fallback("Album Three", candidates)
        self.assertIsNone(result)
    
    def test_case_insensitive_matching(self):
        """Test case-insensitive matching"""
        candidates = ["album name", "other album"]
        result = match_album_with_fallback("Album Name", candidates)
        self.assertEqual(result, "album name")


if __name__ == "__main__":
    unittest.main()
