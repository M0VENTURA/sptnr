# Fix for Missing Artists Issue

## Problem
After PR #248 was merged, the artist list was showing only 17 artists instead of the expected count. Artists like "Various Artists" and others were missing from the list.

## Root Cause
PR #248 changed the artist list query to filter by `album_artist IS NOT NULL AND album_artist != ''` to prevent showing individual track artists from compilations. However, this had an unintended consequence:

1. In `navidrome_import.py` line 333, `album_artist` is populated from Navidrome's `albumArtist` field:
   ```python
   "album_artist": t.get("albumArtist", "")
   ```

2. If Navidrome doesn't provide an `albumArtist` value, it defaults to **empty string** (not NULL)

3. The query `WHERE album_artist IS NOT NULL AND album_artist != ''` excluded all albums where Navidrome didn't provide an albumArtist value

4. This caused many valid artists to disappear from the artist list

## Solution
Restored the `COALESCE(NULLIF(album_artist, ''), artist)` pattern throughout the codebase. This pattern:

1. Uses `NULLIF(album_artist, '')` to convert empty strings to NULL
2. Uses `COALESCE(..., artist)` to fall back to the `artist` field when `album_artist` is NULL or empty
3. Ensures all artists appear in the list while maintaining proper grouping

## Changes Made
Updated all queries that filter or group by artist to use `COALESCE(NULLIF(album_artist, ''), artist)`:

1. **Artist list query** (`app.py` lines 1206-1217)
   - Changed from filtering by `album_artist` only
   - Now uses COALESCE to fall back to `artist` field

2. **Artist detail queries** (`app.py` lines 1368, 1392)
   - Updated albums query and stats query to use COALESCE

3. **Album detail queries** (`app.py` lines 3241, 3279, 3324, 3332)
   - Updated track listing, metadata aggregation, singles count, and genre queries

4. **Album edit query** (`app.py` lines 3671, 3681)
   - Updated UPDATE statement and track selection query

5. **Album art API** (`app.py` line 7479)
   - Updated cover art URL query

6. **Genre aggregation** (`app.py` line 59)
   - Updated genre collection query

## Verification
Testing confirmed the fix works correctly:
- **Before fix**: Only artists with non-empty `album_artist` values appeared (3 artists in test DB)
- **After fix**: All artists appear, using `artist` field when `album_artist` is empty (4 artists in test DB)
- Example: "Queen" now appears (previously had empty `album_artist`)

The `COLLATE NOCASE` clause ensures case-insensitive grouping (e.g., "PINK FLOYD" and "Pink Floyd" are properly grouped together).

## Security & Code Review
- ✅ Code review passed with no issues
- ✅ Security scan (CodeQL) passed with no alerts

## Impact
This fix ensures all artists appear in the artist list, regardless of whether Navidrome provided an `albumArtist` value during import. Artists are properly grouped and displayed consistently across all pages.
