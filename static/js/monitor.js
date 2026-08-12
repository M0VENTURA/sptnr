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

// ===== Queue Groups (legacy MusicBrainz folder groups were removed — the
// queue groups match the folders and support expand/stop/delete) =====
async function loadFolderGroups(options) {
  options = options || {};
  var section = document.getElementById('folderGroupsSection');
  var list = document.getElementById('folderGroupsList');
  var badge = document.getElementById('folderGroupsBadge');
  if (!section || !list) return;
  if (options.keepVisibleOnEmpty !== false) section.style.display = 'block';

  var qItems = [];
  try {
    var qd = await fetchJsonOrThrow('/api/downloads/queue?limit=200');
    qItems = (qd && qd.queue) || [];
  } catch (error) {
    console.error('Error loading queue items:', error);
  }

  var html = '';
  if (qItems.length > 0) {
    html += '<h6 class="px-3 pt-3 mb-0 small text-muted text-uppercase">Queue Items</h6>';
    // Group tracks into album groups — items added together from a
    // MusicBrainz release share an ``import_group`` (mbid_<release_id>);
    // other batch-added tracks fall back to artist+album grouping.
    var queueGroups = buildMonitorQueueGroups(qItems);
    window.__monitorQueueGroups = queueGroups;
    html += '<div class="list-group list-group-flush">' + queueGroups.map(function(group, index) {
      if (group.items.length === 1) {
        return renderMonitorQueueItemRow(group.items[0]);
      }
      return renderMonitorQueueGroupRow(group, index);
    }).join('') + '</div>';
  }

  var total = qItems.length;
  if (total === 0) {
    if (options.keepVisibleOnEmpty !== false) {
      if (badge) badge.textContent = '0 items';
      list.innerHTML = '<div class="alert alert-info m-3"><i class="bi bi-info-circle"></i> No items in queue right now.</div>';
    } else {
      section.style.display = 'none';
    }
    await renderUnmatchedFolders(options);
    return;
  }

  section.style.display = 'block';
  if (badge) badge.textContent = total + ' items';
  list.innerHTML = html;
  attachMonitorQueueGroupToggles(list);
  restoreQueueGroupExpansion(list);
  updateQueuePageControls(total, total);
  await renderUnmatchedFolders(options);
}

