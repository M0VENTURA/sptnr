# Single Detection System - Recommendations for Further Improvements

## Current Architecture
- **single_detector.py**: Multi-source weighted scoring with configurable thresholds
- **ddg_searchapi_checker.py**: DuckDuckGo video verification via SearchAPI.io
- **singledetection.py**: Integration point with legacy detection fallback
- **beets_integration.py**: Skip-existing artists during import (already implemented)

---

## High-Impact Recommendations

### 1. **API Response Caching** (Priority: HIGH)
**Problem**: External API calls (Spotify, Discogs, MB, DDG) add 5-30s per track check  
**Solution**: Implement tiered caching

```python
# Add to check_db.py schema
CREATE TABLE single_detection_cache (
    id INTEGER PRIMARY KEY,
    artist TEXT NOT NULL,
    title TEXT NOT NULL,
    album TEXT,
    source TEXT NOT NULL,  -- 'discogs'|'spotify'|'musicbrainz'|'lastfm'|'ddg'
    result BLOB,  -- JSON
    confidence REAL,
    cached_at TIMESTAMP,
    expires_at TIMESTAMP,
    UNIQUE(artist, title, source)
)
```

**Benefits**:
- Dramatically faster re-scans (cache hits instead of API calls)
- Reduced API rate limiting pressure
- Can skip expensive checks if cached recently

**Implementation**:
```python
def get_cached_result(artist, title, source, max_age_days=30) -> Optional[dict]:
    """Get cached result if fresh enough"""
    conn = get_db()
    result = conn.execute("""
        SELECT result FROM single_detection_cache
        WHERE artist=? AND title=? AND source=?
        AND expires_at > datetime('now')
    """, (artist, title, source)).fetchone()
    return json.loads(result[0]) if result else None

def cache_result(artist, title, source, result, ttl_days=30):
    """Store detection result in cache"""
    conn = get_db()
    conn.execute("""
        INSERT OR REPLACE INTO single_detection_cache
        (artist, title, source, result, cached_at, expires_at)
        VALUES (?, ?, ?, ?, datetime('now'), datetime('now', '+' || ? || ' days'))
    """, (artist, title, source, json.dumps(result), ttl_days))
    conn.commit()
```

---

### 2. **Parallel Source Checking** (Priority: HIGH)
**Problem**: Sources are checked sequentially, adding 20-50s per decision  
**Solution**: Concurrent API requests

```python
from concurrent.futures import ThreadPoolExecutor, as_completed

def decide_is_single_parallel(meta: TrackMeta, max_workers=4) -> Dict[str, Any]:
    """Check all sources in parallel"""
    sources = {
        'spotify': lambda: check_spotify_single(meta),
        'discogs': lambda: check_discogs_single(meta),
        'musicbrainz': lambda: check_musicbrainz_single(meta),
        'lastfm': lambda: check_lastfm_single(meta),
        'ddg_video': lambda: check_ddg_video(meta),
    }
    
    breakdown = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(func): name for name, func in sources.items()}
        
        for future in as_completed(futures, timeout=30):
            source_name = futures[future]
            try:
                result = future.result()
                breakdown[source_name] = result
            except Exception as e:
                logging.warning(f"Source {source_name} failed: {e}")
    
    # Aggregate results
    return aggregate_sources(breakdown)
```

**Benefits**:
- 4-5x faster decision making
- Better responsiveness for UI lookups
- Timeout handling per-source

---

### 3. **Decision Audit Trail** (Priority: HIGH)
**Problem**: Can't explain why a track was classified as single/album in past scans  
**Solution**: Store decision history

```python
CREATE TABLE single_detection_decisions (
    id INTEGER PRIMARY KEY,
    track_id TEXT NOT NULL,
    artist TEXT NOT NULL,
    title TEXT NOT NULL,
    album TEXT,
    is_single BOOLEAN NOT NULL,
    score INTEGER,
    confidence TEXT,
    source_breakdown BLOB,  -- JSON with details
    decision_timestamp TIMESTAMP,
    reviewed_by TEXT,  -- Manual override if applicable
    override_reason TEXT,
    FOREIGN KEY(track_id) REFERENCES tracks(id)
)

CREATE INDEX idx_single_decisions_track ON single_detection_decisions(track_id)
CREATE INDEX idx_single_decisions_timestamp ON single_detection_decisions(decision_timestamp DESC)
```

**Benefits**:
- Audit trail for ML model training
- Manual override capability
- Analytics on decision confidence over time

---

