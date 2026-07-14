# Playlist Templates - Complete Code Extraction

## Overview
This document extracts complete HTML, JavaScript, and CSS from the old playlist templates before refactor at commit 9521c95.

**Source Templates:**
- `templates/playlist_manager.html` (old)
- `templates/playlist_importer.html` (old)

**Target Templates (after refactor):**
- `templates/playlists_browse.html` - Browse & Manage + Smart Playlists
- `templates/playlists_create.html` - Custom Creator + Last.fm + ListenBrainz
- `templates/playlists_import.html` - Spotify Import + Results + Replacement Modal

---

# SECTION 1: playlists_browse.html Content

## HTML Structure

### Browse & Manage Section
```html
<!-- BROWSE & MANAGE PLAYLISTS -->
<div id="browseContent" class="tab-pane fade">
  <div class="container-lg py-4">
    <div class="row">
      <div class="col-md-8 mx-auto">
        <h3><i class="bi bi-music-note-list"></i> Browse & Manage Playlists</h3>
        
        <!-- Playlist Selection -->
        <div class="mb-4">
          <label class="form-label"><strong>Select a Playlist</strong></label>
          <select id="playlistFileSelect" class="form-select" onchange="loadPlaylistForDownload()">
            <option value="">Select a playlist...</option>
          </select>
        </div>

        <!-- Empty State -->
        <div id="downloaderEmpty" style="display: block;">
          <div class="alert alert-secondary text-center py-5">
            <p><i class="bi bi-inbox"></i></p>
            <p>Select a playlist to view and manage its tracks</p>
          </div>
        </div>

        <!-- Results Section -->
        <div id="downloaderResults" style="display: none;">
          <!-- Playlist Metadata -->
          <div id="playlistMetaInfo" class="alert alert-secondary my-2"></div>

          <!-- Stats -->
          <div class="row mb-4">
            <div class="col-md-3">
              <div class="card">
                <div class="card-body text-center">
                  <h5 class="text-primary"><span id="dlTotalCount">0</span></h5>
                  <small class="text-muted">Total Tracks</small>
                </div>
              </div>
            </div>
            <div class="col-md-3">
              <div class="card">
                <div class="card-body text-center">
                  <h5 class="text-success"><span id="dlMatchedCount">0</span></h5>
                  <small class="text-muted">Matched</small>
                </div>
              </div>
            </div>
            <div class="col-md-3">
              <div class="card">
                <div class="card-body text-center">
                  <h5 class="text-warning"><span id="dlCoverage">0</span>%</h5>
                  <small class="text-muted">Coverage</small>
                </div>
              </div>
            </div>
          </div>

          <!-- Original & Matched -->
          <div class="row mb-4">
            <!-- Original Songs (Left Column) -->
            <div class="col-md-6">
              <h5>Original Songs</h5>
              <div id="dlOriginalSongs"></div>
            </div>

            <!-- Detected Matches (Right Column) -->
            <div class="col-md-6">
              <h5>Detected Matches</h5>
              <div id="dlDetectedMatches"></div>
            </div>
          </div>

          <!-- Download Button -->
          <div class="text-center mb-4">
            <button class="btn btn-primary btn-lg" id="dlDownloadBtn" onclick="downloadPlaylistMatches()">
              <i class="bi bi-download"></i> Download Matched Files
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>
```

### Smart Playlists Section
```html
<!-- SMART PLAYLISTS -->
<div id="smartPlaylistsContent" class="tab-pane fade">
  <div class="container-lg py-4">
    <div class="row">
      <div class="col-md-8 mx-auto">
        <h3><i class="bi bi-sparkles"></i> Smart Playlists</h3>
        
        <!-- Selection -->
        <div class="mb-4">
          <label class="form-label"><strong>Select a Smart Playlist</strong></label>
          <select id="smartPlaylistDropdown" class="form-select" onchange="loadSmartPlaylistDetail()">
            <option value="">Select a smart playlist...</option>
          </select>
          <small class="form-text text-muted">Smart playlists are dynamic and update based on criteria</small>
        </div>

        <!-- Empty State -->
        <div id="smartPlaylistEmpty" style="display: block;">
          <div class="alert alert-secondary text-center py-5">
            <p><i class="bi bi-inbox"></i></p>
            <p>Select a smart playlist to view details</p>
          </div>
        </div>

        <!-- Details Section -->
        <div id="smartPlaylistDetails" style="display: none;">
          <div class="card mb-4">
            <div class="card-header">
              <h4 id="smartPlaylistName" class="mb-0"></h4>
            </div>
            <div class="card-body">
              <div id="smartPlaylistMetadata" class="mb-3"></div>
              <div id="smartPlaylistTracks"></div>
            </div>
            <div class="card-footer">
              <button class="btn btn-primary" onclick="editPlaylist()">
                <i class="bi bi-pencil"></i> Edit in Navidrome
              </button>
              <button class="btn btn-outline-primary ms-2" onclick="refreshSmartPlaylistDetail()">
                <i class="bi bi-arrow-clockwise"></i> Refresh
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>
```

## JavaScript Functions - Browse & Manage

```javascript
// =============================================================================
// PLAYLIST LIST LOADING
// =============================================================================

let currentPlaylistData = null;
let currentPlaylistId = null;

async function loadPlaylistList() {
  try {
    console.log('[Playlist Manager] Starting loadPlaylistList');
    const response = await fetch('/api/playlist/list');
    const data = await response.json();
    console.log('[Playlist Manager] Playlist list API response:', data);        

    const select = document.getElementById('playlistFileSelect');
    select.innerHTML = '<option value="">Select a playlist...</option>';        

    if (!data || !data.playlists) {
      console.error('[Playlist Manager] Invalid playlist list response:', data);
      select.innerHTML = '<option value="">Error: Invalid API response</option>';
      return;
    }

    console.log('[Playlist Manager] Found', data.playlists.length, 'playlists');
    if (data.playlists && data.playlists.length > 0) {
      data.playlists.forEach(playlist => {
        const option = document.createElement('option');
        option.value = playlist.path;
        let typeLabel = '';
        if (playlist.type === 'smart' || playlist.type === 'smart-local') typeLabel = ' (Smart)';
        option.textContent = `${playlist.name || playlist.path}${typeLabel} [${playlist.songCount || 0}]`;
        option.dataset.type = playlist.type;
        option.dataset.songCount = playlist.songCount || 0;
        option.dataset.owner = playlist.owner || '';
        select.appendChild(option);
      });
    } else {
      select.innerHTML = '<option value="">No playlists found - check Navidrome configuration</option>';
      console.warn('[Playlist Manager] No playlists returned from /api/playlist/list');
    }
  } catch (error) {
    console.error('Error loading playlists:', error);
    document.getElementById('playlistFileSelect').innerHTML = '<option value="">Error loading playlists</option>';
  }
}

async function loadPlaylistForDownload() {
  const playlistPath = document.getElementById('playlistFileSelect').value;     
  if (!playlistPath) {
    alert('Please select a playlist');
    return;
  }

  try {
    const response = await fetch('/api/playlist/load', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ playlist_path: playlistPath })
    });

    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Failed to load playlist'); 

    currentPlaylistData = data;
    displayPlaylistDownloader(data);
  } catch (error) {
    console.error('Error:', error);
    alert('Error: ' + error.message);
  }
}

function displayPlaylistDownloader(data) {
  const songs = data.songs || [];
  const matched = data.matched_files || [];

  document.getElementById('downloaderEmpty').style.display = 'none';
  document.getElementById('downloaderResults').style.display = 'block';

  // Update counts and show playlist type/metadata if available
  document.getElementById('dlTotalCount').textContent = songs.length;
  document.getElementById('dlMatchedCount').textContent = matched.length;       
  const coverage = songs.length > 0 ? Math.round((matched.length / songs.length) * 100) : 0;
  document.getElementById('dlCoverage').textContent = coverage + '%';
  
  // Show playlist type and metadata (for unified management)
  if (data && data.playlist_type) {
    let meta = `Type: <b>${escapeHtml(data.playlist_type)}</b>`;
    if (data.owner) meta += ` &nbsp; Owner: <b>${escapeHtml(data.owner)}</b>`;  
    if (data.created) meta += ` &nbsp; Created: <b>${escapeHtml(data.created)}</b>`;
    if (data.changed) meta += ` &nbsp; Updated: <b>${escapeHtml(data.changed)}</b>`;
    if (data.comment) meta += `<br><i>${escapeHtml(data.comment)}</i>`;
    let metaDiv = document.getElementById('playlistMetaInfo');
    if (!metaDiv) {
      metaDiv = document.createElement('div');
      metaDiv.id = 'playlistMetaInfo';
      metaDiv.className = 'alert alert-secondary my-2';
      document.querySelector('.mb-4').insertAdjacentElement('afterend', metaDiv);
    }
    metaDiv.innerHTML = meta;
  }

  // Original Songs (Left Column)
  const originalHtml = songs.map((song, idx) => `
    <div class="card mb-2">
      <div class="card-body py-2">
        <div class="d-flex justify-content-between align-items-start">
          <div>
            <strong class="d-block">${escapeHtml(song.title || 'Unknown')}</strong>
            <small class="text-muted">${escapeHtml(song.artist || 'Unknown Artist')}</small>
            ${song.album ? `<br><small class="text-secondary text-opacity-75">${escapeHtml(song.album)}</small>` : ''}
          </div>
          <span class="badge ${song.detected ? 'bg-success' : 'bg-warning'}">
            ${song.detected ? '✓' : '✗'}
          </span>
        </div>
      </div>
    </div>
  `).join('');
  document.getElementById('dlOriginalSongs').innerHTML = originalHtml || '<p class="text-muted text-center py-5">No songs in playlist</p>';
  
  // Detected Matches (Right Column)
  const matchedHtml = matched.map((match, idx) => `
    <div class="card mb-2 border-success">
      <div class="card-body py-2">
        <div class="d-flex justify-content-between align-items-start gap-2">
          <div style="flex: 1;">
            <strong class="d-block">${escapeHtml(match.title || 'Unknown')}</strong>
            <small class="text-muted">${escapeHtml(match.artist || 'Unknown')}</small>
            ${match.filename ? `<br><small class="text-secondary text-opacity-75">${escapeHtml(match.filename.split(/[\/\\]/).pop())}</small>` : ''}
          </div>
          <button class="btn btn-sm btn-outline-primary" onclick="replacePlaylistMatch(${idx})" title="Replace this match">
            <i class="bi bi-arrow-repeat"></i> Replace
          </button>
        </div>
      </div>
    </div>
  `).join('');
  document.getElementById('dlDetectedMatches').innerHTML = matchedHtml || '<p class="text-muted text-center py-5">No matches detected yet</p>';
  
  selectedReplacements = {};
}

function replacePlaylistMatch(matchIndex) {
  // TODO: Open modal to search for alternative match
  alert('Replace functionality coming soon');
}

async function downloadPlaylistMatches() {
  if (!currentPlaylistData) return;
  const btn = document.getElementById('dlDownloadBtn');
  const originalText = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Downloading...';
  
  try {
    const response = await fetch('/api/playlist/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        playlist_path: currentPlaylistData.playlist_path,
        replacements: selectedReplacements
      })
    });

    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Download failed');

    alert('✅ Download queued! ' + (data.queued || 0) + ' files enqueued');
  } catch (error) {
    console.error('Error:', error);
    alert('❌ Error: ' + error.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = originalText;
  }
}

let selectedReplacements = {};
```

