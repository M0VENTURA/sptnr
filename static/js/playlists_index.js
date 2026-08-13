// ===============================
// PLAYLISTS INDEX - JAVASCRIPT
// ===============================
// Rebuilt /playlists page: list all smart + regular playlists, view
// tracks, and rename playlists (name + .nsp file name for smart ones).

// ===============================
// STATE
// ===============================
let playlists = [];
let currentPlaylist = null;
let currentTracks = [];
let _playlistFilter = '';
let _csvImportData = null;
const compactView = window.matchMedia('(max-width: 767.98px)');

// ===============================
// UTILITIES
// ===============================

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text == null ? '' : String(text);
  return div.innerHTML;
}

// Guarantee JSON: an HTML response (404 page, stale build) must surface a
// readable message instead of "Unexpected token '<' ... is not valid JSON".
async function parseJsonOrThrow(response) {
  const contentType = (response.headers.get('content-type') || '').toLowerCase();
  if (!contentType.includes('application/json')) {
    const text = await response.text().catch(() => '');
    if (text.trim().startsWith('<!DOCTYPE') || text.trim().startsWith('<html')) {
      throw new Error(
        'Server returned HTML (HTTP ' + response.status + ') — the API route may be ' +
        'unavailable in this build. Check /logs for the error.'
      );
    }
    throw new Error('Server returned HTTP ' + response.status + ' ' + response.statusText);
  }
  return response.json();
}

// ── Mobile panel switching (single panel at a time < 992px) ──
function switchToDetail() {
  if (window.innerWidth < 992) {
    document.getElementById('playlistListPanel').classList.add('d-none');
    document.getElementById('playlistDetailPanel').classList.remove('d-none');
  }
}

function showPlaylistList() {
  document.getElementById('playlistListPanel').classList.remove('d-none');
  document.getElementById('playlistDetailPanel').classList.add('d-none');
}

function formatDuration(seconds) {
  seconds = Number(seconds) || 0;
  const mins = Math.floor(seconds / 60);
  const secs = Math.floor(seconds % 60);
  return `${mins}:${secs.toString().padStart(2, '0')}`;
}

function showAlert(message, isError) {
  const container = document.getElementById('playlistsAlert');
  container.innerHTML = `
    <div class="alert ${isError ? 'alert-danger' : 'alert-success'} alert-dismissible fade show py-2 small">
      ${escapeHtml(message)}
      <button type="button" class="btn-close py-2" data-bs-dismiss="alert" aria-label="Close"></button>
    </div>`;
}

function playlistIcon(playlist) {
  return playlist.type === 'smart' ? 'bi-stars text-warning' : 'bi-collection-play text-primary';
}

// ===============================
// LISTING
// ===============================

async function loadPlaylists() {
  const listEl = document.getElementById('playlistList');
  listEl.innerHTML = '<p class="text-center text-muted py-4 small">Loading playlists…</p>';
  currentPlaylist = null;
  resetDetail();

  try {
    const response = await fetch('/api/playlists/all');
    const data = await parseJsonOrThrow(response);
    if (!response.ok) throw new Error(data.error || 'Failed to load playlists');
    playlists = data.playlists || [];
    renderList();
    showPlaylistList();
  } catch (err) {
    console.error('loadPlaylists:', err);
    listEl.innerHTML = '<p class="text-center text-muted py-4 small">Could not load playlists</p>';
    showAlert(err.message || 'Could not load playlists', true);
  }
}

