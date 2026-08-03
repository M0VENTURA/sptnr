// ===== DOWNLOAD PAGES JAVASCRIPT =====
// Central logic for downloading, queue management, and external API searches.

// ============================================================================
// UTILITY FUNCTIONS
// ============================================================================
// Ensure doLookup exists globally immediately to prevent ReferenceErrors
window.doLookup = window.doLookup || function(artist, album, track, year, callback) {
    if (typeof window.openGlobalMbSearch === 'function') {
        window.openGlobalMbSearch(artist, album, callback || function(selected) {
            if (typeof window.downloadMbRelease === 'function') {
                window.downloadMbRelease(selected.id, selected.title, selected.artist, 'slskd');
            }
        }, track, year);
    } else {
        console.warn('Global MB Search modal not yet initialized.');
    }
};

async function fetchJsonOrThrow(url, options = {}, timeoutMs = 30000) {
  const controller = new AbortController();
  const mergedOptions = { ...options, signal: options?.signal || controller.signal };
  const timeoutId = setTimeout(() => {
    if (!options?.signal) controller.abort();
  }, timeoutMs);

  let response;
  let raw;
  try {
    response = await fetch(url, mergedOptions);
    raw = await response.text();
  } catch (error) {
    if (error?.name === 'AbortError') {
      if (options?.signal?.aborted) throw error;
      throw new Error(`Request timed out after ${Math.round(timeoutMs / 1000)}s`);
    }
    throw error;
  } finally {
    clearTimeout(timeoutId);
  }

  if ([524, 504, 502].includes(response.status)) {
    throw new Error(`Connection timed out (HTTP ${response.status}). The server took too long to respond.`);
  }

  let data = null;
  try {
    data = raw ? JSON.parse(raw) : {};
  } catch (_) {
    const contentType = (response.headers.get('content-type') || '').toLowerCase();
    if (contentType.includes('text/html') || raw?.trim().startsWith('<!DOCTYPE') || raw?.trim().startsWith('<html')) {
      throw new Error(`Server returned HTML instead of JSON (HTTP ${response.status}).`);
    }
    throw new Error(`Server returned non-JSON response (HTTP ${response.status}).`);
  }

  if (!response.ok) {
    const serverMsg = data && data.error ? data.error : response.statusText;
    throw new Error(`HTTP ${response.status}: ${serverMsg || 'Request failed'}`);
  }

  return data;
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

// Derives the display category for a MusicBrainz release-group the same way
// the backend does (secondary types take precedence over primary), so the
// type dropdown filters correctly even when the server-side category is
// missing or stale. Defined here as well as in the shared modal component —
// the last-loaded copy wins and both are identical.
window.mbDerivedCategory = function(release) {
  const secondary = (release.secondary_types || []).map(s => String(s).toLowerCase());
  const secondaryFirst = ['compilation', 'live', 'remix', 'soundtrack', 'dj-mix', 'mixtape', 'demo', 'spokenword', 'interview', 'audiobook'];
  for (let i = 0; i < secondaryFirst.length; i++) {
    if (secondary.indexOf(secondaryFirst[i]) !== -1) return secondaryFirst[i];
  }
  // Prefer the server-derived category — primary_type is often stale (e.g.
  // remixes persisted with a default "Album"), and category already encodes
  // primary + secondary types.
  const pt = String(release.category || release.primary_type || '').toLowerCase();
  return pt || 'other';
};

function formatBytes(bytes) {
  if (!bytes || bytes === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return Math.round((bytes / Math.pow(k, i)) * 100) / 100 + ' ' + sizes[i];
}

function formatDuration(rawValue) {
  if (rawValue == null || rawValue === '') return 'Unknown';
  const n = Number(rawValue);
  if (!Number.isFinite(n) || n <= 0) return 'Unknown';

  let seconds = n >= 100000000 ? n / 1000000 : (n > 10000 ? n / 1000 : n);
  seconds = Math.max(0, Math.floor(seconds));
  
  const hours = Math.floor(seconds / 3600);
  const mins = Math.floor((seconds % 3600) / 60);
  const secs = seconds % 60;

  if (hours > 0) return `${hours}:${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
  return `${mins}:${secs.toString().padStart(2, '0')}`;
}

function formatETA(seconds) {
  if (seconds === 8640000 || seconds < 0) return '∞';
  if (seconds === 0) return '–';
  
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const secs = seconds % 60;
  
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${secs}s`;
  return `${secs}s`;
}

function encodeInlineArg(value) {
  return encodeURIComponent(JSON.stringify(value)).replace(/'/g, '%27');
}

function decodeInlineArg(value, fallback = null) {
  try {
    return JSON.parse(decodeURIComponent(value));
  } catch (error) {
    console.warn('Failed to decode inline argument:', error);
    return fallback;
  }
}

// ============================================================================
// GLOBAL SEARCH INTEGRATION
// ============================================================================

/**
 * The global MusicBrainz modal opener lives in main.js (canonical, loaded on
 * every page). It fills the 4-field form, wires window._mbSearchCallback, and
 * auto-runs performMbSearch. Nothing page-specific is needed here.
 */

// ============================================================================
// LOOKUP FORM HELPERS (shared across dashboard + downloads)
// ============================================================================

/**
 * Gather fields from the lookup card and open the MusicBrainz search modal.
 * Called by the lookup form on dashboard.html and monitor.html.
 */
window.doLookup = function(artist, album, track, year, callback) {
    // If called without arguments, read from standard form inputs or modal inputs
    if (!artist && !album && !track && !year) {
        artist = document.getElementById('lookupArtist')?.value?.trim() || document.getElementById('mbSearchArtist')?.value?.trim() || '';
        album  = document.getElementById('lookupAlbum')?.value?.trim() || document.getElementById('mbSearchAlbum')?.value?.trim() || '';
        track  = document.getElementById('lookupTrack')?.value?.trim() || document.getElementById('mbSearchTrack')?.value?.trim() || '';
        year   = document.getElementById('lookupYear')?.value?.trim() || document.getElementById('mbSearchYear')?.value?.trim() || '';
    }
    
    if (!artist && !album && !track && !year) return;

    if (typeof callback === 'function') {
        window._mbSearchCallback = callback;
    } else {
        window._mbSearchCallback = function(selected) {
            if (typeof window.downloadMbRelease === 'function') {
                window.downloadMbRelease(selected.id, selected.title, selected.artist, 'slskd');
            }
        };
    }
    
    if (typeof window.openGlobalMbSearch === 'function') {
        window.openGlobalMbSearch(artist, album, window._mbSearchCallback, track, year);
    }
};

/**
 * Clear all lookup form fields.
 */
window.clearLookup = function() {
    ['lookupArtist','lookupAlbum','lookupTrack','lookupYear','mbSearchArtist','mbSearchAlbum','mbSearchTrack','mbSearchYear'].forEach(function(id) {
        var el = document.getElementById(id);
        if (el) el.value = '';
    });
};


// ============================================================================
// MUSICBRAINZ SEARCH & RESULTS CORE
// ============================================================================

window.performMbSearch = async function() {
  // 1. Gather advanced fields if they exist
  const artist = document.getElementById('mbSearchArtist')?.value.trim() || '';
  const album = document.getElementById('mbSearchAlbum')?.value.trim() || '';
  const track = document.getElementById('mbSearchTrack')?.value.trim() || '';
  const year = document.getElementById('mbSearchYear')?.value.trim() || '';
  
  let query = '';

  // 2. Combine them into a search string, or fallback to the single input
  if (artist || album || track || year) {
      query = [artist, album, track, year].filter(Boolean).join(' ');
  } else {
      const singleInput = document.getElementById('mbSearchInput');
      query = singleInput?.value.trim() || '';
  }

  if (!query) return;

  // 3. When only the artist field is populated, search by artist name
  //    (release-groups BY the artist) instead of a free-text title search.
  let artistOnly = false;
  if (window._mbArtistOnlySearch === true) {
    artistOnly = true;
    window._mbArtistOnlySearch = false;
  } else if (artist && !album && !track && !year) {
    artistOnly = true;
  }
  if (query.toLowerCase().startsWith('artist:')) {
    artistOnly = true;
    artist = query.substring(7).trim();
    query = [artist, album, track, year].filter(Boolean).join(' ');
  }
  window._mbArtistOnlySearch = artistOnly;

  const resultsEl = document.getElementById('mbSearchResults') || document.getElementById('mbResults');
  
  if (resultsEl) {
    resultsEl.innerHTML = '<div class="text-center mt-4"><div class="spinner-border text-info"></div><p class="mt-2 text-muted">Searching MusicBrainz...</p></div>';
  }

  try {
    // Send structured fields so each form entry maps to its MusicBrainz
    // Lucene index (artist / releasegroup / recording / date) on the backend.
    const payload = { artist, album, track, year };
    if (!artist && !album && !track && !year) {
      payload.query = query; // legacy single free-text input
    }
    if (artistOnly) payload.artist_only = true;
    const releaseTypeServer = document.getElementById('mbReleaseType')?.value || '';
    if (releaseTypeServer) payload.type = releaseTypeServer; // server-side primarytype/secondarytype filter

    const data = await fetchJsonOrThrow('/api/musicbrainz/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    
    let releases = data.releases || [];

    // Apply the release-type dropdown filter (modal field), if present.
    // Derive the category client-side (secondary types take precedence) so
    // "Album" never admits remix/live/compilation release-groups and stale
    // primary_type/category values can't leak other types in.
    const releaseType = document.getElementById('mbReleaseType')?.value || '';
    if (releaseType) {
      const want = releaseType.toLowerCase();
      releases = releases.filter(r => window.mbDerivedCategory(r) === want);
    }

    // Honour the results-limit dropdown (12/25/50) — it was read by the
    // shared modal but never applied here, so changing it had no effect.
    const limitEl = document.getElementById('mbResultLimit');
    const max = parseInt(limitEl ? limitEl.value : '25', 10) || 25;
    if (releases.length > max) releases = releases.slice(0, max);

    if (releases.length === 0) {
      if (resultsEl) resultsEl.innerHTML = `<div class="alert alert-info"><i class="bi bi-info-circle"></i> No releases found for "${escapeHtml(query)}"</div>`;
      return;
    }

    let html = '<div class="list-group">';
    releases.forEach(release => {
      const coverArt = release.cover_art_url || '';
      const releaseDate = release.first_release_date || 'Unknown';
      const category = release.category || release.primary_type || 'Release';
      const resultArtist = release.artist || (release['artist-credit']?.[0]?.name) || 'Unknown Artist';
      
      const imgHtml = coverArt
        ? `<img src="${escapeHtml(coverArt)}" class="rounded shadow-sm" style="width:80px;height:80px;object-fit:cover;" alt="">`
        : '<div class="rounded bg-secondary d-flex align-items-center justify-content-center shadow-sm" style="width:80px;height:80px;"><i class="bi bi-music-note-beamed text-white fs-4"></i></div>';

      const actionButtons = window._mbSearchCallback
        ? `<button class="btn btn-sm btn-success" onclick="handleGlobalMbSelect('${encodeInlineArg(release)}')"><i class="bi bi-check-circle"></i> Select Match</button>`
        : `<div class="btn-group" role="group">
             <button class="btn btn-sm btn-success" onclick="downloadMbRelease('${escapeHtml(release.id)}', '${escapeHtml(release.title)}', '${escapeHtml(resultArtist)}', 'slskd')" title="Download via Soulseek"><i class="bi bi-music-note-list"></i> Soulseek</button>
             <button class="btn btn-sm btn-primary" onclick="downloadMbRelease('${escapeHtml(release.id)}', '${escapeHtml(release.title)}', '${escapeHtml(resultArtist)}', 'qbittorrent')" title="Download via qBittorrent"><i class="bi bi-cloud-download"></i> qBit</button>
           </div>`;

      html += `
        <div class="list-group-item bg-dark-subtle border-secondary mb-2 rounded">
          <div class="d-flex gap-3 align-items-start">
            ${imgHtml}
            <div class="flex-grow-1">
              <h6 class="mb-1 fw-bold">${escapeHtml(release.title)}</h6>
              <p class="mb-1 text-muted small">${escapeHtml(resultArtist)}</p>
              <div class="d-flex gap-2 align-items-center mb-2 flex-wrap">
                <span class="badge bg-secondary">${escapeHtml(category)}</span>
                <span class="badge bg-info text-dark"><i class="bi bi-cloud"></i> MusicBrainz</span>
                <span class="text-muted small">${escapeHtml(releaseDate)}</span>
              </div>
            </div>
            <div class="flex-shrink-0 mt-2 mt-sm-0">
              ${actionButtons}
            </div>
          </div>
        </div>`;
    });
    html += '</div>';
    
    if (resultsEl) resultsEl.innerHTML = html;

  } catch (error) {
    if (resultsEl) resultsEl.innerHTML = `<div class="alert alert-danger"><i class="bi bi-exclamation-triangle"></i> Error: ${escapeHtml(error.message)}</div>`;
  }
};

// Handles routing the selected release back to the component that asked for it
window.handleGlobalMbSelect = function(releaseEnc) {
    const release = decodeInlineArg(releaseEnc);
    if (window._mbSearchCallback && release) {
        window._mbSearchCallback(release);
        window._mbSearchCallback = null; // Clear the callback so it doesn't fire twice
    }
    
    // Close the modal
    const modalEl = document.getElementById('musicBrainzModal');
    if (modalEl) {
        const modal = bootstrap.Modal.getInstance(modalEl);
        if (modal) modal.hide();
    }
};

// Also map performMbDownloadSearch to performMbSearch in case any old HTML buttons rely on it
window.performMbDownloadSearch = window.performMbSearch;


// ============================================================================
// QBITTORRENT FUNCTIONS
// ============================================================================

let qbitMonLoaded = false;
let qbitMonInFlight = false;

async function performQbitSearch() {
  const query = document.getElementById('qbitSearchInput')?.value;
  if (!query) return;
  
  document.getElementById('qbitLoading').style.display = 'block';
  document.getElementById('qbitResults').innerHTML = '';
  
  try {
    const data = await fetchJsonOrThrow('/api/qbittorrent/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query })
    });

    document.getElementById('qbitLoading').style.display = 'none';
    
    if (data.error) {
      document.getElementById('qbitResults').innerHTML = `<div class="alert alert-danger"><i class="bi bi-exclamation-triangle"></i> ${data.error}</div>`;
      return;
    }
    
    const results = data.results || [];
    if (results.length === 0) {
      document.getElementById('qbitResults').innerHTML = `<div class="alert alert-info"><i class="bi bi-info-circle"></i> No results found.</div>`;
      return;
    }
    
    let html = '<div class="list-group">';
    results.forEach((result) => {
      const size = formatBytes(result.fileSize || 0);
      const seedClass = result.nbSeeders > 10 ? 'text-success' : (result.nbSeeders > 0 ? 'text-warning' : 'text-danger');
      
      html += `
        <div class="list-group-item list-group-item-action">
          <div class="d-flex w-100 justify-content-between align-items-start">
            <div class="flex-grow-1 me-2">
              <h6 class="mb-1" style="font-size: 0.9rem;">${escapeHtml(result.fileName || 'Unknown')}</h6>
              <small class="text-muted">${escapeHtml(result.siteUrl || '')}</small>
            </div>
            <button class="btn btn-sm btn-success" onclick="addTorrent('${escapeHtml(result.fileUrl)}')" ${!result.fileUrl ? 'disabled' : ''}>
              <i class="bi bi-plus-circle"></i> Add
            </button>
          </div>
          <div class="d-flex gap-3 mt-2">
            <small><i class="bi bi-hdd"></i> ${size}</small>
            <small class="${seedClass}"><i class="bi bi-arrow-up-circle"></i> ${result.nbSeeders || 0}</small>
            <small><i class="bi bi-arrow-down-circle"></i> ${result.nbLeechers || 0}</small>
          </div>
        </div>
      `;
    });
    html += '</div>';
    document.getElementById('qbitResults').innerHTML = html;

  } catch (error) {
    document.getElementById('qbitLoading').style.display = 'none';
    document.getElementById('qbitResults').innerHTML = `<div class="alert alert-danger"><i class="bi bi-exclamation-triangle"></i> ${error.message}</div>`;
  }
}

async function addTorrent(url) {
  if (!url || !confirm('Add this torrent to qBittorrent?')) return;
  try {
    const data = await fetchJsonOrThrow('/api/qbittorrent/add', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url })
    });
    if (data.success) {
      alert('✓ Torrent added successfully!');
    } else {
      alert('✗ Error: ' + (data.error || 'Failed to add torrent'));
    }
  } catch (error) {
    alert('✗ Network error: ' + error.message);
  }
}

