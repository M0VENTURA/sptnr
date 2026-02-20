// ===============================
// PLAYLIST MANAGER - JAVASCRIPT
// ===============================
// Unified JavaScript for all playlist pages (browse, create, import)

// ===============================
// GLOBAL VARIABLES
// ===============================
let currentPlaylistId = null;
let currentPlaylistData = null;
let currentSmartPlaylistId = null;
let currentSmartPlaylistData = null;
let currentImportData = null;
let missingTracksForSearch = [];
let spotifyPlaylistsData = [];
let lfmRecommendationsData = null;
let lbRecommendationsData = null;
let replacingTrackInfo = null;
let selectedSongs = [];
let selectedReplacements = {};

// ===============================
// UTILITY FUNCTIONS
// ===============================

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

function formatDuration(seconds) {
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  return `${mins}:${secs.toString().padStart(2, '0')}`;
}

// ===============================
// DOCUMENT INITIALIZATION
// ===============================

document.addEventListener('DOMContentLoaded', function() {
  try {
    console.log('[Playlist Manager] DOMContentLoaded starting');
    
    // Initialize Bootstrap tabs if they exist
    const tabElements = document.querySelectorAll('button[data-bs-toggle="tab"]');
    console.log(`[Playlist Manager] Found ${tabElements.length} tab buttons`);
    
    // Load initial data based on page type
    const pageType = document.body.dataset.pageType || 'browse';
    
    Promise.all([
      loadNavidromePlaylists().catch(e => {
        console.error('Error loading Navidrome playlists:', e);
      }),
      loadPlaylistList().catch(e => {
        console.error('Error loading playlist list:', e);
      }),
      loadSmartPlaylists().catch(e => {
        console.error('Error loading smart playlists:', e);
      }),
      (pageType === 'import' ? loadSpotifyPlaylists() : Promise.resolve()).catch(e => {
        console.error('Error loading Spotify playlists:', e);
      })
    ]).then(() => {
      console.log('[Playlist Manager] Initialization complete');
    }).catch(e => {
      console.error('[Playlist Manager] Initialization error:', e);
    });

    // Setup event listeners for browse tab
    setupBrowsePageListeners();
    setupCreatePageListeners();
    setupImportPageListeners();

  } catch (error) {
    console.error('[Playlist Manager] DOMContentLoaded error:', error);
  }
});

// ===============================
// BROWSE PAGE SETUP
// ===============================

function setupBrowsePageListeners() {
  try {
    const smartPlaylistSelect = document.getElementById('smartPlaylistSelect');
    if (smartPlaylistSelect) {
      smartPlaylistSelect.addEventListener('change', function() {
        if (this.value) {
          document.getElementById('regularPlaylistSelect').value = '';
          loadNavidromePlaylistDetail(this.value, 'smart');
        }
      });
    }
  } catch (e) {
    console.warn('[Playlist Manager] Could not set up smartPlaylistSelect listener:', e);
  }

  try {
    const regularPlaylistSelect = document.getElementById('regularPlaylistSelect');
    if (regularPlaylistSelect) {
      regularPlaylistSelect.addEventListener('change', function() {
        if (this.value) {
          document.getElementById('smartPlaylistSelect').value = '';
          loadNavidromePlaylistDetail(this.value, 'regular');
        }
      });
    }
  } catch (e) {
    console.warn('[Playlist Manager] Could not set up regularPlaylistSelect listener:', e);
  }

  // Tab switching events
  const smartTab = document.getElementById('smartTab');
  if (smartTab) {
    smartTab.addEventListener('show.bs.tab', function(e) {
      console.log('[Playlist Manager] Smart tab shown');
      loadSmartPlaylists().catch(err => {
        console.error('[Playlist Manager] Error loading smart playlists on tab switch:', err);
      });
    });
  }

  const browseTab = document.getElementById('browseTab');
  if (browseTab) {
    browseTab.addEventListener('show.bs.tab', function(e) {
      console.log('[Playlist Manager] Browse tab shown');
    });
  }
}

// ===============================
// CREATE PAGE SETUP
// ===============================

function setupCreatePageListeners() {
  // Search and create playlist form handling
  const customPlaylistForm = document.getElementById('customPlaylistForm');
  if (customPlaylistForm) {
    customPlaylistForm.addEventListener('submit', createCustomPlaylist);
  }
}

