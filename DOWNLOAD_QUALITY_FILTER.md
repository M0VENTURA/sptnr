# Download Quality Filter - Enhanced Matching with Format/Bitrate Priority

## Overview

The Download Quality Filter is a new feature that improves download accuracy by automatically rejecting files that don't meet your specified quality requirements. This prevents false matches from being moved into your music library.

**Problem Solved:**
- Downloads with wrong bitrate (e.g., 128 kbps instead of 320 kbps)
- Downloads in undesired formats (e.g., accepting M4A when you want 320 MP3 only)
- Better matching accuracy by filtering low-quality files early

## Configuration

### Basic Setup (Recommended)

Add this to your `/config/config.yaml`:

```yaml
downloads:
  folder: "/downloads/Music"
  quality_filter:
    enabled: true
    reject_others: true
    bitrate_tolerance: 5
    priorities:
      - format: "mp3"
        bitrate_kbps: 320      # Priority 1: 320 kbps MP3
      - format: "flac"
        bitrate_kbps: null     # Priority 2: FLAC (lossless)
```

### How It Works

1. **Enabled**: Set to `false` to disable filtering and accept all files
2. **Reject Others**: 
   - `true` = Only accept files matching the priority list; reject everything else
   - `false` = Accept files matching priority first, but also accept non-matching files
3. **Bitrate Tolerance**: Allow variance in bitrate (e.g., ±5 kbps)
   - Default: 5 kbps
   - Set to 10 for more lenient matching
4. **Priorities**: List of format+bitrate combinations in order of preference
   - First matching rule is used (priority order matters!)

## Configuration Examples

### Example 1: 320 MP3 Only (Most Strict)

```yaml
quality_filter:
  enabled: true
  reject_others: true
  bitrate_tolerance: 5
  priorities:
    - format: "mp3"
      bitrate_kbps: 320
```

- ✅ Accepts: 320 kbps MP3, 315-325 kbps MP3
- ❌ Rejects: 256 kbps MP3, FLAC, M4A, 320 OGG, etc.

### Example 2: 320 MP3 or FLAC (Recommended Balance)

```yaml
quality_filter:
  enabled: true
  reject_others: true
  bitrate_tolerance: 5
  priorities:
    - format: "mp3"
      bitrate_kbps: 320      # High-quality lossy
    - format: "flac"
        bitrate_kbps: null   # Lossless (no bitrate check)
```

- ✅ Accepts: 320 kbps MP3, FLAC
- ❌ Rejects: 256 kbps MP3, M4A, OGG, 128 kbps MP3, etc.

### Example 3: 320 MP3, FLAC, or 256 M4A (Flexible)

```yaml
quality_filter:
  enabled: true
  reject_others: true
  bitrate_tolerance: 5
  priorities:
    - format: "mp3"
      bitrate_kbps: 320
    - format: "flac"
      bitrate_kbps: null
    - format: "m4a"
      bitrate_kbps: 256
```

- ✅ Accepts: 320 MP3, FLAC, 256 M4A, 192 OGG
- ❌ Rejects: 128 MP3, 128 M4A, etc.

### Example 4: Accept All Formats (Disabled Filter)

```yaml
quality_filter:
  enabled: false
```

or

```yaml
quality_filter:
  enabled: true
  reject_others: false
  priorities:
    - format: "mp3"
      bitrate_kbps: 320
    - format: "flac"
      bitrate_kbps: null
```

- Prefers 320 MP3 and FLAC, but accepts anything else

## How Matching Works

### Matching Process

1. **Format Check First**: File extension must match one of the priorities
   - Examples: `.mp3` → "mp3", `.flac` → "flac"
   
2. **Bitrate Validation** (if specified in priority):
   - Extract bitrate from file metadata (ID3 tags, FLAC headers, etc.)
   - Compare against priority with ±tolerance applied
   - Example: Priority is 320 kbps with tolerance 5
     - ✅ Accepts: 315-325 kbps
     - ❌ Rejects: 310 kbps, 330 kbps
   
3. **Lossless Formats**: When `bitrate_kbps` is `null`:
   - FLAC, WAV, etc. don't need bitrate validation
   - Just format match is sufficient

4. **Metadata Fallback**: 
   - If file bitrate can't be read from metadata, file is accepted anyway
   - Prevents rejecting legitimate files with missing metadata

### Artist/Title Matching Still Applies

Quality filter works **in addition to** existing artist/title matching:

```
File Found → Extract Format & Bitrate
    ↓
[QUALITY FILTER] Does format/bitrate match priority? 
    ↓ (passes)
[ARTIST/TITLE MATCH] Does artist/title match queue item?
    ↓ (passes)
ACCEPT FILE → Move to /music
```