async function refreshQbitMonitor(options = {}) {
  const silent = options.silent === true;
  const loading = document.getElementById('qbitMonLoading');
  const errorBox = document.getElementById('qbitMonError');
  const results = document.getElementById('qbitMonResults');
  const empty = document.getElementById('qbitMonEmpty');
  const table = document.getElementById('qbitMonTable');
  const tbody = document.getElementById('qbitMonTableBody');
  const countBadge = document.getElementById('qbitMonCount');

  if (!loading || qbitMonInFlight) return;
  qbitMonInFlight = true;

  if (!qbitMonLoaded && !silent) loading.style.display = 'block';

  try {
    const response = await fetch('/api/qbittorrent/status');
    const data = await response.json();
    
    if (loading) loading.style.display = 'none';
    qbitMonLoaded = true;

    if (data.error) {
      if (errorBox) { errorBox.textContent = 'Error: ' + data.error; errorBox.style.display = 'block'; }
      if (results) results.style.display = 'none';
      return;
    }

    if (errorBox) errorBox.style.display = 'none';
    if (results) results.style.display = 'block';
    
    const torrents = data.torrents || [];
    if (countBadge) {
      countBadge.style.display = torrents.length ? 'inline-block' : 'none';
      countBadge.textContent = `${torrents.length} active`;
    }

    if (torrents.length === 0) {
      if (empty) empty.style.display = 'block';
      if (table) table.style.display = 'none';
      return;
    }

    if (empty) empty.style.display = 'none';
    if (table) table.style.display = 'block';
    if (tbody) tbody.innerHTML = '';

    torrents.forEach(torrent => {
      const row = document.createElement('tr');
      const state = (torrent.state || '').toLowerCase();
      let stateClass = 'bg-secondary';
      if (state.includes('downloading')) stateClass = 'bg-primary';
      else if (state.includes('uploading') || state.includes('seeding')) stateClass = 'bg-success';
      else if (state.includes('paused')) stateClass = 'bg-warning';
      else if (state.includes('error')) stateClass = 'bg-danger';
      else if (state.includes('stalled')) stateClass = 'bg-warning';

      const needsForceStart = state.includes('stalled') || state.includes('paused') || state.includes('error');
      const isRunning = !state.includes('stalled') && !state.includes('paused') && state !== 'stopped';
      const hash = torrent.hash || '';
      
      const actionHtml = hash ? `<div style="display: flex; gap: 4px; justify-content: center;">
          ${needsForceStart ? `<button class="btn btn-sm btn-outline-primary" onclick="forceStartQbitTorrent('${escapeHtml(hash)}')" title="Force start torrent"><i class="bi bi-lightning-charge"></i> Start</button>` : ''}
          ${isRunning ? `<button class="btn btn-sm btn-outline-danger" onclick="stopQbitTorrent('${escapeHtml(hash)}')" title="Stop torrent"><i class="bi bi-pause-circle"></i> Stop</button>` : ''}
        </div>` : '<span class="text-muted">–</span>';

      row.innerHTML = `
        <td>
          <div class="text-truncate" style="max-width: 420px;" title="${escapeHtml(torrent.name || '')}">
            ${escapeHtml(torrent.name || '')}
          </div>
          ${torrent.category ? `<small class="text-muted"><i class="bi bi-tag"></i> ${escapeHtml(torrent.category)}</small>` : ''}
        </td>
        <td class="text-center"><span class="badge ${stateClass}">${escapeHtml(torrent.state || '')}</span></td>
        <td class="text-center">
          <div class="progress" style="height: 20px; min-width: 100px;">
            <div class="progress-bar ${torrent.progress >= 100 ? 'bg-success' : 'bg-primary'}" role="progressbar" style="width: ${torrent.progress}%">${torrent.progress}%</div>
          </div>
        </td>
        <td class="text-center">${formatBytes(torrent.size || 0)}</td>
        <td class="text-center">${torrent.dlspeed > 0 ? `<span class="text-primary"><i class="bi bi-arrow-down"></i> ${formatBytes(torrent.dlspeed)}/s</span>` : '–'}</td>
        <td class="text-center">${torrent.upspeed > 0 ? `<span class="text-success"><i class="bi bi-arrow-up"></i> ${formatBytes(torrent.upspeed)}/s</span>` : '–'}</td>
        <td class="text-center"><span class="${torrent.num_seeds > 0 ? 'text-success' : 'text-muted'}">${torrent.num_seeds} / ${torrent.num_leechs}</span></td>
        <td class="text-center">${formatETA(torrent.eta)}</td>
        <td class="text-center">${actionHtml}</td>
      `;
      if (tbody) tbody.appendChild(row);
    });
  } catch (err) {
    if (!silent && loading) loading.style.display = 'none';
    if (errorBox) { errorBox.textContent = 'Network error: ' + err.message; errorBox.style.display = 'block'; }
  } finally {
    qbitMonInFlight = false;
  }
}