## JavaScript Functions - Smart Playlists

```javascript
// =============================================================================
// SMART PLAYLISTS
// =============================================================================

let currentSmartPlaylistId = null;
let currentSmartPlaylistData = null;

async function loadSmartPlaylists() {
  try {
    console.log('[Playlist Manager] Loading smart playlists dropdown');
    const response = await fetch('/api/navidrome/playlists');
    const data = await response.json();
    console.log('[Playlist Manager] Smart playlists API response:', data);

    const select = document.getElementById('smartPlaylistDropdown');
    if (!select) {
      console.warn('[Playlist Manager] smartPlaylistDropdown element not found');
      return;
    }

    select.innerHTML = '<option value="">Select a smart playlist...</option>';

    // Check for API error response
    if (data.error) {
      console.error('[Playlist Manager] API Error:', data.error);
      select.innerHTML = '<option value="">Error: ' + data.error + '</option>';
      return;
    }

    console.log('[Playlist Manager] Smart playlists count:', data.smart?.length || 0);
    if (data.smart && data.smart.length > 0) {
      data.smart.forEach(playlist => {
        const option = document.createElement('option');
        option.value = playlist.id;
        option.textContent = playlist.name;
        select.appendChild(option);
      });
      console.log('[Playlist Manager] Populated smartPlaylistDropdown with', data.smart.length, 'playlists');
    } else {
      select.innerHTML = '<option value="">No smart playlists found</option>';
      console.log('[Playlist Manager] No smart playlists in API response');
    }
  } catch (error) {
    console.error('[Playlist Manager] Error loading smart playlists:', error);
    const select = document.getElementById('smartPlaylistDropdown');
    if (select) {
      select.innerHTML = '<option value="">Error: Network failure</option>';
    }
  }
}

async function loadSmartPlaylistDetail() {
  const playlistId = document.getElementById('smartPlaylistDropdown').value;
  if (!playlistId) {
    alert('Please select a smart playlist');
    return;
  }

  try {
    const response = await fetch(`/api/navidrome/playlist/${playlistId}`);
    const data = await response.json();

    if (!response.ok) {
      throw new Error(data.error || 'Failed to load playlist');
    }

    currentSmartPlaylistId = playlistId;
    currentSmartPlaylistData = data;
    displaySmartPlaylistDetails(data);
  } catch (error) {
    console.error('Error loading smart playlist details:', error);
    alert('Failed to load playlist: ' + error.message);
  }
}

function displaySmartPlaylistDetails(data) {
  const detailsSection = document.getElementById('smartPlaylistDetails');
  const nameEl = document.getElementById('smartPlaylistName');
  const metadataEl = document.getElementById('smartPlaylistMetadata');
  const tracksEl = document.getElementById('smartPlaylistTracks');

  // Set playlist name
  nameEl.textContent = data.name || 'Unknown Smart Playlist';

  // Build metadata
  let metadataHtml = '<div class="row g-3">';
  metadataHtml += `<div class="col-md-3"><strong>Tracks:</strong> ${data.songCount || 0}</div>`;
  if (data.duration) {
    const hours = Math.floor(data.duration / 3600);
    const minutes = Math.floor((data.duration % 3600) / 60);
    metadataHtml += `<div class="col-md-3"><strong>Duration:</strong> ${hours}h ${minutes}m</div>`;
  }
  if (data.owner) {
    metadataHtml += `<div class="col-md-3"><strong>Owner:</strong> ${escapeHtml(data.owner)}</div>`;
  }
  if (data.comment) {
    metadataHtml += `<div class="col-12"><strong>Description:</strong> ${escapeHtml(data.comment)}</div>`;
  }
  metadataHtml += '</div>';
  metadataEl.innerHTML = metadataHtml;

  // Build track list
  if (data.tracks && data.tracks.length > 0) {
    let tracksHtml = '<h6 class="mb-2">Tracks</h6>';
    data.tracks.forEach((track, idx) => {
      tracksHtml += `
        <div class="d-flex justify-content-between align-items-center py-2 border-bottom">
          <div style="flex: 1;">
            <strong>${escapeHtml(track.title || 'Unknown Track')}</strong><br>
            <small class="text-muted">${escapeHtml(track.artist || 'Unknown Artist')} ${track.album ? '· ' + escapeHtml(track.album) : ''}</small>
          </div>
          ${track.duration ? `<small class="text-muted">${formatDuration(track.duration)}</small>` : ''}
        </div>
      `;
    });
    tracksEl.innerHTML = tracksHtml;
  } else {
    tracksEl.innerHTML = '<p class="text-muted text-center py-3">No tracks in this playlist</p>';
  }

  // Show the details section
  document.getElementById('smartPlaylistEmpty').style.display = 'none';
  detailsSection.style.display = 'block';
  detailsSection.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function editPlaylist() {
  if (!currentPlaylistId || !currentPlaylistData) {
    alert('No playlist selected');
    return;
  }

  const navidromeUrl = currentPlaylistData.navidromeUrl || '/navidrome';
  window.open(`${navidromeUrl}/#/playlist/${currentPlaylistId}`, '_blank');
}

function refreshPlaylistDetails() {
  if (currentPlaylistId) {
    const type = document.getElementById('smartPlaylistSelect').value ? 'smart' : 'regular';
    loadNavidromePlaylistDetail(currentPlaylistId, type);
  }
}

function refreshSmartPlaylistDetail() {
  if (currentSmartPlaylistId) {
    loadSmartPlaylistDetail();
  }
}

function formatDuration(seconds) {
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  return `${mins}:${secs.toString().padStart(2, '0')}`;
}
```

## CSS for Browse & Manage

```css
/* Playlist Browse Styling */
.track-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 0;
  border-bottom: 1px solid var(--border-color);
}

.track-row:last-child {
  border-bottom: none;
}

.track-info {
  flex: 1;
}

.track-title {
  font-weight: 600;
  margin-bottom: 4px;
}

.track-artist {
  font-size: 0.875rem;
  color: var(--text-secondary);
  margin-bottom: 2px;
}

.track-album {
  font-size: 0.75rem;
  color: var(--text-secondary);
  opacity: 0.7;
}

.track-actions {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-left: 12px;
}

/* Dark theme support */
:root {
  --border-color: #495057;
  --text-secondary: #adb5bd;
}

@media (prefers-color-scheme: dark) {
  .bg-light {
    background-color: var(--secondary-bg) !important;
  }
}

/* Tab Pane Fade Animation */
.tab-pane {
  animation: fadeIn 0.3s ease-in;
}