If quality filter fails:
```
File Found → Extract Format & Bitrate
    ↓
[QUALITY FILTER] Does format/bitrate match priority? 
    ↓ (FAILS)
REJECT FILE → Skip/ignore
```

## Supported Formats

| Format | Extension | Bitrate Type | Notes |
|--------|-----------|--------------|-------|
| MP3 | `.mp3` | VBR/CBR (kbps) | Main lossy format |
| FLAC | `.flac` | Lossless | No bitrate requirement |
| M4A | `.m4a` | AAC (kbps) | iTunes/Apple format |
| OGG | `.ogg` | Vorbis (kbps) | Open format |
| WAV | `.wav` | Lossless | High file size |
| AAC | `.aac` | AAC (kbps) | Raw AAC |

## Logging & Debugging

When a file is rejected by the quality filter, you'll see logs like:

```
[QUALITY-FILTER] Rejected: /downloads/song.mp3 - No matching priority: mp3 192 kbps
[FORMAT-FILTER] Enabled with 2 priority rule(s): [{'format': 'mp3', 'bitrate_kbps': 320}, ...]
```

### Check Download Queue Logs

```bash
tail -f /config/download_queue.log | grep QUALITY-FILTER
```

### Check Recent Queue Events

API endpoint: `GET /api/downloads/queue-events?limit=50&type=quality_filter_reject`

Returns recent quality filter rejections in JSON format.

## Performance Impact

- **Minimal**: Bitrate extraction happens during existing metadata read
- **No extra disk I/O**: Reuses metadata already being extracted
- **No performance penalty**: Filtering is O(1) per file

## Common Issues

### Issue: Files being rejected when they shouldn't be

**Cause**: Bitrate tolerance too strict, or file has different bitrate encoding

**Solution**: 
1. Increase `bitrate_tolerance` (try 10 or 15)
2. Check actual file bitrate: `ffprobe -v error -select_streams a:0 -show_entries stream=bit_rate -of default=noprint_wrappers=1:nokey=1:nk=1 file.mp3`
3. Disable filter temporarily to test

### Issue: Filter not working / all files accepted

**Check:**
1. Is `enabled: true`? 
2. Is `reject_others: true` if you want strict filtering?
3. Are `priorities` configured correctly?
4. Check logs: `tail -f /config/download_queue.log | grep FORMAT-FILTER`

### Issue: Some high-quality files rejected

**Possible:** File's bitrate falls outside tolerance range

**Solution:**
1. Check file: `ffprobe file.mp3`
2. Increase tolerance: `bitrate_tolerance: 10`
3. Or add the specific bitrate to priorities

## Advanced: Custom Format Priority

You can reorder priorities to prefer certain formats:

```yaml
# This will prefer FLAC over MP3
priorities:
  - format: "flac"            # Try FLAC first
    bitrate_kbps: null
  - format: "mp3"             # Fall back to MP3
    bitrate_kbps: 320
```

## Database Schema

New columns added to track quality in download_queue:

```sql
-- No new columns needed; filtering happens during match
-- Rejection is logged to download_queue.log
-- Queue items with rejected files remain in 'queued' status
```

## Recovery: Disabling for Stuck Downloads

If quality filter is too strict and blocking imports:

```yaml
quality_filter:
  enabled: false  # Temporary workaround
```

Then re-enable once you've found the right tolerance/priority settings.

## Future Enhancements

Potential improvements:
1. Per-artist quality overrides (allow lower bitrate for obscure artists)
2. Sample rate validation (44.1 kHz vs 48 kHz vs 96 kHz)
3. Channel config matching (stereo vs mono)
4. VBR vs CBR preferences
5. Web UI for testing quality rules

## Integration with slskd

This feature works with Soulseek downloads managed by slskd:

1. slskd downloads files to `/downloads/Music`
2. Download Queue Monitor scans for new files
3. **Quality Filter** validates format/bitrate
4. Artist/title matching confirms correctness
5. Files moved to `/music` library

The filter helps reject low-quality matches from Soulseek's P2P network before they contaminate your library.

## See Also

- [DOWNLOAD_FILE_VERIFICATION.md](./DOWNLOAD_FILE_VERIFICATION.md) - File validation after download
- [DOWNLOADS_ENHANCEMENT_COMPLETE.md](./DOWNLOADS_ENHANCEMENT_COMPLETE.md) - Overall download system
- [download_queue_manager.py](./download_queue_manager.py) - Implementation details
