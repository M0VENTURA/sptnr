# Fix Summary: Force Feature, NavidromeClient, and Singles Count

## Issues Fixed

### 1. Force Feature Not Being Used from YAML Config
**Problem:** The `force` setting in `config.yaml` under `features.force` was being ignored during album rescans. The scan pipeline was hardcoded to always use `force=True`.

**Root Cause:** In `app.py`, the `_run_artist_scan_pipeline()` function was calling:
- `scan_artist_to_db(artist_name, artist_id, verbose=True, force=True)` 
- `popularity_scan(verbose=True, force=True, artist_filter=artist_name)`

Both were hardcoded with `force=True` instead of reading from the config.

**Fix:**
- Modified `_run_artist_scan_pipeline()` to read the force setting from config: 
  ```python
  config_data, _ = _read_yaml(CONFIG_PATH)
  force = config_data.get("features", {}).get("force", False)
  ```
- Now passes the config value to both scan functions
- Added logging to show which force setting is being used

**Testing:** 
- Set `force: false` in `config.yaml` → scans should skip already-scanned albums
- Set `force: true` in `config.yaml` → scans should rescan everything regardless of history

---

### 2. NavidromeClient Not Initialized (NoneType Error)
**Problem:** Artist scans were failing with error:
```
'NoneType' object has no attribute 'fetch_artist_albums'
```

**Root Cause:** 
- `popularity_helpers.py` functions like `fetch_artist_albums()` were importing `nav_client` from `start.py`
- In the `app.py` context (web UI scans), `start.nav_client` is `None` because it's only initialized when running `start.py` as main
- When web UI calls scan functions, there's no NavidromeClient available

**Fix:**
- Added `_get_nav_client()` helper function in `popularity_helpers.py` that:
  1. First tries to import `nav_client` from `start.py`
  2. If that's None or unavailable, creates a new `NavidromeClient` from config.yaml
  3. Supports both multi-user (`navidrome_users`) and single-user (`navidrome`) config formats
- Updated all functions that need NavidromeClient to use `_get_nav_client()`:
  - `fetch_artist_albums()`
  - `fetch_album_tracks()`
  - `build_artist_index()`

**Testing:**
- Trigger an artist scan from the web UI
- Check logs for successful Navidrome metadata import
- Verify no NoneType errors

---

### 3. Singles Count Not Displayed on Album View
**Problem:** The album detail page showed individual tracks' single status but didn't display an aggregate count of how many singles are in the album.

**Root Cause:**
- The `album_detail()` route in `app.py` was not calculating or passing a `singles_count` variable to the template
- The `album.html` template had no metadata card for displaying singles count

**Fix:**
- Added singles count query in `album_detail()` route:
  ```python
  cursor.execute("""
      SELECT COUNT(*) as singles_count
      FROM tracks
      WHERE artist = ? AND album = ? AND stars = 5
  """, (artist, album))
  album_data['singles_count'] = singles_row['singles_count']
  ```
- Added new metadata card in `album.html` template to display the count
- Card only shows when `singles_count > 0`
- Uses 5-star ratings as the indicator of detected singles

**Note:** The singles detection algorithm assigns 5 stars to tracks it identifies as singles, so counting `stars = 5` gives us the detected singles count.

**Testing:**
- Navigate to any album page
- If the album has tracks with 5-star ratings, a "Detected Singles" card should appear
- The count should match the number of 5-star tracks in the album

---

## Configuration Changes

### config.yaml
The `force` feature under `features:` section is now properly used:
```yaml
features:
  force: false  # Set to true to force rescan all albums, ignoring scan history
```

### config.html (Web UI)
Updated the description for the `force` option to be more accurate:
- Old: "Force certain operations to run even if pre-checks or validations fail. Use with caution."
- New: "Force rescanning of albums even if they were already scanned. When enabled, ignores scan history and rescans all albums during artist/album rescans."

---

## Files Modified

1. **app.py**
   - `_run_artist_scan_pipeline()`: Read force from config instead of hardcoding
   - `album_detail()`: Added singles_count calculation

2. **popularity_helpers.py**
   - Added `_get_nav_client()` function
   - Updated `fetch_artist_albums()`, `fetch_album_tracks()`, `build_artist_index()`

3. **templates/album.html**
   - Added "Detected Singles" metadata card

4. **templates/config.html**
   - Updated force feature description

---

## How to Use

### Force Rescan Feature
1. Edit `config.yaml` or use the web UI config page
2. Set `features.force: true` to enable force rescanning
3. Trigger an artist or album rescan
4. All albums will be rescanned regardless of scan history
5. Set back to `false` for normal operation (skips already-scanned albums)

### Singles Count Display
- Navigate to any album page in the web UI
- Look for the "Detected Singles" card in the album metadata section
- The count shows how many tracks in the album have been detected as singles (5-star rated tracks)
- This helps you quickly see which albums contain popular/single tracks

---

## Additional Notes

### Why Count 5-Star Tracks as Singles?
The popularity detection algorithm in this codebase assigns star ratings based on:
- Spotify popularity
- Last.fm play counts
- ListenBrainz data
- Track age

Tracks that score highest (singles) are given 5 stars. Therefore, counting 5-star tracks gives us the detected singles count.

### Multi-User Support
The `_get_nav_client()` function supports both configuration formats:
- **Multi-user**: Uses `navidrome_users` array (takes first user's credentials)
- **Single-user**: Falls back to `navidrome` object

This ensures compatibility with both old and new configuration formats.
