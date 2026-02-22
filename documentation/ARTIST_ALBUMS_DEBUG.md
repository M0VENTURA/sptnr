## Artist Album Display Debug Guide

### Issue
Artist detail pages show `album_count = 0` despite albums existing in the database.

### Potential Causes Identified

1. **Missing Album Type Data (MOST LIKELY)**
   - The `spotify_album_type` field may not be populated for albums
   - This field is needed for album categorization in the template
   - Location: `popularity_helpers.py` → `spotify_metadata_fetcher.py` extracts this at line 286
   - Status: Data is extracted from Spotify but may not exist for older scans

2. **Album Categorization Logic**
   - Albums are categorized by `spotify_album_type` (album/ep/single/compilation)
   - If type is empty, uses track_count as fallback (>6 = album, 3-6 = EP, <3 = single)
   - Categorization happens in app.py lines 1447-1460
   - All albums must be in one of the categories to display

3. **Database Query Logic**
   - Both artist list and detail pages use `COALESCE(album_artist, artist)` 
   - This prioritizes `album_artist` field, falls back to `artist` field
   - May cause mismatch if one is NULL and the other isn't populated consistently

### Testing Instructions

To debug your specific setup:

```bash
# 1. Run the debug script (requires Python 3.8+)
python debug_artist_albums.py

# 2. List all artists:
python debug_artist_albums.py

# 3. Debug a specific artist:
python debug_artist_albums.py "Artist Name"
```

The script will show:
- Total tracks for the artist
- Distinct album count
- List of all albums with track counts
- Sample tracks to verify data structure
- Album type values (which should be album/ep/single/compilation, not empty)

### Recommended Fixes

**Option 1: Populate Missing Album Types** (Recommended)
- Re-run popularity scan with `--force` flag to refresh all metadata
- This will call `fetch_comprehensive_metadata()` for all tracks
- Album types will be extracted from Spotify and saved

**Option 2: Update Fallback Logic**
- If Spotify metadata is unavailable, rely on track count heuristics (current fallback)
- Verify that fallback categorization works correctly

### Code References

- Template album display: [templates/artist.html](templates/artist.html#L453)
- Categorization logic: [app.py](app.py#L1447-L1465)
- Stats query: [app.py](app.py#L1356-L1381)
- Metadata extraction: [spotify_metadata_fetcher.py](spotify_metadata_fetcher.py#L286)
- Genre population: [popularity.py](popularity.py#L2096-L2119)

### Database Schema
The `tracks` table has all required columns for album categorization:
- `album` - Album name (for grouping)
- `spotify_album_type` - Type from Spotify (album/ep/single/compilation)
- `is_single` - Local single detection flag
- `album_artist` - Album artist (may differ from track artist)
- `artist` - Track artist

### Next Steps
1. Run debug script to identify specific issue
2. Check if `spotify_album_type` is empty for your albums
3. If empty, run: `python app.py --scan-type popularity --artist "Artist Name" --force`
4. Re-test artist detail page

