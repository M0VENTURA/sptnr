# Single Manual Override Feature

## Overview

This feature prevents single detection scans from overwriting user-manually-set single status on tracks. When a user marks a song as a single (or not a single) via the UI, that setting is preserved across future scans and won't be automatically changed by detection algorithms.

## Problem Solved

Previously, when users manually marked/unmarked tracks as singles in the UI, an automated single detection scan would automatically overwrite those changes if the detection algorithm disagreed. This led to frustration when:
- Users corrected mis-detected singles only to have them reverted on next scan
- High-confidence manual settings were lost to algorithm changes
- No way to permanently mark songs as manually set

## Solution Architecture

### 1. Database Schema Addition

New field added to `tracks` table:
```python
"single_manual_override": "INTEGER",  # 1 if user manually set is_single (skip auto-detection)
```

- **Type:** INTEGER (0 or 1, NULL represents 0)
- **Purpose:** Flag that indicates a track's single status was set manually
- **When Set:** Automatically set to 1 when user edits `is_single` field via track edit form
- **Effect:** Single detection scans skip processing this track

### 2. Single Detection Logic Update

Modified `batch_update_advanced_singles()` in `advanced_single_detection.py`:

```python
# Build query with filters
where_clauses = ["single_manual_override IS NULL OR single_manual_override = 0"]
params = []
```

The detection batch update now:
- Includes `single_manual_override IS NULL OR single_manual_override = 0` in WHERE clause
- Skips any track where `single_manual_override = 1`
- Only processes auto-detectable singles, leaving manual overrides untouched

### 3. Automatic Flagging on Edit

Track edit form (`/track/<track_id>/edit`) now sets the flag:

```python
cursor.execute("""
    UPDATE tracks
    SET title = ?, ..., is_single = ?, ..., single_manual_override = 1
    WHERE id = ?
""", (...))
```

Whenever user changes `is_single` field via the form, `single_manual_override = 1` is set automatically.

### 4. UI Indicators

Added visual badge in `templates/track.html`:

```html
<label class="form-check-label" for="is_single">
    <i class="bi bi-single-music"></i> Is Single
    {% if track.single_manual_override == 1 %}
    <span class="badge bg-warning text-dark ms-2" 
          title="Manually set - single detection scan will skip this track">
        Manual
    </span>
    {% endif %}
</label>
```

- Shows `Manual` badge when track is manually overridden
- Tooltip explains: "Manually set - single detection scan will skip this track"
- Helps users visually identify which singles are protected from auto-updates

### 5. API Endpoint for Toggle Control

New endpoint added to `app.py`:

```
POST /api/track/<track_id>/toggle-manual-single
```

**Purpose:** Allow users to toggle the manual override flag without full edit

**Response:**
```json
{
  "success": true,
  "track_id": "track123",
  "single_manual_override": 1,
  "message": "Single manual override enabled - single detection scan will skip this track"
}
```

**Usage:** Can be called via AJAX to toggle flag without page navigation

## Workflow Examples

### Scenario 1: User Manually Marks Song as Single

1. User finds track incorrectly marked as "Not Single"
2. User edits track, checks "Is Single" checkbox
3. User saves form
4. `single_manual_override = 1` is automatically set
5. Badge shows "Manual" on track page
6. Next scan runs → Track is skipped by single detection, value preserved

### Scenario 2: User Corrects a High-Confidence Single Detection

1. Detection algorithm marks song as single (5-star confidence)
2. User listens, disagrees (thinks it's not a single)
3. User unchecks "Is Single" and saves
4. `single_manual_override = 1` is set
5. Badge shows "Manual"
6. Future scans won't re-mark it as single

### Scenario 3: User Undoes Manual Override

1. User has a track with manual override (badge shows "Manual")
2. User realizes mistake, wants detection to run again
3. User calls API: `POST /api/track/<track_id>/toggle-manual-single`
4. Flag toggles to 0
5. Badge disappears
6. Next scan will process this track normally

## Key Benefits

✅ **Preserves User Intent:** Manually set singles won't be overwritten by scans  
✅ **Clear Visibility:** "Manual" badge shows which singles are protected  
✅ **Flexible Control:** API endpoint allows toggling without full form edit  
✅ **Non-Destructive:** Easy to revert - just toggle the flag back  
✅ **Automatic:** No extra steps - flag sets automatically on save  
✅ **Skip Scans:** Only high-confidence (5-star) and manual settings are marked  

## Implementation Details

### Detection Confidence Levels

The feature works with:
- **Auto-Detected Highs (5-star):** Detection algorithm marked as single
  - Can be overridden manually
  - Override prevents future scans from changing it back

- **Manually Marked:** User marked in UI
  - Automatically flagged as `single_manual_override = 1`
  - Protected from all future scans

### Database Migration

The `check_db.update_schema()` function will automatically:
- Create the new `single_manual_override` column if it doesn't exist
- Initialize all existing tracks with NULL (treated as 0)
- No data loss or migration needed

### Affected Functions

1. **advanced_single_detection.py**
   - `batch_update_advanced_singles()` - Now skips flagged tracks

2. **app.py**
   - `track_edit()` - Sets flag to 1 on save
   - `api_toggle_manual_single()` - NEW, toggles flag via API

3. **templates/track.html**
   - Shows "Manual" badge when flag = 1

## Future Enhancements

Possible additions:
- Bulk toggle API to set/clear flag for multiple tracks
- Admin interface to see all manually overridden singles
- Audit log showing when overrides were set/changed
- Statistics: "X tracks with manual overrides"
- Option to auto-clear old overrides (e.g., after X scans)

## Testing Checklist

✅ Add new field to schema  
✅ Set flag automatically on track edit  
✅ Verify single detection skips flagged tracks  
✅ Show badge on track page  
✅ Test API toggle endpoint  
✅ Run full scan and verify skipped tracks  
✅ Test revert (toggle flag back to 0)  

## Backwards Compatibility

- **No Breaking Changes:** NULL/0 values are treated the same
- **Existing Tracks:** Automatically initialized to NULL (treated as 0)
- **Gradual Adoption:** Used only when users edit tracks
- **Reversible:** Can be toggled back anytime

## Notes

- Flag is set automatically on ALL edits via form (no special action needed)
- Only high-confidence singles are marked during detection
- Manual 5-star singles are still considered "high confidence"
- Different from "5-star" - that's the user's rating, this is internal flag
