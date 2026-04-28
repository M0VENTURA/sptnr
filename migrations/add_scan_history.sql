-- Add scan_history table to track individual album scans
CREATE TABLE IF NOT EXISTS scan_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artist TEXT NOT NULL,
    album TEXT NOT NULL,
    scan_type TEXT NOT NULL,  -- 'navidrome', 'popularity', or 'beets'
    scan_timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    tracks_processed INTEGER DEFAULT 0,
    status TEXT DEFAULT 'completed'  -- 'completed', 'error', 'skipped'
);

CREATE INDEX IF NOT EXISTS idx_scan_history_timestamp ON scan_history(scan_timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_scan_history_artist_album ON scan_history(artist, album);
