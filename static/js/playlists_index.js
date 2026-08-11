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
  const smart = playlists.filter(p => p.type === 'smart');
  const regular = playlists.filter(p => p.type === 'regular');

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

  if (!playlists.length) {
    listEl.innerHTML = '<p class="text-center text-muted py-4 small">No playlists found</p>';
  }
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
  document.getElementById('detailTracks').innerHTML =
    '<tr><td colspan="6" class="text-center text-muted py-4">Loading tracks…</td></tr>';

  try {
    const response = await fetch('/api/playlists/tracks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        id: playlist.id,
        source: playlist.source,
        file_path: playlist.file_path || null,
      }),
    });
    const data = await parseJsonOrThrow(response);
    if (!response.ok) throw new Error(data.error || 'Failed to load tracks');
    renderTracks(data.tracks || []);
  } catch (err) {
    console.error('selectPlaylist:', err);
    document.getElementById('detailTracks').innerHTML =
      `<tr><td colspan="6" class="text-center text-danger py-4">${escapeHtml(err.message)}</td></tr>`;
  }
}

function renderTracks(tracks) {
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

function resetDetail() {
  document.getElementById('detailHeader').classList.add('d-none');
  document.getElementById('detailEmpty').classList.remove('d-none');
  document.getElementById('detailTracksWrap').classList.add('d-none');
  document.getElementById('detailTracks').innerHTML = '';
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
  document.getElementById('renameFileName').value = target.file_name ? target.file_name.replace(/\.nsp$/i, '') : '';
  document.getElementById('renameFilePath').textContent = target.file_path ? `Currently: ${target.file_path}` : '';
  document.getElementById('renameFileGroup').style.display = target.source === 'file' ? '' : 'none';

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
// INIT
// ===============================

document.addEventListener('DOMContentLoaded', loadPlaylists);