@keyframes fadeIn {
  from {
    opacity: 0;
  }
  to {
    opacity: 1;
  }
}
```

---

# SECTION 2: playlists_create.html Content

## HTML Structure

### Custom Playlist Creator Section
```html
<!-- CREATE CUSTOM PLAYLIST -->
<div id="manualContent" class="tab-pane fade">
  <div class="container-lg py-4">
    <div class="row">
      <div class="col-md-8 mx-auto">
        <h3><i class="bi bi-plus-circle"></i> Create Custom Playlist</h3>
        
        <!-- Search & Add Songs -->
        <div class="card mb-4">
          <div class="card-header">
            <h6 class="mb-0">Search & Add Songs</h6>
          </div>
          <div class="card-body">
            <div class="mb-3">
              <input type="text" class="form-control" id="songSearchInput" placeholder="Search songs..." />
            </div>
            <div class="row mb-3 g-2">
              <div class="col-md-4">
                <input type="text" class="form-control" id="searchTitleInput" placeholder="Title" />
              </div>
              <div class="col-md-4">
                <input type="text" class="form-control" id="searchArtistInput" placeholder="Artist" />
              </div>
              <div class="col-md-4">
                <input type="text" class="form-control" id="searchAlbumInput" placeholder="Album" />
              </div>
            </div>
            <button class="btn btn-primary" onclick="searchSongs()">
              <i class="bi bi-search"></i> Search
            </button>
          </div>
        </div>

        <!-- Search Results -->
        <div id="songSearchResults" style="display: none;" class="card mb-4">
          <div class="card-header">
            <h6 class="mb-0">Search Results</h6>
          </div>
          <div class="card-body"></div>
        </div>

        <!-- Selected Songs -->
        <div class="card mb-4">
          <div class="card-header d-flex justify-content-between align-items-center">
            <h6 class="mb-0">
              <i class="bi bi-check-circle"></i> Selected Songs 
              <span class="badge bg-primary" id="selectedSongsCount">0</span>
            </h6>
          </div>
          <div class="card-body">
            <div id="selectedSongsList">
              <p class="text-muted text-center py-5">No songs added yet</p>
            </div>
          </div>
        </div>

        <!-- Create Playlist Form -->
        <form onsubmit="createCustomPlaylist(event)" class="card">
          <div class="card-header">
            <h6 class="mb-0">Create Playlist</h6>
          </div>
          <div class="card-body">
            <div class="mb-3">
              <label class="form-label">Playlist Name *</label>
              <input type="text" class="form-control" id="customPlaylistName" required />
            </div>
            <div class="mb-3">
              <label class="form-label">Description</label>
              <textarea class="form-control" id="customPlaylistDesc" rows="3"></textarea>
            </div>
            <div class="mb-3">
              <label class="form-label">User</label>
              <select class="form-select" id="customPlaylistUser">
                <option value="admin">Admin</option>
              </select>
            </div>
            <div class="mb-3 form-check">
              <input type="checkbox" class="form-check-input" id="customPlaylistPublic" />
              <label class="form-check-label" for="customPlaylistPublic">
                Make public
              </label>
            </div>
          </div>
          <div class="card-footer">
            <button type="submit" class="btn btn-success">
              <i class="bi bi-plus-circle"></i> Create Playlist
            </button>
          </div>
        </form>
      </div>
    </div>
  </div>
</div>
```

### Last.fm Recommendations Section
```html
<!-- LAST.FM RECOMMENDATIONS -->
<div id="lastfmContent" class="tab-pane fade">
  <div class="container-lg py-4">
    <div class="row">
      <div class="col-md-8 mx-auto">
        <h3><i class="bi bi-spotify"></i> Last.fm Recommendations</h3>
        
        <!-- Selection -->
        <div class="mb-4">
          <label class="form-label"><strong>Recommendation Type</strong></label>
          <select id="lfmRecType" class="form-select" onchange="loadLastfmRecommendations()">
            <option value="tracks">Top Tracks</option>
            <option value="artists">Top Artists</option>
            <option value="albums">Top Albums</option>
          </select>
        </div>

        <!-- Empty State -->
        <div id="lfmRecommendationsEmpty" style="display: block;">
          <div class="alert alert-secondary text-center py-5">
            <p><i class="bi bi-inbox"></i></p>
            <p>Select a recommendation type above</p>
          </div>
        </div>

        <!-- Results Section -->
        <div id="lfmRecommendationsResults" style="display: none;">
          <!-- Stats -->
          <div class="row mb-4">
            <div class="col-md-3">
              <div class="card">
                <div class="card-body text-center">
                  <h5 class="text-primary"><span id="lfmTotalCount">0</span></h5>
                  <small class="text-muted">Total</small>
                </div>
              </div>
            </div>
            <div class="col-md-3">
              <div class="card">
                <div class="card-body text-center">
                  <h5 class="text-success"><span id="lfmMatchedCount">0</span></h5>
                  <small class="text-muted">Matched</small>
                </div>
              </div>
            </div>
            <div class="col-md-3">
              <div class="card">
                <div class="card-body text-center">
                  <h5 class="text-warning"><span id="lfmMissingCount">0</span></h5>
                  <small class="text-muted">Missing</small>
                </div>
              </div>
            </div>
          </div>

          <!-- Matched Tracks -->
          <div class="card mb-4">
            <div class="card-header">
              <h6 class="mb-0"><i class="bi bi-check-circle"></i> Matched Tracks</h6>
            </div>
            <div class="card-body">
              <div id="lfmMatchedTracks"></div>
            </div>
          </div>

          <!-- Missing Tracks -->
          <div class="card mb-4">
            <div class="card-header">
              <h6 class="mb-0"><i class="bi bi-exclamation-circle"></i> Missing Tracks</h6>
            </div>
            <div class="card-body">
              <div id="lfmMissingTracks"></div>
            </div>
          </div>

          <!-- Create Playlist Button -->
          <div class="text-center mb-4">
            <button class="btn btn-success btn-lg" onclick="createPlaylistFromLastfm()">
              <i class="bi bi-plus-circle"></i> Create Playlist from Matched Tracks
            </button>
            <button class="btn btn-outline-secondary btn-lg ms-2" id="lfmSearchBtn" onclick="searchLastfmMissingTracksOnSoulseek()" style="display: none;">
              <i class="bi bi-cloud-arrow-down"></i> Search Missing on Soulseek
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>
```

### ListenBrainz Recommendations Section
```html
<!-- LISTENBRAINZ RECOMMENDATIONS -->
<div id="listenbrainzContent" class="tab-pane fade">
  <div class="container-lg py-4">
    <div class="row">
      <div class="col-md-8 mx-auto">
        <h3><i class="bi bi-music-note-list"></i> ListenBrainz Recommendations</h3>
        
        <!-- Selection -->
        <div class="mb-4">
          <label class="form-label"><strong>Recommendation Type</strong></label>
          <select id="lbRecType" class="form-select" onchange="loadListenBrainzRecommendations()">
            <option value="tracks">Top Tracks</option>
            <option value="artists">Top Artists</option>
            <option value="albums">Top Albums</option>
          </select>
        </div>

        <!-- Empty State -->
        <div id="lbRecommendationsEmpty" style="display: block;">
          <div class="alert alert-secondary text-center py-5">
            <p><i class="bi bi-inbox"></i></p>
            <p>Select a recommendation type above</p>
          </div>
        </div>

        <!-- Results Section -->
        <div id="lbRecommendationsResults" style="display: none;">
          <!-- Stats -->
          <div class="row mb-4">
            <div class="col-md-3">
              <div class="card">
                <div class="card-body text-center">
                  <h5 class="text-primary"><span id="lbTotalCount">0</span></h5>
                  <small class="text-muted">Total</small>
                </div>
              </div>
            </div>
            <div class="col-md-3">
              <div class="card">
                <div class="card-body text-center">
                  <h5 class="text-success"><span id="lbMatchedCount">0</span></h5>
                  <small class="text-muted">Matched</small>
                </div>
              </div>
            </div>
            <div class="col-md-3">
              <div class="card">
                <div class="card-body text-center">
                  <h5 class="text-warning"><span id="lbMissingCount">0</span></h5>
                  <small class="text-muted">Missing</small>
                </div>
              </div>
            </div>
          </div>

          <!-- Matched Tracks -->
          <div class="card mb-4">
            <div class="card-header">
              <h6 class="mb-0"><i class="bi bi-check-circle"></i> Matched Tracks</h6>
            </div>
            <div class="card-body">
              <div id="lbMatchedTracks"></div>
            </div>
          </div>

          <!-- Missing Tracks -->
          <div class="card mb-4">
            <div class="card-header">
              <h6 class="mb-0"><i class="bi bi-exclamation-circle"></i> Missing Tracks</h6>
            </div>
            <div class="card-body">
              <div id="lbMissingTracks"></div>
            </div>
          </div>

          <!-- Create Playlist Button -->
          <div class="text-center mb-4">
            <button class="btn btn-success btn-lg" onclick="createPlaylistFromListenBrainz()">
              <i class="bi bi-plus-circle"></i> Create Playlist from Matched Tracks
            </button>
            <button class="btn btn-outline-secondary btn-lg ms-2" id="lbSearchBtn" onclick="searchMissingTracksOnSoulseek()" style="display: none;">
              <i class="bi bi-cloud-arrow-down"></i> Search Missing on Soulseek
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>
```

## JavaScript Functions - Custom Creator

```javascript
// =============================================================================
// CUSTOM PLAYLIST CREATOR
// =============================================================================

