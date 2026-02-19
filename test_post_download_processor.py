#!/usr/bin/env python3
"""
Tests for Post-Download Processor
Tests automatic metadata update, file renaming, and organization
"""

import os
import sys
import sqlite3
import tempfile
import shutil
from pathlib import Path
import unittest

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from post_download_processor import (
    sanitize_filename,
    update_file_metadata,
    rename_and_move_file,
    process_completed_queue_item,
    process_pending_completed_items
)


class TestPostDownloadProcessor(unittest.TestCase):
    """Test post-download processing functionality"""
    
    def setUp(self):
        """Set up test environment"""
        # Create temporary directories
        self.temp_dir = tempfile.mkdtemp()
        self.downloads_dir = os.path.join(self.temp_dir, 'downloads')
        self.music_dir = os.path.join(self.temp_dir, 'music')
        self.db_path = os.path.join(self.temp_dir, 'test.db')
        
        os.makedirs(self.downloads_dir)
        os.makedirs(self.music_dir)
        
        # Set environment variables
        os.environ['DOWNLOADS_DIR'] = self.downloads_dir
        os.environ['MUSIC_ROOT'] = self.music_dir
        os.environ['DB_PATH'] = self.db_path
        
        # Create test database
        self._create_test_database()
        
        # Create test MP3 file
        self.test_file = self._create_test_mp3()
    
    def tearDown(self):
        """Clean up test environment"""
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def _create_test_database(self):
        """Create test database with download_queue table"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE download_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                artist TEXT NOT NULL,
                title TEXT NOT NULL,
                album TEXT,
                search_query TEXT,
                source TEXT DEFAULT 'soulseek',
                status TEXT DEFAULT 'queued',
                file_path TEXT,
                track_number TEXT,
                album_artist TEXT,
                year TEXT,
                release_id TEXT,
                release_source TEXT,
                imported_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        conn.commit()
        conn.close()
    
    def _create_test_mp3(self):
        """Create a minimal test MP3 file"""
        # Create a minimal valid MP3 file with ID3 tag
        test_file = os.path.join(self.downloads_dir, 'test_track.mp3')
        
        # MP3 header (minimal valid MP3 frame)
        mp3_header = bytes([
            0xFF, 0xFB, 0x90, 0x00,  # MP3 sync word and header
            0x00, 0x00, 0x00, 0x00,
            0x00, 0x00, 0x00, 0x00
        ])
        
        # Write ID3v2 tag header
        with open(test_file, 'wb') as f:
            # ID3v2 header
            f.write(b'ID3')  # ID3v2 identifier
            f.write(b'\x04\x00')  # Version 2.4.0
            f.write(b'\x00')  # Flags
            f.write(b'\x00\x00\x00\x00')  # Size (syncsafe int)
            
            # Write minimal MP3 frame
            f.write(mp3_header * 100)  # Make it a bit larger
        
        return test_file
    
    def test_sanitize_filename(self):
        """Test filename sanitization"""
        # Test invalid characters
        self.assertEqual(sanitize_filename('test<file>name'), 'test_file_name')
        self.assertEqual(sanitize_filename('test:file|name'), 'test_file_name')
        self.assertEqual(sanitize_filename('test"file*name'), 'test_file_name')
        
        # Test leading/trailing spaces and dots
        self.assertEqual(sanitize_filename('  test  '), 'test')
        self.assertEqual(sanitize_filename('..test..'), 'test')
    
    def test_rename_and_move_file_basic(self):
        """Test basic file renaming and moving"""
        metadata = {
            'track_number': '01',
            'artist': 'Test Artist',
            'album_artist': 'Test Artist',
            'album': 'Test Album',
            'year': '2023',
            'title': 'Test Track'
        }
        
        result = rename_and_move_file(self.test_file, metadata)
        
        self.assertTrue(result['success'])
        self.assertIsNotNone(result['target_path'])
        
        # Check that file was moved to correct location
        expected_dir = os.path.join(self.music_dir, 'Test Artist', '2023 - Test Album')
        self.assertTrue(os.path.exists(expected_dir))
        
        # Check filename format
        expected_filename = '01. Test Artist - Test Track.mp3'
        expected_path = os.path.join(expected_dir, expected_filename)
        self.assertEqual(result['target_path'], expected_path)
        self.assertTrue(os.path.exists(expected_path))
    
    def test_rename_and_move_file_different_album_artist(self):
        """Test file organization with different album artist"""
        metadata = {
            'track_number': '05',
            'artist': 'Track Artist',
            'album_artist': 'Album Artist',
            'album': 'Compilation Album',
            'year': '2024',
            'title': 'Track Title'
        }
        
        result = rename_and_move_file(self.test_file, metadata)
        
        self.assertTrue(result['success'])
        
        # Should use album_artist for folder structure
        expected_dir = os.path.join(self.music_dir, 'Album Artist', '2024 - Compilation Album')
        self.assertTrue(os.path.exists(expected_dir))
        
        # But use track artist in filename
        expected_filename = '05. Track Artist - Track Title.mp3'
        self.assertIn(expected_filename, result['target_path'])
    
    def test_rename_and_move_file_with_special_characters(self):
        """Test handling of special characters in metadata"""
        metadata = {
            'track_number': '03',
            'artist': 'Artist: With / Special * Characters',
            'album_artist': 'Artist: With / Special * Characters',
            'album': 'Album | With <Special> Characters',
            'year': '2022',
            'title': 'Track? With "Quotes"'
        }
        
        result = rename_and_move_file(self.test_file, metadata)
        
        self.assertTrue(result['success'])
        
        # Check that special characters were sanitized in the filename portion
        # (excluding directory separators which are valid)
        filename = os.path.basename(result['target_path'])
        self.assertNotIn('*', filename)
        self.assertNotIn('|', filename)
        self.assertNotIn('<', filename)
        self.assertNotIn('>', filename)
        self.assertNotIn(':', filename)
        self.assertNotIn('"', filename)
        self.assertNotIn('?', filename)
    
    def test_rename_and_move_file_duplicate_handling(self):
        """Test handling of duplicate filenames"""
        metadata = {
            'track_number': '01',
            'artist': 'Test Artist',
            'album_artist': 'Test Artist',
            'album': 'Test Album',
            'year': '2023',
            'title': 'Test Track'
        }
        
        # First move
        result1 = rename_and_move_file(self.test_file, metadata)
        self.assertTrue(result1['success'])
        
        # Create another test file
        test_file2 = self._create_test_mp3()
        
        # Second move (should handle duplicate)
        result2 = rename_and_move_file(test_file2, metadata)
        self.assertTrue(result2['success'])
        
        # Should create a different filename
        self.assertNotEqual(result1['target_path'], result2['target_path'])
        self.assertTrue('_1' in result2['target_path'] or result1['target_path'] == result2['target_path'])
    
    def test_process_completed_queue_item_with_metadata(self):
        """Test processing a completed queue item with metadata"""
        # Add queue item to database
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO download_queue 
            (artist, title, album, status, file_path, track_number, album_artist, year, release_id, release_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ('Test Artist', 'Test Track', 'Test Album', 'completed', self.test_file,
              '01', 'Test Artist', '2023', 'mb-12345', 'musicbrainz'))
        
        queue_id = cursor.lastrowid
        conn.commit()
        
        # Get queue item
        cursor.execute("SELECT * FROM download_queue WHERE id = ?", (queue_id,))
        row = cursor.fetchone()
        queue_item = dict(row)
        conn.close()
        
        # Process item
        result = process_completed_queue_item(queue_item)
        
        self.assertTrue(result['success'])
        self.assertIsNotNone(result['target_path'])
        self.assertTrue(os.path.exists(result['target_path']))
    
    def test_process_completed_queue_item_without_metadata(self):
        """Test processing a completed queue item without release metadata"""
        # Add queue item without metadata
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO download_queue 
            (artist, title, album, status, file_path)
            VALUES (?, ?, ?, ?, ?)
        """, ('Test Artist', 'Test Track', 'Test Album', 'completed', self.test_file))
        
        queue_id = cursor.lastrowid
        conn.commit()
        
        # Get queue item
        cursor.execute("SELECT * FROM download_queue WHERE id = ?", (queue_id,))
        row = cursor.fetchone()
        queue_item = dict(row)
        conn.close()
        
        # Process item
        result = process_completed_queue_item(queue_item)
        
        # Should skip processing
        self.assertFalse(result['success'])
        self.assertEqual(result['message'], 'No metadata available')
    
    def test_process_pending_completed_items(self):
        """Test batch processing of pending completed items"""
        # Add multiple queue items
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        for i in range(3):
            test_file = self._create_test_mp3()
            test_file_renamed = os.path.join(self.downloads_dir, f'track_{i}.mp3')
            os.rename(test_file, test_file_renamed)
            
            cursor.execute("""
                INSERT INTO download_queue 
                (artist, title, album, status, file_path, track_number, album_artist, year, release_id, release_source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (f'Artist {i}', f'Track {i}', 'Test Album', 'completed', test_file_renamed,
                  f'{i+1:02d}', 'Various Artists', '2023', f'mb-{i}', 'musicbrainz'))
        
        conn.commit()
        conn.close()
        
        # Process pending items
        stats = process_pending_completed_items(limit=10)
        
        self.assertEqual(stats['processed'], 3)
        self.assertEqual(stats['failed'], 0)
        self.assertEqual(stats['skipped'], 0)
    
    def test_process_pending_items_with_missing_files(self):
        """Test handling of items with missing files"""
        # Add queue item with non-existent file
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO download_queue 
            (artist, title, album, status, file_path, track_number, album_artist, year, release_id, release_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ('Test Artist', 'Test Track', 'Test Album', 'completed', '/nonexistent/file.mp3',
              '01', 'Test Artist', '2023', 'mb-12345', 'musicbrainz'))
        
        conn.commit()
        conn.close()
        
        # Process pending items
        stats = process_pending_completed_items(limit=10)
        
        # Should fail gracefully
        self.assertEqual(stats['failed'], 1)
        self.assertGreater(len(stats['errors']), 0)


if __name__ == '__main__':
    # Run tests
    unittest.main(verbosity=2)