// ===== Unmatched Folders (folders in /downloads not tied to a release) =====
async function renderUnmatchedFolders(options) {
  var section = document.getElementById('unmatchedFoldersSection');
  var list = document.getElementById('unmatchedFoldersList');
  var badge = document.getElementById('unmatchedFoldersBadge');
  if (!section || !list) return;
  var folders = [];
  try {
    var data = await fetchJsonOrThrow('/api/downloads/unmatched-folders');
    if (data && data.success) folders = data.folders || [];
  } catch (error) {
    console.error('Error loading unmatched folders:', error);
  }
  if (folders.length === 0) {
    section.style.display = 'none';
    list.innerHTML = '';
    if (badge) badge.textContent = '0 items';
    return;
  }
  section.style.display = 'block';
  if (badge) badge.textContent = folders.length + ' item' + (folders.length !== 1 ? 's' : '');

  // Empty folders (0 audio, not matched) collapse into one group so they
  // don't dominate the list; a one-click header action deletes them all.
  var emptyFolders = folders.filter(function(f) {
    return f.status !== 'matched' && !(f.audio_count > 0);
  });
  var contentFolders = folders.filter(function(f) {
    return f.status === 'matched' || (f.audio_count || 0) > 0;
  });
  window.__emptyUnmatchedFolders = emptyFolders;

  var html = '';
  if (contentFolders.length > 0) {
    html += '<div class="list-group list-group-flush">' + contentFolders.map(function(f) {
      var matched = f.status === 'matched';
      var files = Array.isArray(f.files) ? f.files : [];
      var fileHtml = '';
      if (files.length > 0) {
        fileHtml = '<ul class="list-unstyled mb-0 mt-1" style="max-height:160px;overflow-y:auto;">' +
          files.slice(0, 30).map(function(file) {
            var base = file && file.name ? file.name : String(file || '').split(/[\\/]/).pop();
            return '<li class="text-muted" style="font-size:0.75rem;"><i class="bi bi-file-earmark-music me-1"></i>' +
              '<span class="text-truncate d-inline-block" style="max-width:calc(100% - 1.4rem);vertical-align:bottom;" title="' + escapeHtml(base) + '">' + escapeHtml(cleanQueueFileName(base)) + '</span></li>';
          }).join('') +
          (files.length > 30 ? '<li class="fst-italic small text-muted">+' + (files.length - 30) + ' more</li>' : '') +
          '</ul>';
      }
      // Slim pill sits inline with the folder title; actions stay side-by-side
      // in a fixed right column (Match green / Delete red outline).
      var statusBadge = matched
        ? '<span class="badge status-pill status-complete ms-2" title="All files were imported to the library">Matched ✓</span>'
        : '<span class="badge status-pill status-queued ms-2">' + (f.audio_count || 0) + ' audio</span>';
      return '<div class="list-group-item">' +
        '<div class="d-flex justify-content-between align-items-start gap-2">' +
        '<div class="flex-grow-1" style="min-width:0;">' +
        '<div class="text-truncate"><i class="bi bi-folder2 me-1 text-muted"></i><strong>' + escapeHtml(f.display_name || f.name || 'Unknown') + '</strong>' + statusBadge + '</div>' + fileHtml +
        '</div>' +
        '<div class="d-flex flex-shrink-0 gap-1">' +
        '<button class="btn btn-sm btn-outline-primary py-0 unmatched-match-btn" data-path="' + escapeHtml(f.name) + '" title="Copy into the library as a MusicBrainz release (uses the naming convention from Settings)"><i class="bi bi-link-45deg"></i> Match</button>' +
        '<button class="btn btn-sm btn-outline-danger py-0 unmatched-delete-btn" data-path="' + escapeHtml(f.name) + '" title="Delete this folder from the downloads folder"><i class="bi bi-trash3"></i> Delete</button>' +
        '</div></div></div>';
    }).join('') + '</div>';
  }

  if (emptyFolders.length > 0) {
    html += '<div class="list-group list-group-flush"><div class="list-group-item">' +
      '<div class="d-flex justify-content-between align-items-center gap-2">' +
      '<button class="btn btn-sm btn-link text-muted p-0 text-decoration-none collapsed" type="button" data-bs-toggle="collapse" data-bs-target="#emptyUnmatchedFoldersList" aria-expanded="false"><i class="bi bi-chevron-right me-1"></i><i class="bi bi-folder-x me-1"></i>Empty Folders (' + emptyFolders.length + ')</button>' +
      '<div class="d-flex align-items-center gap-2">' +
      '<small class="text-muted">' + emptyFolders.length + ' empty</small>' +
      '<button class="btn btn-sm btn-outline-danger py-0" onclick="deleteAllEmptyFolders(this)" title="Delete all folders with 0 audio files"><i class="bi bi-trash3"></i> Prune All</button>' +
      '</div>' +
      '</div>' +
      '<div id="emptyUnmatchedFoldersList" class="collapse"><ul class="list-unstyled mb-0 mt-1" style="max-height:200px;overflow-y:auto;">' +
      emptyFolders.map(function(f) {
        return '<li style="font-size:0.75rem;" class="text-muted"><i class="bi bi-folder me-1"></i>' + escapeHtml(f.display_name || f.name || 'Unknown') + '</li>';
      }).join('') +
      '</ul></div></div></div>';
  }

  list.innerHTML = html;
  attachUnmatchedFolderActions(list);
}

// Strip the trailing numeric hash suffixes raw downloads carry
// (e.g. "13 - Headlines (Friendship Never Ends)_639218336827019346.flac").
function cleanQueueFileName(name) {
  return String(name || '').replace(/_\d{12,}(\.\w+)$/i, '$1');
}