let selectedSongs = [];

async function searchSongs() {
  const query = document.getElementById('songSearchInput').value.trim();
  const title = document.getElementById('searchTitleInput').value.trim();
  const artist = document.getElementById('searchArtistInput').value.trim();
  const album = document.getElementById('searchAlbumInput').value.trim();

  if (!query && !title && !artist && !album) {
    alert('Please enter at least one search field');
    return;
  }

  try {
    const response = await fetch('/api/playlist/search-songs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query, title, artist, album })
    });

    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Search failed');

    displaySearchResults(data.songs || []);
  } catch (error) {
    console.error('Error:', error);
    alert('Error: ' + error.message);
  }
}

function displaySearchResults(songs) {
  const resultsDiv = document.getElementById('songSearchResults');
  if (songs.length === 0) {
    resultsDiv.innerHTML = '<p class="text-muted text-center">No songs found</p>';
    resultsDiv.style.display = 'block';
    return;
  }

  resultsDiv.innerHTML = songs.map((song, idx) => `
    <div class="d-flex justify-content-between align-items-center py-2 border-bottom">
      <div>
        <strong>${escapeHtml(song.title || 'Unknown')}</strong><br>
        <small class="text-muted">${escapeHtml(song.artist || 'Unknown')} ${song.album ? '· ' + escapeHtml(song.album) : ''}</small>
      </div>
      <button class="btn btn-sm btn-outline-success" data-song-idx="${idx}" type="button">
        <i class="bi bi-plus"></i> Add
      </button>
    </div>
  `).join('');
  
  // Attach event listeners for add buttons
  songs.forEach((song, idx) => {
    const btn = resultsDiv.querySelector(`button[data-song-idx="${idx}"]`);
    if (btn) {
      btn.addEventListener('click', function() {
        addSongToPlaylistObj(song);
      });
    }
  });
  resultsDiv.style.display = 'block';
}

function getSongKey(song) {
  // Use id if present, else composite key
  return song.id || `${song.title || ''}|${song.artist || ''}|${song.album || ''}`;
}

function addSongToPlaylistObj(song) {
  const key = getSongKey(song);
  if (selectedSongs.find(s => getSongKey(s) === key)) {
    alert('This song is already in the playlist');
    return;
  }
  console.log('Adding song to playlist:', song); // Debug log
  selectedSongs.push(song);
  updateSelectedSongsList();
}

function updateSelectedSongsList() {
  const listDiv = document.getElementById('selectedSongsList');
  const countBadge = document.getElementById('selectedSongsCount');

  countBadge.textContent = selectedSongs.length;

  if (selectedSongs.length === 0) {
    listDiv.innerHTML = '<p class="text-muted text-center py-5">No songs added yet</p>';
    return;
  }

  listDiv.innerHTML = selectedSongs.map((song, idx) => `
    <div class="d-flex justify-content-between align-items-center py-2 border-bottom">
      <div>
        <strong>${escapeHtml(song.title || 'Unknown')}</strong><br>
        <small class="text-muted">${escapeHtml(song.artist || 'Unknown')}</small>
      </div>
      <button class="btn btn-sm btn-outline-danger" onclick="removeSongFromPlaylist(${idx})">
        <i class="bi bi-trash"></i>
      </button>
    </div>
  `).join('');
}

function removeSongFromPlaylist(index) {
  selectedSongs.splice(index, 1);
  updateSelectedSongsList();
}

async function createCustomPlaylist(event) {
  event.preventDefault();

  if (selectedSongs.length === 0) {
    alert('Please add at least one song to the playlist');
    return;
  }

  const btn = event.target.querySelector('button[type="submit"]');
  const originalText = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Creating...';
  
  try {
    const response = await fetch('/api/playlist/create-custom', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: document.getElementById('customPlaylistName').value.trim(),
        description: document.getElementById('customPlaylistDesc').value.trim(),
        user: document.getElementById('customPlaylistUser').value,
        is_public: document.getElementById('customPlaylistPublic').checked,
        songs: selectedSongs
      })
    });

    let data;
    try {
      data = await response.json();
    } catch (jsonErr) {
      const raw = await response.text();
      console.error('Raw response (not valid JSON):', raw);
      throw new Error('Invalid JSON response from server.');
    }
    if (!response.ok) throw new Error(data.error || 'Failed to create playlist');
    
    alert('✅ Playlist created successfully!');
    // Reset form
    event.target.reset();
    selectedSongs = [];
    updateSelectedSongsList();
  } catch (error) {
    console.error('Error:', error);
    alert('❌ Error: ' + error.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = originalText;
  }
}
```

## JavaScript Functions - Last.fm & ListenBrainz

```javascript
// =============================================================================
// LAST.FM RECOMMENDATIONS
// =============================================================================

let lfmRecommendationsData = null;

async function loadLastfmRecommendations() {
  const recType = document.getElementById('lfmRecType').value;

  try {
    document.getElementById('lfmRecommendationsEmpty').style.display = 'none';
    document.getElementById('lfmRecommendationsResults').style.display = 'block';
    
    // Show loading state
    document.getElementById('lfmTotalCount').textContent = '...';
    document.getElementById('lfmMatchedCount').textContent = '...';
    document.getElementById('lfmMissingCount').textContent = '...';

    const response = await fetch(`/api/lastfm/create-playlist`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type: recType })
    });

    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.error || 'Failed to load recommendations');
    }

    const data = await response.json();
    lfmRecommendationsData = data;

    // Update counts
    document.getElementById('lfmTotalCount').textContent = data.total_recommendations || 0;
    document.getElementById('lfmMatchedCount').textContent = data.matched || 0;
    document.getElementById('lfmMissingCount').textContent = data.missing || 0;

    // Display matched tracks
    const matchedHtml = (data.matched_tracks || []).map(track => `
      <div class="card mb-2 border-success">
        <div class="card-body py-2">
          <div class="d-flex justify-content-between align-items-start">
            <div>
              <strong class="d-block">${escapeHtml(track.title || 'Unknown')}</strong>
              <small class="text-muted">${escapeHtml(track.artist || 'Unknown Artist')}</small>
            </div>
            <span class="badge bg-success">✓</span>
          </div>
        </div>
      </div>
    `).join('');
    document.getElementById('lfmMatchedTracks').innerHTML = matchedHtml || '<p class="text-muted text-center py-5">No matched tracks found</p>';
    
    // Display missing tracks
    const missingHtml = (data.missing_tracks || []).map(track => `
      <div class="card mb-2 border-warning">
        <div class="card-body py-2">
          <div class="d-flex justify-content-between align-items-start">
            <div>
              <strong class="d-block">${escapeHtml(track.title || 'Unknown')}</strong>
              <small class="text-muted">${escapeHtml(track.artist || 'Unknown Artist')}</small>
              ${track.playcount ? `<br><small class="text-secondary">Playcount: ${track.playcount.toLocaleString()}</small>` : ''}
            </div>
            <span class="badge bg-warning">Missing</span>
          </div>
        </div>
      </div>
    `).join('');
    document.getElementById('lfmMissingTracks').innerHTML = missingHtml || '<p class="text-muted text-center py-5">All tracks are in your library!</p>';
  } catch (error) {
    console.error('Error loading Last.fm recommendations:', error);
    alert('Error: ' + error.message);
    document.getElementById('lfmRecommendationsResults').style.display = 'none';
    document.getElementById('lfmRecommendationsEmpty').style.display = 'block';
  }
}

async function createPlaylistFromLastfm() {
  if (!lfmRecommendationsData || !lfmRecommendationsData.matched_tracks || lfmRecommendationsData.matched_tracks.length === 0) {
    alert('No matched tracks to create playlist from');
    return;
  }

  const recType = document.getElementById('lfmRecType').value;
  const playlistName = prompt('Enter playlist name:', `Last.fm ${recType === 'tracks' ? 'Top Tracks' : recType === 'artists' ? 'Top Artists' : 'Top Albums'}`);
  if (!playlistName) return;

  try {
    // Get current user from config - use first configured user if available
    const currentUser = document.getElementById('customPlaylistUser')?.value || 'admin';
    // Use existing custom playlist creation
    const response = await fetch('/api/playlist/create-custom', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: playlistName,
        description: `Last.fm recommendations: ${recType.replace(/_/g, ' ')}`,
        user: currentUser,
        is_public: false,
        songs: lfmRecommendationsData.matched_tracks
      })
    });

    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.error || 'Failed to create playlist');
    }

    alert('✅ Playlist created successfully!');
  } catch (error) {
    console.error('Error creating playlist:', error);
    alert('❌ Error: ' + error.message);
  }
}

async function searchLastfmMissingTracksOnSoulseek() {
  if (!lfmRecommendationsData || !lfmRecommendationsData.missing_tracks) {
    alert('No missing tracks to search for');
    return;
  }

  alert('Soulseek search functionality coming soon!\n\nMissing ' + lfmRecommendationsData.missing_tracks.length + ' tracks.');
  // TODO: Implement batch search on Soulseek for missing tracks
}

// =============================================================================
// LISTENBRAINZ RECOMMENDATIONS
// =============================================================================

let lbRecommendationsData = null;