function renderList() {
  const listEl = document.getElementById('playlistList');
  const needle = _playlistFilter.trim().toLowerCase();
  const filtered = needle
    ? playlists.filter(p => (p.name || '').toLowerCase().includes(needle))
    : playlists;
  const smart = filtered.filter(p => p.type === 'smart');
  const regular = filtered.filter(p => p.type === 'regular');

  document.getElementById('smartCount').textContent = smart.length;
  document.getElementById('regularCount').textContent = regular.length;

  listEl.innerHTML = '';

  const renderGroup = (label, items) => {
    if (!items.length) return;
    listEl.insertAdjacentHTML('beforeend', `
      <div class="small fw-semibold text-muted text-uppercase mt-1 mb-1 px-1">${escapeHtml(label)}</div>
    `);
    items.forEach(item => listEl.appendChild(playlistCard(item)));
  };

  renderGroup('Smart Playlists', smart);
  renderGroup('Regular Playlists', regular);

  if (!filtered.length) {
    listEl.innerHTML = '<p class="text-center text-muted py-4 small">' +
      (needle ? 'No playlists match "' + escapeHtml(_playlistFilter.trim()) + '"' : 'No playlists found') +
      '</p>';
  }
}

function applyPlaylistFilter(value) {
  _playlistFilter = value || '';
  renderList();
}

function playlistCard(playlist) {
  const card = document.createElement('div');
  const isActive = currentPlaylist && currentPlaylist.id === playlist.id && currentPlaylist.source === playlist.source;
  const fileHint = playlist.file_name ? `<small class="text-muted d-block text-truncate" title="${escapeHtml(playlist.file_name)}">${escapeHtml(playlist.file_name)}</small>` : '';
  const countBadge = playlist.rule_based
    ? '<span class="badge bg-info-subtle text-info-emphasis text-nowrap">Rules</span>'
    : `<span class="badge bg-secondary-subtle text-secondary-emphasis text-nowrap">${playlist.track_count || 0} tracks</span>`;

  card.className = `card playlist-card ${isActive ? 'border-primary' : ''}`;
  card.style.cursor = 'pointer';
  card.innerHTML = `
    <div class="card-body py-2 d-flex justify-content-between align-items-center gap-2">
      <div style="min-width:0">
        <div class="d-flex align-items-center gap-2">
          <i class="bi ${playlistIcon(playlist)}"></i>
          <span class="fw-semibold text-truncate">${escapeHtml(playlist.name)}</span>
        </div>
        ${fileHint}
      </div>
      <div class="d-flex align-items-center gap-2 text-nowrap">
        ${countBadge}
        <button type="button" class="btn btn-sm btn-outline-secondary py-0" title="Rename playlist">
          <i class="bi bi-pencil"></i>
        </button>
      </div>
    </div>`;

  card.querySelector('button').addEventListener('click', event => {
    event.stopPropagation();
    openRenameModal(playlist.id, playlist.source);
  });
  card.addEventListener('click', () => selectPlaylist(playlist));
  return card;
}

// ===============================
// DETAIL / TRACKS
// ===============================

