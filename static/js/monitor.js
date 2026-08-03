/**
 * Monitor page JavaScript — CSV import, queue management, upcoming releases, folder groups.
 * Loaded separately from the esbuild bundle because it is page-specific.
 * Uses shared utilities from downloads.js (fetchJsonOrThrow, escapeHtml).
 */

// ===== CSV Inline Import (reusable from monitor.html) =====
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

      var totalAdded = 0, totalSkipped = 0, totalFailed = 0;
      var processedCount = 0;

      csvSetProgress(2, 'Adding to queue…',
        'Sending ' + items.length + ' track' + (items.length !== 1 ? 's' : '') + ' to the download queue.');

      for (var i = 0; i < items.length; i += BATCH_SIZE) {
        var batch = items.slice(i, i + BATCH_SIZE);
        var batchAdded = 0, batchSkipped = 0, batchFailed = 0;
        try {
          var res = await fetch('/api/queue/add-batch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              items: batch,
              import_group: nameSlug,
              import_type: 'playlist',
              source: 'soulseek',
            }),
          });
          var data = await res.json();
          if (data && (data.added !== undefined || data.failed !== undefined)) {
            batchAdded   = data.added   || 0;
            batchSkipped = data.skipped || 0;
            batchFailed  = data.failed  || 0;
          } else if (!res.ok) {
            batchFailed = batch.length;
          }
        } catch (batchErr) {
          console.warn('Batch queue add error:', batchErr);
          batchFailed = batch.length;
        }

        totalAdded   += batchAdded;
        totalSkipped += batchSkipped;
        totalFailed  += batchFailed;
        processedCount += batch.length;

        var pct = (processedCount / items.length) * 100;
        csvSetProgress(pct, 'Adding to queue…',
          'Processed ' + processedCount + ' of ' + items.length + '  •  Added ' + totalAdded + '  •  Failed ' + totalFailed);
      }

      csvSetProgress(100, 'Done!', totalAdded + ' track' + (totalAdded !== 1 ? 's' : '') + ' added to the queue.');

      document.getElementById('csvInlineProgress').style.display = 'none';
      document.getElementById('csvInlineResult').style.display = '';

      var queueOk = totalFailed === 0;
      document.getElementById('csvInlineResultStats').innerHTML = '' +
        '<div class="col-6 col-md-4">' +
          '<div class="card text-center bg-info bg-opacity-10 border-info h-100">' +
            '<div class="card-body py-3">' +
              '<div class="h3 text-info mb-1">' + allTracks.length + '</div>' +
              '<div class="small text-secondary">Total Tracks</div>' +
            '</div>' +
          '</div>' +
        '</div>' +
        '<div class="col-6 col-md-4">' +
          '<div class="card text-center bg-success bg-opacity-10 border-success h-100">' +
            '<div class="card-body py-3">' +
              '<div class="h3 text-success mb-1">' + totalAdded + '</div>' +
              '<div class="small text-secondary">Added to Queue</div>' +
            '</div>' +
          '</div>' +
        '</div>' +
        '<div class="col-6 col-md-4">' +
          '<div class="card text-center bg-secondary bg-opacity-10 border-secondary h-100">' +
            '<div class="card-body py-3">' +
              '<div class="h3 text-secondary mb-1">' + totalSkipped + '</div>' +
              '<div class="small text-secondary">Already Queued</div>' +
            '</div>' +
          '</div>' +
        '</div>' +
        '<div class="col-12">' +
          '<div class="alert alert-' + (queueOk ? 'success' : 'warning') + ' mb-0 py-2">' +
            '<i class="bi bi-' + (queueOk ? 'check-circle-fill' : 'exclamation-triangle-fill') + ' me-2"></i>' +
            (queueOk
              ? '<strong>' + totalAdded + '</strong> track' + (totalAdded !== 1 ? 's' : '') + ' queued. ' +
                (totalSkipped > 0 ? '<span class="text-secondary">(' + totalSkipped + ' already in queue)</span>' : '') +
                ' Matching &amp; tagging can now be done in the queue.'
              : 'Queued ' + totalAdded + ', skipped ' + totalSkipped + ', failed ' + totalFailed + '.'
            ) +
          '</div>' +
        '</div>';

      if (allTracks.length) {
        document.getElementById('csvInlineQueuedSection').style.display = '';
        document.getElementById('csvInlineQueuedList').innerHTML = allTracks.map(function (t) {
          var esc = function (v) { return escapeHtml(String(v ?? '')); };
          return '<div style="padding:0.4rem 0;border-bottom:1px solid #495057;font-size:0.9rem;">' +
            '<span class="fw-semibold">' + esc(t.title) + '</span>' +
            '<span class="text-secondary ms-2">' + esc(t.artist) + '</span>' +
          '</div>';
        }).join('');
      }
    } catch (err) {
      console.error('CSV import error:', err);
      csvSetProgress(0, 'Error', err.message);
      document.getElementById('csvInlineProgress').style.display = 'none';
      document.getElementById('csvInlineForm').style.display = '';
      var alertDiv = document.createElement('div');
      alertDiv.className = 'alert alert-danger mt-3 mb-0';
      alertDiv.innerHTML = '<i class="bi bi-exclamation-triangle-fill me-2"></i><strong>Error:</strong> ' + escapeHtml(err.message);
      var formBody = document.getElementById('csvInlineForm');
      var existing = formBody.querySelector('.alert');
      if (existing) existing.remove();
      formBody.appendChild(alertDiv);
      setTimeout(function () { alertDiv.remove(); }, 8000);
    }
  };
})();

