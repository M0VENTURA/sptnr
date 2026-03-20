# Soulseek Queue Download Fix

## Issue

Songs were being searched on Soulseek but never being passed to download due to timeout issues.

## Root Causes Fixed

1. **Timeout too short**: The 30-second timeout was insufficient for Soulseek to resolve all peer responses (each peer times out after 5 seconds)
2. **Early completion logic missing**: System wasn't waiting for all responses to come in before timing out
3. **File format handling**: Code wasn't properly handling different response object formats

## Changes Made

### 1. Increased Poll Timeouts

- **app.py** (managed_downloads): 30s → 60s
- **queue_processor.py** (download queue): 15s → 45s
- Reason: Soulseek peer responses are slow due to network conditions; more time = more results collected

### 2. Added Early Exit Logic

- Exit immediately when search completes AND we have a good match (≥30% score)
- Exit when search completes with NO results
- This prevents unnecessary waiting while still collecting all available data

### 3. Improved File Detection

- Handle both dict and object formats from responses
- Better logging to track why files aren't being found
- More robust filename extraction

## Verification Checklist

### 1. Ensure Queue Processor is Running

```bash
# Check if queue_processor is running
ps aux | grep queue_processor

# If not running, either:
# - Navigate to SPTNR web UI → Tools → Queue Manager → Restart Processor
# - Or manually start it:
#   cd /path/to/sptnr
#   python3 queue_processor.py 30 &
```

### 2. Check Queue Items

- Go to SPTNR Dashboard
- Look for items in the "Download Queue" section
- Items should be in "queued" status initially
- Then move to "searching" → "downloading" → "completed"

### 3. Monitor Logs

```bash
# Watch queue processor logs
tail -f /config/queue_processor.log

# Watch for lines like:
# - "Queue X: Searching for 'artist - title'..."
# - "Queue X: ✓ Found result after Xs from username"
# - "Queue X: Downloading 'filename'..."
```

### 4. Check Soulseek Activity

- Open slskd web interface (usually `http://localhost:5030`)
- You should see downloads appearing in the downloads section
- Files should complete and appear in `/downloads` folder

## If Downloads Still Not Starting

### Check 1: Items Not in Queue

- Verify items are being added to the download_queue table
- Check what's triggering queue additions (playlists, manual requests, etc.)

### Check 2: Queue Processor Not Processing

- Verify queue_processor.py is running: `ps aux | grep queue_processor`
- Restart it: Use SPTNR UI or run `python3 queue_processor.py 30 &`

### Check 3: Soulseek Issues

- Check Soulseek logs for timeout patterns
- If too many timeouts, may indicate network issues or slskd connection problems
- Verify slskd API key is configured correctly

### Check 4: Database Issues

- Verify download_queue table exists: `sqlite3 /database/sptnr.db ".tables" | grep download_queue`
- Check for corrupted records: `sqlite3 /database/sptnr.db "SELECT COUNT(*) FROM download_queue;"`

## Performance Notes

- With 45-60 second timeouts, searches may take longer but will collect more results
- System will stop waiting early if a good match is found (≥30% match score)
- Timeout is per-search, not global; multiple searches can run in parallel
- Monitor system resources if many downloads are happening simultaneously
