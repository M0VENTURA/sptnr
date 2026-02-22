# Configuration Examples for New Features

## Watcher Service Configuration

Add to your `config.yaml`:

```yaml
# Watcher Service Configuration
watcher:
  scan_interval: 30                 # Check for new files every 30 seconds
  navidrome_sync_wait: 600          # Wait 10 minutes for Navidrome scan
  auto_import_enabled: true         # Auto-import new songs from Navidrome
  auto_popularity_scan: true        # Auto-scan new songs for popularity
  downloads_watcher_enabled: true   # Monitor downloads folder
```

## Example: Disable Auto-Import

If you want to manually control imports:

```yaml
watcher:
  scan_interval: 60
  navidrome_sync_wait: 600
  auto_import_enabled: false        # Disable auto-import
  auto_popularity_scan: false       # Disable auto-scan
  downloads_watcher_enabled: true
```

## Example: Fast Scanning

For faster response times (uses more resources):

```yaml
watcher:
  scan_interval: 15                 # Check every 15 seconds
  navidrome_sync_wait: 300          # Wait only 5 minutes
  auto_import_enabled: true
  auto_popularity_scan: true
  downloads_watcher_enabled: true
```

## Example: Slow Scanning

For lower resource usage:

```yaml
watcher:
  scan_interval: 120                # Check every 2 minutes
  navidrome_sync_wait: 900          # Wait 15 minutes
  auto_import_enabled: true
  auto_popularity_scan: false       # Skip auto-scan to save resources
  downloads_watcher_enabled: true
```

## Using the Settings Page

You can also configure these settings through the web UI:

1. Navigate to **Settings** from the main menu
2. Scroll to the **Watcher Service** section
3. Adjust the settings:
   - **Scan Interval**: How often to check for changes (10-3600 seconds)
   - **Navidrome Sync Wait**: Time to wait for Navidrome to complete scanning
   - **Auto-Import New Songs**: Toggle automatic import
   - **Auto Popularity Scan**: Toggle automatic popularity scanning
   - **Downloads Watcher**: Toggle downloads folder monitoring
4. Click **Save Configuration**

## Environment Variables (Alternative)

You can also override settings with environment variables:

```bash
# Override scan interval
export WATCHER_SCAN_INTERVAL=45

# Start the watcher
python3 music_watcher.py
```

Note: Environment variables are overridden by config.yaml settings if present.
