#!/usr/bin/env python3
"""
Diagnostic script to check Creed scan results.
Shows which songs have writer credits and which are detected as singles.
"""

from app import get_db

def check_creed_tracks():
    conn = get_db()
    cursor = conn.cursor()
    
    # First check what Creed-related artists exist
    cursor.execute("""
        SELECT DISTINCT artist
        FROM tracks
        WHERE LOWER(artist) LIKE '%creed%'
    """)
    
    artists = cursor.fetchall()
    if artists:
        print(f"\n📋 Found {len(artists)} artist(s) matching 'creed':")
        for artist in artists:
            print(f"   - {artist[0]}")
        print()
        
        # Use the first matching artist
        artist_name = artists[0][0]
    else:
        print("\n⚠️  No artists matching 'creed' found in database!")
        print("Checking all artists in database...")
        cursor.execute("SELECT DISTINCT artist FROM tracks LIMIT 10")
        sample_artists = cursor.fetchall()
        if sample_artists:
            print(f"\nSample of artists in database:")
            for artist in sample_artists:
                print(f"   - {artist[0]}")
        else:
            print("\n❌ Database appears to be empty!")
        conn.close()
        return
    
    # Get tracks for the found artist
    cursor.execute(f"""
        SELECT title, album, writer, is_single, single_sources, single_confidence, 
               spotify_popularity, lastfm_ratio, popularity_score
        FROM tracks
        WHERE artist = ?
        ORDER BY album, track_number
    """, (artist_name,))
    
    tracks = cursor.fetchall()
    
    print(f"\n📊 {artist_name} Scan Results ({len(tracks)} tracks found):\n")
    print("=" * 120)
    
    tracks_with_writer = 0
    tracks_marked_single = 0
    known_singles = ["Higher", "My Own Prison", "With Arms Wide Open", "One Last Breath", "My Sacrifice"]
    
    for idx, track in enumerate(tracks, 1):
        title = track[0]
        album = track[1]
        writer = track[2]
        is_single = track[3]
        single_sources = track[4]
        single_confidence = track[5]
        spotify_pop = track[6]
        lastfm_ratio = track[7]
        pop_score = track[8]
        
        # Count stats
        if writer:
            tracks_with_writer += 1
        if is_single:
            tracks_marked_single += 1
        
        # Identify known singles
        is_known_single = any(known in title for known in known_singles)
        single_marker = " ⭐ [KNOWN SINGLE]" if is_known_single else ""
        
        print(f"\n{idx}. {title} ({album}){single_marker}")
        print(f"   Writer: {writer if writer else '❌ MISSING'}")
        print(f"   Single: {'✅ Yes' if is_single else '❌ No'} | Sources: {single_sources or 'none'} | Confidence: {single_confidence or 'n/a'}")
        print(f"   Scores: Spotify={spotify_pop or 0}, LastFM={lastfm_ratio or 0:.1f}, Combined={pop_score or 0:.1f}")
        
        # Flag issues
        if is_known_single and not is_single:
            print(f"   ⚠️  WARNING: Known single not detected!")
        if not writer:
            print(f"   ⚠️  WARNING: Missing writer credits!")
    
    print("\n" + "=" * 120)
    print(f"\n📈 Summary:")
    print(f"   Total tracks: {len(tracks)}")
    print(f"   Tracks with writer credits: {tracks_with_writer} ({tracks_with_writer/len(tracks)*100:.1f}%)")
    print(f"   Tracks marked as singles: {tracks_marked_single}")
    print(f"   Known singles expected: {len(known_singles)}")
    
    conn.close()

if __name__ == "__main__":
    check_creed_tracks()