// Delete every empty (0 audio) unmatched folder in one click.
async function deleteAllEmptyFolders(btn) {
  var folders = window.__emptyUnmatchedFolders || [];
  if (!folders.length) return;
  if (!confirm('Delete ' + folders.length + ' empty folder(s) (no audio files) from the downloads folder? This cannot be undone.')) return;
  btn.disabled = true;
  var deleted = 0, failed = 0;
  for (var i = 0; i < folders.length; i++) {
    try {
      var data = await fetchJsonOrThrow('/api/downloads/folder/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ folder_path: folders[i].name }),
      });
      if (data.success) deleted++; else failed++;
    } catch (error) {
      failed++;
    }
  }
  showToastMsg('Deleted ' + deleted + ' empty folder(s).' + (failed ? ' ⚠️ ' + failed + ' failed.' : ''), !!failed);
  btn.disabled = false;
  if (typeof window.loadFolderGroups === 'function') {
    window.loadFolderGroups({ forceRender: true, keepVisibleOnEmpty: true });
  }
}

function attachUnmatchedFolderActions(listEl) {
  if (!listEl) return;
  listEl.querySelectorAll('.unmatched-match-btn').forEach(function(btn) {
    if (btn.getAttribute('data-bound')) return;
    btn.setAttribute('data-bound', '1');
    btn.addEventListener('click', async function() {
      var folderPath = btn.getAttribute('data-path');
      var mbId = prompt('Paste a MusicBrainz release or release-group URL/ID for this folder (e.g. https://musicbrainz.org/release/xxxx or release-group/xxxx):');
      if (!mbId) return;
      btn.disabled = true;
      try {
        var data = await fetchJsonOrThrow('/api/downloads/folder/match', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ folder_path: folderPath, mb_id: mbId.trim() }),
        });
        if (data.success) {
          showToastMsg('Matched ' + data.moved + ' file(s) to "' + (data.album_artist || '') + ' - ' + (data.album || '') + '" (' + (data.year || '') + '). Folder deleted from downloads.', false);
        } else {
          showToastMsg(data.error || 'Match failed', true);
        }
      } catch (error) {
        showToastMsg('Network error: ' + error.message, true);
      } finally {
        btn.disabled = false;
        if (typeof window.loadFolderGroups === 'function') {
          window.loadFolderGroups({ forceRender: true, keepVisibleOnEmpty: true });
        }
      }
    });
  });
  listEl.querySelectorAll('.unmatched-delete-btn').forEach(function(btn) {
    if (btn.getAttribute('data-bound')) return;
    btn.setAttribute('data-bound', '1');
    btn.addEventListener('click', async function() {
      var folderPath = btn.getAttribute('data-path');
      if (!confirm('Delete this folder and ALL its files from the downloads folder?\n\n' + folderPath)) return;
      btn.disabled = true;
      try {
        var data = await fetchJsonOrThrow('/api/downloads/folder/delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ folder_path: folderPath }),
        });
        showToastMsg(data.success ? 'Folder deleted.' : (data.error || 'Delete failed'), !data.success);
      } catch (error) {
        showToastMsg('Network error: ' + error.message, true);
      } finally {
        btn.disabled = false;
        if (typeof window.loadFolderGroups === 'function') {
          window.loadFolderGroups({ forceRender: true, keepVisibleOnEmpty: true });
        }
      }
    });
  });
}

// ===== Queue group expansion state (shared with downloads.js) =====
// The auto-refresh poll re-renders the queue list every few seconds; without
// persisted expansion state every re-render collapses any opened folder.
window.__expandedQueueGroups = window.__expandedQueueGroups || new Set();

function sanitizeQueueGroupKey(key) {
  return String(key || 'x').replace(/[^a-zA-Z0-9_-]/g, '_');
}
window.__sanitizeQueueGroupKey = window.__sanitizeQueueGroupKey || sanitizeQueueGroupKey;

function restoreQueueGroupExpansion(listEl) {
  if (!listEl) return;
  var expanded = window.__expandedQueueGroups;
  var found = new Set();
  listEl.querySelectorAll('.queue-group-body').forEach(function(body) {
    found.add(body.id);
    if (!expanded.has(body.id)) return;
    body.style.display = 'block';
    var item = body.closest('.list-group-item');
    var btn = item && item.querySelector('.queue-group-toggle');
    var chevron = btn && btn.querySelector('.queue-group-chevron');
    if (chevron) chevron.classList.add('rotated');
  });
  // Drop ids that no longer exist so the set never grows unbounded.
  expanded.forEach(function(id) { if (!found.has(id)) expanded.delete(id); });
}
window.__restoreQueueGroupExpansion = window.__restoreQueueGroupExpansion || restoreQueueGroupExpansion;

