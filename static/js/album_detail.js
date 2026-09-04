// ===== Album Detail Page JavaScript =====

var _pageData = window._pageData || {};
let currentEditTrackId = null;
let currentAlbumGenres = new Set();
let mbComparisonData = null;

// Built-in Player Helpers
function playTrackFromAlbum(btn) {
  if (typeof Player === 'undefined') return;
  Player.playTrack(
    btn.dataset.trackId,
    btn.dataset.title,
    btn.dataset.artist,
    btn.dataset.art
  );
}

function playAlbum() {
  if (typeof Player === 'undefined') return;
  const rows = document.querySelectorAll('#albumTracksTbody tr[data-track-id]');
  const tracks = [];

  rows.forEach(row => {
    const trackId = row.dataset.trackId;
    if (!trackId) return;
    tracks.push({
      id: trackId,
      title: row.dataset.trackTitle || '',
      artist: row.dataset.trackArtist || '',
      albumArtUrl: document.getElementById('albumArtImage')?.src || ''
    });
  });

  if (!tracks.length) {
    alert('No tracks found on this album.');
    return;
  }
  Player.playQueue(tracks);
}

// MusicBrainz Album Lookup & Field Population
function openAlbumLookupModal() {
  const artist = _pageData.artistName || '';
  const album = _pageData.albumName || '';

  window._mbSearchCallback = function (selected) {
    window._mbSearchCallback = null;
    if (!selected) return;

    const group = selected.release || {};
    const rgMbid = group.id || selected.id;
    const releaseMbid = (selected.id && selected.id !== rgMbid) ? selected.id : '';
    const year = (group.first_release_date || '').toString().split('-')[0] || '';
    const albumType = _buildAlbumType(group.primary_type, group.secondary_types);
    const cover = group.cover_art_url || '';
    const albumArtist = selected.artist || group.artist || '';

    populateAlbumFields(selected.title || group.title || album, year, albumType, releaseMbid, '', cover, '', rgMbid, albumArtist);
  };

  window._mbSearchIncludeOwned = true;
  window._mbSearchWithReleases = true;

  if (typeof window.openGlobalMbSearch === 'function') {
    window.openGlobalMbSearch(artist, album, window._mbSearchCallback, '', '');
  } else {
    alert('Search modal not initialized.');
  }
}

function _buildAlbumType(primaryType, secondaryTypes) {
  const primary = (primaryType || 'album').toLowerCase().trim();
  const secondary = (secondaryTypes || []).map(s => s.toLowerCase().trim());
  const displayable = secondary.find(s => ['compilation', 'live', 'remix', 'soundtrack', 'spokenword', 'ep', 'single'].includes(s));
  if (displayable && primary === 'album') return `${primary}+${displayable}`;
  return primary;
}

function populateAlbumFields(title, year, albumType, mbid, discogsId, coverArtUrl, genres, releaseGroupMbid, albumArtist) {
  const mbModal = document.getElementById('musicBrainzModal');
  if (mbModal && window.bootstrap) {
    bootstrap.Modal.getInstance(mbModal)?.hide();
  }

  if (title) document.getElementById('album_title').value = title;
  if (albumArtist) document.getElementById('album_artist').value = albumArtist;
  if (year) document.getElementById('release_year').value = year;
  if (mbid) document.getElementById('album_mbid').value = mbid;
  if (releaseGroupMbid) {
    const rgEl = document.getElementById('album_release_group_mbid');
    if (rgEl) rgEl.value = releaseGroupMbid;
  }
  if (coverArtUrl) document.getElementById('cover_art_url').value = coverArtUrl;

  if (albumType) {
    const select = document.getElementById('album_type');
    if (select) select.value = albumType;
  }

  if (window.markFormDirty) window.markFormDirty('albumMetadataForm');
}

// Genre Staging Functions
function addAlbumGenre() {
  const input = document.getElementById('newAlbumGenreInput');
  const genre = (input.value || '').trim();
  if (!genre) return;

  if (!currentAlbumGenres.has(genre)) {
    currentAlbumGenres.add(genre);
    updateAlbumGenresDisplay();
  }
  input.value = '';
}

function stageRemoveAlbumGenre(genre) {
  if (!genre) return;
  currentAlbumGenres.delete(genre);
  updateAlbumGenresDisplay();
}

function updateAlbumGenresDisplay() {
  const container = document.getElementById('albumGenresContainer');
  const hiddenInput = document.getElementById('album_genres');
  if (!container || !hiddenInput) return;

  if (currentAlbumGenres.size === 0) {
    container.innerHTML = '<span class="text-muted small">No genres set</span>';
    hiddenInput.value = '';
  } else {
    let html = '';
    Array.from(currentAlbumGenres).sort().forEach(g => {
      html += `<span class="badge bg-primary me-1 mb-1">${escapeHtml(g)} <button type="button" class="btn-close btn-close-white ms-1" style="font-size:0.6rem;" onclick="stageRemoveAlbumGenre('${escapeJsString(g)}')"></button></span>`;
    });
    container.innerHTML = html;
    hiddenInput.value = Array.from(currentAlbumGenres).join(', ');
  }
  if (window.markFormDirty) window.markFormDirty('albumMetadataForm');
}

