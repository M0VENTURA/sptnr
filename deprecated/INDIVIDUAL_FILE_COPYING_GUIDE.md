# Individual File Download Organization System

## Overview

The download queue system has been enhanced to handle **individual file matching and copying** instead of requiring top-level folder matching. This allows files from the same album to be in different subfolders and still be properly organized.

## Key Features

### 1. Individual File Matching

- Each file is now matched independently within the downloads folder
- Files can be in any subfolder (e.g., `/downloads/artist/album/track.mp3` or `/downloads/track.mp3`)
- Fuzzy matching on artist, album, and title ensures flexible matching
- No requirement for matching top-level folder structure

### 2. Per-File Copy Options

- Each completed file in the queue shows individual copy button
- One-click copy to `/music` location
- Uses the configured naming convention from config.html:
  - **File format**: `[track_number]. [artist] - [title].[ext]`
  - **Directory structure**: `/music/[album_artist]/[year] - [album]/`

### 3. MusicBrainz Metadata Updates

- Before copying, file metadata is automatically updated with:
  - Track number and disc number
  - Artist and album artist names
  - Album name and release year
  - Title (from queue metadata)
- Applies to both MP3 and FLAC files
- FLAC files can be converted to MP3 during copy (optional)

### 4. Individual File Tracking

- Each file copy is tracked in the queue with:
  - `copied_individually` flag (0/1)
  - `copied_individually_at` timestamp
  - Status updated to `imported` after successful copy
- Album copy progress shown as percentage (e.g., "3 of 5 files copied")

### 5. Album Progress Tracking

- View which files in an album have been copied
- See overall album copy completion percentage
- Know which files still need to be copied

## New Functions

### `copy_queue_item_file_to_music(queue_id, music_dir=None)`

**Purpose**: Copy a single queue item file to the music directory.

**Parameters**:
- `queue_id` (int): The queue item ID to copy
- `music_dir` (str, optional): Override music directory path

**Returns**:
```python
{
    'success': bool,              # Whether copy succeeded
    'target_path': str,           # Where file was copied to
    'error': str,                 # Error message if failed
    'metadata_updated': bool,     # Whether tags were updated
    'file_copied': bool           # Whether file was actually copied
}
```

**Usage**:
```python
from download_queue_manager import copy_queue_item_file_to_music

result = copy_queue_item_file_to_music(queue_id=42)
if result['success']:
    print(f"File copied to: {result['target_path']}")
```

### `mark_file_as_copied_individually(queue_id, target_path=None)`

**Purpose**: Mark a file as having been copied individually (tracks album progress).

**Parameters**:
- `queue_id` (int): The queue item ID
- `target_path` (str, optional): The path where file was copied to

**Returns**: Updated queue item dict or None

**Usage**:
```python
from download_queue_manager import mark_file_as_copied_individually

result = mark_file_as_copied_individually(queue_id=42, target_path="/music/Artist/2024 - Album/01. Artist - Song.mp3")
```

### `get_album_files_with_status(album, album_artist, downloads_dir=None)`

**Purpose**: Get all files for an album showing their copy status.

**Parameters**:
- `album` (str): Album name
- `album_artist` (str): Album artist name
- `downloads_dir` (str, optional): Override downloads directory

**Returns**:
```python
{
    'album': str,                 # Album name
    'artist': str,                # Artist name
    'files': [                    # List of file records
        {
            'queue_id': int,
            'filename': str,
            'file_path': str,
            'title': str,
            'track_number': str,
            'status': str,        # 'discovered', 'downloading', 'completed', 'imported'
            'copied': bool,       # Whether this file has been copied
            'copied_at': datetime # When it was copied
        }
    ],
    'summary': {
        'total': int,             # Total tracks in album
        'copied': int,            # How many have been copied
        'pending': int,           # How many still need to be copied
        'progress_pct': float     # Percentage copied (0-100)
    }
}
```

**Usage**:
```python
from download_queue_manager import get_album_files_with_status

status = get_album_files_with_status("Fear Inoculum", "Tool")
print(f"Album progress: {status['summary']['progress_pct']}% copied")
for file in status['files']:
    print(f"  {file['title']}: {'✓ copied' if file['copied'] else '○ pending'}")
```

### `get_album_copy_progress(album, album_artist)`

**Purpose**: Quick summary of album copy progress.

**Parameters**:
- `album` (str): Album name
- `album_artist` (str): Album artist name

**Returns**:
```python
{
    'album': str,
    'artist': str,
    'total_tracks': int,
    'copied_tracks': int,
    'pending_tracks': int,
    'progress_pct': float,        # 0-100
    'is_complete': bool           # All files copied?
}
```

**Usage**:
```python
from download_queue_manager import get_album_copy_progress

progress = get_album_copy_progress("Fear Inoculum", "Tool")
if progress['is_complete']:
    print(f"✓ Album complete! All {progress['total_tracks']} files copied")
else:
    print(f"Album progress: {progress['copied_tracks']}/{progress['total_tracks']} files")
```