function attachMonitorQueueGroupToggles(listEl) {
  if (!listEl) return;
  listEl.querySelectorAll('.queue-group-toggle').forEach(function(btn) {
    // Downloads.js (loaded after this file) also binds these — guard against
    // double-binding by checking a marker.
    if (btn.getAttribute('data-toggle-bound')) return;
    btn.setAttribute('data-toggle-bound', '1');
    btn.addEventListener('click', function() {
      var body = document.getElementById(btn.getAttribute('data-target'));
      var chevron = btn.querySelector('.queue-group-chevron');
      if (!body) return;
      var show = body.style.display === 'none' || body.style.display === '';
      body.style.display = show ? 'block' : 'none';
      if (chevron) chevron.classList.toggle('rotated', show);
      if (show) {
        window.__expandedQueueGroups.add(body.id);
      } else {
        window.__expandedQueueGroups.delete(body.id);
      }
    });
  });
}

// ===== Album-folder grouping for the Download Queue section =====
// Legacy parity: albums added from a MusicBrainz release share an
// ``import_group`` (mbid_<release_id>); other batch-added tracks group by
// artist+album. Albums render as an expandable folder with each song a queue
// item below, matching old_system's downloads page layout.

function buildMonitorQueueGroups(items) {
  var groups = [];
  var map = {};
  items.forEach(function(item) {
    var album = (item.album || '').trim();
    var artist = (item.album_artist || item.artist || '').trim();
    var title = (item.title || '').trim();

    var key, label, sublabel;
    if (item.import_group) {
      key = 'grp_' + String(item.import_group);
      label = album || String(item.import_group);
      sublabel = artist;
    } else if (album && album !== title) {
      key = 'alb_' + artist.toLowerCase() + '|' + album.toLowerCase();
      label = album;
      sublabel = artist;
    } else {
      key = 'solo_' + item.id;
      label = null;
      sublabel = null;
    }

    if (!map[key]) {
      map[key] = { key: key, label: label, sublabel: sublabel, items: [] };
      groups.push(map[key]);
    }
    map[key].items.push(item);
  });
  return groups;
}

function renderMonitorQueueItemRow(item) {
  // Skip ghost parent/search-tracking rows (no title AND no artist) — they
  // have nothing actionable for the user and only clutter the queue view.
  if (!(item.title || '').trim() && !(item.artist || '').trim() && !(item.album || '').trim()) return '';
  var st = item.status || 'queued';

  // Action buttons grouped flush-right; role depends on the status.
  var actions = '';
  if (st === 'failed' && typeof window.retryQueueItem === 'function') {
    actions += '<button class="btn btn-outline-warning" title="Retry now" onclick="retryQueueItem(' + item.id + ')"><i class="bi bi-arrow-clockwise"></i></button>';
  } else if (st === 'downloading' && typeof window.cancelQueueItem === 'function') {
    actions += '<button class="btn btn-outline-secondary" title="Cancel download" onclick="cancelQueueItem(' + item.id + ')"><i class="bi bi-stop-circle"></i></button>';
  }
  if (typeof window.deleteQueueItem === 'function') {
    actions += '<button class="btn btn-outline-danger" title="Remove from queue" onclick="deleteQueueItem(' + item.id + ', false)"><i class="bi bi-trash"></i></button>';
  }
  var actionsHtml = actions
    ? '<div class="btn-group btn-group-sm flex-shrink-0">' + actions + '</div>'
    : '';

  return '<div class="list-group-item queue-item"><div class="d-flex justify-content-between align-items-center gap-2">' +
    '<div style="min-width:0;">' +
      '<div class="text-truncate"><strong>' + escapeHtml(item.title || 'Unknown') + '</strong>' +
      (item.artist ? ' <small class="text-muted">' + escapeHtml(item.artist) + (item.album ? ' - ' + escapeHtml(item.album) : '') + '</small>' : '') +
      '</div>' +
      (st === 'failed' && item.failure_reason ? '<div class="small text-danger text-truncate"><i class="bi bi-exclamation-triangle"></i> ' + escapeHtml(item.failure_reason) + '</div>' : '') +
    '</div>' +
    '<div class="d-flex align-items-center gap-2 flex-shrink-0">' + queueStatusPill(st, item) + actionsHtml + '</div>' +
    '</div></div>';
}

