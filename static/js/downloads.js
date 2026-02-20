// ===== DOWNLOAD PAGES JAVASCRIPT =====
// This file contains all JavaScript functions for the downloads pages

// ===== UTILITY FUNCTIONS =====

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
  
  fetch('/api/qbittorrent/search', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query: query })
  })
  .then(response => response.json())
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
  
  fetch('/api/qbittorrent/add', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url: url })
  })
  .then(response => response.json())
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
    const response = await fetch('/api/upcoming-releases/clear', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' }
    });
    
    const data = await response.json();
    
    if (!response.ok) {
      throw new Error(data.error || 'Failed to clear database');
    }
    
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
    const response = await fetch('/api/upcoming-releases/scrape', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' }
    });
    
    const data = await response.json();
    
    if (!response.ok) {
      throw new Error(data.error || 'Failed to scrape releases');
    }
    
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
    const response = await fetch(`/api/upcoming-releases?collection=${filterCollection}`);
    const data = await response.json();
    
    if (!response.ok) {
      throw new Error(data.error || 'Failed to load releases');
    }
    
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
                <table class="table table-hover table-sm mb-0">
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

// Placeholder for MusicBrainz search function (implement as needed)
function searchMusicBrainzRelease(event, artist, album) {
  event.preventDefault();
  console.log(`Searching MusicBrainz for: ${artist} - ${album}`);
  // Implement MusicBrainz search integration here
  alert(`Search functionality for ${album} by ${artist} - to be implemented`);
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
