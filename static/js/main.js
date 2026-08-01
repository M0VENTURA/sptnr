/**
 * Popularr main entry point.
 *
 * This file is the entry point for esbuild bundling.
 * All JS modules are imported here and bundled into static/dist/main.js.
 *
 * To add a new JS module:
 *   1. Create the module in static/js/
 *   2. Import it below
 *   3. Rebuild: `npm run build`
 */

// Global initialization runs after DOM is ready
document.addEventListener("DOMContentLoaded", () => {
  // Initialize Bootstrap tooltips if Bootstrap loaded
  if (typeof bootstrap !== "undefined" && bootstrap.Tooltip) {
    const tooltipTriggerList = document.querySelectorAll('[data-bs-toggle="tooltip"]');
    [...tooltipTriggerList].map((el) => new bootstrap.Tooltip(el));
  }
});

// ==========================================================================
// Global functions (moved from inline base.html <script> block)
// These must be on window because they are called by inline onclick/onsubmit
// handlers in templates (e.g. navSearch, openSlideOver).
// ==========================================================================

window.openSlideOver = function(url, title) {
  var el = document.getElementById('detailSlideOver');
  var contentEl = document.getElementById('slideOverContent');
  var titleEl = document.getElementById('slideOverTitle');
  titleEl.textContent = title || 'Loading...';
  contentEl.innerHTML = '<div class="d-flex justify-content-center py-5"><div class="spinner-border text-primary"></div></div>';
  var bsOffcanvas = new bootstrap.Offcanvas(el);
  bsOffcanvas.show();
  fetch(url)
    .then(function(r) { return r.text(); })
    .then(function(html) { contentEl.innerHTML = html; })
    .catch(function(err) { contentEl.innerHTML = '<div class="alert alert-danger m-3">Error loading details: ' + err.message + '</div>'; });
};

window.formatFilePath = function(filePath) {
  if (!filePath) return 'Not available';
  var musicMatch = filePath.match(/[\/\\]music[\/\\](.+)$/i);
  if (musicMatch) return '\\Music\\' + musicMatch[1].replace(/\//g, '\\');
  return filePath;
};

window.lookupMetadata = async function(type, identifier) {
  var modal = document.getElementById('metadataModal');
  var body = document.getElementById('metadataModalBody');
  var bsModal = new bootstrap.Modal(modal);
  body.innerHTML = '<div class="spinner-border" role="status"><span class="visually-hidden">Loading...</span></div>';
  bsModal.show();
  try {
    var response = await fetch('/api/metadata?type=' + encodeURIComponent(type) + '&id=' + encodeURIComponent(identifier));
    if (!response.ok) throw new Error('Metadata lookup failed');
    var data = await response.json();
    var fieldCategories = {
      'Track Info': ['title', 'artist', 'album', 'track', 'track_id', 'date', 'length'],
      'Tags': ['genre', 'comment', 'bpm'],
      'IDs': ['mbid', 'musicbrainz_id', 'isrc', 'ean', 'spotify_uri'],
      'Scores': ['spotify_score', 'lastfm_ratio', 'final_score'],
      'File Info': ['file_path'],
      'Notes': ['note']
    };
    var html = '';
    var processedKeys = new Set();
    for (var _i = 0, _a = Object.entries(fieldCategories); _i < _a.length; _i++) {
      var _b = _a[_i], category = _b[0], fields = _b[1];
      var categoryHtml = '';
      for (var _c = 0; _c < fields.length; _c++) {
        var field = fields[_c];
        if (field in data && !processedKeys.has(field)) {
          processedKeys.add(field);
          var value = data[field];
          var displayKey = field.replace(/_/g, ' ').replace(/\b\w/g, function(l) { return l.toUpperCase(); });
          var displayValue = value || '\u2014';
          if (field === 'file_path' && value) {
            displayValue = '<code style="word-break: break-all; font-size: 0.85rem;">' + window.escapeHtml(window.formatFilePath(value)) + '</code>';
          } else if (Array.isArray(value)) {
            displayValue = value.join(', ');
          } else if (typeof displayValue === 'string') {
            displayValue = window.escapeHtml(displayValue);
          }
          categoryHtml += '<div class="metadata-row"><div class="metadata-label">' + displayKey + '</div><div class="metadata-value">' + displayValue + '</div></div>';
        }
      }
      if (category === 'Notes') {
        for (var key in data) {
          if (!processedKeys.has(key)) {
            processedKeys.add(key);
            var dk = key.replace(/_/g, ' ').replace(/\b\w/g, function(l) { return l.toUpperCase(); });
            var dv = Array.isArray(data[key]) ? data[key].join(', ') : (data[key] || '\u2014');
            categoryHtml += '<div class="metadata-row"><div class="metadata-label">' + dk + '</div><div class="metadata-value">' + window.escapeHtml(String(dv)) + '</div></div>';
          }
        }
      }
      if (categoryHtml) html += '<div class="mb-4"><h6 class="text-spotify-green mb-2" style="border-bottom:1px solid var(--border-color);padding-bottom:0.5rem;">' + category + '</h6>' + categoryHtml + '</div>';
    }
    body.innerHTML = html || '<div class="alert alert-info">No metadata available</div>';
  } catch (error) {
    body.innerHTML = '<div class="alert alert-danger">Error loading metadata: ' + error.message + '</div>';
  }
};

window.escapeHtml = function(text) {
  var div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
};

window.addBookmark = function(type, name, artist, album, trackId) {
  fetch('/api/bookmarks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ type: type, name: name, artist: artist, album: album, track_id: trackId })
  })
  .then(function(r) { return r.json(); })
  .then(function(data) {
    if (data.success) {
      if (typeof showToast === 'function') showToast('Success', 'Bookmark added', 'success');
      else alert('Bookmark added successfully');
    } else {
      if (typeof showToast === 'function') showToast('Info', data.error || 'Bookmark may already exist', 'warning');
      else alert(data.error || 'Bookmark may already exist');
    }
  })
  .catch(function(error) {
    if (typeof showToast === 'function') showToast('Error', 'Failed to add bookmark: ' + error.message, 'error');
    else alert('Failed to add bookmark: ' + error.message);
  });
};

