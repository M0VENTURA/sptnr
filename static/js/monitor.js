/**
 * Monitor page JavaScript — CSV import, unmatched folders, and disk discovery.
 * Relies on downloads.js for all queue and search logic.
 */

// ===== CSV Inline Import =====
(function () {
  function csvSetProgress(pct, label, sub) {
    document.getElementById('csvInlineProgressBar').style.width = pct + '%';
    document.getElementById('csvInlineProgressBar').setAttribute('aria-valuenow', pct);
    document.getElementById('csvInlineProgressPct').textContent = pct + '%';
    if (label !== undefined) document.getElementById('csvInlineProgressLabel').textContent = label;
    if (sub !== undefined) document.getElementById('csvInlineProgressSub').textContent = sub;
  }

  window.csvInlineReset = function () {
    document.getElementById('csvInlineForm').style.display = '';
    document.getElementById('csvInlineProgress').style.display = 'none';
    document.getElementById('csvInlineResult').style.display = 'none';
    document.getElementById('csvInlineFormEl').reset();
  };

  window.csvInlineImport = async function (event) {
    event.preventDefault();
    const fileInput  = document.getElementById('csvInlineFile');
    const importName = document.getElementById('csvInlineName').value.trim();
    if (!fileInput.files.length || !importName) return;

    document.getElementById('csvInlineForm').style.display = 'none';
    document.getElementById('csvInlineProgress').style.display = '';
    document.getElementById('csvInlineResult').style.display = 'none';
    csvSetProgress(5, 'Parsing CSV…', 'Reading track metadata from the file.');

    try {
      const formData = new FormData();
      formData.append('file', fileInput.files[0]);
      formData.append('playlist_name', importName);
      formData.append('skip_matching', 'true');

      const parseRes  = await fetch('/api/playlist/import/csv', { method: 'POST', body: formData });
      const parseData = await parseRes.json();
      if (!parseRes.ok) throw new Error(parseData.error || 'CSV processing failed');

      const allTracks = parseData.all_tracks || [];
      if (!allTracks.length) throw new Error('No tracks found in CSV');

      const BATCH_SIZE = 10;
      const nameSlug    = importName.replace(/\s+/g, '_').substring(0, 80);
      const currentYear = String(new Date().getFullYear());
      const items = allTracks.map(function (t, idx) {
        return {
          artist:       t.artist,
          title:        t.title,
          album:        importName,
          album_artist: 'Various Artists',
          year:         currentYear,
          track_number: idx + 1,
          duration:     t.duration_s  || null,
          genres:       t.genres      || null,
          isrc:         t.isrc        || null,
        };
      });

      let totalAdded = 0, totalSkipped = 0, totalFailed = 0, processedCount = 0;
      csvSetProgress(2, 'Adding to queue…', `Sending ${items.length} track(s) to the queue.`);

      for (let i = 0; i < items.length; i += BATCH_SIZE) {
        let batch = items.slice(i, i + BATCH_SIZE);
        let batchAdded = 0, batchSkipped = 0, batchFailed = 0;
        try {
          let res = await fetch('/api/queue/add-batch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ items: batch, import_group: nameSlug, import_type: 'playlist', source: 'soulseek' }),
          });
          let data = await res.json();
          if (data && (data.added !== undefined || data.failed !== undefined)) {
            batchAdded = data.added || 0;
            batchSkipped = data.skipped || 0;
            batchFailed = data.failed || 0;
          } else if (!res.ok) {
            batchFailed = batch.length;
          }
        } catch (batchErr) {
          batchFailed = batch.length;
        }

        totalAdded += batchAdded;
        totalSkipped += batchSkipped;
        totalFailed += batchFailed;
        processedCount += batch.length;

        csvSetProgress((processedCount / items.length) * 100, 'Adding to queue…', `Processed ${processedCount} of ${items.length}`);
      }

      csvSetProgress(100, 'Done!', `${totalAdded} track(s) added to the queue.`);
      document.getElementById('csvInlineProgress').style.display = 'none';
      document.getElementById('csvInlineResult').style.display = '';

      const queueOk = totalFailed === 0;
      document.getElementById('csvInlineResultStats').innerHTML = `
        <div class="col-6 col-md-4">
          <div class="card text-center bg-info bg-opacity-10 border-info h-100">
            <div class="card-body py-3"><div class="h3 text-info mb-1">${allTracks.length}</div><div class="small text-secondary">Total Tracks</div></div>
          </div>
        </div>
        <div class="col-6 col-md-4">
          <div class="card text-center bg-success bg-opacity-10 border-success h-100">
            <div class="card-body py-3"><div class="h3 text-success mb-1">${totalAdded}</div><div class="small text-secondary">Added to Queue</div></div>
          </div>
        </div>
        <div class="col-6 col-md-4">
          <div class="card text-center bg-secondary bg-opacity-10 border-secondary h-100">
            <div class="card-body py-3"><div class="h3 text-secondary mb-1">${totalSkipped}</div><div class="small text-secondary">Already Queued</div></div>
          </div>
        </div>
      `;

    } catch (err) {
      csvSetProgress(0, 'Error', err.message);
      document.getElementById('csvInlineProgress').style.display = 'none';
      document.getElementById('csvInlineForm').style.display = '';
      alert('Error: ' + err.message);
    }
  };
})();