async function loadListenBrainzRecommendations() {
  const recType = document.getElementById('lbRecType').value;

  try {
    document.getElementById('lbRecommendationsEmpty').style.display = 'none';
    document.getElementById('lbRecommendationsResults').style.display = 'block';

    // Show loading state
    document.getElementById('lbTotalCount').textContent = '...';
    document.getElementById('lbMatchedCount').textContent = '...';
    document.getElementById('lbMissingCount').textContent = '...';

    const response = await fetch(`/api/listenbrainz/create-playlist`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type: recType })
    });

    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.error || 'Failed to load recommendations');
    }

    const data = await response.json();
    lbRecommendationsData = data;

    // Update counts
    document.getElementById('lbTotalCount').textContent = data.total_recommendations || 0;
    document.getElementById('lbMatchedCount').textContent = data.matched || 0;
    document.getElementById('lbMissingCount').textContent = data.missing || 0;

    // Display matched tracks
    const matchedHtml = (data.matched_tracks || []).map(track => `
      <div class="card mb-2 border-success">
        <div class="card-body py-2">
          <div class="d-flex justify-content-between align-items-start">
            <div>
              <strong class="d-block">${escapeHtml(track.title || 'Unknown')}</strong>
              <small class="text-muted">${escapeHtml(track.artist || 'Unknown Artist')}</small>
            </div>
            <span class="badge bg-success">✓</span>
          </div>
        </div>
      </div>
    `).join('');
    document.getElementById('lbMatchedTracks').innerHTML = matchedHtml || '<p class="text-muted text-center py-5">No matched tracks found</p>';
    
    // Display missing tracks
    const missingHtml = (data.missing_tracks || []).map(track => `
      <div class="card mb-2 border-warning">
        <div class="card-body py-2">
          <div class="d-flex justify-content-between align-items-start">
            <div>
              <strong class="d-block">${escapeHtml(track.title || 'Unknown')}</strong>
              <small class="text-muted">${escapeHtml(track.artist || 'Unknown Artist')}</small>
              ${track.mbid ? `<br><small class="text-secondary">MBID: ${track.mbid}</small>` : ''}
            </div>
            <span class="badge bg-warning">Missing</span>
          </div>
        </div>
      </div>
    `).join('');
    document.getElementById('lbMissingTracks').innerHTML = missingHtml || '<p class="text-muted text-center py-5">All tracks are in your library!</p>';
  } catch (error) {
    console.error('Error loading ListenBrainz recommendations:', error);
    alert('Error: ' + error.message);
    document.getElementById('lbRecommendationsResults').style.display = 'none';
    document.getElementById('lbRecommendationsEmpty').style.display = 'block';
  }
}

async function createPlaylistFromListenBrainz() {
  if (!lbRecommendationsData || !lbRecommendationsData.matched_tracks || lbRecommendationsData.matched_tracks.length === 0) {
    alert('No matched tracks to create playlist from');
    return;
  }

  const recType = document.getElementById('lbRecType').value;
  const playlistName = prompt('Enter playlist name:', `ListenBrainz ${recType.replace(/_/g, ' ')}`);
  if (!playlistName) return;

  try {
    // Get current user from config - use first configured user if available
    // TODO: Get actual current logged-in user from session
    const currentUser = document.getElementById('customPlaylistUser')?.value || 'admin';
    // Use existing custom playlist creation
    const response = await fetch('/api/playlist/create-custom', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: playlistName,
        description: `ListenBrainz recommendations: ${recType.replace(/_/g, ' ')}`,
        user: currentUser,
        is_public: false,
        songs: lbRecommendationsData.matched_tracks
      })
    });

    const result = await response.json();

    if (result.success) {
      alert(`Playlist "${playlistName}" created successfully!`);
      switchTab('browse');
      await loadPlaylistList();
    } else {
      alert('Error creating playlist: ' + (result.error || 'Unknown error'));
    }
  } catch (error) {
    console.error('Error creating playlist:', error);
    alert('Error: ' + error.message);
  }
}

async function searchMissingTracksOnSoulseek() {
  if (!lbRecommendationsData || !lbRecommendationsData.missing_tracks) {
    alert('No missing tracks to search for');
    return;
  }

  alert('Soulseek search functionality coming soon!\n\nMissing ' + lbRecommendationsData.missing_tracks.length + ' tracks.');
  // TODO: Implement batch search on Soulseek for missing tracks
}
```

---

# SECTION 3: playlists_import.html Content

## HTML Structure

### Spotify Import Form Section
```html
<!-- SPOTIFY IMPORT FORM -->
<div id="importFormSection" class="container-lg py-4">
  <div class="row">
    <div class="col-md-8 mx-auto">
      <div class="card mb-4">
        <div class="card-header">
          <h5><i class="bi bi-spotify"></i> Import from Spotify Playlist</h5>
        </div>
        <div class="card-body">
          <form onsubmit="importPlaylist(event)">
            <div class="mb-3">
              <label class="form-label"><strong>Spotify Playlist URL or URI *</strong></label>
              <input type="text" class="form-control" id="spotifyUrl" 
                     placeholder="https://open.spotify.com/playlist/..." required />
              <small class="form-text text-muted">
                Right-click on a Spotify playlist and choose "Share" → "Copy link"
              </small>
            </div>

            <div class="mb-3">
              <label class="form-label"><strong>Playlist Name *</strong></label>
              <input type="text" class="form-control" id="playlistName" 
                     placeholder="Enter a name for this playlist" required />
            </div>

            <div class="mb-3">
              <label class="form-label">Description (optional)</label>
              <textarea class="form-control" id="playlistDescription" 
                        rows="3" placeholder="Add a description..."></textarea>
            </div>

            <div class="mb-3">
              <button type="submit" class="btn btn-primary btn-lg">
                <i class="bi bi-cloud-arrow-down"></i> Import Playlist
              </button>
              <small id="importStatus" class="ms-3"></small>
            </div>
          </form>
        </div>
      </div>
    </div>
  </div>
</div>
```

### Results Display Section
```html
<!-- IMPORT RESULTS -->
<div id="resultsSection" style="display: none;" class="container-lg py-4">
  <div class="row">
    <div class="col-md-10 mx-auto">
      <h4 class="mb-4">Import Results</h4>

      <!-- Summary Stats -->
      <div class="row mb-4">
        <div class="col-md-3">
          <div class="card">
            <div class="card-body text-center">
              <h5 class="text-primary"><span id="totalCount">0</span></h5>
              <small class="text-muted">Total Tracks</small>
            </div>
          </div>
        </div>
        <div class="col-md-3">
          <div class="card">
            <div class="card-body text-center">
              <h5 class="text-success"><span id="matchedCount">0</span></h5>
              <small class="text-muted">Matched</small>
            </div>
          </div>
        </div>
        <div class="col-md-3">
          <div class="card">
            <div class="card-body text-center">
              <h5 class="text-warning"><span id="missingCount">0</span></h5>
              <small class="text-muted">Missing</small>
            </div>
          </div>
        </div>
        <div class="col-md-3">
          <div class="card">
            <div class="card-body text-center">
              <h5 class="text-info"><span id="coverage">0</span>%</h5>
              <small class="text-muted">Coverage</small>
            </div>
          </div>
        </div>
      </div>

      <!-- Matched Tracks -->
      <div class="card mb-4">
        <div class="card-header">
          <h6 class="mb-0"><i class="bi bi-check-circle"></i> Matched Tracks</h6>
        </div>
        <div class="card-body" style="max-height: 400px; overflow-y: auto;">
          <div id="matchedTracksContainer"></div>
        </div>
      </div>

      <!-- Missing Tracks -->
      <div class="card mb-4">
        <div class="card-header">
          <h6 class="mb-0"><i class="bi bi-exclamation-circle"></i> Missing Tracks</h6>
        </div>
        <div class="card-body" style="max-height: 400px; overflow-y: auto;">
          <div id="missingTracksContainer"></div>
        </div>
      </div>

      <!-- Action Buttons -->
      <div class="d-flex gap-2 mb-4">
        <button class="btn btn-success btn-lg" id="createPlaylistBtn" onclick="createPlaylist()">
          <i class="bi bi-check-circle"></i> Create Playlist
        </button>
        <button class="btn btn-outline-secondary btn-lg" id="searchMissingBtn" 
                onclick="searchMissingTracks()" style="display: none;">
          <i class="bi bi-cloud-arrow-down"></i> Search Missing on Soulseek
        </button>
      </div>
    </div>
  </div>
</div>
```

### Error Section
```html
<!-- ERROR SECTION -->
<div id="errorSection" style="display: none;" class="container-lg py-4">
  <div class="row">
    <div class="col-12">
      <div class="alert alert-danger" role="alert">
        <h5 class="alert-heading"><i class="bi bi-exclamation-triangle"></i> Error</h5>
        <p id="errorMessage"></p>
      </div>
    </div>
  </div>