// ===== MONITOR-SPECIFIC FUNCTIONS =====

let queueLogPaused = false;
let searchLogPaused = false;

// ===== Folder Groups =====
async function loadFolderGroups(options) {
  options = options || {};
  var section = document.getElementById('folderGroupsSection');
  var list = document.getElementById('folderGroupsList');
  var badge = document.getElementById('folderGroupsBadge');
  if (!section || !list) return;
  if (options.keepVisibleOnEmpty !== false) section.style.display = 'block';

  var groups = [];
  try {
    var data = await fetchJsonOrThrow('/api/downloads/grouped-folders');
    if (data && data.success) groups = data.folder_groups || [];
  } catch (error) {
    console.error('Error loading folder groups, falling back to queue items:', error);
  }

  // The grouped-folders endpoint only covers MusicBrainz-managed releases.
  // Fall back to the real download_queue so items added via the search /
  // download flows still appear in the Download Queue section.
  if (groups.length === 0) {
    try {
      var qd = await fetchJsonOrThrow('/api/downloads/queue?limit=200');
      var qItems = (qd && qd.queue) || [];
      if (qItems.length > 0) {
        if (badge) badge.textContent = qItems.length + ' items';
        list.innerHTML = '<div class="list-group list-group-flush">' +
          qItems.map(function(item) {
            var st = item.status || 'queued';
            var badgeCls = st === 'failed' ? 'danger' : (st === 'downloading' ? 'warning' : 'info');
            return '<div class="list-group-item"><div class="d-flex justify-content-between align-items-center">' +
              '<div><strong>' + escapeHtml(item.title || 'Unknown') + '</strong>' +
              (item.artist ? '<br><small class="text-muted">' + escapeHtml(item.artist) + (item.album ? ' - ' + escapeHtml(item.album) : '') + '</small>' : '') +
              '</div><span class="badge bg-' + badgeCls + '">' + escapeHtml(st) + '</span></div></div>';
          }).join('') + '</div>';
        updateQueuePageControls(qItems.length, qItems.length);
        return;
      }
    } catch (error) {
      console.error('Error loading queue fallback:', error);
    }
    if (options.keepVisibleOnEmpty !== false) {
      if (badge) badge.textContent = '0 items';
      list.innerHTML = '<div class="alert alert-info m-3"><i class="bi bi-info-circle"></i> No items in queue right now.</div>';
    } else {
      section.style.display = 'none';
    }
    return;
  }

  section.style.display = 'block';
  if (badge) badge.textContent = groups.length + ' items';
  // Simple render: show folder names
  list.innerHTML = '<div class="list-group list-group-flush">' +
    groups.map(function(g) {
      var name = g.folder_name || g.folder_path || g.name || 'Unknown';
      var artist = g.artist || '';
      var album = g.album || '';
      var trackCount = g.track_count || (g.tracks ? g.tracks.length : 0);
      return '<div class="list-group-item"><div class="d-flex justify-content-between"><div><strong>' + escapeHtml(name) + '</strong>' +
        (artist ? '<br><small class="text-muted">' + escapeHtml(artist) + (album ? ' - ' + escapeHtml(album) : '') + '</small>' : '') +
        '</div><span class="badge bg-info">' + trackCount + ' tracks</span></div></div>';
    }).join('') + '</div>';
  updateQueuePageControls(groups.length, groups.length);
}

