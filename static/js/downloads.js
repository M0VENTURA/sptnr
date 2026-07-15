// ===== DOWNLOAD PAGES JAVASCRIPT =====
// This file contains all JavaScript functions for the downloads pages

// ===== UTILITY FUNCTIONS =====

/**
 * Safely fetch and parse JSON, throwing an error if the HTTP status is not OK.
 * This prevents "Unexpected token '<'" errors when the server returns HTML error pages.
 */
async function fetchJsonOrThrow(url, options = {}, timeoutMs = 30000) {
  const controller = new AbortController();
  const mergedOptions = { ...options, signal: options?.signal || controller.signal };
  const timeoutId = setTimeout(() => {
    if (!options?.signal) {
      controller.abort();
    }
  }, timeoutMs);

  let response;
  let raw;
  try {
    response = await fetch(url, mergedOptions);
    raw = await response.text();
  } catch (error) {
    if (error?.name === 'AbortError') {
      if (options?.signal?.aborted) {
        throw error;
      }
      throw new Error(`Request timed out after ${Math.round(timeoutMs / 1000)}s`);
    }
    throw error;
  } finally {
    clearTimeout(timeoutId);
  }

  if (response.status === 524 || response.status === 504 || response.status === 502) {
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

function formatBytes(bytes) {
  if (bytes === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return Math.round((bytes / Math.pow(k, i)) * 100) / 100 + ' ' + sizes[i];
}

function formatDuration(rawValue) {
  if (rawValue == null || rawValue === '') return 'Unknown';

  const n = Number(rawValue);
  if (!Number.isFinite(n) || n <= 0) return 'Unknown';

  // MusicBrainz values can be seconds, milliseconds, or microseconds.
  // Keep this aligned with backend normalization (>10000 => likely ms).
  let seconds;
  if (n >= 100000000) {
    seconds = n / 1000000; // likely microseconds
  } else if (n > 10000) {
    seconds = n / 1000; // likely milliseconds
  } else {
    seconds = n; // likely seconds
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

// ===== ENCODING / STRING UTILITY FUNCTIONS =====

/**
 * Encode a JS value for safe inclusion in single-quoted onclick attribute strings.
 * Combines JSON.stringify + encodeURIComponent, then escapes apostrophes.
 */
function encodeInlineArg(value) {
  return encodeURIComponent(JSON.stringify(value)).replace(/'/g, '%27');
}

/**
 * Decode a value previously encoded with encodeInlineArg.
 */
function decodeInlineArg(value, fallback = null) {
  try {
    return JSON.parse(decodeURIComponent(value));
  } catch (error) {
    console.warn('Failed to decode inline argument:', error);
    return fallback;
  }
}

/**
 * Normalize a Soulseek search query by removing HTML entities and extra whitespace.
 */
function normalizeSoulseekQuery(value) {
  return String(value || '')
    .replace(/\\u0026/gi, ' ')
    .replace(/&amp;/gi, ' ')
    .replace(/&/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

/**
 * Normalize track text for fuzzy comparison: lowercase, strip brackets/parens, remove non-alphanumeric.
 */
function normalizeTrackText(value) {
  return String(value || '')
    .toLowerCase()
    .replace(/\([^)]*\)/g, ' ')
    .replace(/\[[^\]]*\]/g, ' ')
    .replace(/[^a-z0-9]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

/**
 * Compute a similarity score (0-1) between two track title strings.
 * Handles "feat", remaster variants, and substring matches.
 */
function getTrackSimilarity(a, b) {
  const x = normalizeTrackText(a);
  const y = normalizeTrackText(b);
  if (!x || !y) return 0;
  if (x === y) return 1;
  if (x.includes(y) || y.includes(x)) return 0.9;

  const xWords = x.split(' ');
  const yWords = y.split(' ');
  const ySet = new Set(yWords);
  const common = xWords.filter(w => ySet.has(w)).length;
  const maxLen = Math.max(xWords.length, yWords.length);
  let score = maxLen > 0 ? (common / maxLen) : 0;

  if (score >= 0.34 && (x.includes(' feat ') || y.includes(' feat ') || x.includes(' remaster') || y.includes(' remaster'))) {
    score = Math.max(score, 0.5);
  }

  return score;
}

/**
 * Given an array of items that may have release_mbid or release_id,
 * return the most common release ID, or null if none found.
 */
function getCommonReleaseId(items) {
  const ids = items.map(i => i.release_mbid || i.release_id).filter(Boolean);
  if (ids.length === 0) return null;
  const first = ids[0];
  if (ids.every(id => id === first)) return first;
  const counts = {};
  let maxCount = 0;
  let maxId = null;
  for (const id of ids) {
    counts[id] = (counts[id] || 0) + 1;
    if (counts[id] > maxCount) {
      maxCount = counts[id];
      maxId = id;
    }
  }
  return maxId;
}

/**
 * Sanitize a string for safe use as an HTML element ID or CSS selector.
 */
function sanitizeId(id) {
  return String(id).replace(/[^a-zA-Z0-9_-]/g, '_');
}

/**
 * Make a safe DOM ID from an arbitrary value (max 120 chars).
 */
function makeSafeDomId(value) {
  return String(value || '')
    .replace(/[^a-zA-Z0-9_-]/g, '_')
    .replace(/_+/g, '_')
    .slice(0, 120);
}

/**
 * Classify a track file path to determine its type (remote, local downloads, local music, etc.).
 */
function classifyTrackPath(pathValue, fieldName) {
  const raw = String(pathValue || '').trim();
  if (!raw) return { kind: 'unknown', label: 'Path' };

  const normalized = raw.replace(/\\/g, '/').toLowerCase();
  const sourceField = String(fieldName || '').toLowerCase();

  if (sourceField === 'source_file_path') {
    return { kind: 'remote', label: 'Remote path' };
  }

  if (sourceField === 'source_music_path') {
    return { kind: 'local', label: 'Local music path' };
  }

  if (/^(https?|ftp|smb):\/\//i.test(raw) || raw.startsWith('\\\\')) {
    return { kind: 'remote', label: 'Remote path' };
  }

  if (normalized.startsWith('/downloads/') || normalized.includes('/downloads/')) {
    return { kind: 'local', label: 'Local downloads path' };
  }

  if (normalized.startsWith('/music/') || normalized.includes('/music/')) {
    return { kind: 'local', label: 'Local music path' };
  }

  if (/^[a-z]:\//i.test(normalized)) {
    return { kind: 'local', label: 'Local path' };
  }

  return { kind: 'unknown', label: 'Path' };
}

// ===== qBITTORRENT FUNCTIONS =====

function performQbitSearch() {
  const query = document.getElementById('qbitSearchInput').value;
  if (!query) return;
  
  document.getElementById('qbitLoading').style.display = 'block';
  document.getElementById('qbitResults').innerHTML = '';
  
  fetchJsonOrThrow('/api/qbittorrent/search', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query: query })
  })
  .then(data => {
    document.getElementById('qbitLoading').style.display = 'none';
    
    if (data.error) {
      document.getElementById('qbitResults').innerHTML = `
        <div class="alert alert-danger">
          <i class="bi bi-exclamation-triangle"></i> ${data.error}
        </div>
      `;
      return;
    }
    
    const results = data.results || [];
    
    if (results.length === 0) {
      document.getElementById('qbitResults').innerHTML = `
        <div class="alert alert-info">
          <i class="bi bi-info-circle"></i> No results found.
        </div>
      `;
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
            <button class="btn btn-sm btn-success" onclick="addTorrent('${escapeHtml(result.fileUrl)}')"
              ${!result.fileUrl ? 'disabled' : ''}>
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
  })
  .catch(error => {
    document.getElementById('qbitLoading').style.display = 'none';
    document.getElementById('qbitResults').innerHTML = `
      <div class="alert alert-danger">
        <i class="bi bi-exclamation-triangle"></i> ${error.message}
      </div>
    `;
  });
}

function addTorrent(url) {
  if (!url || !confirm('Add this torrent to qBittorrent?')) return;
  
  fetchJsonOrThrow('/api/qbittorrent/add', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url: url })
  })
  .then(data => {
    if (data.success) {
      alert('✓ Torrent added successfully!');
    } else {
      alert('✗ Error: ' + (data.error || 'Failed to add torrent'));
    }
  })
  .catch(error => {
    alert('✗ Network error: ' + error.message);
  });
}

let qbitMonLoaded = false;
let qbitMonInFlight = false;

function refreshQbitMonitor(options = {}) {
  const silent = options.silent === true;
  const loading = document.getElementById('qbitMonLoading');
  if (!loading) return;
  const errorBox = document.getElementById('qbitMonError');
  const results = document.getElementById('qbitMonResults');
  const empty = document.getElementById('qbitMonEmpty');
  const table = document.getElementById('qbitMonTable');
  const tbody = document.getElementById('qbitMonTableBody');
  const countBadge = document.getElementById('qbitMonCount');

  if (qbitMonInFlight) return;
  qbitMonInFlight = true;

  if (!qbitMonLoaded && !silent) {
    loading.style.display = 'block';
  }

  fetch('/api/qbittorrent/status')
    .then(resp => resp.json())
    .then(data => {
      if (loading) loading.style.display = 'none';
      qbitMonLoaded = true;

      if (data.error) {
        if (errorBox) {
          errorBox.textContent = 'Error: ' + data.error;
          errorBox.style.display = 'block';
        }
        if (results) results.style.display = 'none';
        qbitMonInFlight = false;
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
        qbitMonInFlight = false;
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
        const hash = torrent.hash || '';
        const isRunning = !state.includes('stalled') && !state.includes('paused') && state !== 'stopped';
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
              <div class="progress-bar ${torrent.progress >= 100 ? 'bg-success' : 'bg-primary'}" role="progressbar" style="width: ${torrent.progress}%" aria-valuenow="${torrent.progress}" aria-valuemin="0" aria-valuemax="100">${torrent.progress}%</div>
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

      qbitMonInFlight = false;
    })
    .catch(err => {
      if (!silent && loading) {
        loading.style.display = 'none';
      }
      if (errorBox) {
        errorBox.textContent = 'Network error: ' + err.message;
        errorBox.style.display = 'block';
      }
      qbitMonInFlight = false;
    });
}

function forceStartQbitTorrent(hash) {
  if (!hash) return;
  fetch('/api/qbittorrent/force-start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ hash })
  })
    .then(resp => resp.json())
    .then(data => {
      if (data.success) {
        refreshQbitMonitor({ silent: true });
      } else {
        alert('✗ Error: ' + (data.error || 'Failed to force start'));
      }
    })
    .catch(err => alert('✗ Network error: ' + err.message));
}

function stopQbitTorrent(hash) {
  if (!hash) return;
  if (!confirm('Stop this torrent?')) return;
  fetch('/api/qbittorrent/stop', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ hash })
  })
    .then(resp => resp.json())
    .then(data => {
      if (data.success) {
        refreshQbitMonitor({ silent: true });
      } else {
        alert('✗ Error: ' + (data.error || 'Failed to stop torrent'));
      }
    })
    .catch(err => alert('✗ Network error: ' + err.message));
}

