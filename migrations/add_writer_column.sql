-- Add writer column for storing lyricist/songwriter information from Navidrome
ALTER TABLE tracks ADD COLUMN writer TEXT;  -- JSON array of writer/lyricist names from Navidrome

-- Add indexes for writer-based queries
CREATE INDEX IF NOT EXISTS idx_tracks_writer ON tracks(writer);
