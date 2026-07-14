#!/usr/bin/env python3
"""
Tests for Scan Resume Module
=============================

Tests the auto-resume functionality for interrupted scans.
"""

import os
import sys
import json
import unittest
import tempfile
import shutil
from datetime import datetime, timedelta

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scan_resume import (
    load_scan_progress,
    save_scan_progress,
    detect_interrupted_scan,
    get_artists_to_scan
)


class TestProgressFileOperations(unittest.TestCase):
    """Test basic progress file operations"""
    
    def setUp(self):
        """Create a temporary directory for test files"""
        self.test_dir = tempfile.mkdtemp()
        self.progress_file = os.path.join(self.test_dir, "test_progress.json")
        # Set environment variable for testing
        os.environ["NAVIDROME_PROGRESS_FILE"] = self.progress_file
    
    def tearDown(self):
        """Clean up test directory"""
        shutil.rmtree(self.test_dir)
    
    def test_save_and_load_progress(self):
        """Test saving and loading progress"""
        progress_data = {
            "current_artist": "Test Artist",
            "processed_artists": 50,
            "total_artists": 100,
            "is_running": True,
            "percent_complete": 50,
            "last_updated": datetime.now().isoformat()
        }
        
        # Save progress
        result = save_scan_progress("navidrome", progress_data)
        self.assertTrue(result)
        
        # Load progress
        loaded = load_scan_progress("navidrome")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["current_artist"], "Test Artist")
        self.assertEqual(loaded["processed_artists"], 50)
        self.assertEqual(loaded["total_artists"], 100)
    
    def test_load_nonexistent_progress(self):
        """Test loading progress when file doesn't exist"""
        # Use a different file path
        os.environ["NAVIDROME_PROGRESS_FILE"] = "/tmp/nonexistent_progress.json"
        result = load_scan_progress("navidrome")
        self.assertIsNone(result)


class TestInterruptedScanDetection(unittest.TestCase):
    """Test interrupted scan detection"""
    
    def setUp(self):
        """Create a temporary directory for test files"""
        self.test_dir = tempfile.mkdtemp()
        self.progress_file = os.path.join(self.test_dir, "test_progress.json")
        os.environ["NAVIDROME_PROGRESS_FILE"] = self.progress_file
    
    def tearDown(self):
        """Clean up test directory"""
        shutil.rmtree(self.test_dir)
    
    def test_detect_recent_interrupted_scan(self):
        """Test detection of recently interrupted scan"""
        progress_data = {
            "current_artist": "Test Artist",
            "processed_artists": 50,
            "total_artists": 100,
            "is_running": True,
            "percent_complete": 50,
            "last_updated": datetime.now().isoformat()
        }
        
        save_scan_progress("navidrome", progress_data)
        
        # Should detect interrupted scan
        result = detect_interrupted_scan("navidrome")
        self.assertIsNotNone(result)
        self.assertEqual(result["current_artist"], "Test Artist")
    
    def test_dont_detect_old_scan(self):
        """Test that old scans are not resumed"""
        # Create an old progress file (25 hours ago)
        old_time = datetime.now() - timedelta(hours=25)
        progress_data = {
            "current_artist": "Test Artist",
            "processed_artists": 50,
            "total_artists": 100,
            "is_running": True,
            "percent_complete": 50,
            "last_updated": old_time.isoformat()
        }
        
        save_scan_progress("navidrome", progress_data)
        
        # Should not detect as resumable (too old)
        result = detect_interrupted_scan("navidrome")
        self.assertIsNone(result)
    
    def test_dont_detect_completed_scan(self):
        """Test that completed scans are not resumed"""
        progress_data = {
            "current_artist": "Test Artist",
            "processed_artists": 100,
            "total_artists": 100,
            "is_running": False,
            "percent_complete": 100,
            "last_updated": datetime.now().isoformat()
        }
        
        save_scan_progress("navidrome", progress_data)
        
        # Should not detect (not running)
        result = detect_interrupted_scan("navidrome")
        self.assertIsNone(result)


class TestArtistListResume(unittest.TestCase):
    """Test artist list resume logic"""
    
    def test_get_all_artists_when_no_resume(self):
        """Test getting all artists when not resuming"""
        all_artists = ["Artist 1", "Artist 2", "Artist 3", "Artist 4"]
        result = get_artists_to_scan(all_artists, None)
        self.assertEqual(result, all_artists)
    
    def test_resume_from_middle(self):
        """Test resuming from middle of artist list"""
        all_artists = ["Artist 1", "Artist 2", "Artist 3", "Artist 4"]
        # Resume from Artist 2 (should skip Artist 1 and 2, start from Artist 3)
        result = get_artists_to_scan(all_artists, "Artist 2")
        self.assertEqual(result, ["Artist 3", "Artist 4"])
    
    def test_resume_from_first(self):
        """Test resuming from first artist"""
        all_artists = ["Artist 1", "Artist 2", "Artist 3", "Artist 4"]
        result = get_artists_to_scan(all_artists, "Artist 1")
        self.assertEqual(result, ["Artist 2", "Artist 3", "Artist 4"])
    
    def test_resume_from_last(self):
        """Test resuming from last artist (should have empty list)"""
        all_artists = ["Artist 1", "Artist 2", "Artist 3", "Artist 4"]
        result = get_artists_to_scan(all_artists, "Artist 4")
        self.assertEqual(result, [])
    
    def test_resume_from_nonexistent_artist(self):
        """Test resuming from artist not in list (should return all)"""
        all_artists = ["Artist 1", "Artist 2", "Artist 3", "Artist 4"]
        result = get_artists_to_scan(all_artists, "Nonexistent Artist")
        self.assertEqual(result, all_artists)


if __name__ == "__main__":
    unittest.main()
