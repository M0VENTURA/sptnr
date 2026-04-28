# Summary: MusicBrainz Rate Limiting & Beets Configuration Improvements

## Overview

This PR addresses two main issues:
1. **MusicBrainz IP blocking** due to too aggressive lookups
2. **Beets configuration consolidation** into config.yaml with separate profiles for different use cases

## Problem Statement

From the original issue:
> It looks like my IP has been blocked by musicbrainz. Are my lookups too aggressive. Can this be improved?
> Can the beets config be moved into the config.yaml. It should have different config options for importing from downloads and scanning the music collection, renaming, metadata, etc

## Changes Made

### 1. MusicBrainz Rate Limiting

**Problem**: 
- MusicBrainz enforces strict 1 request/second rate limit
- Code had hardcoded `time.sleep(1.0)` delays but no centralized tracking
- Multiple concurrent operations could exceed the rate limit
- IP blocking occurs when limits are exceeded

**Solution**:
```python
# Added to api_rate_limiter.py
MUSICBRAINZ_RATE_LIMIT_PER_SECOND = 1
MUSICBRAINZ_MIN_INTERVAL = 1.0

def check_musicbrainz_limit() -> tuple[bool, str]
def record_musicbrainz_request()
def wait_if_needed_musicbrainz(max_wait_seconds: float = 2.0) -> bool
```

**Integration**:
- All MusicBrainz API calls now use the rate limiter:
  - `is_single()` - Single detection
  - `get_genres()` - Genre/tag lookups  
  - `get_suggested_mbid()` - MBID suggestions
  - Nested release lookups

**Benefits**:
- Prevents IP blocking by enforcing strict rate limits
- Centralizes rate limiting across all MusicBrainz operations
- Tracks daily MusicBrainz usage statistics
- Automatic delays without manual sleep() calls

### 2. Beets Configuration Consolidation

**Problem**:
- Separate `beetsconfig.yaml` file
- Single configuration for all use cases
- No distinction between importing new music vs scanning existing collection

**Solution**:
Added comprehensive `beets:` section to `config/config.yaml`:

```yaml
beets:
  enabled: true
  
  # For importing new downloads
  import_downloads:
    copy: false
    write: true       # Write metadata to files
    autotag: true     # Auto-tag using MusicBrainz
    resume: true
    incremental: true
    quiet_fallback: "skip"
    timid: false
    detail: true
    log: "/config/beets_import.log"
    
  # For scanning existing collection
  scan_collection:
    copy: false
    write: false      # Read-only, don't modify files
    autotag: false    # Don't auto-tag
    resume: false
    incremental: true
    log: "/config/beets_scan.log"
    
  # File organization
  paths:
    default: "$albumartist/$album%aunique{}/$track - $title"
    singleton: "Non-Album/$artist - $title"
    comp: "Compilations/$album%aunique{}/$track - $title"
    
  # Metadata matching
  match:
    strong_rec_thresh: 0.04
    medium_rec_thresh: 0.25
    
  # MusicBrainz integration
  musicbrainz:
    enabled: true
    rate_limit: 1.0
    
  # Plugins
  plugins:
    - duplicates
    - info
    - missing
    
  # Directories
  directory: "/music"
  library: "/config/beets/musiclibrary.db"
```

**Implementation**:
- `BeetsClient` loads config from `config.yaml`
- Generates mode-specific `beetsconfig.yaml` on demand
- `import_music()` accepts `mode` parameter to select profile

**Usage**:
```python
from beets_integration import BeetsClient

client = BeetsClient(config_path="/config")

# Import new downloads with auto-tagging
client.import_music("/downloads/Music", mode="import_downloads")

# Scan existing collection (read-only)
client.import_music("/music", mode="scan_collection")
```

## Files Modified

### Core Changes
- `api_rate_limiter.py`: Added MusicBrainz rate limiting
- `api_clients/musicbrainz.py`: Integrated with rate limiter
- `config/config.yaml`: Added comprehensive beets configuration
- `beets_integration.py`: Load from config.yaml, generate mode-specific configs

