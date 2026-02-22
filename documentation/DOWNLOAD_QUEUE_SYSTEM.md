# Download Queue System

The download queue system provides automatic, persistent Soulseek track downloading with retry logic. When you add album tracks to the queue from the artist page, they are automatically searched, downloaded, and imported into your music library.

## Architecture

### Data Flow
1. **User adds album tracks** → Artist page "Queue Tracks" button
2. **Tracks added to database** → `/api/queue/add` stores in `download_queue` table
3. **Queue Processor searches** → `queue_processor.py` continuously polls for queued items
4. **Soulseek search** → Finds matching tracks on the network
5. **Auto-download** → Downloads top result when found
6. **File monitoring** → `/downloads` folder watched for completions
7. **Auto-import** → Moved to `/music` library using beets or custom logic

### Components

#### 1. **Frontend** (`artist.html`)
- "Queue Individual Tracks" button in download modal
- Fetches album tracklist
- Calls `/api/queue/add` for each track
- Shows progress with status indicators

#### 2. **Backend Queue API** (`app.py`)
- `GET /api/queue/status` - Get queue status
- `POST /api/queue/add` - Add track to queue
- `POST /api/queue/<id>/update` - Update status
- `DELETE /api/queue/<id>` - Remove from queue
- `POST /api/queue/<id>/organize` - Move file to library

#### 3. **Queue Manager** (`download_queue_manager.py`)
- Database functions for queue operations
- `add_to_queue()` - Insert queue item
- `get_queue()` - Retrieve active items
- `get_retry_queue()` - Get items ready for retry
- `update_queue_item()` - Update status/fields
- `mark_as_failed()` - Mark failed with retry scheduling
- `check_downloads_folder()` - Monitor for completed downloads

#### 4. **Queue Processor** (`queue_processor.py`) ⭐ **CRITICAL - Must be running!**
- Background worker that processes the queue
- **Searches Soulseek** for queued tracks
- **Auto-downloads** matching results
- **Retries failed** items with exponential backoff
- **Monitors** `/downloads` for completions
- **Runs continuously** with configurable interval (default 30s)

#### 5. **Downloads Watcher** (`downloads_watcher.py`)
- Monitors `/downloads` folder for new files
- Extracts metadata from MP3s
- Organizes files into `/music` library
- Matches to queue items

## Database Schema

### `download_queue` Table

```
id                     INTEGER PRIMARY KEY
artist                 TEXT NOT NULL
album                  TEXT
title                  TEXT NOT NULL
search_query           TEXT              • "Artist Album Track" for searching
source                 TEXT DEFAULT 'soulseek'  • 'soulseek' or 'qbittorrent'
priority               INTEGER DEFAULT 5  • 1-10, lower = higher priority
status                 TEXT DEFAULT 'queued'
  • queued             → Waiting to be processed
  • searching          → Searching Soulseek
  • downloading        → Downloading from peer
  • completed          → Downloaded to /downloads
  • failed             → Failed, not retrying
  • imported           → Already in library
  
retry_count            INTEGER DEFAULT 0  • Current retry attempt
max_retries            INTEGER DEFAULT 5  • Max attempts before giving up
failure_reason         TEXT               • Why it failed
last_failure_time      TIMESTAMP          • When it last failed
retry_delay_minutes    INTEGER DEFAULT 30 • Minutes to wait before retry
next_retry_at          TIMESTAMP          • When to retry next

found_filename         TEXT               • Matched filename in /downloads
file_path              TEXT UNIQUE        • Full path in /music after import
metadata               JSON               • Track metadata after import
created_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP
updated_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP
```

## Setup & Installation

### Docker Compose

Enable the queue processor in `docker-compose.yml`:

```yaml
queue-processor:
  container_name: sptnr-queue-processor
  image: moventura/sptnr:latest
  entrypoint: ["python", "queue_processor.py"]
  args: ["30"]  # Processing interval in seconds
  environment:
    - DB_PATH=/database/sptnr.db
    - DOWNLOADS_DIR=/downloads
    - MUSIC_ROOT=/music
  volumes:
    - ./data:/config
    - /path/to/your/downloads:/downloads
    - /path/to/your/music:/music
  depends_on:
    - sptnr
```

Then start it:
```bash
docker-compose up -d queue-processor
```

### Systemd (Linux Bare Metal)

1. Copy service file:
```bash
sudo cp sptnr-queue-processor.service /etc/systemd/system/
```

2. Update paths in service file:
```bash
sudo nano /etc/systemd/system/sptnr-queue-processor.service
```

3. Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable sptnr-queue-processor
sudo systemctl start sptnr-queue-processor
sudo systemctl status sptnr-queue-processor
```

4. View logs:
```bash
sudo journalctl -u sptnr-queue-processor -f
```

### Manual (Testing)

```bash
# Run once (processes batch)
python3 queue_processor.py 30

# Run with custom interval
python3 queue_processor.py 60
```

## How It Works - Step by Step

### Adding Album Tracks

1. Open artist page → "Download" button on album row
2. Choose "Soulseek" tab
3. Click "Queue Individual Tracks"
4. Select tracks to queue (or all tracks in album)
5. Tracks added to queue with `status='queued'`

### Processing Loop

The `queue_processor.py` runs continuously:

```
While True:
  1. Get up to 5 queued items
  2. For each item:
    a. Search Soulseek using item's search_query
    b. Wait up to 15 seconds for results
    c. If result found:
       - Update status to 'downloading'
       - Auto-download top result
    d. If no result found:
       - Mark as failed
       - Schedule retry (default 60 mins, can retry up to 5 times)
  3. Check /downloads folder for completed files
  4. Match files to queue items
  5. Update status to 'completed'
  6. Sleep for interval (default 30s)
