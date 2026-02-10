#!/usr/bin/env python3
"""Debug script to check artist album count issues"""
import sqlite3
import sys

DB_PATH = "spotify_popularity.db"
DB_TIMEOUT = 120.0

def debug_artist_albums(artist_name):
    """Debug why album count is showing as 0"""
    conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    print(f"\n{'='*70}")
    print(f"Debugging artist: {artist_name}")
    print(f"{'='*70}\n")
    
    # Check if artist has any tracks at all
    cursor.execute("SELECT COUNT(*) FROM tracks WHERE COALESCE(album_artist, artist) = ?", (artist_name,))
    total_tracks = cursor.fetchone()[0]
    print(f"✓ Total tracks (COALESCE logic): {total_tracks}")
    
    cursor.execute("SELECT COUNT(*) FROM tracks WHERE artist = ?", (artist_name,))
    tracks_by_artist = cursor.fetchone()[0]
    print(f"✓ Tracks by artist column: {tracks_by_artist}")
    
    cursor.execute("SELECT COUNT(*) FROM tracks WHERE album_artist = ?", (artist_name,))
    tracks_by_album_artist = cursor.fetchone()[0]
    print(f"✓ Tracks by album_artist column: {tracks_by_album_artist}")
    
    # Check distinct albums
    cursor.execute("""
        SELECT COUNT(DISTINCT album) 
        FROM tracks 
        WHERE COALESCE(album_artist, artist) = ?
    """, (artist_name,))
    distinct_albums = cursor.fetchone()[0]
    print(f"✓ Distinct albums (COALESCE logic): {distinct_albums}")
    
    # List all albums and their track counts
    print(f"\n📀 Albums by this artist:")
    print(f"{'-'*70}")
    cursor.execute("""
        SELECT 
            album,
            COUNT(*) as track_count,
            AVG(stars) as avg_stars,
            MAX(spotify_album_type) as album_type
        FROM tracks
        WHERE COALESCE(album_artist, artist) = ?
        GROUP BY album
        ORDER BY album
    """, (artist_name,))
    
    albums = cursor.fetchall()
    if albums:
        for album in albums:
            print(f"  • {album['album']}")
            print(f"    ├─ Tracks: {album['track_count']}")
            print(f"    ├─ Avg Rating: {album['avg_stars']:.2f if album['avg_stars'] else 'N/A'}")
            print(f"    └─ Type: {album['album_type'] or 'Unknown'}")
    else:
        print("  (No albums found)")
    
    # Check raw tracks for this artist to understand the data structure
    print(f"\n🎵 Sample tracks:")
    print(f"{'-'*70}")
    cursor.execute("""
        SELECT 
            title,
            artist,
            album_artist,
            album,
            spotify_album_type,
            COALESCE(album_artist, artist) as coalesce_result
        FROM tracks
        WHERE COALESCE(album_artist, artist) = ?
        LIMIT 5
    """, (artist_name,))
    
    sample_tracks = cursor.fetchall()
    if sample_tracks:
        for track in sample_tracks:
            print(f"  • {track['title']}")
            print(f"    ├─ Artist: {track['artist']}")
            print(f"    ├─ Album Artist: {track['album_artist']}")
            print(f"    ├─ Album: {track['album']}")
            print(f"    ├─ Type: {track['spotify_album_type']}")
            print(f"    └─ COALESCE result: {track['coalesce_result']}")
    else:
        print("  (No tracks found)")
    
    # Check what the stats query returns
    print(f"\n📊 Stats query result:")
    print(f"{'-'*70}")
    try:
        cursor.execute("""
            SELECT 
                COUNT(*) as track_count,
                COUNT(DISTINCT album) as album_count,
                AVG(stars) as avg_stars,
                SUM(CASE WHEN stars = 5 THEN 1 ELSE 0 END) as five_star_count,
                MAX(beets_artist_mbid) as beets_artist_mbid,
                MAX(spotify_artist_id) as spotify_artist_id,
                MAX(discogs_release_id) as discogs_release_id
            FROM tracks
            WHERE COALESCE(album_artist, artist) = ?
        """, (artist_name,))
        stats = cursor.fetchone()
        if stats:
            print(f"  Track count: {stats['track_count']}")
            print(f"  Album count: {stats['album_count']}")
            print(f"  Avg rating: {stats['avg_stars']:.2f if stats['avg_stars'] else 'N/A'}")
            print(f"  5-star tracks: {stats['five_star_count']}")
            print(f"  Discogs release ID: {stats['discogs_release_id']}")
    except Exception as e:
        print(f"  (Error: {e})")
    
    conn.close()

def list_all_artists():
    """List all unique artists in database"""
    conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT)
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT DISTINCT COALESCE(album_artist, artist) as artist_name
        FROM tracks
        ORDER BY artist_name
    """)
    
    artists = [row[0] for row in cursor.fetchall()]
    conn.close()
    
    return artists

if __name__ == "__main__":
    if len(sys.argv) > 1:
        # Debug specific artist
        artist_name = " ".join(sys.argv[1:])
        debug_artist_albums(artist_name)
    else:
        # List all artists
        print("\n" + "="*70)
        print("Artists in database:")
        print("="*70 + "\n")
        
        artists = list_all_artists()
        if artists:
            for i, artist in enumerate(artists[:10], 1):
                print(f"{i:2}. {artist}")
            if len(artists) > 10:
                print(f"\n... and {len(artists) - 10} more")
            print(f"\nTotal artists: {len(artists)}")
            
            print("\n" + "-"*70)
            print("Usage: python debug_artist_albums.py 'Artist Name'")
            print("-"*70)
        else:
            print("No artists found in database")
