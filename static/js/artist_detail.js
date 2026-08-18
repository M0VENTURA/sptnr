// Artist Detail Page JS
// Extracted from templates/pages/artist_detail.html

const SLSKD_MAX_POLL_ATTEMPTS = 60;
const SLSKD_POLL_INTERVAL_MS = 2000;
const BYTES_TO_MB = 1024 * 1024;

// Event listener for Soulseek tab - show track queue option when tab is clicked
document.addEventListener('DOMContentLoaded', function() {
  const slskdTab = document.getElementById('slskd-tab');
  if (slskdTab) {
    slskdTab.addEventListener('shown.bs.tab', function() {
      if (currentDownloadAlbum.album) {
        showSlskdTrackQueueOption();
      }
    });
  }
});

// Fallback functions if genre-utils.js fails to load
if (typeof toggleGenreCheckbox === 'undefined') {
    window.toggleGenreCheckbox = function(containerId, buttonId) {
        const button = document.getElementById(buttonId);
        const container = document.getElementById(containerId);
        if (!container || !button) return;
        const checkboxes = container.querySelectorAll('input[type="checkbox"]');
        const checkedBoxes = Array.from(checkboxes).filter(cb => cb.checked);
        if (checkedBoxes.length > 0) {
            button.style.display = 'inline-block';
            button.textContent = `Remove ${checkedBoxes.length} Selected Genre${checkedBoxes.length > 1 ? 's' : ''}`;
        } else {
            button.style.display = 'none';
        }
    };
}

if (typeof getSelectedGenres === 'undefined') {
    window.getSelectedGenres = function(containerId) {
        const container = document.getElementById(containerId);
        if (!container) return [];
        return Array.from(container.querySelectorAll('input[type="checkbox"]:checked')).map(cb => cb.value);
    };
}

if (typeof handleGenreRemoval === 'undefined') {
    window.handleGenreRemoval = function(artistName, albumName, genres, contextType) {
        // Fallback handler for genre removal
        alert(`Would remove genres: ${genres.join(', ')}`);
    };
}

// ===== Artist IDs Modal Functions (defined early for onclick handlers) =====
function openEditArtistIdsModal() {
  const artistName = _pd.artistName;
  const musicbrainzId = document.getElementById('musicbrainzArtistId').value;
  const discogsId = (function() { const el = document.getElementById('discogsArtistId'); return el ? el.value : ''; })();
  
  const modalHtml = `
    <div class="modal fade" id="editArtistIdsModal" tabindex="-1">
      <div class="modal-dialog">
        <div class="modal-content">
          <div class="modal-header">
            <h5 class="modal-title"><i class="bi bi-pencil"></i> Edit Artist IDs</h5>
            <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
          </div>
          <div class="modal-body">
            <div class="mb-3">
              <label for="editMusicbrainzArtistId" class="form-label">MusicBrainz Artist ID</label>
              <input type="text" class="form-control" id="editMusicbrainzArtistId" value="${escapeHtml(musicbrainzId)}" placeholder="e.g., a74b1b7f-71a5-4011-9441-d0b5e4122711">
              <div class="form-text">
                <a href="https://musicbrainz.org/search?query=${encodeURIComponent(artistName)}&type=artist" target="_blank">Search MusicBrainz</a> to find the artist ID
              </div>
            </div>
            <div class="mb-3">
              <label for="editDiscogsArtistId" class="form-label">Discogs Artist ID</label>
              <input type="text" class="form-control" id="editDiscogsArtistId" value="${escapeHtml(discogsId)}" placeholder="e.g., 123456">
              <div class="form-text">
                <a href="https://www.discogs.com/search/?q=${encodeURIComponent(artistName)}&type=artist" target="_blank">Search Discogs</a> to find the artist ID
              </div>
            </div>
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
            <button type="button" class="btn btn-outline-info" onclick="lookupAndSaveArtistIds(this)">
              <i class="bi bi-cloud-download"></i> Lookup and Save
            </button>
            <button type="button" class="btn btn-primary" onclick="saveArtistIds()">
              <i class="bi bi-save"></i> Save Changes
            </button>
          </div>
        </div>
      </div>
    </div>
  `;
  
  const existingModal = document.getElementById('editArtistIdsModal');
  if (existingModal) existingModal.remove();
  
  document.body.insertAdjacentHTML('beforeend', modalHtml);
  
  const modal = new bootstrap.Modal(document.getElementById('editArtistIdsModal'));
  modal.show();
}

function saveArtistIds() {
  const artistName = _pd.artistName;
  const musicbrainzId = document.getElementById('editMusicbrainzArtistId').value.trim();
  const discogsId = document.getElementById('editDiscogsArtistId').value.trim();
  const saveBtn = document.querySelector('#editArtistIdsModal .btn.btn-primary');
  const originalBtnHtml = saveBtn ? saveBtn.innerHTML : null;
  if (saveBtn) {
    saveBtn.disabled = true;
    saveBtn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status" style="width:0.8rem;height:0.8rem;"></span> Saving...';
  }
  
  fetch('/api/artist/update-ids', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      artist: artistName,
      lastfm_artist_mbid: musicbrainzId,
      musicbrainz_artist_id: musicbrainzId,
      discogs_artist_id: discogsId
    })
  })
    .then(r => r.json())
    .then(data => {
      if (data.success) {
        document.getElementById('musicbrainzArtistId').value = musicbrainzId;
        if (document.getElementById('discogsArtistId')) {
          document.getElementById('discogsArtistId').value = discogsId;
        }
        
        const modalInstance = bootstrap.Modal.getInstance(document.getElementById('editArtistIdsModal'));
        if (modalInstance) modalInstance.hide();
        
        alert('✅ Artist IDs updated successfully!');
        setTimeout(() => location.reload(), 1000);
      } else {
        alert('❌ Error: ' + (data.error || 'Failed to update IDs'));
      }
    })
    .catch(err => {
      console.error('Error:', err);
      alert('❌ Network error: ' + err.message);
    })
    .finally(() => {
      if (saveBtn) {
        saveBtn.disabled = false;
        saveBtn.innerHTML = originalBtnHtml;
      }
    });
}

function lookupAndSaveArtistIds(button) {
  const artistName = _pd.artistName;
  const originalHtml = button.innerHTML;
  button.disabled = true;
  button.innerHTML = '<span class="spinner-border spinner-border-sm" role="status" style="width:0.8rem;height:0.8rem;"></span> Looking up...';

  fetch('/api/artist/lookup-ids', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ artist: artistName })
  })
    .then(r => r.json())
    .then(data => {
      if (!data.success) {
        alert('❌ Error: ' + (data.error || 'Lookup failed'));
        return;
      }

      const mbid = data.musicbrainz_artist_id || '';
      const discogs = data.discogs_artist_id || '';

      const editMb = document.getElementById('editMusicbrainzArtistId');
      const editDc = document.getElementById('editDiscogsArtistId');
      if (editMb && mbid) editMb.value = mbid;
      if (editDc && discogs) editDc.value = discogs;

      const readonlyMb = document.getElementById('musicbrainzArtistId');
      const readonlyDc = document.getElementById('discogsArtistId');
      if (readonlyMb && mbid) readonlyMb.value = mbid;
      if (readonlyDc && discogs) readonlyDc.value = discogs;

      alert('✅ Lookup complete and IDs saved to artist tracks.');
    })
    .catch(err => {
      alert('❌ Network error: ' + err.message);
    })
    .finally(() => {
      button.disabled = false;
      button.innerHTML = originalHtml;
    });
}

// Store current album info for modal
let currentDownloadAlbum = {
  artist: null,
  album: null
};

function openDownloadSearch(artistName, albumName) {
  // Store for track queueing
  currentDownloadAlbum = { artist: artistName, album: albumName };
  
  document.getElementById('downloadArtistName').textContent = artistName + (albumName ? ' - ' + albumName : '');
  
  // Set search query
  const query = albumName ? artistName + ' ' + albumName : artistName;
  
  const slskdInput = document.getElementById('slskdSearchInput');
  if (slskdInput) {
    slskdInput.value = query;
    document.getElementById('slskdResults').innerHTML = '';
    // Clear track queue section
    document.getElementById('slskdTracksContainer').innerHTML = '';
  }
  
  const modal = new bootstrap.Modal(document.getElementById('downloadModal'));
  modal.show();
  
  // Auto-search on the active tab
  setTimeout(() => {
    // Show track queue option for Soulseek
    if (albumName) {
      showSlskdTrackQueueOption();
    }
    performSlskdSearch();
  }, 300);
}

function showSlskdTrackQueueOption() {
  const container = document.getElementById('slskdTracksContainer');
  if (!container) return;
  
  container.innerHTML = `
    <div class="alert alert-info d-flex justify-content-between align-items-center mb-3">
      <div>
        <i class="bi bi-info-circle"></i> 
        Queue individual tracks from <strong>${escapeHtml(currentDownloadAlbum.album)}</strong> to Soulseek download queue
      </div>
      <button class="btn btn-sm btn-success" onclick="queueAlbumTracksToSlskd()">
        <i class="bi bi-plus-circle"></i> Queue Tracks
      </button>
    </div>
    <div id="slskdTrackQueueStatus"></div>
  `;
}

function queueAlbumTracksToSlskd() {
  if (!currentDownloadAlbum.album) {
    alert('No album selected');
    return;
  }
  
  const statusDiv = document.getElementById('slskdTrackQueueStatus');
  statusDiv.innerHTML = `
    <div class="text-center py-3">
      <div class="spinner-border spinner-border-sm text-primary" role="status">
        <span class="visually-hidden">Loading...</span>
      </div>
      <p class="mt-2">Fetching album tracklist...</p>
    </div>
  `;
  
  // Fetch album tracklist
  const params = new URLSearchParams({
    artist: currentDownloadAlbum.artist,
    album: currentDownloadAlbum.album
  });
  
  fetch(`/api/album/tracklist?${params}`)
    .then(response => response.json())
    .then(data => {
      if (data.error || !data.tracks || data.tracks.length === 0) {
        statusDiv.innerHTML = `
          <div class="alert alert-danger">
            <i class="bi bi-exclamation-triangle"></i> Could not fetch tracklist: ${escapeHtml(data.error || 'No tracks found')}
          </div>
        `;
        return;
      }
      
      const tracks = data.tracks;
      let queuedCount = 0;
      let html = `
        <div class="alert alert-info">
          <strong>Adding ${tracks.length} track${tracks.length !== 1 ? 's' : ''} to download queue...</strong>
        </div>
        <div class="table-responsive">
          <table class="table table-sm">
            <thead>
              <tr>
                <th style="width: 40px;"></th>
                <th>Track</th>
                <th class="text-center">Status</th>
              </tr>
            </thead>
            <tbody id="trackQueueTable">
      `;
      
      // Display all tracks first
      tracks.forEach((track, idx) => {
        const trackTitle = track.title || `Track ${idx + 1}`;
        html += `
          <tr id="queue-track-${idx}">
            <td class="text-center"><small class="text-muted">${idx + 1}</small></td>
            <td><small>${escapeHtml(trackTitle)}</small></td>
            <td class="text-center"><small id="queue-status-${idx}" class="text-muted">pending...</small></td>
          </tr>
        `;
      });
      
      html += `
            </tbody>
          </table>
        </div>
      `;
      
      statusDiv.innerHTML = html;
      
      // Queue tracks sequentially
      let index = 0;
      const queueNextTrack = () => {
        if (index >= tracks.length) {
          // Update summary
          const summary = document.querySelector('.alert-info strong');
          if (summary) {
            summary.textContent = `✓ Added ${queuedCount} of ${tracks.length} track${tracks.length !== 1 ? 's' : ''} to download queue`;
          }
          return;
        }
        
        const track = tracks[index];
        const statusEl = document.getElementById(`queue-status-${index}`);
        const trackTitle = track.title || `Track ${index + 1}`;
        
        statusEl.textContent = 'queuing...';
        statusEl.classList.add('text-warning');
        
        fetch('/api/queue/add', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json'
          },
          body: JSON.stringify({
            artist: currentDownloadAlbum.artist,
            title: trackTitle,
            album: currentDownloadAlbum.album,
            source: 'soulseek',
            priority: 5
          })
        })
        .then(response => response.json())
        .then(queueData => {
          if (queueData.success || queueData.queue_id) {
            queuedCount++;
            statusEl.textContent = '✓ queued';
            statusEl.classList.remove('text-warning');
            statusEl.classList.add('text-success');
          } else {
            throw new Error(queueData.error || 'Unknown error');
          }
          index++;
          queueNextTrack();
        })
        .catch(error => {
          statusEl.textContent = '✗ failed';
          statusEl.classList.remove('text-warning');
          statusEl.classList.add('text-danger');
          index++;
          queueNextTrack();
        });
      };
      
      queueNextTrack();
    })
    .catch(error => {
      statusDiv.innerHTML = `
        <div class="alert alert-danger">
          <i class="bi bi-exclamation-triangle"></i> Network error: ${escapeHtml(error.message)}
        </div>
      `;
    });
}

function performSlskdSearch() {
  const query = document.getElementById('slskdSearchInput').value;
  if (!query) return;
  
  document.getElementById('slskdLoading').style.display = 'block';
  document.getElementById('slskdResults').innerHTML = '';
  
  fetch('/api/slskd/search', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({ query: query })
  })
  .then(response => response.json())
  .then(data => {
    if (data.error) {
      document.getElementById('slskdLoading').style.display = 'none';
      document.getElementById('slskdResults').innerHTML = `
        <div class="alert alert-danger">
          <i class="bi bi-exclamation-triangle"></i> Error: ${escapeHtml(data.error)}
        </div>
      `;
      return;
    }

    if (data.slotBusy) {
      document.getElementById('slskdResults').innerHTML = `
        <div class="alert alert-warning">
          <i class="bi bi-clock"></i> <strong>Soulseek search slot is busy.</strong>
          An active search is in progress. Retrying automatically…
        </div>
      `;
      // Poll /api/slskd/search-slot until free, then retry
      const slotPoll = setInterval(() => {
        fetch('/api/slskd/search-slot')
          .then(r => r.json())
          .then(slotData => {
            if (slotData.slotFree) {
              clearInterval(slotPoll);
              performSlskdSearch();
            }
          })
          .catch(() => {});
      }, 2000);
      return;
    }
    
    const searchId = data.searchId;
    // Poll for results
    pollSlskdResults(searchId, query);
  })
  .catch(error => {
    document.getElementById('slskdLoading').style.display = 'none';
    document.getElementById('slskdResults').innerHTML = `
      <div class="alert alert-danger">
        <i class="bi bi-exclamation-triangle"></i> Network error: ${escapeHtml(error.message)}
      </div>
    `;
  });
}

