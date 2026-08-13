// ===============================
// ADD TO PLAYLIST PICKER
// ===============================
// Shared by the album page (per-track ⋮ menu) and the track page (Actions
// menu).  Lists file-backed playlists (nsp/m3u — Navidrome playlists are
// read-only here), with a "Create New" option that asks for a name.

let _atpModalEl = null;
let _atpTrackId = null;
let _atpTrackTitle = '';
let _atpPlaylists = [];

function _atpEsc(text) {
  const div = document.createElement('div');
  div.textContent = text == null ? '' : String(text);
  return div.innerHTML;
}

function _atpAttr(text) {
  return _atpEsc(text).replace(/"/g, '&quot;');
}

function _atpError(message) {
  const el = document.getElementById('atpError');
  el.textContent = message;
  el.classList.remove('d-none');
}

function _atpBuildModal() {
  if (_atpModalEl) return;
  const wrap = document.createElement('div');
  wrap.innerHTML = `
  <div class="modal fade" id="addToPlaylistModal" tabindex="-1" aria-hidden="true">
    <div class="modal-dialog modal-dialog-scrollable">
      <div class="modal-content">
        <div class="modal-header">
          <h6 class="modal-title"><i class="bi bi-list-plus"></i> Add to Playlist</h6>
          <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
        </div>
        <div class="modal-body">
          <p class="small text-muted mb-2 text-truncate" id="atpTrackLabel"></p>
          <div id="atpOptions" class="d-flex flex-column gap-2" style="max-height: 38vh; overflow-y: auto;">
            <div class="text-center text-muted py-3 small">Loading playlists…</div>
          </div>
          <div class="form-check mt-3">
            <input class="form-check-input" type="radio" name="atpChoice" id="atpCreateNew" value="new" onchange="_atpToggleCreateNew()">
            <label class="form-check-label" for="atpCreateNew"><i class="bi bi-plus-circle me-1"></i>Create New Playlist…</label>
          </div>
          <div class="d-none mt-2" id="atpNewNameRow">
            <input type="text" id="atpNewName" class="form-control form-control-sm" placeholder="Playlist name" maxlength="120" autocomplete="off">
          </div>
          <div id="atpError" class="alert alert-danger py-2 small d-none mt-2"></div>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-sm btn-outline-secondary" data-bs-dismiss="modal">Cancel</button>
          <button type="button" class="btn btn-sm btn-success" id="atpSaveBtn" onclick="submitAddToPlaylist()">
            <i class="bi bi-check-lg"></i> Add
          </button>
        </div>
      </div>
    </div>
  </div>`;
  document.body.appendChild(wrap.firstElementChild);
  _atpModalEl = document.getElementById('addToPlaylistModal');
}

function _atpToggleCreateNew() {
  const row = document.getElementById('atpNewNameRow');
  const checked = document.getElementById('atpCreateNew').checked;
  row.classList.toggle('d-none', !checked);
  if (checked) document.getElementById('atpNewName').focus();
}

function openAddToPlaylistModal(trackId, trackTitle) {
  _atpTrackId = trackId;
  _atpTrackTitle = trackTitle || '';
  _atpBuildModal();
  bootstrap.Modal.getOrCreateInstance(_atpModalEl).show();
  _atpLoadOptions();
}

async function _atpLoadOptions() {
  document.getElementById('atpTrackLabel').textContent =
    `Track: ${_atpTrackTitle || _atpTrackId}`;
  const errorEl = document.getElementById('atpError');
  errorEl.classList.add('d-none');
  const options = document.getElementById('atpOptions');
  options.innerHTML = '<div class="text-center text-muted py-3 small">Loading playlists…</div>';
  try {
    const response = await fetch('/api/playlists/all', {
      headers: { 'Content-Type': 'application/json' },
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Failed to load playlists');
    // Navidrome playlists are read-only from here — file-backed only.
    _atpPlaylists = (data.playlists || []).filter(p => p.source === 'file');
    if (!_atpPlaylists.length) {
      options.innerHTML =
        '<div class="text-center text-muted py-3 small">No editable playlists yet — use "Create New" below.</div>';
      return;
    }
    options.innerHTML = _atpPlaylists.map((playlist, index) => `
      <div class="form-check">
        <input class="form-check-input" type="radio" name="atpChoice" id="atpP${index}"
               value="${_atpAttr(playlist.id)}" onchange="_atpClearNewName()">
        <label class="form-check-label" for="atpP${index}" style="cursor:pointer;">
          <i class="bi ${playlist.kind === 'm3u' ? 'bi-music-note-list' : 'bi-stars'} me-1 text-warning"></i>
          ${_atpEsc(playlist.name)}
          <span class="badge bg-secondary ms-1">${playlist.track_count || 0}</span>
          <small class="text-muted ms-1">${playlist.kind === 'm3u' ? 'M3U' : 'Smart'}</small>
        </label>
      </div>`).join('');
  } catch (err) {
    options.innerHTML = `<div class="text-danger small py-2">${_atpEsc(err.message)}</div>`;
  }
}

function _atpClearNewName() {
  document.getElementById('atpNewName').value = '';
  document.getElementById('atpNewNameRow').classList.add('d-none');
}

async function submitAddToPlaylist() {
  const errorEl = document.getElementById('atpError');
  errorEl.classList.add('d-none');
  const selected = document.querySelector('input[name="atpChoice"]:checked');
  if (!selected) {
    _atpError('Select a playlist or choose "Create New".');
    return;
  }
  const payload = { track_id: _atpTrackId };
  if (selected.value === 'new') {
    const name = document.getElementById('atpNewName').value.trim();
    if (!name) {
      _atpError('Enter a playlist name.');
      return;
    }
    payload.new_name = name;
  } else {
    payload.playlist_id = selected.value;
  }

  const button = document.getElementById('atpSaveBtn');
  button.disabled = true;
  try {
    const response = await fetch('/api/playlists/add-track', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Failed to add track');
    bootstrap.Modal.getInstance(_atpModalEl).hide();
    if (typeof window.showToast === 'function') {
      window.showToast(
        'Added to playlist',
        `"${_atpTrackTitle || 'Track'}" → ${data.playlist}`,
        data.added === false ? 'info' : 'success'
      );
    }
  } catch (err) {
    _atpError(err.message);
  } finally {
    button.disabled = false;
  }
}