## New Module: `download_file_manager.py`

This module handles the actual file operations for individual file copying.

### `copy_file_to_music(source_file_path, queue_item, music_dir)`

**Purpose**: Copy a file from downloads to music with metadata and proper naming.

**Workflow**:
1. Reads file metadata from source
2. Updates file tags with MusicBrainz metadata
3. Prepares target path using naming convention
4. Copies file to destination
5. Returns success/failure status

### `update_file_metadata(file_path, metadata)`

**Purpose**: Update ID3/FLAC tags with new metadata.

**Metadata fields**:
- `title`, `artist`, `album_artist`, `album`
- `year`, `track_number`, `disc_number`
- `ext` (file extension)

### `prepare_filename_and_path(music_dir, metadata)`

**Purpose**: Generate proper filename and directory path.

**Naming Formula**:
- Directory: `{music_dir}/{album_artist}/{year} - {album}/`
- Filename: `{track_number}. {artist} - {title}.{ext}`

**Examples**:
- Track 1 on album: `01. Tool - Fear Inoculum.mp3`
- Track 3 on multi-disc: `103. Pink Floyd - Shine On You Crazy Diamond.flac`

## Database Schema Changes

New columns added to `download_queue` table:

```sql
ALTER TABLE download_queue ADD COLUMN track_number TEXT;
ALTER TABLE download_queue ADD COLUMN disc_number TEXT;
ALTER TABLE download_queue ADD COLUMN album_artist TEXT;
ALTER TABLE download_queue ADD COLUMN year TEXT;
ALTER TABLE download_queue ADD COLUMN copied_individually INTEGER DEFAULT 0;
ALTER TABLE download_queue ADD COLUMN copied_individually_at TEXT;
```

These columns track:
- **track_number**: Track number from metadata
- **disc_number**: Disc number (for multi-disc albums)
- **album_artist**: Album artist name (may differ from track artist)
- **year**: Release year
- **copied_individually**: Flag (1=copied, 0=not copied)
- **copied_individually_at**: Timestamp when file was copied

## Web UI Integration Points

### Queue Item Display
Each completed queue item in the UI should show:
1. File name and status
2. Artist - Title  
3. Album and year
4. Copy button (if not already copied)
5. Copy progress indicator (X of Y files copied)

### API Endpoints (to be implemented)

- `GET /api/queue/{queue_id}/copy-status` - Get copy status of single file
- `POST /api/queue/{queue_id}/copy` - Copy a single file to music directory
- `GET /api/albums/{artist}/{album}/files-status` - Get all files and copy status for album
- `GET /api/albums/{artist}/{album}/copy-progress` - Get album copy progress

## Example Workflows

### Copy Single File
```python
# 1. Get queue item
queue_item = get_queue(status='completed', limit=1)[0]

# 2. Copy to music
result = copy_queue_item_file_to_music(queue_item['id'])
if result['success']:
    print(f"✓ Copied to: {result['target_path']}")
else:
    print(f"✗ Error: {result['error']}")
```

### Copy Entire Album
```python
# 1. Get all files for album
status = get_album_files_with_status("Album Name", "Artist Name")

# 2. Copy each pending file
for file in status['files']:
    if not file['copied']:
        result = copy_queue_item_file_to_music(file['queue_id'])
        print(f"{file['title']}: {'✓' if result['success'] else '✗'}")

# 3. Check final progress
progress = get_album_copy_progress("Album Name", "Artist Name")
print(f"Final: {progress['progress_pct']}% complete")
```

### Get Album Status for Display
```python
# Fetch status for showing in UI
status = get_album_files_with_status("Fear Inoculum", "Tool")

# Display header
print(f"{status['album']} - {status['artist']}")
print(f"Progress: {status['summary']['copied']}/{status['summary']['total']} files copied")

# Display file list
for file in status['files']:
    icon = "✓" if file['copied'] else "○"
    print(f"  {icon} {file['track_number']}. {file['title']}")
```

## Key Advantages

1. **No Folder Matching Required**: Files can be in any subfolder structure
2. **Per-File Control**: Copy individual tracks or entire albums as needed
3. **Automatic Metadata**: MusicBrainz data from queue automatically applied
4. **Progress Tracking**: See exactly which files have been copied
5. **Album Awareness**: Track progress across all files in an album
6. **Clean Organization**: All files organized consistently in `/music`
7. **Flexible**: Copy one at a time or all at once based on your preference

## Implementation Notes

- Queue item must have `file_path` set (file must exist in downloads)
- Queue item must have MusicBrainz metadata (track_number, album_artist, year, etc.)
- Files are copied, not moved (originals remain in downloads for verification)
- Metadata is updated BEFORE copying to ensure tags are correct in destination
- Both MP3 and FLAC files are supported
- Windows and Linux paths are handled transparently
