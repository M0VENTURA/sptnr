# Writer/Lyricist Field Import Fix

## Problem Identified
The writer/lyricist field from Navidrome was being extracted but **not being imported into the database** during scans.

### Root Cause Analysis
The issue had three components:

#### 1. **Extraction Working** ✅
- [navidrome.py](api_clients/navidrome.py#L280-L303): Successfully extracts lyricists from Navidrome and converts to JSON array
- Returns `"writer": writer_json` in the metadata dictionary

#### 2. **Data Collection Working** ✅
- [scan_helpers.py](helpers/scan_helpers.py#L237): Includes writer in the track data being prepared for database save
- Code: `"writer": extracted.get("writer", "[]")`

#### 3. **Database Column Missing** ❌
- The `writer` column **did not exist** in the tracks SQLite table
- When `save_to_db()` tried to INSERT the writer value, it failed because the column didn't exist
- The INSERT/UPDATE was silently failing or the column was being ignored

## Solution Implemented

### 1. Created Migration File
**File**: [migrations/add_writer_column.sql](migrations/add_writer_column.sql)
- Adds `writer TEXT` column to tracks table
- Creates index `idx_tracks_writer` for efficient queries on writer field

### 2. Created Migration Function
**File**: [helpers/db_utils.py](helpers/db_utils.py#L216-L275)
- Function: `ensure_writer_column()`
- Checks if writer column exists
- Adds column if missing
- Handles errors gracefully (doesn't fail app startup)
- Follows same pattern as `ensure_album_artist_column()` and `ensure_musicbrainz_album_mbid_column()`

### 3. Integrated Into App Startup
**File**: [app.py](app.py#L2-L6) and [app.py](app.py#L368)
- Added `ensure_writer_column` to imports from helpers.db_utils
- Called immediately after other column migrations on app startup
- Runs automatically when app starts, before any database operations

## How It Works Now

### During Next Scan:
1. **Navidrome Client** extracts lyricists from track metadata
2. **Scan Helpers** collects extracted data including writer field
3. **Save to DB** function inserts track data with writer column
4. **Database** stores writer as JSON array of lyricist names

### Data Flow:
```
Navidrome API
    ↓
navidrome.extract_track_metadata()  → Returns writer_json (JSON array)
    ↓
scan_artist_to_db()  → Collects writer field in track data
    ↓
save_to_db(track_data)  → Inserts/updates tracks table with writer column
    ↓
tracks.writer  → Stored as "["Lyricist 1", "Lyricist 2"]" (JSON)
```

## Testing Instructions

### To Verify the Fix:

1. **Run a Navidrome scan** (next scan after deploying this fix)
   ```bash
   # In app UI or via Python
   # Navigate to Navidrome scan and run it
   ```

2. **Check database for writer values**:
   ```sql
   SELECT id, title, writer FROM tracks 
   WHERE writer NOT NULL AND writer != '[]'
   LIMIT 10;
   ```
   Expected: Shows tracks with writer field populated as JSON arrays

3. **Check logs for success**:
   - Look for: `"✓ Successfully added writer column to tracks table"`
   - Or: `"✓ Writer column already exists in tracks table"`

4. **Verify on track pages**:
   - Browse a track detail page
   - Should now show writer/lyricist information if available from Navidrome

## Related Files Modified

| File | Changes |
|------|---------|
| [migrations/add_writer_column.sql](migrations/add_writer_column.sql) | **NEW** - SQL migration to add column |
| [helpers/db_utils.py](helpers/db_utils.py) | Added `ensure_writer_column()` function |
| [app.py](app.py) | Added import and call to `ensure_writer_column()` |

## Edge Cases Handled

1. **Column already exists** → Function detects and returns gracefully
2. **Tracks table doesn't exist yet** → Function skips (will run again later)
3. **Database locked** → Uses existing connection pooling/timeout
4. **App startup failure** → Migration runs but doesn't block startup (logged as warning)

## Why This Matters

- **Complete metadata capture**: Lyricist information now properly stored
- **Self-improving system**: Each scan adds more writer data
- **Frontend consistency**: Track pages can now display lyricist credits
- **Data integrity**: Multiple sources (Navidrome, MusicBrainz) can contribute to writer field

## Verification Status

- ✅ Migration function created
- ✅ Function added to app startup
- ✅ Imports configured correctly
- ✅ No breaking changes (backward compatible)
- ✅ Follows existing pattern in codebase
- ✅ Handles errors gracefully

## Next Steps

1. Deploy this fix
2. Restart the application
3. Run a Navidrome scan
4. Verify writer column is populated with non-empty values
5. Update UI if needed to display writer/lyricist information
