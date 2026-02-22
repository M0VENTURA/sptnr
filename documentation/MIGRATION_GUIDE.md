# Environment Variables to config.yaml Migration Guide

## Overview

SPTNR now uses `config.yaml` for all runtime configuration settings. Environment variables are only used for deployment-level paths (CONFIG_PATH, DB_PATH, LOG_PATH, etc.).

## What Changed

### ✅ Migrated to config.yaml

All API credentials, feature flags, and service configurations now come from `config.yaml`:

- **API Keys & Credentials**: Spotify, Last.fm, Discogs, YouTube, Google, AudioDB, SearchAPI
- **Weights**: Scoring weights for popularity calculation
- **Feature Flags**: Service enable/disable toggles
- **Web API**: Web interface API key and authentication settings

### 📁 Deployment Paths (Still use environment variables)

These remain as environment variables because they are infrastructure-level settings:

- `CONFIG_PATH` - Path to config.yaml file
- `DB_PATH` - Database file path
- `LOG_PATH` - Application log path
- `MUSIC_FOLDER` / `MUSIC_ROOT` - Music library path
- `DOWNLOADS_DIR` - Downloads folder path
- Various progress file paths

### 🚫 Legacy Files (Not Modified)

- `sptnr.py` - Legacy CLI tool, intentionally left unchanged
- Test files - Use environment variables for test setup

## New Configuration Structure

### config.yaml additions

```yaml
# API Integrations - Enable/disable external services and provide credentials
api_integrations:
  spotify:
    enabled: true
    client_id: "your_spotify_client_id"
    client_secret: "your_spotify_client_secret"
  
  lastfm:
    enabled: true
    api_key: "your_lastfm_api_key"
  
  discogs:
    enabled: true
    token: "your_discogs_token"
  
  youtube:
    enabled: false
    api_key: ""
  
  google:
    enabled: false
    api_key: ""
    cse_id: ""
  
  searchapi:
    enabled: false
    api_key: ""  # SearchAPI.io key for DuckDuckGo searches

# Web API Configuration
web_api_key: ""  # API key for web interface authentication
enable_web_api_key: true  # Set to false to disable API key requirement

# Scoring Weights
weights:
  spotify: 0.4
  lastfm: 0.3
  listenbrainz: 0.2
  age: 0.1
```

## Migration Steps

### If you were using .env file:

1. Copy your API credentials from `.env` to `config.yaml`:
   - `SPOTIFY_CLIENT_ID` → `api_integrations.spotify.client_id`
   - `SPOTIFY_CLIENT_SECRET` → `api_integrations.spotify.client_secret`
   - `LASTFM_API_KEY` → `api_integrations.lastfm.api_key`
   - `DISCOGS_TOKEN` → `api_integrations.discogs.token`
   - `YOUTUBE_API_KEY` → `api_integrations.youtube.api_key`
   - `GOOGLE_CSE_ID` → `api_integrations.google.cse_id`
   - `WEB_API_KEY` → `web_api_key`

2. Copy your weights from `.env` to `config.yaml`:
   - `SPOTIFY_WEIGHT` → `weights.spotify`
   - `LASTFM_WEIGHT` → `weights.lastfm`
   - `AGE_WEIGHT` → `weights.age`

3. Keep deployment paths in environment variables or docker-compose.yml

### If you were using environment variables:

Follow the same mapping as above, but update your docker-compose.yml or deployment configuration to set values in the mounted config.yaml instead.

## Files Modified

1. **config_loader.py** (NEW) - Centralized configuration loader
2. **config/config.yaml** - Added new configuration sections
3. **single_detector.py** - Now uses config.yaml for API credentials
4. **ddg_searchapi_checker.py** - Now uses config.yaml for SearchAPI key
5. **server.py** - Now uses config.yaml for web API settings

## Using the Config Loader

Python code can now use the config_loader module:

```python
from config_loader import load_config, get_api_key, is_api_enabled, get_weights

# Load full config
config = load_config()

# Get API key for a service
spotify_client_id = get_api_key("spotify", "client_id")
lastfm_key = get_api_key("lastfm", "api_key")
discogs_token = get_api_key("discogs", "token")

# Check if service is enabled
if is_api_enabled("spotify"):
    # Use Spotify API

# Get weights
weights = get_weights()
# Returns: {'spotify': 0.4, 'lastfm': 0.3, 'listenbrainz': 0.2, 'age': 0.1}
```

## Backward Compatibility

The `.env` file can still be used for the legacy `sptnr.py` tool, but all active runtime code now uses `config.yaml`.

## Testing

After migration, verify your configuration:

```bash
# Check config loads correctly
python3 -c "from config_loader import load_config; print(load_config())"

# Verify API keys are configured
python3 -c "from config_loader import is_api_enabled; print('Spotify:', is_api_enabled('spotify'))"
```