</div>
```

### Replacement Track Modal
```html
<!-- REPLACEMENT TRACK MODAL -->
<div class="modal fade" id="replaceTrackModal" tabindex="-1" aria-hidden="true">
  <div class="modal-dialog modal-lg">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title">Find Replacement Track</h5>
        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
      </div>
      <div class="modal-body">
        <!-- Original Track Display -->
        <div class="row mb-4">
          <div class="col-12">
            <div class="card border-warning">
              <div class="card-header bg-warning bg-opacity-10">
                <h6 class="mb-0"><i class="bi bi-music-note"></i> Original Missing Track</h6>
              </div>
              <div class="card-body">
                <div id="originalTrackDisplay" class="track-row">
                  <div class="track-info">
                    <div class="track-title text-warning" id="originalTitle"></div>
                    <div class="track-artist" id="originalArtist"></div>
                    <div class="track-album" id="originalAlbum"></div>
                  </div>
                  <span class="badge bg-warning">Missing from Library</span>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- Arrow -->
        <div class="text-center mb-4">
          <i class="bi bi-arrow-down-circle-fill text-primary" style="font-size: 2rem;"></i>
        </div>

        <!-- Search for Replacement -->
        <div class="mb-3">
          <label class="form-label"><strong>Search Library for Replacement Track</strong></label>
          <div class="row g-2">
            <div class="col-12 col-md-4">
              <input type="text" class="form-control" id="replacementArtistInput" 
                     placeholder="Artist" onkeypress="if(event.key==='Enter') searchForReplacementTrack();" />
            </div>
            <div class="col-12 col-md-4">
              <input type="text" class="form-control" id="replacementTitleInput" 
                     placeholder="Title" onkeypress="if(event.key==='Enter') searchForReplacementTrack();" />
            </div>
            <div class="col-12 col-md-4">
              <input type="text" class="form-control" id="replacementAlbumInput" 
                     placeholder="Album (optional)" onkeypress="if(event.key==='Enter') searchForReplacementTrack();" />
            </div>
          </div>
          <div class="mt-2 d-flex justify-content-between align-items-center flex-wrap gap-2">
            <small class="text-muted">Provide artist/title to narrow matches; album is optional.</small>
            <button class="btn btn-primary" type="button" onclick="searchForReplacementTrack()">
              <i class="bi bi-search"></i> Search
            </button>
          </div>
        </div>

        <!-- Replacement Options Card -->
        <div class="card border-success">
          <div class="card-header bg-success bg-opacity-10">
            <h6 class="mb-0"><i class="bi bi-check-circle"></i> Select Replacement from Library</h6>
          </div>
          <div class="card-body">
            <div id="replacementTrackSearchResults" style="max-height: 400px; overflow-y: auto;">
              <p class="text-muted text-center py-4">Enter a search query above to find replacement tracks from your library</p>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>
```

## JavaScript Functions - Import

```javascript
// =============================================================================
// SPOTIFY PLAYLIST IMPORT
// =============================================================================

let currentImportData = null;
let missingTracksForSearch = [];

async function importPlaylist(event) {
  event.preventDefault();

  const spotifyUrl = document.getElementById('spotifyUrl').value.trim();
  const playlistName = document.getElementById('playlistName').value.trim();
  const playlistDescription = document.getElementById('playlistDescription').value.trim();
  const statusEl = document.getElementById('importStatus');

  if (!spotifyUrl || !playlistName) {
    alert('Please fill in all required fields');
    return;
  }

  // Show loading state
  statusEl.textContent = '⏳ Importing...';
  statusEl.classList.remove('text-danger');
  statusEl.classList.add('text-secondary');

  try {
    const response = await fetch('/api/playlist/import', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        spotify_url: spotifyUrl,
        playlist_name: playlistName,
        playlist_description: playlistDescription
      })
    });

    const data = await response.json();

    if (!response.ok) {
      throw new Error(data.error || 'Import failed');
    }

    currentImportData = data;
    displayResults(data);
    statusEl.textContent = '✅ Import complete!';
    statusEl.classList.remove('text-secondary', 'text-danger');
    statusEl.classList.add('text-success');

    // Store missing tracks for individual search buttons
    if (data.missing_tracks && data.missing_tracks.length > 0) {
      missingTracksForSearch = data.missing_tracks;
    }
  } catch (error) {
    console.error('Error:', error);
    statusEl.textContent = `❌ ${error.message}`;
    statusEl.classList.remove('text-secondary', 'text-success');
    statusEl.classList.add('text-danger');

    document.getElementById('errorSection').style.display = 'block';
    document.getElementById('errorMessage').textContent = error.message;
    document.getElementById('resultsSection').style.display = 'none';
  }
}

function displayResults(data) {
  const matchedCount = data.matched_tracks ? data.matched_tracks.length : 0;
  const missingCount = data.missing_tracks ? data.missing_tracks.length : 0;
  const totalCount = matchedCount + missingCount;
  const coverage = totalCount > 0 ? Math.round((matchedCount / totalCount) * 100) : 0;
  
  // Update summary
  document.getElementById('matchedCount').textContent = matchedCount;
  document.getElementById('missingCount').textContent = missingCount;
  document.getElementById('totalCount').textContent = totalCount;
  document.getElementById('coverage').textContent = coverage + '%';

  // Update progress bar color based on coverage
  const coverageEl = document.getElementById('coverage').parentElement.parentElement;
  if (coverage >= 90) {
    coverageEl.classList.remove('bg-warning', 'bg-danger');
    coverageEl.classList.add('bg-success');
  } else if (coverage >= 70) {
    coverageEl.classList.remove('bg-danger', 'bg-success');
    coverageEl.classList.add('bg-warning');
  } else {
    coverageEl.classList.remove('bg-success', 'bg-warning');
    coverageEl.classList.add('bg-danger');
  }

  // Display matched tracks (side-by-side playlist vs local)
  const matchedContainer = document.getElementById('matchedTracksContainer');
  if (matchedCount > 0) {
    matchedContainer.innerHTML = data.matched_tracks.map((track, idx) => `
      <div class="track-row">
        <div class="track-info split-col">
          <div class="track-label text-secondary">Playlist Track</div>
          <div class="track-title">${escapeHtml(track.title)}</div>
          <div class="track-artist">${escapeHtml(track.artist)}</div>
          <div class="track-album">${escapeHtml(track.album)}</div>
        </div>
        <div class="track-info split-col border-start ps-3">
          <div class="track-label text-success">Matched in Library</div>
          <div class="track-title text-success">${escapeHtml(track.title)}</div>
          <div class="track-artist">${escapeHtml(track.artist)}</div>
          <div class="track-album">${escapeHtml(track.album)}</div>
          ${track.stars !== undefined ? `<div class="text-muted small">Rating: ${track.stars || 0}★</div>` : ''}
        </div>
        <div class="track-actions">
          <span class="badge bg-success">✓ Found</span>
          <button class="btn btn-sm btn-outline-info" onclick="openReplacementTrackModal('${escapeHtml(track.artist)}', '${escapeHtml(track.title)}', '${escapeHtml(track.album || '')}', 'matched', ${idx})" title="Replace with a different local track">
            <i class="bi bi-arrow-repeat"></i> Replace
          </button>
        </div>
      </div>
    `).join('');
  } else {
    matchedContainer.innerHTML = '<p class="text-secondary text-center py-5">No matched tracks found</p>';
  }

  // Display missing tracks
  const missingContainer = document.getElementById('missingTracksContainer');
  if (missingCount > 0) {
    missingContainer.innerHTML = data.missing_tracks.map((track, idx) => `
      <div class="track-row">
        <div class="track-info">
          <div class="track-title">${escapeHtml(track.title)}</div>
          <div class="track-artist">${escapeHtml(track.artist)}</div>
          <div class="track-album">${escapeHtml(track.album || 'Unknown Album')}</div>
        </div>
        <div class="track-actions">
          <span class="missing-track-badge">✗ Missing</span>
          ${data.slskd_enabled ? `
          <button class="btn btn-sm btn-outline-success" onclick="searchTrackInSoulseek('${escapeHtml(track.artist + ' ' + track.title)}', this)" title="Search in Soulseek">
            <i class="bi bi-cloud-arrow-down"></i> Search
          </button>
          ` : ''}
          <button class="btn btn-sm btn-outline-info" onclick="openReplacementTrackModal('${escapeHtml(track.artist)}', '${escapeHtml(track.title)}', '${escapeHtml(track.album || '')}', 'missing', ${idx})" title="Replace with different song">
            <i class="bi bi-arrow-repeat"></i> Replace
          </button>
        </div>
      </div>
    `).join('');
  } else {
    missingContainer.innerHTML = '<p class="text-success text-center py-5">All tracks found in library!</p>';
  }

  // Show/hide the "Search Missing Tracks" button based on slskd status and if there are missing tracks
  const searchMissingBtn = document.getElementById('searchMissingBtn');
  if (searchMissingBtn) {
    if (data.slskd_enabled && missingCount > 0) {
      searchMissingBtn.style.display = 'inline-block';
    } else {
      searchMissingBtn.style.display = 'none';
    }
  }

  // Show results section
  document.getElementById('resultsSection').style.display = 'block';
  document.getElementById('errorSection').style.display = 'none';
}

