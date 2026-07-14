# MusicBrainz Tags Implementation

## Overview
This document explains how to properly tag MP3 files with MusicBrainz metadata, specifically release country, status, and type using ID3v2 TXXX (user-defined text) frames instead of genre tags.

## Proper MP3 ID3 Tags for MusicBrainz Data

According to [Navidrome's tag mappings](https://github.com/navidrome/navidrome/blob/master/resources/mappings.yaml), the following TXXX frames should be used:

### Release Country
- **Frame**: `TXXX:MUSICBRAINZ ALBUM RELEASE COUNTRY`
- **Description**: 2-letter country code (e.g., `US`, `GB`, `DE`) or full country name
- **Source**: MusicBrainz release area
- **Readable by**: Navidrome, Picard, mp3tag, et al.

### Release Status
- **Frame**: `TXXX:MUSICBRAINZ ALBUM STATUS` 
- **Description**: Release status classification
- **Values**: `Official`, `Promotion`, `Bootleg`, `Pseudo-release`, `Withdrawn`
- **Source**: MusicBrainz release status field
- **Readable by**: Navidrome, Picard

### Release Type
- **Frame**: `TXXX:MUSICBRAINZ ALBUM TYPE`
- **Description**: Release type classification
- **Values**: `Album`, `EP`, `Single`, `Compilation`, `Soundtrack`, `Live`, `Remix`, `Other`
- **Source**: MusicBrainz release type field
- **Readable by**: Navidrome, Picard

## Implementation in sptnr

### New Helper Functions in metadata_reader.py

#### Writing Tags
```python
from metadata_reader import write_musicbrainz_tags_to_mp3

# Write release information to MP3 file
write_musicbrainz_tags_to_mp3(
    file_path='/path/to/track.mp3',
    release_country='US',
    release_status='Official',
    release_type='Album'
)
```

#### Reading Tags
```python
from metadata_reader import read_musicbrainz_tags_from_mp3

# Read MusicBrainz TXXX tags from MP3 file
tags = read_musicbrainz_tags_from_mp3(file_path='/path/to/track.mp3')
# Returns:
# {
#   'release_country': 'US',
#   'release_status': 'Official',
#   'release_type': 'Album'
# }
```

## Integration with Popularity Scan

When the popularity scanner fetches MusicBrainz metadata during a scan:

1. **Get Release Information**: During MusicBrainz lookup, retrieve:
   - Release country (area)
   - Release status (official/promotion/bootleg)
   - Release type (album/ep/single)

2. **Write to MP3 Files**: Use the helper functions to write these as proper TXXX frames:
   ```python
   # Instead of adding country to genre tag:
   # ✗ BAD: audio.tags['TCON'] = TCON(encoding=3, text=['US'])
   
   # ✓ GOOD: Use proper ID3 TXXX frames
   write_musicbrainz_tags_to_mp3(
       file_path=track_path,
       release_country=mbz_area,
       release_status=mbz_status,
       release_type=mbz_type
   )
   ```

3. **Display in UI**: Release info appears as proper metadata fields in Navidrome and other ID3-aware clients

## Why Proper Tags Matter

- **Standardization**: Follows MusicBrainz Picard conventions
- **Tool Compatibility**: Works correctly with Navidrome, Tag Editors, Media Players
- **Metadata Clarity**: Separates release metadata from genre classifications
- **API Consistency**: Navidrome's tag mappings recognize these TXXX frames automatically
- **Future-proof**: Standards-compliant, won't break in updates

## Navidrome Tag Mapping Reference

Navidrome automatically maps these TXXX frames to searchable fields in smart playlists:
- `TXXX:MUSICBRAINZ ALBUM RELEASE COUNTRY` → `releasecountry` field
- `TXXX:MUSICBRAINZ ALBUM STATUS` → `releasestatus` field
- `TXXX:MUSICBRAINZ ALBUM TYPE` → `releasetype` field

This allows filtering and searching by release country, status, or type in Navidrome smart playlists.

## File References

- **Helper Functions**: [metadata_reader.py](metadata_reader.py#L25-L120)
- **MP3 Tag Constants**: [metadata_reader.py](metadata_reader.py#L19-L22)
- **Navidrome Mappings**: https://github.com/navidrome/navidrome/blob/master/resources/mappings.yaml
- **ID3v2 Specification**: https://id3.org/ID3v2.3.0
