# Tag Editor Integration Guide

## Quick Start: Adding Edit Tags Buttons to Existing Templates

### For track.html

Add this button to the track metadata section:

```html
<!-- In track details, near other action buttons -->
<div class="action-buttons mb-3">
  <!-- ... existing buttons ... -->
  
  <button type="button" class="btn btn-outline-primary btn-sm" 
          onclick="editTrackTags('{{ track.id }}')">
    <i class="bi bi-pencil"></i> Edit Tags
  </button>
</div>
```

### For album.html

Add this button to the album header section:

```html
<!-- In album header, near other action buttons -->
<div class="button-group">
  <!-- ... existing buttons ... -->
  
  <button type="button" class="btn btn-outline-primary btn-sm"
          onclick="editAlbumTags('{{ album }}', '{{ artist }}')">
    <i class="bi bi-pencil"></i> Edit Album Tags
  </button>
</div>
```

### Display Conflict Indicator

Add a warning badge if conflicts exist:

```html
<!-- In album header -->
<div id="albumTagStatus">
  {% if conflicts %}
    <span class="badge bg-danger">
      <i class="bi bi-exclamation-triangle"></i> Metadata Conflicts
    </span>
  {% endif %}
</div>

<script>
// Check for conflicts when page loads (for album view)
async function checkAlbumConflicts() {
  try {
    const response = await fetch(
      `/api/tags/album/{{ album | urlencode }}/{{ artist | urlencode }}/conflicts`
    );
    const data = await response.json();
    
    const statusDiv = document.getElementById('albumTagStatus');
    if (data.has_conflicts) {
      statusDiv.innerHTML = `
        <span class="badge bg-danger">
          <i class="bi bi-exclamation-triangle"></i> 
          ${Object.keys(data.conflicts).length} Metadata Conflicts
        </span>
      `;
    }
  } catch (error) {
    console.error('Error checking conflicts:', error);
  }
}

// Run on page load
document.addEventListener('DOMContentLoaded', checkAlbumConflicts);
</script>
```

## Where to Put the Tag Editor Modal

The tag editor HTML should be included once per page (usually at the bottom):

```html
<!-- At the end of track.html or album.html -->
{% include 'tag_editor.html' %}
```

Or include it in a base template if used across multiple pages:

```html
<!-- In base.html template -->
{% block content %} ... {% endblock %}

<!-- Include tag editor at the very end -->
{% include 'tag_editor.html' %}
```

## Display Album Tags Inline

Show album-level metadata that can be bulk edited:

```html
<!-- Album metadata display section -->
<div class="album-metadata">
  <div class="row">
    <div class="col-md-6">
      <h6>Release Information</h6>
      <ul class="list-unstyled small">
        <li><strong>Label:</strong> {{ album_data.label | default('—') }}</li>
        <li><strong>Release Country:</strong> {{ album_data.releasecountry | default('—') }}</li>
        <li><strong>Release Type:</strong> {{ album_data.releasetype | default('—') }}</li>
        <li><strong>Status:</strong> {{ album_data.releasestatus | default('—') }}</li>
        <li><strong>Media:</strong> {{ album_data.media | default('—') }}</li>
      </ul>
    </div>
    
    <div class="col-md-6">
      <h6>Identifiers</h6>
      <ul class="list-unstyled small">
        <li><strong>UPC:</strong> <code>{{ album_data.barcode | default('—') }}</code></li>
        <li><strong>Catalog #:</strong> {{ album_data.catalognumber | default('—') }}</li>
        <li><strong>ASIN:</strong> {{ album_data.asin | default('—') }}</li>
        <li><strong>MB Release ID:</strong> <code>{{ album_data.musicbrainz_albumid | default('—') }}</code></li>
      </ul>
    </div>
  </div>
</div>
```

## Display Track-Level Metadata in Track Details

```html
<!-- Track metadata display -->
<div class="track-details">
  <div class="detail-row">
    <label>Artists:</label>
    <span>
      {% if track.artists %}
        {{ track.artists | json_decode | join(', ') }}
      {% else %}
        {{ track.artist }}
      {% endif %}
    </span>
  </div>
  
  <div class="detail-row">
    <label>Composer:</label>
    <span>{{ track.composer | default('—') }}</span>
  </div>
  
  <div class="detail-row">
    <label>Producer:</label>
    <span>
      {% if track.producer %}
        {{ track.producer | json_decode | join(', ') }}
      {% else %}
        —
      {% endif %}
    </span>
  </div>
  
  <div class="detail-row">
    <label>Writer:</label>
    <span>
      {% if track.writer %}
        {{ track.writer | json_decode | join(', ') }}
      {% else %}
        —
      {% endif %}
    </span>
  </div>
  
  <div class="detail-row">
    <label>Recording ID:</label>
    <span>
      {% if track.mbid %}
        <code><a href="https://musicbrainz.org/recording/{{ track.mbid }}" 
                 target="_blank" class="text-decoration-none">{{ track.mbid[:8] }}...</a></code>
      {% else %}
        —
      {% endif %}
    </span>
  </div>
</div>
```

## Highlight Conflict Fields in Form

Add custom CSS for conflict highlighting:

