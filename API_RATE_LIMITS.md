# API Rate Limits and Usage Guidelines

## Overview

SPTNR uses external APIs (Spotify and Last.fm) for fetching track metadata and popularity data. These APIs have rate limits to prevent abuse and ensure fair usage. This document explains the limits and how SPTNR manages them.

## Spotify Web API

### Rate Limits

- **Per-Second Limit**: ~8 requests per second (250 requests per 30 seconds using Client Credentials flow)
- **Daily Limit**: Conservative estimate of 500,000 requests per day
- **Official Documentation**: [Spotify Rate Limits](https://developer.spotify.com/documentation/web-api/concepts/rate-limits)

### SPTNR Implementation

SPTNR includes an API rate limiter that:
- Tracks API usage in a rolling 30-second window
- Tracks daily API usage with automatic reset at midnight
- Waits up to 5 seconds if rate limit is reached (then skips the request)
- Logs rate limit warnings to help you monitor usage

### Reducing Spotify API Calls

1. **Database Caching**: Spotify artist IDs are cached in the database. Subsequent scans reuse these IDs without making API calls.

2. **24-Hour Cache**: Track popularity scores are cached for 24 hours. Tracks scanned recently are skipped unless you use `--force` mode.

3. **Keyword Filtering**: Live, remix, and acoustic tracks are automatically excluded from Spotify lookups to save API calls.

### Typical Usage

For a library of 10,000 tracks:
- **First Scan**: ~10,000 track lookups + artist ID lookups = ~12,000 API calls
- **Rescan (same day)**: 0 API calls (cached)
- **Rescan (next day)**: ~10,000 track lookups (artist IDs cached) = ~10,000 API calls

At 8 requests/second, a full scan of 10,000 tracks takes ~20 minutes.

### Extended Quota Mode

If you need higher limits for production use, Spotify offers "Extended Quota Mode":
- [Request Extended Quota](https://developer.spotify.com/documentation/web-api/concepts/quota-modes)

## Last.fm API

### Rate Limits

- **Per-Second Limit**: ~1 request per second
- **Daily Limit**: Conservative estimate of 50,000 requests per day (not officially documented)
- **Official Documentation**: [Last.fm API ToS](https://www.last.fm/api/tos)

### SPTNR Implementation

SPTNR includes an API rate limiter that:
- Enforces 1-second delay between Last.fm requests
- Tracks daily API usage with automatic reset at midnight
- Waits up to 2 seconds if rate limit is reached (then skips the request)
- Logs rate limit warnings to help you monitor usage

### Reducing Last.fm API Calls

1. **Keyword Filtering**: Live, remix, and acoustic tracks are automatically excluded from Last.fm lookups.

2. **Combined with Spotify**: Last.fm lookups only happen if Spotify lookup succeeded (or was skipped intentionally).

### Typical Usage

For a library of 10,000 tracks:
- **First Scan**: ~10,000 track lookups = ~10,000 API calls
- **Rescan**: Last.fm data is not cached (API is cheap and data changes frequently)

At 1 request/second, a full scan of 10,000 tracks takes ~2.8 hours.

**Note**: If you're scanning a large library for 6+ hours and hitting rate limits, you may be approaching the estimated daily limit of 50,000 requests. Consider:
- Scanning in smaller batches (by artist or album)
- Using `--artist` filter to scan specific artists
- Spreading scans across multiple days

## Monitoring API Usage

### View Current Usage

The rate limiter stores usage statistics in `/database/api_rate_limiter_state.json`:

```json
{
  "spotify_daily_count": 12453,
  "lastfm_daily_count": 8721,
  "last_reset": "2026-01-18T00:00:00"
}
```

### Check Stats Programmatically

```python
from api_rate_limiter import get_rate_limiter

limiter = get_rate_limiter()
stats = limiter.get_stats()

print(f"Spotify: {stats['spotify_daily_count']}/{stats['spotify_daily_limit']} ({stats['spotify_daily_percent']:.1f}%)")
print(f"Last.fm: {stats['lastfm_daily_count']}/{stats['lastfm_daily_limit']} ({stats['lastfm_daily_percent']:.1f}%)")
```

## Best Practices

### For Large Libraries (10,000+ tracks)

1. **First Scan**:
   - Run during off-peak hours
   - Use `--verbose` to monitor progress
   - Expect 3-6 hours for combined Spotify + Last.fm lookups

2. **Regular Scans**:
   - Daily scans will use 24-hour cache (fast)
   - Force rescans only when needed (`--force`)

3. **Filter by Artist**:
   ```bash
   python popularity.py --artist "Queen" --verbose
   ```

4. **Filter by Album**:
   ```bash
   python popularity.py --artist "Queen" --album "A Night at the Opera" --verbose
   ```

### Avoiding Rate Limits

1. **Don't run multiple parallel scans** - The rate limiter is per-process, not global.

2. **Use the 24-hour cache** - Avoid `--force` unless necessary.

3. **Monitor the logs** - Watch for rate limit warnings:
   - `⏸️ Spotify rate limit: ...`
   - `⏸️ Last.fm rate limit: ...`

4. **Spread large scans across days** - For 50,000+ track libraries, consider scanning 10-20k tracks per day.

## Improved Last.fm Scoring

### Old Algorithm (Issue)

```python
lastfm_score = min(100, playcount // 100)
```

**Problem**: Any song with 10,000+ plays got capped at 100 points.

### New Algorithm (Fixed)

```python
lastfm_score = 12.5 * log10(playcount)
```

**Benefits**:
- 100 plays → 25 points
- 1,000 plays → 37.5 points
- 10,000 plays → 50 points ✅ (was 100, capped)
- 100,000 plays → 62.5 points ✅ (was 100, capped)
- 1,000,000 plays → 75 points ✅ (was 100, capped)
- 10,000,000 plays → 87.5 points ✅ (was 100, capped)

This logarithmic scale better represents the relative popularity of tracks and prevents popular songs from all getting the same score.

## Live Album Detection

### Issue

Songs from live albums (e.g., "Alice in Chains - Unplugged") were being matched with their studio versions on Spotify, resulting in incorrect popularity scores and metadata.

### Solution

SPTNR now detects live/unplugged/acoustic albums and includes album context in Spotify searches:

```python
is_live_album = is_live_or_alternate_album(album)
# Returns True for albums like:
# - "MTV Unplugged"
# - "Live at Budokan"
# - "Acoustic Sessions"
# - "In Concert"
```

When a live album is detected:
1. Log message: `📻 Detected live/unplugged album`
2. Album name is included in Spotify search
3. Prevents matching with studio versions
4. More accurate popularity and metadata

## Configuration

### Environment Variables

```bash
# Override API timeout (default: 30 seconds)
export POPULARITY_API_TIMEOUT=45

# Enable verbose logging
export SPTNR_VERBOSE_POPULARITY=1

# Force rescan all albums
export SPTNR_FORCE_RESCAN=1
```

### Command Line

```bash
# Scan with verbose logging
python popularity.py --verbose

# Force rescan (ignore 24-hour cache)
python popularity.py --force

# Scan specific artist
python popularity.py --artist "Pink Floyd" --verbose

# Scan specific album
python popularity.py --artist "Pink Floyd" --album "The Dark Side of the Moon" --verbose
```

## Troubleshooting

### Issue: "Spotify rate limit: 250/250 requests in 30s"

**Cause**: You've hit the Spotify rate limit.

**Solution**: 
- Wait 30 seconds or let the rate limiter handle it automatically
- The scan will resume after the wait period
- Consider using `--artist` filter for incremental scans

### Issue: "Daily Spotify API limit reached (500000 requests/day)"

**Cause**: You've exceeded the estimated daily limit.

**Solution**:
- Wait until midnight (automatic reset)
- This is extremely rare - indicates a bug or misconfiguration
- Check for infinite loops or duplicate scans

### Issue: "Last.fm lookup timed out after 30s"

**Cause**: Last.fm API is slow or unresponsive.

**Solution**:
- Increase timeout: `export POPULARITY_API_TIMEOUT=60`
- Check Last.fm service status: https://www.last.fm/
- The scan will continue without Last.fm data for timed-out tracks

### Issue: Every Last.fm score is 100

**Cause**: Using old scoring algorithm (fixed in this update).

**Solution**:
- Update to latest version with logarithmic scoring
- Force rescan: `python popularity.py --force --verbose`

## API Keys

### Spotify

1. Create app at: https://developer.spotify.com/dashboard
2. Get Client ID and Client Secret
3. Add to config.yaml:
   ```yaml
   api_integrations:
     spotify:
       enabled: true
       client_id: "your_client_id"
       client_secret: "your_client_secret"
   ```

### Last.fm

1. Create API account at: https://www.last.fm/api/account/create
2. Get API key
3. Add to config.yaml:
   ```yaml
   api_integrations:
     lastfm:
       api_key: "your_api_key"
   ```

## Support

For issues or questions:
- GitHub Issues: https://github.com/M0VENTURA/sptnr/issues
- Check logs in `/database/` directory
- Enable `--verbose` for detailed debugging