// Slim outlined pill badges (failed / downloading% / queued / …).
function queueStatusPill(st, item) {
  var map = {
    failed: ['failed', '🔴 Failed'],
    downloading: ['downloading', '🔵 Downloading'],
    queued: ['queued', '🟡 Queued'],
    complete: ['complete', '🟢 Complete'],
    moving: ['complete', '🟢 Moving'],
    pending_release: ['pending', '🔘 Pending']
  };
  var cfg = map[st] || ['queued', '🟡 ' + escapeHtml(st)];
  var label = cfg[1];
  if (st === 'downloading' && item.progress !== undefined && item.progress !== null && item.progress !== '') {
    var pct = Math.max(0, Math.min(100, Math.round(Number(item.progress) || 0)));
    label = '🔵 ' + pct + '%';
  }
  return '<span class="badge status-pill status-' + cfg[0] + '">' + label + '</span>';
}

// Remove every track in a folder group at once (per-item API calls).
async function deleteQueueGroup(index) {
  var groups = window.__monitorQueueGroups || [];
  var group = groups[index];
  if (!group || !group.items.length) return;
  if (!confirm('Remove ' + group.items.length + ' track(s) from the queue for "' + group.label + '"? This cannot be undone.')) return;
  for (var i = 0; i < group.items.length; i++) {
    try {
      if (typeof window.deleteQueueItem === 'function') await window.deleteQueueItem(group.items[i].id, false);
    } catch (err) {
      console.warn('Group delete failed for item', group.items[i].id, err);
    }
  }
  if (typeof window.loadFolderGroups === 'function') {
    window.loadFolderGroups({ forceRender: true, keepVisibleOnEmpty: true });
  }
}

function renderMonitorQueueGroupRow(group, index) {
  var bodyId = 'monQueueGroupBody_' + sanitizeQueueGroupKey(group.key);
  var items = group.items;
  var total = items.length;

  var counts = {};
  items.forEach(function(item) {
    var st = item.status || 'queued';
    counts[st] = (counts[st] || 0) + 1;
  });

  var subline = group.sublabel
    ? ' <small class="text-muted">' + escapeHtml(group.sublabel) + '</small>'
    : '';

  var children = items.map(function(item) {
    return renderMonitorQueueItemRow(item);
  }).join('');

  var groupDelete = '';
  if (typeof window.deleteQueueItem === 'function') {
    groupDelete = '<button class="btn btn-sm btn-outline-danger py-0 px-1 flex-shrink-0" title="Remove all tracks in this folder" onclick="deleteQueueGroup(' + index + ')"><i class="bi bi-trash"></i></button>';
  }

  return '<div class="list-group-item">' +
    '<div class="d-flex justify-content-between align-items-center gap-2">' +
    '<button type="button" class="btn btn-sm btn-link p-0 text-decoration-none queue-group-toggle flex-shrink-0" data-target="' + bodyId + '" title="Expand album" style="color:var(--text-secondary);">' +
      '<i class="bi bi-chevron-down queue-group-chevron"></i>' +
    '</button>' +
    '<div class="text-truncate flex-grow-1" style="min-width:0;">' +
      '<i class="bi bi-folder2-open me-1 text-muted"></i><strong>' + escapeHtml(group.label) + '</strong>' + subline +
    '</div>' +
    queueGroupSummaryPill(counts, total) +
    groupDelete +
    '</div>' +
    '<div id="' + bodyId + '" class="queue-group-body ps-3 border-start ms-2 mt-2" style="display:none;">' + children + '</div>' +
    '</div>';
}

// One compact pill on the folder header: the most urgent status wins
// (failed → downloading → moving → complete → queued).
function queueGroupSummaryPill(counts, total) {
  var labels = {
    failed: ['failed', '🔴 ' + (counts.failed || 0) + ' Failed'],
    downloading: ['downloading', '🔵 ' + (counts.downloading || 0) + ' Downloading'],
    moving: ['complete', '🟢 ' + (counts.moving || 0) + ' Moving'],
    complete: ['complete', '🟢 ' + (counts.complete || 0) + ' Complete']
  };
  var order = ['failed', 'downloading', 'moving', 'complete'];
  for (var i = 0; i < order.length; i++) {
    var key = order[i];
    if (counts[key]) {
      return '<span class="badge status-pill status-' + labels[key][0] + '">' + labels[key][1] + '</span>';
    }
  }
  return '<span class="badge status-pill status-queued">🟡 ' + total + ' Queued</span>';
}

