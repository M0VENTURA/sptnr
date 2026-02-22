# Multi-Tier Logging System

## Overview

Sptnr now uses a centralized multi-tier logging system with three distinct log files:

1. **Unified Log** (`unified_scan.log`) - Basic operational details for dashboard viewing
2. **Info Log** (`info.log`) - All requests and operations (except Flask HTTP logs)
3. **Debug Log** (`debug.log`) - Detailed debug information for troubleshooting

## Log Files

### Unified Scan Log (`/config/unified_scan.log`)
- **Purpose**: Dashboard-viewable log showing basic operational status
- **Content**: High-level summaries of:
  - Navidrome imports (e.g., "Navidrome: Scanning Artist X (10 albums)")
  - Popularity scanning (e.g., "Popularity: Scan started at 12:30:45")
  - Single detection (e.g., "Single detected: Track Y")
  - Beets scanning (e.g., "Beets: Imported 5 tracks")
- **Filters**: Automatically filters out:
  - HTTP request/response logs
  - Verbose debug messages
  - Internal system details
- **Rotation**: 10MB per file, 7 backups (7 days)

### Info Log (`/config/info.log`)
- **Purpose**: Comprehensive operational log for all non-Flask activities
- **Content**: Detailed information about:
  - API requests (external services like Spotify, Last.fm, etc.)
  - User actions and operations
  - System operations and state changes
  - Scan progress and completions
- **Prefix**: Service-specific (e.g., `navidrome_import_`, `popularity_`, `WebUI_`)
- **Rotation**: 10MB per file, 7 backups

### Debug Log (`/config/debug.log`)
- **Purpose**: Detailed troubleshooting information
- **Content**: All debug-level logs including:
  - Stack traces and error details
  - Verbose operation details
  - Internal function calls and parameters
  - API response debugging
- **Prefix**: Service-specific (e.g., `navidrome_import_`, `popularity_`, `WebUI_`)
- **Rotation**: 10MB per file, 7 backups

## Using the Logging System

### In Python Code

```python
from logging_config import log_unified, log_info, log_debug

# Log basic operation to dashboard (unified log only)
log_unified(f"Scanning artist: {artist_name}")

# Log detailed operation info (info log only)
log_info(f"Processing album: {album_name} with {track_count} tracks")

# Log debug information (debug log only)
log_debug(f"API response: {api_response}")
```

### Service-Specific Setup

```python
from logging_config import setup_logging

# Set up logging with service name prefix
setup_logging("my_service")
```

### Dashboard Access

1. **View Unified Log**: Visible on the main dashboard in the "Unified Log" section
2. **Download Logs**: Click download buttons below the log viewer:
   - "Unified (1h)" - Last hour of unified log
   - "Info (1h)" - Last hour of info log
   - "Debug (1h)" - Last hour of debug log

## Log Download API

### Endpoint
```
GET /api/download-log/<log_type>
```

### Parameters
- `log_type`: One of `unified`, `info`, or `debug`

### Returns
- Plain text file containing last hour of the specified log
- Filename format: `{log_type}_log_{timestamp}.txt`

### Example
```bash
curl http://localhost:5000/api/download-log/unified -o unified_log.txt
```

## Configuration

### Environment Variables

- `LOG_PATH`: Base directory for log files (default: `/config`)
- `UNIFIED_SCAN_LOG_PATH`: Override path for unified log
- `SPTNR_VERBOSE`: Enable verbose logging (set to "1")

### Log Rotation

Logs automatically rotate when they reach 10MB:
- Each log keeps 7 backup files (approximately 7 days of history)
- Old backups are automatically deleted
- Format: `logfile.log`, `logfile.log.1`, `logfile.log.2`, etc.

## Migrating Old Code

### Old Pattern (deprecated)
```python
logging.info("Processing track")
```

### New Pattern
```python
from logging_config import log_info
log_info("Processing track")
```

### Legacy Compatibility

Old logging patterns are still supported through wrapper functions:
- `log_basic()` → redirects to `log_info()`
- `log_verbose()` → redirects to `log_debug()`

## Best Practices

1. **Use appropriate log level**:
   - `log_unified()` - Only for dashboard-worthy status updates
   - `log_info()` - For operational details and API calls
   - `log_debug()` - For troubleshooting and verbose details

2. **Keep unified log concise**:
   - Avoid logging every single operation
   - Focus on high-level progress (artist, album level)
   - Don't log implementation details

3. **Include context in messages**:
   ```python
   # Good
   log_info(f"[Navidrome] Imported {count} tracks from {album}")
   
   # Bad
   log_info(f"Imported tracks")
   ```

4. **Use debug for verbose details**:
   ```python
   log_debug(f"API request params: {params}")
   log_debug(f"Response status: {response.status_code}")
   ```

## Troubleshooting

### No logs appearing
1. Check log file permissions in `/config`
2. Verify `LOG_PATH` environment variable
3. Check disk space

### Dashboard not showing logs
1. Verify `/config/unified_scan.log` exists and is readable
2. Check browser console for API errors
3. Refresh the page

### Download buttons not working
1. Ensure logs contain timestamps in format: `YYYY-MM-DD HH:MM:SS`
2. Check API endpoint: `GET /api/download-log/unified`
3. Verify browser doesn't block downloads

## Technical Details

### UnifiedLogFilter

The unified log uses a custom filter (`UnifiedLogFilter`) that:
- Blocks HTTP request logs (GET/POST patterns)
- Blocks messages containing `[DEBUG]` or `[VERBOSE]`
- Allows all other INFO-level messages through

### ServicePrefixFormatter

Adds service-specific prefixes to log messages:
- Format: `{service_name}_{message}`
- Examples: `WebUI_Request received`, `popularity_Scanning artist`

### Log File Structure
```
/config/
├── unified_scan.log      # Unified log (filtered, dashboard view)
├── info.log              # Info log (detailed operations)
├── debug.log             # Debug log (verbose troubleshooting)
└── sptnr.log             # Legacy log (deprecated, may be removed)
```