// ===============================
// IMPORT PAGE SETUP
// ===============================

function setupImportPageListeners() {
  const playlistForm = document.getElementById('playlistForm');
  if (playlistForm) {
    playlistForm.addEventListener('submit', importPlaylist);
  }

  // Spotify user ID input handlers
  const spotifyUserIdInput = document.getElementById('spotifyUserId');
  if (spotifyUserIdInput) {
    spotifyUserIdInput.addEventListener('keypress', function(e) {
      if (e.key === 'Enter') {
        loadSpotifyPlaylistsByUser();
      }
    });
  }
}

// ===============================
// NAVIDROME PLAYLISTS
// ===============================

async function loadNavidromePlaylists() {
  try {
    console.log('[Playlist Manager] Starting loadNavidromePlaylists');
    const response = await fetch('/api/navidrome/playlists');
    const data = await response.json();
    console.log('[Playlist Manager] API response:', data);
    
    const smartSelect = document.getElementById('smartPlaylistSelect');
    const regularSelect = document.getElementById('regularPlaylistSelect');
    
    if (!smartSelect || !regularSelect) {
      console.error('[Playlist Manager] Could not find playlist select elements');
      return;
    }

    smartSelect.innerHTML = '<option value="">Select a smart playlist...</option>';
    regularSelect.innerHTML = '<option value="">Select a regular playlist...</option>';
    
    if (data.error) {
      console.error('[Playlist Manager] API Error:', data.error);
      smartSelect.innerHTML = '<option value="">Error: ' + data.error + '</option>';
      regularSelect.innerHTML = '<option value="">Error: ' + data.error + '</option>';
      return;
    }
    
    if (data.smart && data.smart.length > 0) {
      data.smart.forEach(pl => {
        const opt = document.createElement('option');
        opt.value = pl.id;
        opt.textContent = pl.name;
        smartSelect.appendChild(opt);
      });
    }
    if (data.regular && data.regular.length > 0) {
      data.regular.forEach(pl => {
        const opt = document.createElement('option');
        opt.value = pl.id;
        opt.textContent = pl.name;
        regularSelect.appendChild(opt);
      });
    }
    if ((!data.smart || data.smart.length === 0) && (!data.regular || data.regular.length === 0)) {
      console.warn('[Playlist Manager] No playlists found in Navidrome');
      smartSelect.innerHTML = '<option value="">No smart playlists found</option>';
      regularSelect.innerHTML = '<option value="">No regular playlists found</option>';
    }
  } catch (e) {
    console.error('[Playlist Manager] Failed to load Navidrome playlists:', e);
  }
}

async function loadNavidromePlaylistDetail(playlistId, type) {
  currentPlaylistId = playlistId;
  
  try {
    const response = await fetch(`/api/navidrome/playlist/${playlistId}`);
    const data = await response.json();
    
    if (!response.ok) {
      throw new Error(data.error || 'Failed to load playlist');
    }
    
    currentPlaylistData = data;
    displayPlaylistDetails(data, type);
  } catch (e) {
    console.error('Failed to load playlist details:', e);
    alert('Failed to load playlist: ' + e.message);
  }
}

function displayPlaylistDetails(data, type) {
  const detailsSection = document.getElementById('playlistDetailsSection');
  const nameEl = document.getElementById('playlistDetailName');
  const metadataEl = document.getElementById('playlistMetadata');
  const tracksEl = document.getElementById('playlistTracks');
  
  if (!detailsSection) return;
  
  nameEl.textContent = data.name || 'Unknown Playlist';
  
  let metadataHtml = '<div class="row g-3">';
  metadataHtml += `<div class="col-md-3"><strong>Type:</strong> ${type === 'smart' ? 'Smart Playlist' : 'Regular Playlist'}</div>`;
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
  
  if (data.tracks && data.tracks.length > 0) {
    let tracksHtml = '<h6 class="mt-3 mb-2">Tracks</h6>';
    tracksHtml += '<div style="max-height: 400px; overflow-y: auto;">';
    data.tracks.forEach((track, idx) => {
      tracksHtml += `
        <div class="d-flex justify-content-between align-items-center py-2 border-bottom">
          <div>
            <span class="text-muted me-2">${idx + 1}.</span>
            <strong>${escapeHtml(track.title || 'Unknown Track')}</strong><br>
            <small class="text-muted ms-4">${escapeHtml(track.artist || 'Unknown Artist')} ${track.album ? '– ' + escapeHtml(track.album) : ''}</small>
          </div>
          ${track.duration ? `<small class="text-muted">${formatDuration(track.duration)}</small>` : ''}
        </div>
      `;
    });
    tracksHtml += '</div>';
    tracksEl.innerHTML = tracksHtml;
  } else {
    tracksEl.innerHTML = '<p class="text-muted text-center py-3">No tracks available</p>';
  }
  
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
    const type = document.getElementById('smartPlaylistSelect')?.value ? 'smart' : 'regular';
    loadNavidromePlaylistDetail(currentPlaylistId, type);
  }
}