// ===== UPCOMING RELEASES FUNCTIONS =====

async function clearUpcomingReleases() {
  if (!confirm('Are you sure you want to clear all upcoming releases from the database? This cannot be undone.')) {
    return;
  }

  const statusEl = document.getElementById('upcomingStatus');
  const statusText = document.getElementById('upcomingStatusText');
  const errorEl = document.getElementById('upcomingError');
  
  statusEl.style.display = 'block';
  errorEl.style.display = 'none';
  statusText.textContent = 'Clearing database...';
  
  try {
    const data = await fetchJsonOrThrow('/api/upcoming-releases/clear', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' }
    });
    
    statusText.textContent = `✓ ${data.message}`;
    setTimeout(() => {
      statusEl.style.display = 'none';
      document.getElementById('upcomingReleases').innerHTML = `
        <div class="text-center py-5">
          <p class="text-muted">Database cleared. Click <strong>Check for Updates</strong> to search MusicBrainz for new release data.</p>
        </div>
      `;
    }, 2000);
    
  } catch (error) {
    console.error('Error clearing database:', error);
    statusEl.style.display = 'none';
    errorEl.style.display = 'block';
    errorEl.innerHTML = `
      <i class="bi bi-exclamation-triangle"></i>
      <strong>Error clearing database:</strong> ${error.message}
    `;
  }
}