// ===== Upcoming Releases =====
async function checkForUpdatesMonitor() {
  localStorage.setItem('upcomingReleasesLastChecked', Date.now().toString());
  await refreshUpcomingReleasesMonitor();
}

async function refreshUpcomingReleasesMonitor() {
  var container = document.getElementById('upcomingReleasesMonitor');
  if (!container) return;
  var filterCollection = document.getElementById('upcomingFilterCollectionMonitor')?.checked || false;
  if (window.upcomingReleasesRequestController) {
    try { window.upcomingReleasesRequestController.abort(); } catch (_) {}
  }
  window.upcomingReleasesRequestController = new AbortController();
  var rc = window.upcomingReleasesRequestController;
  container.innerHTML = '<div class="text-center py-4"><div class="spinner-border text-primary spinner-border-sm" role="status"></div><p class="mt-2 small mb-0">Loading upcoming releases...</p></div>';
  try {
    var data = await fetchJsonOrThrow('/api/upcoming-releases?collection=' + filterCollection + '&include_queue=true', { signal: rc.signal }, 20000);
    if (!Array.isArray(data.releases) || data.releases.length === 0) {
      container.innerHTML = '<div class="text-center py-4"><p class="text-muted mb-0">No upcoming releases found.</p></div>';
      return;
    }
    var releases = data.releases.filter(function(r) { return !r.album_in_collection; });
    if (filterCollection && releases.length === 0) {
      container.innerHTML = '<div class="text-center py-4"><p class="text-muted mb-0"><i class="bi bi-check-circle"></i> All upcoming releases from your collection are accounted for.</p></div>';
      return;
    }
    var grouped = {};
    releases.forEach(function(r) {
      var m = (r.release_date || 'Unknown Date').substring(0, 7);
      if (!grouped[m]) grouped[m] = [];
      grouped[m].push(r);
    });
    var sortedMonths = Object.keys(grouped).sort();
    var html = '<div class="accordion" id="upcomingReleaseAccordionMonitor">';
    sortedMonths.forEach(function(month, idx) {
      var monthReleases = grouped[month];
      var monthLabel = new Date(month + '-01').toLocaleDateString('en-US', { year: 'numeric', month: 'long' });
      html += '<div class="accordion-item"><h2 class="accordion-header"><button class="accordion-button collapsed" type="button" data-bs-toggle="collapse" data-bs-target="#ucm' + idx + '"><strong>' + escapeHtml(monthLabel) + '</strong><span class="badge bg-primary ms-2">' + monthReleases.length + '</span></button></h2><div id="ucm' + idx + '" class="accordion-collapse collapse"><div class="accordion-body p-0"><div class="table-responsive"><table class="table table-hover table-striped table-dark table-sm mb-0"><thead><tr><th>Artist</th><th>Album</th><th>Date</th><th>MBID</th><th>Action</th></tr></thead><tbody>';
      monthReleases.forEach(function(release) {
        var artistEnc = encodeInlineArg(release.artist_name || '');
        var albumEnc = encodeInlineArg(release.album_name || '');
        html += '<tr><td>' + escapeHtml(release.artist_name || '') + '</td><td>' + escapeHtml(release.album_name || '') + (release.in_queue ? ' <span class="badge bg-info text-dark ms-1">In Queue</span>' : '') + '</td><td><small>' + escapeHtml(release.release_date || 'TBA') + '</small></td><td>' + (release.release_group_mbid ? '<code class="small">' + escapeHtml(release.release_group_mbid.slice(0, 8)) + '...</code>' : '<span class="text-muted small">unmatched</span>') + '</td><td><button type="button" class="btn btn-sm btn-outline-info" onclick="searchUpcomingReleaseFromEncoded(\'' + artistEnc + '\', \'' + albumEnc + '\', ' + (Number(release.id) || 0) + ')"><i class="bi bi-search"></i> Search / Download</button></td></tr>';
      });
      html += '</tbody></table></div></div></div></div>';
    });
    html += '</div>';
    container.innerHTML = html;
  } catch (error) {
    if (error?.name === 'AbortError') return;
    container.innerHTML = '<div class="alert alert-danger mb-0"><i class="bi bi-exclamation-triangle"></i> <strong>Error loading releases:</strong> ' + escapeHtml(error.message) + '</div>';
  } finally {
    if (window.upcomingReleasesRequestController === rc) window.upcomingReleasesRequestController = null;
  }
}

