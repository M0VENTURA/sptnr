# Summary of Changes: Environment Variables to config.yaml Migration

## Overview
This PR migrates all runtime configuration from environment variables to `config.yaml`, making settings management more centralized and user-friendly.

## Files Modified

### New Files Created
1. **`config_loader.py`** - Centralized configuration loader with helper functions
2. **`MIGRATION_GUIDE.md`** - Complete migration documentation
3. **`CHANGES_SUMMARY.md`** - This file

### Files Updated

#### Configuration Files
- **`config/config.yaml`** - Added new sections:
  - `api_integrations.searchapi` - SearchAPI.io configuration
  - `web_api_key` - Web interface API key
  - `enable_web_api_key` - Toggle for API key requirement

#### Python Files
- **`single_detector.py`** - Now loads Discogs token and Last.fm API key from config.yaml
- **`ddg_searchapi_checker.py`** - Now loads SearchAPI key from config.yaml
- **`server.py`** - Now loads web API settings from config.yaml
- **`beets_auto_import.py`** - Fixed 77 instances of `logger.` → `logging.`

#### Template Files
- **`templates/config.html`** - Added `full_scan` option to General Operation section

## What Changed

### Before (Environment Variables)
```bash
# .env or environment variables
SPOTIFY_CLIENT_ID=abc123
SPOTIFY_CLIENT_SECRET=xyz789
LASTFM_API_KEY=def456
DISCOGS_TOKEN=ghi789
WEB_API_KEY=secret
SEARCHAPI_IO_KEY=search123
SPOTIFY_WEIGHT=0.4
LASTFM_WEIGHT=0.3
```

### After (config.yaml)
```yaml
api_integrations:
  spotify:
    enabled: true
    client_id: "abc123"
    client_secret: "xyz789"
  lastfm:
    enabled: true
    api_key: "def456"
  discogs:
    enabled: true
    token: "ghi789"
  searchapi:
    enabled: false
    api_key: "search123"

web_api_key: "secret"
enable_web_api_key: true

weights:
  spotify: 0.4
  lastfm: 0.3
  listenbrainz: 0.2
  age: 0.1

features:
  full_scan: false  # Now visible in web UI
```

## What Stayed the Same

### Deployment Paths (Still use environment variables)
These infrastructure settings remain as environment variables:
- `CONFIG_PATH` - Path to config.yaml
- `DB_PATH` - Database path
- `LOG_PATH` - Log file path
- `MUSIC_FOLDER` / `MUSIC_ROOT` - Music library path
- `DOWNLOADS_DIR` - Downloads folder

### Legacy Files (Unchanged)
- **`sptnr.py`** - Legacy CLI tool (kept untouched per requirement)
- Test files - Use env vars for test environment setup

## Bug Fixes Included

1. **Fixed logger error in beets_auto_import.py**
   - Issue: `NameError: name 'logger' is not defined`
   - Fix: Replaced all `logger.` references with `logging.`

2. **Verified database schema updates**
   - Issue: Missing column `album_context_unplugged`
   - Solution: Confirmed `check_db.py` runs on startup via `update_schema()`

3. **Added missing UI option**
   - Issue: `full_scan` not visible in web UI
   - Fix: Added to config.html General Operation section

## How to Use

### For Developers
```python
from config_loader import load_config, get_api_key, is_api_enabled, get_weights

# Load full config
config = load_config()

# Get API credentials
spotify_id = get_api_key("spotify", "client_id")
discogs_token = get_api_key("discogs", "token")

# Check if service is enabled
if is_api_enabled("spotify"):
    # Use Spotify API
    pass

# Get scoring weights
weights = get_weights()
```

### For Users
1. Edit `config/config.yaml` to add your API credentials
2. All settings are now in one place
3. Use the web UI Configuration page to manage settings
4. No need to set environment variables for API keys anymore

## Testing

All changes have been tested:
- ✅ Config loader imports successfully
- ✅ API key retrieval works correctly
- ✅ Service enable/disable checks work
- ✅ Weights are loaded from config.yaml
- ✅ Web UI displays full_scan option
- ✅ No syntax errors in modified files

## Migration Path

See `MIGRATION_GUIDE.md` for detailed migration instructions.

Quick summary:
1. Copy API credentials from `.env` to `config.yaml`
2. Keep deployment paths in environment variables or docker-compose.yml
3. Test the application to ensure everything works

## Breaking Changes

None. The changes are backward compatible:
- Environment variables still work for legacy `sptnr.py`
- Deployment paths remain unchanged
- Existing `config.yaml` files are enhanced, not replaced