function pollSlskdResults(searchId, query, pollCount = 0) {
  if (pollCount > SLSKD_MAX_POLL_ATTEMPTS) {
    document.getElementById('slskdLoading').style.display = 'none';
    document.getElementById('slskdResults').innerHTML = `
      <div class="alert alert-warning">
        <i class="bi bi-clock"></i> Search timed out. Try again or check Downloads page.
      </div>
    `;
    return;
  }
  
  fetch(`/api/slskd/search/${searchId}`)
    .then(response => response.json())
    .then(data => {
      if (!data.isComplete) {
        setTimeout(() => pollSlskdResults(searchId, query, pollCount + 1), SLSKD_POLL_INTERVAL_MS);
        return;
      }
      
      document.getElementById('slskdLoading').style.display = 'none';
      
      if (data.fileCount === 0 || !data.results || data.results.length === 0) {
        document.getElementById('slskdResults').innerHTML = `
          <div class="alert alert-info">
            <i class="bi bi-info-circle"></i> No results found. Try a different search query.
          </div>
        `;
        return;
      }
      
      let html = `
        <div class="table-responsive">
          <table class="table table-hover">
            <thead>
              <tr>
                <th>File</th>
                <th class="text-center">User</th>
                <th class="text-center">Size</th>
                <th class="text-center">Bitrate</th>
                <th class="text-center">Action</th>
              </tr>
            </thead>
            <tbody>
      `;
      
      data.results.forEach((result, idx) => {
        const sizeMB = result.size_mb || (result.size ? (result.size / BYTES_TO_MB).toFixed(2) : 'N/A');
        const bitrate = result.bitrate || 'unknown';
        
        html += `
          <tr>
            <td>
              <div class="small" style="max-width: 500px; overflow: hidden; text-overflow: ellipsis;">
                ${escapeHtml(result.filename || 'Unknown')}
              </div>
            </td>
            <td class="text-center">
              <small class="text-muted">${escapeHtml(result.username || 'N/A')}</small>
            </td>
            <td class="text-center">${sizeMB} MB</td>
            <td class="text-center">
              <small class="text-muted">${bitrate}</small>
            </td>
            <td class="text-center">
              <button class="btn btn-sm btn-success" 
                      onclick="downloadSlskdFile('${escapeJsString(result.username)}', '${escapeJsString(result.filename)}', ${parseInt(result.size) || 0})">
                <i class="bi bi-download"></i> Download
              </button>
            </td>
          </tr>
        `;
      });
      
      html += `
            </tbody>
          </table>
        </div>
        <div class="text-muted small mt-2">
          Found ${data.results.length} result(s)
        </div>
      `;
      
      document.getElementById('slskdResults').innerHTML = html;
    })
    .catch(error => {
      document.getElementById('slskdLoading').style.display = 'none';
      document.getElementById('slskdResults').innerHTML = `
        <div class="alert alert-danger">
          <i class="bi bi-exclamation-triangle"></i> Error: ${escapeHtml(error.message)}
        </div>
      `;
    });
}

function downloadSlskdFile(username, filename, size) {
  if (!confirm('Download this file from Soulseek?')) return;
  
  fetch('/api/slskd/download', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      username: username,
      filename: filename,
      size: parseInt(size)
    })
  })
  .then(response => response.json())
  .then(data => {
    if (data.error) {
      alert('❌ Error: ' + data.error);
    } else {
      alert('✅ Download started! Check the Downloads page for progress.');
    }
  })
  .catch(error => {
    alert('❌ Network error: ' + error.message);
  });
}