async function scrapeUpcomingReleasesMonitor() {
  var statusEl = document.getElementById('upcomingStatusMonitor');
  var statusText = document.getElementById('upcomingStatusTextMonitor');
  var errorEl = document.getElementById('upcomingErrorMonitor');
  if (!statusEl || !statusText || !errorEl) return;
  statusEl.style.display = 'block';
  errorEl.style.display = 'none';
  statusText.textContent = 'Scraping Wikipedia for upcoming releases...';
  try {
    var data = await fetchJsonOrThrow('/api/upcoming-releases/scrape', { method: 'POST', headers: { 'Content-Type': 'application/json' } }, 120000);
    statusText.textContent = '✓ ' + data.message;
    setTimeout(function() { statusEl.style.display = 'none'; refreshUpcomingReleasesMonitor(); }, 1500);
  } catch (error) {
    statusEl.style.display = 'none';
    errorEl.style.display = 'block';
    errorEl.innerHTML = '<i class="bi bi-exclamation-triangle"></i> <strong>Error scraping Wikipedia:</strong> ' + escapeHtml(error.message);
  }
}

async function clearUpcomingReleasesMonitor() {
  if (!confirm('Are you sure you want to clear all upcoming releases from the database?')) return;
  var statusEl = document.getElementById('upcomingStatusMonitor');
  var statusText = document.getElementById('upcomingStatusTextMonitor');
  var errorEl = document.getElementById('upcomingErrorMonitor');
  var container = document.getElementById('upcomingReleasesMonitor');
  if (!statusEl || !statusText || !errorEl || !container) return;
  statusEl.style.display = 'block';
  errorEl.style.display = 'none';
  statusText.textContent = 'Clearing database...';
  try {
    var data = await fetchJsonOrThrow('/api/upcoming-releases/clear', { method: 'POST', headers: { 'Content-Type': 'application/json' } }, 15000);
    statusText.textContent = '✓ ' + data.message;
    setTimeout(function() {
      statusEl.style.display = 'none';
      container.innerHTML = '<div class="text-center py-4"><p class="text-muted mb-0">Database cleared.</p></div>';
    }, 1500);
  } catch (error) {
    statusEl.style.display = 'none';
    errorEl.style.display = 'block';
    errorEl.innerHTML = '<i class="bi bi-exclamation-triangle"></i> <strong>Error clearing database:</strong> ' + escapeHtml(error.message);
  }
}

