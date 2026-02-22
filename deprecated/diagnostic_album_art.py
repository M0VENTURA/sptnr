import sqlite3

db_path = 'navidrome.db'
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

print('=== Album Art Diagnostic ===\n')

# Check album_art table
print('[1] Checking album_art table...')
cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='album_art'")
if cursor.fetchone():
    cursor.execute('SELECT COUNT(*) FROM album_art')
    count = cursor.fetchone()[0]
    print(f'    ✓ album_art table has {count} rows')
    
    if count > 0:
        cursor.execute('SELECT artist_name, album_name, source FROM album_art LIMIT 3')
        for row in cursor.fetchall():
            print(f'      - {row[0]} - {row[1]} ({row[2]})')
else:
    print('    ✗ album_art table DOES NOT EXIST')

# Check cover_art_url
print('\n[2] Checking cover_art_url in tracks...')
cursor.execute('PRAGMA table_info(tracks)')
columns = {row[1] for row in cursor.fetchall()}

if 'cover_art_url' in columns:
    cursor.execute('SELECT COUNT(*) FROM tracks WHERE cover_art_url IS NOT NULL AND cover_art_url != ""')
    count = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM tracks')
    total = cursor.fetchone()[0]
    pct = (count * 100) // total if total > 0 else 0
    print(f'    ✓ {count}/{total} tracks have cover_art_url ({pct}%)')
    
    if count > 0:
        cursor.execute('SELECT DISTINCT artist, album FROM tracks WHERE cover_art_url IS NOT NULL LIMIT 2')
        for row in cursor.fetchall():
            print(f'      - {row[0]} - {row[1]}')
else:
    print('    ✗ cover_art_url column DOES NOT EXIST')

conn.close()
print('\nDone.')
