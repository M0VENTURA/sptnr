# Fix for Discogs Lookup Not Being Called During Artist Scan

## Problem Statement

When performing an artist scan via the artist page, Discogs API lookup was not happening for single detection even though the logs showed "Using sources: Spotify, MusicBrainz, Discogs, Discogs Video".

## Root Causes

### 1. Verbose Parameter Not Passed Correctly

The `popularity_scan` function has a `verbose` parameter that controls logging verbosity. However, when it called `detect_single_for_track`, it was passing the module-level constant `VERBOSE` instead of the function's `verbose` parameter.

**Before:**
```python
# In popularity_scan at line 1109
detection_result = detect_single_for_track(
    title=title,
    artist=artist,
    album_track_count=album_track_count,
    spotify_results_cache=spotify_results_cache,
    verbose=VERBOSE  # Wrong! This is module constant, not function parameter
)
```

**After:**
```python
detection_result = detect_single_for_track(
    title=title,
    artist=artist,
    album_track_count=album_track_count,
    spotify_results_cache=spotify_results_cache,
    verbose=verbose,  # Correct! Pass function parameter
    discogs_token=discogs_token  # Also pass already-loaded token
)
```

**Impact:** Even when artist scan was run with `verbose=True`, the single detection code would not produce verbose logs, making it impossible to debug why Discogs wasn't being called.

### 2. Spotify Results Cache Key Mismatch

The Spotify search results were being cached with `track_id` as the key but looked up with `title` as the key.

**Before:**
```python
# Storing cache at line 983
spotify_results_cache[track_id] = spotify_search_results

# Looking up cache at line 651
spotify_results = spotify_results_cache.get(title)  # Mismatch!
```

**After:**
```python
# Storing cache
spotify_results_cache[title] = spotify_search_results

# Looking up cache
spotify_results = spotify_results_cache.get(title)  # Matches!
```

**Impact:** The cache was never being reused, causing redundant Spotify API calls for every track during single detection.

### 3. Silent Config Loading Failures

When `detect_single_for_track` tried to load the Discogs token from config and failed, it only logged the error if `verbose=True`.

**Before:**
```python
try:
    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)
    discogs_token = config.get("api_integrations", {}).get("discogs", {}).get("token", "")
except Exception as e:
    if verbose:  # Only log if verbose!
        log_verbose(f"   ⚠ Could not load Discogs token from config: {e}")
```

**After:**
```python
try:
    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)
    discogs_token = config.get("api_integrations", {}).get("discogs", {}).get("token", "")
except Exception as e:
    # Always log config loading errors
    log_unified(f"   ⚠ Could not load Discogs token from config at {config_path}: {e}")
```

**Impact:** If the config file was missing or malformed in the Docker container, it would silently fail to load the Discogs token without any error message.

## Changes Made

### 1. Modified `detect_single_for_track` Function Signature

Added `discogs_token` as an optional parameter:

```python
def detect_single_for_track(
    title: str,
    artist: str,
    album_track_count: int = 1,
    spotify_results_cache: dict = None,
    verbose: bool = False,
    discogs_token: str = None  # NEW parameter
) -> dict:
```

This allows the caller to pass an already-loaded token, avoiding redundant config file reads.

### 2. Updated Call Site in `popularity_scan`

Changed the call to pass both `verbose` and `discogs_token`:

```python
detection_result = detect_single_for_track(
    title=title,
    artist=artist,
    album_track_count=album_track_count,
    spotify_results_cache=spotify_results_cache,
    verbose=verbose,  # Pass function parameter
    discogs_token=discogs_token  # Pass already-loaded token
)
```

### 3. Fixed Spotify Cache Key

Changed cache storage to use `title` as key:

```python
# Cache results for singles detection reuse (using title as key)
spotify_results_cache[title] = spotify_search_results
```

### 4. Always Log Config Errors

Removed the `if verbose:` guard from config loading error logging.

### 5. Updated Docstring

Fixed the docstring to reflect that the cache maps `title` to Spotify results, not `track_id`.

## Testing

Created comprehensive test suite in `test_discogs_lookup_fix.py` that validates:

1. **Discogs token parameter passing** - Verifies token is passed correctly and used by the Discogs client
2. **Verbose parameter passing** - Verifies verbose flag flows through the call stack
3. **Spotify cache key consistency** - Verifies cache uses correct key (title)
4. **Config loading error handling** - Verifies errors are logged even when verbose=False

All tests pass successfully.

## How to Verify the Fix

### In Docker Environment

1. Ensure `/config/config.yaml` exists and contains Discogs token:
   ```yaml
   api_integrations:
     discogs:
       enabled: true
       token: "your_discogs_token_here"
   ```

2. Run an artist scan via the artist page with verbose logging enabled

3. Check the logs at `/config/unified_scan.log` for:
   ```
   Detecting singles for "<Artist> - <Album>"
      Using sources: Spotify, MusicBrainz, Discogs, Discogs Video
      Checking Discogs for single: <Track Name>
      ✓ Discogs confirms single: <Track Name>
   ```
   OR
   ```
      ⓘ Discogs does not confirm single: <Track Name>
   ```

4. If you see "Checking Discogs for single" messages, the fix is working!

### Expected Behavior After Fix

- Discogs API will be called during single detection
- Verbose logs will show Discogs checks when artist scan is run with verbose=True
- Config loading errors will always be logged, making troubleshooting easier
- Spotify results will be cached and reused, reducing API calls

## Files Modified

- `popularity.py` - Core fixes to detect_single_for_track and popularity_scan
- `test_discogs_lookup_fix.py` - New comprehensive test suite

## Backward Compatibility

The changes are fully backward compatible:
- The `discogs_token` parameter is optional and defaults to loading from config
- Other call sites of `detect_single_for_track` (in `single_detector.py` and `test_alternate_versions.py`) continue to work without modification
- All existing tests pass
