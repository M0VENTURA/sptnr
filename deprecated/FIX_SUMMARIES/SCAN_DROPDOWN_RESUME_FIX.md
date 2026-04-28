# Dashboard Scan Dropdown and Resume Fixes

## Changes Summary

This PR fixes two issues with the dashboard scan controls:

### 1. Dropdown Visibility Fix

**Problem**: Scan option dropdowns were being hidden behind other page elements.

**Solution**: 
- Increased dropdown z-index from 1050 to 1060
- Added `overflow: visible` to cards and card-body elements to prevent clipping
- Set `.btn-group` to `position: static` to ensure proper dropdown positioning

### 2. Resume (Forced) Option

**Problem**: Resume options only had a regular resume mode, but not a forced resume mode that combines resume functionality with force rescan.

**Solution**: Added "Resume (Forced)" option to all three scan types:
- **Navidrome Sync**: Resume from last scanned artist with force rescan enabled
- **Popularity & Singles**: Resume from last scanned artist with force rescan enabled
- **Combined Scan**: Resume from last scanned artist with force rescan enabled

### How Resume Works

#### Artist-Initiated Scans
When you scan an artist from the artist/album page:
1. The scan updates the artist's `last_scanned` timestamp in the database
2. This makes it available for dashboard resume functionality

#### Dashboard Resume
When you select "Resume from Last" on the dashboard:
1. `get_last_scanned_artist()` is called which:
   - First checks progress files for interrupted scans
   - Falls back to database to find the most recently scanned artist (by `last_scanned` timestamp)
2. The scan continues from that artist (or the next artist if using `resume_force`)

#### Resume vs Resume (Forced)
- **Resume from Last**: Continues from the last scanned artist, skipping recently updated items (respects `skip_days` settings)
- **Resume (Forced)**: Continues from the last scanned artist, but forces rescanning of all data regardless of when it was last updated

## Technical Details

### Files Modified

1. **templates/dashboard.html**
   - Added CSS for dropdown visibility fix
   - Added "Resume (Forced)" menu items to all three scan dropdowns

2. **app.py**
   - Updated `scan_popularity_route()` to handle `mode=resume_force`
   - Updated `scan_navidrome()` to handle `mode=resume_force`
   - Updated `scan_combined()` to handle `mode=resume_force`
   - Updated flash message mappings to include resume mode descriptions

3. **test_resume_force.py** (new)
   - Tests to verify resume_force mode correctly sets both force and resume flags
   - Tests for all three scan types
   - Tests for flash message descriptions

### Mode Logic

For all scan types, the mode parameter now supports:
- `all`: Full scan, skip recently updated
- `force`: Full scan, ignore last update times
- `missing`: Only scan items with missing data
- `resume`: Resume from last scanned artist, skip recently updated
- `resume_force`: Resume from last scanned artist, force rescan all data

The backend logic for `resume_force`:
```python
force_rescan = (mode == 'force' or mode == 'resume_force')
resume_mode = (mode == 'resume' or mode == 'resume_force')
```

## Testing

Run the test suite:
```bash
python3 test_resume_force.py -v
```

All tests should pass, verifying:
- Force flag is set correctly for each mode
- Resume flag is set correctly for each mode
- Flash messages are mapped correctly

## Future Enhancements

Potential improvements:
- Add progress indicators for resume operations showing which artist is being resumed from
- Add confirmation dialog for force operations to prevent accidental full rescans
- Add keyboard shortcuts for common scan operations
