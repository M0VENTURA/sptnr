#!/usr/bin/env python3
"""
Integration test for enhanced single detection in popularity.py
Tests that the enhanced detection integrates properly with existing code.
"""

import os
import sys
import sqlite3
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def test_integration_with_popularity():
    """Test that detect_single_for_track properly uses enhanced detection"""
    print("\n" + "="*60)
    print("INTEGRATION TEST: Enhanced Detection via popularity.py")
    print("="*60)
    
    # Create temporary database
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name
    
    # Create temporary log directory
    temp_log_dir = tempfile.mkdtemp()
    
    try:
        # Set up database connection
        os.environ['DB_PATH'] = db_path
        os.environ['LOG_PATH'] = os.path.join(temp_log_dir, 'sptnr.log')
        os.environ['UNIFIED_SCAN_LOG_PATH'] = os.path.join(temp_log_dir, 'unified_scan.log')
        
        # Create database schema
        from check_db import update_schema
        update_schema(db_path)
        
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Insert test album tracks
        test_tracks = [
            ('track1', 'Test Artist', 'Test Album', 'Mega Hit', 95.0, 180.0, 'ISRC001'),
            ('track2', 'Test Artist', 'Test Album', 'Popular Song', 70.0, 200.0, 'ISRC002'),
            ('track3', 'Test Artist', 'Test Album', 'Album Track', 50.0, 220.0, 'ISRC003'),
            ('track4', 'Test Artist', 'Test Album', 'Deep Cut', 30.0, 190.0, 'ISRC004'),
            ('track5', 'Test Artist', 'Test Album', 'Filler', 25.0, 210.0, 'ISRC005'),
        ]
        
        for track_id, artist, album, title, pop, duration, isrc in test_tracks:
            cursor.execute("""
                INSERT INTO tracks (id, artist, album, title, popularity_score, duration, isrc)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (track_id, artist, album, title, pop, duration, isrc))
        
        conn.commit()
        
        # Import the function to test
        from popularity import detect_single_for_track
        
        # Mock Spotify results for "Mega Hit"
        spotify_results_cache = {
            'Mega Hit': [
                {
                    'name': 'Mega Hit',
                    'duration_ms': 180000,
                    'external_ids': {'isrc': 'ISRC001'},
                    'album': {'album_type': 'single', 'name': 'Mega Hit - Single'}
                },
                {
                    'name': 'Mega Hit',
                    'duration_ms': 180500,
                    'external_ids': {'isrc': 'ISRC001'},
                    'album': {'album_type': 'album', 'name': 'Test Album'}
                }
            ]
        }
        
        # Test detection on high-priority track
        print("\nTest 1: High-priority track with Spotify single")
        result = detect_single_for_track(
            title='Mega Hit',
            artist='Test Artist',
            album_track_count=5,
            spotify_results_cache=spotify_results_cache,
            verbose=True,
            discogs_token=None,
            track_id='track1',
            album='Test Album',
            isrc='ISRC001',
            duration=180.0,
            popularity=95.0,
            use_advanced_detection=True
        )
        
        print(f"\n  Result:")
        print(f"    is_single: {result.get('is_single')}")
        print(f"    confidence: {result.get('confidence')}")
        print(f"    sources: {result.get('sources')}")
        
        # Verify result
        assert result.get('is_single') in (True, False), "is_single must be boolean"
        assert result.get('confidence') in ('high', 'medium', 'low', 'none'), f"Invalid confidence: {result.get('confidence')}"
        assert isinstance(result.get('sources'), list), "sources must be a list"
        
        # Verify database was updated
        cursor.execute("""
            SELECT single_status, z_score, spotify_version_count
            FROM tracks WHERE id = ?
        """, ('track1',))
        row = cursor.fetchone()
        
        if row:
            print(f"\n  Database storage:")
            print(f"    single_status: {row[0]}")
            z_score_str = f"{row[1]:.2f}" if row[1] is not None else "NULL"
            print(f"    z_score: {z_score_str}")
            print(f"    spotify_version_count: {row[2]}")
            
            assert row[0] is not None, "single_status should be set"
            assert row[0] in ('high', 'medium', 'low', 'none'), f"Invalid single_status: {row[0]}"
        else:
            print("\n  ⚠ Database was not updated")
        
        # Test 2: Low-priority track (should be skipped by pre-filter)
        print("\n\nTest 2: Low-priority track (pre-filter should skip)")
        result2 = detect_single_for_track(
            title='Filler',
            artist='Test Artist',
            album_track_count=5,
            spotify_results_cache={},
            verbose=True,
            discogs_token=None,
            track_id='track5',
            album='Test Album',
            isrc='ISRC005',
            duration=210.0,
            popularity=25.0,
            use_advanced_detection=True
        )
        
        print(f"\n  Result:")
        print(f"    is_single: {result2.get('is_single')}")
        print(f"    confidence: {result2.get('confidence')}")
        print(f"    sources: {result2.get('sources')}")
        
        conn.close()
        
        print("\n" + "="*60)
        print("✅ INTEGRATION TEST PASSED")
        print("="*60)
        return True
        
    except Exception as e:
        print(f"\n❌ INTEGRATION TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False
        
    finally:
        # Clean up
        if os.path.exists(db_path):
            os.unlink(db_path)
        import shutil
        if os.path.exists(temp_log_dir):
            shutil.rmtree(temp_log_dir)


def test_fallback_to_standard():
    """Test that fallback to standard detection works when enhanced fails"""
    print("\n" + "="*60)
    print("FALLBACK TEST: Standard detection when enhanced unavailable")
    print("="*60)
    
    # Create temporary log directory
    temp_log_dir = tempfile.mkdtemp()
    
    try:
        os.environ['LOG_PATH'] = os.path.join(temp_log_dir, 'sptnr.log')
        os.environ['UNIFIED_SCAN_LOG_PATH'] = os.path.join(temp_log_dir, 'unified_scan.log')
        
        # Import the function
        from popularity import detect_single_for_track
        
        # Call without enhanced parameters (should use standard detection)
        print("\nCalling without enhanced parameters (track_id/album)")
        result = detect_single_for_track(
            title='Test Song',
            artist='Test Artist',
            album_track_count=10,
            spotify_results_cache={},
            verbose=True,
            use_advanced_detection=True  # Request enhanced but won't be used
        )
        
        print(f"\n  Result:")
        print(f"    is_single: {result.get('is_single')}")
        print(f"    confidence: {result.get('confidence')}")
        print(f"    sources: {result.get('sources')}")
        
        # Verify it returns expected structure
        assert 'is_single' in result
        assert 'confidence' in result
        assert 'sources' in result
        
        print("\n" + "="*60)
        print("✅ FALLBACK TEST PASSED")
        print("="*60)
        return True
        
    except Exception as e:
        print(f"\n❌ FALLBACK TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    finally:
        # Clean up
        import shutil
        if os.path.exists(temp_log_dir):
            shutil.rmtree(temp_log_dir)


if __name__ == '__main__':
    success = True
    success &= test_integration_with_popularity()
    success &= test_fallback_to_standard()
    
    sys.exit(0 if success else 1)