async function scrapeUpcomingReleases() {
  const statusEl = document.getElementById('upcomingStatus');
  const statusText = document.getElementById('upcomingStatusText');
  const errorEl = document.getElementById('upcomingError');
  
  statusEl.style.display = 'block';
  errorEl.style.display = 'none';
  statusText.textContent = 'Scraping Wikipedia for upcoming releases...';
  
  try {
    const data = await fetchJsonOrThrow('/api/upcoming-releases/scrape', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' }
    });
    
    statusText.textContent = `✓ ${data.message}`;
    setTimeout(() => {
      statusEl.style.display = 'none';
      refreshUpcomingReleases();
    }, 2000);
    
  } catch (error) {
    console.error('Error scraping:', error);
    statusEl.style.display = 'none';
    errorEl.style.display = 'block';
    errorEl.innerHTML = `
      <i class="bi bi-exclamation-triangle"></i>
      <strong>Error scraping Wikipedia:</strong> ${error.message}
    `;
  }
}

async function checkForUpdates() {
  localStorage.setItem('upcomingReleasesLastChecked', Date.now().toString());
  await refreshUpcomingReleases();
}

async function refreshUpcomingReleases() {
  const container = document.getElementById('upcomingReleases');
  const filterCollection = document.getElementById('upcomingFilterCollection').checked;
  
  container.innerHTML = `
    <div class="text-center py-4">
      <div class="spinner-border text-primary spinner-border-sm" role="status">
        <span class="visually-hidden">Loading...</span>
      </div>
      <p class="mt-2 small">Loading upcoming releases...</p>
    </div>
  `;
  
  try {
    const data = await fetchJsonOrThrow(`/api/upcoming-releases?collection=${filterCollection}&include_queue=true`);
    
    if (!data.releases || data.releases.length === 0) {
      container.innerHTML = `
        <div class="alert alert-info">
          <i class="bi bi-info-circle"></i>
          No upcoming releases found. Click <strong>Check for Updates</strong> to search MusicBrainz for new and upcoming releases.
        </div>
      `;
      return;
    }
    
    // Filter to only show albums NOT in collection
    let releases = data.releases;
    if (filterCollection) {
      releases = releases.filter(r => !r.album_in_collection);
      if (releases.length === 0) {
        container.innerHTML = `
          <div class="alert alert-info">
            <i class="bi bi-check-circle"></i> You have all upcoming releases from artists in your collection!
          </div>
        `;
        return;
      }
    }
    
    // Group by month
    const grouped = {};
    releases.forEach(release => {
      const date = release.release_date || 'Unknown Date';
      const month = date.substring(0, 7); // YYYY-MM
      if (!grouped[month]) grouped[month] = [];
      grouped[month].push(release);
    });
    
    // Sort months
    const sortedMonths = Object.keys(grouped).sort();
    
    let html = '<div class="accordion" id="releaseAccordion">';
    
    sortedMonths.forEach((month, idx) => {
      const monthReleases = grouped[month];
      const monthLabel = new Date(month + '-01').toLocaleDateString('en-US', {
        year: 'numeric',
        month: 'long'
      });
      
      html += `
        <div class="accordion-item">
          <h2 class="accordion-header" id="heading${idx}">
            <button class="accordion-button ${idx > 0 ? 'collapsed' : ''}" type="button" data-bs-toggle="collapse" 
              data-bs-target="#collapse${idx}" aria-expanded="${idx === 0}">
              <strong>${monthLabel}</strong>
              <span class="badge bg-primary ms-2">${monthReleases.length}</span>
            </button>
          </h2>
          <div id="collapse${idx}" class="accordion-collapse collapse ${idx === 0 ? 'show' : ''}" 
            data-bs-parent="#releaseAccordion">
            <div class="accordion-body p-0">
              <div class="table-responsive">
                <table class="table table-dark table-hover table-sm mb-0">
                  <thead>
                    <tr>
                      <th>Artist</th>
                      <th>Album</th>
                      <th>Date</th>
                      <th style="width: 120px;">Action</th>
                    </tr>
                  </thead>
                  <tbody>
      `;
      
      monthReleases.forEach(release => {
        let albumStatus = '';
        if (release.album_in_collection) {
          albumStatus = ' <span class="badge bg-success ms-1">In Collection</span>';
        } else if (release.in_queue) {
          albumStatus = ' <span class="badge bg-warning text-dark ms-1">Downloading</span>';
        }

        let artistStatus = '';
        if (release.artist_in_collection) {
          artistStatus += ' <span class="badge bg-success ms-1">Artist in Collection</span>';
        }
        if (release.artist_in_recommended) {
          artistStatus += ' <span class="badge bg-warning text-dark ms-1">Artist in Recommended</span>';
        }

        // Build JS-safe string literals for inline onclick arguments.
        const artistArg = JSON.stringify(String(release.artist_name || ''));
        const albumArg = JSON.stringify(String(release.album_name || ''));
        
        html += `
          <tr>
            <td>${escapeHtml(release.artist_name)}${artistStatus}</td>
            <td>${escapeHtml(release.album_name)}${albumStatus}</td>
            <td><small>${release.release_date || 'TBA'}</small></td>
            <td>
              <button type="button" class="btn btn-sm btn-outline-primary" title="Search on MusicBrainz"
                onclick='searchMusicBrainzRelease(event, ${artistArg}, ${albumArg})'>
                <i class="bi bi-search"></i> Search
              </button>
            </td>
          </tr>
        `;
      });
      
      html += `
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </div>
      `;
    });
    
    html += '</div>';
    container.innerHTML = html;
    
  } catch (error) {
    console.error('Error loading releases:', error);
    container.innerHTML = `
      <div class="alert alert-danger">
        <i class="bi bi-exclamation-triangle"></i>
        <strong>Error loading releases:</strong> ${error.message}
      </div>
    `;
  }
}

