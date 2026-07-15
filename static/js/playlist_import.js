/**
 * Playlist / CSV import functions for the unified search page.
 * Depends on: downloads.js (for escapeHtml, fetchJsonOrThrow)
 */

let currentImportData = null;
let missingTracksForSearch = [];

const _trackPayloads = {};
let _payloadIdx = 0;
function storePayload(obj) {
  const id = _payloadIdx++;
  _trackPayloads[id] = obj;
  return id;
}

async function importPlaylistFromCSV(event) {
  event.preventDefault();
  const fileInput = document.getElementById('csvFile');
  const playlistName = document.getElementById('csvPlaylistName').value.trim();
  const playlistDescription = document.getElementById('csvPlaylistDescription').value.trim();
  const targetUser = document.getElementById('csvTargetUser').value.trim();
  const statusEl = document.getElementById('csvImportStatus');

  if (!fileInput.files.length || !playlistName) {
    alert('Please select a CSV file and enter a playlist name');
    return;
  }

  statusEl.textContent = '⏳ Importing...';
  statusEl.className = 'ms-2 text-secondary';

  const formData = new FormData();
  formData.append('file', fileInput.files[0]);
  formData.append('playlist_name', playlistName);
  formData.append('playlist_description', playlistDescription);
  formData.append('target_user', targetUser);

  try {
    const response = await fetch('/api/playlist/import/csv', { method: 'POST', body: formData });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Import failed');
    currentImportData = data;
    missingTracksForSearch = data.missing_tracks || [];
    displayResults(data);
    statusEl.textContent = '✅ Import complete!';
    statusEl.className = 'ms-2 text-success';
  } catch (error) {
    console.error('CSV import error:', error);
    statusEl.textContent = '❌ ' + error.message;
    statusEl.className = 'ms-2 text-danger';
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

  document.getElementById('matchedCount').textContent = matchedCount;
  document.getElementById('missingCount').textContent = missingCount;
  document.getElementById('totalCount').textContent = totalCount;
  document.getElementById('coverage').textContent = coverage + '%';

  const coverageCard = document.getElementById('coverageCard');
  coverageCard.classList.remove('bg-success', 'bg-warning', 'bg-danger');
  if (coverage >= 90)      coverageCard.classList.add('bg-success');
  else if (coverage >= 70) coverageCard.classList.add('bg-warning');
  else                     coverageCard.classList.add('bg-danger');

  const matchedContainer = document.getElementById('matchedTracksContainer');
  if (matchedCount > 0) {
    matchedContainer.innerHTML = data.matched_tracks.map(function (track, idx) {
      const pid = storePayload({ artist: track.artist, title: track.title, album: track.album || '' });
      return '' +
        '<div class="track-row">' +
          '<div class="track-info">' +
            '<div class="track-title">' + escapeHtml(track.title) + '</div>' +
            '<div class="track-artist">' + escapeHtml(track.artist) + '</div>' +
            '<div class="track-album">' + escapeHtml(track.album) + '</div>' +
          '</div>' +
          '<div class="track-actions">' +
            '<span class="badge bg-success">✓ Found</span>' +
          '</div>' +
        '</div>';
    }).join('');
  } else {
    matchedContainer.innerHTML = '<p class="text-secondary text-center py-5">No matched tracks found</p>';
  }

  const missingContainer = document.getElementById('missingTracksContainer');
  if (missingCount > 0) {
    missingContainer.innerHTML = data.missing_tracks.map(function (track) {
      return '' +
        '<div class="track-row">' +
          '<div class="track-info">' +
            '<div class="track-title">' + escapeHtml(track.title) + '</div>' +
            '<div class="track-artist">' + escapeHtml(track.artist) + '</div>' +
            '<div class="track-album">' + escapeHtml(track.album || 'Unknown Album') + '</div>' +
          '</div>' +
          '<div class="track-actions">' +
            '<span class="missing-track-badge">✗ Missing</span>' +
          '</div>' +
        '</div>';
    }).join('');
  } else {
    missingContainer.innerHTML = '<p class="text-success text-center py-5">All tracks found in library!</p>';
  }

  document.getElementById('resultsSection').style.display = 'block';
  document.getElementById('errorSection').style.display = 'none';
}
