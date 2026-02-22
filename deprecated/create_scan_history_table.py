import sqlite3

DB_PATH = 'sptnr.db'

schema = '''
CREATE TABLE IF NOT EXISTS scan_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artist TEXT,
    album TEXT,
    scan_type TEXT,
    scan_timestamp TEXT,
    tracks_processed INTEGER,
    status TEXT
);
'''

try:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.executescript(schema)
    conn.commit()
    print('scan_history table created or already exists.')
    conn.close()
except Exception as e:
    print(f'Error creating scan_history table: {e}')