```

### Retry Logic

Failed downloads automatically retry with exponential backoff:

- **1st failure** → Retry in 30 minutes
- **2nd failure** → Retry in 60 minutes (or configurable)
- **3rd+ failures** → Continue retrying up to `max_retries` (default 5)
- **After max retries** → Mark as permanently `failed`

Logs show each attempt:
```
2026-02-17 14:23:15 [Queue Processor] INFO - Queue 42: Searching for 'Artist Album Track'...
2026-02-17 14:23:22 INFO - Queue 42: Found result after 7s: track.mp3
2026-02-17 14:23:23 INFO - Queue 42: Download queued successfully
2026-02-17 14:23:45 INFO - Queue 42: Matched file 'track.mp3'
2026-02-17 14:23:46 INFO - Queue 42: Updated to completed
```

### Monitoring Progress

**Via Web UI:**
- Downloads page shows queue status
- Each track shows: artist, album, title, status, retries

**Via Logs:**
```bash
# Docker
docker logs sptnr-queue-processor -f

# Systemd
journalctl -u sptnr-queue-processor -f
```

**Via Database:**
```sql
SELECT id, artist, album, title, status, retry_count, failure_reason, next_retry_at 
FROM download_queue 
WHERE status != 'imported' 
ORDER BY next_retry_at ASC, created_at DESC;
```

## Configuration

### Queue Processor Interval

Controls how often the processor checks for queued items. Lower = more responsive, higher = less CPU:

- **Docker**: Change `args: ["30"]` in compose file (seconds)
- **Systemd**: Change `ExecStart` command argument
- **Manual**: Pass as command argument: `python3 queue_processor.py 30`

### Retry Settings

Edit database directly or modify `queue_processor.py`:

```python
# In download_queue_manager.py add_to_queue():
priority = 5           # 1=highest, 10=lowest
source = 'soulseek'    # or 'qbittorrent'

# In queue_processor.py mark_failed():
retry_delay_minutes = 30  # Wait before retry
max_retries = 5           # Max attempts
```

### Search Parameters

The search query is automatically built from track info:

```python
# In download_queue_manager.py add_to_queue():
if album:
    search_query = f"{artist} {album} {title}"
else:
    search_query = f"{artist} - {title}"
```

Customize by modifying the query construction before calling `/api/queue/add`.

## Troubleshooting

### Queue items stuck in "queued" state

**Problem:** Items not being processed

**Solutions:**
1. Check processor is running:
   ```bash
   docker ps | grep queue-processor
   ps aux | grep queue_processor
   ```

2. Check logs for errors:
   ```bash
   docker logs sptnr-queue-processor
   journalctl -u sptnr-queue-processor
   ```

3. Verify Soulseek/slskd is enabled and accessible:
   - Check `config.yml` for `slskd.enabled: true`
   - Test slskd endpoint: `curl http://localhost:5030/api/`

4. Check database:
   ```sql
   SELECT COUNT(*) FROM download_queue WHERE status = 'queued';
   ```

### No search results

**Problem:** Tracks searched but nothing found

**Solutions:**
1. Verify search query is correct:
   ```sql
   SELECT id, search_query FROM download_queue WHERE status = 'failed' LIMIT 5;
   ```

2. Search manually on Soulseek to verify track exists

3. Increase retry attempts in database:
   ```sql
   UPDATE download_queue SET retry_count = 0, next_retry_at = CURRENT_TIMESTAMP 
   WHERE id = 42;
   ```

### Files not moving to /music

**Problem:** Downloads completed but not imported

**Solutions:**
1. Check `/downloads` folder has write permissions
2. Check `/music` folder exists and has space
3. Verify beets or file organization script is working:
   ```bash
   python3 downloads_watcher.py
   ```

### Database Locked

**Problem:** "Database is locked" errors

**Solutions:**
1. Ensure only one queue processor is running
2. Check for stray processes:
   ```bash
   lsof /var/lib/sptnr/sptnr.db
   ```

3. Gracefully stop and restart:
   ```bash
   docker stop sptnr-queue-processor
   sleep 5
   docker start sptnr-queue-processor
   ```

## Performance Tuning

### Process More Items Faster

- Reduce interval: `queue_processor.py 10` (10 second checks)
- Increase batch size in `get_queued_items(limit=10)` → increase 10
- Use higher priority for important albums: `priority=1`

### Reduce CPU/Network Usage

- Increase interval: `queue_processor.py 60` (1 minute checks)
- Decrease batch size: `get_queued_items(limit=3)`
- Increase search timeout if searches are timing out

### Large Album Queues

For albums with 50+ tracks:
- Queue them over time (don't all at once)
- Use priority levels (batch 1 = priority 1, batch 2 = priority 3, etc.)
- Space out across multiple days

## API Reference

### Add Track to Queue
```
POST /api/queue/add
Body: {
  "artist": "Artist Name",
  "title": "Song Title",
  "album": "Album Name",
  "source": "soulseek",
  "priority": 5
}
Response: {
  "success": true,
  "queue_id": 42,
  "item": {...full queue record...}
}
```

### Get Queue Status
```
GET /api/queue/status?status=queued&source=soulseek&limit=50
Response: {
  "active": [...queue items...],
  "completed": [...completed items...],
  "newly_completed": [...matched files...]
}
```

### Update Queue Item
```
POST /api/queue/{id}/update
Body: {
  "action": "searching|downloading|failed|completed"
}
```

### Delete Queue Item
```
DELETE /api/queue/{id}
```

## See Also

- [Soulseek Integration](./LISTENBRAINZ_API_FIX.md)
- [Downloads System](./FEATURES_DOWNLOADS.md)
- [Queue Database Schema](./check_db.py) - Lines 662-688
