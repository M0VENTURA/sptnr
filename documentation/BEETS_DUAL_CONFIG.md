# Beets Dual-Config Implementation

## Overview

This implementation provides two separate beets YAML configurations to handle different use cases:

1. **read_config.yml** - For library scanning and metadata import (non-destructive)
2. **update_config.yml** - For tag writing and file organization (with file modifications)

This separation ensures safe, controlled metadata management with clear user intent.

## Configuration Files

### read_config.yml (Read-Only)
Location: `/config/read_config.yml`

```yaml
directory: /music
library: /config/beets/musiclibrary.db

import:
  autotag: true          # Fetch MusicBrainz data for DB
  copy: false            # Do not copy files
  write: false           # Do NOT write tags to files
  incremental: true      # Skip already imported files
  log: /config/beets_import.log
  resume: no
  quiet: no

musicbrainz:
  enabled: true

plugins:
  - duplicates
  - info
```

**Purpose**: Used during automatic library scans
- Fetches metadata from MusicBrainz
- Updates beets database
- Does NOT modify any files or tags
- Safe to run repeatedly without side effects

**Usage**: `beet -c /config/read_config.yml import /music`

### update_config.yml (Update/Write)
Location: `/config/update_config.yml`

```yaml
directory: /music
library: /config/beets/musiclibrary.db

import:
  autotag: true
  copy: false
  write: true            # WRITE tags to files
  incremental: true
  resume: no
  quiet: no

musicbrainz:
  enabled: true

plugins:
  - duplicates
  - info
  - convert

item_fields:
  disc_and_track: u'%d%02d' % (disc, track)

paths:
  default: $albumartist/$year - $album/$disc_and_track. $artist - $title
  comp: Various Artists/$year - $album/$disc_and_track. $artist - $title
  albumtype:soundtrack: Soundtrack/$year - $album/$disc_and_track. $artist - $title
  singleton: $artist/$year - $title/$track. $artist - $title

convert:
  auto: no               # Don't auto-convert on import
  copy: yes
  format: mp3
  bitrate: 320
  threads: 2
  dest: /music/mp3
  never_convert_lossy: yes
```

**Purpose**: Used when user explicitly wants to update an album
- Writes corrected metadata tags to files
- Reorganizes files based on standardized paths
- Can convert formats if needed
- User-initiated only (explicit button click)

**Usage**: `beet -c /config/update_config.yml move path:/music/Artist\ Name/Album\ Name`

## Database Changes

### New Column: `album_folder`
Added to `tracks` table in check_db.py

```sql
ALTER TABLE tracks ADD COLUMN album_folder TEXT;
```

**Purpose**: Stores the directory path of the album folder

**Populated during**: Beets sync via `sync_beets_to_sptnr()`

**Extraction logic**:
```python
# From track file path: /music/Artist Name/Year - Album/01.mp3
# Extract: /music/Artist Name/Year - Album
album_folder = str(Path(beets_path).parent)
```

**Usage**: Enables selective album updates without needing to specify full path

## Code Architecture

### BeetsAutoImporter Class Updates

```python
class BeetsAutoImporter:
    def __init__(self, ...):
        # Path references
        self.beets_config_readonly = Path(CONFIG_PATH) / "read_config.yml"
        self.beets_config_update = Path(CONFIG_PATH) / "update_config.yml"
        self.beets_config = self.beets_config_readonly  # Default
        
    def ensure_beets_config(self, use_update: bool = False):
        """Switch between read-only and update configs"""
        config_file = (self.beets_config_update if use_update 
                      else self.beets_config_readonly)
        self.beets_config = config_file
        
    def _create_config_file(self, config_path: Path, readonly: bool = True):
        """Create either config type with appropriate settings"""
        if readonly:
            # Read-only settings: autotag=True, write=False
        else:
            # Update settings: write=True, file paths, convert plugin
```

### Beets Sync Enhancement

During `sync_beets_to_sptnr()`:
```python
# Extract album folder from track path
album_folder = str(Path(beets_path).parent)

# Store in database
cursor.execute("""
    UPDATE tracks SET
        ...
        album_folder = ?
    WHERE id = ?
""", (..., album_folder, ...))
```

## New Module: beets_update.py

### Main Function
```python
def update_album_with_beets(album_folder: str) -> Dict:
    """
    Update an album folder with beets.
    
    Command: beet -c /config/update_config.yml move path:{album_folder}
    
    Returns:
    {
        "success": bool,
        "message": str,
        "output": str,  # Command output
        "folder": str,  # Album folder path
        "error": str    # Error message if failed
    }
    """
```

### Helper Functions
```python
def get_album_folder_for_artist_album(artist: str, album: str) -> Optional[str]:
    """Get album folder from database"""

def get_album_folder_for_track(track_id: str) -> Optional[str]:
    """Get album folder for specific track"""

def get_all_album_folders_for_artist(artist: str) -> list:
    """Get all album folders for an artist"""
```

## API Endpoints

### POST /api/beets/update-album
Update a specific album with beets.