async function createPlaylist() {
  if (!currentImportData) return;

  const createBtn = document.getElementById('createPlaylistBtn');
  const originalText = createBtn.innerHTML;
  createBtn.disabled = true;
  createBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Creating...';
  
  try {
    const response = await fetch('/api/playlist/create', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        playlist_name: currentImportData.playlist_name,
        playlist_description: currentImportData.playlist_description,
        matched_tracks: currentImportData.matched_tracks
      })
    });

    const data = await response.json();

    if (!response.ok) {
      throw new Error(data.error || 'Playlist creation failed');
    }

    alert(`✅ Playlist "${currentImportData.playlist_name}" created successfully!`);
    createBtn.innerHTML = '<i class="bi bi-check-circle"></i> Playlist Created!';
    createBtn.classList.remove('btn-primary');
    createBtn.classList.add('btn-success');
  } catch (error) {
    alert(`❌ Error: ${error.message}`);
    createBtn.disabled = false;
    createBtn.innerHTML = originalText;
  }
}

async function searchMissingTracks() {
  if (missingTracksForSearch.length === 0) {
    alert('No missing tracks to search');
    return;
  }

  const searchBtn = document.getElementById('searchMissingBtn');
  const originalText = searchBtn.innerHTML;
  searchBtn.disabled = true;
  searchBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Searching...';
  
  try {
    // Start searches for all missing tracks
    for (const track of missingTracksForSearch) {
      const query = `${track.artist} ${track.title}`;

      const response = await fetch('/api/slskd/search', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({ query })
      });

      if (response.ok) {
        const data = await response.json();
        console.log(`Started search for: ${query}`, data);
      }

      // Small delay between requests
      await new Promise(resolve => setTimeout(resolve, 500));
    }

    alert(`✅ Started searching for ${missingTracksForSearch.length} missing tracks in Soulseek. Check the Downloads page for results!`);
    searchBtn.disabled = false;
    searchBtn.innerHTML = originalText;
  } catch (error) {
    alert(`❌ Error starting searches: ${error.message}`);
    searchBtn.disabled = false;
    searchBtn.innerHTML = originalText;
  }
}

async function searchTrackInSoulseek(query, buttonEl) {
  const originalText = buttonEl.innerHTML;
  buttonEl.disabled = true;
  buttonEl.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
  
  try {
    const response = await fetch('/api/slskd/search', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ query: query.trim() })
    });

    if (!response.ok) {
      throw new Error('Search failed');
    }

    const data = await response.json();
    buttonEl.innerHTML = '<i class="bi bi-check-circle text-success"></i> Searching...';
    buttonEl.classList.add('btn-success');
    buttonEl.classList.remove('btn-outline-success');

    // Poll for results immediately
    setTimeout(() => {
      pollSoulseekResults(data.searchId, query, buttonEl, originalText);
    }, 1000);
  } catch (error) {
    alert(`❌ Error searching for track: ${error.message}`);
    buttonEl.disabled = false;
    buttonEl.innerHTML = originalText;
  }
}

async function pollSoulseekResults(searchId, query, buttonEl, originalText) {
  try {
    const response = await fetch(`/api/slskd/search/${searchId}`);
    if (!response.ok) throw new Error('Failed to get results');

    const data = await response.json();

    if (!data.isComplete) {
      // Still searching, poll again after 2 seconds
      setTimeout(() => {
        pollSoulseekResults(searchId, query, buttonEl, originalText);
      }, 2000);
      return;
    }

    // Results are complete
    if (data.fileCount === 0) {
      alert(`⚠️ No results found for: "${query}"`);
      buttonEl.disabled = false;
      buttonEl.innerHTML = originalText;
      return;
    }

    // Show modal with results and download options
    showSoulseekResultsModal(data.results, query);
    buttonEl.disabled = false;
    buttonEl.innerHTML = originalText;
  } catch (error) {
    console.error('Error polling results:', error);
    buttonEl.disabled = false;
    buttonEl.innerHTML = originalText;
  }
}

function showSoulseekResultsModal(results, query) {
  let html = `
    <div class="modal fade" id="soulseekResultsModal" tabindex="-1" aria-hidden="true">
      <div class="modal-dialog modal-lg">
        <div class="modal-content">
          <div class="modal-header">
            <h5 class="modal-title">Soulseek Results for: ${escapeHtml(query)}</h5>
            <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
          </div>
          <div class="modal-body" style="max-height: 500px; overflow-y: auto;">
            <div class="list-group">
  `;

  results.forEach((result, idx) => {
    html += `
      <div class="list-group-item">
        <div class="d-flex justify-content-between align-items-start">
          <div class="flex-grow-1">
            <h6 class="mb-1">${escapeHtml(result.filename)}</h6>
            <small class="text-muted">
              <i class="bi bi-person"></i> ${escapeHtml(result.username)} |
              <i class="bi bi-file-earmark-music"></i> ${result.size_mb} MB |
              <i class="bi bi-speedometer"></i> ${result.bitrate || 'unknown'} bps
            </small>
          </div>
          <button class="btn btn-sm btn-success" onclick="downloadSoulseekFile('${escapeHtml(result.username)}', '${escapeHtml(result.filename)}', '${escapeHtml(result.size)}')">
            <i class="bi bi-download"></i> Download
          </button>
        </div>
      </div>
    `;
  });

  html += `
            </div>
          </div>
          <div class="modal-footer">
            <small class="text-muted">Found ${results.length} result(s). Downloads will appear on the Downloads page.</small>
          </div>
        </div>
      </div>
    </div>
  `;

  // Remove old modal if exists
  const oldModal = document.getElementById('soulseekResultsModal');
  if (oldModal) oldModal.remove();

  // Add new modal to body
  document.body.insertAdjacentHTML('beforeend', html);

  // Show modal
  const modal = new bootstrap.Modal(document.getElementById('soulseekResultsModal'));
  modal.show();
}

async function downloadSoulseekFile(username, filename, size) {
  try {
    const response = await fetch('/api/slskd/download-single', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        username: username,
        filename: filename,
        size: parseInt(size)
      })
    });

    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.error || 'Download failed');
    }

    const data = await response.json();
    alert(`✅ Download started for: ${escapeHtml(filename)}`);

    // Close modal
    const modal = bootstrap.Modal.getInstance(document.getElementById('soulseekResultsModal'));
    if (modal) modal.hide();
  } catch (error) {
    alert(`❌ Error downloading: ${error.message}`);
  }
}

// Replacement track modal functions
let replacingTrackInfo = null;

function openReplacementTrackModal(artist, title, album, source = 'missing', sourceIndex = null) {
  replacingTrackInfo = { artist, title, album, source, sourceIndex };

  // Populate original track display
  document.getElementById('originalTitle').textContent = title;
  document.getElementById('originalArtist').textContent = artist;
  document.getElementById('originalAlbum').textContent = album || 'Unknown Album';
  
  // Pre-populate search fields for easier searching
  document.getElementById('replacementArtistInput').value = artist || '';
  document.getElementById('replacementTitleInput').value = title || '';
  document.getElementById('replacementAlbumInput').value = album || '';
  document.getElementById('replacementTrackSearchResults').innerHTML = '<p class="text-muted text-center py-4">Enter artist/title above to find replacement tracks from your library</p>';
  
  const modal = new bootstrap.Modal(document.getElementById('replaceTrackModal'));
  modal.show();
}

async function searchForReplacementTrack() {
  const artist = document.getElementById('replacementArtistInput').value.trim();
  const title = document.getElementById('replacementTitleInput').value.trim();
  const album = document.getElementById('replacementAlbumInput').value.trim();

  const combined = [artist, title, album].filter(Boolean).join(' ').trim();
  if (!combined) {
    alert('Please provide at least one search field');
    return;
  }

  const resultsDiv = document.getElementById('replacementTrackSearchResults');
  resultsDiv.innerHTML = '<div class="text-center"><span class="spinner-border spinner-border-sm"></span> Searching...</div>';
  
  try {
    const response = await fetch('/api/playlist/search-songs', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ query: combined, artist, title, album })
    });

    if (!response.ok) {
      const error = await response.text();
      resultsDiv.innerHTML = `<p class="text-danger text-center">Error: ${error}</p>`;
      return;
    }

    const data = await response.json();

    const tracks = data.songs || [];

    if (!tracks || tracks.length === 0) {
      resultsDiv.innerHTML = '<p class="text-muted text-center">No tracks found</p>';
      return;
    }

    // Display tracks
    let html = '<div style="border-top: 1px solid var(--border-color);">';
    tracks.forEach(track => {
      html += `
        <div class="track-row" style="padding: 0.75rem 0; border-bottom: 1px solid var(--border-color);">
          <div class="track-info">
            <div class="track-title text-success">${escapeHtml(track.title)}</div>
            <div class="track-artist">${escapeHtml(track.artist)}</div>
            <div class="track-album">${escapeHtml(track.album || 'Unknown Album')}</div>
          </div>
          <div class="track-actions">
            <button class="btn btn-sm btn-success" onclick="replaceSelectedTrack('${escapeHtml(track.title)}', '${escapeHtml(track.artist)}', '${escapeHtml(track.album || '')}', '${escapeHtml(track.id || '')}'"
                    title="Swap with this track from your library">
              <i class="bi bi-arrow-left-right"></i> Swap with This Track
            </button>
          </div>
        </div>
      `;
    });
    html += '</div>';
    resultsDiv.innerHTML = html;
  } catch (error) {
    resultsDiv.innerHTML = `<div class="alert alert-danger">Error: ${error.message}</div>`;
  }
}

