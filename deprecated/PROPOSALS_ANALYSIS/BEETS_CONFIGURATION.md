# Beets Configuration Guide

## Overview

Beets is now configured through the main `config.yaml` file instead of a separate `beetsconfig.yaml`. This allows for better integration and management of different use cases.

## Configuration Location

All beets settings are now in `/config/config.yaml` under the `beets:` section.

## Configuration Modes

The beets configuration supports two distinct modes:

### 1. Import Downloads Mode

Used when importing new music from the downloads folder. This mode:
- **Auto-tags** files using MusicBrainz
- **Writes** metadata to files
- **Does not copy** files (they're already in place)
- **Logs** to `/config/beets_import.log`

```yaml
beets:
  import_downloads:
    copy: false
    write: true
    autotag: true
    resume: true
    incremental: true
    quiet_fallback: "skip"
    timid: false
    detail: true
    log: "/config/beets_import.log"
```

### 2. Scan Collection Mode

Used when scanning the existing music collection. This mode:
- **Does not auto-tag** (read-only scan)
- **Does not write** metadata to files
- **Does not copy** files
- **Logs** to `/config/beets_scan.log`

```yaml
beets:
  scan_collection:
    copy: false
    write: false
    autotag: false
    resume: false
    incremental: true
    log: "/config/beets_scan.log"
```

## File Organization

Configure how beets organizes your music files:

```yaml
beets:
  paths:
    default: "$albumartist/$album%aunique{}/$track - $title"
    singleton: "Non-Album/$artist - $title"
    comp: "Compilations/$album%aunique{}/$track - $title"
```

This creates a structure like:
```
/music/
  ├── Artist Name/
  │   └── Album Name/
  │       ├── 01 - Track Title.mp3
  │       └── 02 - Another Track.mp3
  ├── Non-Album/
  │   └── Artist - Single Track.mp3
  └── Compilations/
      └── Various Artists Album/
          └── 01 - Track.mp3
```

## Metadata Matching

Control how beets matches tracks to MusicBrainz:

```yaml
beets:
  match:
    strong_rec_thresh: 0.04  # Auto-accept strong matches
    medium_rec_thresh: 0.25  # May prompt for medium matches
```

Lower thresholds = stricter matching (fewer false positives)

## MusicBrainz Integration

Configure MusicBrainz lookups for beets:

```yaml
beets:
  musicbrainz:
    enabled: true
    rate_limit: 1.0  # Enforce 1 request per second
```

**Important**: Beets' MusicBrainz integration now respects the global rate limiter to prevent IP blocking.

## Plugins

Enable beets plugins for additional functionality:

```yaml
beets:
  plugins:
    - duplicates  # Find duplicate tracks
    - info        # Display track information
    - missing     # Find missing tracks in albums
```

## Using the Configuration

### Automatic Mode Selection

The `BeetsClient` class automatically selects the appropriate configuration mode:

```python
from beets_integration import BeetsClient

client = BeetsClient(config_path="/config")

# Import new downloads (uses import_downloads mode)
client.import_music("/downloads/Music/New Album", mode="import_downloads")

# Scan existing collection (uses scan_collection mode)
client.import_music("/music", mode="scan_collection")
```

### Manual Configuration Generation

You can manually generate the beets config for a specific mode:

```python
client._generate_beets_config(mode="import_downloads")
# or
client._generate_beets_config(mode="scan_collection")
```

This creates `/config/beetsconfig.yaml` with the appropriate settings.

## Migration from Old Configuration

If you have an existing `beetsconfig.yaml`, you can:

1. **Keep using it**: The old config file still works if it exists
2. **Migrate to new format**: Add your settings to the `beets:` section in `config.yaml`
3. **Use both**: Settings in `config.yaml` take precedence when regenerating the config

### Migration Example

Old `beetsconfig.yaml`:
```yaml
directory: /music
library: /config/beets/musiclibrary.db
import:
  copy: false
  write: true
  autotag: yes
```

New `config.yaml`:
```yaml
beets:
  enabled: true
  directory: /music
  library: /config/beets/musiclibrary.db
  import_downloads:
    copy: false
    write: true
    autotag: true
```

## Troubleshooting

### Issue: Beets not finding configuration

**Solution**: Ensure `config.yaml` has a `beets:` section and `beets.enabled: true`

### Issue: Wrong configuration mode being used

**Solution**: Pass the `mode` parameter explicitly:
```python
client.import_music(path, mode="scan_collection")
```

### Issue: MusicBrainz rate limiting errors

**Solution**: The integrated rate limiter automatically enforces delays. If you still see errors:
- Check the rate limiter state: `/database/api_rate_limiter_state.json`
- Ensure only one beets process is running at a time
- Wait 1 second between manual beets commands

## Advanced Configuration

### Custom Paths per Music Type

```yaml
beets:
  paths:
    default: "$albumartist/$album%aunique{}/$track - $title"
    comp: "Compilations/$album%aunique{}/$track - $title"
    soundtrack: "Soundtracks/$album/$track - $title"
```

### Import with Specific Options

```yaml
beets:
  import_downloads:
    copy: false
    write: true
    autotag: true
    quiet_fallback: "skip"  # Skip albums that can't be tagged
    timid: false            # Auto-accept good matches
    detail: true            # Show detailed info
```

## Best Practices

1. **Import Downloads**: Use `import_downloads` mode for new music
   - Automatically tags and organizes files
   - Writes metadata to file tags
   - Skips poor matches to avoid incorrect tagging

2. **Scan Collection**: Use `scan_collection` mode for existing library
   - Read-only operation
   - Won't modify your existing tags
   - Useful for cataloging what you have

3. **Rate Limiting**: Let the system handle MusicBrainz rate limiting
   - Don't run multiple beets processes simultaneously
   - The rate limiter ensures 1 request/second
   - Prevents IP blocking

4. **Incremental Imports**: Keep `incremental: true` to skip already-imported items
   - Faster subsequent imports
   - Won't re-process the same music

## Support

For issues or questions:
- Check the logs: `/config/beets_import.log` or `/config/beets_scan.log`
- Enable verbose mode in the import command
- Verify `config.yaml` has the correct beets configuration