async function selectPlaylist(playlist) {
  currentPlaylist = playlist;
  renderList(); // re-render for active highlighting
  switchToDetail(); // mobile: show the detail panel, hide the list

  const header = document.getElementById('detailHeader');
  const empty = document.getElementById('detailEmpty');
  const wrap = document.getElementById('detailTracksWrap');

  header.classList.remove('d-none');
  empty.classList.add('d-none');
  wrap.classList.remove('d-none');
  document.getElementById('detailIcon').className = `bi ${playlistIcon(playlist)}`;
  document.getElementById('detailName').textContent = playlist.name;
  document.getElementById('detailComment').textContent = playlist.comment || '';
  document.getElementById('detailMeta').textContent = `${playlist.type === 'smart' ? 'Smart' : 'Regular'} playlist · ${playlist.rule_based ? 'rule-based' : (playlist.track_count || 0) + ' tracks'}`;

  // File path subtitle with copy button (file-backed playlists only).
  const pathRow = document.getElementById('detailPathRow');
  const pathEl = document.getElementById('detailPath');
  if (playlist.file_path) {
    pathEl.textContent = playlist.file_path;
    pathEl.title = playlist.file_path;
    pathRow.classList.remove('d-none');
    pathRow.classList.add('d-flex');
  } else {
    pathRow.classList.add('d-none');
    pathRow.classList.remove('d-flex');
  }

  renderTracks([]);
  document.getElementById('detailTracks').innerHTML =
    '<tr><td colspan="6" class="text-center text-muted py-4">Loading tracks…</td></tr>';
  document.getElementById('detailStacked').innerHTML =
    '<div class="text-center text-muted py-4 small">Loading tracks…</div>';

  // Abort after 30s so a stalled smart-playlist lookup (e.g. slow Navidrome)
  // never leaves the panel hanging on "Loading tracks…" forever.
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 30000);
  try {
    const response = await fetch('/api/playlists/tracks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        id: playlist.id,
        source: playlist.source,
        file_path: playlist.file_path || null,
      }),
      signal: controller.signal,
    });
    const data = await parseJsonOrThrow(response);
    if (!response.ok) throw new Error(data.error || 'Failed to load tracks');
    renderTracks(data.tracks || []);
  } catch (err) {
    console.error('selectPlaylist:', err);
    const message = err.name === 'AbortError'
      ? 'Timed out loading tracks after 30s — check the Activity Center logs and the Navidrome connection.'
      : (err.message || 'Failed to load tracks');
    document.getElementById('detailTracks').innerHTML =
      `<tr><td colspan="6" class="text-center text-danger py-4">${escapeHtml(message)}</td></tr>`;
    document.getElementById('detailStacked').innerHTML =
      `<div class="text-center text-danger py-4 small">${escapeHtml(message)}</div>`;
  } finally {
    clearTimeout(timeout);
  }
}

function renderTracks(tracks) {
  currentTracks = tracks;
  const tableWrap = document.getElementById('detailTableWrap');
  const stackedWrap = document.getElementById('detailStackedWrap');
  if (compactView.matches) {
    renderStackedTracks(tracks);
    tableWrap.classList.add('d-none');
    stackedWrap.classList.remove('d-none');
  } else {
    renderTableTracks(tracks);
    stackedWrap.classList.add('d-none');
    tableWrap.classList.remove('d-none');
  }
}

function renderTableTracks(tracks) {
  const tbody = document.getElementById('detailTracks');
  if (!tracks.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted py-4">No tracks in this playlist</td></tr>';
    return;
  }
  tbody.innerHTML = tracks.map((track, index) => `
    <tr>
      <td class="text-muted">${index + 1}</td>
      <td class="text-truncate" style="max-width: 220px;" title="${escapeHtml(track.title)}">${escapeHtml(track.title)}</td>
      <td class="text-truncate" style="max-width: 160px;" title="${escapeHtml(track.artist)}">${escapeHtml(track.artist)}</td>
      <td class="text-truncate" style="max-width: 160px;" title="${escapeHtml(track.album)}">${escapeHtml(track.album)}</td>
      <td class="text-end text-nowrap">${formatDuration(track.duration)}</td>
      <td class="text-center">${track.rating ? '<i class="bi bi-star-fill text-warning"></i> ' + track.rating : ''}</td>
    </tr>`).join('');
}

// Mobile compact rows: Title (bold) / Artist • Album (muted small) on the
// left, Duration + star rating on the right — no horizontal scrolling.
function renderStackedTracks(tracks) {
  const container = document.getElementById('detailStacked');
  if (!tracks.length) {
    container.innerHTML = '<div class="text-center text-muted py-4 small">No tracks in this playlist</div>';
    return;
  }
  container.innerHTML = tracks.map((track, index) => {
    const artist = escapeHtml(track.artist || '');
    const album = escapeHtml(track.album || '');
    return `
    <div class="border rounded p-2 d-flex justify-content-between align-items-center gap-2">
      <div style="min-width:0">
        <div class="fw-semibold text-truncate" title="${escapeHtml(track.title)}">${index + 1}. ${escapeHtml(track.title)}</div>
        <div class="small text-muted text-truncate">${artist}${album ? ' • ' + album : ''}</div>
      </div>
      <div class="text-end text-nowrap">
        <div class="small">${formatDuration(track.duration)}</div>
        ${starRating(track.rating)}
      </div>
    </div>`;
  }).join('');
}

