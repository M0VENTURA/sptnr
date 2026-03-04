# Downloads Monitor Enhancements - Implementation Plan

## Overview
Enhance downloads_monitor.html to show:
1. Completion tracking (X of Y tracks downloaded/matched)
2. Visual highlighting of matched vs missing tracks
3. Duplicate folder detection and merging
4. MusicBrainz match status persistence

## Backend APIs (Already Implemented)
- `/api/downloads/folder-status` - Get all folder matches with completion data
- `/api/downloads/folder-duplicates` - Find duplicate folders matching same album
- `/api/downloads/folder-merge` - Merge duplicate folders into primary

## Frontend Changes Needed

### 1. Load Folder Status on Page Load
Add function to fetch folder status and merge with folder groups display:
```javascript
async function loadFolderStatus() {
  const response = await fetch('/api/downloads/folder-status');
  const data = await response.json();
  return data.folder_matches || [];
}
```

### 2. Enhance Folder Display with Completion Badges
For each folder group, show:
- "3/12 tracks" badge with completion percentage
- Color-coded: green (100%), yellow (50-99%), red (<50%)
- Progress bar for visual completion status

### 3. Track List Highlighting
In the track list table:
- Green highlight for matched/organized tracks
- Gray/dimmed for missing/unmatched tracks
- Checkmark icon for matched tracks

### 4. Duplicate Detection Section
Add new section above folder groups:
```html
<div id="duplicateWarnings" class="alert alert-warning">
  <strong>Duplicate Folders Detected:</strong>
  • Folder A and Folder B both match "Artist - Album"
  <button>Merge Duplicates</button>
</div>
```

### 5. Auto-Refresh After Organization
After moveMatchedFolder() completes:
- Refresh folder status
- Update completion badges
- Highlight newly organized tracks

## Implementation Status
- [x] Backend APIs created
- [x] Database migration run
- [x] organize_folder_to_music updated to track matches
- [ ] Frontend loadFolderStatus function
- [ ] Completion badge display
- [ ] Track highlighting CSS and logic
- [ ] Duplicate detection UI
- [ ] Merge duplicates function
