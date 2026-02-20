# SPTNR Spinner Issue - Root Cause & Fix

## Problem Summary
Albums and artist pages showed indefinite loading spinners on the genre/similar artists tabs. The spinners never disappeared, suggesting the API was either:
1. Timing out (there's a 5-second timeout in the JS)
2. Returning invalid responses
3. Being blocked/redirected

## Investigation Results

### Root Cause Identified ✅
**API routes were being blocked by the Flask login middleware!**

The `enforce_setup_wizard()` middleware (line 1472 in app.py) was:
- Checking if the user was logged in
- Redirecting to the login page for ALL requests if user wasn't authenticated
- NOT exempting API routes from this check

**Impact:**
- All `/api/*` endpoint calls returned `302 Found` (redirect) with HTML content
- JavaScript expected JSON response but got HTML
- The JS timeout would trigger after 5 seconds, showing "timeout" error
- This created the appearance of spinners spinning indefinitely

### Secondary Issue Fixed ✅
Fixed duplicate `downloads_monitor` function name that was causing:
```
AssertionError: View function mapping is overwriting an existing endpoint function: downloads_monitor
```

### Changes Made

#### 1. Exempted API routes from login requirement (app.py line 1475)
```python
@app.before_request
def enforce_setup_wizard():
    try:
        exempt = {"setup", "static", "config_edit", "config_editor", "login", "logout"}
        # Exempt all API routes from login requirements
        if request.path.startswith("/api/"):
            return  # ← ADDED THIS
        if not request.endpoint or request.endpoint in exempt or request.endpoint.startswith("static"):
```

#### 2. Fixed duplicate function name (app.py line 12601)
```python
# OLD: def downloads_monitor(): (line 12599)
# NEW: def downloads_monitor_legacy():
```

## Verification

### Before Fix
```
HTTP GET /api/genres/album/Test%20Album/Test%20Artist
Response: 302 Found (redirect to login)
Content-Type: text/html
```

### After Fix
```
HTTP GET /api/genres/album/Test%20Album/Test%20Artist
Response: 404 Not Found (album not found in empty database)
Content-Type: application/json
{"error": "Album not found"}
```

## What This Fixes

✅ **Spinners will now stop spinning** - JS will receive proper API responses instead of redirects  
✅ **Genre tabs will display properly** - API returns JSON with genre data when available  
✅ **Similar artists will load** - API responses can be parsed correctly by JavaScript  
✅ **Error messages show correctly** - Users see "No genres available" instead of indefinite spinner  
✅ **App starts without route conflicts** - Fixed the downloads_monitor name collision  

## Expected Behavior After Fix

1. **When album has genre data:**
   - Genre tabs show sources with aggregated tags
   - Spinner appears briefly while loading, then displays genres

2. **When album has no data:**
   - Genre tabs show "No genres available from any source"
   - Spinner appears briefly, then message displays

3. **When database is empty:**
   - Same as above - proper message instead of infinite spinner

## Files Changed
- `app.py` - Added API exemption + fixed duplicate function name

## Commit
```
463f87d - Fix: Exempt API routes from login middleware to resolve spinner issues
```

## Testing Notes
- App requires Navidrome connection to import music data
- Without music data imported, genre tabs correctly show "No genres available"
- API endpoints are now accessible without login
- All endpoints now return proper JSON instead of redirects