// ===== Upcoming Releases (delegates to UpcomingReleasesService) =====
async function checkForUpdatesMonitor() {
  localStorage.setItem('upcomingReleasesLastChecked', Date.now().toString());
  await refreshUpcomingReleasesMonitor();
}

async function refreshUpcomingReleasesMonitor() {
  var container = document.getElementById('upcomingReleasesMonitor');
  if (!container) return;
  var filterCollection = document.getElementById('upcomingFilterCollectionMonitor')?.checked || false;
  container.innerHTML = '<div class="text-center py-4"><div class="spinner-border text-primary spinner-border-sm" role="status"></div><p class="mt-2 small mb-0">Loading upcoming releases...</p></div>';
  try {
    var data = await UpcomingReleasesService.fetchReleases({
      filter: filterCollection ? 'collection' : undefined,
      include_queue: true,
      limit: 200,
    });
    var releases = data.releases || [];
    buildUpcomingMonthFilterMonitor(releases);
    if (releases.length === 0) {
      container.innerHTML = '<div class="text-center py-4"><p class="text-muted mb-0">No upcoming releases found.</p></div>';
      return;
    }
    if (filterCollection) {
      releases = releases.filter(function (r) { return !r.album_in_collection; });
      if (releases.length === 0) {
        container.innerHTML = '<div class="text-center py-4"><p class="text-muted mb-0"><i class="bi bi-check-circle"></i> All upcoming releases from your collection are accounted for.</p></div>';
        return;
      }
    }
    var monthEl = document.getElementById('upcomingMonthFilterMonitor');
    var month = monthEl ? monthEl.value : '';
    if (month) {
      releases = releases.filter(function (r) { return (r.release_date || '').substring(0, 7) === month; });
      if (releases.length === 0) {
        container.innerHTML = '<div class="text-center py-4"><p class="text-muted mb-0">No releases in ' + escapeHtml(month) + '.</p></div>';
        return;
      }
    }
    UpcomingReleasesService.renderTable('upcomingReleasesMonitor', releases);
  } catch (error) {
    console.error('Error loading upcoming releases:', error);
    container.innerHTML = '<div class="text-center py-4"><p class="text-danger mb-2"><i class="bi bi-exclamation-triangle"></i> Error loading upcoming releases.</p><p class="text-muted small mb-0">' + escapeHtml(error.message || 'Unknown error') + '</p><button class="btn btn-sm btn-outline-primary mt-2" onclick="refreshUpcomingReleasesMonitor()"><i class="bi bi-arrow-clockwise"></i> Retry</button></div>';
  }
}

