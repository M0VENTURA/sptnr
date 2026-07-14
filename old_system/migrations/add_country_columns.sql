-- Add country/origin columns for artist geographical information
-- This enables displaying country as a genre tag or classification

-- Add country column to tracks table (for display and filtering)
ALTER TABLE tracks ADD COLUMN artist_country TEXT;

-- Add country column to artists table (for caching artist origin)
ALTER TABLE artists ADD COLUMN country TEXT;

-- Add MusicBrainz area ID for detailed geographical data
ALTER TABLE artists ADD COLUMN musicbrainz_area_id TEXT;

-- Create index for efficient country-based queries
CREATE INDEX IF NOT EXISTS idx_tracks_artist_country ON tracks(artist_country);
CREATE INDEX IF NOT EXISTS idx_artists_country ON artists(country);
