# Fix Summary: Discogs Lookup Logging in Single Detection

## Problem Statement
Discogs lookup was not showing up in any of the three log files during single detection scanning on `popularity.py`.

## Root Cause
The `single_detection_enhanced.py` module was using Python's standard `logging` module (`logger.info()`, `logger.debug()`) instead of the centralized logging functions (`log_unified()`, `log_info()`, `log_debug()`) from `logging_config.py`.

This meant that when advanced single detection was enabled (which is the default), Discogs API calls were being logged to Python's default logging system instead of to the three log files that users were checking:
- `unified_scan.log` - User-friendly operational messages
- `info.log` - Detailed API call information  
- `debug.log` - Verbose debugging information

## Solution
Updated `single_detection_enhanced.py` to use centralized logging functions:

### 1. Added Centralized Logging Imports
```python
# Import centralized logging functions
# Use centralized logging to ensure API activity appears in unified_scan.log, info.log, and debug.log
# instead of Python's default logging system which doesn't route to these files
from logging_config import log_unified, log_info, log_debug
```

### 2. Updated Discogs API Logging
**Before:**
```python
logger.info(f"   Checking Discogs for single: {title}")
```

**After:**
```python
log_unified(f"   Checking Discogs for single: {title}")
log_info(f"   Discogs API: Searching for single '{title}' by '{artist}'")
```

### 3. Updated MusicBrainz API Logging
Applied the same pattern to MusicBrainz logging for consistency.

### 4. Updated Debug Logging
Changed all `logger.debug()` calls to `log_debug()` throughout the file.

### 5. Optimized Log Noise
- Removed `if verbose:` guards from active API calls (Discogs/MusicBrainz checks and results)
- Kept `if verbose:` guards for client availability messages to reduce log noise

## Testing
Created and ran an integration test that verified:
- ✅ `unified_scan.log` receives user-friendly Discogs status messages
- ✅ `info.log` receives detailed Discogs API call information
- ✅ `debug.log` receives verbose debugging information

## Expected Behavior After Fix

When popularity scanning runs with single detection, users will now see:

### unified_scan.log
```
   Checking Discogs for single: Track Name
   ✓ Discogs confirms single: Track Name
```

### info.log
```
   Discogs API: Searching for single 'Track Name' by 'Artist Name'
   Discogs result: Single confirmed for 'Track Name'
```

### debug.log (when verbose mode enabled)
```
   [DEBUG] Single detection sources for Track Name: ['discogs']
   [DEBUG] Final single status for Track Name: high
```

## Files Modified
- `single_detection_enhanced.py` - Updated all logging calls to use centralized logging functions

## Impact
- 🔍 Discogs and MusicBrainz API activity now visible in all three log files
- 📊 Better debugging and troubleshooting for single detection
- ✅ Consistent with existing logging pattern for Spotify and Last.fm in `popularity.py`
- 🎯 Reduced log noise by keeping client availability messages verbose-only

## Backward Compatibility
No breaking changes. The fix only affects logging output, not functionality.