async function forceStartQbitTorrent(hash) {
  if (!hash) return;
  try {
    const data = await fetchJsonOrThrow('/api/qbittorrent/force-start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hash })
    });
    if (data.success) refreshQbitMonitor({ silent: true });
    else alert('✗ Error: ' + (data.error || 'Failed to force start'));
  } catch (err) {
    alert('✗ Network error: ' + err.message);
  }
}

async function stopQbitTorrent(hash) {
  if (!hash || !confirm('Stop this torrent?')) return;
  try {
    const data = await fetchJsonOrThrow('/api/qbittorrent/stop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hash })
    });
    if (data.success) refreshQbitMonitor({ silent: true });
    else alert('✗ Error: ' + (data.error || 'Failed to stop torrent'));
  } catch (err) {
    alert('✗ Network error: ' + err.message);
  }
}


// ============================================================================
// UPCOMING RELEASES
// ============================================================================

async function clearUpcomingReleases() {
  if (!confirm('Are you sure you want to clear all upcoming releases from the database? This cannot be undone.')) return;
  const statusEl = document.getElementById('upcomingStatus');
  const statusText = document.getElementById('upcomingStatusText');
  const errorEl = document.getElementById('upcomingError');
  
  statusEl.style.display = 'block'; errorEl.style.display = 'none';
  statusText.textContent = 'Clearing database...';
  
  try {
    const data = await fetchJsonOrThrow('/api/upcoming-releases/clear', { method: 'POST' });
    statusText.textContent = `✓ ${data.message}`;
    setTimeout(() => {
      statusEl.style.display = 'none';
      document.getElementById('upcomingReleases').innerHTML = `<div class="text-center py-5"><p class="text-muted">Database cleared. Click <strong>Check for Updates</strong> to search MusicBrainz for new release data.</p></div>`;
    }, 2000);
  } catch (error) {
    statusEl.style.display = 'none'; errorEl.style.display = 'block';
    errorEl.innerHTML = `<i class="bi bi-exclamation-triangle"></i> <strong>Error clearing database:</strong> ${error.message}`;
  }
}

async function scrapeUpcomingReleases() {
  const statusEl = document.getElementById('upcomingStatus');
  const statusText = document.getElementById('upcomingStatusText');
  const errorEl = document.getElementById('upcomingError');
  
  statusEl.style.display = 'block'; errorEl.style.display = 'none';
  statusText.textContent = 'Scraping MusicBrainz for upcoming releases...';
  
  try {
    const data = await fetchJsonOrThrow('/api/upcoming-releases/scrape', { method: 'POST' });
    statusText.textContent = `✓ ${data.message}`;
    setTimeout(() => {
      statusEl.style.display = 'none';
      refreshUpcomingReleases();
    }, 2000);
  } catch (error) {
    statusEl.style.display = 'none'; errorEl.style.display = 'block';
    errorEl.innerHTML = `<i class="bi bi-exclamation-triangle"></i> <strong>Error updating releases:</strong> ${error.message}`;
  }
}

async function checkForUpdates() {
  localStorage.setItem('upcomingReleasesLastChecked', Date.now().toString());
  await scrapeUpcomingReleases();
}

async function refreshUpcomingReleases() {
  const container = document.getElementById('upcomingReleases');
  if (!container) return;
  const filterCollection = document.getElementById('upcomingFilterCollection')?.checked || false;
  
  container.innerHTML = `<div class="text-center py-4"><div class="spinner-border text-primary spinner-border-sm"></div><p class="mt-2 small">Loading upcoming releases...</p></div>`;
  
  try {
    const data = await fetchJsonOrThrow(`/api/upcoming-releases?collection=${filterCollection}&include_queue=true`);
    
    if (!data.releases || data.releases.length === 0) {
      container.innerHTML = `<div class="alert alert-info"><i class="bi bi-info-circle"></i> No upcoming releases found. Click <strong>Check for Updates</strong> to scan MusicBrainz.</div>`;
      return;
    }
    
    let releases = data.releases;
    if (filterCollection) {
      releases = releases.filter(r => !r.album_in_collection);
      if (releases.length === 0) {
        container.innerHTML = `<div class="alert alert-info"><i class="bi bi-check-circle"></i> You have all upcoming releases from artists in your collection!</div>`;
        return;
      }
    }
    
    const grouped = {};
    releases.forEach(release => {
      const month = (release.release_date || 'Unknown Date').substring(0, 7);
      if (!grouped[month]) grouped[month] = [];
      grouped[month].push(release);
    });
    
    const sortedMonths = Object.keys(grouped).sort();
    let html = '<div class="accordion" id="releaseAccordion">';
    
    sortedMonths.forEach((month, idx) => {
      const monthReleases = grouped[month];
      const monthLabel = new Date(month + '-01').toLocaleDateString('en-US', { year: 'numeric', month: 'long' });
      
      html += `
        <div class="accordion-item">
          <h2 class="accordion-header" id="heading${idx}">
            <button class="accordion-button ${idx > 0 ? 'collapsed' : ''}" type="button" data-bs-toggle="collapse" data-bs-target="#collapse${idx}">
              <strong>${monthLabel}</strong> <span class="badge bg-primary ms-2">${monthReleases.length}</span>
            </button>
          </h2>
          <div id="collapse${idx}" class="accordion-collapse collapse ${idx === 0 ? 'show' : ''}" data-bs-parent="#releaseAccordion">
            <div class="accordion-body p-0">
              <div class="table-responsive">
                <table class="table table-dark table-hover table-sm mb-0">
                  <thead><tr><th>Artist</th><th>Album</th><th>Date</th><th style="width: 120px;">Action</th></tr></thead>
                  <tbody>
      `;
      
      monthReleases.forEach(release => {
        let albumStatus = release.album_in_collection ? ' <span class="badge bg-success ms-1">In Collection</span>' : (release.in_queue ? ' <span class="badge bg-warning text-dark ms-1">Downloading</span>' : '');
        let artistStatus = release.artist_in_collection ? ' <span class="badge bg-success ms-1">Artist in Collection</span>' : '';
        const artistArg = JSON.stringify(String(release.artist_name || ''));
        const albumArg = JSON.stringify(String(release.album_name || ''));
        
        html += `
          <tr>
            <td>${escapeHtml(release.artist_name)}${artistStatus}</td>
            <td>${escapeHtml(release.album_name)}${albumStatus}</td>
            <td><small>${release.release_date || 'TBA'}</small></td>
            <td>
              <button type="button" class="btn btn-sm btn-outline-primary" title="Search on MusicBrainz" onclick='searchMusicBrainzRelease(event, ${artistArg}, ${albumArg})'>
                <i class="bi bi-search"></i> Search
              </button>
            </td>
          </tr>`;
      });
      html += `</tbody></table></div></div></div></div>`;
    });
    html += '</div>';
    container.innerHTML = html;
  } catch (error) {
    container.innerHTML = `<div class="alert alert-danger"><i class="bi bi-exclamation-triangle"></i> <strong>Error:</strong> ${error.message}</div>`;
  }
}