// MusicBrainz release search for Wikipedia upcoming releases (with Discogs fallback)
async function searchMusicBrainzRelease(event, artist, album, upcomingReleaseId = null) {
  if (event) {
    event.preventDefault();
    event.stopPropagation();
  }

  window.currentUpcomingReleaseContext = upcomingReleaseId ? {
    releaseId: upcomingReleaseId,
    artist,
    album
  } : null;

  const modalEl = document.getElementById('musicBrainzModal');
  const statusEl = document.getElementById('mbSearchStatus');
  const errorEl = document.getElementById('mbSearchError');
  const resultsEl = document.getElementById('mbSearchResults');
  const infoEl = document.getElementById('mbSearchInfo');
  const infoArtistEl = document.getElementById('mbSearchArtist');
  const infoAlbumEl = document.getElementById('mbSearchAlbum');

  if (!modalEl || !statusEl || !errorEl || !resultsEl) {
    alert('Search UI not available on this page.');
    return;
  }

  // Check if Bootstrap JS is loaded and available
  const hasBootstrapModal = !!(window.bootstrap && window.bootstrap.Modal);
  
  if (hasBootstrapModal) {
    // Use Bootstrap modal
    const modal = new bootstrap.Modal(modalEl);
    modal.show();
  } else {
    // Fallback for environments where Bootstrap JS is not loaded
    modalEl.style.display = 'block';
    modalEl.classList.add('show');
    // Add backdrop if it doesn't exist
    let backdrop = document.querySelector('.modal-backdrop');
    if (!backdrop) {
      backdrop = document.createElement('div');
      backdrop.className = 'modal-backdrop fade show';
      document.body.appendChild(backdrop);
    }
    document.body.classList.add('modal-open');
  }

  if (infoEl && infoArtistEl && infoAlbumEl) {
    infoArtistEl.textContent = artist || '';
    infoAlbumEl.textContent = album || '';
    infoEl.style.display = 'block';
  } else if (infoEl) {
    // Fallback for pages where child spans are absent
    infoEl.innerHTML = (album && album.trim())
      ? `Searching <strong>${escapeHtml(artist)}</strong> — <strong>${escapeHtml(album)}</strong>`
      : `Searching <strong>${escapeHtml(artist)}</strong>`;
    infoEl.style.display = 'block';
  }

  statusEl.style.display = 'block';
  statusEl.innerHTML = '<div class="spinner-border spinner-border-sm me-2"></div>Searching MusicBrainz...';
  errorEl.style.display = 'none';
  resultsEl.innerHTML = '';

  const parseJsonResponse = async (resp, sourceName) => {
    const contentType = (resp.headers.get('content-type') || '').toLowerCase();
    if (!contentType.includes('application/json')) {
      const raw = await resp.text();
      const htmlResponse = raw && raw.trim().startsWith('<');
      const hint = htmlResponse
        ? 'Received HTML instead of JSON (possible auth/session redirect or server error).'
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
      statusEl.style.display = 'none';
      displayMusicBrainzResults(data.results);
      return;
    }

    if (isArtistOnlySearch) {
      statusEl.style.display = 'none';
      errorEl.textContent = 'No releases found on MusicBrainz for this artist';
      errorEl.style.display = 'block';
      return;
    }

    statusEl.innerHTML = '<div class="spinner-border spinner-border-sm me-2"></div>Searching Discogs fallback...';

    const discogsResponse = await fetch('/api/upcoming-releases/search-discogs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ artist, album })
    });
    const discogsData = await parseJsonResponse(discogsResponse, 'Discogs');

    statusEl.style.display = 'none';

    const allResults = [];
    if (Array.isArray(data.results)) allResults.push(...data.results);
    if (discogsData.success && Array.isArray(discogsData.results)) allResults.push(...discogsData.results);

    if (allResults.length === 0) {
      errorEl.textContent = 'No releases found on MusicBrainz or Discogs';
      errorEl.style.display = 'block';
      return;
    }

    displayMusicBrainzResults(allResults);
  } catch (error) {
    statusEl.style.display = 'none';
    errorEl.textContent = 'Error searching: ' + error.message;
    errorEl.style.display = 'block';
  }
}

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
      year: release.date || release.year || null,
      release_id: release.release_id || null,
      release_group_id: release.release_group_id || null,
      source: release.source || 'musicbrainz'
    };

    const source = release.source || 'musicbrainz';
    const sourceBadge = source === 'discogs'
      ? '<span class="badge bg-info ms-2">Discogs</span>'
      : '<span class="badge ms-2" style="background:#f06a34;color:#fff;border-radius:999px;"><i class="bi bi-hexagon-fill me-1" style="color:#d45aa3;"></i>MusicBrainz</span>';

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
        const modal = bootstrap.Modal.getInstance(modalEl);
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
  let managedPathFailure = null;

  try {
    // For full release downloads with a known release_id, use the managed release flow.
    // This creates a monitoring folder under /downloads/Music and enqueues tracks first.
    if (release_id && tracks.length > 1) {
      try {
        const startData = await fetchJsonOrThrow(
          `/api/musicbrainz/release/${encodeURIComponent(release_id)}/start`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              release_title: album,
              artist,
              method: 'slskd'
            })
          },
          60000
        );

        if (startData && startData.success) {
          const created = startData.queue_items_created || tracks.length;
          alert(`Created monitoring release and queued ${created} tracks: ${artist} - ${album}`);
          if (closeModal) {
            const modalEl = document.getElementById('musicBrainzModal');
            if (modalEl && window.bootstrap && window.bootstrap.Modal) {
              const existingModal = bootstrap.Modal.getInstance(modalEl);
              if (existingModal) existingModal.hide();
            }
          }

          try {
            const timestamp = Date.now();
            localStorage.setItem('sptnr_queue_updated', timestamp.toString());
          } catch (e) {
            console.warn('Could not update localStorage:', e);
          }
          return true;
        }

        managedPathFailure = (startData && startData.error)
          ? String(startData.error)
          : 'Managed release start did not return success';
      } catch (managedErr) {
        managedPathFailure = managedErr && managedErr.message
          ? managedErr.message
          : String(managedErr);
      }

      if (managedPathFailure) {
        console.warn(
          `[MB_DOWNLOAD] Managed release start failed for ${artist} - ${album} (${release_id}); falling back to batch queue add: ${managedPathFailure}`
        );
      }
    }

    let releaseYear = null;
    if (year) {
      const yearStr = String(year);
      releaseYear = yearStr.substring(0, 4);
    }

    const trackItems = tracks.map(track => ({
      artist,
      title: track.title,
      album,
      source: 'soulseek',
      priority: 5,
      track_number: track.position || null,
      album_artist: artist,
      year: releaseYear,
      release_id,
      release_source: source || 'musicbrainz'
    }));

    const import_group = `${artist}_${album}`.replace(/\s+/g, '_').substring(0, 100);

    const response = await fetch('/api/queue/add-batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        items: trackItems,
        import_group,
        import_type: 'album'
      })
    });

    const data = await response.json();
    if (data.success) {
      const label = selectionLabel ? ` ${selectionLabel}` : ' tracks';
      const fallbackNote = managedPathFailure ? '\n(Managed release start failed; used direct queue add fallback.)' : '';
      alert(`Added ${tracks.length}${label} to queue: ${artist} - ${album}${fallbackNote}`);
      if (closeModal) {
        const modalEl = document.getElementById('musicBrainzModal');
        if (modalEl) {
          if (window.bootstrap && window.bootstrap.Modal) {
            const existingModal = bootstrap.Modal.getInstance(modalEl);
            if (existingModal) existingModal.hide();
          }
        }
      }
      
      // Try to trigger refresh on monitor page if it's open in another tab
      try {
        // Use localStorage to notify other tabs/windows to refresh
        const timestamp = Date.now();
        localStorage.setItem('sptnr_queue_updated', timestamp.toString());
      } catch (e) {
        console.warn('Could not update localStorage:', e);
      }
      return true;
    } else {
      alert('Error adding tracks: ' + (data.error || 'Unknown error'));
      return false;
    }
  } catch (error) {
    alert('Error adding tracks: ' + error.message);
    return false;
  }
}

