# Discogs API Token Setup Guide

## Problem

Your Discogs API searches are returning no results because your config file has the placeholder token `"your_discogs_token"` instead of a real API token.

## Root Cause

The system has built-in validation to detect and reject placeholder Discogs tokens:

- Detects: "your_discogs_token", "your_token", "placeholder", or empty token
- When detected: Logs warning and disables Discogs single detection
- This prevents wasted API calls with invalid credentials

**Why Discogs appears to work but returns no results**: If a real-but-invalid token is used, the Discogs API returns HTTP 401 (Unauthorized). The error is caught and single detection returns False silently.

## Recent Improvements (Commit 878741d)

Enhanced error logging and validation:

1. **Placeholder Token Detection**: Now explicitly warns when a placeholder token prevents Discogs use

   ```
   ⚠ Discogs token appears to be a placeholder - Discogs single detection will be disabled
   ```

2. **HTTP Error Logging**: Shows clear error messages for auth failures

   ```
   ERROR: Discogs API authentication failed (401): invalid or expired token
   ```

3. **Debug Logging**: Full tracebacks available in debug mode to diagnose API issues

4. **Token Validation**: Checks that token is long enough (20+ characters) to be valid

## Solution: Get and Configure Your Discogs Token

### Step 1: Get Your Discogs API Token

1. Visit https://www.discogs.com/settings/developers
2. Log in to your Discogs account (create one if needed - it's free)
3. Click "Generate new token" in the Personal Access Tokens section
4. Copy the token that appears

### Step 2: Update Your Config

Edit `/config/config.yaml` and replace:

```yaml
discogs:
  enabled: true
  token: "your_discogs_token"
```

With:

```yaml
discogs:
  enabled: true
  token: "YOUR_ACTUAL_TOKEN_HERE"
```

Paste your actual token in place of `YOUR_ACTUAL_TOKEN_HERE`.

### Step 3: Verify the Fix

Restart the application and run a test scan. You should now see Discogs API queries working in the logs:

```
[DISCOGS] ✓ CONFIRMED as single - Track found in Discogs singles list
```

## How Discogs Helps

Once configured with a valid token, Discogs provides:

- **Single/EP Detection**: Identifies if a track is officially released as a single or EP
- **Music Video Confirmation**: Verifies if official music videos exist
- **Authority**: Uses Discogs' curated database of music releases (highest confidence source for singles)

The Discogs API calls are rate-limited to 0.35 seconds per request, so single detection scanning will be faster with a valid token.

## Troubleshooting

### Still seeing "No single found for..." in Discogs?

1. **Verify token is saved**: Restart the application after updating config
2. **Check token is valid**: Generate a new token at https://www.discogs.com/settings/developers if unsure
3. **Check logs for errors**: Look for HTTP errors in the debug output

### Rate limit errors?

The system automatically handles Discogs rate limiting with:
- 0.35 second minimum delay between requests
- Automatic retry on 429 responses
- Exponential backoff for server errors

If you see persistent rate limit errors, try running fewer parallel searches or reducing the batch size.

## API Rate Limits

- **Without token**: 60 requests/hour (very restrictive, effectively unusable)
- **With personal token**: 240 requests/hour (sufficient for most usage)
- **With commercial token**: Higher limits available (contact Discogs)

## References

- Discogs Developers: https://www.discogs.com/developers/
- API Documentation: https://www.discogs.com/developers/
- Personal Access Tokens: https://www.discogs.com/settings/developers