// Rich MusicBrainz search for Wikipedia/upcoming releases, with Discogs
// fallback, per-track selection and "Use MBID" matching. Restored from the
// original old_system implementation so the queue page's "Choose" flow keeps
// its intended accordion UX while rendering into the shared modal.
async function searchMusicBrainzRelease(event, artist, album, upcomingReleaseId = null) {
  if (event) { event.preventDefault(); event.stopPropagation(); }

  window.currentUpcomingReleaseContext = upcomingReleaseId ? { releaseId: upcomingReleaseId, artist, album } : null;

  const modalEl = document.getElementById('musicBrainzModal');
  if (!modalEl) {
    alert('Search UI not available on this page.');
    return;
  }

  const resultsEl = document.getElementById('mbSearchResults');
  if (resultsEl) {
    resultsEl.innerHTML = '<div class="text-center py-4"><div class="spinner-border text-primary"></div><p class="mt-2 text-muted">Searching MusicBrainz...</p></div>';
  }

  // Populate the shared modal's 4-field form so the Release Type / Result
  // Limit dropdowns re-search with the current query instead of no-oping
  // (performMbSearch builds its query from these fields).
  const artistField = document.getElementById('mbSearchArtist');
  const albumField = document.getElementById('mbSearchAlbum');
  if (artistField && artist) artistField.value = artist;
  if (albumField && album) albumField.value = album;

  // Show the shared modal (included globally by base.html)
  const hasBootstrapModal = !!(window.bootstrap && window.bootstrap.Modal);
  if (hasBootstrapModal) {
    const modal = bootstrap.Modal.getInstance(modalEl) || new bootstrap.Modal(modalEl);
    modal.show();
  } else {
    modalEl.style.display = 'block';
    modalEl.classList.add('show');
    modalEl.removeAttribute('aria-hidden');
    modalEl.setAttribute('aria-modal', 'true');
    document.body.classList.add('modal-open');
  }

  const parseJsonResponse = async (resp, sourceName) => {
    const contentType = (resp.headers.get('content-type') || '').toLowerCase();
    if (!contentType.includes('application/json')) {
      const raw = await resp.text();
      const hint = raw && raw.trim().startsWith('<')
        ? 'Received HTML instead of JSON (possible auth/session redirect).'
        : `Unexpected response format from ${sourceName}.`;
      throw new Error(`${hint} HTTP ${resp.status}`);
    }
    const parsed = await resp.json();
    if (!resp.ok) {
      throw new Error(parsed.error || parsed.message || `${sourceName} request failed (HTTP ${resp.status})`);
    }
    return parsed;
  };

  try {
    const response = await fetch('/api/upcoming-releases/search-musicbrainz', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ artist, album })
    });
    const data = await parseJsonResponse(response, 'MusicBrainz');

    const isArtistOnlySearch = !album || !String(album).trim();

    if (data.success && data.results && data.results.length >= 1) {
      if (resultsEl) resultsEl.innerHTML = '';
      displayMusicBrainzResults(data.results);
      return;
    }

    if (isArtistOnlySearch) {
      if (resultsEl) {
        resultsEl.innerHTML = '<div class="alert alert-info"><i class="bi bi-info-circle"></i> No releases found on MusicBrainz for this artist.</div>';
      }
      return;
    }

    if (resultsEl) {
      resultsEl.innerHTML = '<div class="text-center py-4"><div class="spinner-border text-primary"></div><p class="mt-2 text-muted">Searching Discogs as fallback...</p></div>';
    }

    const discogsResponse = await fetch('/api/upcoming-releases/search-discogs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ artist, album })
    });
    const discogsData = await parseJsonResponse(discogsResponse, 'Discogs');

    const allResults = [];
    if (Array.isArray(data.results)) allResults.push(...data.results);
    if (discogsData.success && Array.isArray(discogsData.results)) allResults.push(...discogsData.results);

    if (allResults.length === 0) {
      if (resultsEl) {
        resultsEl.innerHTML = '<div class="alert alert-info"><i class="bi bi-info-circle"></i> No releases found on MusicBrainz or Discogs.</div>';
      }
      return;
    }

    if (resultsEl) resultsEl.innerHTML = '';
    displayMusicBrainzResults(allResults);
  } catch (error) {
    if (resultsEl) {
      resultsEl.innerHTML = '<div class="alert alert-danger"><i class="bi bi-exclamation-triangle"></i> Error searching: ' + escapeHtml(error.message) + '</div>';
    }
  }
}

// Route the shared modal's card selection back into the queue-page download
// flow when the modal was opened via the upcoming-releases "Search / Download"
// button. That flow doesn't set window._mbSearchCallback, so the component's
// selectMbRelease falls back to this CustomEvent.
document.addEventListener('mbReleaseSelected', function(evt) {
  var detail = evt.detail || {};
  var release = detail.release || window._selectedMusicBrainzRelease;
  if (!release) return;
  var id = detail.releaseId || release.id || '';
  var title = detail.title || release.title || '';
  var artistName = release.artist
    || (release['artist-credit'] && release['artist-credit'][0] && release['artist-credit'][0].name)
    || 'Unknown Artist';
  window._selectedMusicBrainzRelease = null;
  if (typeof window.downloadMbRelease === 'function' && id && title) {
    window.downloadMbRelease(id, title, artistName, 'slskd');
  }
});

