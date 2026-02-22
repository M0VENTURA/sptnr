# Downloads Folder Monitoring with Beets Integration

## Overview

The enhanced downloads watcher monitors a designated downloads folder for new audio files (MP3, FLAC, M4A, OGG, OPUS, WMA) and automatically processes them using beets to rename and organize them into the main music library. After successful import, it triggers a Navidrome library scan to update the metadata.

## Features

- **Automatic File Detection**: Monitors the downloads folder for new audio files
- **Supported Formats**: MP3, FLAC, M4A, OGG, OPUS, WMA
- **Beets Integration**: Uses beets to automatically tag, rename, and move files
- **Navidrome Sync**: Triggers Navidrome library scan after successful imports
- **Configurable Scan Interval**: Customize how often the watcher checks for new files

## Components

### 1. Enhanced Downloads Watcher (`enhanced_downloads_watcher.py`)

A standalone service that continuously monitors the downloads folder and processes new files.

**Usage:**
```bash
python enhanced_downloads_watcher.py
```

**Key Features:**
- Scans downloads folder at regular intervals
- Detects new audio files by extension
- Uses beets to import and organize files
- Triggers Navidrome scan after successful imports
- Maintains a processed files cache to avoid re-processing

### 2. Music Watcher (`music_watcher.py`)

A comprehensive watcher service that monitors both downloads and music folders.

**Usage:**
```bash
python music_watcher.py
```

**Key Features:**
- Monitors downloads folder for new files
- Monitors music folder for changes
- Processes downloads using beets
- Triggers Navidrome sync when changes detected
- Runs initial Navidrome sync on startup

### 3. Navidrome API Integration

Added new methods to the NavidromeClient for triggering library scans:

- `start_scan()` - Trigger a Navidrome library scan
- `get_scan_status()` - Check if a scan is currently running

## Configuration

### Environment Variables

Configure the watcher using these environment variables:

```bash
# Downloads folder path (where new files are detected)
DOWNLOADS_DIR=/downloads/Music

# Music library folder (where files are organized)
MUSIC_ROOT=/music

# Beets configuration file
BEETS_CONFIG=/config/update_config.yaml

# Scan interval in seconds (default: 30)
WATCHER_SCAN_INTERVAL=30

# Navidrome connection settings
NAVIDROME_BASE_URL=http://localhost:4533
NAVIDROME_USER=admin
NAVIDROME_PASS=password
```

### Beets Configuration

The watcher uses the beets configuration at `/config/update_config.yaml` which should include:

```yaml
directory: /music
library: /config/beets/musiclibrary.db

import:
  autotag: true
  copy: false
  write: true            # Write tags to files
  incremental: true
  resume: no
  quiet: no

musicbrainz:
  enabled: true

# File organization paths
paths:
  default: $albumartist/$year - $album/$disc_and_track. $artist - $title
  comp: Various Artists/$year - $album/$disc_and_track. $artist - $title
  singleton: $artist/$year - $title/$track. $artist - $title
```

## How It Works

### File Processing Flow

1. **Detection**: Watcher scans the downloads folder at regular intervals
2. **Filtering**: Identifies new audio files (MP3, FLAC, etc.)
3. **Import**: Runs beets import on new files
   - Beets fetches metadata from MusicBrainz
   - Files are renamed according to the configured path format
   - Files are moved to the main music library
4. **Sync**: Triggers Navidrome library scan via Subsonic API
5. **Wait**: Waits for Navidrome to complete scanning
6. **Complete**: Files are now available in Navidrome with proper metadata

### Example

```
Downloads Folder:
  /downloads/Music/unknown_song.mp3

↓ (Watcher detects new file)

Beets Import:
  - Identifies: "Artist Name - Song Title"
  - From album: "Album Name (2023)"
  
↓ (Beets processes and moves file)

Music Library:
  /music/Artist Name/2023 - Album Name/101. Artist Name - Song Title.mp3

↓ (Navidrome scan triggered)

Navidrome:
  - File detected and indexed
  - Metadata available in Navidrome UI
```

## Running the Watcher

### Standalone Mode

Run the enhanced downloads watcher as a standalone service:

```bash
python enhanced_downloads_watcher.py
```

### With Docker

Add to your docker-compose.yml:

```yaml
services:
  sptnr:
    # ... existing configuration ...
    environment:
      - DOWNLOADS_DIR=/downloads/Music
      - MUSIC_ROOT=/music
      - WATCHER_SCAN_INTERVAL=30
      - NAVIDROME_BASE_URL=http://navidrome:4533
      - NAVIDROME_USER=admin
      - NAVIDROME_PASS=password
    volumes:
      - /path/to/downloads:/downloads/Music
      - /path/to/music:/music
```

### As a Background Service

You can also integrate the watcher into the main application by calling it from your startup script.

## Troubleshooting

### Beets Not Installed

If beets is not installed, the watcher will log a warning and skip file import:

```
⚠️ Beets not installed, skipping import
```

Install beets:
```bash
pip install beets
```

### Navidrome Connection Failed

If the watcher cannot connect to Navidrome:

```
⚠️ Could not trigger Navidrome scan: Connection refused
```

Check:
1. Navidrome is running and accessible
2. NAVIDROME_BASE_URL is correct
3. NAVIDROME_USER and NAVIDROME_PASS are correct

### Files Not Being Processed

Check the logs for:
1. File detection issues (wrong folder, permissions)
2. Beets import errors (metadata not found, invalid files)
3. File format support (only certain extensions are monitored)

## Testing

Run the test suite to verify the implementation:

```bash
python test_downloads_watcher.py
```

This tests:
- File detection for various audio formats
- Beets availability check
- Navidrome API connectivity

## Logging

The watcher logs all activity to:
- Console output (stdout)
- `/config/downloads_watcher.log` (enhanced watcher)
- `/config/music_watcher.log` (music watcher)

Log levels:
- INFO: Normal operation, file detection, imports
- WARNING: Non-critical issues (beets not installed, Navidrome unavailable)
- ERROR: Critical errors that prevent processing

## API Methods

### NavidromeClient.start_scan()

Trigger a library scan in Navidrome.

```python
from api_clients.navidrome import NavidromeClient

client = NavidromeClient(base_url, username, password)
success = client.start_scan()
```

Returns: `True` if scan was triggered successfully

### NavidromeClient.get_scan_status()

Check if a scan is currently running.

```python
status = client.get_scan_status()
# Returns: {"success": True, "scanning": False, "count": 0}
```

## Future Enhancements

Potential improvements for the downloads watcher:

1. **Real-time Monitoring**: Use inotify/watchdog for instant detection
2. **Parallel Processing**: Process multiple files concurrently
3. **Failed Import Retry**: Automatically retry failed imports
4. **Duplicate Detection**: Avoid importing duplicate files
5. **Format Conversion**: Auto-convert FLAC to MP3 for compatibility
6. **Smart Playlists**: Auto-add imported files to "Recently Added" playlist
7. **Notifications**: Send notifications when new files are imported

## Related Files

- `enhanced_downloads_watcher.py` - Standalone watcher service
- `music_watcher.py` - Combined downloads/music watcher
- `api_clients/navidrome.py` - Navidrome API client with scan methods
- `beets_integration.py` - Beets integration module
- `beets_auto_import.py` - Automated beets import functionality
- `test_downloads_watcher.py` - Test suite for the watcher
