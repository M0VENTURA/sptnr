# Integration Recommendations: sptnr.py Analysis

## Summary

This document summarizes the analysis of `sptnr.py` compared to the current integration in `popularity.py`, `start.py`, and `unified_scan.py`. It provides recommendations for what features should be adopted from sptnr.py's implementation.

## Changes Made in This PR

### ✅ Already Integrated

1. **Resume Checkpoint Logic** - Added to `popularity.py`
   - Database-based tracking using `scan_history` table
   - Allows resuming scans from last processed artist
   - Supports fuzzy matching for artist names

2. **Genre Aggregation System** - Added to `popularity.py`
   - Weighted scoring from 5 sources:
     - MusicBrainz (40% weight) - Most trusted
     - Discogs (25%)
     - AudioDB (20%)
     - Last.fm (10%)
     - Spotify (5%)
   - Contextual filtering (punk vs electronic, metal variants)
   - Deduplication and normalization

3. **Video Detection** - Enhanced in `popularity.py`
   - Added `has_discogs_video()` as single detection source
   - Requires secondary confirmation (won't mark as single with video alone)
   - Integrated into timeout-protected API call chain

4. **Essential Playlist Creation** - Restored in `popularity.py`
   - Creates NSP (Navidrome Smart Playlist) files
   - Two modes:
     - 10+ five-star tracks → Pure 5★ essentials
     - 100+ total tracks → Top 10% by rating
   - Proper file sanitization and error handling

5. **Spotify Credentials Fix**
   - Changed from `sys.exit(1)` to warning log
   - Prevents app.py crash on import when credentials missing

6. **Removed sptnr.py Dependency**
   - Created `rate_artist_single_detection()` wrapper in start.py
   - Uses `unified_scan_pipeline` with artist filtering
   - No more direct imports from sptnr.py in app.py

## Key Differences: sptnr.py vs Current Implementation

### 🟢 Current Implementation is Better

| Feature | Advantage |
|---------|-----------|
| **Database Concurrency** | WAL mode prevents lock conflicts, more scalable |
| **Timeout Enforcement** | `_run_with_timeout()` prevents API hangs |
| **Structured Logging** | Dual log files with unified_logger |
| **Batch Updates** | `executemany()` for efficient database operations |
| **Album Tracking** | Scan history per album prevents redundant rescans |

### 🟡 sptnr.py Has Unique Features

| Feature | Status | Recommendation |
|---------|--------|----------------|
| **Genre Aggregation** | ✅ **INTEGRATED** | Now available in popularity.py |
| **File Path Tracking** | ❌ Missing | **Consider adding** for library management |
| **Fallback Scoring** | ❌ Missing | Random score (5-15) for unmapped tracks |
| **YouTube Detection** | ❌ Missing | YouTube official channel verification |
| **Google Custom Search** | ❌ Missing | Fallback single detection method |

### 🔴 Features to Consider

#### 1. File Path Tracking
**sptnr.py approach:**
```python
file_path = song.get("path", "")  # Get file path from Navidrome
track["file_path"] = file_path    # Store in track data
```

**Benefit:** Useful for file operations, library organization, and debugging

**Recommendation:** Add `file_path` column to `tracks` table

#### 2. Fallback Scoring
**sptnr.py approach:**
```python
final_score = round(track["score"]) if track["score"] > 0 else random.randint(5, 15)
```

**Benefit:** Ensures every track has a non-zero score

**Recommendation:** Consider adding but use a more deterministic fallback (e.g., based on album position)

#### 3. YouTube Single Detection
**sptnr.py features:**
- YouTube official channel verification
- Channel ID caching
- Fuzzy artist name matching
- Video title comparison

**Status:** Not currently implemented in api_clients

**Recommendation:** Low priority - Spotify, MusicBrainz, and Discogs coverage is good

## Scoring Algorithm Comparison

### Both Implementations Match ✅

- **Banding:** Divide tracks into 4 bands
- **Jump Threshold:** `median_score * 1.7` for 5-star boost
- **Single Confidence:**
  - High (2+ sources) → 5 stars
  - Medium (1 source) → +1 star boost
  - Low (0 sources) → normal banding
- **Album Context:** Downgrade medium → low if album has >3 tracks

## Genre System Details

### Weighted Aggregation
```python
GENRE_WEIGHTS = {
    "musicbrainz": 0.40,   # Most trusted
    "discogs": 0.25,       # Still strong
    "audiodb": 0.20,       # Good for fallback
    "lastfm": 0.10,        # Tags can be messy
    "spotify": 0.05        # Too granular
}
```

### Contextual Filtering
- **Live detection:** Boosts "live" genre if in title/album
- **Christmas detection:** Boosts "christmas" if in title/album
- **Metal subgenres:** Removes "heavy metal" if specific metal genres exist
- **Genre conflicts:** Removes "electronic" when punk/metal dominates

### Normalization
- "hip hop" → "hip-hop"
- "r&b" → "rnb"
- Lowercase and trim all genres
- Deduplication while preserving order

## Video Detection Implementation

### How It Works
```python
# Step 1: Check if video exists
if has_discogs_video(title, artist):
    # Step 2: Only add as source if we have another confirmation
    if len(single_sources) >= 1:
        single_sources.append("discogs_video")
    else:
        # Video alone is not enough
        log("Video detected but needs second source")
```

### Confidence Calculation
- **2+ sources (including video)** → High confidence → 5 stars
- **1 source + video** → High confidence → 5 stars
- **Video only** → Low confidence → Normal banding

## Usage in UI

### Album/Song Genre Display
The UI should query the `genres` field from the database, which now contains:
```json
["Progressive Metal", "Death Metal", "Technical"]
```

These are the weighted, normalized, and deduplicated genres from multiple sources.

### Playlist Access
Essential playlists are stored as NSP files in `/music/Playlists/`:
```
/music/Playlists/Essential Metallica.nsp
/music/Playlists/Essential Iron Maiden.nsp
```

Navidrome automatically picks these up and displays them in the UI.

## Testing Recommendations

1. **Genre Aggregation**
   - Test with artists that have conflicting genres (e.g., punk + electronic)
   - Verify metal subgenre handling
   - Check normalization of "hip hop" vs "hip-hop"

2. **Video Detection**
   - Test with known single that has official video
   - Verify it doesn't mark as single with video alone
   - Check that video + 1 other source = high confidence

3. **Resume Functionality**
   - Start a popularity scan
   - Cancel midway
   - Resume with `resume_from` parameter
   - Verify it continues from correct artist

4. **Playlist Creation**
   - Test artist with 10+ five-star tracks
   - Test artist with 100+ total tracks but <10 five-stars
   - Verify NSP files are created correctly

## Conclusion

The current integration in `popularity.py` and `unified_scan.py` is **superior** to sptnr.py in most ways:
- Better concurrency and database handling
- Timeout protection for API calls
- More comprehensive logging

This PR successfully integrates the **best features** from sptnr.py:
- ✅ Genre aggregation with weighted scoring
- ✅ Video detection with confirmation requirement
- ✅ Resume checkpoint functionality
- ✅ Essential playlist creation

**Future considerations:**
- File path tracking (low priority)
- YouTube detection (low priority - good coverage without it)
- Deterministic fallback scoring (optional improvement)
