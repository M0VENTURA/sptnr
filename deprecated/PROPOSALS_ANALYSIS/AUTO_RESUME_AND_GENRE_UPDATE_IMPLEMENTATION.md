# Auto-Resume Scan and Genre Update Implementation Summary

## Overview
This PR implements five major features requested in the problem statement:
1. Auto-resume scan after reboot
2. Navidrome import genre/title tag processing
3. Single detection bracket filtering
4. Album matching with Deluxe/Remastered fallback
5. Special album time window matching

## Implementation Details

### 1. Auto-Resume Scan After Reboot

**Module:** `scan_resume.py`

**Features:**
- Detects interrupted scans using progress file
- Saves progress after each artist to `/database/navidrome_scan_progress.json`
- Resume only if scan was interrupted recently (< 24 hours)
- Skips already processed artists when resuming
- Clears progress file on successful completion

**Integration:** `navidrome_import.py` `scan_library_to_db()`
- Calls `should_resume_scan()` at startup
- Uses `get_artists_to_scan()` to get remaining artists
- Calls `mark_scan_completed()` when done

**Usage Example:**
```python
from scan_resume import should_resume_scan, get_artists_to_scan

# Check for interrupted scan
should_resume, resume_from_artist = should_resume_scan("navidrome")

# Get artists to scan (skipping already processed)
artists_to_scan = get_artists_to_scan(all_artists, resume_from_artist)
```

### 2. Navidrome Import - Genre/Title Tag Processing

**Module:** `genre_title_processor.py`

**Automatic Processing Rules:**
1. **Genre → Title:** If genre contains "acoustic"/"live"/"unplugged", append to title if not present
2. **Title → Genre:** If title has (Live)/(Acoustic)/(Demo)/(Remix)/(Unplugged), add to genres
3. **Album → Track:** If album has "acoustic"/"unplugged", add to track titles and genres
4. **File Update:** Updates both database and MP3/FLAC audio files

**Integration:** `navidrome_import.py` `scan_artist_to_db()`
- Calls `process_track_genres_and_title()` for each track
- Updates database with new title and genres
- Calls `update_track_metadata_file()` to update audio files

**Usage Example:**
```python
from genre_title_processor import process_track_genres_and_title

# Process track metadata
updated_title, updated_genres = process_track_genres_and_title(
    track_title="Song Name",
    album_name="MTV Unplugged",
    genre_list=["Rock"]
)
# Result: title="Song Name (unplugged)", genres=["Rock", "Unplugged"]
```

### 3. Single Detection - Ignore Specific Brackets

**Module:** `single_detection_enhanced.py`

**Changes:**
- Modified `is_non_canonical_version_strict()` function
- Allows (radio edit), (single), (remastered) during matching
- These canonical single versions are no longer rejected
- Brackets are still removed for comparison

**Before:**
```python
# These were rejected as non-canonical
"Song Name (Radio Edit)"  # ✗ Rejected
"Song Name (Single)"      # ✗ Rejected
"Song Name (Remastered)"  # ✗ Rejected
```

**After:**
```python
# These are now allowed
"Song Name (Radio Edit)"  # ✓ Allowed
"Song Name (Single)"      # ✓ Allowed
"Song Name (Remastered)"  # ✓ Allowed
```

### 4. Album Matching - Deluxe/Remastered Fallback

**Module:** `album_matching_enhancements.py`

**Features:**
- `normalize_album_for_fallback()` - Removes edition markers
- `match_album_with_fallback()` - Tries exact match, then normalized match
- Handles: Deluxe, Remastered, Rereleased editions

**Usage Example:**
```python
from album_matching_enhancements import match_album_with_fallback

candidates = ["Album Name", "Other Album"]
match = match_album_with_fallback("Album Name (Deluxe Edition)", candidates)
# Result: "Album Name" (fallback to original)
```

### 5. Special Album Time Window Matching

**Module:** `album_matching_enhancements.py`

**Features:**
- Detects special albums: live, symphony, symphonic, acoustic, unplugged
- Applies ±1 year time window restriction for single matching
- Falls back gracefully if release dates unavailable

**Integration:** `single_detection_enhanced.py` `detect_single_enhanced()`
- Integrated into Spotify single detection logic
- Calls `should_apply_time_window_restriction()` for each result
- Rejects tracks outside time window for special albums

**Usage Example:**
```python
from album_matching_enhancements import should_apply_time_window_restriction

# Check if time window should be applied
should_restrict, is_within = should_apply_time_window_restriction(
    album_name="Live at Wembley",
    track_release_date="2020-05-15",
    album_release_date="2019-06-01"
)
# Result: should_restrict=True, is_within=True (within 1 year)
```

## Testing

All features are thoroughly tested with 59 tests across 3 test files:

### Test Coverage
- **test_genre_title_processor.py:** 19 tests
  - Parenthetical tag detection
  - Tag extraction from titles
  - Album tag detection
  - Title/genre processing logic
  - Tag appending

- **test_scan_resume.py:** 10 tests
  - Progress file operations
  - Interrupted scan detection
  - Artist list resume logic
  - Old scan filtering

- **test_album_matching_enhancements.py:** 30 tests
  - Album normalization
  - Special album detection
  - Year extraction
  - Time window validation
  - Album matching with fallback

### Running Tests
```bash
# Run all tests
python3 -m unittest test_genre_title_processor test_scan_resume test_album_matching_enhancements -v

# Run individual test files
python3 -m unittest test_genre_title_processor -v
python3 -m unittest test_scan_resume -v
python3 -m unittest test_album_matching_enhancements -v
```

## Files Added

**New Modules:**
- `genre_title_processor.py` - Genre and title automatic processing
- `scan_resume.py` - Auto-resume scan functionality
- `album_matching_enhancements.py` - Album matching enhancements

**Test Files:**
- `test_genre_title_processor.py`
- `test_scan_resume.py`
- `test_album_matching_enhancements.py`

## Files Modified

- `navidrome_import.py` - Integrated auto-resume and genre/title processing
- `single_detection_enhanced.py` - Bracket filtering and time window validation

## Backward Compatibility

All changes are backward compatible:
- Auto-resume is optional (only activates if interrupted scan detected)
- Genre/title processing only adds tags, doesn't remove existing ones
- Single detection improvements only expand what's allowed
- Album matching fallback only applies if exact match fails
- Time window validation only applies to special albums

## Future Enhancements

Possible future improvements:
1. Add resume support for popularity scans (currently only Navidrome)
2. Make time window configurable (currently fixed at ±1 year)
3. Add more special album types (e.g., "orchestral", "remix album")
4. Add UI for viewing/managing scan progress
5. Add configuration for genre/title processing rules

## References

- Problem Statement: GitHub Actions workflow reference provided in issue
- Database Schema: Uses existing `tracks` table columns
- Audio File Handling: Uses existing `metadata_reader.py` functions
- Single Detection: Builds on existing `single_detection_enhanced.py` logic