// Rich accordion renderer used by searchMusicBrainzRelease (original intent).
function displayMusicBrainzResults(results) {
  const container = document.getElementById('mbSearchResults');
  if (!container) return;

  if (!window.mbReleaseData) {
    window.mbReleaseData = {};
  }

  let html = '<div class="accordion" id="mbResultsAccordion">';

  results.forEach((release, index) => {
    const releaseId = `mbRelease${index}`;
    const dataKey = `release_${Date.now()}_${index}`;

    window.mbReleaseData[dataKey] = {
      artist: release.artist,
      album: release.title,
      tracks: release.tracks,
      year: release.date || release.first_release_date || release.year || null,
      release_id: release.release_id || null,
      release_group_id: release.release_group_id || null,
      source: release.source || 'musicbrainz'
    };

    const source = release.source || 'musicbrainz';
    const sourceBadge = source === 'discogs'
      ? '<span class="badge bg-info ms-2">Discogs</span>'
      : '<span class="badge bg-primary ms-2">MusicBrainz</span>';

    const tracksHtml = (release.tracks || []).map((track, trackIndex) => {
      let duration = 'N/A';
      if (track.length != null && track.length !== '') {
        duration = formatDuration(track.length);
      } else if (track.duration != null && track.duration !== '') {
        duration = track.duration;
      }

      return `
        <tr class="table-dark">
          <td style="width: 44px;" class="text-center">
            <input type="checkbox" class="form-check-input mb-track-select" data-release-key="${dataKey}" data-track-index="${trackIndex}">
          </td>
          <td>${escapeHtml(track.position || '')}</td>
          <td>${escapeHtml(track.title || '')}</td>
          <td>${duration}</td>
          <td style="width: 120px;" class="text-center">
            <button class="btn btn-sm btn-outline-success mb-download-track" data-release-key="${dataKey}" data-track-index="${trackIndex}">
              <i class="bi bi-download"></i> Download
            </button>
          </td>
        </tr>
      `;
    }).join('');

    const releaseInfo = [];
    if (release.type) {
      releaseInfo.push(release.type);
    } else if (Array.isArray(release.formats) && release.formats.length > 0) {
      releaseInfo.push(release.formats.join(', '));
    } else {
      releaseInfo.push('Album');
    }
    if (release.date) releaseInfo.push(release.date);
    else if (release.year) releaseInfo.push(release.year);
    else releaseInfo.push('Unknown date');
    releaseInfo.push(`${release.track_count || (release.tracks || []).length} tracks`);

    html += `
      <div class="accordion-item">
        <h2 class="accordion-header" id="heading${releaseId}">
          <button class="accordion-button ${index === 0 ? '' : 'collapsed'}" type="button"
            data-bs-toggle="collapse" data-bs-target="#${releaseId}"
            aria-expanded="${index === 0 ? 'true' : 'false'}" aria-controls="${releaseId}">
            <div class="w-100">
              <strong>${escapeHtml(release.title || '')}</strong>${sourceBadge}
              <small class="text-muted ms-2">${releaseInfo.join(' · ')}</small>
            </div>
          </button>
        </h2>
        <div id="${releaseId}" class="accordion-collapse collapse ${index === 0 ? 'show' : ''}"
          aria-labelledby="heading${releaseId}" data-bs-parent="#mbResultsAccordion">
          <div class="accordion-body">
            <div class="mb-3 d-flex flex-wrap align-items-center gap-2">
              <button class="btn btn-success mb-download-release" data-release-key="${dataKey}">
                <i class="bi bi-download"></i> Download All Tracks (${release.track_count || (release.tracks || []).length})
              </button>
              <button class="btn btn-outline-success mb-download-selected" data-release-key="${dataKey}" disabled>
                <i class="bi bi-check2-square"></i> Download Selected (0)
              </button>
              ${window.currentUpcomingReleaseContext && source !== 'discogs' && release.release_group_id ? `
              <button class="btn btn-outline-primary mb-save-upcoming-match" data-release-key="${dataKey}">
                <i class="bi bi-link-45deg"></i> Use MBID
              </button>
              ` : ''}
              <div class="form-check mb-0 ms-md-2">
                <input class="form-check-input mb-select-all" type="checkbox" id="mbSelectAll_${releaseId}" data-release-key="${dataKey}">
                <label class="form-check-label" for="mbSelectAll_${releaseId}">Select All</label>
              </div>
            </div>
            <table class="table table-sm table-hover table-striped table-dark mb-track-table">
              <thead>
                <tr>
                  <th style="width: 44px;" class="text-center"></th>
                  <th style="width: 60px;">#</th>
                  <th>Title</th>
                  <th style="width: 100px;">Duration</th>
                  <th style="width: 120px;" class="text-center">Action</th>
                </tr>
              </thead>
              <tbody>${tracksHtml}</tbody>
            </table>
          </div>
        </div>
      </div>
    `;
  });

  html += '</div>';
  container.innerHTML = html;

  container.querySelectorAll('.mb-download-release').forEach(button => {
    button.addEventListener('click', function () {
      const dataKey = this.dataset.releaseKey;
      const releaseData = window.mbReleaseData[dataKey];
      if (!releaseData) {
        alert('Error: Release data not found');
        return;
      }
      downloadMusicBrainzRelease(
        releaseData.artist,
        releaseData.album,
        releaseData.tracks,
        releaseData.year,
        releaseData.release_id,
        releaseData.source
      );
    });
  });

  container.querySelectorAll('.mb-download-track').forEach(button => {
    button.addEventListener('click', async function () {
      const dataKey = this.dataset.releaseKey;
      const trackIndex = Number(this.dataset.trackIndex);
      const releaseData = window.mbReleaseData[dataKey];
      if (!releaseData || !Array.isArray(releaseData.tracks) || !releaseData.tracks[trackIndex]) {
        alert('Error: Track data not found');
        return;
      }
      const singleTrack = [releaseData.tracks[trackIndex]];
      const queued = await downloadMusicBrainzRelease(
        releaseData.artist,
        releaseData.album,
        singleTrack,
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
    button.addEventListener('click', async function () {
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

  container.querySelectorAll('.mb-save-upcoming-match').forEach(button => {
    button.addEventListener('click', async function () {
      const dataKey = this.dataset.releaseKey;
      const releaseData = window.mbReleaseData[dataKey];
      const context = window.currentUpcomingReleaseContext;
      if (!releaseData || !releaseData.release_group_id) {
        alert('This result does not include a MusicBrainz release-group MBID');
        return;
      }
      if (!context || !context.releaseId) {
        alert('No upcoming release is selected for matching');
        return;
      }

      try {
        const response = await fetch(`/api/upcoming-releases/${context.releaseId}/match`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            release_group_mbid: releaseData.release_group_id,
            source: 'manual_selection'
          })
        });
        const data = await response.json();
        if (!response.ok || !data.success) {
          throw new Error(data.error || 'Failed to save upcoming release match');
        }

        const modalEl = document.getElementById('musicBrainzModal');
        const modal = modalEl ? bootstrap.Modal.getInstance(modalEl) : null;
        if (modal) {
          modal.hide();
        }

        if (typeof refreshUpcomingReleases === 'function') {
          await refreshUpcomingReleases();
        }
        if (typeof refreshUpcomingReleasesMonitor === 'function') {
          await refreshUpcomingReleasesMonitor();
        }
      } catch (error) {
        console.error('Error saving upcoming release match:', error);
        alert('Error saving upcoming release match: ' + error.message);
      }
    });
  });

  container.querySelectorAll('.mb-select-all').forEach(checkbox => {
    checkbox.addEventListener('change', function () {
      const dataKey = this.dataset.releaseKey;
      const checked = this.checked;
      container.querySelectorAll(`.mb-track-select[data-release-key="${dataKey}"]`).forEach(trackBox => {
        trackBox.checked = checked;
      });
      updateMBSelectionUI(container, dataKey);
    });
  });

  container.querySelectorAll('.mb-track-select').forEach(checkbox => {
    checkbox.addEventListener('change', function () {
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

  const closeModal = options.closeModal !== false;
  const selectionLabel = options.selectionLabel || null;

  try {
    let releaseYear = null;
    if (year) {
      releaseYear = String(year).substring(0, 4);
    }

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
      release_mbid: release_id || null,
      release_source: source || 'musicbrainz',
      recording_mbid: track.recording_mbid || null,
      duration: track.length || null
    }));

    const import_group = `${artist}_${album}`.replace(/\s+/g, '_').substring(0, 100);

    const response = await fetch('/api/queue/add-batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        items: trackItems,
        import_group: import_group,
        import_type: 'album'
      })
    });

    const data = await response.json();
    if (!data.success) {
      alert('❌ Error: ' + (data.error || 'Failed to add tracks to queue'));
      return false;
    }

    if (closeModal) {
      const modalEl = document.getElementById('musicBrainzModal');
      const modal = modalEl ? bootstrap.Modal.getInstance(modalEl) : null;
      if (modal) {
        modal.hide();
      }
    }

    const label = selectionLabel ? ` ${selectionLabel}` : ' tracks';
    let message = `✅ Added ${data.added || tracks.length}${label} from "${album}" to download queue`;
    if (data.failed > 0) {
      message += `\n⚠️ Failed to add ${data.failed} tracks`;
    }
    if ((data.added || 0) > 0 && import_group) {
      message += `\n\n📦 All ${data.import_type || 'album'} tracks are grouped as: "${album}"`;
      message += `\nOnce downloads complete, use "Organize All" in the Completed section to move them to /music`;
    }
    alert(message);

    if (typeof loadQueueStatus === 'function') {
      await loadQueueStatus();
    }
    try {
      localStorage.setItem('popularr_queue_updated', Date.now().toString());
    } catch (e) {
      console.warn('Could not update localStorage:', e);
    }
    return true;
  } catch (error) {
    console.error('Error downloading release:', error);
    alert('❌ Error: ' + error.message);
    return false;
  }
}

// Canonical alias so inline page overrides (e.g. queue.html) can delegate to
// this implementation instead of re-implementing the shared-modal flow.
window.searchMusicBrainzReleaseCanonical = searchMusicBrainzRelease;


// ============================================================================
// MANAGED DOWNLOADS (MusicBrainz/Queuing)
// ============================================================================

const MB_DEFAULT_MAX_RETRIES = 3;

function getMbStatusBadge(status) {
  const s = (status || '').toLowerCase();
  if (s === 'completed') return '<span class="badge bg-success"><i class="bi bi-check-circle"></i> Completed</span>';
  if (['downloading', 'in_progress', 'initiating_download'].includes(s)) return '<span class="badge bg-info"><i class="bi bi-download"></i> Downloading</span>';
  if (['queued', 'pending'].includes(s)) return '<span class="badge bg-secondary"><i class="bi bi-clock"></i> Queued</span>';
  if (['failed', 'error'].includes(s)) return '<span class="badge bg-danger"><i class="bi bi-x-circle"></i> Failed</span>';
  if (s === 'searching') return '<span class="badge bg-warning"><i class="bi bi-search"></i> Searching</span>';
  if (s === 'awaiting_selection') return '<span class="badge bg-primary"><i class="bi bi-hand-index"></i> Select File</span>';
  return `<span class="badge bg-secondary">${escapeHtml(status)}</span>`;
}

async function downloadMbRelease(releaseId, releaseTitle, artist, method) {
  const persistentEl = document.getElementById('persistentSearchCheck');
  const persistentSearch = persistentEl ? persistentEl.checked : false;
  const sessionSelector = document.getElementById('mbSessionSelector');
  const selectedSession = sessionSelector ? sessionSelector.value : '';

  if (selectedSession === 'create') {
    const sessionName = prompt('Enter name for new playlist session:');
    if (!sessionName) return;
    try {
      const data = await fetchJsonOrThrow('/api/playlist-downloads/create', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_name: sessionName, total_tracks: null, priority_queue: false })
      });
      _addMbDownloadToSession(releaseId, releaseTitle, artist, method, persistentSearch, data.session_id);
    } catch (e) {
      alert('Error creating session: ' + e.message);
    }
    return;
  }

  if (!confirm(`Download "${releaseTitle}" by ${artist} via ${method}?${persistentSearch ? '\n\nPersistent search enabled - will auto-retry if failed.' : ''}`)) return;
  _addMbDownloadToSession(releaseId, releaseTitle, artist, method, persistentSearch, selectedSession || null);
}

async function _addMbDownloadToSession(releaseId, releaseTitle, artist, method, persistentSearch, sessionId) {
  try {
    const data = await fetchJsonOrThrow('/api/musicbrainz/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        release_id: releaseId,
        release_title: releaseTitle,
        artist: artist,
        method: method,
        persistent_search: persistentSearch,
        max_retries: MB_DEFAULT_MAX_RETRIES,
        session_id: sessionId
      })
    });
    
    let msg = `Download queued: ${releaseTitle}\nTracking ID: ${data.tracking_id || 'N/A'}`;
    if (data.persistent_search) msg += '\n\n✓ Persistent search enabled - will retry automatically on failure';
    if (data.session_id) msg += `\n✓ Added to session ID: ${data.session_id}`;
    alert(msg);
    setTimeout(refreshMbDownloads, 1000);
  } catch (e) {
    alert('Error initiating download: ' + e.message);
  }
}

async function loadMbSessionSelector() {
  try {
    const data = await fetchJsonOrThrow('/api/playlist-downloads');
    if (!data.sessions) return;
    const selector = document.getElementById('mbSessionSelector');
    if (!selector) return;
    const createOption = selector.querySelector('option[value="create"]');
    Array.from(selector.options).forEach((opt, i) => { if (i > 1) opt.remove(); });
    data.sessions.filter(s => s.status !== 'completed' && s.status !== 'cancelled').forEach(s => {
      const opt = document.createElement('option');
      opt.value = s.id;
      opt.textContent = `${s.session_name} (${s.completed_tracks}/${s.total_tracks})`;
      selector.insertBefore(opt, createOption);
    });
  } catch (e) {
    console.error('Error loading sessions:', e);
  }
}