function starRating(rating) {
  const count = Math.max(0, Math.min(5, Math.round(Number(rating) || 0)));
  if (!count) return '';
  return `<span class="text-warning small">${'⭐'.repeat(count)}</span>`;
}

function resetDetail() {
  document.getElementById('detailHeader').classList.add('d-none');
  document.getElementById('detailEmpty').classList.remove('d-none');
  document.getElementById('detailTracksWrap').classList.add('d-none');
  document.getElementById('detailTracks').innerHTML = '';
  document.getElementById('detailStacked').innerHTML = '';
  document.getElementById('detailPathRow').classList.add('d-none');
  document.getElementById('detailPathRow').classList.remove('d-flex');
  currentTracks = [];
}

// ===============================
// COPY PATH
// ===============================

async function copyPath() {
  const path = document.getElementById('detailPath').textContent || '';
  if (!path) return;
  try {
    await navigator.clipboard.writeText(path);
  } catch (err) {
    // Clipboard API unavailable (insecure context) — fall back to a temp input.
    const input = document.createElement('input');
    input.value = path;
    document.body.appendChild(input);
    input.select();
    try {
      document.execCommand('copy');
    } catch (err2) {
      showAlert('Could not copy path', true);
      input.remove();
      return;
    }
    input.remove();
  }
  showAlert('Path copied to clipboard', false);
}

// ===============================
// RENAME
// ===============================

function openRenameModal(id, source) {
  // Called from card buttons with explicit id/source, or from the header
  // with no args (uses currentPlaylist).
  let target = currentPlaylist;
  if (id || source) {
    target = playlists.find(p => p.id === id && p.source === source) || currentPlaylist;
  }
  if (!target) return;

  currentPlaylist = target;
  document.getElementById('renameName').value = target.name || '';
  const isM3u = String(target.kind || '').toLowerCase() === 'm3u' ||
    String(target.file_name || '').toLowerCase().endsWith('.m3u') ||
    String(target.file_name || '').toLowerCase().endsWith('.m3u8');
  // Generated .m3u playlists derive their file name from the playlist name —
  // only .nsp smart playlists expose the raw file name field.
  document.getElementById('renameFileGroup').style.display =
    (target.source === 'file' && !isM3u) ? '' : 'none';
  if (target.source === 'file' && !isM3u) {
    document.getElementById('renameFileName').value = target.file_name ? target.file_name.replace(/\.nsp$/i, '') : '';
    document.getElementById('renameFilePath').textContent = target.file_path ? `Currently: ${target.file_path}` : '';
  }

  const modal = new bootstrap.Modal(document.getElementById('renameModal'));
  modal.show();
  document.getElementById('renameName').focus();
}

async function submitRename() {
  if (!currentPlaylist) return;

  const name = document.getElementById('renameName').value.trim();
  if (!name) {
    showAlert('Playlist name is required', true);
    return;
  }

  const button = document.querySelector('#renameModal .modal-footer .btn-primary');
  button.disabled = true;

  try {
    const response = await fetch('/api/playlists/rename', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        id: currentPlaylist.id,
        source: currentPlaylist.source,
        name: name,
        file_name: currentPlaylist.source === 'file'
          ? document.getElementById('renameFileName').value.trim()
          : null,
      }),
    });
    const data = await parseJsonOrThrow(response);
    if (!response.ok) throw new Error(data.error || 'Rename failed');

    bootstrap.Modal.getInstance(document.getElementById('renameModal')).hide();
    showAlert(`Renamed playlist to "${name}"`, false);

    // Rebuild the list and re-select the renamed playlist if it was open.
    await loadPlaylists();
    const match = currentPlaylist.source === 'file'
      ? playlists.find(p => p.file_path === data.file_path)
      : playlists.find(p => p.id === currentPlaylist.id && p.source === 'navidrome');
    if (match) selectPlaylist(match);
  } catch (err) {
    console.error('submitRename:', err);
    showAlert(err.message || 'Rename failed', true);
  } finally {
    button.disabled = false;
  }
}