function fetchGenreRecommendations() {
  const btn = document.getElementById('fetchGenresBtn');
  const section = document.getElementById('recommendedGenresSection');
  const container = document.getElementById('recommendedGenres');
  if (!btn || !section || !container) return;

  section.style.display = 'block';
  container.innerHTML = '<span class="text-muted small">Fetching recommendations...</span>';
  btn.disabled = true;

  fetch(`/api/album/recommendations?artist=${encodeURIComponent(_pageData.artistName)}&album=${encodeURIComponent(_pageData.albumName)}`)
    .then(r => r.json())
    .then(data => {
      btn.disabled = false;
      const recs = data.recommendations || data.genres || [];
      if (!recs.length) {
        container.innerHTML = '<span class="text-muted small">No recommendations found.</span>';
        return;
      }
      container.innerHTML = recs.map(g => `<span class="badge bg-info text-dark" style="cursor:pointer;" onclick="stageAddGenre('${escapeJsString(g)}')"><i class="bi bi-plus"></i> ${escapeHtml(g)}</span>`).join(' ');
    })
    .catch(err => {
      btn.disabled = false;
      container.innerHTML = '<span class="text-danger small">Failed to load suggestions.</span>';
    });
}

function stageAddGenre(g) {
  if (!currentAlbumGenres.has(g)) {
    currentAlbumGenres.add(g);
    updateAlbumGenresDisplay();
  }
}

function applySelectedAlbumSourceTags() {
  const checked = document.querySelectorAll('.album-source-tag-check:checked');
  checked.forEach(cb => {
    if (cb.value) currentAlbumGenres.add(cb.value.trim());
    cb.checked = false;
  });
  updateAlbumGenresDisplay();
  document.querySelectorAll('.album-apply-source-tags-btn').forEach(b => b.style.display = 'none');
}

// Similar Artists
async function loadSimilarArtistsForAlbum(artist) {
  const container = document.getElementById('albumSimilarArtistsContainer');
  if (!container) return;

  try {
    const resp = await fetch(`/api/artist/${encodeURIComponent(artist)}/similar`);
    const data = await resp.json();
    const lastfm = (data.similar_artists && data.similar_artists.lastfm) || [];
    const listenbrainz = (data.similar_artists && data.similar_artists.listenbrainz) || [];
    const all = [...lastfm, ...listenbrainz];

    if (!all.length) {
      container.innerHTML = '<div class="alert alert-secondary bg-dark border-secondary text-muted small">No similar artists found.</div>';
      return;
    }

    const seen = new Set();
    const unique = all.filter(a => {
      const name = (typeof a === 'string' ? a : a.name || '').trim();
      if (!name || seen.has(name.toLowerCase())) return false;
      seen.add(name.toLowerCase());
      return true;
    });

    let html = '<div class="row g-2">';
    unique.slice(0, 12).forEach(item => {
      const name = typeof item === 'string' ? item : item.name;
      html += `
        <div class="col-6 col-md-3">
          <a href="/artist/${encodeURIComponent(name)}" class="card p-2 text-decoration-none text-light bg-dark border-secondary h-100">
            <span class="fw-bold text-truncate">${escapeHtml(name)}</span>
          </a>
        </div>`;
    });
    html += '</div>';
    container.innerHTML = html;
  } catch (e) {
    container.innerHTML = '<div class="alert alert-danger py-2 small">Error loading similar artists.</div>';
  }
}

// Utilities & Init
function goToAlbumGenres() {
  const tabBtn = document.getElementById('tab-genres-btn');
  if (tabBtn && window.bootstrap) {
    bootstrap.Tab.getOrCreateInstance(tabBtn).show();
  }
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text || '';
  return div.innerHTML;
}

function escapeJsString(str) {
  if (!str) return '';
  return str.replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/"/g, '\\"');
}

document.addEventListener('DOMContentLoaded', () => {
  // Prepopulate existing album genres
  const genresVal = document.getElementById('album_genres')?.value || '';
  if (genresVal) {
    currentAlbumGenres = new Set(genresVal.split(',').map(s => s.trim()).filter(Boolean));
    updateAlbumGenresDisplay();
  }

  // Load similar artists automatically when clicking the similar tab
  const similarTab = document.getElementById('tab-similar-btn');
  if (similarTab) {
    similarTab.addEventListener('shown.bs.tab', () => {
      if (_pageData.artistName) loadSimilarArtistsForAlbum(_pageData.artistName);
    }, { once: true });
  }

  // Monitor checkbox changes inside detected genres
  document.addEventListener('change', e => {
    if (e.target && e.target.classList.contains('album-source-tag-check')) {
      const pane = e.target.closest('.tab-pane');
      if (!pane) return;
      const btn = pane.querySelector('.album-apply-source-tags-btn');
      if (btn) btn.style.display = pane.querySelectorAll('.album-source-tag-check:checked').length > 0 ? '' : 'none';
    }
  });
});