// ===== Unmatched Folders (Disk Scanning) =====
async function renderUnmatchedFolders(options) {
  const section = document.getElementById('unmatchedFoldersSection');
  const list = document.getElementById('unmatchedFoldersList');
  const badge = document.getElementById('unmatchedFoldersBadge');
  if (!section || !list) return;

  try {
    const data = await fetchJsonOrThrow('/api/downloads/unmatched-folders');
    const folders = data.folders || [];

    if (folders.length === 0) {
      section.style.display = 'none';
      list.innerHTML = '';
      if (badge) badge.textContent = '0 items';
      return;
    }
    section.style.display = 'block';
    if (badge) badge.textContent = `${folders.length} item(s)`;

    const emptyFolders = folders.filter(f => f.status !== 'matched' && !(f.audio_count > 0));
    const contentFolders = folders.filter(f => f.status === 'matched' || (f.audio_count || 0) > 0);
    window.__emptyUnmatchedFolders = emptyFolders;

    let html = '';
    if (contentFolders.length > 0) {
      html += '<div class="list-group list-group-flush">' + contentFolders.map(f => {
        const isAssociated = !!(f.match || f.release_mbid);
        const statusBadge = f.status === 'matched' 
            ? '<span class="badge status-pill status-complete ms-2">Matched ✓</span>' 
            : '<span class="badge status-pill status-queued ms-2">' + (f.audio_count || 0) + ' audio</span>';

        let actionsHtml = isAssociated 
            ? `<button class="btn btn-sm btn-outline-warning py-0 unmatched-change-match-btn" data-path="${escapeHtml(f.name)}" data-artist="${escapeHtml(f.artist || '')}" data-album="${escapeHtml(f.album || '')}"><i class="bi bi-arrow-repeat"></i> Change Match</button>
               <button class="btn btn-sm btn-success py-0 unmatched-confirm-btn" data-path="${escapeHtml(f.name)}" data-mbid="${escapeHtml(f.release_mbid || '')}"><i class="bi bi-check-lg"></i> Confirm Match</button>`
            : `<button class="btn btn-sm btn-outline-primary py-0 unmatched-match-btn" data-path="${escapeHtml(f.name)}" data-artist="${escapeHtml(f.artist || '')}" data-album="${escapeHtml(f.album || '')}"><i class="bi bi-search"></i> Match</button>`;
        
        actionsHtml += `<button class="btn btn-sm btn-outline-danger py-0 unmatched-delete-btn" data-path="${escapeHtml(f.name)}"><i class="bi bi-trash3"></i> Delete</button>`;

        return `
            <div class="list-group-item">
                <div class="d-flex justify-content-between align-items-start gap-2">
                    <div class="flex-grow-1" style="min-width:0;">
                        <div class="text-truncate"><i class="bi bi-folder2 me-1 text-muted"></i><strong>${escapeHtml(f.display_name || f.name)}</strong>${statusBadge}</div>
                        ${f.artist && f.album ? `<div class="text-muted small mt-1">${escapeHtml(f.artist)} — ${escapeHtml(f.album)}</div>` : ''}
                    </div>
                    <div class="d-flex flex-shrink-0 gap-1 flex-wrap justify-content-end">${actionsHtml}</div>
                </div>
            </div>`;
      }).join('') + '</div>';
    }

    if (emptyFolders.length > 0) {
      html += `
        <div class="list-group list-group-flush">
            <div class="list-group-item d-flex justify-content-between align-items-center">
                <span class="text-muted"><i class="bi bi-folder-x me-1"></i> Empty Folders (${emptyFolders.length})</span>
                <button class="btn btn-sm btn-outline-danger py-0" onclick="deleteAllEmptyFolders(this)"><i class="bi bi-trash3"></i> Prune All</button>
            </div>
        </div>`;
    }

    list.innerHTML = html;
    attachUnmatchedFolderActions(list);
  } catch (error) {
    console.error('Error loading unmatched folders:', error);
  }
}