// ===============================
// DELETE
// ===============================

function openDeleteModal() {
  if (!currentPlaylist) return;
  document.getElementById('deleteName').textContent = currentPlaylist.name;
  document.getElementById('deleteHint').textContent =
    currentPlaylist.source === 'file'
      ? `Removes ${currentPlaylist.file_name || 'the playlist file'} from the Playlists folder. This cannot be undone.`
      : 'Deletes the playlist from Navidrome. This cannot be undone.';
  new bootstrap.Modal(document.getElementById('deleteModal')).show();
}

async function submitDelete() {
  if (!currentPlaylist) return;
  const button = document.querySelector('#deleteModal .modal-footer .btn-danger');
  button.disabled = true;
  try {
    const response = await fetch('/api/playlists/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        id: currentPlaylist.id,
        source: currentPlaylist.source,
        file_path: currentPlaylist.file_path || null,
      }),
    });
    const data = await parseJsonOrThrow(response);
    if (!response.ok) throw new Error(data.error || 'Delete failed');

    bootstrap.Modal.getInstance(document.getElementById('deleteModal')).hide();
    showAlert(`Deleted playlist "${currentPlaylist.name}"`, false);
    await loadPlaylists();
  } catch (err) {
    console.error('submitDelete:', err);
    showAlert(err.message || 'Delete failed', true);
  } finally {
    button.disabled = false;
  }
}

// ===============================
// GENERATOR (Last.fm / ListenBrainz recommendations)
// ===============================

function openGeneratorModal() {
  document.getElementById('genResult').classList.add('d-none');
  document.getElementById('genResult').innerHTML = '';
  const modal = new bootstrap.Modal(document.getElementById('generatorModal'));
  modal.show();
  document.getElementById('genName').focus();
}

async function submitGenerator() {
  const btn = document.getElementById('genSubmitBtn');
  const resultEl = document.getElementById('genResult');
  const name = document.getElementById('genName').value.trim() || 'Recommended Mix';
  const limit = Math.max(1, Math.min(parseInt(document.getElementById('genLimit').value, 10) || 12, 25));

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Generating…';
  resultEl.classList.remove('d-none');
  resultEl.className = 'alert alert-info small py-2';
  resultEl.innerHTML = '<i class="bi bi-hourglass-split me-1"></i>Fetching recommendations — this can take a minute (API rate limits).';

  try {
    const response = await fetch('/api/playlists/generate/recommendations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source: document.getElementById('genSource').value, name: name, limit: limit }),
    });
    const data = await parseJsonOrThrow(response);
    if (!response.ok) throw new Error(data.error || 'Generation failed');

    const queuedNote = data.queued_failed > 0
      ? ` <span class="text-danger">(${data.queued_failed} failed to queue)</span>`
      : '';
    resultEl.className = 'alert alert-success small py-2';
    resultEl.innerHTML =
      '<i class="bi bi-check-circle me-1"></i>' +
      `Playlist "<strong>${escapeHtml(data.playlist_name)}</strong>" ready — ` +
      `<strong>${data.added_now}</strong> track(s) from the library` +
      (data.playlist_path ? ` (<code>${escapeHtml(data.playlist_path.split('/').pop())}</code>)` : '') +
      ` · <strong>${data.queued_ok}</strong> missing track(s) queued to Soulseek${queuedNote}.`;

    // Refresh the list so the new .m3u appears; close on success.
    bootstrap.Modal.getInstance(document.getElementById('generatorModal')).hide();
    await loadPlaylists();
  } catch (err) {
    resultEl.className = 'alert alert-danger small py-2';
    resultEl.innerHTML = '<i class="bi bi-exclamation-triangle me-1"></i>' + escapeHtml(err.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<i class="bi bi-magic"></i> Generate';
  }
}

