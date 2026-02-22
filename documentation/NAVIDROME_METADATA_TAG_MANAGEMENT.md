# Navidrome Metadata Tag Management - Implementation Guide

## Overview

A comprehensive metadata tag management system that imports all Navidrome metadata fields into the database and provides UI components for viewing, editing, and updating tags at both track and album levels.

## Imported Navidrome Fields

### Basic Metadata (5 fields)
- `title` - Song title
- `artist` - Track artist  
- `album` - Album name
- `album_artist` / `albumartist` - Album artist (two variants)
- `albumartistsort` - Album artist sort name

### Credits (6 fields)
- `arranger` - Track arranger
- `composer` - Composer/songwriter
- `mixer` - Audio engineer/mixer
- `performer` - JSON array of performer credits
- `producer` - JSON array of producer names
- `writer` - JSON array of songwriter/writer names

### Release Information (13 fields)
- `label` - Record label
- `releasecountry` - Release country code  
- `releasestatus` - official/bootleg/promotion/pseudo-release
- `releasetype` - album/single/ep/compilation/soundtrack/live/remix
- `media` - CD/Vinyl/Digital/Cassette
- `barcode` - UPC/EAN barcode
- `catalognumber` - Release catalog number
- `asin` - Amazon Standard Identification Number
- `originaldate` - Original release date (YYYY-MM-DD format)
- `originalyear` - Original release year
- `totaldiscs` - Total discs in release
- `year` - Release year
- `date` - Release date

### Artist Information (4 fields)
- `artists` - JSON array of artist names
- `artistsort` - Artist sort name
- `genre` - Genre
- `work` - Musical work/composition name

### MusicBrainz IDs (7 fields)
- `mbid` - Track/recording MBID
- `musicbrainz_albumid` - Album/release MBID
- `musicbrainz_albumartistid` - Album artist MBID
- `musicbrainz_albumstatus` - Release status from MB
- `musicbrainz_albumtype` - Release type from MB
- `musicbrainz_releasegroupid` - Release group MBID
- `musicbrainz_releasetrackid` - Release track MBID
- `musicbrainz_workid` - Work MBID

### Technical Fields (3 fields)
- `bpm` - Beats per minute
- `isrc` - International Standard Recording Code
- `script` - Script code (e.g., Latn, Cyrl)

## Database Schema Updates

All fields are stored as TEXT or INTEGER in the `tracks` table. Array fields (artists, performer, producer, writer) are stored as JSON arrays.

### Schema Changes
```python
# Added to check_db.py required_columns
"albumartist": "TEXT",                  # Navidrome albumartist field
"albumartistsort": "TEXT",              # Album artist sort name
"arranger": "TEXT",                     # Track arranger
"artists": "TEXT",                      # JSON array of artist names
"artistsort": "TEXT",                   # Artist sort name
"asin": "TEXT",                         # Amazon Standard Identification Number
"barcode": "TEXT",                      # Album barcode (UPC/EAN)
"catalognumber": "TEXT",                # Catalog number
"label": "TEXT",                        # Record label
"media": "TEXT",                        # Release media type
"mixer": "TEXT",                        # Audio engineer/mixer
"performer": "TEXT",                    # JSON array of performer credits
"producer": "TEXT",                     # JSON array of producer names
"releasecountry": "TEXT",               # Release country code
"releasestatus": "TEXT",                # Release status
"releasetype": "TEXT",                  # Release type
"script": "TEXT",                       # Script code
"work": "TEXT",                         # Musical work name
"writer": "TEXT",                       # JSON array of songwriter/writer names
"musicbrainz_albumartistid": "TEXT",    # MusicBrainz album artist ID
"musicbrainz_albumid": "TEXT",          # MusicBrainz album/release ID
"musicbrainz_albumstatus": "TEXT",      # Release status from MusicBrainz
"musicbrainz_albumtype": "TEXT",        # Release type from MusicBrainz
"musicbrainz_releasegroupid": "TEXT",   # MusicBrainz release group ID
"musicbrainz_releasetrackid": "TEXT",   # MusicBrainz release track ID
"musicbrainz_workid": "TEXT",           # MusicBrainz work ID
"originaldate": "TEXT",                 # Original release date (YYYY-MM-DD)
"originalyear": "INTEGER",              # Original release year
"totaldiscs": "INTEGER",                # Total number of discs
"tracktotal": "INTEGER",                # Total number of tracks on album
```

## Module: tag_manager.py

Core module for tag operations with the following functions:

### Key Functions

#### `get_track_tags(track_id: str) -> Dict[str, Any]`
Retrieves all editable tags for a track from the database.

```python
from tag_manager import get_track_tags

tags = get_track_tags("track_id_123")
# Returns: {
#   "title": "Song Title",
#   "artist": "Artist Name",
#   "album": "Album Name",
#   ...other fields...
# }
```