async function refreshMbDownloads() {
  const loadingEl = document.getElementById('mbDownloadsLoading');
  const errorEl = document.getElementById('mbDownloadsError');
  const resultsEl = document.getElementById('mbDownloadsResults');
  const emptyEl = document.getElementById('mbDownloadsEmpty');
  const tableEl = document.getElementById('mbDownloadsTable');
  const tableBody = document.getElementById('mbDownloadsTableBody');
  const countBadge = document.getElementById('mbDownloadCount');

  if (!loadingEl) return;
  loadingEl.style.display = 'block';
  if (errorEl) errorEl.style.display = 'none';
  if (resultsEl) resultsEl.style.display = 'none';

  try {
    const data = await fetchJsonOrThrow('/api/musicbrainz/downloads');
    loadingEl.style.display = 'none';
    if (resultsEl) resultsEl.style.display = 'block';

    const downloads = data.downloads || [];
    if (downloads.length === 0) {
      if (emptyEl) emptyEl.style.display = 'block';
      if (tableEl) tableEl.style.display = 'none';
      if (countBadge) countBadge.style.display = 'none';
      return;
    }

    if (emptyEl) emptyEl.style.display = 'none';
    if (tableEl) tableEl.style.display = 'block';
    if (countBadge) { countBadge.textContent = downloads.length; countBadge.style.display = 'inline-block'; }

    if (tableBody) {
      tableBody.innerHTML = downloads.map(dl => {
        const statusBadge = getMbStatusBadge(dl.status);
        const canRetry = ['failed', 'error', 'timeout'].includes((dl.status || '').toLowerCase());
        const canRemove = ['completed', 'failed', 'error', 'cancelled'].includes((dl.status || '').toLowerCase());
        const isAwaitingSelection = (dl.status || '').toLowerCase() === 'awaiting_selection' && dl.method === 'slskd';
        const persistentBadge = dl.persistent_search ? ' <span class="badge bg-secondary ms-1" title="Auto-retry enabled"><i class="bi bi-arrow-repeat"></i> Auto-retry</span>' : '';
        const retryInfo = (dl.persistent_search && dl.retry_count) ? `<div><small class="text-muted">(Retry ${dl.retry_count}/${dl.max_retries || MB_DEFAULT_MAX_RETRIES})</small></div>` : '';
        
        return `<tr data-dl-id="${escapeHtml(String(dl.id))}">
          <td>
            <div><strong>${escapeHtml(dl.release_title)}</strong>${persistentBadge}</div>
            ${retryInfo}
            ${dl.total_tracks ? `<small class="text-muted">Tracks: ${dl.completed_tracks}/${dl.total_tracks} completed</small>` : ''}
          </td>
          <td>${escapeHtml(dl.artist)}</td>
          <td class="text-center"><span class="badge ${dl.method === 'slskd' ? 'bg-success' : 'bg-primary'}">${dl.method === 'slskd' ? 'Soulseek' : 'qBittorrent'}</span></td>
          <td class="text-center">${statusBadge}</td>
          <td class="text-center text-muted small">${new Date(dl.created_at).toLocaleString()}</td>
          <td class="text-center">
            <div class="btn-group btn-group-sm">
              ${isAwaitingSelection ? `<button class="btn btn-primary" onclick="showSlskdResults('${dl.id}')"><i class="bi bi-hand-index"></i> Select</button>` : ''}
              ${canRetry ? `<button class="btn btn-outline-warning" onclick="retryMbDownload('${dl.id}')"><i class="bi bi-arrow-clockwise"></i></button>` : ''}
              ${canRemove ? `<button class="btn btn-outline-danger" onclick="removeMbDownload('${dl.id}')"><i class="bi bi-trash"></i></button>` : ''}
            </div>
          </td>
        </tr>`;
      }).join('');
    }
  } catch (error) {
    if (loadingEl) loadingEl.style.display = 'none';
    if (errorEl) { errorEl.textContent = 'Error loading downloads: ' + error.message; errorEl.style.display = 'block'; }
  }
}

async function retryMbDownload(downloadId) {
  if (!confirm('Retry this download?')) return;
  try {
    const data = await fetchJsonOrThrow(`/api/musicbrainz/download/${downloadId}/retry`, { method: 'POST' });
    if (data.success) { alert('Download retry initiated'); refreshMbDownloads(); }
    else alert('Error: ' + (data.error || 'Unknown error'));
  } catch (e) { alert('Error retrying download: ' + e.message); }
}

async function removeMbDownload(downloadId) {
  if (!confirm('Remove this download from the list?')) return;
  try {
    const data = await fetchJsonOrThrow(`/api/musicbrainz/download/${downloadId}`, { method: 'DELETE' });
    if (data.success) refreshMbDownloads();
    else alert('Error: ' + (data.error || 'Unknown error'));
  } catch (e) { alert('Error removing download: ' + e.message); }
}


// ============================================================================
// SOULSEEK SEARCH FUNCTIONS
// ============================================================================

let currentSlskdSearchId = null;
let slskdPollInterval = null;

async function searchSoulseek(event) {
  if (event && event.preventDefault) event.preventDefault();
  const query = document.getElementById('slskdSearchQuery')?.value.trim();
  if (!query) return;

  const emptyEl = document.getElementById('slskdSearchEmpty');
  const resultsEl = document.getElementById('slskdSearchResults');
  if (emptyEl) emptyEl.style.display = 'none';
  if (resultsEl) {
    resultsEl.style.display = 'block';
    resultsEl.innerHTML = '<div class="text-center p-3"><span class="spinner-border spinner-border-sm me-2"></span>Searching Soulseek…</div>';
  }

  if (slskdPollInterval) { clearInterval(slskdPollInterval); slskdPollInterval = null; }

  try {
    const data = await fetchJsonOrThrow('/api/slskd/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query })
    });

    if (data.slotBusy) {
      if (resultsEl) resultsEl.innerHTML = '<div class="alert alert-warning"><i class="bi bi-clock"></i> <strong>Soulseek search slot is busy.</strong> Retrying automatically…</div>';
      const slotPoll = setInterval(async () => {
        try {
          const slotData = await fetchJsonOrThrow('/api/slskd/search-slot');
          if (slotData.slotFree) {
            clearInterval(slotPoll);
            searchSoulseek({ preventDefault: () => {} });
          }
        } catch (_) {}
      }, 2000);
      return;
    }
    
    currentSlskdSearchId = data.searchId;
    slskdPollInterval = setInterval(pollSlskdSearchResults, 1500);
    pollSlskdSearchResults();

  } catch (error) {
    if (resultsEl) resultsEl.innerHTML = `<div class="alert alert-danger">Network error: ${escapeHtml(error.message)}</div>`;
  }
}

async function pollSlskdSearchResults() {
  if (!currentSlskdSearchId) return;
  try {
    const data = await fetchJsonOrThrow(`/api/slskd/search/${encodeURIComponent(currentSlskdSearchId)}`);
    const resultsEl = document.getElementById('slskdSearchResults');
    if (!resultsEl) return;
    
    const results = data.results || [];
    const isComplete = data.isComplete || false;
    const state = data.state || 'Searching';

    if (isComplete && results.length === 0) {
      resultsEl.innerHTML = `<div class="alert alert-info"><i class="bi bi-info-circle"></i> Search complete (${state}). No results found.</div>`;
      if (slskdPollInterval) { clearInterval(slskdPollInterval); slskdPollInterval = null; }
      return;
    }

    let html = '<div class="table-responsive"><table class="table table-hover"><thead><tr><th>File</th><th class="text-center">User</th><th class="text-center">Size</th><th class="text-center">Bitrate</th><th class="text-center">Action</th></tr></thead><tbody>';
    results.forEach(r => {
      const sizeMB = r.size_mb || (r.size ? (r.size / (1024 * 1024)).toFixed(2) : 'N/A');
      html += `<tr>
        <td><div class="small" style="max-width:500px;overflow:hidden;text-overflow:ellipsis;">${escapeHtml(r.filename || 'Unknown')}</div></td>
        <td class="text-center"><small class="text-muted">${escapeHtml(r.username || 'N/A')}</small></td>
        <td class="text-center">${sizeMB} MB</td>
        <td class="text-center"><small class="text-muted">${r.bitrate || 'unknown'}</small></td>
        <td class="text-center"><button class="btn btn-sm btn-success" onclick="downloadSlskdFile('${escapeHtml(r.username)}', '${escapeHtml(r.filename)}', ${parseInt(r.size) || 0})"><i class="bi bi-download"></i> Download</button></td>
      </tr>`;
    });
    html += `</tbody></table></div><div class="text-muted small mt-2">Found ${results.length} result(s) — State: ${escapeHtml(state)}${isComplete ? ' (complete)' : ''}</div>`;
    resultsEl.innerHTML = html;

    if (isComplete && slskdPollInterval) {
      clearInterval(slskdPollInterval);
      slskdPollInterval = null;
    }
  } catch (error) {
    const resultsEl = document.getElementById('slskdSearchResults');
    if (resultsEl) resultsEl.innerHTML = `<div class="alert alert-danger">Network error: ${escapeHtml(error.message)}</div>`;
    if (slskdPollInterval) { clearInterval(slskdPollInterval); slskdPollInterval = null; }
  }
}

// ============================================================================
// QUEUE MANAGEMENT FUNCTIONS
// ============================================================================

let queuePageOffset = 0;
const QUEUE_PAGE_LIMIT = 500;

function getQueuePageUrl(limit, offset) {
  const params = new URLSearchParams({
    limit: String(limit || QUEUE_PAGE_LIMIT),
    offset: String(Math.max(0, offset || queuePageOffset))
  });
  return `/api/downloads/queue?${params.toString()}`;
}

function updateQueuePageControls(totalCount, loadedCount) {
  const summary = document.getElementById('queuePageSummary');
  const prevBtn = document.getElementById('queuePrevPageBtn');
  const nextBtn = document.getElementById('queueNextPageBtn');
  const safeTotal = Number(totalCount || 0);
  const safeLoaded = Number(loadedCount || 0);
  const start = safeTotal === 0 ? 0 : queuePageOffset + 1;
  const end = safeTotal === 0 ? 0 : Math.min(queuePageOffset + safeLoaded, safeTotal);
  
  if (summary) summary.textContent = safeTotal === 0 ? 'Showing 0 of 0' : `Showing ${start}-${end} of ${safeTotal}`;
  if (prevBtn) prevBtn.disabled = queuePageOffset <= 0;
  if (nextBtn) nextBtn.disabled = (queuePageOffset + safeLoaded) >= safeTotal;
}

function changeQueuePage(direction) {
  const nextOffset = Math.max(0, queuePageOffset + (direction * QUEUE_PAGE_LIMIT));
  if (nextOffset === queuePageOffset) return;
  queuePageOffset = nextOffset;
  if (typeof window.loadFolderGroups === 'function') {
      window.loadFolderGroups({ forceRender: true, keepVisibleOnEmpty: true });
  }
}

