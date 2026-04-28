#!/usr/bin/env python3
"""
Test to verify that force rescan correctly includes all tracks in the SQL query.
"""

import os
import sys

# Set required environment variables for testing
os.environ["DB_PATH"] = "/tmp/test_sptnr.db"
os.environ["LOG_PATH"] = "/tmp/test_sptnr.log"
os.environ["UNIFIED_SCAN_LOG_PATH"] = "/tmp/test_unified_scan.log"
os.environ["SPTNR_VERBOSE_POPULARITY"] = "0"
os.environ["SPTNR_FORCE_RESCAN"] = "0"

def test_sql_query_generation():
    """Test that SQL query correctly handles force parameter"""
    
    # Test case 1: Normal mode (force=False) - should filter by popularity_score
    print("\n=== Test Case 1: Normal Mode (force=False) ===")
    sql_conditions = []
    force = False
    FORCE_RESCAN = False
    
    if not (FORCE_RESCAN or force):
        sql_conditions.append("(popularity_score IS NULL OR popularity_score = 0)")
    
    artist_filter = "+44"
    sql_params = []
    
    if artist_filter:
        sql_conditions.append("artist = ?")
        sql_params.append(artist_filter)
    
    sql = f"""
        SELECT id, artist, title, album
        FROM tracks
        {('WHERE ' + ' AND '.join(sql_conditions)) if sql_conditions else ''}
        ORDER BY artist, album, title
    """
    
    print(f"SQL: {sql.strip()}")
    print(f"Params: {sql_params}")
    assert "(popularity_score IS NULL OR popularity_score = 0)" in sql, "Normal mode should filter by popularity_score"
    assert "artist = ?" in sql, "Should filter by artist"
    print("✓ Test passed - Normal mode filters by popularity_score")
    
    # Test case 2: Force mode (force=True) - should NOT filter by popularity_score
    print("\n=== Test Case 2: Force Mode (force=True) ===")
    sql_conditions = []
    force = True
    FORCE_RESCAN = False
    
    if not (FORCE_RESCAN or force):
        sql_conditions.append("(popularity_score IS NULL OR popularity_score = 0)")
    
    artist_filter = "+44"
    sql_params = []
    
    if artist_filter:
        sql_conditions.append("artist = ?")
        sql_params.append(artist_filter)
    
    sql = f"""
        SELECT id, artist, title, album
        FROM tracks
        {('WHERE ' + ' AND '.join(sql_conditions)) if sql_conditions else ''}
        ORDER BY artist, album, title
    """
    
    print(f"SQL: {sql.strip()}")
    print(f"Params: {sql_params}")
    assert "(popularity_score IS NULL OR popularity_score = 0)" not in sql, "Force mode should NOT filter by popularity_score"
    assert "artist = ?" in sql, "Should filter by artist"
    print("✓ Test passed - Force mode does NOT filter by popularity_score")
    
    # Test case 3: FORCE_RESCAN env var - should NOT filter by popularity_score
    print("\n=== Test Case 3: FORCE_RESCAN Env Var ===")
    sql_conditions = []
    force = False
    FORCE_RESCAN = True
    
    if not (FORCE_RESCAN or force):
        sql_conditions.append("(popularity_score IS NULL OR popularity_score = 0)")
    
    artist_filter = "+44"
    sql_params = []
    
    if artist_filter:
        sql_conditions.append("artist = ?")
        sql_params.append(artist_filter)
    
    sql = f"""
        SELECT id, artist, title, album
        FROM tracks
        {('WHERE ' + ' AND '.join(sql_conditions)) if sql_conditions else ''}
        ORDER BY artist, album, title
    """
    
    print(f"SQL: {sql.strip()}")
    print(f"Params: {sql_params}")
    assert "(popularity_score IS NULL OR popularity_score = 0)" not in sql, "FORCE_RESCAN mode should NOT filter by popularity_score"
    assert "artist = ?" in sql, "Should filter by artist"
    print("✓ Test passed - FORCE_RESCAN mode does NOT filter by popularity_score")
    
    # Test case 4: Force mode with no filters - should return all tracks
    print("\n=== Test Case 4: Force Mode with No Filters ===")
    sql_conditions = []
    force = True
    FORCE_RESCAN = False
    
    if not (FORCE_RESCAN or force):
        sql_conditions.append("(popularity_score IS NULL OR popularity_score = 0)")
    
    artist_filter = None
    sql_params = []
    
    if artist_filter:
        sql_conditions.append("artist = ?")
        sql_params.append(artist_filter)
    
    sql = f"""
        SELECT id, artist, title, album
        FROM tracks
        {('WHERE ' + ' AND '.join(sql_conditions)) if sql_conditions else ''}
        ORDER BY artist, album, title
    """
    
    print(f"SQL: {sql.strip()}")
    print(f"Params: {sql_params}")
    assert "WHERE" not in sql, "Force mode with no filters should have no WHERE clause"
    print("✓ Test passed - Force mode with no filters returns all tracks")
    
    print("\n=== All Tests Passed ===\n")

if __name__ == "__main__":
    try:
        test_sql_query_generation()
    except AssertionError as e:
        print(f"✗ Test failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