### Documentation
- `API_RATE_LIMITS.md`: Added MusicBrainz section and updated examples
- `BEETS_CONFIGURATION.md`: New comprehensive guide for beets configuration

## Testing Results

✅ **All tests passed**:
- Python syntax validation (py_compile)
- YAML syntax validation
- Rate limiter functionality test
- Config generation logic test
- Code review (1 minor issue fixed)
- Security scan (0 alerts)

### Rate Limiter Test
```
✓ First MusicBrainz request allowed
✓ Recorded MusicBrainz request
✓ Second immediate request blocked: MusicBrainz rate limit: must wait 1.0s between requests
✓ MusicBrainz stats present: 1 requests
```

### Config Generation Test
```
✓ import_downloads mode has autotag=True
✓ import_downloads mode has write=True
✓ scan_collection mode has autotag=False
✓ scan_collection mode has write=False
✓ All required sections present
```

## Migration Guide

### MusicBrainz Rate Limiting
**No action required** - automatically enabled:
1. Rate limiter loads existing state from `/database/api_rate_limiter_state.json`
2. Adds `musicbrainz_daily_count` and `musicbrainz_last_request` tracking
3. All MusicBrainz calls automatically enforced at 1 req/sec

If your IP was already blocked:
- Wait 24 hours before making more requests
- Update to this version to prevent future blocking
- The rate limiter will respect limits going forward

### Beets Configuration
**Three options**:

**Option 1: Keep existing beetsconfig.yaml**
- No changes needed, old config still works

**Option 2: Migrate to new format**
```bash
# 1. Backup old config
cp /config/beetsconfig.yaml /config/beetsconfig.yaml.bak

# 2. Add settings to config.yaml beets section
# 3. BeetsClient will auto-generate new config
```

**Option 3: Use both**
- Keep old config for compatibility
- New settings in `config.yaml` take precedence

## Impact Assessment

### Performance
- **MusicBrainz**: Slight delay added (enforced 1 sec between requests)
  - Before: Attempted 1 sec delay, but no enforcement
  - After: Guaranteed 1 sec delay, prevents IP blocking
  - Trade-off: Slightly slower but prevents blocking

### Reliability  
- **Before**: Risk of IP blocking if multiple operations ran concurrently
- **After**: Centralized rate limiting prevents blocking across all operations

### Usability
- **Before**: Separate beets config file, single profile
- **After**: Integrated config, multiple profiles for different use cases

## Breaking Changes

**None** - fully backward compatible:
- Existing `beetsconfig.yaml` files work as-is
- MusicBrainz rate limiting is transparent (just adds delays)
- No API signature changes
- No database schema changes

## Security Review

- ✅ CodeQL security scan: 0 alerts
- ✅ No hardcoded credentials
- ✅ No SQL injection vulnerabilities
- ✅ Proper input validation

## Future Enhancements

Potential improvements:
1. Web UI for rate limiter statistics monitoring
2. Additional beets modes (e.g., "organize_only", "update_metadata")
3. Rate limiter alerts when approaching limits
4. Beets plugin configuration in config.yaml
5. Per-operation rate limiting statistics

## Documentation

Comprehensive documentation added:

**BEETS_CONFIGURATION.md** covers:
- Configuration modes explanation
- File organization options
- Metadata matching settings
- Migration from old config
- Troubleshooting guide
- Best practices

**API_RATE_LIMITS.md** updated with:
- MusicBrainz rate limits
- Integration details
- Troubleshooting IP blocking
- Usage statistics examples

## Support

For questions or issues:
- See `BEETS_CONFIGURATION.md` for beets setup
- See `API_RATE_LIMITS.md` for rate limiting details
- Check logs: `/config/beets_import.log` or `/config/beets_scan.log`
- Review rate limiter state: `/database/api_rate_limiter_state.json`
- Open GitHub issue for bugs or feature requests
