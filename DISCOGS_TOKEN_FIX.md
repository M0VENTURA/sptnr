# Discogs Singles Detection Fix

## Issue
Singles detection was not correctly calling the Discogs API, causing songs that are definitely singles on Discogs to not be detected.

## Root Cause
The `_get_discogs_client()` function in `api_clients/discogs.py` created a singleton `DiscogsClient` instance. The bug was that once created with an initial token value, the client would never update its token even when subsequent calls provided a different token.

### The Problem Flow
1. First call to `is_discogs_single()` or `has_discogs_video()` happens with an empty token (or wrong token)
2. `_get_discogs_client('')` creates a `DiscogsClient` with empty token
3. Subsequent calls with a valid token: `is_discogs_single('Song', 'Artist', None, token='valid_token')`
4. `_get_discogs_client('valid_token')` checks `if _discogs_client is None` → False (already exists)
5. Returns the existing client with **empty token** instead of the valid one
6. Discogs API calls fail with authentication errors
7. Singles are not detected

## The Fix
Changed the condition in `_get_discogs_client()` from:
```python
if _discogs_client is None:
    _discogs_client = DiscogsClient(token, enabled=enabled)
```

To:
```python
if _discogs_client is None or _discogs_client.token != token:
    _discogs_client = DiscogsClient(token, enabled=enabled)
```

This ensures:
- Client is created on first use
- Client is **recreated** when the token changes
- Client is reused (for caching benefits) when the token hasn't changed

## Impact
This fix resolves singles detection failures for:
- `is_discogs_single()` - Checks if a track is released as a single on Discogs
- `has_discogs_video()` - Checks if a track has an official music video on Discogs

Both functions now correctly use the provided token instead of a cached empty/wrong token.

## Testing
Added `test_discogs_token_fix.py` which validates:
1. Client created with empty token
2. Client updated when called with different token
3. Client reused when token unchanged
4. Wrapper functions (`is_discogs_single`, `has_discogs_video`) correctly pass and update tokens

Run the test:
```bash
python3 test_discogs_token_fix.py
```

Expected output:
```
Testing Discogs client token update mechanism...
✓ Test 1: Client created with empty token
✓ Test 2: Client token updated to 'new_token_abc123'
✓ Test 3: Client reused when token unchanged
✓ Test 4: Wrapper function correctly passes token to client

✅ All tests passed! Token update mechanism working correctly.

Testing has_discogs_video token update...
✓ has_discogs_video correctly updates client token

============================================================
ALL TESTS PASSED
============================================================
```

## Verification
To verify this fix resolves the singles detection issue:

1. **Set your Discogs token** in your environment:
   ```bash
   export DISCOGS_TOKEN="your_discogs_token_here"
   ```

2. **Test with a known single** (example: "+44 - When Your Heart Stops Beating"):
   ```python
   from api_clients.discogs import is_discogs_single
   import os
   
   token = os.getenv('DISCOGS_TOKEN')
   result = is_discogs_single(
       title='When Your Heart Stops Beating',
       artist='+44',
       album_context=None,
       token=token
   )
   print(f"Is single: {result}")  # Should be True
   ```

3. **Check the logs** for Discogs API calls:
   - Look for "Discogs single detected" or "Discogs single not detected" messages
   - Verify there are no authentication errors
   - Confirm API calls are being made (with rate limiting: 1 per 0.35 seconds)

## Files Changed
- `api_clients/discogs.py` - Fixed `_get_discogs_client()` function (1 line changed)
- `test_discogs_token_fix.py` - New test file to validate the fix (70 lines)

## Security Review
✅ No security issues found by CodeQL analysis

## Code Review
✅ Addressed all review feedback:
- Used `is` instead of `==` for boolean False checks
- Added comprehensive test coverage
- Minimal change with clear intent

## Summary
This fix resolves the singles detection issue by ensuring the Discogs API client always uses the correct authentication token. The change is minimal (1 line), well-tested, and has no security concerns.