#### `get_album_tags(album: str, artist: str) -> Dict[str, Any]`
Retrieves album-level tags that are typically the same across all tracks.

```python
from tag_manager import get_album_tags

tags = get_album_tags("Album Name", "Artist Name")
```

#### `check_field_conflicts(album: str, artist: str) -> Dict[str, List[str]]`
Detects conflicting metadata values within an album, particularly useful for identifying mismatches between `album_artist` and `albumartist` fields.

```python
from tag_manager import check_field_conflicts

conflicts = check_field_conflicts("Album Name", "Artist Name")
# Returns conflicts like:
# {
#   "album_artist_vs_albumartist": {
#     "album_artist": ["value1"],
#     "albumartist": ["value2"]
#   },
#   "label": ["Label A", "Label B"]
# }
```

#### `update_track_tags(track_id: str, tag_updates: Dict[str, Any]) -> bool`
Updates metadata for a single track in the database.

```python
from tag_manager import update_track_tags

success = update_track_tags("track_id_123", {
    "title": "New Title",
    "year": 2024,
    "genre": "Rock"
})
```

#### `update_album_tags(album: str, artist: str, tag_updates: Dict[str, Any], selected_tracks: Optional[List[str]] = None) -> int`
Updates metadata for all tracks in an album (or specific tracks).

```python
from tag_manager import update_album_tags

updated_count = update_album_tags(
    "Album Name",
    "Artist Name",
    {"label": "New Label", "releaseyear": 2024},
    selected_tracks=["track_1", "track_2"]  # None for all tracks
)
```

#### `sync_track_tags_to_file(track_id: str) -> bool`
Writes tags from database back to the audio file (MP3/FLAC).

```python
from tag_manager import sync_track_tags_to_file

success = sync_track_tags_to_file("track_id_123")
```

## API Endpoints

### Track Tags

#### GET `/api/tags/track/<track_id>`
Retrieve all editable tags for a specific track.

**Response:**
```json
{
  "success": true,
  "track_id": "track_123",
  "tags": {
    "title": "Song Title",
    "artist": "Artist Name",
    "album": "Album Name",
    ...other fields...
  }
}
```

#### POST `/api/tags/track/<track_id>`
Update tags for a specific track.

**Request Body:**
```json
{
  "tags": {
    "title": "New Title",
    "year": 2024,
    "genre": "Rock"
  },
  "sync_to_file": false
}
```

**Response:**
```json
{
  "success": true,
  "track_id": "track_123",
  "updated_fields": 3,
  "file_synced": false
}
```

### Album Tags

#### GET `/api/tags/album/<album>/<artist>`
Retrieve album-level tags and detect any conflicts.

**Response:**
```json
{
  "success": true,
  "album": "Album Name",
  "artist": "Artist Name",
  "tags": {
    "label": "Record Label",
    "releasecountry": "US",
    "totaldiscs": 1,
    ...album-level fields...
  },
  "conflicts": {
    "album_artist_vs_albumartist": {
      "album_artist": ["Angelfish"],
      "albumartist": ["1994"]
    }
  }
}
```

#### POST `/api/tags/album/<album>/<artist>`
Update tags for all tracks in an album.

**Request Body:**
```json
{
  "tags": {
    "label": "New Label",
    "releasecountry": "GB"
  },
  "track_ids": null,
  "sync_to_files": false
}
```

**Response:**
```json
{
  "success": true,
  "album": "Album Name",
  "artist": "Artist Name",
  "updated_count": 10,
  "synced_count": 0,
  "message": "Updated 10 track(s), synced 0 file(s)"
}
```

#### GET `/api/tags/album/<album>/<artist>/conflicts`
Check for metadata conflicts in an album.

**Response:**
```json
{
  "success": true,
  "album": "Album Name",
  "artist": "Artist Name",
  "has_conflicts": true,
  "conflicts": {
    "album_artist_vs_albumartist": {
      "album_artist": ["Correct Name"],
      "albumartist": ["Incorrect Name"]
    },
    "label": ["Label A", "Label B"]
  }
}
```

### File Sync

#### POST `/api/tags/sync/<track_id>`
Sync database tags back to the audio file.

**Response:**
```json
{
  "success": true,
  "track_id": "track_123",
  "message": "Tags synced to file"
}
```

## UI Component: Tag Editor

The tag editor is a Bootstrap modal dialog with tabbed interface for organizing metadata fields.

### File Location
`templates/tag_editor.html`

### Tabs
1. **Basic Info** - Title, artist, album, album artist variants
2. **Credits** - Composer, arranger, producer, mixer, writer, performer
3. **Release Info** - Dates, status, type, label, country, media, UPC, catalog#
4. **MusicBrainz** - All MBID fields
5. **Technical** - ISRC, BPM, script