### 4. **Periodic Recheck Logic** (Priority: MEDIUM)
**Problem**: Singles sometimes become album tracks and vice versa (e.g., compilation release)  
**Solution**: Automatic recheck based on age

```python
# Configuration
RECHECK_INTERVALS = {
    'high_confidence': 90,   # 90 days
    'medium_confidence': 30, # 30 days
    'low_confidence': 7,     # 7 days
    'never_checked': -1      # Always recheck
}

def should_recheck_single(track_id, last_decision: dict) -> bool:
    """Check if decision is stale and needs rechecking"""
    if not last_decision:
        return True
    
    confidence = last_decision.get('confidence', 'low')
    interval = RECHECK_INTERVALS[confidence]
    
    decision_age_days = (datetime.now() - parse_timestamp(
        last_decision['decision_timestamp']
    )).days
    
    return decision_age_days >= interval
```

**Benefits**:
- Catches reclassifications (single → album track)
- Keeps old data fresh automatically
- Configurable by confidence level

---

### 5. **Batch Import Optimization** (Priority: MEDIUM)
**Problem**: Single detection during large imports re-checks everything  
**Solution**: Bulk pre-detection before import

```python
def detect_singles_before_import(artist_path: str, batch_size=50):
    """Pre-scan all tracks in import path before beets import"""
    
    tracks = collect_tracks_from_path(artist_path)
    decisions = {}
    
    # Process in batches for efficient API usage
    for i in range(0, len(tracks), batch_size):
        batch = tracks[i:i+batch_size]
        
        # Check cache first
        uncached = [t for t in batch if not get_cached_result(t)]
        
        # Parallel check uncached
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(detect_single_advanced, t['artist'], t['title']): t 
                for t in uncached
            }
            
            for future in as_completed(futures):
                track = futures[future]
                result = future.result()
                decisions[f"{track['artist']}-{track['title']}"] = result
                cache_result(track['artist'], track['title'], result)
    
    return decisions
```

---

### 6. **Local Spotify Cache** (Priority: MEDIUM)
**Problem**: Spotify API rate limits and latency  
**Solution**: Cache Spotify album metadata locally

```python
CREATE TABLE spotify_cache (
    id INTEGER PRIMARY KEY,
    artist TEXT NOT NULL,
    album TEXT NOT NULL,
    album_type TEXT,  -- 'single'|'album'|'compilation'
    total_tracks INTEGER,
    release_date TEXT,
    cached_at TIMESTAMP,
    UNIQUE(artist, album)
)

def get_spotify_album_cached(artist: str, album: str) -> Optional[dict]:
    """Get Spotify data from cache or API"""
    # Check cache first (24 hour TTL)
    cache = get_db().execute("""
        SELECT * FROM spotify_cache
        WHERE artist=? AND album=?
        AND datetime(cached_at, '+24 hours') > datetime('now')
    """, (artist, album)).fetchone()
    
    if cache:
        return dict(cache)
    
    # Fetch from API and cache
    data = spotify_client.get_album(artist, album)
    if data:
        cache_spotify_album(artist, album, data)
    
    return data
```

**Benefits**:
- 90%+ cache hit rate after first scan
- Eliminates 90% of Spotify API calls
- Faster single detection

---

### 7. **Confidence Scoring Visualization** (Priority: MEDIUM)
**Problem**: UI doesn't show why tracks are classified as singles  
**Solution**: Add decision details to album/track pages

```html
<!-- In track.html or album.html -->
<div class="single-detection-details">
    <p>Classification: 
        <span class="badge" 
              title="Based on: {{ track.single_sources|join(', ') }}">
            {% if track.is_single %}Single{% else %}Album Track{% endif %}
        </span>
    </p>
    <small class="text-muted">
        Confidence: 
        <span class="confidence-{{ decision.confidence }}">
            {{ decision.confidence|upper }} ({{ decision.score }}/{{ threshold }})
        </span>
    </small>
    <details>
        <summary>View sources</summary>
        <ul>
        {% for source, details in decision.source_breakdown.items() %}
            <li>{{ source }}: {{ details.is_single|yesno:'Yes,No' }} 
                (weight: {{ details.weight }})
            </li>
        {% endfor %}
        </ul>
    </details>
</div>
```

---

### 8. **Configuration Page for Tuning** (Priority: LOW)
**Problem**: Weights are hardcoded; can't tune without code changes  
**Solution**: Database-backed configuration