// ===== MusicBrainz Managed Search & Download Functions =====

const MB_DEFAULT_MAX_RETRIES = 3;

function formatTimestamp(ts) {
  if (!ts) return 'N/A';
  return new Date(ts).toLocaleString();
}

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

function performMbSearch() {
  const query = (document.getElementById('mbSearchInput') || {}).value;
  if (!query || !query.trim()) return;

  const loadingEl = document.getElementById('mbLoading');
  const resultsEl = document.getElementById('mbResults');
  if (loadingEl) loadingEl.style.display = 'block';
  if (resultsEl) resultsEl.innerHTML = '';

  fetchJsonOrThrow('/api/musicbrainz/search', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query: query.trim() })
  })
  .then(data => {
    if (loadingEl) loadingEl.style.display = 'none';

    if (data.error) {
      if (resultsEl) resultsEl.innerHTML = `<div class="alert alert-danger"><i class="bi bi-exclamation-triangle"></i> ${escapeHtml(data.error)}</div>`;
      return;
    }

    const releases = data.releases || [];
    if (releases.length === 0) {
      if (resultsEl) resultsEl.innerHTML = `<div class="alert alert-info"><i class="bi bi-info-circle"></i> No releases found for "${escapeHtml(query.trim())}"</div>`;
      return;
    }

    let html = '<div class="list-group">';
    releases.forEach(release => {
      const coverArt = release.cover_art_url || '';
      const releaseDate = release.first_release_date || 'Unknown';
      const category = release.category || release.primary_type || 'Release';
      const artist = release.artist || (release['artist-credit'] && release['artist-credit'][0] && release['artist-credit'][0].name) || 'Unknown Artist';
      const source = release.source || 'musicbrainz';
      const sourceBadge = source === 'local'
        ? '<span class="badge bg-success"><i class="bi bi-database"></i> Cached</span>'
        : '<span class="badge bg-info"><i class="bi bi-cloud"></i> MusicBrainz</span>';
      const imgHtml = coverArt
        ? `<img src="${escapeHtml(coverArt)}" class="rounded" style="width:80px;height:80px;object-fit:cover;" alt="">`
        : '<div class="rounded bg-secondary d-flex align-items-center justify-content-center" style="width:80px;height:80px;"><i class="bi bi-music-note-beamed text-white fs-4"></i></div>';

      html += `
        <div class="list-group-item">
          <div class="d-flex gap-3 align-items-start">
            ${imgHtml}
            <div class="flex-grow-1">
              <h6 class="mb-1">${escapeHtml(release.title)}</h6>
              <p class="mb-1 text-muted small">${escapeHtml(artist)}</p>
              <div class="d-flex gap-2 align-items-center mb-2">
                <span class="badge bg-secondary">${escapeHtml(category)}</span>
                ${sourceBadge}
                <span class="text-muted small">${escapeHtml(releaseDate)}</span>
              </div>
            </div>
            <div class="btn-group" role="group">
              <button class="btn btn-sm btn-success mb-slskd-dl"
                data-release-id="${escapeHtml(release.id)}"
                data-release-title="${escapeHtml(release.title)}"
                data-release-artist="${escapeHtml(artist)}"
                title="Download via Soulseek">
                <i class="bi bi-music-note-list"></i> Soulseek
              </button>
              <button class="btn btn-sm btn-primary mb-qbit-dl"
                data-release-id="${escapeHtml(release.id)}"
                data-release-title="${escapeHtml(release.title)}"
                data-release-artist="${escapeHtml(artist)}"
                title="Download via qBittorrent">
                <i class="bi bi-cloud-download"></i> qBittorrent
              </button>
            </div>
          </div>
        </div>`;
    });
    html += '</div>';
    if (resultsEl) {
      resultsEl.innerHTML = html;
      resultsEl.querySelectorAll('.mb-slskd-dl').forEach(btn => {
        btn.addEventListener('click', () => downloadMbRelease(btn.dataset.releaseId, btn.dataset.releaseTitle, btn.dataset.releaseArtist, 'slskd'));
      });
      resultsEl.querySelectorAll('.mb-qbit-dl').forEach(btn => {
        btn.addEventListener('click', () => downloadMbRelease(btn.dataset.releaseId, btn.dataset.releaseTitle, btn.dataset.releaseArtist, 'qbittorrent'));
      });
    }
  })
  .catch(error => {
    if (loadingEl) loadingEl.style.display = 'none';
    if (resultsEl) resultsEl.innerHTML = `<div class="alert alert-danger"><i class="bi bi-exclamation-triangle"></i> Error: ${escapeHtml(error.message)}</div>`;
  });
}