async function autoMatchAllUpcomingReleasesMonitor(btn) {
  var origHtml = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span> Matching...';
  var matched = 0, failed = 0;
  try {
    var filterCollection = document.getElementById('upcomingFilterCollectionMonitor')?.checked || false;
    var data = await fetchJsonOrThrow('/api/upcoming-releases?collection=' + filterCollection + '&include_queue=true', {}, 20000);
    var unmatched = (data.releases || []).filter(function(r) { return !r.release_group_mbid; });
    if (unmatched.length === 0) {
      btn.innerHTML = '<i class="bi bi-check-circle"></i> All matched';
      setTimeout(function() { btn.disabled = false; btn.innerHTML = origHtml; }, 2000);
      return;
    }
    for (var i = 0; i < unmatched.length; i++) {
      try {
        var resp = await fetch('/api/upcoming-releases/' + unmatched[i].id + '/match', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
        var d = await resp.json();
        if (resp.ok && d.success) matched++; else failed++;
      } catch (_) { failed++; }
    }
    await refreshUpcomingReleasesMonitor();
    btn.innerHTML = '<i class="bi bi-check-circle"></i> ' + matched + ' matched' + (failed ? ', ' + failed + ' failed' : '');
    setTimeout(function() { btn.disabled = false; btn.innerHTML = origHtml; }, 3000);
  } catch (err) {
    btn.disabled = false;
    btn.innerHTML = origHtml;
  }
}

async function matchUpcomingReleaseMonitor(releaseId) {
  if (!releaseId) return;
  try {
    var resp = await fetch('/api/upcoming-releases/' + releaseId + '/match', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    var data = await resp.json();
    if (!resp.ok || !data.success) throw new Error(data.error || 'Match failed');
    await refreshUpcomingReleasesMonitor();
  } catch (error) {
    alert('Error matching: ' + error.message);
  }
}

function searchUpcomingReleaseFromEncoded(artistEnc, albumEnc, releaseId) {
  var artist = decodeInlineArg(artistEnc, '');
  var album = decodeInlineArg(albumEnc, '');
  if (!artist || !album) { alert('Missing artist/album info.'); return; }
  var fn = window.searchMusicBrainzRelease || function(){};
  fn(null, artist, album, releaseId);
}

// ===== Discovery =====
async function discoverFiles(clickEvent) {
  try {
    var button = clickEvent?.currentTarget;
    if (!button) { alert('Could not determine button context.'); return; }
    button.disabled = true;
    button.innerHTML = '<i class="bi bi-hourglass-split"></i> Scanning...';
    document.getElementById('scanLogSection').style.display = 'block';
    document.getElementById('scanLogContent').style.display = 'block';
    var logEl = document.getElementById('scanLogText');
    logEl.textContent = '[SCAN STARTED] Initializing file discovery...\n';
    var progressInterval = setInterval(async function() {
      try {
        var pData = await fetch('/api/downloads/scan-progress').then(function(r) { return r.json(); });
        if (pData.scanning) {
          document.getElementById('scanLogStats').textContent = (pData.files_found || 0) + ' files found';
        }
      } catch (e) {}
    }, 500);
    var data = await fetchJsonOrThrow('/api/downloads/discover', { method: 'POST', headers: { 'Content-Type': 'application/json' } });
    clearInterval(progressInterval);
    button.disabled = false;
    button.innerHTML = '<i class="bi bi-search"></i> Discover Files';
    if (data.success) {
      var stats = data.stats;
      logEl.textContent += '\n[SCAN COMPLETE]\nTotal files scanned: ' + stats.scanned + '\nFiles added to queue: ' + stats.queued + '\nAlready in queue: ' + stats.already_in_queue + '\nAlready in library: ' + stats.already_in_library + '\n';
      document.getElementById('discoveryMessage').textContent = 'Scanned ' + stats.scanned + ' files. Added ' + stats.queued + ' to queue.';
      document.getElementById('discoveryResults').style.display = 'block';
      document.getElementById('scanLogStats').textContent = stats.scanned + ' files scanned';
      var qs = window.loadQueueStatus || function(){};
      await qs();
      var fg = window.loadFolderGroups || function(){};
      await fg();
      setTimeout(function() { document.getElementById('discoveryResults').style.display = 'none'; }, 10000);
    } else {
      logEl.textContent += '\n[ERROR]\n' + (data.error || 'Failed');
    }
  } catch (error) {
    console.error('Error discovering files:', error);
    var btn2 = clickEvent?.currentTarget;
    if (btn2) { btn2.disabled = false; btn2.innerHTML = '<i class="bi bi-search"></i> Discover Files'; }
  }
}

function toggleScanLog() {
  var content = document.getElementById('scanLogContent');
  var icon = document.getElementById('scanLogIcon');
  if (!content || !icon) return;
  if (content.style.display === 'none') {
    content.style.display = 'block';
    icon.classList.remove('bi-plus'); icon.classList.add('bi-dash');
  } else {
    content.style.display = 'none';
    icon.classList.remove('bi-dash'); icon.classList.add('bi-plus');
  }
}

async function processAlbums(clickEvent) {
  try {
    var button = clickEvent?.currentTarget;
    if (!button) { alert('Could not determine button context.'); return; }
    button.disabled = true;
    button.innerHTML = '<i class="bi bi-hourglass-split"></i> Processing...';
    var data = await fetchJsonOrThrow('/api/downloads/process-albums', { method: 'POST', headers: { 'Content-Type': 'application/json' } });
    button.disabled = false;
    button.innerHTML = '<i class="bi bi-collection"></i> Process Albums';
    if (data.success) {
      var stats = data.stats;
      document.getElementById('discoveryMessage').textContent = 'Checked ' + stats.checked + ' albums. ' + stats.processed + ' processed.';
      document.getElementById('discoveryResults').style.display = 'block';
      var qs = window.loadQueueStatus || function(){};
      await qs();
      setTimeout(function() { document.getElementById('discoveryResults').style.display = 'none'; }, 10000);
    }
  } catch (error) {
    var btn2 = clickEvent?.currentTarget;
    if (btn2) { btn2.disabled = false; btn2.innerHTML = '<i class="bi bi-collection"></i> Process Albums'; }
  }
}

// ===== Logging =====
async function loadQueueLog() {
  if (queueLogPaused) return;
  var logEl = document.getElementById('queueActivityLog');
  if (!logEl) return;
  try {
    var data = await fetchJsonOrThrow('/api/queue/events?limit=100');
    var events = Array.isArray(data.events) ? data.events : [];
    var lines = events.slice().reverse().map(function(event) {
      var ts = event.created_at || event.timestamp || null;
      var timeLabel = ts ? new Date(ts).toLocaleTimeString([], { hour12: false }) : '--:--:--';
      return '[' + timeLabel + '] ' + (event.event_type || 'info').toUpperCase() + ' ' + (event.message || '');
    });
    logEl.textContent = lines.length ? lines.join('\n') : 'No queue events yet.';
    logEl.scrollTop = logEl.scrollHeight;
  } catch (error) {
    console.error('Error loading queue log:', error);
  }
}

function toggleQueueLogPause() {
  queueLogPaused = !queueLogPaused;
  var btn = document.getElementById('queueLogPauseBtn');
  if (!btn) return;
  btn.innerHTML = queueLogPaused ? '<i class="bi bi-play-fill"></i> Resume' : '<i class="bi bi-pause-fill"></i> Pause';
  if (!queueLogPaused) loadQueueLog();
}

async function loadSearchLog() {
  if (searchLogPaused) return;
  var logEl = document.getElementById('soulseekSearchLog');
  if (!logEl) return;
  try {
    var data = await fetchJsonOrThrow('/api/queue/search-events?limit=100');
    var events = Array.isArray(data.events) ? data.events : [];
    var chunks = [];
    events.slice().reverse().forEach(function(event) {
      var ts = event.timestamp ? new Date(event.timestamp) : null;
      var timeLabel = ts ? ts.toLocaleTimeString([], { hour12: false }) : '--:--:--';
      var type = (event.search_type || 'unknown').toUpperCase();
      chunks.push('[' + type + '] [' + timeLabel + '] Query: "' + (event.query || '') + '"  |  ' + (event.artist || '') + ' - ' + (event.title || ''));
      chunks.push('    Results: ' + (event.result_count ?? 0) + '  Duration: ' + (event.duration_seconds != null ? event.duration_seconds + 's' : 'n/a'));
    });
    logEl.textContent = chunks.length ? chunks.join('\n') : 'No Soulseek search events yet.';
    logEl.scrollTop = logEl.scrollHeight;
  } catch (error) {
    console.error('Error loading search log:', error);
  }
}

function toggleSearchLogPause() {
  searchLogPaused = !searchLogPaused;
  var btn = document.getElementById('searchLogPauseBtn');
  if (!btn) return;
  btn.innerHTML = searchLogPaused ? '<i class="bi bi-play-fill"></i> Resume' : '<i class="bi bi-pause-fill"></i> Pause';
  if (!searchLogPaused) loadSearchLog();
}

async function migrateExistingQueueItems(buttonEl) {
  if (!confirm('Migrate existing queue rows to the grouped setup now?')) return;
  var button = buttonEl || null;
  var previousHtml = button ? button.innerHTML : '';
  try {
    if (button) { button.disabled = true; button.innerHTML = '<i class="bi bi-hourglass-split"></i> Migrating...'; }
    var data = await fetchJsonOrThrow('/api/queue/migrate-existing', { method: 'POST' });
    if (data.success) {
      alert('✅ Queue migration complete\n\nRows updated: ' + (data.updated_rows || 0));
      var fg = window.loadFolderGroups || function(){};
      await fg({ forceRender: true });
    } else {
      alert('❌ Error: ' + (data.error || 'Migration failed'));
    }
  } catch (e) { alert('❌ Network error: ' + e.message); }
  if (button) { button.disabled = false; button.innerHTML = previousHtml; }
}

async function executeFolderMerge() { alert('Folder merge functionality coming soon.'); }
async function showDestinationFolderPicker() { alert('Folder picker coming soon.'); }

// ===== Merged Queue Releases Renderer (replaces loadCurrentQueueReleasesForFolder + loadCurrentQueueReleases) =====
async function renderCurrentQueueReleases(containerId, artist, album, actionHtmlBuilder) {
  const container = document.getElementById(containerId);
  if (!container) return;

  try {
    const apiUrl = `/api/queue/matched-releases?artist=${encodeURIComponent(artist || '')}&album=${encodeURIComponent(album || '')}&limit=80`;
    const data = await fetchJsonOrThrow(apiUrl);
    const releases = data.releases || [];

    if (!releases.length) {
      container.innerHTML = '<div class="alert alert-info mb-0 small">No matched releases found. Use <strong>Search Online</strong> below.</div>';
      return;
    }

    const rows = releases.map(rel => {
      const relArtist = rel.artist || '';
      const relAlbum = rel.album || '';
      const relMbid = rel.mbid || '';
      const relYear = rel.year || '';
      const relCount = rel.track_count || 0;

      return `
        <tr>
          <td>${escapeHtml(relArtist)}</td>
          <td>${escapeHtml(relAlbum)}</td>
          <td>${escapeHtml(String(relYear))}</td>
          <td>${escapeHtml(String(relCount))}</td>
          <td><small class="text-info">${escapeHtml(relMbid)}</small></td>
          <td>${actionHtmlBuilder(relArtist, relAlbum, relMbid)}</td>
        </tr>
      `;
    }).join('');

    container.innerHTML = `
      <div class="table-responsive">
        <table class="table table-sm table-dark table-striped mb-0">
          <thead>
            <tr>
              <th>Artist</th><th>Album</th><th>Year</th><th>Tracks</th><th>MBID</th><th>Action</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    `;
  } catch (error) {
    console.error(`Error loading queue releases for ${containerId}:`, error);
    container.innerHTML = '<div class="alert alert-warning mb-0 small">Could not load current queue releases.</div>';
  }
}

// ===== Fixed queueMissingTracks (Promise.all for concurrent requests) =====
async function queueMissingTracks(tracksJson, artist) {
  try {
    const tracks = JSON.parse(tracksJson);
    const missingTracks = tracks.filter(t => t.status === 'missing');

    if (missingTracks.length === 0) {
      alert('No missing tracks to queue');
      return;
    }

    // Use Promise.all to run requests concurrently instead of one-by-one
    const queuePromises = missingTracks.map(track =>
      fetch('/api/downloads/queue', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: track.title || '',
          artist: artist,
          album: track.release_title || '',
          source: 'musicbrainz',
          status: 'queued'
        })
      }).then(r => r.ok)
    );

    const results = await Promise.all(queuePromises);
    const queuedCount = results.filter(Boolean).length;

    if (queuedCount > 0) {
      alert(`Successfully queued ${queuedCount}/${missingTracks.length} missing tracks`);
      setTimeout(loadQueueStatus, 500);

      const modal = bootstrap.Modal.getInstance(document.getElementById('releaseTracklistModal'));
      if (modal) modal.hide();
    } else {
      alert('Failed to queue any tracks');
    }
  } catch (error) {
    console.error('Error queuing missing tracks:', error);
    alert(`Error: ${error.message}`);
  }
}

// ===== Page-load init =====
// Load the queue summary cards and the Download Queue section as soon as the
// page is ready. downloads.js (loaded after this file) defines loadQueueStatus.
document.addEventListener('DOMContentLoaded', function() {
  if (typeof window.loadQueueStatus === 'function') {
    window.loadQueueStatus();
  }
  if (typeof window.loadFolderGroups === 'function') {
    window.loadFolderGroups({ forceRender: true, keepVisibleOnEmpty: true });
  }
  if (typeof window.loadQueueLog === 'function') {
    window.loadQueueLog();
  }
  if (typeof window.loadSearchLog === 'function') {
    window.loadSearchLog();
  }
});
