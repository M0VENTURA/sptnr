# Dashboard Visual Mockup

## Unified Log Section (Updated)

```
┌──────────────────────────────────────────────────────────────────┐
│ 🖥️  Unified Log                                      [⏸️ Pause]  │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  2026-01-18 02:26:16 [INFO] Navidrome: Scanning The Beatles     │
│  2026-01-18 02:26:16 [INFO]   Album 1/12: Abbey Road            │
│  2026-01-18 02:26:16 [INFO]     ✓ Imported 17 tracks            │
│  2026-01-18 02:26:16 [INFO] Navidrome: Completed The Beatles    │
│  2026-01-18 02:26:16 [INFO] Popularity: Scan started            │
│  2026-01-18 02:26:16 [INFO] Popularity: Processing The Beatles  │
│  2026-01-18 02:26:16 [INFO] Popularity: Completed - 204 tracks  │
│  2026-01-18 02:26:17 [INFO] Single: Detected 45 singles         │
│                                                                  │
├──────────────────────────────────────────────────────────────────┤
│                                               [Download Buttons] │
│                          [📥 Unified (1h)] [📥 Info (1h)]       │
│                                              [📥 Debug (1h)]     │
└──────────────────────────────────────────────────────────────────┘
```

## Button Descriptions

### 📥 Unified (1h) - Blue/Primary
- Downloads: `unified_scan.log` (last hour)
- Contains: Basic operational status
- Best for: Quick overview, dashboard viewing
- File size: Smallest (~10-50 KB)

### 📥 Info (1h) - Cyan/Info
- Downloads: `info.log` (last hour)
- Contains: Detailed operations, API calls
- Best for: Troubleshooting operations
- File size: Medium (~100-500 KB)

### 📥 Debug (1h) - Yellow/Warning
- Downloads: `debug.log` (last hour)
- Contains: Verbose debug info, stack traces
- Best for: Deep troubleshooting, bug reports
- File size: Largest (~500 KB - 2 MB)

## Example Downloaded File

**Filename**: `unified_log_20260118_023000.txt`

**Content**:
```
2026-01-18 02:26:16,251 [INFO] Navidrome: Scanning The Beatles (12 albums)
2026-01-18 02:26:16,351 [INFO]   Album 1/12: Abbey Road
2026-01-18 02:26:16,452 [INFO]     ✓ Imported 17 tracks from Abbey Road
2026-01-18 02:26:16,552 [INFO] Navidrome: Completed The Beatles - 12 albums, 204 tracks
2026-01-18 02:26:16,652 [INFO] Popularity: Scan started at 02:30:45
2026-01-18 02:26:16,752 [INFO] Popularity: Processing The Beatles
2026-01-18 02:26:16,853 [INFO] Popularity: Completed The Beatles - 204 tracks rated
2026-01-18 02:26:17,100 [INFO] Single: Detected 45 singles
```

## User Workflow

### Viewing Logs in Real-Time
1. Navigate to Dashboard
2. Scroll to "Unified Log" section
3. See live updates as operations occur
4. Click "Pause" button to stop auto-refresh if needed

### Downloading Logs for Support
1. Navigate to Dashboard
2. Scroll to "Unified Log" section
3. Click appropriate download button:
   - Quick check? → "Unified (1h)"
   - Need details? → "Info (1h)"
   - Deep debug? → "Debug (1h)"
4. File downloads automatically
5. Share with support team or review locally

### Troubleshooting Workflow
```
┌─────────────────────────────────────────────┐
│ Problem: Artist not scanning properly       │
└─────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────┐
│ Step 1: Check unified log on dashboard     │
│ → See: "Navidrome: Scanning Artist X"      │
│ → Status: Appears to be working            │
└─────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────┐
│ Step 2: Download "Info (1h)" log           │
│ → See detailed API calls and responses     │
│ → Found: API timeout on album fetch        │
└─────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────┐
│ Step 3: Download "Debug (1h)" log          │
│ → See verbose API request/response details │
│ → Found: Specific error message & trace    │
└─────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────┐
│ Resolution: Identified network timeout      │
│ Action: Adjust timeout config or retry     │
└─────────────────────────────────────────────┘
```

## Log File Comparison

| Feature | Unified | Info | Debug |
|---------|---------|------|-------|
| Dashboard view | ✅ Yes | ❌ No | ❌ No |
| HTTP requests | ❌ Filtered | ✅ Yes | ✅ Yes |
| Debug messages | ❌ Filtered | ❌ No | ✅ Yes |
| Service prefix | ❌ No | ✅ Yes | ✅ Yes |
| Stack traces | ❌ No | ❌ No | ✅ Yes |
| API responses | ❌ No | ⚠️ Summary | ✅ Full |
| File size | Small | Medium | Large |
| Best for | Overview | Operations | Debugging |

## Color Scheme (Bootstrap)

```
Unified Button:  btn-outline-primary  (Blue)
Info Button:     btn-outline-info     (Cyan)
Debug Button:    btn-outline-warning  (Yellow/Orange)
```

Visual hierarchy: Primary → Info → Warning
Indicates increasing verbosity/detail level
