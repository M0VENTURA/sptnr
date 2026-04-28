-- Migration: Add Discogs metadata columns for comprehensive single detection
-- These columns store raw Discogs API responses to enable detailed single verification

-- Discogs release identifiers
ALTER TABLE tracks ADD COLUMN discogs_release_id TEXT;
ALTER TABLE tracks ADD COLUMN discogs_master_id TEXT;

-- Discogs format information (stored as JSON)
ALTER TABLE tracks ADD COLUMN discogs_formats TEXT;  -- JSON array of format objects
ALTER TABLE tracks ADD COLUMN discogs_format_descriptions TEXT;  -- JSON array of description strings

-- Discogs single detection result
ALTER TABLE tracks ADD COLUMN discogs_is_single INTEGER DEFAULT 0;  -- Boolean: 1 if single, 0 if not

-- Discogs track information (stored as JSON)
ALTER TABLE tracks ADD COLUMN discogs_track_titles TEXT;  -- JSON array of track title strings

-- Additional Discogs metadata
ALTER TABLE tracks ADD COLUMN discogs_release_year INTEGER;
ALTER TABLE tracks ADD COLUMN discogs_label TEXT;
ALTER TABLE tracks ADD COLUMN discogs_country TEXT;

-- Index on discogs_release_id for faster lookups
CREATE INDEX IF NOT EXISTS idx_tracks_discogs_release_id ON tracks(discogs_release_id);
CREATE INDEX IF NOT EXISTS idx_tracks_discogs_master_id ON tracks(discogs_master_id);