window.updateAlbumWithBeets = function(artist, album) {
  if (!confirm('Update "' + album + '" by "' + artist + '" with beets?')) return;
  var btn = event.target.closest('button');
  var originalText = btn.innerHTML;
  btn.innerHTML = '<i class="bi bi-hourglass-split"></i> Updating...';
  btn.disabled = true;
  fetch('/api/beets/update-album', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ artist: artist, album: album })
  })
  .then(function(r) { return r.json(); })
  .then(function(data) {
    if (data.success) {
      btn.innerHTML = '<i class="bi bi-check-circle"></i> Updated!';
      btn.classList.remove('btn-outline-warning');
      btn.classList.add('btn-success');
      if (typeof showToast === 'function') showToast('Success', 'Album updated with beets', 'success');
      else alert('Album updated successfully!');
      setTimeout(function() { location.reload(); }, 2000);
    } else {
      btn.innerHTML = originalText;
      btn.disabled = false;
      if (typeof showToast === 'function') showToast('Error', 'Failed to update album: ' + data.error, 'error');
      else alert('Error: ' + data.error);
    }
  })
  .catch(function(error) {
    btn.innerHTML = originalText;
    btn.disabled = false;
    if (typeof showToast === 'function') showToast('Error', 'Failed to update album: ' + error.message, 'error');
    else alert('Error: ' + error.message);
  });
};

window.updateArtistAlbumsWithBeets = function(artist) {
  if (!confirm('Update all albums by "' + artist + '" with beets?')) return;
  fetch('/api/beets/album-folders/' + encodeURIComponent(artist))
  .then(function(r) { return r.json(); })
  .then(function(data) {
    if (data.success && data.album_folders && data.album_folders.length > 0) {
      var folders = data.album_folders;
      var btn = event.target.closest('button');
      var originalText = btn.innerHTML;
      btn.innerHTML = '<i class="bi bi-hourglass-split"></i> Updating ' + folders.length + ' album(s)...';
      btn.disabled = true;
      window.updateAlbumsSequentially(folders, 0, function() {
        btn.innerHTML = '<i class="bi bi-check-circle"></i> Complete!';
        btn.classList.remove('btn-outline-warning');
        btn.classList.add('btn-success');
        if (typeof showToast === 'function') showToast('Success', 'Updated ' + folders.length + ' album(s)', 'success');
        setTimeout(function() { location.reload(); }, 2000);
      });
    } else {
      if (typeof showToast === 'function') showToast('Info', 'No albums found', 'info');
      else alert('No albums found with folder information');
    }
  })
  .catch(function(error) {
    if (typeof showToast === 'function') showToast('Error', 'Failed to get album folders: ' + error.message, 'error');
    else alert('Error: ' + error.message);
  });
};

