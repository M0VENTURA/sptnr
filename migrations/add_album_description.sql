-- Migration: Add album_description column to store album notes from Discogs
-- This column stores the album description/notes field from Discogs releases

ALTER TABLE tracks ADD COLUMN album_description TEXT;
