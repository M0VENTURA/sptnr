-- Migration: Merge standalone "compilation" to "album+compilation"
-- This eliminates duplicate compilation entries in the album type dropdown
-- Run on application startup

UPDATE tracks 
SET spotify_album_type = 'album+compilation' 
WHERE spotify_album_type = 'compilation';
