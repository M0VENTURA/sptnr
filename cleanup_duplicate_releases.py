#!/usr/bin/env python3
"""
Cleanup duplicate album releases in upcoming_releases table.
Merges similar album names (e.g., "The Wilted EP" and "The Wilted EP(EP)") 
by keeping the record with the shorter/better name format.
"""
import sqlite3
import os
import re
from datetime import datetime

DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")
if not os.path.exists(DB_PATH):
    DB_PATH = "database.db"

def normalize_album_name(album_name: str) -> str:
    """Normalize album name by removing common suffixes."""
    if not album_name:
        return ""
    
    # Remove common suffixes in parentheses or brackets
    normalized = re.sub(r'\s*[\[\(](EP|LP|Album|Deluxe|Deluxe Edition|Remaster|Remastered|Extended|Single|Feat|feat|Featuring|feat\.|Bonus|Expanded|Anniversary|Edition|Mix|Unofficial|Limited|Special)[\]\)].*$', '', album_name, flags=re.IGNORECASE)
    
    # Also remove trailing (something) or [something] that remains
    normalized = re.sub(r'\s*[\[\(].*[\]\)]$', '', normalized)
    
    # Strip extra whitespace and convert to lowercase
    normalized = normalized.strip().lower()
    
    return normalized

def cleanup_duplicates():
    """Find and merge duplicate releases with normalized names."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Get all releases
        cursor.execute("SELECT * FROM upcoming_releases ORDER BY artist_name, release_date")
        all_releases = cursor.fetchall() or []
        
        # Group by (artist, normalized_album, release_date)
        groups = {}
        for release in all_releases:
            artist = release['artist_name']
            album = release['album_name']
            date = release['release_date']
            normalized = normalize_album_name(album)
            
            key = (artist.lower(), normalized, date)
            if key not in groups:
                groups[key] = []
            groups[key].append(dict(release))
        
        total_merged = 0
        
        # For each group with duplicates
        for key, releases_in_group in groups.items():
            if len(releases_in_group) > 1:
                artist, normalized, date = key
                print(f"\n🔍 Found {len(releases_in_group)} duplicates for {artist} - {date}:")
                
                # Find the best record (shortest/cleanest name)
                best_record = min(releases_in_group, key=lambda x: len(x['album_name']))
                print(f"  Keeping: '{best_record['album_name']}'")
                
                # Delete others
                for record in releases_in_group:
                    if record['id'] != best_record['id']:
                        print(f"  Removing: '{record['album_name']}'")
                        cursor.execute("DELETE FROM upcoming_releases WHERE id = ?", (record['id'],))
                        total_merged += 1
        
        if total_merged > 0:
            conn.commit()
            print(f"\n✅ Merged {total_merged} duplicate release entries")
        else:
            print("\n✓ No duplicates found")
        
        # Show remaining releases for verification
        cursor.execute("SELECT COUNT(*) as count FROM upcoming_releases")
        total_count = cursor.fetchone()['count']
        print(f"📊 Total remaining releases: {total_count}")
        
        # Show Paleface Swiss releases specifically
        print("\n📀 Paleface Swiss releases:")
        cursor.execute("SELECT album_name, release_date FROM upcoming_releases WHERE artist_name = 'Paleface Swiss' ORDER BY release_date")
        pf_releases = cursor.fetchall() or []
        for rel in pf_releases:
            print(f"  - {rel['album_name']} ({rel['release_date']})")
        
        conn.close()
        
    except Exception as e:
        print(f"❌ Error: {e}")
        raise

if __name__ == "__main__":
    print("🔧 Cleaning up duplicate album releases...")
    cleanup_duplicates()
    print("✨ Done!")
