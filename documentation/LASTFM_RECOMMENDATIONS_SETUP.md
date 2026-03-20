# Last.fm Recommendations Setup & Troubleshooting Guide

## Overview
Last.fm recommendations in the playlist creator fetch personalized song/artist/album recommendations from your Last.fm account and match them against your local music library.

## Prerequisites

### 1. Last.fm Account
- Create a free account at https://www.last.fm
- Have some scrobbling history (listen to music and scrobble to Last.fm)
- *Note:* New accounts or accounts with no recent scrobbles may not have recommendations available

### 2. Last.fm API Key
Get your Last.fm API key:
1. Go to https://www.last.fm/api/account/create
2. Log in with your Last.fm account
3. Fill in the form (Application name, Description, etc.)
4. Accept terms and submit
5. Copy your **API Key** (not the shared secret)

### 3. sptnr Configuration
Update your `config.yaml`:

```yaml
api_integrations:
  lastfm:
    enabled: true
    api_key: "YOUR_API_KEY_HERE"  # Paste your API key here
```

### 4. (Optional) Per-User Last.fm Username
For personalized recommendations, configure your Last.fm username in the Navidrome users list:

```yaml
navidrome_users:
  - user: "your_navidrome_user"
    lastfm_username: "your_lastfm_username"  # Add this line
```

**What this does:**
- **With username:** Uses Last.fm's `user.getRecommendedTracks` API for your personalized recommendations
- **Without username:** Falls back to general Last.fm charts

## Setup Checklist

- [ ] Created Last.fm account
- [ ] Have some scrobbling history on Last.fm
- [ ] Generated Last.fm API key
- [ ] Added API key to `config.yaml` under `api_integrations.lastfm.api_key`
- [ ] Set `api_integrations.lastfm.enabled = true`
- [ ] Restarted sptnr application
- [ ] (Optional) Added Last.fm username to your Navidrome user config

## Testing Your Setup

### 1. Verify Configuration
Check that the API is enabled and configured:
```bash
# Check if Last.fm is enabled in logs
tail -f logs/*  # Look for "Last.fm" mentions
```

### 2. Test the Playlist Creator
1. Go to `/playlists/create?type=lastfm`
2. A "Requirements" info box should appear
3. Select "Top Tracks" from the dropdown
4. Wait for recommendations to load

### 3. Debug with Browser Console
If recommendations don't load:
1. Open your browser's Developer Tools (F12)
2. Go to the **Console** tab
3. Look for messages starting with `[Last.fm Playlist]`
4. Check the **Network** tab for the `/api/lastfm/create-playlist` request

### Example Console Output (Success)
```
[Last.fm Playlist] Loading tracks recommendations...
[Last.fm Playlist] API Response status: 200 OK
[Last.fm Playlist] API Data: {total_recommendations: 15, matched: 8, missing: 7, ...}
```

### Example Console Output (Config Issue)
```
[Last.fm Playlist] Loading tracks recommendations...
[Last.fm Playlist] API Response status: 400 Bad Request
[Last.fm Playlist] API Error: {error: "Last.fm API key not configured"}
```

## Common Issues & Solutions

### Issue: "No ... recommendations found" (404 Error)

**Possible causes:**
1. **Last.fm account has no scrobbling history**
   - Solution: Listen to music and ensure it's scrobbling to Last.fm
   - Check: https://www.last.fm/user/YOUR_USERNAME/library/tracks

2. **Last.fm API key is invalid or expired**
   - Solution: Get a new API key from https://www.last.fm/api/account/create
   - Re-add it to `config.yaml`

3. **API key not saved properly**
   - Solution: Verify it's in `config.yaml` exactly as:
     ```yaml
     api_integrations:
       lastfm:
         enabled: true
         api_key: "YOUR_API_KEY_HERE"
     ```
   - Restart sptnr after editing config

4. **Last.fm API is temporarily unavailable**
   - Solution: Wait a few minutes and try again
   - Check: https://www.last.fm/ to see if the site is up

### Issue: API Response Shows "Invalid API Key"

**Solution:**
1. Go to https://www.last.fm/api/account/create
2. Generate a new API key
3. Update `config.yaml`
4. Restart sptnr

### Issue: Browser Console Shows Error But No Details

**Solution:**
1. Open `/playlists/create?type=lastfm`
2. Open Browser DevTools (F12) → Console tab
3. Select "Top Tracks" and wait
4. Look for `[Last.fm Playlist]` messages
5. Share the console output in a bug report if it still doesn't work

### Issue: Form Won't Load at All

**Solution:**
1. Check that `/playlists/create` page loads without error
2. Check browser console (F12) → Console tab for any JavaScript errors
3. Verify Last.fm is enabled in `config.yaml`
4. Verify you're logged into sptnr (not the login page)

## How It Works

### Data Flow
```
1. User selects recommendation type (Tracks, Artists, or Albums)
   ↓
2. JavaScript calls /api/lastfm/create-playlist endpoint
   ↓
3. Backend fetches recommendations from Last.fm API
   - Uses user.getRecommendedTracks (if username configured)
   - Falls back to chart.getTopTracks (if no username)
   ↓
4. Backend searches local music library for matching tracks
   - For Tracks: Matches by artist name + track title
   - For Albums: Matches by artist name + album name
   - For Artists: Finds 5 tracks by each recommended artist
   ↓
5. Results split into "Matched" and "Missing"
   ↓
6. Frontend displays results and allows creating playlist
```

### Matched vs. Missing
- **Matched**: Tracks found in your local library (can be added to playlist)
- **Missing**: Tracks from recommendations NOT in your library (can't add, but shows for reference)

## Advanced Configuration

### Retry Logic
The Last.fm client has built-in retry logic:
- 3 automatic retries per API request
- Exponential backoff (wait longer between retries)
- Logs all retry attempts

### Caching
- Recommendations are fetched fresh each time you change the dropdown
- No caching between requests to always show latest data

### Rate Limiting
Last.fm API rate limits:
- 5 requests per second per IP
- 120 requests per 5 minutes per IP
- sptnr respects these limits and won't exceed them

## Reporting Issues

If Last.fm recommendations still don't work:

1. **Gather information:**
   - Browser console output (F12 → Console)
   - sptnr server logs
   - Your `config.yaml` (without API key!)
   - Steps to reproduce

2. **Check your setup:**
   - Is Last.fm API key valid? (Test at https://www.last.fm/api)
   - Does your Last.fm account have scrobbles? (Check history)
   - Is sptnr restarted after config changes?

3. **Common issues checklist:**
   - [ ] API key is correct
   - [ ] API key is in `config.yaml` (not commented out)
   - [ ] Last.fm account exists and has scrobbling history
   - [ ] sptnr has been restarted after config changes
   - [ ] No firewall blocking requests to api.last.fm
   - [ ] Browser console shows full error (not generic message)

## Testing Without Configuration

If you want to test the UI without having Last.fm set up:
1. The form will show "Last.fm not enabled" message
2. Once configured, just select a recommendation type to load data
3. No manual configuration needed after initial setup

## Related Documentation
- Last.fm API Docs: https://www.last.fm/api
- Navidrome Docs: https://www.navidrome.org/
- sptnr GitHub: Check README.md for more features
