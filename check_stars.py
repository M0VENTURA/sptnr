#!/usr/bin/env python3
import sqlite3

DB_PATH = '/database/sptnr.db'
try:
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    cursor = conn.cursor()
    
    # Check total tracks
    cursor.execute("SELECT COUNT(*) FROM tracks")
    total = cursor.fetchone()[0]
    print(f"Total tracks in DB: {total}")
    
    # Check 5-star tracks
    cursor.execute("SELECT COUNT(*) FROM tracks WHERE stars = 5")
    five_star = cursor.fetchone()[0]
    print(f"5-star tracks: {five_star}")
    
    # Check stars distribution
    cursor.execute("""
        SELECT stars, COUNT(*) as count
        FROM tracks
        GROUP BY stars
        ORDER BY stars DESC
    """)
    print("\nStars distribution:")
    for row in cursor.fetchall():
        print(f"  {row[0]} stars: {row[1]} tracks")
    
    # Check some sample 5-star tracks
    if five_star > 0:
        cursor.execute("""
            SELECT id, title, artist, stars
            FROM tracks
            WHERE stars = 5
            LIMIT 5
        """)
        print("\nSample 5-star tracks:")
        for row in cursor.fetchall():
            print(f"  {row[2]} - {row[1]} (ID: {row[0]}, stars: {row[3]})")
    
    conn.close()
except Exception as e:
    print(f"Error: {e}")
