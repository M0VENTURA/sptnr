# Discogs Music Video Detection Logging - Implementation Summary

## Problem Statement
User reported: 
> "I can now see that discogs is searching, but I'm not sure if discogs music video is running during the single detection as this is an official music video"
>
> Example: FEUERSCHWANZ - Bastard Von Asgard feat. Fabienne Erni (Eluveitie) (Official Video)

## Root Cause
The Discogs single detection code had a "Strong path 3" that checks for music videos in releases (lines 426-437 in `api_clients/discogs.py`), but it only logged at `logger.debug()` level. This meant users couldn't see when this check was running during normal operation.

## Solution Implemented
Enhanced the logging in the `is_single()` method to use the centralized logging system:

### Changes to `api_clients/discogs.py`:

1. **Added Centralized Logging Import** (lines 9-22):
   - Imports `log_unified`, `log_info`, `log_debug` from `logging_config`
   - Includes defensive try-except to support tests without LOG_PATH
   - Fallback to standard logger if centralized logging not available

2. **Enhanced Video Detection Logging** (lines 444-453):
   - Added `log_info()` to show when checking videos in a release
   - Changed from `logger.debug()` to `log_unified()` when video is found
   - Added detailed `log_info()` with video title information

### Before vs After

**Before:**
```python
# Strong path 3: Check for music videos in the release
videos = data.get("videos", []) or []
for video in videos:
    video_title = (video.get("title") or "").lower()
    video_desc = (video.get("description") or "").lower()
    if nav_title in video_title or nav_title in video_desc:
        logger.debug(f"Found video for '{title}' in Discogs release {rid}")  # ❌ DEBUG ONLY
        self._single_cache[cache_key] = True
        return True
```

**After:**
```python
# Strong path 3: Check for music videos in the release
videos = data.get("videos", []) or []
if videos:
    log_info(f"   Discogs: Checking {len(videos)} video(s) in release {rid} for '{title}'")  # ✅ VISIBLE
for video in videos:
    video_title = (video.get("title") or "").lower()
    video_desc = (video.get("description") or "").lower()
    if nav_title in video_title or nav_title in video_desc:
        log_unified(f"   ✓ Discogs confirms single via music video in release {rid}: {title}")  # ✅ VISIBLE
        log_info(f"   Discogs result: Music video found in release for '{title}' (video: {video.get('title', 'N/A')})")  # ✅ VISIBLE
        self._single_cache[cache_key] = True
        return True
```

## What Users Will See

When Discogs detects a single via music video (like the FEUERSCHWANZ example), the logs will now show:

```
[INFO] sptnr_   Discogs: Checking 1 video(s) in release 12345 for 'Bastard Von Asgard'
[INFO]    ✓ Discogs confirms single via music video in release 12345: Bastard Von Asgard
[INFO] sptnr_   Discogs result: Music video found in release for 'Bastard Von Asgard' (video: FEUERSCHWANZ - Bastard Von Asgard feat. Fabienne Erni (Eluveitie) (Official Video))
```

These messages appear in:
- `unified_scan.log` - For basic operational visibility
- `info.log` - For all requests and operations

## Testing

### Test Coverage
✅ **New Test Created**: `/tmp/test_video_logging.py`
- Verifies logging is visible when video is found
- Confirms no unnecessary logging when no videos present
- Validates logging pattern consistency with `popularity.py`

✅ **Existing Tests Pass**: `test_discogs_integration.py`
- Defensive import ensures backward compatibility
- No breaking changes to existing functionality

### Example Test Output
```
[INFO] sptnr_   Discogs: Checking 1 video(s) in release 12345 for 'Bastard Von Asgard'
[INFO]    ✓ Discogs confirms single via music video in release 12345: Bastard Von Asgard
[INFO] sptnr_   Discogs result: Music video found in release for 'Bastard Von Asgard' (video: FEUERSCHWANZ - Bastard Von Asgard feat. Fabienne Erni (Eluveitie) (Official Video))

✅ Video detection logging is visible
✅ Users can see when Discogs checks for music videos
✅ Users can see when a music video confirms single status
```

## Quality Checks

✅ **Code Review**: No issues in modified files
✅ **Security Scan**: 0 vulnerabilities found  
✅ **Syntax Check**: All files compile successfully
✅ **Integration Test**: Existing tests pass

## Impact

### User Experience
- **Before**: Users couldn't tell if video detection was running
- **After**: Clear, visible logging shows exactly when video checks occur

### Consistency
- Logging pattern matches other Discogs checks in `popularity.py`
- Messages use same format and level as other API integrations
- Fits seamlessly into existing logging infrastructure

### Performance
- No performance impact (same logic, just better logging)
- No additional API calls
- Minimal memory overhead (a few log messages)

## Files Modified
- `api_clients/discogs.py` - 21 lines added (logging imports + enhanced messages)

## How to Verify

1. **Enable Discogs API** in your config with a valid token
2. **Scan a track** that has an official music video on Discogs
3. **Check the logs** at `/config/unified_scan.log` or `/config/info.log`
4. **Look for** messages like:
   - `Discogs: Checking X video(s) in release...`
   - `✓ Discogs confirms single via music video...`

## Example: FEUERSCHWANZ Case

For the specific example mentioned in the problem statement:
- **Track**: Bastard Von Asgard
- **Artist**: FEUERSCHWANZ
- **Video**: Bastard Von Asgard feat. Fabienne Erni (Eluveitie) (Official Video)

The system will now clearly show:
1. When it searches Discogs for this track
2. When it finds a release with a video
3. When the video matches the track title
4. That the video confirms it as a single

## Backward Compatibility

✅ **100% Backward Compatible**
- Defensive import with fallback ensures no breaking changes
- Existing tests continue to pass
- No changes to API signatures
- No changes to database schema
- No changes to configuration requirements

## Conclusion

This minimal change (21 lines) solves the user's problem by making the Discogs music video detection **visible** during single detection scans. Users can now clearly see when this check is running and when it successfully identifies a track as a single based on an official music video.

The implementation follows best practices:
- Uses existing centralized logging infrastructure
- Maintains consistency with other API checks
- Includes comprehensive testing
- Has zero security vulnerabilities
- Is fully backward compatible