// ===============================
// CSV IMPORT
// ===============================

function openCsvImportModal() {
  document.getElementById('csvImportForm').reset();
  document.getElementById('csvImportStatus').textContent = '';
  document.getElementById('csvImportResult').classList.add('d-none');
  _csvImportData = null;
  new bootstrap.Modal(document.getElementById('csvImportModal')).show();
}

async function submitCsvImport(event) {
  event.preventDefault();
  const fileInput = document.getElementById('csvImportFile');
  const name = document.getElementById('csvImportName').value.trim();
  if (!fileInput.files.length || !name) {
    alert('Please select a CSV file and enter a playlist name');
    return;
  }

  const statusEl = document.getElementById('csvImportStatus');
  const submitBtn = document.getElementById('csvImportSubmitBtn');
  statusEl.textContent = '⏳ Importing and matching tracks…';
  statusEl.className = 'mb-2 small text-secondary';
  submitBtn.disabled = true;
  submitBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span> Importing…';

  try {
    const formData = new FormData();
    formData.append('file', fileInput.files[0]);
    formData.append('playlist_name', name);
    formData.append('playlist_description', '');

    const response = await fetch('/api/playlist/import/csv', { method: 'POST', body: formData });
    const data = await parseJsonOrThrow(response);
    if (!response.ok) throw new Error(data.error || 'Import failed');

    _csvImportData = data;
    const matched = (data.matched_tracks || []).length;
    const missing = (data.missing_tracks || []).length;
    const total = matched + missing;
    const coverage = total > 0 ? Math.round((matched / total) * 100) : 0;

    document.getElementById('csvImportMatched').textContent = `${matched} matched`;
    document.getElementById('csvImportMissing').textContent = `${missing} missing`;
    document.getElementById('csvImportCoverage').textContent =
      `${total} track(s) · ${coverage}% in library`;
    document.getElementById('csvImportCreateBtn').disabled = matched === 0;
    document.getElementById('csvImportResult').classList.remove('d-none');
    statusEl.textContent = '✅ Import complete';
    statusEl.className = 'mb-2 small text-success';
  } catch (err) {
    console.error('submitCsvImport:', err);
    statusEl.textContent = '❌ ' + err.message;
    statusEl.className = 'mb-2 small text-danger';
  } finally {
    submitBtn.disabled = false;
    submitBtn.innerHTML = '<i class="bi bi-upload"></i> Import';
  }
}

async function createPlaylistFromImport() {
  if (!_csvImportData) return;
  const name = document.getElementById('csvImportName').value.trim();
  const btn = document.getElementById('csvImportCreateBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span> Creating…';
  try {
    const response = await fetch('/api/playlist/create', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        playlist_name: name,
        playlist_description: '',
        matched_tracks: _csvImportData.matched_tracks || [],
        format: 'm3u',
      }),
    });
    const data = await parseJsonOrThrow(response);
    if (!response.ok) throw new Error(data.error || 'Creation failed');

    btn.innerHTML = '<i class="bi bi-check-circle"></i> Created';
    btn.classList.remove('btn-success');
    btn.classList.add('btn-outline-success');
    showAlert(`Playlist "${name}" created with ${data.track_count} track(s)`, false);
    bootstrap.Modal.getInstance(document.getElementById('csvImportModal')).hide();
    await loadPlaylists();
  } catch (err) {
    console.error('createPlaylistFromImport:', err);
    showAlert(err.message || 'Failed to create playlist', true);
    btn.disabled = false;
    btn.innerHTML = '<i class="bi bi-plus-circle"></i> Create Playlist from Matched Tracks';
  }
}

// ===============================
// INIT
// ===============================

// Re-render tracks in the right layout when crossing the mobile breakpoint.
compactView.addEventListener('change', () => {
  if (currentPlaylist) renderTracks(currentTracks);
});

document.addEventListener('DOMContentLoaded', loadPlaylists);