// ===============================
// PLAYLIST DOWNLOADER
// ===============================

async function loadPlaylistList() {
  try {
    console.log('[Playlist Manager] Starting loadPlaylistList');
    const response = await fetch('/api/playlist/list');
    const data = await response.json();
    
    const select = document.getElementById('playlistFileSelect');
    if (!select) return;
    
    select.innerHTML = '<option value="">Select a playlist...</option>';
    
    if (!data || !data.playlists) {
      console.error('[Playlist Manager] Invalid playlist list response:', data);
      select.innerHTML = '<option value="">Error: Invalid API response</option>';
      return;
    }
    
    if (data.playlists && data.playlists.length > 0) {
      data.playlists.forEach(playlist => {
        const option = document.createElement('option');
        option.value = playlist.path;
        let typeLabel = '';
        if (playlist.type === 'smart' || playlist.type === 'smart-local') typeLabel = ' (Smart)';
        option.textContent = `${playlist.name || playlist.path}${typeLabel} [${playlist.songCount || 0}]`;
        select.appendChild(option);
      });
    } else {
      select.innerHTML = '<option value="">No playlists found</option>';
    }
  } catch (error) {
    console.error('Error loading playlists:', error);
    const select = document.getElementById('playlistFileSelect');
    if (select) {
      select.innerHTML = '<option value="">Error loading playlists</option>';
    }
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
  
  const downloaderEmpty = document.getElementById('downloaderEmpty');
  const downloaderResults = document.getElementById('downloaderResults');
  
  if (!downloaderEmpty || !downloaderResults) return;
  
  downloaderEmpty.style.display = 'none';
  downloaderResults.style.display = 'block';

  document.getElementById('dlTotalCount').textContent = songs.length;
  document.getElementById('dlMatchedCount').textContent = matched.length;
  const coverage = songs.length > 0 ? Math.round((matched.length / songs.length) * 100) : 0;
  document.getElementById('dlCoverage').textContent = coverage + '%';

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
  document.getElementById('dlDetectedMatches').innerHTML = matchedHtml || '<p class="text-muted text-center py-5">No matches detected</p>';

  selectedReplacements = {};
}

function replacePlaylistMatch(matchIndex) {
  alert('Replace functionality coming soon');
}

async function downloadPlaylistMatches() {
  if (!currentPlaylistData) return;
  const btn = document.getElementById('dlDownloadBtn');
  if (!btn) return;
  
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

    alert('✓ Download queued! ' + (data.queued || 0) + ' files enqueued');
  } catch (error) {
    console.error('Error:', error);
    alert('✗ Error: ' + error.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = originalText;
  }
}

// ===============================
// CUSTOM PLAYLIST CREATOR
// ===============================

async function searchSongs() {
  const query = document.getElementById('songSearchInput')?.value.trim() || '';
  const title = document.getElementById('searchTitleInput')?.value.trim() || '';
  const artist = document.getElementById('searchArtistInput')?.value.trim() || '';
  const album = document.getElementById('searchAlbumInput')?.value.trim() || '';

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
  if (!resultsDiv) return;
  
  if (songs.length === 0) {
    resultsDiv.innerHTML = '<p class="text-muted text-center">No songs found</p>';
    resultsDiv.style.display = 'block';
    return;
  }

  resultsDiv.innerHTML = songs.map((song, idx) => `
    <div class="d-flex justify-content-between align-items-center py-2 border-bottom">
      <div>
        <strong>${escapeHtml(song.title || 'Unknown')}</strong><br>
        <small class="text-muted">${escapeHtml(song.artist || 'Unknown')} ${song.album ? '– ' + escapeHtml(song.album) : ''}</small>
      </div>
      <button class="btn btn-sm btn-outline-success" data-song-idx="${idx}" type="button">
        <i class="bi bi-plus"></i> Add
      </button>
    </div>
  `).join('');
  
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
  return song.id || `${song.title || ''}|${song.artist || ''}|${song.album || ''}`;
}

function addSongToPlaylistObj(song) {
  const key = getSongKey(song);
  if (selectedSongs.find(s => getSongKey(s) === key)) {
    alert('This song is already in the playlist');
    return;
  }
  selectedSongs.push(song);
  updateSelectedSongsList();
}

function updateSelectedSongsList() {
  const listDiv = document.getElementById('selectedSongsList');
  const countBadge = document.getElementById('selectedSongsCount');
  
  if (!listDiv || !countBadge) return;
  
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
      throw new Error('Invalid JSON response from server.');
    }
    if (!response.ok) throw new Error(data.error || 'Failed to create playlist');

    alert('✓ Playlist created successfully!');
    event.target.reset();
    selectedSongs = [];
    updateSelectedSongsList();
  } catch (error) {
    console.error('Error:', error);
    alert('✗ Error: ' + error.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = originalText;
  }
}