async function addToQueue(event) {
  event.preventDefault();
  const artist = document.getElementById('queueArtist').value.trim();
  const title = document.getElementById('queueTitle').value.trim();
  const album = document.getElementById('queueAlbum').value.trim();
  const source = document.getElementById('queueSource')?.value || 'soulseek';
  const priority = parseInt(document.getElementById('queuePriority')?.value || 5);
  
  if (!artist) { alert('Please enter an artist.'); return; }
  if (!title && !album) { alert('Please enter either a song title or an album name.'); return; }
  
  if (!title && album) {
    if (confirm('No song title entered. Search MusicBrainz releases for this artist/album instead?')) {
        searchMusicBrainzForQueue();
    }
    return;
  }
  
  try {
    const data = await fetchJsonOrThrow('/api/queue/add', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ artist, title, album, source, priority })
    });
    
    alert(`✅ Added to queue: ${artist} - ${title}`);
    const form = document.getElementById('addToQueueForm');
    if (form) form.reset();
    
    queuePageOffset = 0;
    await loadQueueStatus();
    if (typeof window.loadFolderGroups === 'function') {
        await window.loadFolderGroups({ forceRender: true, keepVisibleOnEmpty: true });
    }
  } catch (error) {
    alert('❌ Network error: ' + error.message);
  }
}

function searchMusicBrainzForQueue() {
  const artist = document.getElementById('queueArtist')?.value.trim() || '';
  const album = document.getElementById('queueAlbum')?.value.trim() || '';
  const track = document.getElementById('queueTitle')?.value.trim() || '';
  
  if (!artist && !album && !track) {
    alert('Please enter at least one field before searching MusicBrainz.');
    return;
  }
  
  window.openGlobalMbSearch(artist, album, (selectedRelease) => {
      downloadMbRelease(selectedRelease.id, selectedRelease.title, selectedRelease.artist, 'slskd');
  }, track);
}

async function loadQueueStatus() {
  try {
    const data = await fetchJsonOrThrow('/api/downloads/queue?limit=1&offset=0');
    if (!data || !data.queue) {
      ['queueTotalCount','queueActiveCount','queueQueuedCount','queueCompletedCount','queueMovingCount','queueFailedCount'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.textContent = '0';
      });
      return;
    }
    const statusCounts = data.status_counts || {};
    const countFor = (...s) => s.reduce((sum, st) => sum + Number(statusCounts[st] || 0), 0);
    const setNum = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = String(val); };
    
    setNum('queueTotalCount', countFor('queued','searching','unmatched','pending_match','discovered','queried','matched','downloading','completed','moving','importing','failed','possible_duplicate','duplicate'));
    setNum('queueQueuedCount', countFor('queued','searching','unmatched','pending_match','discovered','queried','matched'));
    setNum('queueActiveCount', countFor('downloading'));
    setNum('queueCompletedCount', countFor('completed'));
    setNum('queueMovingCount', countFor('moving','importing'));
    setNum('queueFailedCount', countFor('failed'));
  } catch (error) {
    console.error('Error loading queue status:', error);
  }
  // Refresh the queue item lists and logs on whichever page is active.
  await renderQueueSection();
  await renderQueueLog();
  await renderSearchLog();
  await renderQueuePage();
}

async function clearEntireQueue() {
  if (!confirm('Clear the entire queue? This removes queued, failed and completed items but keeps imported records.')) return;
  try {
    const data = await fetchJsonOrThrow('/api/queue/clear', { method: 'DELETE' });
    alert(`✅ Cleared ${data.deleted || 0} item(s) from queue`);
    await loadQueueStatus();
  } catch (e) { alert('❌ Network error: ' + e.message); }
}

// Alias used by the monitor page's "Clear Queue" button.
async function clearQueue() {
  return clearEntireQueue();
}

async function purgeAllQueueAndDownloads() {
  if (!confirm('PURGE ALL? This deletes all queue rows and permanently deletes all files/folders in your configured downloads folder. This cannot be undone.')) return;
  try {
    const data = await fetchJsonOrThrow('/api/queue/purge-all', { method: 'DELETE' });
    alert(`✅ Purge complete\n\nDownloads folder: ${data.downloads_dir || 'unknown'}\nQueue items removed: ${data.queue_items_deleted || 0}\nFiles deleted: ${data.deleted_files || 0}`);
    queuePageOffset = 0;
    await loadQueueStatus();
  } catch (e) { alert('❌ Network error: ' + e.message); }
}

async function retryAllFailed() {
  if (!confirm('Re-queue all failed downloads?')) return;
  try {
    const data = await fetchJsonOrThrow('/api/queue/retry-all-failed', { method: 'POST' });
    alert(`✅ Re-queued ${data.retried || 0} failed item(s)`);
    await loadQueueStatus();
  } catch (e) { alert('❌ Network error: ' + e.message); }
}

async function cleanupCopiedSources() {
  if (!confirm('Delete copied source files from /downloads while keeping queue history?')) return;
  try {
    const data = await fetchJsonOrThrow('/api/queue/cleanup-copied', { method: 'POST' });
    alert(`✅ ${data.message} (scanned ${data.scanned})`);
    await loadQueueStatus();
  } catch (e) { alert('❌ Network error: ' + e.message); }
}

async function loadQueueEvents() {
  try {
    const data = await fetchJsonOrThrow('/api/queue/events?limit=50');
    const events = data.events || [];
    const emptyDiv = document.getElementById('queueEventsEmpty');
    const table = document.getElementById('queueEventsTable');
    const tbody = document.getElementById('queueEventsBody');
    if (!emptyDiv || !table || !tbody) return;
    
    if (events.length === 0) {
      emptyDiv.style.display = 'block';
      table.style.display = 'none';
      return;
    }
    emptyDiv.style.display = 'none';
    table.style.display = 'table';
    tbody.innerHTML = events.map(e => {
      const ts = new Date(e.created_at);
      const badgeClass = e.event_type === 'file_found' ? 'bg-info' : e.event_type === 'status_change' ? 'bg-primary' : e.event_type === 'error' ? 'bg-danger' : 'bg-success';
      return `<tr><td class="small text-muted">${ts.toLocaleString()}</td><td><span class="badge ${badgeClass}">${escapeHtml((e.event_type || '').replace(/_/g, ' ').toUpperCase())}</span></td><td>${escapeHtml(e.message || '')}</td></tr>`;
    }).join('');
  } catch (error) { console.error('Error loading queue events:', error); }
}

function clearQueueEventsLog() {
  const tbody = document.getElementById('queueEventsBody');
  const emptyDiv = document.getElementById('queueEventsEmpty');
  const table = document.getElementById('queueEventsTable');
  if (tbody) tbody.innerHTML = '';
  if (table) table.style.display = 'none';
  if (emptyDiv) emptyDiv.style.display = 'block';
}

async function restartQueueProcessor() {
  const btn = document.getElementById('restartProcessorBtn');
  const originalText = btn ? btn.innerHTML : '';
  try {
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Restarting...'; }
    await fetchJsonOrThrow('/api/queue-processor/restart', { method: 'POST', headers: { 'Content-Type': 'application/json' } });
    if (btn) btn.innerHTML = '<i class="bi bi-check-circle"></i> Restarted!';
    setTimeout(() => {
      if (typeof window.loadQueueStatus === 'function') window.loadQueueStatus();
      if (btn) { btn.innerHTML = originalText; btn.disabled = false; }
    }, 2000);
  } catch (e) {
    if (btn) btn.innerHTML = '<i class="bi bi-exclamation-circle"></i> Error!'; 
    setTimeout(() => { if (btn) { btn.innerHTML = originalText; btn.disabled = false; } }, 3000);
  }
}

async function deleteQueueItem(queueId, deleteDownloadsFile) {
  const promptText = deleteDownloadsFile ? 'Delete this item from queue AND remove its file from /downloads?' : 'Remove from queue?';
  if (!confirm(promptText)) return;
  try {
    const query = deleteDownloadsFile ? '?delete_download_file=1' : '';
    await fetchJsonOrThrow('/api/queue/' + queueId + '/delete' + query, { method: 'DELETE' });
    alert('✅ Removed from queue');
    await loadQueueStatus();
  } catch (e) { alert('❌ Network error: ' + e.message); }
}

async function retryQueueItem(queueId) {
  try {
    await fetchJsonOrThrow('/api/queue/' + queueId + '/requeue', { method: 'POST', headers: { 'Content-Type': 'application/json' } });
    alert('✅ Retrying download...');
    await loadQueueStatus();
  } catch (e) { alert('❌ Network error: ' + e.message); }
}

async function organizeFile(queueId) {
  if (!confirm('Copy file to music library?')) return;
  try {
    await fetchJsonOrThrow('/api/queue/' + queueId + '/organize', { method: 'POST' }, 120000);
    alert('✅ File organized successfully!');
    await loadQueueStatus();
    if (typeof window.loadFolderGroups === 'function') {
        await window.loadFolderGroups({ forceRender: true });
    }
  } catch (e) { alert('❌ Network error: ' + e.message); }
}

async function runQueueCleanup() {
  if (!confirm('Run queue cleanup now?')) return;
  try {
    const data = await fetchJsonOrThrow('/api/queue/cleanup', { method: 'POST' });
    const stats = data.stats || {};
    alert('✅ Cleanup complete\n\nDuplicates removed: ' + (stats.deleted_duplicates || 0) + '\nCompleted albums: ' + (stats.completed_albums || 0));
    await loadQueueStatus();
    if (typeof window.loadFolderGroups === 'function') {
        await window.loadFolderGroups({ forceRender: true });
    }
  } catch (e) { alert('❌ Network error: ' + e.message); }
}

async function organizeSelected() { alert('organizeSelected not yet implemented'); }
async function batchOrganizeSelected() { alert('batchOrganizeSelected not yet implemented'); }

// ============================================================================
// QUEUE RENDERING (element-guarded — safe on any page that loads downloads.js)
// ============================================================================