```css
/* In your stylesheet */
.form-control.is-invalid,
.form-select.is-invalid {
  border-color: #dc3545;
  background-color: rgba(220, 53, 69, 0.1);
}

.form-control.is-invalid:focus,
.form-select.is-invalid:focus {
  border-color: #dc3545;
  box-shadow: 0 0 0 0.2rem rgba(220, 53, 69, 0.25);
}

#conflictWarning {
  animation: slideDown 0.3s ease-out;
}

@keyframes slideDown {
  from {
    opacity: 0;
    transform: translateY(-10px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

.conflict-badge {
  background-color: #fff3cd !important;
  border-left: 4px solid #f0ad4e;
}
```

## Add Filter for Unmatched album_artist

Show which albums have metadata conflicts:

```html
<!-- Album list with conflict indicator -->
<div class="album-list">
  {% for album in albums %}
    <div class="album-card {% if album.artist_mismatch %}album-with-conflicts{% endif %}">
      <h5>{{ album.name }}</h5>
      
      {% if album.artist_mismatch %}
        <div class="alert alert-warning alert-sm">
          <i class="bi bi-exclamation-triangle"></i>
          Artist name mismatch detected
          <button class="btn btn-sm btn-link" onclick="editAlbumTags('{{ album.name }}', '{{ album.artist }}')">
            View conflicts
          </button>
        </div>
      {% endif %}
      
      <p>{{ album.artist }}</p>
    </div>
  {% endfor %}
</div>
```

## JavaScript Filter Function

Add to page to filter/search by metadata fields:

```javascript
// Filter albums by metadata
function filterByMetadata(field, value) {
  const albums = document.querySelectorAll('.album-card');
  
  albums.forEach(album => {
    const fieldValue = album.dataset[field];
    if (fieldValue && fieldValue.toLowerCase().includes(value.toLowerCase())) {
      album.style.display = '';
    } else {
      album.style.display = 'none';
    }
  });
}

// Example: Filter by label
document.getElementById('labelFilter').addEventListener('change', (e) => {
  filterByMetadata('label', e.target.value);
});
```

## Bulk Update UI Pattern

Add a checkbox interface for bulk album updates:

```html
<!-- Album track list with checkboxes for bulk edit -->
<div class="track-list">
  <div class="action-header">
    <label class="form-check">
      <input type="checkbox" class="form-check-input select-all-tracks">
      <span class="form-check-label">Select all</span>
    </label>
    
    <button class="btn btn-sm btn-primary" id="bulkEditBtn" disabled>
      <i class="bi bi-pencil"></i> Edit Selected
    </button>
  </div>
  
  {% for track in tracks %}
    <div class="track-row">
      <input type="checkbox" class="track-checkbox" value="{{ track.id }}" data-album="{{ album }}" data-artist="{{ artist }}">
      <span>{{ track.title }}</span>
    </div>
  {% endfor %}
</div>

<script>
// Handle bulk edit
document.getElementById('bulkEditBtn').addEventListener('click', () => {
  const selected = Array.from(document.querySelectorAll('.track-checkbox:checked'))
    .map(cb => cb.value);
  
  if (selected.length === 0) {
    alert('No tracks selected');
    return;
  }
  
  // Open editor with pre-selected tracks
  editAlbumTagsWithSelection('{{ album }}', '{{ artist }}', selected);
});

// Select all functionality
document.querySelector('.select-all-tracks')?.addEventListener('change', (e) => {
  document.querySelectorAll('.track-checkbox').forEach(cb => {
    cb.checked = e.target.checked;
  });
  
  // Enable/disable edit button
  const hasSelection = Array.from(document.querySelectorAll('.track-checkbox')).some(cb => cb.checked);
  document.getElementById('bulkEditBtn').disabled = !hasSelection;
});
```

## Validation Examples

Add field validation before saving:

```javascript
// Validate ISRC format
function validateISRC(value) {
  return /^[A-Z]{2}[A-Z0-9]{3}\d{7}$/.test(value);
}

// Validate MBID format (UUID)
function validateMBID(value) {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
}

// Validate year
function validateYear(value) {
  return /^\d{4}$/.test(value) && value >= 1800 && value <= new Date().getFullYear() + 5;
}
```

## Success/Error Notifications

Enhance the save feedback:

```javascript
// Show toast notification
function showNotification(message, type = 'success') {
  const toast = document.createElement('div');
  toast.className = `alert alert-${type} alert-dismissible fade show`;
  toast.innerHTML = `
    ${message}
    <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
  `;
  
  document.body.insertAdjacentElement('afterbegin', toast);
  
  // Auto-dismiss after 5 seconds
  setTimeout(() => toast.remove(), 5000);
}

// Use in save handler
if (success) {
  showNotification(`Updated ${data.updated_count} track(s)`, 'success');
} else {
  showNotification(`Error: ${data.error}`, 'danger');
}
```

## Related Documentation
- [NAVIDROME_METADATA_TAG_MANAGEMENT.md](NAVIDROME_METADATA_TAG_MANAGEMENT.md) - Full implementation guide
- [tag_manager.py](tag_manager.py) - Tagged manager module
- [templates/tag_editor.html](templates/tag_editor.html) - Tag editor modal component
