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
