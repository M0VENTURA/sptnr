# Automatic Post-Download Processing

## Overview

The post-download processing system automatically organizes and tags downloaded tracks when they are downloaded via MusicBrainz or Discogs match. This feature ensures that your music library is properly organized without manual intervention.

## Features

When an album is downloaded via a MusicBrainz or Discogs match, the system automatically:

1. **Updates file metadata** - Writes proper ID3/FLAC tags including:
   - Track number
   - Artist name
   - Album artist
   - Album name
   - Year
   
2. **Renames files** - Uses a consistent naming format:
   ```
   [track_number] - [artist] - [title].[ext]
   ```
   Example: `01 - The Beatles - Come Together.mp3`

3. **Organizes into proper folder structure**:
   ```
   [album_artist]/[year] - [album]/
   ```
   Example: `The Beatles/1969 - Abbey Road/`

## How It Works

### 1. Download Queue Enhancement

When you download an album via MusicBrainz or Discogs search:

- The system stores metadata with each track in the queue:
  - `track_number` - Track position in the album
  - `album_artist` - Album artist name
  - `year` - Release year
  - `release_id` - MusicBrainz/Discogs release ID
  - `release_source` - Either 'musicbrainz' or 'discogs'

### 2. Automatic Processing

The queue processor runs continuously and:

1. Monitors the `/downloads` folder for completed files
2. Matches completed files to queue items
3. For items with MusicBrainz/Discogs metadata:
   - Updates file tags using mutagen library
   - Renames file to standard format
   - Moves file to proper folder in `/music`
4. Marks item as 'imported' in the queue

### 3. File Organization

The system creates a clean, organized library structure:

```
/music/
├── The Beatles/
│   ├── 1969 - Abbey Road/
│   │   ├── 01. The Beatles - Come Together.mp3
│   │   ├── 02. The Beatles - Something.mp3
│   │   └── ...
│   └── 1967 - Sgt. Pepper's Lonely Hearts Club Band/
│       └── ...
└── Pink Floyd/
    └── 1973 - The Dark Side of the Moon/
        └── ...
```

## Usage

### Downloading an Album

1. Go to the Downloads page
2. Search for an upcoming release or use the search feature
3. Click "Search" next to an album to find it on MusicBrainz
4. Select the correct release from the results
5. Click "Download All Tracks"
6. Wait for downloads to complete

The system will automatically:
- Search for each track on Soulseek
- Download matching files
- Update metadata
- Rename and organize files
- Add to your music library

### Monitoring Progress

You can monitor the download queue status:

1. Navigate to the Downloads page
2. View the queue status to see:
   - `queued` - Waiting to be searched
   - `searching` - Actively searching Soulseek
   - `downloading` - File is being downloaded
   - `completed` - Download complete, waiting for processing
   - `imported` - Fully processed and organized

## Technical Details

### Database Schema

The `download_queue` table includes these metadata columns:

```sql
track_number TEXT       -- Track position (e.g., "01", "02")
album_artist TEXT       -- Album artist name
year TEXT              -- Release year (e.g., "2023")
release_id TEXT        -- MusicBrainz/Discogs release ID
release_source TEXT    -- 'musicbrainz' or 'discogs'
```

### Processing Flow

```
1. User adds album via MusicBrainz/Discogs
   ↓
2. Tracks added to queue with metadata
   ↓
3. Queue processor searches & downloads files
   ↓
4. Files saved to /downloads
   ↓
5. Queue processor detects completion
   ↓
6. Post-download processor:
   - Updates ID3/FLAC tags
   - Renames file
   - Moves to /music folder
   ↓
7. Status marked as 'imported'
```

### Supported Formats

- **MP3** - Full ID3v2.4 tag support
- **FLAC** - Vorbis comment tag support

### Error Handling

The system handles various edge cases:

- **Duplicate files** - Adds `_1`, `_2`, etc. suffix
- **Special characters** - Sanitizes to filesystem-safe names
- **Missing files** - Logs error and skips
- **No metadata** - Skips post-processing for manually added items

## Configuration

### Environment Variables

```bash
DOWNLOADS_DIR=/downloads  # Where Soulseek saves files
MUSIC_ROOT=/music        # Organized library location
DB_PATH=/database/sptnr.db  # Database path
```

### Processing Interval

The queue processor runs every 30 seconds by default. This can be adjusted in `queue_processor.py`:

```python
run_processor(interval=30)  # Process every 30 seconds
```

## API Endpoints

### Add tracks with metadata

```http
POST /api/queue/add-batch
Content-Type: application/json

{
  "items": [
    {
      "artist": "The Beatles",
      "title": "Come Together",
      "album": "Abbey Road",
      "track_number": "01",
      "album_artist": "The Beatles",
      "year": "1969",
      "release_id": "mb-12345",
      "release_source": "musicbrainz"
    }
  ],
  "import_group": "The_Beatles_Abbey_Road",
  "import_type": "album"
}
```

## Logs

Monitor post-download processing in the logs:

```bash
# View post-download processor logs
tail -f /config/post_download.log

# View queue processor logs
tail -f /config/queue_processor.log
```

Example log output:

```
2026-02-19 08:56:12 - [Post-Download] INFO - Queue 1: Processing with metadata from musicbrainz
2026-02-19 08:56:12 - [Post-Download] INFO - Updated MP3 metadata: /downloads/track.mp3
2026-02-19 08:56:12 - [Post-Download] INFO - Moved: /downloads/track.mp3 -> /music/The Beatles/1969 - Abbey Road/01. The Beatles - Come Together.mp3
2026-02-19 08:56:12 - [Post-Download] INFO - Queue 1: Successfully processed
2026-02-19 08:56:12 - [Post-Download] INFO - Queue 1: Marked as imported
```

## Troubleshooting

### Files not being organized

1. Check that files have completed downloading
2. Verify the queue shows `completed` status
3. Check logs for errors: `tail -f /config/post_download.log`
4. Ensure MusicBrainz/Discogs metadata was stored (check `release_source` column)

### Wrong metadata applied

1. Select a different release from the MusicBrainz/Discogs search results
2. Re-download the album
3. The new metadata will be used for future downloads

### Files organizing but metadata not updated

1. Ensure `mutagen` Python library is installed
2. Check file format is supported (MP3 or FLAC)
3. Check post-download logs for mutagen errors

## Development

### Testing

Run the test suite:

```bash
python test_post_download_processor.py -v
```

Tests cover:
- Filename sanitization
- File renaming and moving
- Metadata processing
- Error handling
- Batch processing

### Extending Support

To add support for additional audio formats:

1. Add format handler to `update_file_metadata()` in `post_download_processor.py`
2. Import appropriate mutagen class
3. Map metadata fields to format-specific tags
4. Add tests for the new format

Example for OGG Vorbis:

```python
elif ext == '.ogg':
    from mutagen.oggvorbis import OggVorbis
    audio = OggVorbis(file_path)
    if metadata.get('title'):
        audio['title'] = [metadata['title']]
    # ... map other fields
    audio.save()
    return True
```

## Related Files

- `post_download_processor.py` - Main processing logic
- `queue_processor.py` - Queue management and integration
- `download_queue_manager.py` - Queue database operations
- `app.py` - API endpoints for queue management
- `templates/downloads.html` - Frontend UI
- `test_post_download_processor.py` - Test suite

## Future Enhancements

Potential improvements for future versions:

1. **Cover art embedding** - Download and embed album art
2. **Additional formats** - Support for AAC, ALAC, APE, etc.
3. **Custom naming templates** - User-configurable file/folder naming
4. **Metadata validation** - Cross-check with multiple sources
5. **Duplicate detection** - Skip if track already exists in library
6. **Batch re-organization** - Re-organize existing library with new metadata