### Features
- **Conflict Highlighting**: Fields that conflict across album tracks are highlighted in red
- **Array Field Support**: Comma-separated input for multi-value fields (artists, producers, writers, performers)
- **File Sync Option**: Checkbox to write changes back to audio files
- **Tab Navigation**: Organize 40+ fields into logical sections

### JavaScript API

#### Open Track Editor
```javascript
editTrackTags('track_id_123')
```

#### Open Album Editor
```javascript
editAlbumTags('Album Name', 'Artist Name')
```

### Integration in Templates

To include the tag editor in your template:

```html
<!-- Include the tag editor modal -->
{% include 'tag_editor.html' %}

<!-- Add button to open editor -->
<button class="btn btn-sm btn-outline-primary" onclick="editTrackTags('{{ track.id }}')">
  <i class="bi bi-pencil"></i> Edit Tags
</button>

<!-- For album view -->
<button class="btn btn-sm btn-outline-primary" onclick="editAlbumTags('{{ album }}', '{{ artist }}')">
  <i class="bi bi-pencil"></i> Edit Album Tags
</button>
```

## Conflict Detection

The system automatically detects and highlights metadata conflicts, particularly:

### album_artist vs albumartist
These two fields can have different values and indicate metadata issues:
- `album_artist`: The canonical album artist used in the system
- `albumartist`: Raw metadata field from file/Navidrome

When they differ, both fields are highlighted in red with an explanatory alert.

### Other Conflicting Fields
The system also checks for conflicts in:
- Album-level fields (label, releasecountry, releasetype)
- Multi-value fields across tracks

## Workflow Example

### 1. Import Tags from Navidrome
```bash
# During Navidrome scan, all tags are automatically imported and stored
python navidrome_import.py
```

### 2. View Tags via API
```bash
curl http://localhost:8000/api/tags/track/track_id_123
```

### 3. Edit Tags via UI
```javascript
// Open the tag editor modal
editTrackTags('track_id_123')

// Select new values/edit fields
// Click "Save Tags" button
```

### 4. Check Album Conflicts
```bash
curl "http://localhost:8000/api/tags/album/Album%20Name/Artist%20Name/conflicts"
```

### 5. Bulk Update Album
```bash
curl -X POST http://localhost:8000/api/tags/album/Album%20Name/Artist%20Name \
  -H "Content-Type: application/json" \
  -d '{
    "tags": {"label": "New Label"},
    "sync_to_files": true
  }'
```

## Array Fields

Fields that store multiple values (artists, producer, writer, performer) can be managed as comma-separated lists in the UI:

**Input:** `Producer One, Producer Two, Producer Three`
**Stored:** `["Producer One", "Producer Two", "Producer Three"]`

When retrieved, they're automatically formatted for display and editing.

## File Sync

When `sync_to_file` is enabled, changes are written back to the audio file:
- **MP3 files**: ID3 tags are updated
- **FLAC files**: Vorbis comments are updated
- Other formats are skipped with a warning

## Editable Fields

The following fields can be edited:
```python
EDITABLE_FIELDS = {
    # Basic
    "album", "artist", "title", "album_artist", "albumartist", 
    "albumartistsort", "artistsort",
    # Credits
    "arranger", "composer", "mixer", "producer", "writer", "performer",
    # Release
    "label", "releasecountry", "releasestatus", "releasetype",
    "media", "barcode", "catalognumber", "asin",
    # Dates
    "year", "originalyear", "originaldate", "date",
    # Numbering
    "track_number", "tracktotal", "disc_number", "totaldiscs",
    # Content
    "genre", "work",
    # Technical
    "bpm", "isrc", "script",
    # MusicBrainz
    "musicbrainz_albumartistid", "musicbrainz_albumid", "musicbrainz_albumtype",
    "musicbrainz_albumstatus", "musicbrainz_releasegroupid",
    "musicbrainz_releasetrackid", "musicbrainz_workid", "mbid",
}
```

## Album-Level Fields

These fields are considered album-level and can be edited for the entire album at once:
```python
ALBUM_LEVEL_FIELDS = {
    "album", "label", "releasecountry", "releasestatus", "releasetype",
    "media", "barcode", "catalognumber", "asin", "year", "originalyear",
    "originaldate", "totaldiscs", "musicbrainz_albumid", "musicbrainz_albumtype",
    "musicbrainz_albumstatus", "musicbrainz_releasegroupid",
}
```

## Next Steps

To fully integrate this system:

1. **Update track.html template** - Add "Edit Tags" button in track details view
2. **Update album.html template** - Add "Edit Album Tags" button with conflict indicators
3. **Add styling** - Customize conflict highlighting colors
4. **Test end-to-end** - Import → Edit → Sync → Verify in files
5. **Add validation** - Field-specific validation (ISRC format, MBID format, etc.)