window.updateAlbumsSequentially = function(folders, index, onComplete) {
  if (index >= folders.length) { onComplete(); return; }
  var folder = folders[index];
  fetch('/api/beets/update-album', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ folder: folder })
  })
  .then(function(r) { return r.json(); })
  .then(function() {
    window.updateAlbumsSequentially(folders, index + 1, onComplete);
  })
  .catch(function() {
    window.updateAlbumsSequentially(folders, index + 1, onComplete);
  });
};

window.navSearch = function(event) {
  event.preventDefault();
  var form = event && event.target ? event.target : null;
  var activeInput = form ? form.querySelector('input[type="text"], input[type="search"]') : null;
  var fallbackInput = document.getElementById('navSearchInput');
  var fallbackSearchInput = document.getElementById('dashboardTopSearchInput');
  var query = ((activeInput && activeInput.value) || (fallbackInput && fallbackInput.value) || (fallbackSearchInput && fallbackSearchInput.value) || '').trim();
  if (query) window.location.href = '/search?q=' + encodeURIComponent(query);
};

// ==========================================================================
// Global MusicBrainz search modal (canonical implementation)
// ==========================================================================
// Opens the MusicBrainz search modal included from
// components/_musicbrainz_search_modal.html (loaded via base.html). Fills
// the 4-field form (artist/album/track/year), wires the selection callback,
// and auto-runs the search. This is the single source of truth — pages that
// once defined their own copy (base.html inline block, downloads.js) are
// consolidated here.

window.openGlobalMbSearch = function(artist, album, callback, track, year) {
    const modalEl = document.getElementById('musicBrainzModal');
    if (!modalEl) {
        console.error("MusicBrainz modal not found in DOM.");
        return;
    }

    // Auto-fill the 4 search fields
    const artistEl = document.getElementById('mbSearchArtist');
    const albumEl = document.getElementById('mbSearchAlbum');
    const trackEl = document.getElementById('mbSearchTrack');
    const yearEl = document.getElementById('mbSearchYear');
    if (artistEl && artist) artistEl.value = artist;
    if (albumEl && album) albumEl.value = album;
    if (trackEl && track) trackEl.value = track;
    if (yearEl && year) yearEl.value = year;

    // Assign callback to global window object so the component can trigger it
    window._mbSearchCallback = callback;

    // Show Modal
    const modal = bootstrap.Modal.getInstance(modalEl) || new bootstrap.Modal(modalEl);
    modal.show();

    // Auto-search after modal opens
    setTimeout(function() {
        if (typeof window.performMbSearch === 'function') {
            window.performMbSearch();
        }
    }, 500);
};

// ==========================================================================
// Encoded-argument MusicBrainz search (shared by artist/album detail pages)
// ==========================================================================
// Decodes URL-encoded inline arguments and opens the canonical shared modal.
// Some pages (album_detail.js) render buttons calling this with encoded args.

window.searchMusicBrainzReleaseFromEncoded = function(event, artistEnc, albumEnc) {
    const decode = function(value, fallback) {
        try {
            const decoded = decodeURIComponent(value || '');
            try { return JSON.parse(decoded); } catch (_e) { return decoded || fallback; }
        } catch (_e) { return fallback; }
    };
    const artist = decode(artistEnc, '');
    const album = decode(albumEnc, '');
    if (event && event.preventDefault) event.preventDefault();
    if (event && event.stopPropagation) event.stopPropagation();
    window.openGlobalMbSearch(artist, album, function(selectedRelease) {
        if (selectedRelease && typeof window.downloadMbRelease === 'function') {
            window.downloadMbRelease(selectedRelease.id, selectedRelease.title, selectedRelease.artist, 'slskd');
        }
    });
};