```python
CREATE TABLE single_detection_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,  -- JSON for complex values
    description TEXT,
    updated_at TIMESTAMP
)

# In config page
POST /api/config/single-detection
{
    "threshold_score": 100,
    "required_match_count": 2,
    "weights": {
        "discogs_single": 100,
        "spotify_single": 50,
        ...
    },
    "video_exception_artists": ["Weird Al Yankovic"],
    "label_whitelist": ["Universal Music Group", ...]
}
```

---

### 9. **API Rate Limiting & Backoff** (Priority: MEDIUM)
**Problem**: Hit 429 (Too Many Requests) when doing bulk single detection  
**Solution**: Implement exponential backoff

```python
from tenacity import retry, stop_after_attempt, wait_exponential

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10)
)
def check_spotify_single_with_backoff(artist: str, title: str):
    """Retry with exponential backoff on rate limit"""
    return check_spotify_single(TrackMeta(artist=artist, title=title))
```

---

### 10. **Single/Album Statistics Dashboard** (Priority: LOW)
**Problem**: No visibility into classification distribution  
**Solution**: Add stats to dashboard

```sql
SELECT 
    is_single,
    COUNT(*) as count,
    ROUND(AVG(CAST(final_score AS FLOAT)), 2) as avg_score,
    ROUND(AVG(CAST(stars AS FLOAT)), 2) as avg_rating
FROM tracks
GROUP BY is_single

-- By confidence level
SELECT 
    single_confidence,
    is_single,
    COUNT(*) as count
FROM tracks
GROUP BY single_confidence, is_single
```

Display as:
- Pie chart: Singles vs Album Tracks
- Bar chart: By artist (top 10)
- Confidence distribution histogram

---

## Quick Wins (Easy Implementations)

### Add to config.yaml
```yaml
features:
  label_whitelist:
    - "Universal Music Group"
    - "Sony Music Entertainment"
    - "Warner Bros. Records"
  
  video_exception_artists:
    - "Weird Al Yankovic"
    - "The Lonely Island"
  
  single_detection:
    use_cache: true
    cache_ttl_days: 30
    parallel_sources: true
    max_workers: 4
    enable_ddg_check: true
    recheck_stale_decisions: true
```

### Add UI Elements
- Show `single_confidence` badge in track list
- Add confidence tooltip explaining score
- Manual override button with reason field
- "Re-detect single status" action

### Logging Improvements
- Log decision score vs threshold
- Log which source(s) confirmed single
- Log API latency per source
- Alert if any source is consistently failing

---

## Implementation Priority

**Phase 1 (This Month):**
1. API Response Caching - biggest perf win
2. Parallel Source Checking - 4-5x faster decisions
3. Decision Audit Trail - enables ML/analytics

**Phase 2 (Next Month):**
1. Local Spotify Cache - eliminates 90% of API calls
2. Periodic Recheck Logic - keeps data fresh
3. Config Page for Tuning - removes hardcoded values

**Phase 3 (Later):**
1. Batch Import Optimization
2. Confidence Visualization
3. Statistics Dashboard

---

## Testing Strategy

```python
# Test with known singles/albums
test_cases = [
    # (artist, title, album, expected_is_single, expected_confidence)
    ("The Beatles", "A Day in the Life", "Sgt Pepper's", False, "high"),
    ("Weird Al Yankovic", "Hardware Store", "Poodle Hat", False, "high"),  # Video false positive
    ("Taylor Swift", "Lover", None, True, "high"),  # From Lover (single)
    ("Unknown Artist", "Obscure Track", "Unknown Album", None, "low"),  # No data
]

def run_detection_tests():
    for artist, title, album, expected_is_single, expected_conf in test_cases:
        result = detect_single_advanced(artist, title, album)
        assert result['is_single'] == expected_is_single
        assert result['confidence'] >= expected_conf  # At minimum
        print(f"✅ {artist} - {title}")
```

---

## Monitoring & Alerts

Add to logging/monitoring:
```python
if decision['confidence'] == 'low':
    logging.warning(f"Low confidence single decision: {artist} - {title}")

if decision['score'] == threshold:
    logging.info(f"Decision exactly at threshold: {artist} - {title}")

if 'ddg_video' in decision['source_breakdown']:
    logging.info(f"DDG video used in decision: {artist} - {title}")
```

---

## Conclusion

The current implementation is solid! These recommendations focus on:
- **Performance**: Caching, parallelization
- **Explainability**: Audit trails, visualization
- **Maintainability**: Configuration, testing
- **Robustness**: Rate limiting, retries, fallbacks

Start with caching and parallel checking for immediate 5-10x speed improvement.