// Build the "All Months (N)" filter dropdown from the fetched releases.
function buildUpcomingMonthFilterMonitor(releases) {
  var select = document.getElementById('upcomingMonthFilterMonitor');
  if (!select) return;
  var counts = {};
  (releases || []).forEach(function (r) {
    var m = (r.release_date || '').substring(0, 7);
    if (m) counts[m] = (counts[m] || 0) + 1;
  });
  var keys = Object.keys(counts).sort().reverse();
  var html = '<option value="">All Months (' + (releases || []).length + ')</option>';
  keys.forEach(function (m) {
    var label = new Date(m + '-01').toLocaleDateString('en-US', { year: 'numeric', month: 'long' });
    html += '<option value="' + m + '">' + label + ' (' + counts[m] + ')</option>';
  });
  select.innerHTML = html;
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
    var data = await UpcomingReleasesService.triggerScrape();
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
    var resp = await fetch('/api/upcoming-releases/clear', { method: 'POST', headers: { 'Content-Type': 'application/json' } });
    var data = await resp.json().catch(function () { return {}; });
    if (!resp.ok) throw new Error(data.error || 'Failed to clear database');
    statusText.textContent = '✓ Database cleared';
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
    var data = await UpcomingReleasesService.fetchReleases({
      filter: filterCollection ? 'collection' : undefined,
      include_queue: true,
    });
    var unmatched = (data.releases || []).filter(function(r) { return !r.release_group_mbid; });
    if (unmatched.length === 0) {
      btn.innerHTML = '<i class="bi bi-check-circle"></i> All matched';
      setTimeout(function() { btn.disabled = false; btn.innerHTML = origHtml; }, 2000);
      return;
    }
    for (var i = 0; i < unmatched.length; i++) {
      try {
        await UpcomingReleasesService.matchRelease(unmatched[i].id, null);
        matched++;
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
    await UpcomingReleasesService.matchRelease(releaseId, null);
    await refreshUpcomingReleasesMonitor();
  } catch (error) {
    alert('Error matching: ' + error.message);
  }
}

function searchUpcomingReleaseFromEncoded(artistEnc, albumEnc, releaseId) {
  var fn = window.searchMusicBrainzReleaseFromEncoded || function(){};
  fn(null, artistEnc, albumEnc);
}

// ===== Discovery =====
function showToastMsg(message, isError) {
  if (typeof window.showToast === 'function') {
    window.showToast('', message, isError ? 'error' : 'success');
    return;
  }
  if (isError) alert(message);
}

async function discoverFiles(clickEvent) {
  var button = clickEvent?.currentTarget;
  if (!button) {
    showToastMsg('Could not determine button context.', true);
    return;
  }
  button.disabled = true;
  button.innerHTML = '<i class="bi bi-hourglass-split"></i> Scanning...';
  try {
    var data = await fetchJsonOrThrow('/api/downloads/discover', { method: 'POST', headers: { 'Content-Type': 'application/json' } });
    if (data.success) {
      var stats = data.stats || {};
      var msg = 'Scanned ' + (stats.scanned || 0) + ' files · ' + (stats.queued || 0) + ' added to queue · ' +
        (stats.already_in_queue || 0) + ' already queued' +
        (stats.quality_skipped ? ' · ' + stats.quality_skipped + ' skipped (quality filter)' : '');
      var dm = document.getElementById('discoveryMessage');
      if (dm) dm.textContent = msg;
      var dr = document.getElementById('discoveryResults');
      if (dr) {
        dr.style.display = 'block';
        dr.textContent = '✅ ' + msg;
        setTimeout(function () { dr.style.display = 'none'; }, 10000);
      }
      showToastMsg(msg, false);
      var qs = window.loadQueueStatus || function () {};
      await qs();
      var fg = window.loadFolderGroups || function () {};
      await fg();
      var um = window.renderUnmatchedFolders || function () {};
      await um();
    } else {
      showToastMsg(data.error || 'Discovery failed', true);
    }
  } catch (error) {
    console.error('Error discovering files:', error);
    showToastMsg('Discovery failed: ' + error.message, true);
  } finally {
    button.disabled = false;
    button.innerHTML = '<i class="bi bi-search"></i> Scan Downloads';
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

    // Use the real queue-add endpoint. /api/downloads/queue is GET-only —
    // POSTing there returns 405 and silently added nothing.
    const response = await fetch('/api/queue/add-batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        items: missingTracks.map(track => ({
          title: track.title || '',
          artist: artist,
          album: track.release_title || '',
          source: 'musicbrainz'
        }))
      })
    });
    const result = await response.json().catch(() => ({}));
    const queuedCount = (result && typeof result.added === 'number') ? result.added : 0;

    if (queuedCount > 0) {
      alert(`Successfully queued ${queuedCount}/${missingTracks.length} missing tracks`);
      setTimeout(function() {
        if (typeof window.loadFolderGroups === 'function') {
          window.loadFolderGroups({ forceRender: true, keepVisibleOnEmpty: true });
        }
      }, 500);

      const modal = bootstrap.Modal.getInstance(document.getElementById('releaseTracklistModal'));
      if (modal) modal.hide();
    } else {
      alert('Failed to queue any tracks: ' + ((result && result.error) || 'unknown error'));
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
  // Upcoming releases: read straight from the database (no re-scrape on
  // page load — the "Update from Wikipedia" button handles that manually).
  refreshUpcomingReleasesMonitor();
});
