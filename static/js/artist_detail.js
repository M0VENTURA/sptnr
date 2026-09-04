// Artist Detail Page JS

const SLSKD_MAX_POLL_ATTEMPTS = 60;
const SLSKD_POLL_INTERVAL_MS = 2000;
const BYTES_TO_MB = 1024 * 1024;

// Event listener for Soulseek tab - show track queue option when tab is clicked
document.addEventListener('DOMContentLoaded', function() {
  const slskdTab = document.getElementById('slskd-tab');
  if (slskdTab) {
    slskdTab.addEventListener('shown.bs.tab', function() {
      if (typeof currentDownloadAlbum !== 'undefined' && currentDownloadAlbum.album) {
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
        alert(`Would remove genres: ${genres.join(', ')}`);
    };
}

// ===== Artist IDs Modal Functions =====
function openEditArtistIdsModal() {
  const artistName = window._pd ? window._pd.artistName : (window.artistName || '');
  const mbEl = document.getElementById('musicbrainzArtistId');
  const dcEl = document.getElementById('discogsArtistId');
  const musicbrainzId = mbEl ? mbEl.textContent.trim() : '';
  const discogsId = dcEl ? dcEl.textContent.trim() : '';
  
  const modalHtml = `
    <div class="modal fade" id="editArtistIdsModal" tabindex="-1">
      <div class="modal-dialog">
        <div class="modal-content bg-dark text-light border-secondary">
          <div class="modal-header border-secondary">
            <h5 class="modal-title"><i class="bi bi-pencil"></i> Edit Artist IDs</h5>
            <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
          </div>
          <div class="modal-body">
            <div class="mb-3">
              <label for="editMusicbrainzArtistId" class="form-label">MusicBrainz Artist ID</label>
              <input type="text" class="form-control bg-dark text-light border-secondary" id="editMusicbrainzArtistId" value="${escapeHtml(musicbrainzId === 'Not linked' ? '' : musicbrainzId)}" placeholder="e.g., a74b1b7f-71a5-4011-9441-d0b5e4122711">
              <div class="form-text">
                <a href="https://musicbrainz.org/search?query=${encodeURIComponent(artistName)}&type=artist" target="_blank">Search MusicBrainz</a> to find the artist ID
              </div>
            </div>
            <div class="mb-3">
              <label for="editDiscogsArtistId" class="form-label">Discogs Artist ID</label>
              <input type="text" class="form-control bg-dark text-light border-secondary" id="editDiscogsArtistId" value="${escapeHtml(discogsId === 'Not linked' ? '' : discogsId)}" placeholder="e.g., 123456">
              <div class="form-text">
                <a href="https://www.discogs.com/search/?q=${encodeURIComponent(artistName)}&type=artist" target="_blank">Search Discogs</a> to find the artist ID
              </div>
            </div>
          </div>
          <div class="modal-footer border-secondary">
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
  const artistName = window._pd ? window._pd.artistName : (window.artistName || '');
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
        const mbEl = document.getElementById('musicbrainzArtistId');
        const dcEl = document.getElementById('discogsArtistId');
        if (mbEl) mbEl.textContent = musicbrainzId || 'Not linked';
        if (dcEl) dcEl.textContent = discogsId || 'Not linked';
        
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
  const artistName = window._pd ? window._pd.artistName : (window.artistName || '');
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
      if (readonlyMb && mbid) readonlyMb.textContent = mbid;
      if (readonlyDc && discogs) readonlyDc.textContent = discogs;

      alert('✅ Lookup complete and IDs saved.');
    })
    .catch(err => {
      alert('❌ Network error: ' + err.message);
    })
    .finally(() => {
      button.disabled = false;
      button.innerHTML = originalHtml;
    });
}

// ===== Utility and Feature Functions =====
function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

function escapeJsString(str) {
  if (!str) return '';
  return str.replace(/\\/g, '\\\\')
            .replace(/'/g, "\\'")
            .replace(/"/g, '\\"')
            .replace(/\n/g, '\\n')
            .replace(/\r/g, '\\r');
}

function sanitizeBio(text) {
  return escapeHtml(text)
    .replace(/&lt;br\s*\/?&gt;/gi, '<br>')
    .replace(/\n/g, '<br>');
}

// Toggle missing releases for an individual category section
function toggleMissingReleasesForCategory(btn, catId) {
  const section = document.getElementById(catId + '-section');
  if (!section) return;
  const missingRows = section.querySelectorAll('.missing-album-item');
  const isHidden = btn.getAttribute('data-hidden') === 'true';

  missingRows.forEach(row => {
    row.style.display = isHidden ? '' : 'none';
  });

  if (isHidden) {
    btn.setAttribute('data-hidden', 'false');
    btn.innerHTML = '<i class="bi bi-eye-slash me-1"></i><span>Hide Missing</span>';
  } else {
    btn.setAttribute('data-hidden', 'true');
    btn.innerHTML = '<i class="bi bi-eye me-1"></i><span>Show Missing</span>';
  }
}
window.toggleMissingReleasesForCategory = toggleMissingReleasesForCategory;

function forceArtistMetadataRefresh() {
  const form = document.getElementById('artistScanForm');
  if (!form) return;
  const force = document.getElementById('artistForceScan');
  if (force) force.checked = true;
  form.submit();
}

function toggleArtistBio() {
  const clamp = document.getElementById('artistBioClamp');
  const btn = document.getElementById('artistBioToggle');
  if (!clamp || !btn) return;
  const expanded = clamp.classList.toggle('expanded');
  btn.innerHTML = expanded
    ? '<i class="bi bi-chevron-contract me-1"></i>Read Less'
    : '<i class="bi bi-chevron-expand me-1"></i>Read More';
}

function setArtistFilter(filter) {
  const currentlyActive = document.querySelector('.artist-filter-btn.active')?.dataset?.filter;
  const effective = (filter === currentlyActive && filter !== 'all') ? 'all' : filter;

  document.querySelectorAll('.artist-filter-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.filter === effective);
  });
  document.querySelectorAll('.album-row').forEach(row => {
    const status = row.dataset.status || 'discovered';
    if (effective === 'all') {
      row.style.display = '';
      return;
    }
    if (effective === 'library') {
      row.style.display = status === 'missing' ? 'none' : '';
      return;
    }
    if (effective === 'missing') {
      row.style.display = status === 'missing' ? '' : 'none';
    }
  });
  
  if (typeof window.bootstrap !== 'undefined') {
    document.querySelectorAll('.album-row .accordion-collapse.show').forEach(el => {
      bootstrap.Collapse.getInstance(el)?.hide();
    });
  }

  document.querySelectorAll('.category-section').forEach(section => {
    const rows = Array.from(section.querySelectorAll('.album-row'));
    const hasVisible = rows.some(row => row.style.display !== 'none');
    section.style.display = rows.length && !hasVisible ? 'none' : '';
  });
}

function playArtistTopTracks() {
  const tracks = window._artistPlaylist || [];
  if (typeof Player === 'undefined' || typeof Player.playQueue !== 'function') {
    alert('Player unavailable');
    return;
  }
  if (!tracks.length) {
    alert('No playable tracks for this artist');
    return;
  }
  Player.playQueue(tracks);
}

function goToArtistAbout() {
  var btn = document.querySelector('#artistPageTabs [data-bs-target="#tab-about"]');
  if (btn) {
    if (window.bootstrap && bootstrap.Tab) {
      bootstrap.Tab.getOrCreateInstance(btn).show();
    } else {
      btn.click();
    }
  }
}

async function loadArtistCoveredBy(artistName) {
  const container = document.getElementById('artistCoveredByContainer');
  const countEl = document.getElementById('artistCoveredByCount');
  if (!container) return;

  try {
    const resp = await fetch('/api/artist/covered-by?artist=' + encodeURIComponent(artistName));
    const data = await resp.json();
    const covers = data.covers || [];

    if (covers.length === 0) {
      container.innerHTML = `<div class="p-3 text-muted text-center"><i class="bi bi-info-circle"></i> No covers found in library.</div>`;
      if (countEl) countEl.textContent = 'No covers found';
      return;
    }

    if (countEl) countEl.textContent = `${covers.length} cover(s) found`;
    let html = '<div class="table-responsive"><table class="table table-hover mb-0"><thead><tr><th>Covering Artist</th><th>Song Title</th><th>Album</th><th class="text-center">Year</th></tr></thead><tbody>';
    
    covers.forEach(cover => {
      html += `<tr>
        <td>${escapeHtml(cover.artist)}</td>
        <td>${escapeHtml(cover.title)}</td>
        <td>${escapeHtml(cover.album || '—')}</td>
        <td class="text-center">${cover.year || '—'}</td>
      </tr>`;
    });

    html += '</tbody></table></div>';
    container.innerHTML = html;
  } catch (e) {
    container.innerHTML = `<div class="p-3 text-danger text-center">Error loading covers</div>`;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  const artistName = window._pd ? window._pd.artistName : (window.artistName || '');
  if (artistName && typeof loadArtistCoveredBy === 'function') {
    loadArtistCoveredBy(artistName);
  }
});