function replaceSelectedTrack(title, artist, album, trackId) {
  if (!currentImportData || !replacingTrackInfo) {
    alert('No playlist data found');
    return;
  }

  const { source, sourceIndex } = replacingTrackInfo;

  if (source === 'missing') {
    const missingIndex = currentImportData.missing_tracks.findIndex((t, idx) =>
      idx === sourceIndex || (t.title === replacingTrackInfo.title && t.artist === replacingTrackInfo.artist)
    );
    if (missingIndex !== -1) {
      currentImportData.missing_tracks.splice(missingIndex, 1);
    }
  }

  if (source === 'matched' && sourceIndex !== null && currentImportData.matched_tracks[sourceIndex]) {
    currentImportData.matched_tracks[sourceIndex] = {
      id: trackId,
      title,
      artist,
      album,
      stars: 0
    };
  } else {
    currentImportData.matched_tracks.push({
      id: trackId,
      title,
      artist,
      album,
      stars: 0
    });
  }

  // Close modal
  bootstrap.Modal.getInstance(document.getElementById('replaceTrackModal')).hide();
  // Re-display results with updated data
  displayResults(currentImportData);

  // Show success message with clear before/after
  alert(`✅ Swapped Successfully!\n\n` +
    `Playlist track: "${replacingTrackInfo.artist} - ${replacingTrackInfo.title}"\n` +
    `Now using library track: "${artist} - ${title}"`);
  replacingTrackInfo = null;
}
```

## CSS Styling

```css
/* Track Row Styling */
.track-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0.75rem 0;
  border-bottom: 1px solid var(--border-color);
}

.track-row:last-child {
  border-bottom: none;
}

.track-info {
  flex: 1;
}

.split-col {
  flex: 1;
  min-width: 220px;
}

.track-label {
  font-size: 0.8rem;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 0.25rem;
}

.track-title {
  font-weight: 600;
  color: var(--spotify-green);
  margin-bottom: 0.25rem;
}

.track-artist {
  color: var(--text-secondary);
  font-size: 0.9rem;
}

.track-album {
  color: var(--text-tertiary);
  font-size: 0.85rem;
}

.track-actions {
  display: flex;
  gap: 0.5rem;
  align-items: center;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.missing-track-badge {
  background-color: rgba(255, 193, 7, 0.1);
  color: #ffc107;
  padding: 0.25rem 0.75rem;
  border-radius: 4px;
  font-size: 0.8rem;
  white-space: nowrap;
}

.track-actions .btn {
  padding: 0.35rem 0.6rem;
  font-size: 0.85rem;
  min-width: auto;
  white-space: nowrap;
}

.track-actions .btn i {
  margin-right: 0.25rem;
}

/* Dark theme support */
:root {
  --border-color: #495057;
  --text-secondary: #adb5bd;
  --text-tertiary: #868e96;
  --spotify-green: #1DB954;
}

@media (prefers-color-scheme: dark) {
  .bg-light {
    background-color: var(--secondary-bg) !important;
  }
}
```

---

# COMPLETE FUNCTION CROSS-REFERENCE

## All JavaScript Functions by Template

### playlists_browse.html
- `loadPlaylistList()` - Fetch all playlists from /api/playlist/list
- `loadPlaylistForDownload()` - Load specific playlist and show matches
- `displayPlaylistDownloader(data)` - Render original & matched track columns
- `replacePlaylistMatch(matchIndex)` - TODO: Replace match functionality
- `downloadPlaylistMatches()` - Queue matched files for download
- `loadSmartPlaylists()` - Fetch smart playlists from /api/navidrome/playlists
- `loadSmartPlaylistDetail()` - Load details for selected smart playlist
- `displaySmartPlaylistDetails(data)` - Show smart playlist info & tracks
- `editPlaylist()` - Open in Navidrome
- `refreshPlaylistDetails()` - Refresh smart playlist detail
- `refreshSmartPlaylistDetail()` - Refresh after changes
- `formatDuration(seconds)` - Convert seconds to MM:SS format
- `escapeHtml(text)` - Prevent XSS

### playlists_create.html
- `searchSongs()` - Search library with multiple fields
- `displaySearchResults(songs)` - Show search results with add buttons
- `getSongKey(song)` - Generate unique key for duplicate detection
- `addSongToPlaylistObj(song)` - Add to selectedSongs array
- `updateSelectedSongsList()` - Render selected songs list
- `removeSongFromPlaylist(index)` - Remove by index
- `createCustomPlaylist(event)` - POST to /api/playlist/create-custom
- `loadLastfmRecommendations()` - Fetch Last.fm recommendations
- `createPlaylistFromLastfm()` - Create playlist from matched tracks
- `searchLastfmMissingTracksOnSoulseek()` - TODO: Batch Soulseek search
- `createPlaylistFromListenBrainz()` - Create playlist from ListenBrainz matches
- `loadListenBrainzRecommendations()` - Fetch ListenBrainz data
- `searchMissingTracksOnSoulseek()` - TODO: Batch Soulseek search

### playlists_import.html
- `importPlaylist(event)` - Main import flow from Spotify URL
- `displayResults(data)` - Render matched/missing track columns
- `createPlaylist()` - POST to /api/playlist/create
- `searchMissingTracks()` - Batch search all missing on Soulseek
- `searchTrackInSoulseek(query, buttonEl)` - Single track search
- `pollSoulseekResults(searchId, query, buttonEl, originalText)` - Poll search status
- `showSoulseekResultsModal(results, query)` - Render modal with results
- `downloadSoulseekFile(username, filename, size)` - Queue Soulseek download
- `openReplacementTrackModal(artist, title, album, source, sourceIndex)` - Show modal
- `searchForReplacementTrack()` - Search library for replacement
- `replaceSelectedTrack(title, artist, album, trackId)` - Swap track

---

# DEPENDENCIES & DATA FLOW

## Global Variables
```javascript
let currentPlaylistData = null;
let currentPlaylistId = null;
let currentSmartPlaylistId = null;
let currentSmartPlaylistData = null;
let selectedSongs = [];
let lfmRecommendationsData = null;
let lbRecommendationsData = null;
let currentImportData = null;
let missingTracksForSearch = [];
let selectedReplacements = {};
let replacingTrackInfo = null;
```

## API Endpoints Used
- `GET /api/playlist/list` - List all playlists
- `POST /api/playlist/load` - Load playlist details
- `POST /api/playlist/download` - Download matched files
- `GET /api/navidrome/playlists` - List smart playlists
- `GET /api/navidrome/playlist/{id}` - Get smart playlist details
- `POST /api/playlist/search-songs` - Search songs by title/artist/album
- `POST /api/playlist/create-custom` - Create custom playlist
- `POST /api/lastfm/create-playlist` - Get Last.fm recommendations
- `POST /api/listenbrainz/create-playlist` - Get ListenBrainz recommendations
- `POST /api/playlist/import` - Import from Spotify URL
- `POST /api/playlist/create` - Create playlist from import
- `POST /api/slskd/search` - Search Soulseek
- `GET /api/slskd/search/{id}` - Poll Soulseek results
- `POST /api/slskd/download-single` - Download from Soulseek

---

# INITIALIZATION & SETUP

## Page Load Handlers
```javascript
// Load all playlists on page load
document.addEventListener('DOMContentLoaded', function() {
  loadPlaylistList();
  loadSmartPlaylists();
  loadSpotifyPlaylists();
});
```

## Tab Navigation
```javascript
// Likely function (not shown in extract):
function switchTab(tabName) {
  // Hide all tabs
  document.querySelectorAll('.tab-pane').forEach(tab => tab.classList.remove('show'));
  // Show selected tab
  document.getElementById(tabName + 'Content').classList.add('show');
}
```

---

# KEY DESIGN PATTERNS

## Pattern 1: Empty/Results State Toggle
```javascript
// Show empty state
document.getElementById('emptyState').style.display = 'block';
document.getElementById('resultsState').style.display = 'none';

// Show results when data loaded
document.getElementById('emptyState').style.display = 'none';
document.getElementById('resultsState').style.display = 'block';
```

## Pattern 2: Loading Button Spinner
```javascript
const btn = document.getElementById('actionBtn');
const originalText = btn.innerHTML;
btn.disabled = true;
btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Loading...';

// After completion
btn.disabled = false;
btn.innerHTML = originalText;
```

## Pattern 3: Side-by-Side Comparison
```html
<div class="track-row">
  <div class="track-info split-col">
    <!-- Column 1: Source Track -->
  </div>
  <div class="track-info split-col border-start ps-3">
    <!-- Column 2: Matched Track -->
  </div>
  <div class="track-actions">
    <!-- Action Buttons -->
  </div>
</div>
```

## Pattern 4: Modal with Search Results
Replacement track modal opens with original track display, search form populates from modal context, results display in scrollable container, selection updates parent data.

---

# SHARED UTILITY FUNCTION

```javascript
function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}
```

**This function should be in ALL three templates.**

