# ListenBrainz API Fix - Summary

## Problem Identified

Your popularity scans were showing `listenbrainz: 0` because the ListenBrainz API endpoint was deprecated.

**Original code:**
```python
self.base_url = "https://api.listenbrainz.org/1"  # ❌ Returns 410 Gone
```

**Root cause:** The ListenBrainz API v1 endpoint has been retired and now returns HTTP 410 (Gone).

---

## Solution Applied

### 1. Updated API Endpoint Version
**File:** `api_clients/audiodb_and_listenbrainz.py`

Changed API from v1 to v0 (current version):
```python
self.base_url = "https://api.listenbrainz.org/0"  # ✅ Current API
```

### 2. Enhanced Error Handling
Added specific handling for different error conditions:
```python
if res.status_code == 404:
    logger.debug(f"MBID not found in ListenBrainz (404)")
    return 0
elif res.status_code == 410:
    logger.warning(f"ListenBrainz API endpoint is gone (410)")
    return 0
elif res.status_code >= 400:
    logger.debug(f"ListenBrainz API error {res.status_code}")
    return 0
```

### 3. Added User Token Support
**File:** `api_clients/audiodb_and_listenbrainz.py`

The ListenBrainzClient now accepts an optional `user_token`:
```python
def __init__(self, http_session=None, enabled: bool = True, user_token: str = ""):
    self.user_token = user_token
    if user_token:
        self.headers["Authorization"] = f"Token {user_token}"
```

**File:** `popularity_helpers.py`

Now reads token from config:
```python
listenbrainz_token = listenbrainz_cfg.get("token", "")
_listenbrainz_client = ListenBrainzClient(
    enabled=_listenbrainz_enabled, 
    user_token=listenbrainz_token
)
```

### 4. Better Logging
Added detailed debug logging:
```python
logger.debug(f"ListenBrainz: Fetching stats for MBID {mbid} from {url}")
```

---

## Configuration (No Changes Required)

Your existing config will work with the fix:

```yaml
api_integrations:
  listenbrainz:
    enabled: true  # Uses public global stats (no auth needed)
```

**Optional: If you want personal stats,** add your token:
```yaml
api_integrations:
  listenbrainz:
    enabled: true
    token: "your_listenbrainz_user_token"  # Optional
```

To get your ListenBrainz token: https://listenbrainz.org/settings/

---

## Expected Results After Fix

### Before Fix
```
[DEBUG] Weighted popularity calculation - spotify: 4, lastfm: 39.04, listenbrainz: 0, age: 0, final: 19.0
```

### After Fix (with valid MBIDs)
```
[DEBUG] Weighted popularity calculation - spotify: 4, lastfm: 39.04, listenbrainz: 22.5, age: 0, final: 28.7
```

---

## Verification

To verify the fix is working:

1. **Run the test script:**
   ```bash
   python test_listenbrainz_api.py
   ```

2. **Check the logs during popularity scan:**
   ```bash
   grep -i "listenbrainz" logs/unified.log
   ```
   
   You should see:
   - ✅ Successful fetches: `ListenBrainz count for 'Song Title': 12345`
   - ✅ Debug logs showing API calls being made
   - ✅ No 410 errors anymore

3. **Run a popularity scan:**
   ```bash
   python start.py --scan popularity --artist "Test Artist"
   ```

---

## FAQ

**Q: Does ListenBrainz popularity data require an API key?**
A: NO. The public endpoint requires no authentication. An optional token is only for personal/premium features.

**Q: Why Is listenbrainz still 0 after the fix?**
A: Possible reasons:
- Your tracks don't have MBIDs in the database (need to re-scan or import)
- The tracks genuinely have 0 listens on ListenBrainz
- API connectivity issues (check logs)

**Q: Do I need to do anything in my config?**
A: NO. The fix is automatic. Just run your scans again and it should work.

---

## Files Modified

1. ✅ `api_clients/audiodb_and_listenbrainz.py` - Updated API endpoint from v1 to v0
2. ✅ `popularity_helpers.py` - Added token support
3. 📄 `LISTENBRAINZ_API_FIX.md` - Detailed documentation
4. 📝 `test_listenbrainz_api.py` - Test script to diagnose issues

---

## Next Steps

1. Run popularity scans again
2. Check logs for "ListenBrainz" debug messages
3. Verify `listenbrainz` values are non-zero in the weighted popularity calculation
4. If still having issues, run `test_listenbrainz_api.py` for detailed diagnostics
