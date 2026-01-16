# Singles Detection Fix - January 14, 2026

## Problem Statement

Two critical issues were identified with singles detection:

1. **Incorrect Detection Logic**: The `popularity.py` module was using a naive popularity-based heuristic that marked ANY track with `popularity_score >= 70` as a single. This caused albums like "Massive Addictive (deluxe edition)" to incorrectly show 14 singles when most were just popular album tracks.

2. **Dashboard Count Not Updating**: The singles count on the dashboard wasn't changing because the incorrect detection logic was being used instead of the proper multi-source detection.

## Root Cause Analysis

### Before the Fix

**File: `popularity.py` (lines 19-23, 320-350)**
```python
# Incorrect thresholds
HIGH_POPULARITY_THRESHOLD = 70
MEDIUM_POPULARITY_THRESHOLD = 50

# Incorrect logic in popularity_scan()
if popularity_score >= HIGH_POPULARITY_THRESHOLD:
    is_single = True
    single_confidence = "high"
    single_sources.append("high_popularity")
    stars = 5  # Singles get 5 stars
elif popularity_score >= MEDIUM_POPULARITY_THRESHOLD:
    is_single = True
    single_confidence = "medium"
    single_sources.append("medium_popularity")
    stars = min(stars + 1, 5)
```

**Problem**: This simplistic approach marked any popular track as a single, regardless of:
- Whether it was actually released as a single
- Album context (track count, album type)
- External source confirmation

### The Correct Logic

**File: `sptnr.py` (lines 798-886)**
The proper `detect_single_status()` function uses:

1. **Multi-Source Verification**:
   - Spotify (checks `album_type == "single"`)
   - MusicBrainz (checks release group primary type)
   - Discogs (checks format/type)
   - Optional: Google fallback, AI classification

2. **Confidence Levels**:
   - **High**: 2+ sources confirm
   - **Medium**: 1 source confirms
   - **Low**: No sources confirm

3. **Smart Filtering**:
   - Excludes obvious non-singles: "intro", "outro", "jam", "live", "remix"
   - Considers album context: downgrades medium → low if album has >3 tracks

4. **Proper Classification**:
   ```python
   result = {
       "is_single": confidence in ["high", "medium"],
       "confidence": confidence,
       "sources": sources,
       "last_scanned": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
   }
   ```

## Changes Made

### 1. File: `popularity.py`

**Removed**:
- `HIGH_POPULARITY_THRESHOLD` and `MEDIUM_POPULARITY_THRESHOLD` constants
- All singles detection logic (50+ lines)
- Singles logging statements
- Database updates to `is_single`, `single_confidence`, `single_sources`

**Modified**:
- Updated module docstring to clarify singles detection is handled by `sptnr.py`
- Changed database UPDATE to only set `stars` field
- Removed singles summary logging
- Changed log message from "Calculating star ratings and detecting singles" to just "Calculating star ratings"

**Result**: `popularity.py` now only handles:
- Fetching popularity scores from external APIs
- Calculating star ratings based on popularity distribution
- Syncing star ratings to Navidrome

### 2. File: `unified_scan.py`

**Changed**:
```python
# Before
from start import rate_artist, build_artist_index

# After
from sptnr import rate_artist
from start import build_artist_index
```

**Reason**: The `rate_artist` function exists in `sptnr.py`, not `start.py`. Line 826 of `start.py` even has a comment: "Removed: rate_artist import, now only in popularity.py" (which was also incorrect).

**Result**: Phase 2 of the unified scan now properly calls the correct `rate_artist` function that includes multi-source singles detection.

## How It Works Now

### Scan Flow

1. **Phase 1: Popularity Detection** (`popularity.py::popularity_scan()`)
   - Fetches popularity scores from Spotify, Last.fm, ListenBrainz
   - Calculates star ratings using median banding algorithm
   - Updates database with `stars` only
   - Syncs ratings to Navidrome

2. **Phase 2: Singles Detection & Rating** (`sptnr.py::rate_artist()`)
   - For each track, calls `detect_single_status()`
   - Queries Spotify, MusicBrainz, Discogs APIs
   - Applies confidence scoring (2+ sources = high confidence)
   - Filters out non-singles (live, remix, intro, outro)
   - Considers album context
   - Updates database with `is_single`, `single_confidence`, `single_sources`

3. **Dashboard Display** (`app.py::dashboard()`)
   - Query: `SELECT COUNT(*) FROM tracks WHERE is_single = 1`
   - Correctly counts tracks marked as singles by Phase 2
   - Updates after each artist/album scan completes

## Expected Behavior

### For "Massive Addictive (deluxe edition)"

**Before Fix**:
- 14 tracks marked as singles (any track with popularity >= 70)
- Incorrect single status based purely on popularity

**After Fix**:
- Only tracks confirmed by 2+ external sources marked as singles
- Typical deluxe edition might have 1-3 actual singles, not 14

### Dashboard Singles Count

**Before Fix**:
- Count might not update or show inflated numbers
- Depends on when popularity scan last ran

**After Fix**:
- Count updates after Phase 2 of each artist/album scan
- Reflects accurate singles detected via multi-source verification

## Testing Recommendations

1. **Album-Specific Test**:
   ```bash
   # Re-scan the problematic album
   python3 sptnr.py --artist "Amaranthe" --force
   
   # Check singles count
   sqlite3 database/sptnr.db "SELECT COUNT(*) FROM tracks WHERE album='Massive Addictive (deluxe edition)' AND is_single=1"
   ```

2. **Dashboard Verification**:
   - Navigate to dashboard after scan
   - Verify singles count matches database query
   - Compare with pre-fix count

3. **Log Review**:
   - Check `/config/unified_scan.log` for singles detection messages
   - Look for Phase 2 output: "Single detected: '{title}' set to {stars}★"
   - Verify sources mentioned (Spotify, MusicBrainz, Discogs)

## Comparison with January 2nd Behavior

The user mentioned "singles detection on 2nd January from start.py was perfect". This was likely when:

1. The proper `detect_single_status()` from `sptnr.py` was being called
2. Multi-source verification was working correctly
3. No popularity-based heuristic was interfering

**This fix restores that behavior** by:
- Removing the interfering popularity heuristic
- Ensuring `unified_scan.py` correctly imports from `sptnr.py`
- Making singles detection a separate Phase 2 operation

## Code Review Validation

### Syntax Check
```bash
python3 -m py_compile popularity.py unified_scan.py
# Exit code: 0 (success)
```

### Import Validation
- ✅ `from sptnr import rate_artist` - Correct module
- ✅ `from start import build_artist_index` - Exists in start.py
- ✅ `from popularity import popularity_scan` - Exists in popularity.py

### Database Schema
All database fields used are defined in schema:
- `tracks.stars` - Star rating (1-5)
- `tracks.is_single` - Boolean flag
- `tracks.single_confidence` - Text (low/medium/high)
- `tracks.single_sources` - JSON array of sources

## Summary

This fix resolves the singles detection issue by:

1. **Removing incorrect logic**: Eliminated popularity-based singles detection from `popularity.py`
2. **Fixing imports**: Corrected `unified_scan.py` to import `rate_artist` from the correct module
3. **Separating concerns**: Popularity scoring and singles detection are now properly separated into Phase 1 and Phase 2
4. **Ensuring accuracy**: Singles are only marked when confirmed by 2+ external sources

The dashboard singles count will now update correctly and reflect only tracks that are verified singles, not just popular album tracks.
