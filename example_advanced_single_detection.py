#!/usr/bin/env python3
"""
Example script demonstrating advanced single detection usage.

This script shows how to:
1. Use advanced single detection for a single track
2. Batch update an entire artist's discography
3. Customize the z-score threshold
"""

import os
import sys
import sqlite3

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from advanced_single_detection import (
    detect_single_advanced,
    batch_update_advanced_singles
)


def example_single_track():
    """Example 1: Detect singles for a single track"""
    print("\n" + "="*60)
    print("EXAMPLE 1: Single Track Detection")
    print("="*60)
    
    # Connect to database
    db_path = os.environ.get("DB_PATH", "sptnr.db")
    conn = sqlite3.connect(db_path)
    
    # Example track parameters
    result = detect_single_advanced(
        conn=conn,
        track_id="example123",
        title="Bohemian Rhapsody",
        artist="Queen",
        album="A Night at the Opera",
        isrc="GBUM71029604",  # Real ISRC for this track
        duration=354.0,  # 5:54 in seconds
        popularity=85.0,
        album_type="album",
        zscore_threshold=0.20,
        verbose=True
    )
    
    print(f"\nResults:")
    print(f"  Is Single: {result['is_single']}")
    print(f"  Confidence: {result['confidence']}")
    print(f"  Sources: {', '.join(result['sources'])}")
    print(f"  Global Popularity: {result['global_popularity']:.1f}")
    print(f"  Z-Score: {result['zscore']:.3f}")
    print(f"  Metadata Single: {result['metadata_single']}")
    print(f"  Is Compilation: {result['is_compilation']}")
    
    conn.close()


def example_batch_artist():
    """Example 2: Batch update all tracks for an artist"""
    print("\n" + "="*60)
    print("EXAMPLE 2: Batch Update Artist")
    print("="*60)
    
    # Connect to database
    db_path = os.environ.get("DB_PATH", "sptnr.db")
    conn = sqlite3.connect(db_path)
    
    # Update all tracks for an artist
    artist_name = "Queen"
    print(f"\nUpdating all tracks for: {artist_name}")
    
    num_updated = batch_update_advanced_singles(
        conn=conn,
        artist=artist_name,
        zscore_threshold=0.20,
        verbose=False  # Set to True for detailed logging
    )
    
    print(f"✅ Updated {num_updated} tracks")
    
    # Show some results
    cursor = conn.cursor()
    cursor.execute("""
        SELECT title, is_single, single_confidence, global_popularity, zscore
        FROM tracks
        WHERE artist = ?
        ORDER BY global_popularity DESC
        LIMIT 10
    """, (artist_name,))
    
    print(f"\nTop 10 tracks by global popularity:")
    print(f"{'Title':<40} {'Single':<8} {'Conf':<8} {'Pop':<8} {'Z-Score':<8}")
    print("-" * 80)
    
    for row in cursor.fetchall():
        title, is_single, conf, pop, zscore = row
        single_mark = "✓" if is_single else "-"
        print(f"{title[:40]:<40} {single_mark:<8} {conf or 'N/A':<8} {pop or 0:<8.1f} {zscore or 0:<8.3f}")
    
    conn.close()


def example_custom_threshold():
    """Example 3: Use custom z-score threshold"""
    print("\n" + "="*60)
    print("EXAMPLE 3: Custom Z-Score Threshold")
    print("="*60)
    
    # Connect to database
    db_path = os.environ.get("DB_PATH", "sptnr.db")
    conn = sqlite3.connect(db_path)
    
    # Try different thresholds
    thresholds = [0.10, 0.20, 0.30, 0.50]
    
    print("\nComparing detection with different thresholds:")
    print("(Using example track with metadata single status)")
    
    for threshold in thresholds:
        result = detect_single_advanced(
            conn=conn,
            track_id="example123",
            title="Example Single",
            artist="Example Artist",
            album="Example Album",
            isrc="USXXX1234567",
            duration=180.0,
            popularity=75.0,
            album_type="album",
            zscore_threshold=threshold,
            verbose=False
        )
        
        print(f"\n  Threshold: {threshold:.2f}")
        print(f"    Is Single: {result['is_single']}")
        print(f"    Confidence: {result['confidence']}")
    
    print("\nNote: Higher thresholds = stricter detection (fewer singles)")
    print("      Lower thresholds = more lenient (more singles)")
    
    conn.close()


def example_query_results():
    """Example 4: Query and display detection results"""
    print("\n" + "="*60)
    print("EXAMPLE 4: Query Detection Results")
    print("="*60)
    
    # Connect to database
    db_path = os.environ.get("DB_PATH", "sptnr.db")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Find all detected singles
    cursor.execute("""
        SELECT artist, title, global_popularity, zscore, single_confidence
        FROM tracks
        WHERE is_single = 1
        ORDER BY global_popularity DESC
        LIMIT 20
    """)
    
    print("\nTop 20 Detected Singles:")
    print(f"{'Artist':<30} {'Title':<40} {'Pop':<8} {'Z-Score':<10} {'Conf':<8}")
    print("-" * 100)
    
    for row in cursor.fetchall():
        artist, title, pop, zscore, conf = row
        print(f"{artist[:30]:<30} {title[:40]:<40} {pop or 0:<8.1f} {zscore or 0:<10.3f} {conf or 'N/A':<8}")
    
    # Find tracks with high z-scores but not marked as singles
    cursor.execute("""
        SELECT artist, title, global_popularity, zscore, metadata_single
        FROM tracks
        WHERE zscore >= 0.50 AND is_single = 0
        ORDER BY zscore DESC
        LIMIT 10
    """)
    
    print("\nHigh Z-Score tracks NOT marked as singles:")
    print("(These may be popular album tracks without single releases)")
    print(f"{'Artist':<30} {'Title':<40} {'Pop':<8} {'Z-Score':<10} {'Metadata':<10}")
    print("-" * 105)
    
    for row in cursor.fetchall():
        artist, title, pop, zscore, meta = row
        metadata = "Yes" if meta else "No"
        print(f"{artist[:30]:<30} {title[:40]:<40} {pop or 0:<8.1f} {zscore or 0:<10.3f} {metadata:<10}")
    
    conn.close()


def main():
    """Run all examples"""
    print("\n" + "="*60)
    print("ADVANCED SINGLE DETECTION - USAGE EXAMPLES")
    print("="*60)
    
    print("\nNote: These examples require a populated sptnr.db database.")
    print("Run the popularity scanner first to populate track data.")
    
    # Check if database exists
    db_path = os.environ.get("DB_PATH", "sptnr.db")
    if not os.path.exists(db_path):
        print(f"\n❌ Database not found: {db_path}")
        print("Please run the main application first to create the database.")
        return 1
    
    try:
        # Run examples
        example_single_track()
        example_batch_artist()
        example_custom_threshold()
        example_query_results()
        
        print("\n" + "="*60)
        print("✅ All examples completed successfully!")
        print("="*60)
        
        return 0
        
    except Exception as e:
        print(f"\n❌ Error running examples: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
