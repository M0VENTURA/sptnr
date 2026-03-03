// ===== DOWNLOAD PAGES JAVASCRIPT =====
// This file contains all JavaScript functions for the downloads pages

// ===== UTILITY FUNCTIONS =====

/**
 * Safely fetch and parse JSON, throwing an error if the HTTP status is not OK.
 * This prevents "Unexpected token '<'" errors when the server returns HTML error pages.
 */
async function fetchJsonOrThrow(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    // Try to get error message from JSON response if available
    try {
      const errorData = await response.json();
      throw new Error(errorData.error || `HTTP ${response.status}: ${response.statusText}`);
    } catch (e) {
      // If JSON parsing fails, throw a generic HTTP error
      throw new Error(`HTTP ${response.status}: ${response.statusText}`);
    }
  }
  return response.json();
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

function formatDuration(seconds) {
  if (!seconds) return 'Unknown';
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
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
          <p class="text-muted">Database cleared. Click "Update from Wikipedia" to load new release data.</p>
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
    const data = await fetchJsonOrThrow(`/api/upcoming-releases?collection=${filterCollection}`);
    
    if (!data.releases || data.releases.length === 0) {
      container.innerHTML = `
        <div class="alert alert-info">
          <i class="bi bi-info-circle"></i>
          No upcoming releases found. Click "Update from Wikipedia" to load release data.
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
        const albumStatus = release.album_in_collection ? 
          ' <span class="badge bg-success ms-1">In Collection</span>' : '';
        
        html += `
          <tr>
            <td>${escapeHtml(release.artist_name)}</td>
            <td>${escapeHtml(release.album_name)}${albumStatus}</td>
            <td><small>${release.release_date || 'TBA'}</small></td>
            <td>
              <button type="button" class="btn btn-sm btn-outline-primary" title="Search on MusicBrainz"
                onclick="searchMusicBrainzRelease(event, '${escapeHtml(release.artist_name)}', '${escapeHtml(release.album_name)}')">
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
async function searchMusicBrainzRelease(event, artist, album) {
  if (event) {
    event.preventDefault();
    event.stopPropagation();
  }

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

  const modal = new bootstrap.Modal(modalEl);
  modal.show();

  if (infoEl && infoArtistEl && infoAlbumEl) {
    infoArtistEl.textContent = artist;
    infoAlbumEl.textContent = album;
    infoEl.style.display = 'block';
  }

  statusEl.style.display = 'block';
  statusEl.innerHTML = '<div class="spinner-border spinner-border-sm me-2"></div>Searching MusicBrainz...';
  errorEl.style.display = 'none';
  resultsEl.innerHTML = '';

  try {
    const response = await fetch('/api/upcoming-releases/search-musicbrainz', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ artist, album })
    });
    const data = await response.json();

    if (data.success && data.results && data.results.length >= 1) {
      statusEl.style.display = 'none';
      displayMusicBrainzResults(data.results);
      return;
    }

    statusEl.innerHTML = '<div class="spinner-border spinner-border-sm me-2"></div>Searching Discogs fallback...';

    const discogsResponse = await fetch('/api/upcoming-releases/search-discogs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ artist, album })
    });
    const discogsData = await discogsResponse.json();

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
      release_id: release.release_id || release.release_group_id || null,
      source: release.source || 'musicbrainz'
    };

    const source = release.source || 'musicbrainz';
    const sourceBadge = source === 'discogs'
      ? '<span class="badge bg-info ms-2">Discogs</span>'
      : '<span class="badge bg-primary ms-2">MusicBrainz</span>';

    const tracksHtml = (release.tracks || []).map(track => {
      let duration = 'N/A';
      if (track.length != null && track.length !== '') {
        duration = formatDuration(track.length);
      } else if (track.duration != null && track.duration !== '') {
        duration = track.duration;
      }

      return `
        <tr>
          <td>${escapeHtml(track.position || '')}</td>
          <td>${escapeHtml(track.title || '')}</td>
          <td>${duration}</td>
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
            <div class="mb-3">
              <button class="btn btn-success mb-download-release" data-release-key="${dataKey}">
                <i class="bi bi-download"></i> Download All Tracks (${release.track_count || (release.tracks || []).length})
              </button>
            </div>
            <table class="table table-sm table-hover">
              <thead>
                <tr>
                  <th style="width: 60px;">#</th>
                  <th>Title</th>
                  <th style="width: 100px;">Duration</th>
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
}

async function downloadMusicBrainzRelease(artist, album, tracks, year, release_id, source) {
  if (!tracks || tracks.length === 0) {
    alert('No tracks to download');
    return;
  }

  try {
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
        source: source || 'musicbrainz'
      })
    });

    const data = await response.json();
    if (data.success) {
      alert(`Added ${tracks.length} tracks to queue: ${artist} - ${album}`);
      const modalEl = document.getElementById('musicBrainzModal');
      if (modalEl) {
        const existingModal = bootstrap.Modal.getInstance(modalEl);
        if (existingModal) existingModal.hide();
      }
    } else {
      alert('Error adding tracks: ' + (data.error || 'Unknown error'));
    }
  } catch (error) {
    alert('Error adding tracks: ' + error.message);
  }
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
});