function downloadMbRelease(releaseId, releaseTitle, artist, method) {
  const persistentEl = document.getElementById('persistentSearchCheck');
  const persistentSearch = persistentEl ? persistentEl.checked : false;
  const sessionSelector = document.getElementById('mbSessionSelector');
  const selectedSession = sessionSelector ? sessionSelector.value : '';

  if (selectedSession === 'create') {
    const sessionName = prompt('Enter name for new playlist session:');
    if (!sessionName) return;
    fetch('/api/playlist-downloads/create', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_name: sessionName, total_tracks: null, priority_queue: false })
    })
    .then(r => r.json())
    .then(sd => {
      if (sd.error) { alert('Error creating session: ' + sd.error); return; }
      _addMbDownloadToSession(releaseId, releaseTitle, artist, method, persistentSearch, sd.session_id);
    })
    .catch(e => alert('Error creating session: ' + e.message));
    return;
  }

  if (!confirm(`Download "${releaseTitle}" by ${artist} via ${method}?${persistentSearch ? '\n\nPersistent search enabled - will auto-retry if failed.' : ''}`)) return;
  _addMbDownloadToSession(releaseId, releaseTitle, artist, method, persistentSearch, selectedSession || null);
}

function _addMbDownloadToSession(releaseId, releaseTitle, artist, method, persistentSearch, sessionId) {
  fetch('/api/musicbrainz/download', {
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
  })
  .then(r => r.json())
  .then(data => {
    if (data.error) {
      alert('Error: ' + data.error);
    } else {
      let msg = `Download queued: ${releaseTitle}\nTracking ID: ${data.tracking_id || 'N/A'}`;
      if (data.persistent_search) msg += '\n\n✓ Persistent search enabled - will retry automatically on failure';
      if (data.session_id) msg += `\n✓ Added to session ID: ${data.session_id}`;
      alert(msg);
      setTimeout(refreshMbDownloads, 1000);
    }
  })
  .catch(e => alert('Error initiating download: ' + e.message));
}