// ===============================
// SMART PLAYLISTS
// ===============================

async function loadSmartPlaylists() {
  try {
    console.log('[Playlist Manager] Loading smart playlists dropdown');
    const response = await fetch('/api/navidrome/playlists');
    const data = await response.json();
    
    const select = document.getElementById('smartPlaylistDropdown');
    if (!select) {
      console.warn('[Playlist Manager] smartPlaylistDropdown element not found');
      return;
    }
    
    select.innerHTML = '<option value="">Select a smart playlist...</option>';
    
    if (data.error) {
      console.error('[Playlist Manager] API Error:', data.error);
      select.innerHTML = '<option value="">Error: ' + data.error + '</option>';
      return;
    }
    
    if (data.smart && data.smart.length > 0) {
      data.smart.forEach(playlist => {
        const option = document.createElement('option');
        option.value = playlist.id;
        option.textContent = playlist.name;
        select.appendChild(option);
      });
    } else {
      select.innerHTML = '<option value="">No smart playlists found</option>';
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
  if (!detailsSection) return;
  
  const nameEl = document.getElementById('smartPlaylistName');
  const metadataEl = document.getElementById('smartPlaylistMetadata');
  const tracksEl = document.getElementById('smartPlaylistTracks');
  
  nameEl.textContent = data.name || 'Unknown Smart Playlist';
  
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
  
  if (data.tracks && data.tracks.length > 0) {
    let tracksHtml = '<h6 class="mb-2">Tracks</h6>';
    data.tracks.forEach((track, idx) => {
      tracksHtml += `
        <div class="d-flex justify-content-between align-items-center py-2 border-bottom">
          <div style="flex: 1;">
            <strong>${escapeHtml(track.title || 'Unknown Track')}</strong><br>
            <small class="text-muted">${escapeHtml(track.artist || 'Unknown Artist')} ${track.album ? '– ' + escapeHtml(track.album) : ''}</small>
          </div>
          ${track.duration ? `<small class="text-muted">${formatDuration(track.duration)}</small>` : ''}
        </div>
      `;
    });
    tracksEl.innerHTML = tracksHtml;
  } else {
    tracksEl.innerHTML = '<p class="text-muted text-center py-3">No tracks in this playlist</p>';
  }
  
  document.getElementById('smartPlaylistEmpty').style.display = 'none';
  detailsSection.style.display = 'block';
  detailsSection.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function refreshSmartPlaylistDetail() {
  if (currentSmartPlaylistId) {
    loadSmartPlaylistDetail();
  }
}

// ===============================
// SPOTIFY PLAYLIST IMPORT
// ===============================

async function loadSpotifyPlaylists(userId = null) {
  const container = document.getElementById('playlistsContainer');
  const loading = document.getElementById('playlistsLoading');
  const error = document.getElementById('playlistsError');
  const empty = document.getElementById('playlistsEmpty');
  const grid = document.getElementById('playlistsGrid');
  const refreshBtn = document.getElementById('refreshPlaylistsBtn');

  if (!container || !loading) return;

  loading.style.display = 'block';
  container.style.display = 'none';
  if (error) error.style.display = 'none';
  if (empty) empty.style.display = 'none';
  if (refreshBtn) refreshBtn.disabled = true;

  try {
    let url = '/api/spotify/playlists';
    if (userId) {
      url += `?user_id=${encodeURIComponent(userId)}`;
    }
    
    const response = await fetch(url);
    const data = await response.json();

    if (!response.ok) {
      throw new Error(data.error || 'Failed to load playlists');
    }

    const playlists = data.playlists || [];
    spotifyPlaylistsData = playlists;
    
    if (playlists.length === 0) {
      loading.style.display = 'none';
      if (empty) {
        empty.style.display = 'block';
        const emptyText = document.getElementById('playlistsEmptyText');
        if (emptyText) {
          emptyText.innerHTML = data.user_id 
            ? `No public playlists found for user <strong>${escapeHtml(data.user_id)}</strong>`
            : 'No playlists found';
        }
      }
      return;
    }

    if (grid) {
      grid.innerHTML = '';
      playlists.forEach(playlist => {
        const card = document.createElement('div');
        card.className = 'col-12 col-sm-6 col-md-4 col-lg-3';
        card.innerHTML = `
          <div class="card h-100 playlist-card">
            ${playlist.image_url ? `<img src="${escapeHtml(playlist.image_url)}" class="card-img-top" alt="${escapeHtml(playlist.name)}" style="height: 150px; object-fit: cover;">` : `<div class="card-img-top bg-secondary" style="height: 150px; display: flex; align-items: center; justify-content: center;"><i class="bi bi-music-note" style="font-size: 2rem; color: white;"></i></div>`}
            <div class="card-body d-flex flex-column">
              <h6 class="card-title text-truncate" title="${escapeHtml(playlist.name)}">${escapeHtml(playlist.name)}</h6>
              ${playlist.owner ? `<small class="text-muted">by ${escapeHtml(playlist.owner)}</small>` : ''}
              <small class="text-muted mt-2">
                <i class="bi bi-music-note-list"></i> ${playlist.track_count} track${playlist.track_count !== 1 ? 's' : ''}
              </small>
              <button class="btn btn-sm btn-primary mt-auto" onclick="importPlaylistFromSpotify('${escapeHtml(playlist.id)}', '${escapeHtml(playlist.name)}')">
                <i class="bi bi-download"></i> Import
              </button>
            </div>
          </div>
        `;
        grid.appendChild(card);
      });

      loading.style.display = 'none';
      container.style.display = 'block';
    }
  } catch (err) {
    loading.style.display = 'none';
    if (error) {
      error.style.display = 'block';
      const errorText = document.getElementById('playlistsErrorText');
      if (errorText) {
        errorText.textContent = err.message || 'Unable to load playlists';
      }
    }
  } finally {
    if (refreshBtn) refreshBtn.disabled = false;
  }
}

function loadSpotifyPlaylistsByUser() {
  const userId = document.getElementById('spotifyUserId').value.trim();
  if (!userId) {
    alert('Please enter a Spotify User ID');
    return;
  }
  loadSpotifyPlaylists(userId);
}

function clearSpotifyUserId() {
  document.getElementById('spotifyUserId').value = '';
  loadSpotifyPlaylists();
}

function importPlaylistFromSpotify(playlistId, playlistName) {
  const urlField = document.getElementById('spotifyUrl');
  const nameField = document.getElementById('playlistName');
  
  if (urlField) urlField.value = `https://open.spotify.com/playlist/${playlistId}`;
  if (nameField) nameField.value = playlistName;
  
  if (urlField) {
    urlField.scrollIntoView({ behavior: 'smooth', block: 'center' });
    urlField.focus();
    urlField.select();
  }
}

// ===============================
// SPOTIFY IMPORT
// ===============================

async function importPlaylist(event) {
  event.preventDefault();
  
  const spotifyUrl = document.getElementById('spotifyUrl')?.value.trim() || '';
  const playlistName = document.getElementById('playlistName')?.value.trim() || '';
  const playlistDescription = document.getElementById('playlistDescription')?.value.trim() || '';
  const statusEl = document.getElementById('importStatus');
  
  if (!spotifyUrl || !playlistName) {
    alert('Please fill in all required fields');
    return;
  }
  
  if (statusEl) {
    statusEl.textContent = 'Importing...';
    statusEl.classList.remove('text-danger');
    statusEl.classList.add('text-secondary');
  }
  
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
    displayImportResults(data);
    if (statusEl) {
      statusEl.textContent = '✓ Import complete!';
      statusEl.classList.remove('text-secondary', 'text-danger');
      statusEl.classList.add('text-success');
    }
    
    if (data.missing_tracks && data.missing_tracks.length > 0) {
      missingTracksForSearch = data.missing_tracks;
    }
  } catch (error) {
    console.error('Error:', error);
    if (statusEl) {
      statusEl.textContent = `✗ ${error.message}`;
      statusEl.classList.remove('text-secondary', 'text-success');
      statusEl.classList.add('text-danger');
    }
    
    const errorSection = document.getElementById('errorSection');
    const errorMessage = document.getElementById('errorMessage');
    if (errorSection && errorMessage) {
      errorSection.style.display = 'block';
      errorMessage.textContent = error.message;
    }
    const resultsSection = document.getElementById('resultsSection');
    if (resultsSection) resultsSection.style.display = 'none';
  }
}

function displayImportResults(data) {
  const matchedCount = data.matched_tracks ? data.matched_tracks.length : 0;
  const missingCount = data.missing_tracks ? data.missing_tracks.length : 0;
  const totalCount = matchedCount + missingCount;
  const coverage = totalCount > 0 ? Math.round((matchedCount / totalCount) * 100) : 0;
  
  const matchedCountEl = document.getElementById('matchedCount');
  const missingCountEl = document.getElementById('missingCount');
  const totalCountEl = document.getElementById('totalCount');
  const coverageEl = document.getElementById('coverage');
  
  if (matchedCountEl) matchedCountEl.textContent = matchedCount;
  if (missingCountEl) missingCountEl.textContent = missingCount;
  if (totalCountEl) totalCountEl.textContent = totalCount;
  if (coverageEl) coverageEl.textContent = coverage + '%';
  
  const matchedContainer = document.getElementById('matchedTracksContainer');
  if (matchedContainer) {
    if (matchedCount > 0) {
      matchedContainer.innerHTML = data.matched_tracks.map((track, idx) => `
        <div class="track-row">
          <div class="track-info">
            <div class="track-title">${escapeHtml(track.title)}</div>
            <div class="track-artist">${escapeHtml(track.artist)}</div>
            <div class="track-album">${escapeHtml(track.album)}</div>
          </div>
          <div class="track-actions">
            <span class="badge bg-success">✓ Found</span>
          </div>
        </div>
      `).join('');
    } else {
      matchedContainer.innerHTML = '<p class="text-secondary text-center py-5">No matched tracks found</p>';
    }
  }
  
  const missingContainer = document.getElementById('missingTracksContainer');
  if (missingContainer) {
    if (missingCount > 0) {
      missingContainer.innerHTML = data.missing_tracks.map((track, idx) => `
        <div class="track-row">
          <div class="track-info">
            <div class="track-title">${escapeHtml(track.title)}</div>
            <div class="track-artist">${escapeHtml(track.artist)}</div>
            <div class="track-album">${escapeHtml(track.album || 'Unknown Album')}</div>
          </div>
          <div class="track-actions">
            <span class="badge bg-warning">✗ Missing</span>
          </div>
        </div>
      `).join('');
    } else {
      missingContainer.innerHTML = '<p class="text-success text-center py-5">All tracks found in library!</p>';
    }
  }
  
  const resultsSection = document.getElementById('resultsSection');
  if (resultsSection) resultsSection.style.display = 'block';
  
  const errorSection = document.getElementById('errorSection');
  if (errorSection) errorSection.style.display = 'none';
}

async function createPlaylist() {
  if (!currentImportData) return;
  
  const createBtn = document.getElementById('createPlaylistBtn');
  if (!createBtn) return;
  
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
    
    alert(`✓ Playlist "${currentImportData.playlist_name}" created successfully!`);
    createBtn.innerHTML = '<i class="bi bi-check-circle"></i> Playlist Created!';
    createBtn.classList.remove('btn-primary');
    createBtn.classList.add('btn-success');
  } catch (error) {
    alert(`✗ Error: ${error.message}`);
    createBtn.disabled = false;
    createBtn.innerHTML = originalText;
  }
}

// ===============================
// LAST.FM RECOMMENDATIONS
// ===============================

async function loadLastfmRecommendations() {
  const recType = document.getElementById('lfmRecType')?.value || 'tracks';
  
  try {
    const lfmRecommendationsEmpty = document.getElementById('lfmRecommendationsEmpty');
    const lfmRecommendationsResults = document.getElementById('lfmRecommendationsResults');
    
    if (lfmRecommendationsEmpty) lfmRecommendationsEmpty.style.display = 'none';
    if (lfmRecommendationsResults) lfmRecommendationsResults.style.display = 'block';
    
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
    
    document.getElementById('lfmTotalCount').textContent = data.total_recommendations || 0;
    document.getElementById('lfmMatchedCount').textContent = data.matched || 0;
    document.getElementById('lfmMissingCount').textContent = data.missing || 0;
    
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
    const lfmRecommendationsResults = document.getElementById('lfmRecommendationsResults');
    const lfmRecommendationsEmpty = document.getElementById('lfmRecommendationsEmpty');
    if (lfmRecommendationsResults) lfmRecommendationsResults.style.display = 'none';
    if (lfmRecommendationsEmpty) lfmRecommendationsEmpty.style.display = 'block';
  }
}

async function createPlaylistFromLastfm() {
  if (!lfmRecommendationsData || !lfmRecommendationsData.matched_tracks || lfmRecommendationsData.matched_tracks.length === 0) {
    alert('No matched tracks to create playlist from');
    return;
  }
  
  const recType = document.getElementById('lfmRecType')?.value || 'tracks';
  const playlistName = prompt('Enter playlist name:', `Last.fm ${recType === 'tracks' ? 'Top Tracks' : recType === 'artists' ? 'Top Artists' : 'Top Albums'}`);
  
  if (!playlistName) return;
  
  try {
    const currentUser = document.getElementById('customPlaylistUser')?.value || 'admin';
    
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
    
    alert('✓ Playlist created successfully!');
  } catch (error) {
    console.error('Error creating playlist:', error);
    alert('✗ Error: ' + error.message);
  }
}

// ===============================
// LISTENBRAINZ RECOMMENDATIONS
// ===============================

async function loadListenBrainzRecommendations() {
  const recType = document.getElementById('lbRecType')?.value || 'weekly_jams';
  
  try {
    const lbRecommendationsEmpty = document.getElementById('lbRecommendationsEmpty');
    const lbRecommendationsResults = document.getElementById('lbRecommendationsResults');
    
    if (lbRecommendationsEmpty) lbRecommendationsEmpty.style.display = 'none';
    if (lbRecommendationsResults) lbRecommendationsResults.style.display = 'block';
    
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
    
    document.getElementById('lbTotalCount').textContent = data.total_recommendations || 0;
    document.getElementById('lbMatchedCount').textContent = data.matched || 0;
    document.getElementById('lbMissingCount').textContent = data.missing || 0;
    
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
    const lbRecommendationsResults = document.getElementById('lbRecommendationsResults');
    const lbRecommendationsEmpty = document.getElementById('lbRecommendationsEmpty');
    if (lbRecommendationsResults) lbRecommendationsResults.style.display = 'none';
    if (lbRecommendationsEmpty) lbRecommendationsEmpty.style.display = 'block';
  }
}

async function createPlaylistFromListenBrainz() {
  if (!lbRecommendationsData || !lbRecommendationsData.matched_tracks || lbRecommendationsData.matched_tracks.length === 0) {
    alert('No matched tracks to create playlist from');
    return;
  }
  
  const recType = document.getElementById('lbRecType')?.value || 'weekly_jams';
  const playlistName = prompt('Enter playlist name:', `ListenBrainz ${recType.replace(/_/g, ' ')}`);
  
  if (!playlistName) return;
  
  try {
    const currentUser = document.getElementById('customPlaylistUser')?.value || 'admin';
    
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
    } else {
      alert('Error creating playlist: ' + (result.error || 'Unknown error'));
    }
  } catch (error) {
    console.error('Error creating playlist:', error);
    alert('Error: ' + error.message);
  }
}