// Renders the monitor page's Download Queue section: grouped folders first,
// falling back to the real download_queue rows, then an empty state.
async function renderQueueSection() {
  const section = document.getElementById('folderGroupsSection');
  const list = document.getElementById('folderGroupsList');
  const badge = document.getElementById('folderGroupsBadge');
  if (!section || !list) return;
  section.style.display = 'block';

  let groups = [];
  try {
    const data = await fetchJsonOrThrow('/api/downloads/grouped-folders');
    if (data && data.success) groups = data.folder_groups || [];
  } catch (error) {
    console.error('Error loading folder groups, falling back to queue items:', error);
  }

  if (groups.length === 0) {
    try {
      const qd = await fetchJsonOrThrow('/api/downloads/queue?limit=200');
      const qItems = (qd && qd.queue) || [];
      if (qItems.length > 0) {
        if (badge) badge.textContent = qItems.length + ' items';
        list.innerHTML = '<div class="list-group list-group-flush">' +
          qItems.map(function(item) {
            const st = item.status || 'queued';
            const badgeCls = st === 'failed' ? 'danger' : (st === 'downloading' ? 'warning' : 'info');
            return '<div class="list-group-item"><div class="d-flex justify-content-between align-items-center">' +
              '<div><strong>' + escapeHtml(item.title || 'Unknown') + '</strong>' +
              (item.artist ? '<br><small class="text-muted">' + escapeHtml(item.artist) + (item.album ? ' - ' + escapeHtml(item.album) : '') + '</small>' : '') +
              '</div><span class="badge bg-' + badgeCls + '">' + escapeHtml(st) + '</span></div></div>';
          }).join('') + '</div>';
        if (typeof updateQueuePageControls === 'function') updateQueuePageControls(qItems.length, qItems.length);
        return;
      }
    } catch (error) {
      console.error('Error loading queue fallback:', error);
    }
    if (badge) badge.textContent = '0 items';
    list.innerHTML = '<div class="alert alert-info m-3"><i class="bi bi-info-circle"></i> No items in queue right now.</div>';
    return;
  }

  if (badge) badge.textContent = groups.length + ' items';
  list.innerHTML = '<div class="list-group list-group-flush">' +
    groups.map(function(g) {
      const name = g.folder_name || g.folder_path || g.name || 'Unknown';
      const artist = g.artist || '';
      const album = g.album || '';
      const trackCount = g.track_count || (g.tracks ? g.tracks.length : 0);
      return '<div class="list-group-item"><div class="d-flex justify-content-between"><div><strong>' + escapeHtml(name) + '</strong>' +
        (artist ? '<br><small class="text-muted">' + escapeHtml(artist) + (album ? ' - ' + escapeHtml(album) : '') + '</small>' : '') +
        '</div><span class="badge bg-info">' + trackCount + ' tracks</span></div></div>';
    }).join('') + '</div>';
  if (typeof updateQueuePageControls === 'function') updateQueuePageControls(groups.length, groups.length);
}

// Loads the monitor page's Queue Activity Log.
async function renderQueueLog() {
  const logEl = document.getElementById('queueActivityLog');
  if (!logEl) return;
  try {
    const data = await fetchJsonOrThrow('/api/queue/events?limit=100');
    const events = (data && data.events) || [];
    const lines = events.slice().reverse().map(function(event) {
      const ts = event.created_at || event.timestamp || null;
      const timeLabel = ts ? new Date(ts).toLocaleTimeString([], { hour12: false }) : '--:--:--';
      return '[' + timeLabel + '] ' + (event.event_type || 'info').toUpperCase() + ' ' + (event.message || '');
    });
    logEl.textContent = lines.length ? lines.join('\n') : 'No queue events yet.';
    logEl.scrollTop = logEl.scrollHeight;
  } catch (error) {
    console.error('Error loading queue log:', error);
  }
}

// Loads the monitor page's Soulseek Search Log.
async function renderSearchLog() {
  const logEl = document.getElementById('soulseekSearchLog');
  if (!logEl) return;
  try {
    const data = await fetchJsonOrThrow('/api/queue/search-events?limit=100');
    const events = (data && data.events) || [];
    const chunks = [];
    events.slice().reverse().forEach(function(event) {
      const ts = event.timestamp ? new Date(event.timestamp) : null;
      const timeLabel = ts ? ts.toLocaleTimeString([], { hour12: false }) : '--:--:--';
      const type = (event.search_type || 'unknown').toUpperCase();
      chunks.push('[' + type + '] [' + timeLabel + '] Query: "' + (event.query || '') + '"  |  ' + (event.artist || '') + ' - ' + (event.title || ''));
      chunks.push('    Results: ' + (event.result_count ?? 0) + '  Duration: ' + (event.duration_seconds != null ? event.duration_seconds + 's' : 'n/a'));
    });
    logEl.textContent = chunks.length ? chunks.join('\n') : 'No Soulseek search events yet.';
    logEl.scrollTop = logEl.scrollHeight;
  } catch (error) {
    console.error('Error loading search log:', error);
  }
}

// Renders the /downloads Active Queue tab (stats bar + lists).
async function renderQueuePage() {
  const activeList = document.getElementById('activeQueueList');
  if (!activeList) return;
  try {
    const data = await fetchJsonOrThrow('/api/downloads/queue?limit=500');
    const statusCounts = (data && data.status_counts) || {};
    const countFor = (...s) => s.reduce((sum, st) => sum + Number(statusCounts[st] || 0), 0);
    const setNum = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = String(val); };

    setNum('statQueuedNum', countFor('queued','searching','unmatched','pending_match','discovered','queried','matched'));
    setNum('statDownloadingNum', countFor('downloading'));
    setNum('statCompletedNum', countFor('completed'));
    setNum('statFailedNum', countFor('failed'));
    setNum('statImportedNum', countFor('imported','moving'));
    setNum('queueActiveCount', countFor('queued','searching','downloading','failed'));
    setNum('queueCompletedCount', countFor('completed'));
    setNum('queueFailedCount', countFor('failed'));
    const lastRefreshed = document.getElementById('queueLastRefreshed');
    if (lastRefreshed) lastRefreshed.textContent = 'Updated ' + new Date().toLocaleTimeString([], { hour12: false });

    const items = (data && data.queue) || [];
    const completed = (data && data.completed) || [];
    renderQueueList('active', items.filter(i => i.status !== 'failed' && i.status !== 'completed'));
    renderQueueList('completed', completed.filter(i => (i.status || 'completed') !== 'failed'));
    renderQueueList('failed', items.filter(i => i.status === 'failed'));

    const retryAllBtn = document.getElementById('retryAllBtn');
    if (retryAllBtn) retryAllBtn.style.display = Number(statusCounts.failed || 0) > 0 ? 'inline-block' : 'none';
  } catch (error) {
    console.error('Error rendering queue page:', error);
  }
}

function renderQueueList(kind, items) {
  const idPrefix = kind === 'active' ? 'active' : kind;
  const listEl = document.getElementById(idPrefix + 'QueueList');
  const emptyEl = document.getElementById(idPrefix + 'QueueEmpty');
  const badgeEl = document.getElementById(kind === 'active' ? 'queueActiveCount' : kind + 'Badge');
  if (!listEl || !emptyEl) return;
  if (!items.length) {
    listEl.style.display = 'none';
    emptyEl.style.display = 'block';
    if (badgeEl) badgeEl.style.display = 'none';
    return;
  }
  emptyEl.style.display = 'none';
  listEl.style.display = 'block';
  if (badgeEl) { badgeEl.textContent = items.length + ' item' + (items.length !== 1 ? 's' : ''); badgeEl.style.display = 'inline-block'; }

  const rows = items.map(function(item) {
    const st = item.status || 'queued';
    const badgeCls = st === 'failed' ? 'danger' : st === 'downloading' ? 'warning' : st === 'completed' ? 'success' : 'info';
    let actions = '';
    if (kind === 'failed') {
      actions += '<button class="btn btn-sm btn-outline-warning" title="Retry" onclick="retryQueueItem(' + item.id + ')"><i class="bi bi-arrow-clockwise"></i></button>';
    }
    if (kind === 'completed') {
      actions += '<button class="btn btn-sm btn-outline-primary" title="Copy to library" onclick="organizeFile(' + item.id + ')"><i class="bi bi-folder-plus"></i></button>';
    }
    actions += '<button class="btn btn-sm btn-outline-danger" title="Remove" onclick="deleteQueueItem(' + item.id + ', false)"><i class="bi bi-trash"></i></button>';
    return '<div class="list-group-item"><div class="d-flex justify-content-between align-items-center gap-2">' +
      '<div class="text-truncate"><strong>' + escapeHtml(item.title || item.album || 'Unknown') + '</strong>' +
      (item.artist ? '<br><small class="text-muted">' + escapeHtml(item.artist) + (item.album && item.album !== item.title ? ' - ' + escapeHtml(item.album) : '') + '</small>' : '') +
      '</div><div class="d-flex align-items-center gap-2 flex-shrink-0">' +
      '<span class="badge bg-' + badgeCls + '">' + escapeHtml(st) + '</span>' + actions +
      '</div></div></div>';
  });
  listEl.innerHTML = '<div class="list-group list-group-flush">' + rows.join('') + '</div>';
}

// ============================================================================
// INITIALIZATION
// ============================================================================

document.addEventListener('DOMContentLoaded', function() {
  const qbitInput = document.getElementById('qbitSearchInput');
  if (qbitInput) {
    qbitInput.addEventListener('keypress', function(e) {
      if (e.key === 'Enter') performQbitSearch();
    });
  }

  if (document.getElementById('qbitMonLoading')) {
    refreshQbitMonitor();
    setInterval(() => refreshQbitMonitor({ silent: true }), 5000);
  }

  if (document.getElementById('upcomingReleases')) {
    refreshUpcomingReleases();
  }

  if (document.getElementById('mbSearchInput')) {
    document.getElementById('mbSearchInput').addEventListener('keypress', function(e) {
      if (e.key === 'Enter') {
          if (typeof performMbSearch === 'function') performMbSearch();
      }
    });
    loadMbSessionSelector();
    refreshMbDownloads();
  }

  // Populate the monitor page's queue section + logs and the /downloads
  // Active Queue tab on load (loadQueueStatus refreshes counts AND these).
  if (document.getElementById('folderGroupsSection') || document.getElementById('statQueuedNum')) {
    loadQueueStatus();
  }
  if (document.getElementById('queueEventsBody')) {
    loadQueueEvents();
  }
});