async function loadMbSessionSelector() {
  try {
    const data = await fetchJsonOrThrow('/api/playlist-downloads');
    if (!data.sessions) return;
    const selector = document.getElementById('mbSessionSelector');
    if (!selector) return;
    const createOption = selector.querySelector('option[value="create"]');
    // Keep the first two static options: "None" (index 0) and "Create new" (index 1)
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

function refreshMbDownloads() {
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

  fetch('/api/musicbrainz/downloads')
  .then(r => r.json())
  .then(data => {
    if (loadingEl) loadingEl.style.display = 'none';
    if (resultsEl) resultsEl.style.display = 'block';

    if (data.error) {
      if (errorEl) { errorEl.textContent = data.error; errorEl.style.display = 'block'; }
      return;
    }

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
          <td class="text-center text-muted small">${formatTimestamp(dl.created_at)}</td>
          <td class="text-center">
            <div class="btn-group btn-group-sm">
              ${isAwaitingSelection ? '<button class="btn btn-primary mb-dl-select" title="Select file"><i class="bi bi-hand-index"></i> Select</button>' : ''}
              ${canRetry ? '<button class="btn btn-outline-warning mb-dl-retry" title="Retry"><i class="bi bi-arrow-clockwise"></i></button>' : ''}
              ${canRemove ? '<button class="btn btn-outline-danger mb-dl-remove" title="Remove"><i class="bi bi-trash"></i></button>' : ''}
            </div>
          </td>
        </tr>`;
      }).join('');

      // Attach event listeners via data attributes to avoid inline handlers
      tableBody.querySelectorAll('tr[data-dl-id]').forEach(row => {
        const dlId = row.dataset.dlId;
        const selectBtn = row.querySelector('.mb-dl-select');
        const retryBtn = row.querySelector('.mb-dl-retry');
        const removeBtn = row.querySelector('.mb-dl-remove');
        if (selectBtn && typeof showSlskdResults === 'function') selectBtn.addEventListener('click', () => showSlskdResults(dlId));
        if (retryBtn) retryBtn.addEventListener('click', () => retryMbDownload(dlId));
        if (removeBtn) removeBtn.addEventListener('click', () => removeMbDownload(dlId));
      });
    }
  })
  .catch(error => {
    if (loadingEl) loadingEl.style.display = 'none';
    if (errorEl) { errorEl.textContent = 'Error loading downloads: ' + error.message; errorEl.style.display = 'block'; }
  });
}

function retryMbDownload(downloadId) {
  if (!confirm('Retry this download?')) return;
  fetch(`/api/musicbrainz/download/${downloadId}/retry`, { method: 'POST' })
  .then(r => r.json())
  .then(data => {
    if (data.success) { alert('Download retry initiated'); refreshMbDownloads(); }
    else alert('Error: ' + (data.error || 'Unknown error'));
  })
  .catch(e => alert('Error retrying download: ' + e.message));
}

function removeMbDownload(downloadId) {
  if (!confirm('Remove this download from the list?')) return;
  fetch(`/api/musicbrainz/download/${downloadId}`, { method: 'DELETE' })
  .then(r => r.json())
  .then(data => {
    if (data.success) refreshMbDownloads();
    else alert('Error: ' + (data.error || 'Unknown error'));
  })
  .catch(e => alert('Error removing download: ' + e.message));
}

// Initialize page on load
document.addEventListener('DOMContentLoaded', function() {
  const qbitInput = document.getElementById('qbitSearchInput');
  if (qbitInput) {
    qbitInput.addEventListener('keypress', function(e) {
      if (e.key === 'Enter') performQbitSearch();
    });
  }

  // Initialize qBittorrent monitor if present
  if (document.getElementById('qbitMonLoading')) {
    refreshQbitMonitor();
    // Auto-refresh every 5 seconds
    setInterval(() => {
      refreshQbitMonitor({ silent: true });
    }, 5000);
  }

  // Initialize upcoming releases if present
  if (document.getElementById('upcomingReleases')) {
    // Load existing data on page load
    refreshUpcomingReleases();
  }

  // Initialize MusicBrainz search page if present
  if (document.getElementById('mbSearchInput')) {
    document.getElementById('mbSearchInput').addEventListener('keypress', function(e) {
      if (e.key === 'Enter') performMbSearch();
    });
    loadMbSessionSelector();
    refreshMbDownloads();
  }
});

// ===== SOULSEEK SEARCH (downloads_search_soulseek.html) =====

let currentSlskdSearchId = null;
let slskdPollInterval = null;

function searchSoulseek(event) {
  if (event && event.preventDefault) event.preventDefault();
  const queryInput = document.getElementById('slskdSearchQuery');
  if (!queryInput) return;
  const query = queryInput.value.trim();
  if (!query) return;

  const emptyEl = document.getElementById('slskdSearchEmpty');
  const resultsEl = document.getElementById('slskdSearchResults');
  if (emptyEl) emptyEl.style.display = 'none';
  if (resultsEl) {
    resultsEl.style.display = 'block';
    resultsEl.innerHTML = '<div class="text-center p-3"><span class="spinner-border spinner-border-sm me-2"></span>Searching Soulseek…</div>';
  }

  if (slskdPollInterval) {
    clearInterval(slskdPollInterval);
    slskdPollInterval = null;
  }

  fetch('/api/slskd/search', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query })
  })
  .then(response => response.json())
  .then(data => {
    if (data.error) {
      if (resultsEl) resultsEl.innerHTML = `<div class="alert alert-danger">${escapeHtml(data.error)}</div>`;
      return;
    }
    if (data.slotBusy) {
      if (resultsEl) resultsEl.innerHTML = '<div class="alert alert-warning"><i class="bi bi-clock"></i> <strong>Soulseek search slot is busy.</strong> Retrying automatically…</div>';
      const slotPoll = setInterval(() => {
        fetch('/api/slskd/search-slot')
          .then(r => r.json())
          .then(slotData => {
            if (slotData.slotFree) {
              clearInterval(slotPoll);
              searchSoulseek({ preventDefault: () => {} });
            }
          })
          .catch(() => {});
      }, 2000);
      return;
    }
    currentSlskdSearchId = data.searchId;
    slskdPollInterval = setInterval(pollSlskdSearchResults, 1500);
    pollSlskdSearchResults();
  })
  .catch(error => {
    if (resultsEl) resultsEl.innerHTML = `<div class="alert alert-danger">Network error: ${escapeHtml(error.message)}</div>`;
  });
}

function pollSlskdSearchResults() {
  if (!currentSlskdSearchId) return;
  fetch(`/api/slskd/search/${encodeURIComponent(currentSlskdSearchId)}`)
    .then(response => response.json())
    .then(data => {
      const resultsEl = document.getElementById('slskdSearchResults');
      if (!resultsEl) return;
      if (data.error) {
        resultsEl.innerHTML = `<div class="alert alert-danger">${escapeHtml(data.error)}</div>`;
        if (slskdPollInterval) { clearInterval(slskdPollInterval); slskdPollInterval = null; }
        return;
      }
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
    })
    .catch(error => {
      const resultsEl = document.getElementById('slskdSearchResults');
      if (resultsEl) resultsEl.innerHTML = `<div class="alert alert-danger">Network error: ${escapeHtml(error.message)}</div>`;
      if (slskdPollInterval) { clearInterval(slskdPollInterval); slskdPollInterval = null; }
    });
}

// ===== QUEUE MANAGEMENT FUNCTIONS =====

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
  const fn = window.loadFolderGroups || function(){};
  fn({ forceRender: true, keepVisibleOnEmpty: true });
}

let upcomingReleasesRequestController = null;

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
      const fn = window.searchMusicBrainzForQueue || function(){};
      fn();
    }
    return;
  }
  try {
    const data = await fetchJsonOrThrow('/api/queue/add', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ artist, title, album, source, priority })
    });
    if (data.success) {
      alert(`✅ Added to queue: ${artist} - ${title}`);
      const form = document.getElementById('addToQueueForm');
      if (form) form.reset();
      queuePageOffset = 0;
      await loadQueueStatus();
      const fg = window.loadFolderGroups || function(){};
      await fg({ forceRender: true, keepVisibleOnEmpty: true });
    } else {
      alert('❌ Error: ' + (data.error || 'Failed to add to queue'));
    }
  } catch (error) {
    console.error('Error adding to queue:', error);
    alert('❌ Network error: ' + error.message);
  }
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
}

async function clearEntireQueue() {
  if (!confirm('Clear the entire queue? This removes queued, failed and completed items but keeps imported records.')) return;
  try {
    const resp = await fetch('/api/queue/clear', { method: 'DELETE' });
    if (!resp.ok) { alert('❌ Server error: ' + resp.statusText); return; }
    const data = await resp.json();
    if (data.success) {
      alert(`✅ Cleared ${data.deleted || 0} item(s) from queue`);
      await loadQueueStatus();
    } else {
      alert('❌ ' + (data.error || 'Failed to clear queue'));
    }
  } catch (e) { alert('❌ Network error: ' + e.message); }
}

async function purgeAllQueueAndDownloads() {
  if (!confirm('PURGE ALL? This deletes all queue rows and permanently deletes all files/folders in your configured downloads folder. This cannot be undone.')) return;
  try {
    const resp = await fetch('/api/queue/purge-all', { method: 'DELETE' });
    if (!resp.ok) { alert('❌ Server error: ' + resp.statusText); return; }
    const data = await resp.json();
    if (data.success) {
      alert(`✅ Purge complete\n\nDownloads folder: ${data.downloads_dir || 'unknown'}\nQueue items removed: ${data.queue_items_deleted || 0}\nFiles deleted: ${data.deleted_files || 0}`);
      queuePageOffset = 0;
      await loadQueueStatus();
    } else {
      alert('❌ ' + (data.error || 'Failed to purge'));
    }
  } catch (e) { alert('❌ Network error: ' + e.message); }
}

async function retryAllFailed() {
  if (!confirm('Re-queue all failed downloads?')) return;
  try {
    const resp = await fetch('/api/queue/retry-all-failed', { method: 'POST' });
    if (!resp.ok) { alert('❌ Server error: ' + resp.statusText); return; }
    const data = await resp.json();
    if (data.success) {
      alert(`✅ Re-queued ${data.retried || 0} failed item(s)`);
      await loadQueueStatus();
    } else {
      alert('❌ ' + (data.error || 'Failed to retry'));
    }
  } catch (e) { alert('❌ Network error: ' + e.message); }
}

async function cleanupCopiedSources() {
  if (!confirm('Delete copied source files from /downloads while keeping queue history?')) return;
  try {
    const resp = await fetch('/api/queue/cleanup-copied', { method: 'POST' });
    if (!resp.ok) { alert('❌ Server error: ' + resp.statusText); return; }
    const data = await resp.json();
    if (data.success) {
      alert(`✅ ${data.message} (scanned ${data.scanned})`);
      await loadQueueStatus();
    } else {
      alert('❌ Error: ' + (data.error || 'Cleanup failed'));
    }
  } catch (e) { alert('❌ Network error: ' + e.message); }
}

async function loadQueueEvents() {
  try {
    const resp = await fetch('/api/queue/events?limit=50');
    if (!resp.ok) return;
    const data = await resp.json();
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
    tbody.innerHTML = events.map(function(e) {
      var ts = new Date(e.created_at);
      var badgeClass = e.event_type === 'file_found' ? 'bg-info' : e.event_type === 'status_change' ? 'bg-primary' : e.event_type === 'error' ? 'bg-danger' : 'bg-success';
      return '<tr><td class="small text-muted">' + ts.toLocaleString() + '</td><td><span class="badge ' + badgeClass + '">' + escapeHtml((e.event_type || '').replace(/_/g, ' ').toUpperCase()) + '</span></td><td>' + escapeHtml(e.message || '') + '</td></tr>';
    }).join('');
  } catch (error) { console.error('Error loading queue events:', error); }
}

function clearQueueEventsLog() {
  var tbody = document.getElementById('queueEventsBody');
  var emptyDiv = document.getElementById('queueEventsEmpty');
  var table = document.getElementById('queueEventsTable');
  if (tbody) tbody.innerHTML = '';
  if (table) table.style.display = 'none';
  if (emptyDiv) emptyDiv.style.display = 'block';
}

async function restartQueueProcessor() {
  var btn = document.getElementById('restartProcessorBtn');
  var originalText = btn ? btn.innerHTML : '';
  try {
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Restarting...'; }
    var resp = await fetch('/api/queue-processor/restart', { method: 'POST', headers: { 'Content-Type': 'application/json' } });
    var data = await resp.json();
    if (data.success) {
      if (btn) { btn.innerHTML = '<i class="bi bi-check-circle"></i> Restarted!'; }
      setTimeout(function() {
        var fn = window.loadQueueStatus || function(){};
        fn();
        if (btn) { btn.innerHTML = originalText; btn.disabled = false; }
      }, 2000);
    } else {
      if (btn) { btn.innerHTML = '<i class="bi bi-exclamation-circle"></i> Failed!'; setTimeout(function(){ btn.innerHTML = originalText; btn.disabled = false; }, 3000); }
    }
  } catch (e) {
    if (btn) { btn.innerHTML = '<i class="bi bi-exclamation-circle"></i> Error!'; setTimeout(function(){ btn.innerHTML = originalText; btn.disabled = false; }, 3000); }
  }
}

async function deleteQueueItem(queueId, deleteDownloadsFile) {
  var promptText = deleteDownloadsFile ? 'Delete this item from queue AND remove its file from /downloads?' : 'Remove from queue?';
  if (!confirm(promptText)) return;
  try {
    var query = deleteDownloadsFile ? '?delete_download_file=1' : '';
    var resp = await fetch('/api/queue/' + queueId + '/delete' + query, { method: 'DELETE' });
    if (!resp.ok) { alert('❌ Server error: ' + resp.statusText); return; }
    var data = await resp.json();
    if (data.success) {
      alert('✅ Removed from queue');
      await loadQueueStatus();
    } else {
      alert('❌ Error: ' + (data.error || 'Failed to delete'));
    }
  } catch (e) { alert('❌ Network error: ' + e.message); }
}

async function retryQueueItem(queueId) {
  try {
    var data = await fetchJsonOrThrow('/api/queue/' + queueId + '/requeue', { method: 'POST', headers: { 'Content-Type': 'application/json' } });
    if (data.success) {
      alert('✅ Retrying download...');
      await loadQueueStatus();
    } else {
      alert('❌ Error: ' + (data.error || 'Failed to retry'));
    }
  } catch (e) { alert('❌ Network error: ' + e.message); }
}

async function organizeFile(queueId) {
  if (!confirm('Copy file to music library?')) return;
  try {
    var data = await fetchJsonOrThrow('/api/queue/' + queueId + '/organize', { method: 'POST' }, 120000);
    if (data.success) {
      alert('✅ File organized successfully!');
      await loadQueueStatus();
      var fg = window.loadFolderGroups || function(){};
      await fg({ forceRender: true });
    } else {
      alert('❌ Error: ' + (data.error || 'Failed to organize'));
    }
  } catch (e) { alert('❌ Network error: ' + e.message); }
}

async function runQueueCleanup() {
  if (!confirm('Run queue cleanup now?')) return;
  try {
    var data = await fetchJsonOrThrow('/api/queue/cleanup', { method: 'POST' });
    if (data.success) {
      var stats = data.stats || {};
      alert('✅ Cleanup complete\n\nDuplicates removed: ' + (stats.deleted_duplicates || 0) + '\nCompleted albums: ' + (stats.completed_albums || 0));
      await loadQueueStatus();
      var fg = window.loadFolderGroups || function(){};
      await fg({ forceRender: true });
    } else {
      alert('❌ Error: ' + (data.error || 'Cleanup failed'));
    }
  } catch (e) { alert('❌ Network error: ' + e.message); }
}

async function searchMusicBrainzForQueue() {
  var artist = document.getElementById('queueArtist')?.value.trim() || '';
  var album = document.getElementById('queueAlbum')?.value.trim() || '';
  var track = document.getElementById('queueTitle')?.value.trim() || '';
  if (!artist && !album && !track) {
    alert('Please enter at least one field before searching MusicBrainz.');
    return;
  }
  var fn = window.searchMusicBrainzRelease || function(){};
  fn(null, artist || album, album || track);
}

async function organizeSelected() { alert('organizeSelected not yet implemented'); }
async function batchOrganizeSelected() { alert('batchOrganizeSelected not yet implemented'); }
