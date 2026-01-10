
import sqlite3

DB_PATH = 'sptnr.db'


# NOTE: The scan_history table is now created by check_db.py. Ensure check_db.py (or update_schema) runs at startup.

try:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT artist, album, scan_type, scan_timestamp, tracks_processed, status FROM scan_history ORDER BY scan_timestamp DESC LIMIT 10')
    rows = cursor.fetchall()
    if not rows:
        print('No scan_history records found.')
    else:
        for row in rows:
            print(dict(zip(['artist', 'album', 'scan_type', 'scan_timestamp', 'tracks_processed', 'status'], row)))
    conn.close()
except Exception as e:
    print(f'Error reading scan_history: {e}')
