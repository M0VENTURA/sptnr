# Fix: Stuck Beets Scan Detection

## Problem
The application was stuck detecting a beets scan as running even when nothing was actually moving. This prevented users from initiating a full scan.

### Root Cause
When a beets scan crashed, timed out, or completed abnormally, the progress file (`mp3_scan_progress.json`) could remain with `is_running: true` even though no actual scan was running. The system relied solely on this file to determine scan status, creating a false "scan is running" state.

## Solution

### 1. Progress File Validation (`_validate_and_cleanup_progress_file`)
Added a new validation function that:
- Cross-checks the progress file status with the actual thread/process status
- Detects dead threads and automatically updates progress files
- Implements timeout detection (2 hours max) for truly stuck scans
- Automatically cleans up stale progress files

### 2. Enhanced Scan Progress Endpoint (`/api/scan-progress`)
Updated to validate all progress files before returning status:
- MP3 scan (beets)
- Navidrome sync
- Popularity scan
- Singles scan
- Missing releases scan

### 3. Improved Scan Initiation (`/scan/mp3`)
Before starting a new beets scan:
- Cleans up any stale progress files
- Checks if the process is actually alive (not just the file status)
- Prevents false "already running" errors

### 4. Manual Cleanup Feature (`/scan/clear-stuck`)
Added a new endpoint and UI button to manually clear stuck scans:
- Endpoint: `POST /scan/clear-stuck`
- UI: "Clear Stuck Scans" button on the dashboard
- Allows users to recover from edge cases
- Clears all stuck progress files and dead process references

## How It Works

### Automatic Detection
1. **Age Check**: If a progress file says "running" but hasn't been modified in 2+ hours, it's marked as stuck
2. **Process Check**: If a progress file says "running" but the thread/process is dead, it's marked as stuck
3. **Cleanup**: Stuck scans are automatically marked as `is_running: false` with status `error` or `timeout`

### Manual Recovery
If automatic detection doesn't work or you want to force cleanup:
1. Go to the Dashboard
2. Scroll to the "Scan Operations" section
3. Click "Clear Stuck Scans" button at the bottom

## Testing
- Unit tests verify all validation logic
- All tests pass successfully
- CodeQL security scan: no alerts
- Code review: all feedback addressed

## Files Changed
- `app.py`: Added validation, cleanup, and manual clear endpoint
- `templates/dashboard.html`: Added "Clear Stuck Scans" button
- `test_stuck_scan_fix.py`: Unit tests for validation logic

## Result
✅ Users can now initiate full scans even if a previous beets scan got stuck
✅ Automatic cleanup prevents false "scan is running" states
✅ Manual cleanup button provides easy recovery option