**Request**:
```json
{
  "artist": "Artist Name",
  "album": "Album Name"
}
// OR
{
  "folder": "/music/Artist Name/Album Name"
}
```

**Response** (success):
```json
{
  "success": true,
  "message": "Album updated: /music/...",
  "folder": "/music/...",
  "output": "beets output..."
}
```

**Response** (error):
```json
{
  "success": false,
  "error": "Error message",
  "folder": "/music/..."
}
```

### GET /api/beets/album-folders/{artist}
Get all album folders for an artist.

**Response**:
```json
{
  "success": true,
  "artist": "Artist Name",
  "album_folders": [
    "/music/Artist Name/2020 - Album 1",
    "/music/Artist Name/2021 - Album 2"
  ],
  "count": 2
}
```

## User Interface

### Album Page
- **Button**: "Update with Beets"
- **Location**: Album header button bar
- **Action**: Updates single album
- **Confirmation**: Asks user to confirm

### Artist Page
- **Button**: "Update All Albums"
- **Location**: Artist header button bar
- **Action**: Updates all albums for artist sequentially
- **Confirmation**: Shows number of albums to update

### JavaScript Functions

```javascript
function updateAlbumWithBeets(artist, album)
  // Update single album
  // POST /api/beets/update-album
  // Reload page on success

function updateArtistAlbumsWithBeets(artist)
  // Get all folders: GET /api/beets/album-folders/{artist}
  // Update each sequentially
  // Show progress
  // Reload page when complete

function updateAlbumsSequentially(folders, index, callback)
  // Helper for batch processing
  // Updates each album folder one by one
```

## Workflow

### Automatic Library Scan (Read-Only)
```
1. User clicks "Scan" or scheduled scan runs
2. BeetsAutoImporter uses read_config.yml
3. Command: beet -c /config/read_config.yml import /music
4. Fetches metadata from MusicBrainz
5. Updates beets database
6. Syncs to sptnr database with album_folder
7. No files are modified
```

### Manual Album Update (Write)
```
1. User sees album on artist/album page
2. Clicks "Update with Beets" button
3. System confirms action
4. API calls beet with update_config.yml
5. Command: beet -c /config/update_config.yml move path:/music/Album
6. Beets:
   - Verifies/updates metadata tags
   - Reorganizes files based on path template
   - May convert formats if configured
7. Navidrome rescan triggered (TODO)
8. UI updates showing success
```

## Safety Features

1. **Separate Configs**: Read-only config has `write: false` - safe to run anytime
2. **Explicit User Action**: Tag writing requires clicking button (not automatic)
3. **Confirmation Dialog**: User must confirm before updating
4. **Error Handling**: Failed updates don't crash system
5. **Rollback Possible**: Files in beets DB, can revert with command
6. **Logging**: All operations logged for debugging

## Questions Answered

### Are album locations stored?
**Yes**, during beets sync:
- Each track's file path is stored in `beets_path` column
- Album folder is extracted from track path and stored in `album_folder` column
- Enables selective album updates

### Why two configs?
1. **Safety**: Read-only mode can't accidentally modify files
2. **Performance**: Import can focus on metadata fetching
3. **User Intent**: Clear distinction between scan (automatic) and update (manual)
4. **Flexibility**: Users can manually adjust tags when needed

### When should each be used?
- **read_config.yml**: All automatic scans, routine library maintenance
- **update_config.yml**: Only when user explicitly wants to fix/organize tracks

## Future Enhancements

1. **Navidrome Rescan Integration**: Trigger rescan after beets update
2. **Batch Operations**: Update multiple artists at once
3. **Smart Conflict Resolution**: Handle cases where beets and Navidrome disagree
4. **Undo/Rollback**: Ability to revert unsuccessful updates
5. **Progress Tracking**: Real-time progress for batch updates
6. **Email Notifications**: Alert when batch updates complete
7. **Format Conversion**: Enable MP3 conversion in update workflow
8. **Tag Templates**: Customize file naming and organization patterns

## Troubleshooting

### Album folder is NULL
- **Cause**: Track added before album_folder column existed
- **Fix**: Rescan library to repopulate album_folder
- **Command**: `beet -c /config/read_config.yml import /music`

### Update command fails
- Check beets is installed: `beet --version`
- Verify album folder exists: `ls /music/Artist/Album`
- Check file permissions in `/music`
- Review beets logs: `/config/beets_import.log`

### Files in wrong location
- Verify `paths:` section in update_config.yml
- Run: `beet -c /config/update_config.yml info path:/music/Album`
- This shows what beets would do without moving

## Testing

### Test Read-Only Config
```bash
beet -c /config/read_config.yml import /music/TestArtist
# Should not modify files
ls -la /music/TestArtist  # Check timestamps unchanged
```

### Test Update Config (Dry Run)
```bash
beet -c /config/update_config.yml info path:/music/TestArtist/TestAlbum
# Shows proposed changes without applying them
```

### Test via Web UI
1. Go to artist page
2. Click "Update All Albums"
3. Monitor progress
4. Check files are organized
5. Verify Navidrome still works