async function deleteAllEmptyFolders(btn) {
  const folders = window.__emptyUnmatchedFolders || [];
  if (!folders.length || !confirm(`Delete ${folders.length} empty folder(s)?`)) return;
  btn.disabled = true;
  for (let f of folders) {
    try { await fetchJsonOrThrow('/api/downloads/folder/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ folder_path: f.name }) }); } catch (e) {}
  }
  btn.disabled = false;
  renderUnmatchedFolders();
}

function attachUnmatchedFolderActions(listEl) {
  if (!listEl) return;
  
  listEl.querySelectorAll('.unmatched-match-btn, .unmatched-change-match-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      openFolderMbSearch(btn.getAttribute('data-path'), false, btn.getAttribute('data-artist'), btn.getAttribute('data-album'));
    });
  });

  listEl.querySelectorAll('.unmatched-confirm-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        await fetchJsonOrThrow('/api/downloads/confirm-match', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ folder_path: btn.getAttribute('data-path'), release_mbid: btn.getAttribute('data-mbid') }),
        });
        if (typeof window.renderUnmatchedFolders === 'function') window.renderUnmatchedFolders({ forceRender: true });
        if (typeof window.loadQueueStatus === 'function') window.loadQueueStatus();
      } catch (err) { alert(err.message); btn.disabled = false; }
    });
  });

  listEl.querySelectorAll('.unmatched-delete-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (!confirm('Delete this folder?')) return;
      btn.disabled = true;
      try {
        await fetchJsonOrThrow('/api/downloads/folder/delete', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ folder_path: btn.getAttribute('data-path') }),
        });
        if (typeof window.renderUnmatchedFolders === 'function') window.renderUnmatchedFolders({ forceRender: true });
      } catch (err) { alert(err.message); btn.disabled = false; }
    });
  });
}

function openFolderMbSearch(folderPath, isChange, detectedArtist, detectedAlbum) {
  window._folderMatchTarget = { folder_path: folderPath, is_change: !!isChange };
  window._mbSearchCallback = function(selected) {
    fetch('/api/downloads/folder/associate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ folder_path: window._folderMatchTarget.folder_path, mb_id: selected.id }),
    }).then(() => {
      if (typeof window.renderUnmatchedFolders === 'function') window.renderUnmatchedFolders({ forceRender: true });
    });
  };
  window._mbSearchIncludeOwned = true;
  if (typeof window.populateMusicBrainzSearch === 'function') window.populateMusicBrainzSearch(detectedArtist, detectedAlbum, '', '');
  if (typeof window.showMusicBrainzModal === 'function') window.showMusicBrainzModal();
}

