# ListenBrainz API - Configuration & Troubleshooting

## Quick Answer

**Q: Does the ListenBrainz API need an API key for popularity data?**

**A: NO** - The public global statistics endpoint requires no API key or authentication. However, the endpoint was recently changed, which is why you're getting `listenbrainz: 0` in the popularity scan results.

---

## The Issue

The code was using the ListenBrainz API v1 endpoint (`https://api.listenbrainz.org/1`), which now returns **410 Gone**, indicating it's deprecated. The current API is v0 (`https://api.listenbrainz.org/0`).

### What We Fixed

1. ✅ Updated API endpoint from `/1` to `/0` 
2. ✅ Added better error logging to identify API issues
3. ✅ Added support for optional user tokens (if you want personal stats)
4. ✅ Improved error handling for different HTTP status codes

---

## ListenBrainz API Details

### Public Global Statistics (NO AUTH)
```
GET https://api.listenbrainz.org/0/stats/recording/{MBID}
```
- Returns global listen count across all ListenBrainz users
- **No authentication required**
- What we use for popularity scoring
- Response format:
```json
{
  "payload": {
    "total_listen_count": 12345
  }
}
```

### User Personal Statistics (REQUIRES TOKEN)
If you want to get YOUR personal listen stats for tracks:
```
GET https://api.listenbrainz.org/0/user/{username}/listen-count
Authorization: Token YOUR_USER_TOKEN
```

### Configuration Options

**Minimal (global stats only):**
```yaml
api_integrations:
  listenbrainz:
    enabled: true
```

**With personal stats (optional):**
```yaml
api_integrations:
  listenbrainz:
    enabled: true
    token: "YOUR_LISTENBRIAN

Z_USER_TOKEN"

navidrome_users:
  - user: "your_username"
    listenbrainz_user_token: "YOUR_USER_TOKEN"
```

To get your ListenBrainz token:
1. Go to https://listenbrainz.org/settings/
2. Look for "API Token" section
3. Copy your token

---

## What Changed

### Before
- Used API v1: `https://api.listenbrainz.org/1/stats/recording/{MBID}`
- Would fail silently, returning 0
- No clear error messages

### After
- Uses API v0: `https://api.listenbrainz.org/0/stats/recording/{MBID}`
- Better error logging for debugging
- Handles different HTTP status codes (404, 410, 5xx, etc.)
- Support for optional user authentication

---

## Results

After applying this fix, the popularity scan should now:
- ✅ Successfully fetch ListenBrainz listen counts
- ✅ Show non-zero `listenbrainz: X.XX` values in the weighted popularity calculation
- ✅ Provide clear debug logs if there are still issues

Example before fix:
```
Weighted popularity calculation - spotify: 4, lastfm: 39.04, listenbrainz: 0, age: 0, final: 19.0
```

Example after fix (if data exists):
```
Weighted popularity calculation - spotify: 4, lastfm: 39.04, listenbrainz: 22.5, age: 0, final: 28.7
```

---

## Debugging

If ListenBrainz scores are still 0 after the fix:

1. **Check if tracks have MBIDs:**
   ```sql
   SELECT COUNT(*), COUNT(DISTINCT CASE WHEN mbid IS NOT NULL THEN 1 END)
   FROM tracks;
   ```
   If the two counts are very different, many tracks lack MBIDs and can't be looked up.

2. **Check debug logs for ListenBrainz errors:**
   ```
   grep -i "listenbrainz" logs/unified.log
   ```

3. **Test the API manually:**
   ```bash
   # With a valid MBID
   curl "https://api.listenbrainz.org/0/stats/recording/bf93b326-4f95-4d13-b644-306e8b68ccaa"
   ```

4. **Verify config is loaded:**
   ```python
   from popularity_helpers import _listenbrainz_enabled, configure_popularity_helpers
   configure_popularity_helpers()
   print(f"ListenBrainz Enabled: {_listenbrainz_enabled}")
   ```

---

## Reference

- **ListenBrainz API Docs:** https://listenbrainz.readthedocs.io/en/production/dev/api/
- **ListenBrainz Settings:** https://listenbrainz.org/settings/
- **MusicBrainz Recording IDs:** Format is UUIDs like `bf93b326-4f95-4d13-b644-306e8b68ccaa`
