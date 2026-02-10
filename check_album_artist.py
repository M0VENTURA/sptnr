import sqlite3
import os

db_path = os.path.join('database', 'sptnr.db')
if not os.path.exists(db_path):
    db_path = os.environ.get('DB_PATH', 'sptnr.db')

if not os.path.exists(db_path):
    print(f"Database not found at {db_path}")
    exit(1)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# Check if album_artist column exists
cursor.execute("PRAGMA table_info(tracks)")
columns = {row[1] for row in cursor.fetchall()}
print(f'Has album_artist column: {"album_artist" in columns}')

# Check data distribution
cursor.execute("SELECT COUNT(*) FROM tracks WHERE album_artist IS NOT NULL")
with_album_artist = cursor.fetchone()[0]

cursor.execute("SELECT COUNT(*) FROM tracks")
total = cursor.fetchone()[0]

print(f'Tracks with album_artist: {with_album_artist}/{total}')

# Check what query would return
cursor.execute("SELECT COUNT(DISTINCT COALESCE(album_artist, artist)) FROM tracks")
unique_album_artists = cursor.fetchone()[0]

cursor.execute("SELECT COUNT(DISTINCT artist) FROM tracks")
unique_artists = cursor.fetchone()[0]

print(f'Unique album_artists (via COALESCE): {unique_album_artists}')
print(f'Unique artists: {unique_artists}')

# Check missing_releases table
try:
    cursor.execute("PRAGMA table_info(missing_releases)")
    mr_columns = {row[1] for row in cursor.fetchall()}
    print(f'missing_releases has album_artist: {"album_artist" in mr_columns}')
    
    cursor.execute("SELECT COUNT(*) FROM missing_releases")
    mr_count = cursor.fetchone()[0]
    print(f'Rows in missing_releases: {mr_count}')
except Exception as e:
    print(f'Error checking missing_releases: {e}')

conn.close()
