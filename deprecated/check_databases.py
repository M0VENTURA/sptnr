#!/usr/bin/env python3
import sqlite3

# Check navidrome.db first
print("\n=== NAVIDROME.DB ===")
try:
    conn = sqlite3.connect("navidrome.db")
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = cursor.fetchall()
    print(f"Tables: {[t[0] for t in tables]}")
    
    cursor.execute("SELECT COUNT(*) FROM tracks")
    count = cursor.fetchone()[0]
    print(f"Tracks: {count}")
    conn.close()
except Exception as e:
    print(f"Error: {e}")

# Check music.db
print("\n=== MUSIC.DB ===")
try:
    conn = sqlite3.connect("music.db")
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = cursor.fetchall()
    print(f"Tables: {[t[0] for t in tables]}")
    
    cursor.execute("SELECT COUNT(*) FROM tracks")
    count = cursor.fetchone()[0]
    print(f"Tracks: {count}")
    conn.close()
except Exception as e:
    print(f"Error: {e}")

# Check sptnr.db
print("\n=== SPTNR.DB ===")
try:
    conn = sqlite3.connect("sptnr.db")
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = cursor.fetchall()
    print(f"Tables: {[t[0] for t in tables]}")
    
    cursor.execute("SELECT COUNT(*) FROM tracks")
    count = cursor.fetchone()[0]
    print(f"Tracks: {count}")
    conn.close()
except Exception as e:
    print(f"Error: {e}")
