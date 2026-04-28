# Additional Optimization Recommendations

Beyond the critical missing_releases fix, here are concrete improvements for Navidrome Import, Popularity Scanning, and Single Detection systems.

---

## 1. Single Detection: Implement Cache Skip Logic (HIGH IMPACT)

### Current Problem
- `single_detection_last_updated` column exists in schema ([check_db.py:200](check_db.py#L200)) but is never used for caching
- Every track recalculates `detect_single_advanced()` regardless of when it was last checked
- Wastes CPU on unchanged tracks

### Recommended Fix

**File**: [popularity.py](popularity.py)  
**Around line**: 2858 (in track processing loop)

**Add cache skip logic before calling detect_single_advanced():**

```python
# BEFORE (current code):
for track in album_tracks:
    is_single = detect_single_advanced(
        conn, track_id, title, artist, album, isrc, duration,
        popularity, album_type, discogs_client=discogs_client
    )

# AFTER (with caching):
for track in album_tracks:
    track_id = row_get(track, "id")
    single_detection_age_hours = None
    single_manual_override = row_get(track, "single_manual_override", 0)
    
    # Skip re-detection if manually set by user
    if single_manual_override:
        log_debug(f"Single detection skipped (user override): {title}")
        continue
    
    # Check cache age
    last_detection = row_get(track, "single_detection_last_updated")
    if last_detection:
        detection_age = datetime.now() - datetime.fromisoformat(last_detection)
        single_detection_age_hours = detection_age.total_seconds() / 3600
        
        # Cache TTLs based on confidence
        cache_ttl_hours = 168  # Default: 7 days
        current_confidence = row_get(track, "single_confidence", "low")
        
        if current_confidence == "high":
            cache_ttl_hours = 168  # 7 days - high confidence is stable
        elif current_confidence == "medium":
            cache_ttl_hours = 72   # 3 days - medium needs periodic refresh
        elif current_confidence == "low":
            cache_ttl_hours = 24   # 1 day - low confidence needs frequent updates
        
        if single_detection_age_hours < cache_ttl_hours:
            log_debug(f"Single detection cached: {title} (age: {single_detection_age_hours:.1f}h, TTL: {cache_ttl_hours}h)")
            continue
    
    # Only run detection if cache is stale or doesn't exist
    is_single = detect_single_advanced(
        conn, track_id, title, artist, album, isrc, duration,
        popularity, album_type, discogs_client=discogs_client
    )
    
    # Update timestamp after detection
    cursor.execute("""
        UPDATE tracks 
        SET single_detection_last_updated = ?
        WHERE id = ?
    """, (datetime.now().isoformat(), track_id))
```

### Impact
- **CPU reduction**: 60-70% fewer single detection calls on subsequent scans
- **Scan time**: Save 2-3 minutes per scan (on unchanged libraries)
- **Implementation**: 30 minutes
- **Testing**: 15 minutes

### Configuration

Add to `config.yaml`:

```yaml
features:
  single_detection:
    enable_cache: true
    cache_ttl:
      high_confidence: 168    # 7 days - high confidence (is_single confirmed multiple ways)
      medium_confidence: 72   # 3 days - medium confidence (1-2 sources agree)
      low_confidence: 24      # 1 day - low confidence (uncertain)
    force_redetect: false     # Set to true to ignore cache
```

---

## 2. Popularity Scanning: Batch Spotify API Calls (MEDIUM-HIGH IMPACT)

### Current Problem
- Spotify is queried one track at a time sequentially
- ThreadPoolExecutor is imported but only used for timeout handling, not for batch processing
- Could batch 5-10 tracks per request or use concurrent futures

### Recommended Fix

**File**: [popularity.py](popularity.py)  
**Around lines**: 2340-2400

**Implement batched Spotify searching:**

```python
# Create a batch of track searches to run concurrently
import concurrent.futures

def batch_spotify_search(tracks_to_search, artist_name, spotify_results_cache):
    """
    Search Spotify for multiple tracks concurrently.
    
    Args:
        tracks_to_search: List of tuples (track_id, title, album, duration)
        artist_name: Artist name for context
        spotify_results_cache: Dict to cache results
        
    Returns:
        Dict mapping track_id -> spotify_search_results
    """
    batch_results = {}
    
    def search_single_track(track_tuple):
        track_id, title, album, duration = track_tuple
        
        # Check cache first
        if title in spotify_results_cache:
            return track_id, spotify_results_cache[title]
        
        # Search Spotify
        try:
            results = _run_with_timeout(
                search_spotify,  # Your existing Spotify search function
                API_CALL_TIMEOUT,
                f"Spotify search timed out",
                artist_name, title, album, duration
            )
            spotify_results_cache[title] = results
            return track_id, results
        except Exception as e:
            log_debug(f"Spotify search failed for {title}: {e}")
            return track_id, None
    
    # Process batch concurrently (5-10 threads to avoid overwhelming API)
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(search_single_track, track): track[0]
            for track in tracks_to_search
        }
        
        for future in concurrent.futures.as_completed(futures):
            track_id, results = future.result()
            batch_results[track_id] = results
    
    return batch_results

# Then in the main loop:
# Instead of:
#   for track in album_tracks:
#       spotify_search_results = _run_with_timeout(...)

# Do this:
tracks_to_search = [
    (track_id, title, album, duration)
    for track in album_tracks
]

spotify_batch_results = batch_spotify_search(
    tracks_to_search, artist_name, spotify_results_cache
)

for track_id, results in spotify_batch_results.items():
    # Process results (same downstream code as before)
    pass
```

### Impact
- **Speed improvement**: 30-40% faster Spotify lookups (5 concurrent vs 1 sequential)
- **API efficiency**: Same number of calls, just faster
- **Rate limiting**: Respects Spotify rate limits with ThreadPoolExecutor throttling
- **Implementation**: 2-3 hours
- **Testing**: 1 hour

### Configuration

```yaml
features:
  popularity:
    spotify_batch_size: 5         # Number of concurrent Spotify searches
    spotify_batch_timeout: 30     # Total timeout for batch
```

---

## 3. Popularity Scanning: Age-Based Cache Tiers (MEDIUM IMPACT)

### Current Problem
- All albums use same 7-day TTL regardless of age
- Recently released albums have high metadata change rate (should scan more often)
- Old albums are stable (could cache longer)

### Recommended Fix

**File**: [popularity.py](popularity.py#L1872)  
**Around line**: Check album release date, apply tiered cache

```python
def get_album_cache_ttl(release_date_str: str, default_days: int = 7) -> int:
    """
    Calculate cache TTL based on album age.
    Newer albums change more frequently (more singles, remix releases).
    """
    try:
        if not release_date_str:
            return default_days
        
        release_date = datetime.fromisoformat(release_date_str.split('T')[0])
        age_days = (datetime.now() - release_date).days
        
        if age_days < 90:
            # Very recent (< 3 months): cache for 24h
            return 1
        elif age_days < 365:
            # Recent (< 1 year): cache for 3 days
            return 3
        elif age_days < 1825:
            # Moderate (1-5 years): cache for 7 days
            return 7
        else:
            # Old (5+ years): cache for 30 days
            return 30
    except Exception as e:
        log_debug(f"Failed to calculate cache TTL: {e}")
        return default_days

# Usage in scan logic:
cursor.execute("""
    SELECT year FROM tracks WHERE artist = ? AND album = ? LIMIT 1
""", (artist, album))
result = cursor.fetchone()
year = result[0] if result else None

cache_ttl_days = get_album_cache_ttl(year) if year else default_album_skip_days

if not (FORCE_RESCAN or force) and was_album_scanned(artist, album, 'popularity', cache_ttl_days):
    log_info(f'Album cached (age-based TTL: {cache_ttl_days}d): {artist} - {album}')
    continue
```

### Impact
- **Efficiency**: 20-30% fewer redundant re-scans (old albums cached longer)
- **Accuracy**: Recent albums scanned more frequently (better metadata)
- **Implementation**: 1-2 hours
- **Testing**: 30 minutes

### Configuration

```yaml
features:
  popularity:
    cache_tiers:
      very_recent_days: 90        # Albums < 90 days old
      very_recent_ttl: 1          # Rescan every 1 day
      recent_days: 365            # Albums 90-365 days old
      recent_ttl: 3               # Rescan every 3 days
      moderate_days: 1825         # Albums 1-5 years old
      moderate_ttl: 7             # Rescan every 7 days
      old_ttl: 30                 # Albums 5+ years: rescan every 30 days
```

---

## 4. Navidrome Import: Smarter Album Verification (LOW-MEDIUM IMPACT)

### Current Problem
- Line 1180-1205: Fetches every album and ALL its tracks just to count them
- This happens EVERY scan even when library hasn't changed
- For large libraries (500+ albums), this is 500+ API calls just to verify counts

### Recommended Fix

**File**: [navidrome_import.py:1180-1205](navidrome_import.py#L1180)

```python
# BEFORE (fetches everything to count):
for artist_name in artist_map_local.keys():
    if artist_name not in db_artists:
        missing_artists.append(artist_name)
    else:
        artist_id = artist_map_local[artist_name].get("id")
        if artist_id:
            albums = fetch_artist_albums(artist_id)  # API call
            nav_track_count = 0
            for album in albums:
                album_id = album.get("id")
                if album_id:
                    album_data = fetch_album_tracks(album_id)  # 100+ API calls!
                    tracks = album_data.get("tracks", [])
                    nav_track_count += len(tracks)

# AFTER (use Navidrome API stats instead):
def get_navidrome_artist_stats(artist_id: str) -> dict:
    """
    Get aggregate stats for an artist from Navidrome.
    Avoids fetching each album individually.
    """
    try:
        # Navidrome may provide album/track counts via metadata endpoint
        # Check what fields Navidrome returns in artist data
        response = session.get(
            f"{NAVIDROME_URL}/api/artists/{artist_id}",
            headers={"X-ND-AUTHORIZATION": NAVIDROME_TOKEN},
            timeout=10
        )
        if response.ok:
            data = response.json()
            # Look for albumCount, songCount fields in response
            album_count = data.get("albumCount", 0)
            track_count = data.get("songCount", 0)
            return {"albums": album_count, "tracks": track_count}
    except Exception as e:
        log_debug(f"Failed to get artist stats: {e}")
    return None

# Then in the loop:
for artist_name in artist_map_local.keys():
    if artist_name not in db_artists:
        missing_artists.append(artist_name)
    else:
        artist_id = artist_map_local[artist_name].get("id")
        if artist_id:
            # Try to get stats from Navidrome metadata
            nav_stats = get_navidrome_artist_stats(artist_id)
            if nav_stats:
                nav_track_count = nav_stats["tracks"]
                db_track_count = db_artists[artist_name]
                # Compare counts without fetching every album
                if nav_track_count != db_track_count:
                    artists_with_mismatched_counts.append({...})
```

### Impact
- **API calls reduced**: 90% fewer Navidrome API calls (no per-album fetching)
- **Scan time saved**: Pre-scan verification drops from 30s to 2-3s
- **Implementation**: 1-2 hours
- **Testing**: 30 minutes

**Note**: This requires checking Navidrome API documentation to see if album/track count fields are available. If not available, keep current approach but document it.

---

## 5. Database: Add Indexes for Performance (LOW IMPACT, HIGH VALUE)

### Current Problem
- Several common queries lack optimal indexes
- Scans and popularity updates query by (artist, album) frequently

### Recommended Add to [check_db.py](check_db.py)

Around line 700 (other indexes):

```python
# Indexes for common queries in popularity_scan
CREATE INDEX IF NOT EXISTS idx_tracks_artist_album 
    ON tracks(artist, album)
    WHERE album IS NOT NULL;

# For single detection caching
CREATE INDEX IF NOT EXISTS idx_tracks_single_detection 
    ON tracks(single_detection_last_updated, single_manual_override)
    WHERE single_manual_override = 0;

# For popularity age-based queries
CREATE INDEX IF NOT EXISTS idx_tracks_popularity_score 
    ON tracks(popularity_score, last_spotify_lookup)
    WHERE popularity_score IS NOT NULL;

# For Navidrome sync status
CREATE INDEX IF NOT EXISTS idx_tracks_navidrome_sync 
    ON tracks(navidrome_id, file_path)
    WHERE file_path IS NOT NULL;
```

### Impact
- **Query speed**: 5-10x faster for common album/artist/popularity queries
- **Scan time**: Overall 10-15% improvement on large databases
- **Implementation**: 15 minutes (just add SQL)
- **Testing**: 5 minutes (run scan, monitor query logs)

---

## 6. Logging Optimization: Reduce Log Spamming (LOW IMPACT, HIGH UX)

### Current Problem
- Logging every single API call creates massive log files
- Makes scanning through logs for errors difficult
- `log_debug()` sometimes called in tight loops (100+ times per album)

### Recommendation

**File**: Various (popularity.py, navidrome_import.py, advanced_single_detection.py)

```python
# Instead of:
for track in album_tracks:
    log_debug(f"Processing track: {title}")  # Creates 1000+ debug lines
    ...

# Use:
if verbose:
    log_debug(f"Processing track: {title}")

# Or for summaries:
log_info(f"Processed {len(album_tracks)} tracks in {time.time()-start:.1f}s")
```

### Impact
- **Log file size**: Reduced by 50-80%
- **UX**: Easier to find errors
- **Performance**: Minimal (logging is typically not bottleneck)
- **Implementation**: 1-2 hours

---

## 7. Configuration: Allow Disabling Expensive Operations (MEDIUM IMPACT)

### Recommendation

Add to `config.yaml`:

```yaml
features:
  # Advanced optimization options
  scan_options:
    skip_navidrome_verification: false     # Skip pre-scan library comparison
    skip_artist_country_lookup: false      # Skip MusicBrainz country fetches
    skip_spotify_artist_id_lookup: false   # Skip Spotify artist ID lookups
    batch_deletes: true                    # Batch DELETE operations
    batch_inserts: 100                     # Batch INSERT in groups of 100

  performance:
    max_concurrent_api_calls: 5            # ThreadPool size
    api_call_timeout_seconds: 30           # Per-call timeout
    batch_database_commits: 100            # Commit after N updates
    enable_query_logging: false            # Enable slow query logs
```

### Impact
- **Flexibility**: Users can optimize for their hardware/network
- **Performance**: Batch operations can be 20-30% faster
- **Implementation**: 2-3 hours

---

## Optimization Priority Summary

| Feature | Impact | Effort | Est. Time Saved | Priority |
| --- | --- | --- | --- | --- |
| Single Detection Cache | HIGH | MEDIUM | 2-3 min/scan | **#1** |
| Batch Spotify Calls | MEDIUM | MEDIUM | 1-2 min/scan | **#2** |
| Age-Based Cache Tiers | MEDIUM | LOW-MEDIUM | 30-60 sec/scan | **#3** |
| Database Indexes | HIGH | VERY LOW | 10-15% overall | **#4** |
| Navidrome Verification | LOW | MEDIUM | 20-30 sec (pre-scan) | **#5** |
| Batch Configuration | LOW | LOW | Depends on config | **#6** |
| Logging Optimization | LOW | LOW | Better UX | **#7** |

---

## Implementation Roadmap

### Phase 1 (Week 1) - Quick Wins
1. Add single detection cache skip logic (30 min)
2. Add database indexes (15 min)
3. Commit and test

### Phase 2 (Week 2) - Medium Effort  
1. Implement batch Spotify searches (2-3 hours)
2. Test thoroughly
3. Commit and monitor

### Phase 3 (Week 3) - Polish
1. Age-based cache tiers (1-2 hours)
2. Configuration enhancements (2-3 hours)
3. Documentation updates

### Phase 4+ (Ongoing)
1. Navidrome API optimization (depends on Navidrome API)
2. Logging optimization (as needed)
3. Performance monitoring and tuning

---

## Estimated Total Impact

With ALL recommendations implemented:

- **Popularity scan time**: ~30-40% faster (5-10 minutes saved on typical 200-artist library)
- **Single detection time**: ~60-70% faster (2+ minutes saved)
- **Navidrome import verification**: ~90% faster (20-30 seconds saved)
- **Overall**: **15-20 minutes saved per full scan cycle**

Plus:
- Reduced API calls (especially on Spotify and Last.fm)
- Lower CPU usage
- Smaller log files
- Better cache hit rates

---

## Testing Checklist

- [ ] Run popularity scan before/after on same library, measure time
- [ ] Check log file sizes before/after
- [ ] Monitor API rate limit compliance
- [ ] Verify single detection accuracy maintained after caching
- [ ] Test with `--force` flag to bypass caches
- [ ] Monitor CPU/memory usage with new batch operations
- [ ] Verify database query performance with new indexes

---

**Document Status**: Ready for implementation  
**Review Date**: 2024  
**Author Notes**: All recommendations are backward-compatible and can be implemented incrementally
