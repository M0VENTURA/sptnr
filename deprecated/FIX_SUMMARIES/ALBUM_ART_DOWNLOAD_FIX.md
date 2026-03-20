# Album Art Download Fix

## Problem
During the popularity scan for the "10 Years" artist, the logs showed:
```
[ALBUM_ART] Constructed CAA URL for 10 Years - (how to live) AS GHOSTS: https://coverartarchive.org/release-group/d4299b84-864e-48d7-9a10-fc639d4594e8/front-500
```

But the album art was never actually downloaded and saved to the database. The CAA URL was being constructed and logged, but no actual HTTP request was made to download the image.

## Root Cause
The `popularity.py` module had a function `fetch_album_art_url_from_musicbrainz()` that returned only the **URL** to the Cover Art Archive. However, there was no code to actually:
1. Download the image from that URL
2. Save the binary image data to the `album_art` table

The URL was being stored in the `album_art_url` variable and passed to the database UPDATE statement, but that update was only saving it as a reference, not the actual image bytes.

## Solution
Added `download_and_save_album_art()` function to:
1. **Download** the image from the CAA URL using `requests.get()`
2. **Verify** the image data is valid (non-empty, HTTP 200 status)
3. **Save** the binary image data to the `album_art` table with `source='musicbrainz_caa'`

### Code Changes

#### New Function (popularity.py)
```python
def download_and_save_album_art(artist: str, album: str, art_url: str) -> bool:
    """Download album art image from CAA URL and save to database."""
    try:
        import requests
        
        if not art_url:
            return False
        
        # Download image from CAA
        resp = requests.get(art_url, timeout=5)
        if resp.status_code != 200:
            log_debug(f"[ALBUM_ART] Failed to download image from {art_url}: HTTP {resp.status_code}")
            return False
        
        image_data = resp.content
        if not image_data or len(image_data) == 0:
            log_debug(f"[ALBUM_ART] Downloaded image is empty for {artist} - {album}")
            return False
        
        # Save to database
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO album_art 
            (artist_name, album_name, image_data, image_mime_type, source, downloaded_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (artist, album, image_data, "image/jpeg", "musicbrainz_caa"))
        conn.commit()
        conn.close()
        
        log_info(f"[ALBUM_ART] Successfully downloaded and saved album art for {artist} - {album} ({len(image_data)} bytes)")
        return True
        
    except requests.exceptions.Timeout:
        log_debug(f"[ALBUM_ART] Timeout downloading image from {art_url} for {artist} - {album}")
        return False
    except Exception as e:
        log_debug(f"[ALBUM_ART] Failed to download/save album art for {artist} - {album}: {e}")
        return False
```

#### Integration Point (popularity.py, popularity_scan function)
```python
# Before: Only fetching URL, not downloading image
album_art_url = fetch_album_art_url_from_musicbrainz(artist, album)
if album_art_url:
    log_info(f'Fetched album art URL for {artist} - {album}: {album_art_url}')

# After: Actually download and save the image
album_art_url = fetch_album_art_url_from_musicbrainz(artist, album)
if album_art_url:
    log_info(f'Fetched album art URL for {artist} - {album}: {album_art_url}')
    # Download and save the actual image data
    if download_and_save_album_art(artist, album, album_art_url):
        log_info(f'[ALBUM_ART] Album art image downloaded and saved for {artist} - {album}')
    else:
        log_debug(f'[ALBUM_ART] Failed to download album art image for {artist} - {album}')
```

## Impact
- Album art will now be **downloaded and stored** in the `album_art` table during popularity scans
- Images are sourced from MusicBrainz Cover Art Archive (CAA)
- Graceful handling of download failures (timeouts, missing images, etc.)
- Proper logging at both INFO and DEBUG levels

## Database Schema
The `album_art` table structure (from check_db.py):
```sql
CREATE TABLE IF NOT EXISTS album_art (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artist_name TEXT NOT NULL,
    album_name TEXT NOT NULL,
    image_data BLOB,
    image_mime_type TEXT DEFAULT 'image/jpeg',
    source TEXT,
    downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(artist_name, album_name)
);
```

## Bonus Fix: Cursor Closing Error
Also fixed a related issue where closing the cursor before single detection was causing an AttributeError:
```python
# Before: 
if cursor is not None and not cursor._closed:
    cursor.close()

# After:
if cursor is not None:
    try:
        cursor.close()
    except:
        pass  # Cursor might already be closed, that's OK
```

This prevents the error: `'sqlite3.Cursor' object has no attribute '_closed'`

## Testing
After running popularity scan with this fix, you should see in logs:
```
[ALBUM_ART] Successfully downloaded and saved album art for {Artist} - {Album} ({size} bytes)
```

Instead of just the URL being constructed.

## Commit
- Commit: `6e03cf8`
- Message: "Add album art download during popularity scan and fix cursor closing error"
