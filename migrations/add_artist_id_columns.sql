-- Add artist ID columns for various services to enable caching
-- This reduces redundant API calls by storing artist IDs from different services

-- Add Spotify artist ID column
ALTER TABLE tracks ADD COLUMN spotify_artist_id TEXT;

-- Add Last.fm artist MBID column (some artists have MBIDs on Last.fm)
ALTER TABLE tracks ADD COLUMN lastfm_artist_mbid TEXT;

-- Add Discogs artist ID column
ALTER TABLE tracks ADD COLUMN discogs_artist_id TEXT;

-- Add MusicBrainz artist ID column (different from recording MBID)
ALTER TABLE tracks ADD COLUMN musicbrainz_artist_id TEXT;

-- Create indexes for efficient lookups
CREATE INDEX IF NOT EXISTS idx_tracks_spotify_artist_id ON tracks(spotify_artist_id);
CREATE INDEX IF NOT EXISTS idx_tracks_musicbrainz_artist_id ON tracks(musicbrainz_artist_id);
CREATE INDEX IF NOT EXISTS idx_tracks_discogs_artist_id ON tracks(discogs_artist_id);
