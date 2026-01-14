# Testing Single Detection Fix - Discogs Music Videos

This document explains how to test the fix for single detection with Discogs music videos.

## Problem Addressed

The issue reported that "+44 - When your heart stops beating" wasn't detected as a single, even though it's listed on Discogs as a single.

## What Was Fixed

### 1. Enhanced `single_detector.py`
- Added Discogs music video checking as an additional source
- New source: `"discogs_video"` 
- New audit field: `discogs_video_found`
- Video detection now contributes to single confidence scoring

### 2. Enhanced `api_clients/discogs.py`
- Added "Strong path 3" to the `is_single()` method
- Now checks for videos within release data
- If a release has a video matching the track title, it's considered a strong single indicator

## How to Test

### Prerequisites
- Set environment variable: `DISCOGS_TOKEN=<your_token>`
- Ensure the track is in your database

### Manual Test - Specific Example

Test with the track mentioned in the issue:

```bash
# Set up environment
export DISCOGS_TOKEN="your_discogs_token_here"

# Create a test script
cat > /tmp/test_plus44.py << 'EOF'
import os
import sys
sys.path.insert(0, '/home/runner/work/sptnr/sptnr')

from api_clients.discogs import DiscogsClient

# Test the specific track
client = DiscogsClient(token=os.getenv("DISCOGS_TOKEN"), enabled=True)

title = "When Your Heart Stops Beating"
artist = "+44"

print(f"Testing: '{title}' by '{artist}'")
print("-" * 60)

# Check if detected as single
is_single = client.is_single(title, artist)
print(f"is_single(): {is_single}")

# Check if has video
has_video = client.has_official_video(title, artist)
print(f"has_official_video(): {has_video}")

print("-" * 60)
if is_single or has_video:
    print("✅ SUCCESS: Track detected via Discogs")
else:
    print("⚠️  Track not detected - check title/artist variations")
EOF

# Run the test
python3 /tmp/test_plus44.py
```

### Expected Results

The track should now be detected as a single through one or more of these paths:

1. **Discogs Single Format**: If the release has "Single" in its format
2. **Discogs Video in Release**: If a release containing the track has a matching music video
3. **Discogs Official Video**: If there's an official music video in the master release

### Full Integration Test

To test with the full single detection pipeline:

```python
from single_detector import rate_track_single_detection

track = {
    "id": "test_plus44",
    "title": "When Your Heart Stops Beating",
    "is_spotify_single": False,  # May vary based on Spotify data
    "spotify_total_tracks": None
}

artist_name = "+44"
album_ctx = {}  # Or actual album context if available

result = rate_track_single_detection(
    track=track,
    artist_name=artist_name,
    album_ctx=album_ctx,
    config={},
    verbose=True  # Enable verbose logging to see all checks
)

print(f"is_single: {result['is_single']}")
print(f"confidence: {result['single_confidence']}")
print(f"sources: {result['single_sources']}")
```

### Check Logs

When verbose mode is enabled, you should see output like:

```
🎵 Checking: When Your Heart Stops Beating
🔍 Checking Discogs single (online)...
✅ Discogs single FOUND
🔍 Checking MusicBrainz single (online)...
🔍 Checking Last.fm tags (online)...
🔍 Checking Discogs music video (online)...
✅ Discogs music video FOUND
✅ SINGLE (multiple sources): When Your Heart Stops Beating
```

## Confidence Levels

The system now considers these sources:

| Source | Weight | Notes |
|--------|--------|-------|
| spotify | Medium | From Spotify metadata |
| short_release | Low | 1-2 track releases |
| discogs | High | Explicit "Single" format |
| **discogs_video** | **Medium** | **Music video in release** |
| musicbrainz | High | MusicBrainz single type |
| lastfm | Medium | Last.fm "single" tag |

**Confidence calculation:**
- **High confidence**: 2+ sources (including at least one high-weight source)
- **Medium confidence**: 1 source + canonical title
- **Low confidence**: No sources or non-canonical title

## Troubleshooting

### Track Not Detected

If the track still isn't detected:

1. **Check title variations**: Try different capitalizations or formats
   - "When Your Heart Stops Beating"
   - "When your heart stops beating"
   - "When Your Heart Stops Beating"

2. **Check artist name**: Try variations
   - "+44"
   - "Plus 44"
   - "Plus-44"

3. **Verify on Discogs manually**: 
   - Search: https://www.discogs.com/search/?q=%2B44+When+Your+Heart+Stops+Beating
   - Check if it's listed as a single or has a music video

4. **Check rate limiting**: Discogs has rate limits (1 request per 0.35 seconds)
   - Wait a bit and try again
   - Check for HTTP 429 errors in logs

### Debug Mode

Enable debug logging to see detailed API responses:

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

## Database Schema

The fix adds this audit field to track video detection:

```sql
-- New field in tracks table
discogs_video_found INTEGER DEFAULT 0  -- 1 if Discogs video detected, 0 otherwise
```

Note: This field may need to be added to your database schema if running an older version.

## API Rate Limits

Be aware of Discogs API rate limits:
- **Rate**: 1 request per 0.35 seconds per token
- **Daily**: 60 requests per minute (enforced over a sliding window)

The code includes automatic throttling to respect these limits.
