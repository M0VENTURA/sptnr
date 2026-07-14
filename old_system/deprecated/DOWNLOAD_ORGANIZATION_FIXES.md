# Download Queue File Organization Fixes

## Summary

Fixed critical issues preventing MP3 files from being moved from `/downloads` to `/music` in the download queue manager system.

## Issues Identified & Fixed

### 1. No Fallback When Beets Fails

**Problem:**
- The API endpoint only tried beets import
- If beets failed, no fallback mechanism existed
- Users had no way to move files manually

**Solution:**
- Added robust two-tier approach:
  - **Tier 1**: Beets import (primary, optimized for metadata)
  - **Tier 2**: Manual `shutil.move()` fallback (ensures file moves even if beets fails)

### 2. False Positive Success Detection

**Problem:**
- Marked files as "imported" just because beets returncode was 0
- Didn't verify the file actually moved
- Database showed success, but file still in /downloads

**Solution:**
- Added explicit verification after each move attempt
- Checks `os.path.exists()` before marking as imported

### 3. Missing Beets Configuration

**Problem:**
- Hard-coded config path might not exist
- No fallback if `BEETS_UPDATE_CONFIG` env var not set

**Solution:**
- Check if config exists before running beets
- Log warnings with helpful context
- Continue to fallback if config missing

### 4. Directory Structure Not Created

**Problem:**
- Manual fallback tried to move files to non-existent directories
- Would fail with "No such file or directory" error

**Solution:**
- Create full directory structure before move using `os.makedirs()`

### 5. Duplicate File Handling

**Problem:**
- If file already exists at destination, move would fail
- No collision detection

**Solution:**
- Check for existing files and add numeric suffix
- Examples: `song.mp3` → `song_1.mp3` → `song_2.mp3`

### 6. Insufficient Logging

**Problem:**
- Couldn't troubleshoot failures
- No visibility into which method succeeded

**Solution:**
- Added comprehensive logging tagged with `[ORGANIZE]`
- Shows which method succeeded in response

## API Endpoint Changes

### `/api/queue/<int:queue_id>/organize` (Single File)

**Improvements:**
- Beets verification (checks file actually moved)
- Manual fallback with `shutil.move()`
- Directory creation with `os.makedirs()`
- Duplicate filename handling
- Comprehensive [ORGANIZE] logging
- Returns method used ("beets" or "manual_fallback")

### `/api/queue/organize-group` (Multiple Files)

**Improvements:**
- Per-item beets + fallback logic
- Group-level error collection
- Per-file logging
- Returns success count and errors

## Testing

### Quick Test
1. Go to Downloads page
2. Find a completed download
3. Click "Organize" button
4. Check logs for [ORGANIZE] messages
5. Verify file moved to /music/artist/album/

### Monitor Logs
```bash
docker logs -f sptnr 2>&1 | grep "\[ORGANIZE\]"
```

## Debugging
- **File exists**: Check `/downloads` directory
- **Permissions**: Both `/downloads` and `/music` need write access
- **Disk space**: `df -h /music`
- **Beets**: `which beet`
- **Config**: `cat /config/update_config.yaml`

## Compatibility
✅ Fully backward compatible
- Existing working beets imports continue to work
- No changes to database schema
- Fallback only activates if beets fails
- API responses expanded but compatible