async function discoverFiles(clickEvent) {
  const btn = clickEvent?.currentTarget;
  if (btn) { btn.disabled = true; btn.innerHTML = '<i class="bi bi-hourglass-split"></i> Scanning...'; }
  try {
    await fetchJsonOrThrow('/api/downloads/discover', { method: 'POST' });
    if (typeof window.loadQueueStatus === 'function') await window.loadQueueStatus();
    if (typeof window.renderUnmatchedFolders === 'function') window.renderUnmatchedFolders();
  } catch (err) { alert(err.message); }
  if (btn) { btn.disabled = false; btn.innerHTML = '<i class="bi bi-search"></i> Scan Downloads'; }
}

async function processAlbums(clickEvent) {
  const btn = clickEvent?.currentTarget;
  if (btn) { btn.disabled = true; btn.innerHTML = '<i class="bi bi-hourglass-split"></i> Processing...'; }
  try {
    await fetchJsonOrThrow('/api/downloads/process-albums', { method: 'POST' });
    if (typeof window.loadQueueStatus === 'function') await window.loadQueueStatus();
    if (typeof window.renderUnmatchedFolders === 'function') window.renderUnmatchedFolders();
  } catch (err) { alert(err.message); }
  if (btn) { btn.disabled = false; btn.innerHTML = '<i class="bi bi-collection"></i> Process Albums'; }
}

// ===== Upcoming Releases =====
async function checkForUpdatesMonitor() {
  localStorage.setItem('upcomingReleasesLastChecked', Date.now().toString());
  await refreshUpcomingReleasesMonitor();
}

async function refreshUpcomingReleasesMonitor() {
  const container = document.getElementById('upcomingReleasesMonitor');
  if (!container) return;
  const filterCollection = document.getElementById('upcomingFilterCollectionMonitor')?.checked || false;
  container.innerHTML = '<div class="text-center py-4"><div class="spinner-border text-primary spinner-border-sm" role="status"></div><p class="mt-2 small mb-0">Loading upcoming releases...</p></div>';
  try {
    const data = await fetchJsonOrThrow(`/api/upcoming-releases?include_queue=true${filterCollection ? '&collection=true' : ''}`);
    const releases = data.releases || [];
    
    if (releases.length === 0) {
      container.innerHTML = '<div class="text-center py-4"><p class="text-muted mb-0">No upcoming releases found.</p></div>';
      return;
    }
    
    const html = `
      <div class="table-responsive">
        <table class="table table-sm table-dark table-hover mb-0">
          <thead><tr><th>Artist</th><th>Album</th><th>Date</th><th style="width: 120px;">Action</th></tr></thead>
          <tbody>
            ${releases.map(r => `
              <tr>
                <td>${escapeHtml(r.artist_name)}</td>
                <td>${escapeHtml(r.album_name)}</td>
                <td><small>${r.release_date || 'TBA'}</small></td>
                <td><button class="btn btn-sm btn-outline-primary" onclick="searchUpcomingReleaseFromEncoded('${encodeURIComponent(r.artist_name)}', '${encodeURIComponent(r.album_name)}')"><i class="bi bi-search"></i> Search</button></td>
              </tr>`).join('')}
          </tbody>
        </table>
      </div>`;
    container.innerHTML = html;
  } catch (error) {
    container.innerHTML = `<div class="text-center py-4"><p class="text-danger mb-2"><i class="bi bi-exclamation-triangle"></i> Error loading upcoming releases.</p></div>`;
  }
}

function searchUpcomingReleaseFromEncoded(artist, album) {
  if (typeof window.searchMusicBrainzRelease === 'function') {
    window.searchMusicBrainzRelease(null, decodeURIComponent(artist), decodeURIComponent(album));
  }
}

document.addEventListener('DOMContentLoaded', function() {
  if (typeof window.renderUnmatchedFolders === 'function') {
    window.renderUnmatchedFolders();
  }
  if (document.getElementById('upcomingReleasesMonitor')) {
    refreshUpcomingReleasesMonitor();
  }
});
