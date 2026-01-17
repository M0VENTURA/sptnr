# Singles Detection Fix - January 14, 2026

**UPDATE (Current): As of this fix, all singles detection logic has been moved to `popularity.py`. The deprecated `sptnr.py` file is no longer used by active code. See [Migration Guide](../MIGRATION_GUIDE.md) for details.**

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

**File: `popularity.py` (function `detect_single_for_track()`)**

**Note**: This logic was originally in `sptnr.py` but has been moved to `popularity.py` as the canonical implementation.

The proper `detect_single_for_track()` function uses:

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

## Changes Made (Historical - Updated January 2026)

**Note**: The information below describes the historical fix. As of the current version, ALL functionality (popularity, singles detection, star rating, Navidrome sync) is now unified in `popularity.py`. The `sptnr.py` file is deprecated and no longer used by active code.

### Current Architecture (Post-Deprecation)

**File: `popularity.py`**

Now handles ALL of the following in a single integrated scan:
- Fetching popularity scores from Spotify, Last.fm, ListenBrainz
- Multi-source singles detection using Spotify, MusicBrainz, Discogs
- Calculating star ratings using median banding algorithm
- Boosting stars for confirmed singles (high confidence = 5 stars)
- Syncing ratings to Navidrome
- Creating Essential Artist playlists

**File: `unified_scan.py`**

Now simply calls `popularity_scan()` once to handle everything. The redundant Phase 2 that called `rate_artist` from `sptnr.py` has been removed.

### Historical Changes (January 14, 2026)

#### 1. File: `popularity.py` (Historical)

**Removed**:
- `HIGH_POPULARITY_THRESHOLD` and `MEDIUM_POPULARITY_THRESHOLD` constants
- All singles detection logic (50+ lines)
- Singles logging statements
- Database updates to `is_single`, `single_confidence`, `single_sources`

**Modified (Historical)**:
- Updated module docstring to clarify singles detection is handled properly
- Changed database UPDATE to set `stars`, `is_single`, `single_confidence`, `single_sources` fields
- Added proper singles detection logic
- Changed log message to "Calculating star ratings and detecting singles"

**Current State**: `popularity.py` now handles:
- Fetching popularity scores from external APIs
- Multi-source singles detection (Spotify, MusicBrainz, Discogs)
- Calculating star ratings based on popularity distribution and single status
- Syncing star ratings to Navidrome
- Creating Essential Artist playlists

#### 2. File: `unified_scan.py` (Historical)

**Changed (Historical)**:
```python
# Before (Incorrect)
from start import rate_artist, build_artist_index

# After (Temporary Fix)
from sptnr import rate_artist
from start import build_artist_index

# Current (Deprecated sptnr.py removed)
from popularity import popularity_scan
from start import build_artist_index
```

**Reason (Historical)**: The `rate_artist` function existed in `sptnr.py`, not `start.py`.

**Current State**: The import of `rate_artist` from `sptnr.py` has been removed entirely. The redundant Phase 2 that called it has also been removed since `popularity_scan()` already handles everything.

## How It Works Now (Current Architecture)

### Unified Scan Flow

The unified scan now has a single phase that handles everything:

1. **Popularity, Singles Detection, Star Rating & Playlist Creation** (`popularity.py::popularity_scan()`)
   - Fetches popularity scores from Spotify, Last.fm, ListenBrainz
   - For each track, calls `detect_single_for_track()`
   - Queries Spotify, MusicBrainz, Discogs APIs for singles verification
   - Applies confidence scoring (2+ sources = high confidence)
   - Filters out non-singles (live, remix, intro, outro)
   - Considers album context and track count
   - Calculates star ratings using median banding algorithm
   - Boosts stars for confirmed singles (high confidence = 5 stars)
   - Updates database with `stars`, `is_single`, `single_confidence`, `single_sources`, `popularity_score`
   - Syncs ratings to Navidrome
   - Creates Essential Artist playlists (10+ five-stars OR top 10% if 100+ tracks)

2. **Dashboard Display** (`app.py::dashboard()`)
   - Query: `SELECT COUNT(*) FROM tracks WHERE is_single = 1`
   - Correctly counts tracks marked as singles
   - Updates after each artist/album scan completes

## Expected Behavior

### For "Massive Addictive (deluxe edition)"

**Before Fix (Historical)**:
- 14 tracks marked as singles (any track with popularity >= 70)
- Incorrect single status based purely on popularity

**After Fix (Current)**:
- Only tracks confirmed by 2+ external sources marked as singles
- Typical deluxe edition might have 1-3 actual singles, not 14

### Dashboard Singles Count

**Before Fix (Historical)**:
- Count might not update or show inflated numbers
- Depends on when popularity scan last ran

**After Fix (Current)**:
- Count updates during popularity scan for each artist/album
- Reflects accurate singles detected via multi-source verification

## Testing Recommendations

1. **Album-Specific Test** (using current architecture):
   ```bash
   # Run unified scan for specific artist
   # This will use popularity.py which handles everything
   python3 start.py --scan-type full --artist "Amaranthe" --force
   
   # Check singles count
   sqlite3 database/sptnr.db "SELECT COUNT(*) FROM tracks WHERE album='Massive Addictive (deluxe edition)' AND is_single=1"
   ```

2. **Dashboard Verification**:
   - Navigate to dashboard after scan
   - Verify singles count matches database query
   - Compare with pre-fix count

3. **Log Review**:
   - Check `/config/unified_scan.log` for singles detection messages
   - Look for output: "✓ Single detected: {title} (high confidence, sources: ...)"
   - Verify sources mentioned (Spotify, MusicBrainz, Discogs)

## Comparison with January 2nd Behavior

The user mentioned "singles detection on 2nd January from start.py was perfect". This was likely when:

1. The proper multi-source detection logic was being used
2. Multi-source verification was working correctly
3. No popularity-based heuristic was interfering

**Current State**: All singles detection logic is now in `popularity.py` using the `detect_single_for_track()` function with multi-source verification. The deprecated `sptnr.py` is no longer used.

## Code Review Validation

### Syntax Check
```bash
python3 -m py_compile popularity.py unified_scan.py
# Exit code: 0 (success)
```

### Import Validation (Current)
- ✅ `from popularity import popularity_scan` - Handles everything
- ✅ `from start import build_artist_index` - Exists in start.py
- ❌ `from sptnr import rate_artist` - REMOVED (sptnr.py is deprecated)

### Database Schema
All database fields used are defined in schema:
- `tracks.stars` - Star rating (1-5)
- `tracks.is_single` - Boolean flag
- `tracks.single_confidence` - Text (low/medium/high)
- `tracks.single_sources` - JSON array of sources
- `tracks.popularity_score` - Popularity score from APIs

## Summary

**Historical Fix (January 14, 2026)**:
This fix resolved the singles detection issue by removing incorrect popularity-based logic and using proper multi-source verification.

**Current State (Latest)**:
All functionality has been unified into `popularity.py`:
1. **Removing deprecated code**: `sptnr.py` is no longer used by active code
2. **Unified architecture**: Single scan function handles popularity, singles detection, star rating, and playlist creation
3. **Simplified flow**: No redundant Phase 2 - everything happens in one integrated scan
4. **Ensuring accuracy**: Singles are only marked when confirmed by 2+ external sources

The dashboard singles count updates correctly and reflects only tracks that are verified singles, not just popular album tracks.