function formatBytes(bytes) {
  if (bytes === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return Math.round((bytes / Math.pow(k, i)) * 100) / 100 + ' ' + sizes[i];
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

function sanitizeBio(text) {
  // Escape all HTML, then restore safe <br> tags (literal or escaped variants) and newlines.
  return escapeHtml(text)
    .replace(/&lt;br\s*\/?&gt;/gi, '<br>')
    .replace(/\n/g, '<br>');
}

function escapeJsString(str) {
  if (!str) return '';
  return str.replace(/\\/g, '\\\\')
            .replace(/'/g, "\\'")
            .replace(/"/g, '\\"')
            .replace(/\n/g, '\\n')
            .replace(/\r/g, '\\r');
}

function formatDuration(rawValue) {
  if (rawValue == null || rawValue === '') return 'N/A';

  let n = Number(rawValue);
  if (!Number.isFinite(n) || n <= 0) return 'N/A';

  let seconds;
  if (n >= 100000000) {
    seconds = n / 1000000;
  } else if (n > 10000) {
    seconds = n / 1000;
  } else {
    seconds = n;
  }

  seconds = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(seconds / 3600);
  const mins = Math.floor((seconds % 3600) / 60);
  const secs = seconds % 60;

  if (hours > 0) {
    return `${hours}:${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
  }
  return `${mins}:${secs.toString().padStart(2, '0')}`;
}

// Allow Enter key to trigger search
document.addEventListener('DOMContentLoaded', function() {
  const slskdSearchInput = document.getElementById('slskdSearchInput');
  if (slskdSearchInput) {
    slskdSearchInput.addEventListener('keypress', function(e) {
      if (e.key === 'Enter') {
        performSlskdSearch();
      }
    });
  }
});

function importMissingRelease(artistName, releaseId, releaseTitle) {
  // Open the canonical MusicBrainz search prepopulated with the missing
  // entry, then queue the selected release for download through Soulseek.
  // (The old flow called /api/artist/import-release, which created
  // placeholder DB rows with no audio — the MB search → slskd download is
  // the flow that actually produces playable files.)
  if (typeof window.openGlobalMbSearch !== 'function') {
    alert('MusicBrainz search is not available on this page.');
    return;
  }
  window.openGlobalMbSearch(artistName, releaseTitle, function(selectedRelease) {
    if (!selectedRelease) return;
    if (typeof window.downloadMbRelease === 'function') {
      window.downloadMbRelease(selectedRelease.id, selectedRelease.title, selectedRelease.artist, 'slskd');
    } else if (typeof window.downloadReleaseViaSoulseek === 'function') {
      window.downloadReleaseViaSoulseek(selectedRelease.id, selectedRelease.title, selectedRelease.artist);
    } else {
      alert('Soulseek download is not available on this page.');
    }
  });
}

/**
 * Check MusicBrainz for missing releases and inject them inline into the
 * appropriate album sections.
 *
 * @param {string} artistName - Artist to check.
 * @param {boolean} [silent=false] - When true, suppress success/error alert
 *   popups (used for background auto-refresh on page load).
 */
function checkMissingReleases(artistName, silent = false, background = false) {
  const triggerBtn = window.event?.target?.closest('button');
  const originalBtnHtml = triggerBtn ? triggerBtn.innerHTML : null;
  if (triggerBtn) {
    triggerBtn.disabled = true;
    triggerBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span> Checking...';
  }

  const safeForDomId = (value) => String(value || '').replace(/\s+/g, '_').replace(/[^\w\-]/g, '_');
  const categoryToSection = {
    album: 'albums',
    live_album: 'live-albums',
    remix_album: 'remix-albums',
    ep: 'eps',
    single: 'singles',
    compilation: 'compilations'
  };

  const getMissingCategory = (item) => {
    const categoryRaw = String(item.category || item.primary_type || 'album').toLowerCase();
    if (categoryRaw.includes('compilation')) return 'compilation';
    if (categoryRaw.includes('live')) return 'live_album';
    if (categoryRaw.includes('remix')) return 'remix_album';
    if (categoryRaw.includes('single')) return 'single';
    if (categoryRaw.includes('ep')) return 'ep';
    // Fallback: inspect the title for type keywords even when the stored
    // category is the generic "album" value (e.g. secondary type missing
    // from MusicBrainz at scan time).  Mirrors the server-side fallback in
    // ``release_cache_service._fallback_release_category``.
    const titleLower = String(item.title || '').toLowerCase();
    if (titleLower.includes('live') || titleLower.includes('unplugged') || titleLower.includes('in concert')) return 'live_album';
    if (titleLower.includes('remix')) return 'remix_album';
    if (titleLower.includes('compilation') || titleLower.includes('greatest hits') || titleLower.includes('soundtrack')) return 'compilation';
    if (titleLower.includes('single')) return 'single';
    if (/\bep\b/.test(titleLower) || titleLower.includes('(ep)')) return 'ep';
    return 'album';
  };

  const rowExists = (container, albumTitle) => {
    const wanted = String(albumTitle || '').trim().toLowerCase();
    return Array.from(container.querySelectorAll('.album-row')).some(row =>
      String(row.getAttribute('data-album') || '').trim().toLowerCase() === wanted
    );
  };

  // Sort the accordion items by release year (descending).  Injected rows are
  // self-contained accordion-items (collapse body inside the item), so a plain
  // reorder of the container's .album-row children is enough — no paired
  // tracklist rows to keep together (the old table layout had those).
  const sortAccordionByYear = (container) => {
    const rows = Array.from(container.querySelectorAll('.album-row'));
    rows.sort((a, b) => {
      const yearA = parseInt(a.getAttribute('data-year') || '0', 10);
      const yearB = parseInt(b.getAttribute('data-year') || '0', 10);
      return yearB - yearA;
    });
    rows.forEach(row => container.appendChild(row));
  };

  let missingReleasesUrl = '/api/artist/missing-releases?artist=' + encodeURIComponent(artistName);
  if (background) {
    missingReleasesUrl += '&background=1';
  }

  fetch(missingReleasesUrl)
    .then(r => r.json())
    .then(data => {
      if (data.error) {
        throw new Error(data.error);
      }
      const missing = data.missing || [];
      // Remove previously injected live rows before re-inserting.
      document.querySelectorAll('.album-row[data-source="live-missing"]').forEach(row => row.remove());

      let addedCount = 0;
      const touchedSections = new Set();
      missing.forEach(item => {
        const category = getMissingCategory(item);
        const sectionKey = categoryToSection[category];
        const container = document.getElementById(`accordion-${sectionKey}`);
        if (!container) return;
        if (rowExists(container, item.title)) return;

        const year = (item.first_release_date || '').slice(0, 4) || '????';
        const fallbackArt = '/api/album-art-placeholder';
        const artUrl = item.cover_art_url || fallbackArt;
        const safeArtist = safeForDomId(artistName);
        const safeAlbum = safeForDomId(item.title);

        const artistEnc = encodeURIComponent(JSON.stringify(artistName || "")).replace(/'/g, '%27');
        const titleEnc = encodeURIComponent(JSON.stringify(item.title || "")).replace(/'/g, '%27');
        const releaseIdEnc = encodeURIComponent(JSON.stringify(item.id || "")).replace(/'/g, '%27');

        // Simple row matching the v2 category-row markup (no accordion chevron).
        const row = document.createElement('div');
        row.className = 'album-row mb-1 border-0 rounded p-2 d-flex align-items-center justify-content-between gap-2 opacity-75';
        row.style.backgroundColor = 'var(--secondary-bg)';
        row.style.border = '1px solid var(--border-color)';
        row.setAttribute('data-year', year === '????' ? '0' : year);
        row.setAttribute('data-status', 'missing');
        row.setAttribute('data-source', 'live-missing');
        row.setAttribute('data-album', item.title || '');

        row.innerHTML = `
          <div class="d-flex align-items-center gap-3 min-w-0">
            <img src="${artUrl}" alt="${escapeHtml(item.title)}"
                 class="rounded flex-shrink-0"
                 style="width: 48px; height: 48px; object-fit: cover; background-color: #2a2a2a;"
                 onerror="this.src='${fallbackArt}'">
            <div class="min-w-0">
              <div class="fw-bold text-truncate small text-secondary">${escapeHtml(item.title)}</div>
              <div class="extra-small text-muted d-flex align-items-center gap-2">
                <span>${year}</span>
                <span class="badge bg-warning text-dark extra-small" title="Release exists on MusicBrainz but is not in your library">Missing</span>
              </div>
            </div>
          </div>
          <div class="btn-group btn-group-sm flex-shrink-0">
            <button type="button" class="btn btn-outline-success btn-sm" onclick="importReleaseFromEncoded('${artistEnc}', '${releaseIdEnc}', '${titleEnc}')" title="Import this release">
              <i class="bi bi-download"></i> <span class="d-none d-sm-inline ms-1">Import</span>
            </button>
            <button type="button" class="btn btn-outline-secondary btn-sm" onclick="searchMusicBrainzReleaseFromEncoded(event, '${artistEnc}', '${titleEnc}')" title="Search MusicBrainz">
              <i class="bi bi-search"></i>
            </button>
          </div>
        `;

        container.appendChild(row);
        touchedSections.add(sectionKey);
        addedCount += 1;
      });

      ['albums', 'live-albums', 'remix-albums', 'eps', 'singles', 'compilations'].forEach(sectionKey => {
        const container = document.getElementById(`accordion-${sectionKey}`);
        if (container) {
          // A previously-empty category shows a server-rendered placeholder
          // ("No albums in this category") — drop it now that real rows exist.
          container.querySelectorAll('.category-empty-state').forEach(el => el.remove());
          sortAccordionByYear(container);
        }
        // Un-hide the section card if this check populated a previously-empty
        // category (empty sections carry the ``category-empty`` class and are
        // hidden via CSS, so the JS must remove the class — clearing the
        // inline style would lose to the ``!important`` mobile-tab rules).
        if (touchedSections.has(sectionKey)) {
          const section = document.getElementById(`${sectionKey}-section`);
          if (section) {
            section.classList.remove('category-empty');
            ensureToggleMissingButton(sectionKey);
          }
        }
      });

      initializeMissingToggle();
      if (!silent) {
        if (addedCount > 0) {
          alert(`✅ Added ${addedCount} missing release(s) inline from MusicBrainz`);
        } else if (data.info) {
          alert('ℹ️ ' + data.info);
        } else {
          alert('✅ No new missing releases found.');
        }
      }
    })
    .catch(err => {
      if (!silent) {
        alert('❌ Error checking missing releases: ' + err.message);
      }
    })
    .finally(() => {
      if (triggerBtn) {
        triggerBtn.disabled = false;
        triggerBtn.innerHTML = originalBtnHtml;
      }
    });
}

// Import a missing release into the library via the MusicBrainz search modal.
// The old flow fetched the tracklist through /api/artist/import-release and
// created PLACEHOLDER database rows (no audio files) — a dead-end.  The
// canonical flow (shared with the downloads / queue sections) opens the
// MusicBrainz search PREPOPULATED with the missing entry, and the selected
// release is queued for download through Soulseek (slskd), which is the only
// path that actually produces playable files.
function importRelease(artist, releaseId, title) {
  if (typeof window.openGlobalMbSearch !== 'function') {
    alert('MusicBrainz search is not available on this page.');
    return;
  }
  window.openGlobalMbSearch(artist, title, function(selectedRelease) {
    if (!selectedRelease) return;
    if (typeof window.downloadMbRelease === 'function') {
      window.downloadMbRelease(selectedRelease.id, selectedRelease.title, selectedRelease.artist, 'slskd');
    } else if (typeof window.downloadReleaseViaSoulseek === 'function') {
      window.downloadReleaseViaSoulseek(selectedRelease.id, selectedRelease.title, selectedRelease.artist);
    } else {
      alert('Soulseek download is not available on this page.');
    }
  });
}

function decodeInlineArtistArg(value, fallback = '') {
  try {
    const decoded = decodeURIComponent(value);
    try {
      return JSON.parse(decoded);
    } catch (_jsonErr) {
      return decoded || fallback;
    }
  } catch (error) {
    return fallback;
  }
}

function toggleTracklistFromEncoded(artistEnc, albumEnc) {
  return toggleTracklist(
    decodeInlineArtistArg(artistEnc, ''),
    decodeInlineArtistArg(albumEnc, '')
  );
}

function importReleaseFromEncoded(artistEnc, releaseIdEnc, titleEnc) {
  return importRelease(
    decodeInlineArtistArg(artistEnc, ''),
    decodeInlineArtistArg(releaseIdEnc, ''),
    decodeInlineArtistArg(titleEnc, '')
  );
}

function searchMusicBrainzReleaseFromEncoded(event, artistEnc, albumEnc) {
  // Delegate to the canonical shared-modal opener (main.js, loaded on every page).
  const artist = decodeInlineArtistArg(artistEnc, '');
  const album = decodeInlineArtistArg(albumEnc, '');
  if (typeof window.openGlobalMbSearch === 'function') {
    if (event && event.preventDefault) event.preventDefault();
    if (event && event.stopPropagation) event.stopPropagation();
    window.openGlobalMbSearch(artist, album, (selectedRelease) => {
      if (typeof downloadMbRelease === 'function') {
        downloadMbRelease(selectedRelease.id, selectedRelease.title, selectedRelease.artist, 'slskd');
      }
    });
    return;
  }
  return searchMusicBrainzRelease(
    event,
    artist,
    album
  );
}

function loadTracklistFromEncoded(artistEnc, albumEnc, buttonEl, releaseIdEnc) {
  return loadTracklist(
    decodeInlineArtistArg(artistEnc, ''),
    decodeInlineArtistArg(albumEnc, ''),
    buttonEl,
    decodeInlineArtistArg(releaseIdEnc, '')
  );
}

function mbRetrySearch() {
  const artist = (document.getElementById('mbRetryArtist')?.value || '').trim();
  const album  = (document.getElementById('mbRetryAlbum')?.value  || '').trim();
  if (typeof window.openGlobalMbSearch === 'function') {
    window.openGlobalMbSearch(artist, album, (selectedRelease) => {
      if (typeof downloadMbRelease === 'function') {
        downloadMbRelease(selectedRelease.id, selectedRelease.title, selectedRelease.artist, 'slskd');
      }
    });
    return;
  }
  searchMusicBrainzRelease(null, artist, album);
}

function displayMusicBrainzResults(results) {
  const container = document.getElementById('mbSearchResults');
  
  // Global storage for release data to avoid JSON escaping in HTML attributes
  if (!window.mbReleaseData) {
    window.mbReleaseData = {};
  }
  
  let html = '<div class="accordion" id="mbResultsAccordion">';
  
  results.forEach((release, index) => {
    const releaseId = `mbRelease${index}`;
    const dataKey = `release_${Date.now()}_${index}`;
    
    // Store release data in global object for safe access
    window.mbReleaseData[dataKey] = {
      artist: release.artist,
      album: release.title,
      tracks: release.tracks,
      year: release.date || release.first_release_date || release.year || null,
      release_id: release.release_id || release.release_group_id || null,
      source: release.source || 'musicbrainz'
    };
    
    // Handle both MusicBrainz and Discogs formats
    const source = release.source || 'musicbrainz';
    const sourceBadge = source === 'discogs' ? 
      '<span class="badge bg-info ms-2">Discogs</span>' : 
      '<span class="badge bg-primary ms-2">MusicBrainz</span>';
    
    // Format tracks based on source
    const tracksHtml = release.tracks.map((track, trackIndex) => {
      // MusicBrainz uses 'length' in milliseconds, Discogs uses 'duration' as string
      let duration = 'N/A';
      if (track.length != null && track.length !== '') {
        duration = formatDuration(track.length);
      } else if (track.duration != null && track.duration !== '') {
        duration = track.duration;
      }
      
      return `
        <tr>
          <td style="width: 44px;" class="text-center">
            <input type="checkbox" class="form-check-input mb-track-select" data-release-key="${dataKey}" data-track-index="${trackIndex}">
          </td>
          <td>${escapeHtml(track.position)}</td>
          <td>${escapeHtml(track.title)}</td>
          <td>${duration}</td>
          <td style="width: 120px;" class="text-center">
            <button class="btn btn-sm btn-outline-success mb-download-track" data-release-key="${dataKey}" data-track-index="${trackIndex}">
              <i class="bi bi-download"></i> Download
            </button>
          </td>
        </tr>
      `;
    }).join('');
    
    // Build release info
    let releaseInfo = [];
    if (release.type) {
      releaseInfo.push(release.type);
    } else if (release.formats && Array.isArray(release.formats) && release.formats.length > 0) {
      const formatStr = release.formats.join(', ');
      releaseInfo.push(formatStr);
    } else {
      releaseInfo.push('Album');
    }
    
    if (release.date) releaseInfo.push(release.date);
    else if (release.first_release_date) releaseInfo.push(release.first_release_date);
    else if (release.year) releaseInfo.push(release.year);
    else releaseInfo.push('Unknown date');
    
    releaseInfo.push(`${release.track_count} tracks`);
    
    html += `
      <div class="accordion-item">
        <h2 class="accordion-header" id="heading${releaseId}">
          <button class="accordion-button ${index === 0 ? '' : 'collapsed'}" type="button" 
            data-bs-toggle="collapse" data-bs-target="#${releaseId}" 
            aria-expanded="${index === 0 ? 'true' : 'false'}" aria-controls="${releaseId}">
            <div class="w-100">
              <strong>${escapeHtml(release.title)}</strong>${sourceBadge}
              <small class="text-muted ms-2">
                ${releaseInfo.join(' · ')}
              </small>
            </div>
          </button>
        </h2>
        <div id="${releaseId}" class="accordion-collapse collapse ${index === 0 ? 'show' : ''}" 
          aria-labelledby="heading${releaseId}" data-bs-parent="#mbResultsAccordion">
          <div class="accordion-body">
            <div class="mb-3 d-flex flex-wrap align-items-center gap-2">
              <button class="btn btn-success mb-download-release" 
                data-release-key="${dataKey}">
                <i class="bi bi-download"></i> Download All Tracks (${release.track_count})
              </button>
              <button class="btn btn-outline-success mb-download-selected" data-release-key="${dataKey}" disabled>
                <i class="bi bi-check2-square"></i> Download Selected (0)
              </button>
              <div class="form-check mb-0 ms-md-2">
                <input class="form-check-input mb-select-all" type="checkbox" id="mbSelectAll_${releaseId}" data-release-key="${dataKey}">
                <label class="form-check-label" for="mbSelectAll_${releaseId}">Select All</label>
              </div>
            </div>
            <table class="table table-sm table-hover table-striped table-dark mb-0">
              <thead>
                <tr>
                  <th style="width: 44px;" class="text-center"></th>
                  <th style="width: 60px;">#</th>
                  <th>Title</th>
                  <th style="width: 100px;">Duration</th>
                  <th style="width: 120px;" class="text-center">Action</th>
                </tr>
              </thead>
              <tbody>
                ${tracksHtml}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    `;
  });
  
  html += '</div>';
  container.innerHTML = html;
  
  // Attach event listeners to download buttons
  container.querySelectorAll('.mb-download-release').forEach(button => {
    button.addEventListener('click', async function() {
      const dataKey = this.dataset.releaseKey;
      const releaseData = window.mbReleaseData[dataKey];

      if (!releaseData) {
        alert('Error: Release data not found');
        return;
      }

      const btn = this;
      const originalHtml = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1" role="status" aria-hidden="true"></span> Queuing…';
      try {
        await downloadMusicBrainzRelease(releaseData.artist, releaseData.album, releaseData.tracks, releaseData.year, releaseData.release_id, releaseData.source);
      } finally {
        btn.disabled = false;
        btn.innerHTML = originalHtml;
      }
    });
  });

  container.querySelectorAll('.mb-download-track').forEach(button => {
    button.addEventListener('click', async function() {
      const dataKey = this.dataset.releaseKey;
      const trackIndex = Number(this.dataset.trackIndex);
      const releaseData = window.mbReleaseData[dataKey];

      if (!releaseData || !Array.isArray(releaseData.tracks) || !releaseData.tracks[trackIndex]) {
        alert('Error: Track data not found');
        return;
      }

      const queued = await downloadMusicBrainzRelease(
        releaseData.artist,
        releaseData.album,
        [releaseData.tracks[trackIndex]],
        releaseData.year,
        releaseData.release_id,
        releaseData.source,
        { closeModal: false, selectionLabel: 'track' }
      );
      if (queued) {
        markMBTrackQueued(container, dataKey, trackIndex);
      }
    });
  });

  container.querySelectorAll('.mb-download-selected').forEach(button => {
    button.addEventListener('click', async function() {
      const dataKey = this.dataset.releaseKey;
      const releaseData = window.mbReleaseData[dataKey];
      const selectedTracks = getSelectedTracksForRelease(dataKey);
      const selectedIndices = Array.from(container.querySelectorAll(`.mb-track-select[data-release-key="${dataKey}"]:checked`))
        .map(input => Number(input.dataset.trackIndex));

      if (!releaseData || selectedTracks.length === 0) {
        alert('Select at least one track first');
        return;
      }

      const queued = await downloadMusicBrainzRelease(
        releaseData.artist,
        releaseData.album,
        selectedTracks,
        releaseData.year,
        releaseData.release_id,
        releaseData.source,
        { closeModal: false, selectionLabel: 'selected tracks' }
      );
      if (queued) {
        selectedIndices.forEach(index => markMBTrackQueued(container, dataKey, index));
      }
    });
  });

  container.querySelectorAll('.mb-select-all').forEach(checkbox => {
    checkbox.addEventListener('change', function() {
      const dataKey = this.dataset.releaseKey;
      const checked = this.checked;
      container.querySelectorAll(`.mb-track-select[data-release-key="${dataKey}"]`).forEach(trackBox => {
        trackBox.checked = checked;
      });
      updateMBSelectionUI(container, dataKey);
    });
  });

  container.querySelectorAll('.mb-track-select').forEach(checkbox => {
    checkbox.addEventListener('change', function() {
      const dataKey = this.dataset.releaseKey;
      updateMBSelectionUI(container, dataKey);
    });
  });
}

function getSelectedTracksForRelease(dataKey) {
  const releaseData = window.mbReleaseData && window.mbReleaseData[dataKey];
  if (!releaseData || !Array.isArray(releaseData.tracks)) {
    return [];
  }

  const selectedIndices = Array.from(document.querySelectorAll(`.mb-track-select[data-release-key="${dataKey}"]:checked`))
    .map(input => Number(input.dataset.trackIndex))
    .filter(index => Number.isInteger(index) && index >= 0 && index < releaseData.tracks.length);

  return selectedIndices.map(index => releaseData.tracks[index]);
}

function updateMBSelectionUI(container, dataKey) {
  const allTrackBoxes = container.querySelectorAll(`.mb-track-select[data-release-key="${dataKey}"]`);
  const selectedTrackBoxes = container.querySelectorAll(`.mb-track-select[data-release-key="${dataKey}"]:checked`);
  const selectedCount = selectedTrackBoxes.length;
  const totalCount = allTrackBoxes.length;

  const selectedBtn = container.querySelector(`.mb-download-selected[data-release-key="${dataKey}"]`);
  if (selectedBtn) {
    selectedBtn.disabled = selectedCount === 0;
    selectedBtn.innerHTML = `<i class="bi bi-check2-square"></i> Download Selected (${selectedCount})`;
  }

  const selectAll = container.querySelector(`.mb-select-all[data-release-key="${dataKey}"]`);
  if (selectAll) {
    selectAll.checked = totalCount > 0 && selectedCount === totalCount;
    selectAll.indeterminate = selectedCount > 0 && selectedCount < totalCount;
  }
}

function markMBTrackQueued(container, dataKey, trackIndex) {
  const rowCheckbox = container.querySelector(`.mb-track-select[data-release-key="${dataKey}"][data-track-index="${trackIndex}"]`);
  if (rowCheckbox) {
    rowCheckbox.checked = false;
    rowCheckbox.disabled = true;
  }

  const rowButton = container.querySelector(`.mb-download-track[data-release-key="${dataKey}"][data-track-index="${trackIndex}"]`);
  if (rowButton) {
    rowButton.disabled = true;
    rowButton.classList.remove('btn-outline-success');
    rowButton.classList.add('btn-success');
    rowButton.innerHTML = '<i class="bi bi-check2"></i> Queued';
  }

  updateMBSelectionUI(container, dataKey);
}

async function downloadMusicBrainzRelease(artist, album, tracks, year, release_id, source, options = {}) {
  if (!tracks || tracks.length === 0) {
    alert('No tracks to download');
    return false;
  }

  // Keep modal open by default so users can queue multiple albums without re-running the search.
  const closeModal = options.closeModal === true;
  const selectionLabel = options.selectionLabel || null;
  
  try {
    // Extract year from date string if needed (e.g., "2023-04-15" -> "2023")
    let releaseYear = null;
    if (year) {
      // Handle both string and number types
      const yearStr = String(year);
      releaseYear = yearStr.substring(0, 4);
    }
    
    // Batch add all tracks in a single API call with MusicBrainz/Discogs metadata.
    // For "Various Artists" compilations each track can carry its own artist field
    // (populated by the MusicBrainz search endpoint).  When a per-track artist is
    // present it is used as the queue item artist so files are tagged and named
    // correctly, while album_artist always stores the release-level artist (e.g.
    // "Various Artists") for correct library folder organisation.
    const trackItems = tracks.map(track => ({
      artist: track.artist || artist,
      title: track.title,
      album: album,
      source: 'soulseek',
      priority: 5,
      track_number: track.position || null,
      disc_number: track.disc_number || null,
      album_artist: artist,
      year: releaseYear,
      release_id: release_id,
      // release_mbid fills a separate column used for MB-specific duplicate
      // detection and cross-source overwrite merge in add_to_queue.
      release_mbid: release_id || null,
      release_source: source || 'musicbrainz',
      recording_mbid: track.recording_mbid || null,
      duration: track.length || null,
    }));
    
    // Send batch request with import_group based on artist+album
    // This tags all tracks from this specific album together
    // Once all downloads complete, they can be organized together using "Organize All" button
    const import_group = `${artist}_${album}`.replace(/\s+/g, '_').substring(0, 100);
    
    const response = await fetch('/api/queue/add-batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        items: trackItems,
        import_group: import_group,  // Group by artist + album
        import_type: 'album'  // Mark as album import
      })
    });
    
    const data = await response.json();
    
    // Close modal for full-album downloads only
    if (closeModal) {
      const modalEl = document.getElementById('musicBrainzModal');
      const modal = bootstrap.Modal.getInstance(modalEl);
      if (modal) {
        modal.hide();
      }
    }
    
    // Show result message
    if (data.success) {
      const added = data.added || 0;
      const skipped = data.skipped || 0;
      const failed = data.failed || 0;
      const skippedTracks = data.skipped_tracks || [];
      const failedTracks = data.failed_tracks || [];
      const importGroup = data.import_group;
      const importType = data.import_type || 'album';
      
      const label = selectionLabel ? ` ${selectionLabel}` : ' tracks';
      let message = `✅ Added ${added}${label} from "${album}" to download queue`;
      if (skipped > 0) {
        message += `\nℹ️ Skipped ${skipped} already queued tracks`;
        if (skippedTracks.length > 0) {
          const skippedList = skippedTracks.slice(0, 5).join(', ');
          message += `:\n${skippedList}${skippedTracks.length > 5 ? '...' : ''}`;
        }
      }
      if (failed > 0) {
        message += `\n⚠️ Failed to add ${failed} tracks`;
        if (failedTracks.length > 0) {
          const failedList = failedTracks.slice(0, 5).join(', ');
          message += `:\n${failedList}${failedTracks.length > 5 ? '...' : ''}`;
        }
      }
      
      // Inform user about grouping and organization
      if (added > 0 && importGroup) {
        message += `\n\n📦 All ${importType} tracks are grouped as: "${album}"`;
        message += `\nOnce downloads complete, use "Organize All" in the Completed section to move them to /music`;
      }
      
      alert(message);
      
      // Refresh queue status if the function exists (from downloads_monitor context)
      if (typeof loadQueueStatus === 'function') {
        await loadQueueStatus();
      }
      
      // Try to trigger refresh on monitor page if it's open in another tab
      try {
        // Use localStorage to notify other tabs/windows to refresh
        const timestamp = Date.now();
        localStorage.setItem('popularr_queue_updated', timestamp.toString());
      } catch (e) {
        console.warn('Could not update localStorage:', e);
      }
      return true;
    } else {
      alert('❌ Error: ' + (data.error || 'Failed to add tracks to queue'));
      return false;
    }
    
  } catch (error) {
    console.error('Error downloading release:', error);
    alert('❌ Error: ' + error.message);
    return false;
  }
}


// Load artist bio on page load
document.addEventListener('DOMContentLoaded', () => {
  const artistName = _pd.artistName;
  const bioContainer = document.getElementById('artistBio');
  const hasInitialBio = bioContainer && bioContainer.dataset && bioContainer.dataset.hasInitialBio === '1';
  if (!hasInitialBio) {
    loadArtistBio(artistName);
  }
  loadSinglesCount(artistName);
});

function loadArtistBio(artistName) {
  const bioContainer = document.getElementById('artistBio');
  if (!bioContainer) return;

  fetch(`/api/artist/bio?name=${encodeURIComponent(artistName)}`)
    .then(r => r.json())
    .then(data => {
      if (data.bio && data.bio.length > 0) {
        bioContainer.innerHTML = `<p>${sanitizeBio(data.bio)}</p>`;
        if (data.source) {
          bioContainer.innerHTML += `<p class="small text-muted mt-2"><em>Source: ${escapeHtml(data.source)}</em></p>`;
        }
      } else {
        bioContainer.innerHTML = '<p class="text-muted"><em>No biography available for this artist.</em></p>';
      }
    })
    .catch(err => {
      bioContainer.innerHTML = '<p class="text-muted"><em>Unable to load artist biography.</em></p>';
    });
}

function loadSinglesCount(artistName) {
  fetch(`/api/artist/singles-count?name=${encodeURIComponent(artistName)}`)
    .then(r => r.json())
    .then(data => {
      const badge = document.getElementById('singlesCount');
      if (badge && data.count !== undefined) {
        badge.textContent = data.count;
      }
    })
    .catch(err => console.error('Failed to load singles count:', err));
}

function showArtistSingles(artistName) {
  window.location.href = `/artist/${encodeURIComponent(artistName)}/singles`;
}

function createEssentialPlaylist(artistName) {
  if (!confirm(`Create Essential Playlist for ${artistName}?\\nThis will use single detection to pick the best tracks.`)) return;

  const btn = event.target.closest('button');
  const originalContent = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Creating...';

  fetch('/api/artist/create-essential-playlist', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ artist: artistName })
  })
    .then(r => r.json())
    .then(data => {
      if (data.success) {
        alert(`✅ ${data.message}\\nPlaylist: ${data.playlist_name}`);
        if (data.navidrome_url) {
          if (confirm('Open playlist in Navidrome?')) {
            window.open(data.navidrome_url, '_blank');
          }
        }
      } else {
        alert('❌ Error: ' + (data.error || 'Failed to create playlist'));
      }
      btn.disabled = false;
      btn.innerHTML = originalContent;
    })
    .catch(err => {
      alert('❌ Network error: ' + err.message);
      btn.disabled = false;
      btn.innerHTML = originalContent;
    });
}

function openArtistImageModal(artistName) {
  // For now, create a simple modal for image search
  const modalHtml = `
    <div class="modal fade" id="artistImageModal" tabindex="-1">
      <div class="modal-dialog modal-lg">
        <div class="modal-content">
          <div class="modal-header">
            <h5 class="modal-title"><i class="bi bi-image"></i> Change Artist Image</h5>
            <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
          </div>
          <div class="modal-body">
            <div class="mb-3">
              <label for="manualImageUrl" class="form-label">Manual Image URL</label>
              <input type="text" class="form-control" id="manualImageUrl" placeholder="https://example.com/image.jpg">
            </div>
            <div class="d-flex gap-2 mb-4 flex-wrap">
              <button class="btn btn-primary" onclick="applyManualImage('${escapeJsString(artistName)}')">
                <i class="bi bi-check"></i> Apply URL
              </button>
              <button class="btn btn-secondary" onclick="searchArtistImages('${escapeJsString(artistName)}', 'musicbrainz')">
                <i class="bi bi-search"></i> MusicBrainz
              </button>
              <button class="btn btn-secondary" onclick="searchArtistImages('${escapeJsString(artistName)}', 'discogs')">
                <i class="bi bi-disc"></i> Discogs
              </button>
              <button class="btn btn-secondary" onclick="searchArtistImages('${escapeJsString(artistName)}', 'applemusic')">
                <i class="bi bi-apple"></i> Apple Music
              </button>
            </div>
            <div id="artistImageResults"></div>
          </div>
        </div>
      </div>
    </div>
  `;
  
  // Remove existing modal if any
  const existingModal = document.getElementById('artistImageModal');
  if (existingModal) existingModal.remove();
  
  document.body.insertAdjacentHTML('beforeend', modalHtml);
  const modal = new bootstrap.Modal(document.getElementById('artistImageModal'));
  modal.show();
}

function applyManualImage(artistName) {
  const url = document.getElementById('manualImageUrl').value;
  if (!url) {
    alert('Please enter an image URL');
    return;
  }
  applyArtistImage(artistName, url);
}

function searchArtistImages(artistName, source) {
  const resultsDiv = document.getElementById('artistImageResults');
  resultsDiv.innerHTML = '<div class="text-center"><span class="spinner-border"></span> Searching...</div>';

  fetch(`/api/artist/search-images?name=${encodeURIComponent(artistName)}&source=${source}`)
    .then(r => r.json())
    .then(data => {
      if (data.error || !data.images || data.images.length === 0) {
        resultsDiv.innerHTML = '<div class="alert alert-info">No images found</div>';
        return;
      }

      let html = '<div class="row g-3">';
      data.images.forEach(img => {
        html += `
          <div class="col-6 col-md-4">
            <div class="card">
              <img src="${escapeHtml(img.url)}" class="card-img-top" style="height: 200px; object-fit: cover;">
              <div class="card-body p-2">
                <button class="btn btn-sm btn-primary w-100" onclick="applyArtistImage('${escapeJsString(artistName)}', '${escapeJsString(img.url)}')">
                  <i class="bi bi-check"></i> Use This
                </button>
              </div>
            </div>
          </div>
        `;
      });
      html += '</div>';
      resultsDiv.innerHTML = html;
    })
    .catch(err => {
      resultsDiv.innerHTML = '<div class="alert alert-danger">Error: ' + err.message + '</div>';
    });
}

function applyArtistImage(artistName, imageUrl) {
  fetch('/api/artist/set-image', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ artist: artistName, image_url: imageUrl })
  })
    .then(r => r.json())
    .then(data => {
      if (data.success) {
        document.getElementById('artistImage').src = imageUrl + '?t=' + Date.now();
        bootstrap.Modal.getInstance(document.getElementById('artistImageModal'))?.hide();
        alert('✅ Artist image updated!');
      } else {
        alert('❌ Error: ' + (data.error || 'Failed to update image'));
      }
    })
    .catch(err => alert('❌ Network error: ' + err.message));
}

function openSlskdSearch(query, artistName) {
  // If artistName is provided, search for "artist + query", otherwise just query
  const searchQuery = (artistName ? artistName + ' ' + query : query)
    .replace(/\\u0026/gi, ' ')
    .replace(/&amp;/gi, ' ')
    .replace(/&/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  // Manual Soulseek search page — the query is prefilled (editable)
  // and the search runs on load (search_init.js reads the q= param).
  window.location.href = `/downloads/search?q=${encodeURIComponent(searchQuery)}`;
}

function fetchArtistGenreRecommendations() {
  const artistName = _pd.artistName;
  const btn = document.getElementById('fetchArtistGenresBtn');
  const container = document.getElementById('recommendedArtistGenres');
  const section = document.getElementById('recommendedArtistGenresSection');
  
  // Show the recommendations section
  section.style.display = 'block';
  
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Fetching...';
  container.innerHTML = '<span class="text-muted small">Loading recommendations...</span>';
  
  // Fetch recommendations through backend so MusicBrainz headers are always set server-side.
  fetch(`/api/artist/genre-recommendations?artist=${encodeURIComponent(artistName)}`)
    .then(r => {
      if (!r.ok) {
        throw new Error('Failed to fetch artist recommendations');
      }
      return r.json();
    })
    .then(data => {
      const genres = new Set();

      if (Array.isArray(data.recommendations)) {
        data.recommendations.forEach(genre => {
          if (genre && String(genre).trim()) {
            genres.add(String(genre).trim());
          }
        });
      }
      
      if (genres.size === 0) {
        container.innerHTML = '<span class="text-muted small">No genre recommendations found from MusicBrainz</span>';
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-cloud-download"></i> Get Online Suggestions';
        return;
      }
      
      // Display genres as selectable badges
      let html = '';
      Array.from(genres).sort().forEach(genre => {
        html += `<span class="badge badge-outline-primary artist-genre-badge" 
          onclick="toggleArtistGenreSelection('${escapeHtml(genre)}')" 
          data-artist-genre="${escapeHtml(genre)}"
          style="cursor: pointer; border: 2px solid #0d6efd; background-color: transparent; color: #0d6efd;">
          ${escapeHtml(genre)}
        </span>`;
      });
      container.innerHTML = html;
      
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-cloud-download"></i> Get Online Suggestions';
      document.getElementById('applyArtistGenresBtn').style.display = 'inline-block';
    })
    .catch(error => {
      container.innerHTML = '<span class="text-danger small">Error fetching recommendations</span>';
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-cloud-download"></i> Get Online Suggestions';
    });
}

// Artist genre selection tracking
let selectedArtistGenres = new Set();

function toggleArtistGenreSelection(genre) {
  const badge = document.querySelector(`[data-artist-genre="${escapeHtml(genre)}"]`);
  if (!badge) return;
  
  if (selectedArtistGenres.has(genre)) {
    selectedArtistGenres.delete(genre);
    badge.style.backgroundColor = 'transparent';
    badge.style.color = '#0d6efd';
  } else {
    selectedArtistGenres.add(genre);
    badge.style.backgroundColor = '#0d6efd';
    badge.style.color = '#fff';
  }
}

function applySelectedArtistGenres() {
  if (selectedArtistGenres.size === 0) {
    alert('Please select at least one genre to apply');
    return;
  }
  
  const artistName = _pd.artistName;
  const genresArray = Array.from(selectedArtistGenres);
  
  if (!confirm(`Apply genres [${genresArray.join(', ')}] to ALL tracks by ${artistName}?\n\nThis will update the MP3 files for all albums by this artist.`)) {
    return;
  }
  
  const btn = document.getElementById('applyArtistGenresBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Applying...';
  
  fetch('/api/artist/apply-genres', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      artist: artistName,
      genres: genresArray
    })
  })
    .then(r => r.json())
    .then(data => {
      if (data.success) {
        alert(`✅ ${data.message || 'Genres applied successfully to all artist tracks'}`);
        // Refresh the page to show updated genres
        location.reload();
      } else {
        alert('❌ Error: ' + (data.error || 'Failed to apply genres'));
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-check-circle"></i> Apply Selected to All Artist Tracks (MP3 Files)';
      }
    })
    .catch(error => {
      alert('❌ Network error: ' + error.message);
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-check-circle"></i> Apply Selected to All Artist Tracks (MP3 Files)';
    });
}

function fetchArtistCountry() {
  const artistName = _pd.artistName;
  const btn = document.getElementById('fetchArtistCountryBtn');
  const container = document.getElementById('artistCountryDisplay');
  
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Fetching...';
  container.innerHTML = '<span class="text-muted small">Loading country information from MusicBrainz...</span>';
  
  fetch('/api/artist/country', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ artist_name: artistName })
  })
    .then(r => r.json())
    .then(data => {
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-cloud-download"></i> Get from MusicBrainz';
      
      if (data.success && data.country) {
        container.innerHTML = `
          <span class="badge bg-info" style="font-size: 1rem; padding: 0.5rem 1rem;">
            <i class="bi bi-geo-alt"></i> ${escapeHtml(data.country)}
          </span>
          <p class="text-muted small mt-2 mb-0">This can be used as a genre tag for classification</p>
        `;
        // Show a success notification
        alert('✅ ' + data.message);
      } else {
        container.innerHTML = `
          <div class="alert alert-warning mb-0">
            <i class="bi bi-exclamation-triangle"></i> ${escapeHtml(data.error || 'No country information found')}
          </div>
        `;
      }
    })
    .catch(error => {
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-cloud-download"></i> Get from MusicBrainz';
      container.innerHTML = `
        <div class="alert alert-danger mb-0">
          <i class="bi bi-x-circle"></i> Error: ${escapeHtml(error.message)}
        </div>
      `;
    });
}

function editArtistCountry() {
  const artistName = _pd.artistName;
  // Extract country text by getting text nodes (skipping the icon element)
  const badgeEl = document.querySelector('#artistCountryDisplay .badge');
  const currentCountry = badgeEl ? Array.from(badgeEl.childNodes)
    .filter(node => node.nodeType === Node.TEXT_NODE)
    .map(node => node.textContent.trim())
    .join('') : '';
  
  const modalHtml = `
    <div class="modal fade" id="editCountryModal" tabindex="-1">
      <div class="modal-dialog">
        <div class="modal-content">
          <div class="modal-header">
            <h5 class="modal-title"><i class="bi bi-pencil"></i> Edit Artist Country/Origin</h5>
            <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
          </div>
          <div class="modal-body">
            <div class="mb-3">
              <label for="countryInput" class="form-label">Country/Origin</label>
              <input type="text" class="form-control" id="countryInput" value="${escapeHtml(currentCountry)}" placeholder="e.g., United States, United Kingdom, Japan">
              <div class="form-text">Enter the artist's country or region of origin</div>
            </div>
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
            <button type="button" class="btn btn-primary" onclick="saveArtistCountry()">
              <i class="bi bi-save"></i> Save
            </button>
          </div>
        </div>
      </div>
    </div>
  `;
  
  const existingModal = document.getElementById('editCountryModal');
  if (existingModal) existingModal.remove();
  
  document.body.insertAdjacentHTML('beforeend', modalHtml);
  const modal = new bootstrap.Modal(document.getElementById('editCountryModal'));
  modal.show();
}

function saveArtistCountry() {
  const artistName = _pd.artistName;
  const country = document.getElementById('countryInput').value.trim();
  
  if (!country) {
    alert('Please enter a country');
    return;
  }
  
  fetch('/api/artist/country/update', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ artist_name: artistName, country: country })
  })
    .then(r => r.json())
    .then(data => {
      if (data.success) {
        bootstrap.Modal.getInstance(document.getElementById('editCountryModal'))?.hide();
        
        // Update the display
        const container = document.getElementById('artistCountryDisplay');
        container.innerHTML = `
          <div class="d-flex align-items-center gap-2 mb-2">
            <span class="badge bg-info" style="font-size: 0.95rem; padding: 0.4rem 0.8rem;">
              ${escapeHtml(country)}
            </span>
          </div>
        `;
        alert('✅ Country updated successfully!');
      } else {
        alert('❌ Error: ' + (data.error || 'Failed to update country'));
      }
    })
    .catch(err => alert('❌ Network error: ' + err.message));
}

function applyCountryAsGenre() {
  const artistName = _pd.artistName;
  const countryBadge = document.querySelector('#artistCountryDisplay .badge');
  if (!countryBadge) {
    alert('No country information available');
    return;
  }
  
  // Extract country by getting the last text node (after the icon)
  const country = Array.from(countryBadge.childNodes)
    .filter(node => node.nodeType === Node.TEXT_NODE)
    .map(node => node.textContent.trim())
    .join('');
  
  if (!confirm(`Apply "${country}" as a genre tag to all tracks by ${artistName}?`)) {
    return;
  }
  
  fetch('/api/artist/country/apply-as-genre', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ artist_name: artistName })
  })
    .then(r => r.json())
    .then(data => {
      if (data.success) {
        alert(`✅ ${data.message}\n\nUpdated ${data.tracks_updated} track(s)`);
      } else {
        alert('❌ Error: ' + (data.error || 'Failed to apply genre'));
      }
    })
    .catch(err => alert('❌ Network error: ' + err.message));
}

// Remove single genre from all artist tracks (uses shared utilities)
function removeGenreFromArtist(genre) {
  const artistName = _pd.artistName;
  handleGenreRemoval(artistName, null, [genre], 'artist');
}

// Remove selected genres from all artist tracks (batch removal)
function removeSelectedArtistGenres() {
  const artistName = _pd.artistName;
  const selectedGenres = getSelectedGenres('currentArtistGenres');
  
  if (selectedGenres.length === 0) {
    alert('No genres selected');
    return;
  }
  
  handleGenreRemoval(artistName, null, selectedGenres, 'artist');
}

// Toggle apply button visibility when source-tag checkboxes change
document.addEventListener('change', function(e) {
  if (e.target && e.target.classList.contains('artist-source-tag-check')) {
    const pane = e.target.closest('.tab-pane');
    if (!pane) return;
    const applyBtn = pane.querySelector('.artist-apply-source-tags-btn');
    if (!applyBtn) return;
    const anyChecked = pane.querySelectorAll('.artist-source-tag-check:checked').length > 0;
    applyBtn.style.display = anyChecked ? '' : 'none';
  }
});

function applySelectedArtistSourceTags() {
  const artistName = _pd.artistName;
  const checked = document.querySelectorAll('.artist-source-tag-check:checked');
  const selected = Array.from(checked).map(cb => cb.value).filter(Boolean);
  if (selected.length === 0) {
    alert('No tags selected');
    return;
  }
  if (!confirm(`Save tags [${selected.join(', ')}] to ALL tracks by ${artistName}?`)) {
    return;
  }
  const btns = document.querySelectorAll('.artist-apply-source-tags-btn');
  btns.forEach(b => { b.disabled = true; b.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Saving…'; });
  fetch('/api/artist/apply-genres', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ artist: artistName, genres: selected })
  })
    .then(r => r.json())
    .then(data => {
      if (data.success) {
        alert('✅ ' + (data.message || 'Tags saved to all artist tracks'));
        location.reload();
      } else {
        alert('❌ Error: ' + (data.error || 'Failed to save tags'));
        btns.forEach(b => { b.disabled = false; b.innerHTML = '<i class="bi bi-check-circle"></i> Save Selected to All Artist Tracks'; });
      }
    })
    .catch(err => {
      alert('❌ Network error: ' + err.message);
      btns.forEach(b => { b.disabled = false; b.innerHTML = '<i class="bi bi-check-circle"></i> Save Selected to All Artist Tracks'; });
    });
}

// Toggle tracklist visibility
function toggleTracklist(artist, album) {
  const rowId = `tracklist-${artist.replace(/\s+/g, '_')}-${album.replace(/\s+/g, '_')}`;
  const btnId = `toggle-btn-${artist.replace(/\s+/g, '_')}-${album.replace(/\s+/g, '_')}`;
  const contentId = `tracklist-content-${artist.replace(/\s+/g, '_')}-${album.replace(/\s+/g, '_')}`;
  const row = document.getElementById(rowId);
  const btn = document.getElementById(btnId);
  
  if (row) {
    const isHidden = row.style.display === 'none';
    row.style.display = isHidden ? '' : 'none';
    
    // Update button styling and icon
    if (btn) {
      if (isHidden) {
        btn.classList.add('active');
        btn.innerHTML = '<i class="bi bi-chevron-up"></i>';
        // Auto-load tracklist when expanded if not already loaded.
        setTimeout(() => {
          const contentDiv = document.getElementById(contentId);
          if (contentDiv && contentDiv.innerHTML.trim() === '') {
            const trackedRow = document.getElementById(rowId);
            const loadBtn = trackedRow?.querySelector('button.btn-tracklist-load');
            if (loadBtn) {
              loadBtn.click();
            } else {
              // Discovered releases do not have a "Load" button; fetch directly.
              loadTracklist(artist, album, null, null);
            }
          }
        }, 100);
      } else {
        btn.classList.remove('active');
        btn.innerHTML = '<i class="bi bi-list-ul"></i>';
      }
    }
  }
}

function downloadTrack(searchQuery, artist, title) {
  if (!confirm(`Download "${title}" by ${artist}?`)) return;
  
  const btn = event.target.closest('button');
  const originalHTML = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
  
  // Initiate download via Soulseek
  fetch('/api/slskd/search', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query: searchQuery })
  })
  .then(response => response.json())
  .then(data => {
    if (data.error) {
      alert('❌ Error: ' + data.error);
      btn.disabled = false;
      btn.innerHTML = originalHTML;
      return;
    }
    
    const searchId = data.searchId;
    // Poll for results with a shorter timeout since we just want the first match
    pollSlskdResultsForDownload(searchId, artist, title, btn, originalHTML);
  })
  .catch(error => {
    alert('❌ Network error: ' + error.message);
    btn.disabled = false;
    btn.innerHTML = originalHTML;
  });
}

function pollSlskdResultsForDownload(searchId, artist, title, btn, originalHTML, pollCount = 0) {
  if (pollCount > 60) {
    // Timeout - try again or show manual search
    btn.disabled = false;
    btn.innerHTML = originalHTML;
    alert('⏱️ Search timed out. Try downloading manually from the Downloads page.');
    return;
  }
  
  fetch(`/api/slskd/search/${searchId}`)
    .then(response => response.json())
    .then(data => {
      if (!data.isComplete) {
        setTimeout(() => pollSlskdResultsForDownload(searchId, artist, title, btn, originalHTML, pollCount + 1), 1000);
        return;
      }
      
      if (data.fileCount === 0 || !data.results || data.results.length === 0) {
        btn.disabled = false;
        btn.innerHTML = originalHTML;
        alert('❌ No results found. Try searching manually.');
        return;
      }
      
      // Find best match (prefer complete albums/ep, then by size and seeders)
      const bestResult = data.results[0]; // Soulseek API should rank by relevance
      
      // Download the best match
      downloadSlskdFile(bestResult.username, bestResult.filename, bestResult.size || 0, btn, originalHTML);
    })
    .catch(error => {
      btn.disabled = false;
      btn.innerHTML = originalHTML;
      alert('❌ Search error: ' + error.message);
    });
}

function downloadSlskdFile(username, filename, size, btn, originalHTML) {
  fetch('/api/slskd/download-single', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      username: username,
      filename: filename,
      size: parseInt(size)
    })
  })
  .then(response => response.json())
  .then(data => {
    btn.disabled = false;
    if (data.error) {
      btn.innerHTML = originalHTML;
      alert('❌ Error: ' + data.error);
    } else {
      btn.innerHTML = '<i class="bi bi-check-circle"></i>';
      btn.classList.add('btn-success');
      btn.classList.remove('btn-outline-success');
      setTimeout(() => {
        btn.innerHTML = originalHTML;
        btn.classList.remove('btn-success');
        btn.classList.add('btn-outline-success');
      }, 2000);
      alert('✅ Download started!');
    }
  })
  .catch(error => {
    btn.disabled = false;
    btn.innerHTML = originalHTML;
    alert('❌ Network error: ' + error.message);
  });
}

function loadTracklist(artist, album, button = null, mbid = null) {
  const contentId = `tracklist-content-${artist.replace(/\s+/g, '_')}-${album.replace(/\s+/g, '_')}`;
  const contentDiv = document.getElementById(contentId);
  const spinner = button ? button.querySelector('.spinner-border') : null;
  
  if (!contentDiv) return;
  
  // Show spinner
  if (spinner) spinner.style.display = 'inline-block';
  if (button) button.disabled = true;
  
  // Build the tracklist fetch URL
  let tracklistUrl = `/api/album/tracklist?artist=${encodeURIComponent(artist)}&album=${encodeURIComponent(album)}`;
  if (mbid) {
    tracklistUrl += `&mbid=${encodeURIComponent(mbid)}`;
  }
  
  // Fetch and match tracklist
  Promise.all([
    fetch(tracklistUrl).then(r => r.json()),
    fetch(`/api/album/tracklist/match?artist=${encodeURIComponent(artist)}&album=${encodeURIComponent(album)}`).then(r => r.json())
  ])
  .then(([tracklistResponse, matchResponse]) => {
    if (spinner) spinner.style.display = 'none';
    if (button) button.disabled = false;
    
    if (tracklistResponse.error) {
      contentDiv.innerHTML = `<div class="alert alert-warning"><i class="bi bi-exclamation-triangle"></i> ${escapeHtml(tracklistResponse.error)}</div>`;
      return;
    }
    
    const tracklist = tracklistResponse.tracklist || [];
    const matched = new Set((matchResponse.matched || []).map(t => (t.title || '').toLowerCase()));
    const queued = new Set((matchResponse.queued || []).map(t => (t.title || '').toLowerCase()));
    
    if (tracklist.length === 0) {
      contentDiv.innerHTML = '<div class="alert alert-info">No tracks found</div>';
      return;
    }
    
    // Build tracklist HTML
    let html = `
      <div class="card border-info">
        <div class="card-header bg-info bg-opacity-10">
          <h6 class="mb-0"><i class="bi bi-list-ul"></i> Tracklist (${tracklist.length} tracks)</h6>
        </div>
        <div class="list-group list-group-flush">
    `;
    
    tracklist.forEach(track => {
      const normalizedTitle = (track.title || '').toLowerCase();
      const isQueued = queued.has(normalizedTitle);
      const isMatched = matched.has(normalizedTitle) && !isQueued;
      const matchBadge = isMatched
        ? '<span class="badge bg-success ms-2"><i class="bi bi-check-circle"></i> In Library</span>'
        : (isQueued
          ? '<span class="badge bg-warning text-dark ms-2"><i class="bi bi-hourglass-split"></i> In Queue</span>'
          : '');
      const rowClass = isQueued ? 'list-group-item-warning' : '';
      
      // Build search query for download (artist + title)
      const searchQuery = `${artist} ${track.title}`;
      
      // Add action buttons for tracks in library
      const actionButtons = isMatched ? `
        <div class="d-flex gap-2" style="flex-shrink: 0;">
          <button class="btn btn-sm btn-outline-primary" onclick="openEditTrackFromArtistModal('${escapeJsString(track.track_id || '')}', '${escapeJsString(track.title)}')" title="Edit track" style="flex-shrink: 0;">
            <i class="bi bi-pencil"></i>
          </button>
          <a href="/track/${escapeJsString(track.track_id || '')}" class="btn btn-sm btn-outline-info" title="View track page" style="flex-shrink: 0;">
            <i class="bi bi-arrow-right"></i>
          </a>
        </div>
      ` : '';
      
      html += `
        <div class="list-group-item ${rowClass}">
          <div class="d-flex justify-content-between align-items-center">
            <div class="flex-grow-1">
              <span class="text-muted small" style="min-width: 2em; text-align: right; display: inline-block;">
                ${String(track.position || '').padStart(2, '0')}
              </span>
              <span class="ms-2">${escapeHtml(track.title || '')}</span>
              ${matchBadge}
            </div>
            <div class="d-flex align-items-center gap-2">
              <small class="text-muted me-2" style="max-width: 120px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${escapeHtml(track.artist || '')}</small>
              ${actionButtons}
              <button class="btn btn-sm btn-outline-success" onclick="downloadTrack('${escapeJsString(searchQuery)}', '${escapeJsString(artist)}', '${escapeJsString(track.title)}')" title="Download this track" style="flex-shrink: 0;" ${isQueued ? 'disabled' : ''}>
                <i class="bi bi-download"></i>
              </button>
            </div>
          </div>
        </div>
      `;
    });
    
    html += `
        </div>
        <div class="card-footer text-muted small">
          <i class="bi bi-info-circle"></i> Green checkmarks = already in library. Yellow badges = already in queue. Click download button to add missing tracks from Soulseek.
        </div>
      </div>
    `;
    
    contentDiv.innerHTML = html;
  })
  .catch(error => {
    if (spinner) spinner.style.display = 'none';
    button.disabled = false;
    contentDiv.innerHTML = `<div class="alert alert-danger"><i class="bi bi-x-circle"></i> Error: ${escapeHtml(error.message)}</div>`;
  });
}

// Ensure a section has a Show/Hide-Missing toggle button.  The server only
// renders the button when the section already has missing rows (from the DB);
// when checkMissingReleases() injects missing rows into a section that had
// none, the button must be created here so the user can still collapse them.
function ensureToggleMissingButton(category) {
  const section = document.getElementById(category + '-section');
  if (!section) return;
  if (section.querySelector('.toggle-missing-btn')) return;

  const header = section.querySelector('.card-header');
  const actions = header && header.querySelector('.d-flex.gap-2');
  if (!actions) return;

  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'btn btn-sm btn-outline-secondary toggle-missing-btn';
  btn.onclick = () => toggleMissing(category);
  btn.title = 'Show/hide missing ' + category;
  btn.setAttribute('data-show', 'false');
  btn.innerHTML = '<i class="bi bi-eye-slash"></i> <span class="d-none d-sm-inline">Show Missing</span>';
  actions.appendChild(btn);
}

// Toggle missing items visibility
function toggleMissing(category) {
  const section = document.getElementById(category + '-section');
  if (!section) return;
  
  const accordion = section.querySelector('#accordion-' + category);
  const button = section.querySelector('.toggle-missing-btn');
  if (!accordion || !button) return;
  
  const isShowing = button.getAttribute('data-show') === 'true';
  const missingRows = accordion.querySelectorAll('.album-row[data-status="missing"]');
  
  if (isShowing) {
    // Hide missing
    missingRows.forEach(row => row.style.display = 'none');
    button.setAttribute('data-show', 'false');
    button.innerHTML = '<i class="bi bi-eye-slash"></i> <span class="d-none d-sm-inline">Show Missing</span>';
  } else {
    // Show missing
    missingRows.forEach(row => row.style.display = '');
    button.setAttribute('data-show', 'true');
    button.innerHTML = '<i class="bi bi-eye"></i> <span class="d-none d-sm-inline">Hide Missing</span>';
  }
  
  // Save preference to localStorage
  localStorage.setItem('showMissing-' + category, !isShowing);
}

// Initialize toggle states on page load
function initializeMissingToggle() {
  const categories = ['albums', 'compilations', 'live-albums', 'remix-albums', 'eps', 'singles'];
  categories.forEach(category => {
    const section = document.getElementById(category + '-section');
    if (!section) return;
    
    const accordion = section.querySelector('#accordion-' + category);
    const button = section.querySelector('.toggle-missing-btn');
    if (!accordion || !button) return;
    
    // Default is SHOWING missing rows (legacy parity: the old table-based
    // toggle selectors never matched anything, so missing albums were always
    // visible).  Only a stored "false" (user explicitly hid them) hides them
    // again — a fresh device must not look like albums vanished.
    const showMissing = localStorage.getItem('showMissing-' + category) !== 'false';
    const missingRows = accordion.querySelectorAll('.album-row[data-status="missing"]');
    
    if (!showMissing) {
      // Hide
      missingRows.forEach(row => row.style.display = 'none');
      button.setAttribute('data-show', 'false');
      button.innerHTML = '<i class="bi bi-eye-slash"></i> <span class="d-none d-sm-inline">Show Missing</span>';
    } else {
      // Show (default)
      missingRows.forEach(row => row.style.display = '');
      button.setAttribute('data-show', 'true');
      button.innerHTML = '<i class="bi bi-eye"></i> <span class="d-none d-sm-inline">Hide Missing</span>';
    }
  });
}

// Initialize on page load
document.addEventListener('DOMContentLoaded', initializeMissingToggle);

// Search for all releases on MusicBrainz for an artist
function searchMusicBrainzForAllReleases(artistName) {
  // Show the MusicBrainz modal if it exists
  const modalElement = document.getElementById('musicBrainzModal');
  if (!modalElement) {
    alert('MusicBrainz search modal not found. Please ensure the modal is properly included.');
    return;
  }
  
  // Reset modal content
  const mbSearchInfo = document.getElementById('mbSearchInfo');
  const mbSearchStatus = document.getElementById('mbSearchStatus');
  const mbSearchError = document.getElementById('mbSearchError');
  const mbSearchResults = document.getElementById('mbSearchResults');
  const mbSearchArtist = document.getElementById('mbSearchArtist');
  
  if (mbSearchInfo) mbSearchInfo.style.display = 'none';
  if (mbSearchError) mbSearchError.style.display = 'none';
  if (mbSearchResults) mbSearchResults.innerHTML = '';
  if (mbSearchStatus) mbSearchStatus.style.display = 'none';
  
  // Show the modal
  const hasBootstrapModal = !!(window.bootstrap && window.bootstrap.Modal);
  if (hasBootstrapModal) {
    const modal = new bootstrap.Modal(modalElement);
    modal.show();
  } else {
    // Fallback for environments where Bootstrap JS is not loaded.
    modalElement.style.display = 'block';
    modalElement.classList.add('show');
    modalElement.removeAttribute('aria-hidden');
    modalElement.setAttribute('aria-modal', 'true');
    document.body.classList.add('modal-open');
  }
  
  // Trigger the search after modal is shown
  setTimeout(() => {
    performMusicBrainzSearchForArtist(artistName);
  }, 500);
}

function performMusicBrainzSearchForArtist(artistName) {
  const mbSearchInfo = document.getElementById('mbSearchInfo');
  const mbSearchStatus = document.getElementById('mbSearchStatus');
  const mbSearchError = document.getElementById('mbSearchError');
  const mbSearchResults = document.getElementById('mbSearchResults');
  const mbSearchArtist = document.getElementById('mbSearchArtist');
  
  // Show search info
  if (mbSearchArtist) mbSearchArtist.textContent = artistName;
  if (mbSearchInfo) mbSearchInfo.style.display = 'block';
  if (mbSearchStatus) mbSearchStatus.style.display = 'block';
  
  // Fetch and display artist releases from MusicBrainz
  fetch('/api/artist/missing-releases?artist=' + encodeURIComponent(artistName))
    .then(r => r.json())
    .then(data => {
      if (mbSearchStatus) mbSearchStatus.style.display = 'none';
      
      // Handle errors
      if (data.error) {
        throw new Error(data.error);
      }
      
      const missing = data.missing || [];
      
      if (missing.length === 0) {
        if (mbSearchResults) {
          mbSearchResults.innerHTML = `
            <div class="alert alert-info">
              <i class="bi bi-info-circle"></i> No releases found for this artist on MusicBrainz, or all releases are already in your library.
            </div>
          `;
        }
        return;
      }
      
      // Group by type
      const byType = {
        Album: [],
        Compilation: [],
        'Live Album': [],
        EP: [],
        Single: [],
        Other: []
      };
      
      missing.forEach(release => {
        const type = release.category || release.primary_type || 'Album';
        if (!byType[type]) byType[type] = [];
        byType[type].push(release);
      });
      
      // Build results HTML
      let html = `
        <div class="accordion" id="mbResultsAccordion">
      `;
      
      Object.entries(byType).forEach(([type, releases]) => {
        if (releases.length === 0) return;
        
        const itemId = 'accordion-' + type.replace(/\s+/g, '-').toLowerCase();
        
        html += `
          <div class="accordion-item">
            <h2 class="accordion-header">
              <button class="accordion-button${type !== 'Album' ? ' collapsed' : ''}" type="button" data-bs-toggle="collapse" data-bs-target="#${itemId}" aria-expanded="${type === 'Album'}" aria-controls="${itemId}">
                <i class="bi bi-disc"></i> ${type} (${releases.length})
              </button>
            </h2>
            <div id="${itemId}" class="accordion-collapse ${type !== 'Album' ? 'collapse' : 'collapse show'}" data-bs-parent="#mbResultsAccordion">
              <div class="accordion-body p-0">
                <div class="list-group list-group-flush">
        `;
        
        releases.forEach(release => {
          const year = release.first_release_date ? release.first_release_date.substring(0, 4) : 'Unknown';
          const coverUrl = release.cover_art_url || '';
          const coverImg = coverUrl ? `<img src="${escapeHtml(coverUrl)}" alt="${escapeHtml(release.title)}" style="width: 40px; height: 40px; object-fit: cover; border-radius: 3px; margin-right: 0.5rem;">` : '<i class="bi bi-disc" style="font-size: 1.5rem; margin-right: 0.5rem; opacity: 0.5;"></i>';
          
          html += `
            <div class="list-group-item d-flex align-items-center justify-content-between">
              <div class="d-flex align-items-center" style="flex: 1; min-width: 0;">
                ${coverImg}
                <div style="min-width: 0; flex: 1;">
                  <div class="text-truncate"><strong>${escapeHtml(release.title)}</strong></div>
                  <small class="text-muted">${escapeHtml(year)}</small>
                </div>
              </div>
              <button class="btn btn-sm btn-outline-success ms-2" onclick="addMissingReleaseForDownload('${escapeJsString(release.release_id)}', '${escapeJsString(release.title)}', '${escapeJsString(artistName)}')" title="Add to query for download search">
                <i class="bi bi-download"></i> Download
              </button>
            </div>
          `;
        });
        
        html += `
                </div>
              </div>
            </div>
          </div>
        `;
      });
      
      html += `</div>`;
      
      if (mbSearchResults) mbSearchResults.innerHTML = html;
    })
    .catch(error => {
      if (mbSearchStatus) mbSearchStatus.style.display = 'none';
      if (mbSearchError) {
        mbSearchError.textContent = error.message;
        mbSearchError.style.display = 'block';
      }
    });
}

function searchMusicBrainzRelease(event, artist, album) {
  if (event) {
    event.preventDefault();
    event.stopPropagation();
  }
  // Canonical shared-modal flow: fills the search fields, opens the modal,
  // auto-searches, and queues the selected release via Soulseek.
  if (typeof window.openGlobalMbSearch === 'function') {
    window.openGlobalMbSearch(artist, album, function(selectedRelease) {
      if (!selectedRelease) return;
      if (typeof window.downloadMbRelease === 'function') {
        window.downloadMbRelease(selectedRelease.id, selectedRelease.title, selectedRelease.artist, 'slskd');
      } else if (typeof window.downloadReleaseViaSoulseek === 'function') {
        window.downloadReleaseViaSoulseek(selectedRelease.id, selectedRelease.title, selectedRelease.artist);
      }
    });
    return;
  }
  alert('MusicBrainz search is not available on this page.');
}

function addMissingReleaseForDownload(releaseId, releaseTitle, artistName) {
  // Use the MusicBrainz search flow (same as upcoming releases) to find and download the album.
  // This will search MusicBrainz for the specific release and show all available versions
  // with track listings, allowing the user to pick the right version to download.
  searchMusicBrainzRelease(null, artistName, releaseTitle);
}

// ── Favourite Artist ────────────────────────────────────────────────────────

async function loadArtistFavouriteState(artistName) {
  try {
    const resp = await fetch('/api/artist/favourite?artist=' + encodeURIComponent(artistName));
    const data = await resp.json();
    updateFavouriteButtonState(data.is_favourite);
  } catch (e) {
    console.warn('Could not load favourite state:', e);
  }
}

function updateFavouriteButtonState(isFavourite) {
  // Updates every favourite button on the page (desktop header + mobile hero).
  document.querySelectorAll('[data-artist-fav-btn]').forEach(function (btn) {
    if (btn.classList.contains('btn-link')) return; // borderless heart: fill via icon only
    if (isFavourite) {
      btn.classList.remove('btn-outline-danger');
      btn.classList.add('btn-danger');
    } else {
      btn.classList.remove('btn-danger');
      btn.classList.add('btn-outline-danger');
    }
  });
  document.querySelectorAll('[data-artist-fav-icon]').forEach(function (icon) {
    if (isFavourite) {
      icon.classList.remove('bi-heart');
      icon.classList.add('bi-heart-fill');
    } else {
      icon.classList.remove('bi-heart-fill');
      icon.classList.add('bi-heart');
    }
  });
  document.querySelectorAll('[data-artist-fav-label]').forEach(function (label) {
    label.textContent = isFavourite ? 'Favourited' : 'Favourite';
  });
}

async function toggleArtistFavourite(artistName) {
  try {
    const icon = document.querySelector('[data-artist-fav-icon]');
    const btn = document.querySelector('[data-artist-fav-btn]');
    const isFavourite = icon
      ? icon.classList.contains('bi-heart-fill')
      : !!(btn && btn.classList.contains('btn-danger'));

    if (isFavourite) {
      await fetch('/api/artist/favourite?artist=' + encodeURIComponent(artistName), { method: 'DELETE' });
      updateFavouriteButtonState(false);
    } else {
      await fetch('/api/artist/favourite', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ artist: artistName })
      });
      updateFavouriteButtonState(true);
    }
  } catch (e) {
    console.error('Error toggling favourite:', e);
    alert('Error updating favourite status: ' + e.message);
  }
}

// Hero bio "[more]" → jump to the full About tab.  The artist page uses the
// Bootstrap pill tab bar (#artistPageTabs); falls back to the old mobile
// tab engine if that bar isn't present.
function goToArtistAbout() {
  var btn = document.querySelector('#artistPageTabs [data-bs-target="#tab-about"]');
  if (btn) {
    if (window.bootstrap && bootstrap.Tab) {
      bootstrap.Tab.getOrCreateInstance(btn).show();
    } else {
      btn.click();
    }
    return;
  }
  var bar = document.getElementById('artistMobileTabBar');
  var oldBtn = bar && bar.querySelector('[data-tab="about"]');
  if (oldBtn) oldBtn.click();
}

// ── Artist Edit Track Modal (comprehensive) ────────────────────────────────
// Ported from old_system/templates/artist.html — the artist page renders
// "Edit track" buttons (loadTracklist) that need these handlers.

let editArtistTrackCurrentGenres = [];
let editArtistTrackModalInstance = null;

const ARTIST_MODAL_ADVANCED_FIELDS = [
  { name: 'writer', label: 'Writer/Lyricist' },
  { name: 'arranger', label: 'Arranger' },
  { name: 'mixer', label: 'Mixer' },
  { name: 'producer', label: 'Producer' },
  { name: 'work', label: 'Work/Composition' },
  { name: 'isrc', label: 'ISRC' },
  { name: 'bpm', label: 'BPM' },
  { name: 'bitrate', label: 'Bitrate (kbps)' },
  { name: 'sample_rate', label: 'Sample Rate (Hz)' },
  { name: 'titlesort', label: 'Title Sort' },
  { name: 'albumsort', label: 'Album Sort' },
  { name: 'artistsort', label: 'Artist Sort' },
  { name: 'composersort', label: 'Composer Sort' },
  { name: 'albumartistsort', label: 'Album Artist Sort' },
  { name: 'lyricistsort', label: 'Lyricist Sort' },
  { name: 'artistssort', label: 'Artists Sort' },
  { name: 'albumartistssort', label: 'Album Artists Sort' },
  { name: 'artists', label: 'Artists (multi)' },
  { name: 'albumartists', label: 'Album Artists (multi)' },
  { name: 'conductor', label: 'Conductor' },
  { name: 'performer', label: 'Performer' },
  { name: 'director', label: 'Director' },
  { name: 'djmixer', label: 'DJ Mixer' },
  { name: 'engineer', label: 'Engineer' },
  { name: 'remixer', label: 'Remixer' },
  { name: 'lyricist', label: 'Lyricist' },
  { name: 'albumversion', label: 'Album Version' },
  { name: 'recordlabel', label: 'Record Label' },
  { name: 'copyright', label: 'Copyright' },
  { name: 'releasedate', label: 'Release Date' },
  { name: 'releasetype', label: 'Release Type' },
  { name: 'releasestatus', label: 'Release Status' },
  { name: 'releasecountry', label: 'Release Country' },
  { name: 'media', label: 'Media Format' },
  { name: 'barcode', label: 'Barcode' },
  { name: 'catalognumber', label: 'Catalog Number' },
  { name: 'asin', label: 'ASIN' },
  { name: 'originalyear', label: 'Original Year' },
  { name: 'originaldate', label: 'Original Date' },
  { name: 'tracktotal', label: 'Track Total' },
  { name: 'disctotal', label: 'Disc Total' },
  { name: 'script', label: 'Script' },
  { name: 'discsubtitle', label: 'Disc Subtitle' },
  { name: 'subtitle', label: 'Subtitle' },
  { name: 'grouping', label: 'Grouping' },
  { name: 'movement', label: 'Movement' },
  { name: 'movementname', label: 'Movement Name' },
  { name: 'movementtotal', label: 'Movement Total' },
  { name: 'key', label: 'Musical Key' },
  { name: 'language', label: 'Language' },
  { name: 'license', label: 'License' },
  { name: 'website', label: 'Website' },
  { name: 'encodedby', label: 'Encoded By' },
  { name: 'encodersettings', label: 'Encoder Settings' },
  { name: 'explicitstatus', label: 'Explicit Status' },
  { name: 'musicbrainz_albumid', label: 'MusicBrainz Album ID' },
  { name: 'musicbrainz_artistid', label: 'MusicBrainz Artist ID' },
  { name: 'musicbrainz_albumartistid', label: 'MusicBrainz Album Artist ID' },
  { name: 'musicbrainz_releasegroupid', label: 'MusicBrainz Release Group ID' },
  { name: 'musicbrainz_releasetrackid', label: 'MusicBrainz Release Track ID' },
  { name: 'musicbrainz_workid', label: 'MusicBrainz Work ID' },
  { name: 'replaygain_track_gain', label: 'ReplayGain Track Gain' },
  { name: 'replaygain_track_peak', label: 'ReplayGain Track Peak' },
  { name: 'replaygain_album_gain', label: 'ReplayGain Album Gain' },
  { name: 'replaygain_album_peak', label: 'ReplayGain Album Peak' },
  { name: 'r128_track_gain', label: 'R128 Track Gain' },
  { name: 'r128_album_gain', label: 'R128 Album Gain' },
  { name: 'lyrics', label: 'Lyrics', type: 'textarea' }
];

function renderArtistAdvancedTrackFields(trackData) {
  const container = document.getElementById('editArtistTrackAdvancedFields');
  if (!container) return;

  container.innerHTML = ARTIST_MODAL_ADVANCED_FIELDS.map(def => {
    const fieldId = `editArtistTrackAdv_${def.name}`;
    if (def.type === 'textarea') {
      return `
        <div class="col-12">
          <label for="${fieldId}" class="form-label">${def.label}</label>
          <textarea class="form-control" id="${fieldId}" rows="4"></textarea>
        </div>
      `;
    }
    return `
      <div class="col-md-6">
        <label for="${fieldId}" class="form-label">${def.label}</label>
        <input type="text" class="form-control" id="${fieldId}">
      </div>
    `;
  }).join('');

  ARTIST_MODAL_ADVANCED_FIELDS.forEach(def => {
    const el = document.getElementById(`editArtistTrackAdv_${def.name}`);
    if (el) {
      const val = trackData?.[def.name];
      el.value = (val == null) ? '' : String(val);
    }
  });
}

function openEditTrackFromArtistModal(trackId, trackTitle) {
  if (!trackId) {
    alert('❌ Error: No track ID');
    return;
  }
  fetch(`/api/track/${encodeURIComponent(trackId)}`)
    .then(async r => {
      let data = null;
      try { data = await r.json(); } catch (_) { /* ignore */ }
      if (!r.ok) {
        throw new Error((data && data.error) ? data.error : 'Track not found');
      }
      return data;
    })
    .then(trackData => {
      if (!trackData) throw new Error('Empty response from server');
      if (trackData.track && !trackData.title) {
        trackData = Object.assign({}, trackData.track, trackData);
      }
      openComprehensiveEditArtistTrackModal(trackId, trackData);
    })
    .catch(err => {
      alert('❌ Error loading track: ' + err.message);
    });
}

function openComprehensiveEditArtistTrackModal(trackId, trackData) {
  trackData = trackData || {};

  // Set track ID
  document.getElementById('editArtistTrackId').value = trackId;

  // Populate form with track data
  document.getElementById('editArtistTrackTitle').textContent = trackData.title || 'Unknown';
  document.getElementById('editArtistTrackTitleField').value = trackData.title || '';
  document.getElementById('editArtistTrackArtistField').value = trackData.artist || '';
  document.getElementById('editArtistTrackAlbumField').value = trackData.album || '';
  document.getElementById('editArtistTrackYearField').value = trackData.year || '';
  document.getElementById('editArtistTrackStarsField').value = trackData.stars || 0;
  document.getElementById('editArtistTrackSingleField').value = trackData.is_single || 0;
  document.getElementById('editArtistTrackConfidenceField').value = trackData.single_confidence || 'low';
  document.getElementById('editArtistTrackAlbumArtistField').value = trackData.album_artist || '';
  document.getElementById('editArtistTrackComposerField').value = trackData.composer || '';
  document.getElementById('editArtistTrackTrackNumberField').value = trackData.track_number || '';
  document.getElementById('editArtistTrackDiscNumberField').value = trackData.disc_number || '';
  document.getElementById('editArtistTrackMBIDField').value = trackData.mbid || '';
  document.getElementById('editArtistTrackCommentField').value = trackData.comment || '';
  renderArtistAdvancedTrackFields(trackData);

  // Handle genres
  editArtistTrackCurrentGenres = [];
  if (trackData.genres) {
    editArtistTrackCurrentGenres = String(trackData.genres).split(/[;,\\\/]/).map(g => g.trim()).filter(g => g);
  }
  updateEditArtistTrackGenresDisplay();

  // Load and display recommended genres
  loadRecommendedGenresForArtistTrack(trackData.artist, trackId);

  // Show modal
  if (!editArtistTrackModalInstance) {
    editArtistTrackModalInstance = new bootstrap.Modal(document.getElementById('editTrackFromArtistModal'));
  }
  editArtistTrackModalInstance.show();
}

function loadRecommendedGenresForArtistTrack(artist, trackId) {
  const section = document.getElementById('artistRecommendedGenresSection');
  const display = document.getElementById('artistRecommendedGenresDisplay');

  if (!section || !display) return;

  fetch(`/api/genres/track/${encodeURIComponent(trackId)}`)
    .then(r => r.json())
    .then(data => {
      if (!data.genres) return;

      const recommendedGenres = new Map();
      const addAll = (list) => {
        (list || []).forEach(genre => {
          const name = typeof genre === 'object' ? genre.name : genre;
          if (name) recommendedGenres.set(name, (recommendedGenres.get(name) || 0) + 1);
        });
      };
      addAll(data.genres.lastfm_tags);
      addAll(data.genres.discogs_genres);

      if (recommendedGenres.size > 0) {
        section.style.display = 'block';
        let genresHtml = '';
        recommendedGenres.forEach((count, genre) => {
          genresHtml += `<button type="button" class="btn btn-sm btn-outline-info" onclick="addEditArtistGenreFromRecommended('${escapeJsString(genre)}')" title="Add to track genres">
            ${escapeHtml(genre)}
            <small class="text-muted ms-1">(${count})</small>
          </button>`;
        });
        display.innerHTML = genresHtml;
      } else {
        section.style.display = 'none';
      }
    })
    .catch(err => {
      section.style.display = 'none';
      console.warn('Could not load recommended genres:', err);
    });
}

function addEditArtistGenreFromRecommended(genre) {
  if (!editArtistTrackCurrentGenres.includes(genre)) {
    editArtistTrackCurrentGenres.push(genre);
    updateEditArtistTrackGenresDisplay();
  }
}

function updateEditArtistTrackGenresDisplay() {
  const container = document.getElementById('editArtistTrackGenresDisplay');
  if (!container) return;

  container.innerHTML = '';
  if (editArtistTrackCurrentGenres.length === 0) {
    container.innerHTML = '<span class="text-muted small">No genres set</span>';
  } else {
    editArtistTrackCurrentGenres.forEach(genre => {
      const badge = document.createElement('span');
      badge.className = 'badge bg-primary me-1 mb-1';
      badge.style.fontSize = '0.9rem';
      badge.innerHTML = `${escapeHtml(genre)} <button type="button" class="btn-close btn-close-white ms-1" style="font-size: 0.6rem;" onclick="removeEditArtistTrackGenre('${escapeJsString(genre)}')" aria-label="Remove"></button>`;
      container.appendChild(badge);
    });
  }
  document.getElementById('editArtistTrackGenresField').value = editArtistTrackCurrentGenres.join('\\');
}

function addEditArtistTrackGenre() {
  const input = document.getElementById('editArtistTrackGenreInput');
  if (!input) return;
  const genre = input.value.trim();

  if (!genre) return;

  if (!editArtistTrackCurrentGenres.includes(genre)) {
    editArtistTrackCurrentGenres.push(genre);
    updateEditArtistTrackGenresDisplay();
  }

  input.value = '';
  input.focus();
}

function removeEditArtistTrackGenre(genre) {
  editArtistTrackCurrentGenres = editArtistTrackCurrentGenres.filter(g => g !== genre);
  updateEditArtistTrackGenresDisplay();
}

function saveArtistEditedTrack() {
  const trackId = document.getElementById('editArtistTrackId').value;

  if (!trackId) {
    alert('❌ Error: No track ID');
    return;
  }

  const payload = {
    track_id: trackId,
    title: document.getElementById('editArtistTrackTitleField').value.trim(),
    artist: document.getElementById('editArtistTrackArtistField').value.trim(),
    album: document.getElementById('editArtistTrackAlbumField').value.trim(),
    year: document.getElementById('editArtistTrackYearField').value.trim() || null,
    stars: parseInt(document.getElementById('editArtistTrackStarsField').value) || 0,
    is_single: parseInt(document.getElementById('editArtistTrackSingleField').value) === 1,
    single_confidence: document.getElementById('editArtistTrackConfidenceField').value,
    genres: editArtistTrackCurrentGenres.join('\\'),
    album_artist: document.getElementById('editArtistTrackAlbumArtistField').value.trim() || null,
    composer: document.getElementById('editArtistTrackComposerField').value.trim() || null,
    track_number: document.getElementById('editArtistTrackTrackNumberField').value.trim() || null,
    disc_number: document.getElementById('editArtistTrackDiscNumberField').value.trim() || null,
    mbid: document.getElementById('editArtistTrackMBIDField').value.trim() || null,
    comment: document.getElementById('editArtistTrackCommentField').value.trim() || null,
    sync_to_file: true
  };

  ARTIST_MODAL_ADVANCED_FIELDS.forEach(def => {
    const el = document.getElementById(`editArtistTrackAdv_${def.name}`);
    if (!el) return;
    const raw = (el.value || '').trim();
    payload[def.name] = raw || null;
  });

  if (!payload.title) {
    alert('❌ Error: Title is required');
    return;
  }

  fetch('/api/track/update-metadata', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  })
  .then(r => r.json())
  .then(data => {
    if (data.success) {
      if (editArtistTrackModalInstance) {
        editArtistTrackModalInstance.hide();
      }
      if (data.file_synced === false) {
        alert('⚠️ Track metadata saved to database, but file tags were not updated. Check file permissions/path and logs.');
      } else {
        alert('✅ Track metadata updated successfully (database + file tags)');
      }
      setTimeout(() => { location.reload(); }, 500);
    } else {
      alert('❌ Error: ' + (data.error || 'Failed to update'));
    }
  })
  .catch(err => {
    alert('❌ Network error: ' + err.message);
  });
}

// ── Covered By Section ───────────────────────────────────────────────────────

async function loadArtistCoveredBy(artistName) {
  const container = document.getElementById('artistCoveredByContainer');
  const countEl = document.getElementById('artistCoveredByCount');
  if (!container) return;

  try {
    const resp = await fetch('/api/artist/covered-by?artist=' + encodeURIComponent(artistName));
    const raw = await resp.text();
    let data = {};

    try {
      data = raw ? JSON.parse(raw) : {};
    } catch (_) {
      const contentType = (resp.headers.get('content-type') || '').toLowerCase();
      if (contentType.includes('text/html') || raw.trim().startsWith('<!DOCTYPE') || raw.trim().startsWith('<html')) {
        throw new Error(`Server returned HTML instead of JSON (HTTP ${resp.status})`);
      }
      throw new Error(`Server returned non-JSON response (HTTP ${resp.status})`);
    }

    if (!resp.ok) {
      throw new Error(data.error || `HTTP ${resp.status}: ${resp.statusText}`);
    }

    const covers = data.covers || [];

    if (covers.length === 0) {
      container.innerHTML = `
        <div class="p-3 text-muted text-center">
          <i class="bi bi-info-circle"></i> No covers of ${escapeHtml(artistName)}'s songs found in your library yet.
          <br><small>Cover songs are detected automatically during popularity scans when lyricist/writer metadata is available.</small>
        </div>`;
      if (countEl) countEl.textContent = 'No covers found in library';
      return;
    }

    if (countEl) countEl.textContent = `${covers.length} cover${covers.length !== 1 ? 's' : ''} found in library`;

    let html = '<div class="table-responsive"><table class="table table-hover mb-0"><thead><tr>';
    html += '<th>Covering Artist</th><th>Song Title</th><th>Album</th><th class="text-center">Year</th></tr></thead><tbody>';

    covers.forEach(cover => {
      const artistUrl = `/artist/${encodeURIComponent(cover.artist)}`;
      const albumUrl = cover.album ? `/album/${encodeURIComponent(cover.artist)}/${encodeURIComponent(cover.album)}` : null;
      html += `<tr>
        <td><a href="${escapeHtml(artistUrl)}" class="text-decoration-none">${escapeHtml(cover.artist)}</a></td>
        <td>${escapeHtml(cover.title)}</td>
        <td>${albumUrl ? `<a href="${escapeHtml(albumUrl)}" class="text-decoration-none text-muted">${escapeHtml(cover.album)}</a>` : (cover.album ? escapeHtml(cover.album) : '—')}</td>
        <td class="text-center">${cover.year || '—'}</td>
      </tr>`;
    });

    html += '</tbody></table></div>';
    container.innerHTML = html;

  } catch (e) {
    console.error('Error loading covered-by data:', e);
    container.innerHTML = `<div class="p-3 text-danger text-center"><i class="bi bi-exclamation-triangle"></i> Error loading covers: ${escapeHtml(e.message)}</div>`;
  }
}

// ── Artist Corrections Banner + Missing Tracks Check ──────────────────────
(function initCorrectionsAndMissingTracks() {
  const artistNameEl = document.querySelector('[data-artist-name]');
  const artistNameForCorr = artistNameEl ? artistNameEl.dataset.artistName : '';
  if (!artistNameForCorr) return;

  // Show corrections link (always visible; the dot lights up when issues exist)
  const banner = document.getElementById('artist-corrections-banner');
  if (banner) banner.style.display = 'block';

  // Async: check albums with MB MBIDs for missing tracks
  const mbRows = document.querySelectorAll('tr[data-mb-mbid]');
  if (!mbRows || mbRows.length === 0) return;

  const checks = Array.from(mbRows).map(row => {
    const albumName = row.dataset.albumName;
    const mbMbid = row.dataset.mbMbid;
    if (!albumName || !mbMbid) return Promise.resolve(0);

    return fetch(`/api/album/missing-tracks?artist=${encodeURIComponent(artistNameForCorr)}&album=${encodeURIComponent(albumName)}`)
      .then(r => r.ok ? r.json() : { missing_count: 0 })
      .then(data => {
        const count = data.missing_count || 0;
        if (count > 0) {
          // Show badge on the status cell of this album row
          const badge = row.querySelector('.album-missing-tracks-badge');
          if (badge) {
            badge.textContent = `${count} track${count !== 1 ? 's' : ''} missing`;
            badge.style.display = 'inline-block';
          }
          // Show the "Search missing tracks" button
          const searchBtn = row.querySelector('.album-search-missing-btn');
          if (searchBtn) searchBtn.style.display = 'inline-block';
        }
        return count;
      })
      .catch(() => 0);
  });

  Promise.all(checks).then(counts => {
    const totalMissingTracks = counts.reduce((a, b) => a + b, 0);
    if (totalMissingTracks > 0) {
      const badge = document.getElementById('artist-missing-tracks-badge');
      if (badge) {
        badge.textContent = `${totalMissingTracks} track${totalMissingTracks !== 1 ? 's' : ''} missing`;
        badge.style.display = 'inline-block';
      }
      const dot = document.getElementById('artistCorrectionsDot');
      if (dot) dot.style.display = 'inline-block';
    }
  });
})();

// ═══════════════════════════════════════════════════════════════════════════
// Reorganisation Plan (#867) — Tools dropdown, filter bar, mobile tabs,
// bio Read More, accordion single-expansion, one-tap quick queue.
// ═══════════════════════════════════════════════════════════════════════════

// Force Metadata Refresh: tick the Force checkbox on the scan form and submit.
function forceArtistMetadataRefresh() {
  const form = document.getElementById('artistScanForm');
  if (!form) return;
  const force = document.getElementById('artistForceScan');
  if (force) force.checked = true;
  form.submit();
}

// Bio Read More toggle (3-line clamp ⇄ full text).
function toggleArtistBio() {
  const clamp = document.getElementById('artistBioClamp');
  const btn = document.getElementById('artistBioToggle');
  if (!clamp || !btn) return;
  const expanded = clamp.classList.toggle('expanded');
  btn.innerHTML = expanded
    ? '<i class="bi bi-chevron-contract me-1"></i>Read Less'
    : '<i class="bi bi-chevron-expand me-1"></i>Read More';
}

// Album status filter: 'all' | 'library' | 'missing'
function setArtistFilter(filter) {
  document.querySelectorAll('.artist-filter-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.filter === filter);
  });
  document.querySelectorAll('.album-row').forEach(row => {
    const status = row.dataset.status || 'discovered';
    if (filter === 'all') {
      row.style.display = '';
      return;
    }
    if (filter === 'library') {
      row.style.display = status === 'missing' ? 'none' : '';
      return;
    }
    if (filter === 'missing') {
      row.style.display = status === 'missing' ? '' : 'none';
    }
  });
  // Collapse any open accordions when switching filters so hidden rows don't
  // leave orphaned open bodies behind.
  if (typeof window.bootstrap !== 'undefined') {
    document.querySelectorAll('.album-row .accordion-collapse.show').forEach(el => {
      bootstrap.Collapse.getInstance(el)?.hide();
    });
  }
}

// Mobile 4-tab navigation now runs from the shared engine in main.js
// (``[data-mobile-tabs]`` bars) — see initMobileTabs().

// Accordion single-expansion across all artist release categories: opening an
// album tracklist collapses every other open album tracklist on the page.
function initArtistSingleExpansion() {
  document.addEventListener('click', (e) => {
    const toggleBtn = e.target.closest('.accordion-chevron-btn');
    if (!toggleBtn) return;
    const targetId = toggleBtn.getAttribute('data-bs-target');
    if (!targetId) return;
    // Only act when we're about to expand (aria-expanded flips after the click).
    setTimeout(() => {
      const opened = document.querySelector('.album-row .accordion-collapse.show');
      if (!opened) return;
      const openedPanel = opened.closest('.album-row');
      document.querySelectorAll('.album-row .accordion-collapse.show').forEach(panel => {
        if (panel === opened) return;
        if (window.bootstrap) bootstrap.Collapse.getInstance(panel)?.hide();
      });
    }, 50);
  });
}

// Sticky toast feedback bar (One-Tap Queue feedback).
function showQueueToast(message) {
  let toast = document.getElementById('queueToast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'queueToast';
    toast.className = 'queue-toast';
    toast.innerHTML = '<span class="queue-toast-msg"></span><span class="queue-toast-close" onclick="dismissQueueToast()"><i class="bi bi-x-lg"></i></span>';
    document.body.appendChild(toast);
  }
  toast.querySelector('.queue-toast-msg').textContent = message;
  toast.style.display = 'flex';
  clearTimeout(window.__queueToastTimer);
  window.__queueToastTimer = setTimeout(dismissQueueToast, 4000);
}

function dismissQueueToast() {
  const toast = document.getElementById('queueToast');
  if (toast) toast.style.display = 'none';
}

// One-Tap Quick Queue: send a MusicBrainz release straight to the download
// queue using the default quality profile (no confirmation modal).
function quickQueueRelease(releaseId, releaseTitle, artist) {
  // Route through the Release Picker flyout so the user can choose the exact
  // version (15-track CD vs 5-track promo) before anything hits the queue.
  if (typeof window.openReleasePicker === 'function') {
    window.openReleasePicker(releaseId, releaseTitle, artist);
    return;
  }
  showQueueToast('⚠️ Release picker unavailable — cannot queue');
}

// Play the artist's top tracks in the built-in player (Zone D primary CTA).
function playArtistTopTracks() {
  const tracks = window._artistPlaylist || [];
  if (typeof Player === 'undefined' || typeof Player.playQueue !== 'function') {
    showQueueToast('⚠️ Player unavailable');
    return;
  }
  if (!tracks.length) {
    showQueueToast('⚠️ No playable tracks for this artist');
    return;
  }
  Player.playQueue(tracks);
}

// Init: single-expansion listener.  Mobile tabs are driven by the shared
// engine in main.js ([data-mobile-tabs]).
document.addEventListener('DOMContentLoaded', () => {
  initArtistSingleExpansion();
});





