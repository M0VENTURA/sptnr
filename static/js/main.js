/**
 * Popularr main entry point.
 *
 * This file is the entry point for esbuild bundling.
 * All JS modules are imported here and bundled into static/dist/main.js.
 *
 * To add a new JS module:
 *   1. Create the module in static/js/
 *   2. Import it below
 *   3. Rebuild: `npm run build`
 */

// Global initialization runs after DOM is ready
document.addEventListener("DOMContentLoaded", () => {
  // Keep --navbar-height in sync with the real two-row navbar height so main
  // content padding, sticky bars and the search flyout all align.
  const syncNavbarHeight = () => {
    const nav = document.querySelector("nav.navbar.fixed-top");
    if (!nav || !nav.offsetHeight) return;
    document.documentElement.style.setProperty("--navbar-height", nav.offsetHeight + "px");
  };
  syncNavbarHeight();
  window.addEventListener("resize", syncNavbarHeight);

  // Initialize Bootstrap tooltips if Bootstrap loaded
  if (typeof bootstrap !== "undefined" && bootstrap.Tooltip) {
    const tooltipTriggerList = document.querySelectorAll('[data-bs-toggle="tooltip"]');
    [...tooltipTriggerList].map((el) => new bootstrap.Tooltip(el));
  }
});

// ==========================================================================
// Generic mobile tab engine (artist / album / track pages)
// --------------------------------------------------------------------------
// A tab bar with ``data-mobile-tabs`` gets:
//   - data-group-attr    : section attribute grouping content (default
//                          ``data-mobile-group``)
//   - data-active-class  : class toggled on the active section (default
//                          ``mobile-tab-active``)
// Sections are shown/hidden via the active class, so each page's desktop
// media query (all sections visible >= 992px) keeps working untouched.
// Active tab persists to the URL hash on user clicks only.
// ==========================================================================

function initMobileTabs(bar, options) {
  options = options || {};
  const groupAttr = options.groupAttr || 'data-mobile-group';
  const activeClass = options.activeClass || 'mobile-tab-active';
  const buttons = Array.prototype.slice.call(bar.querySelectorAll('[data-tab]'));
  if (!buttons.length) return;

  function apply(tab, persistHash) {
    buttons.forEach((b) => {
      const isActive = b.getAttribute('data-tab') === tab;
      b.classList.toggle('active', isActive);
      b.setAttribute('aria-selected', isActive ? 'true' : 'false');
    });
    document.querySelectorAll('[' + groupAttr + ']').forEach((section) => {
      section.classList.toggle(activeClass, section.getAttribute(groupAttr) === tab);
    });
    if (persistHash && window.history && history.replaceState) {
      history.replaceState(null, '', '#' + tab);
    }
  }

  bar.addEventListener('click', (e) => {
    const btn = e.target.closest ? e.target.closest('[data-tab]') : null;
    if (!btn) return;
    e.preventDefault();
    apply(btn.getAttribute('data-tab'), true);
  });

  // Initial state: URL hash wins, else the pre-marked active button, else
  // the first tab.  The hash is never written at load time so desktop
  // scrollspy anchors keep working.
  const hash = window.location.hash ? window.location.hash.replace('#', '') : '';
  const initial =
    hash && buttons.some((b) => b.getAttribute('data-tab') === hash)
      ? hash
      : (bar.querySelector('.active') || buttons[0]).getAttribute('data-tab');
  apply(initial, false);
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll('[data-mobile-tabs]').forEach((bar) => {
    initMobileTabs(bar, {
      groupAttr: bar.getAttribute('data-group-attr') || 'data-mobile-group',
      activeClass: bar.getAttribute('data-active-class') || 'mobile-tab-active',
    });
  });
});

// ==========================================================================
// Live scan progress toast (SSE)
// --------------------------------------------------------------------------
// Subscribes to /api/scan-progress/stream on every page and shows a compact
// bottom toast while a scan runs (current artist / album + progress bar).
// EventSource reconnects automatically; the toast reappears per scan and is
// dismissible.  Falls back silently to the dashboard's polling UI when SSE
// is unavailable.
// ==========================================================================

function showScanProgressToast(data) {
  let toast = document.getElementById('scanProgressToast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'scanProgressToast';
    toast.className = 'scan-progress-toast d-none';
    toast.innerHTML =
      '<div class="scan-progress-toast-body">' +
        '<div class="scan-progress-toast-title">' +
          '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>' +
          '<strong id="scanProgressTitle">Scanning…</strong>' +
        '</div>' +
        '<div id="scanProgressText" class="small text-muted"></div>' +
        '<div class="progress mt-2" style="height:4px;"><div id="scanProgressBar" class="progress-bar bg-success" style="width:0%;"></div></div>' +
      '</div>' +
      '<button type="button" class="scan-progress-toast-close" aria-label="Dismiss" title="Dismiss">' +
        '<i class="bi bi-x-lg"></i>' +
      '</button>';
    toast.querySelector('.scan-progress-toast-close').addEventListener('click', function () {
      toast.classList.add('d-none');
    });
    document.body.appendChild(toast);
  }

  const active = (data.active_scans || []).filter((s) => s && s.is_running);
  if (!active.length) {
    toast.classList.add('d-none');
    return;
  }
  const scan = active[0];
  const pct = Math.min(Number(scan.percent_complete) || 0, 100);
  const typeLabel = String(scan.scan_type || 'scan')
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
  toast.querySelector('#scanProgressTitle').textContent = typeLabel + ' — ' + pct + '%';
  let detail = '';
  if (scan.current_artist) detail += scan.current_artist;
  if (scan.current_album) detail += (detail ? ' · ' : '') + scan.current_album;
  if (scan.current_stage && String(scan.current_stage) !== 'complete') {
    detail += (detail ? ' — ' : '') + String(scan.current_stage);
  }
  toast.querySelector('#scanProgressText').textContent = detail || scan.message || 'Working…';
  toast.querySelector('#scanProgressBar').style.width = pct + '%';
  toast.classList.remove('d-none');
}

document.addEventListener("DOMContentLoaded", () => {
  if (typeof EventSource === 'undefined') return;
  try {
    const source = new EventSource('/api/scan-progress/stream');
    source.addEventListener('message', (e) => {
      if (!e.data) return;
      try {
        const data = JSON.parse(e.data);
        showScanProgressToast(data);
        updateGlobalScanBar(data);
      } catch (_) {
        /* malformed frame — ignore */
      }
    });
  } catch (_) {
    /* SSE unavailable — dashboard polling still covers progress */
  }
});

// ==========================================================================
// Global log viewer + sticky scan bar (every page)
// --------------------------------------------------------------------------
// The sticky bottom bar and the fullscreen log modal live in base.html.
// The dashboard's own polling (dashboard.js) drives the bar there; on every
// other page the SSE stream updates it.  The modal reads per-source log
// files and can export the last hour with one tap.
// ==========================================================================

const LOG_SOURCE_FILES = {
  scanner: 'unified_scan.log',
  soulseek: 'search.log',
  navidrome: 'info.log',
  system: 'error.log',
};

let activeLogSource = 'scanner';
let logPaused = false;
let logModalVisible = false;

function openUnifiedLogModal() {
  const modalEl = document.getElementById('unifiedLogModal');
  if (!modalEl) return;
  bootstrap.Modal.getOrCreateInstance(modalEl).show();
}

function toggleLogPause() {
  logPaused = !logPaused;
  const pauseBtn = document.getElementById('pauseLogBtn');
  if (pauseBtn) pauseBtn.innerHTML = logPaused ? '<i class="bi bi-play"></i> <span class="d-none d-sm-inline">Resume</span>' : '<i class="bi bi-pause"></i> <span class="d-none d-sm-inline">Pause</span>';
}

function switchLogSource(source) {
  activeLogSource = LOG_SOURCE_FILES[source] ? source : 'scanner';
  document.querySelectorAll('.log-source-tab').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.source === activeLogSource);
  });
  updateUnifiedLog();
}

function downloadActiveLogLastHour() {
  const link = document.createElement('a');
  link.href = '/api/logs/export?source=' + encodeURIComponent(activeLogSource) + '&hours=1';
  link.download = activeLogSource + '_log_last_1hr.log';
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
}

function updateUnifiedLog() {
  if (logPaused) return;
  const logEl = document.getElementById('unifiedLog');
  if (!logEl) return;
  // Self-healing poll: a hung request must not freeze the log panel forever.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 8000);
  fetch('/api/log-file?name=' + encodeURIComponent(LOG_SOURCE_FILES[activeLogSource]) + '&lines=300', { signal: controller.signal })
    .then((r) => r.json())
    .then((data) => {
      if (Array.isArray(data && data.lines)) {
        logEl.textContent = data.lines.join('\n');
        if (logModalVisible) logEl.scrollTop = logEl.scrollHeight;
      }
    })
    .catch(() => {})
    .finally(() => clearTimeout(timer));
}

// The dashboard's own polling (dashboard.js) owns the bar there.
function updateGlobalScanBar(data) {
  if (document.getElementById('dashboardFlags')) return;
  const bar = document.getElementById('scanStatusBar');
  const line = document.getElementById('scanStatusLine');
  const icon = document.getElementById('scanStatusIcon');
  if (!bar || !line || !icon) return;
  const active = (data && (data.active_scans || [])) || [];
  const scan = active[0];
  if (!scan || !scan.is_running) return;
  const pct = Math.min(scan.progress || 0, 100);
  const name = String(scan.scan_type || 'scan').replace(/_/g, ' ');
  line.textContent = `${name} — ${pct}%` + (scan.current_item ? ` · ${scan.current_item}` : '');
  icon.className = 'scan-status-active';
  icon.innerHTML = '<i class="bi bi-activity"></i>';
}

document.addEventListener("DOMContentLoaded", () => {
  const logModalEl = document.getElementById('unifiedLogModal');
  if (logModalEl) {
    logModalEl.addEventListener('shown.bs.modal', () => { logModalVisible = true; updateUnifiedLog(); });
    logModalEl.addEventListener('hidden.bs.modal', () => { logModalVisible = false; });
  }
  setInterval(() => { if (logModalVisible) updateUnifiedLog(); }, 5000);
});

// ==========================================================================
// Sticky save bar (dirty-state tracking)
// --------------------------------------------------------------------------
// Forms opt in with ``data-sticky-save``.  Any input/change marks the form
// dirty and slides up a fixed bottom bar (Discard / Save Metadata).  JS-driven
// edits (chip inputs, quick-fill buttons) call ``markFormDirty(formId)``.
// ==========================================================================

window.markFormDirty = function (formId) {
  const form = document.getElementById(formId);
  if (form && form._setDirty) form._setDirty(true);
};

function initStickySaveBar(form) {
  let bar = document.getElementById('stickySaveBar');
  if (!bar) {
    bar = document.createElement('div');
    bar.id = 'stickySaveBar';
    bar.className = 'sticky-save-bar d-none';
    bar.innerHTML =
      '<div class="sticky-save-bar-inner">' +
        '<span class="sticky-save-bar-msg text-warning small"><i class="bi bi-exclamation-circle me-1"></i>You have unsaved metadata changes</span>' +
        '<div class="d-flex gap-2">' +
          '<button type="button" class="btn btn-outline-secondary btn-sm" data-discard><i class="bi bi-x-lg me-1"></i>Discard</button>' +
          '<button type="button" class="btn btn-success btn-sm" data-save><i class="bi bi-check-lg me-1"></i>Save Metadata</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(bar);
  }

  const setDirty = (dirty) => {
    if (dirty === form._dirty) return;
    form._dirty = dirty;
    bar.classList.toggle('d-none', !dirty);
  };
  form._setDirty = setDirty;

  form.addEventListener('input', () => setDirty(true));
  form.addEventListener('change', () => setDirty(true));
  form.addEventListener('submit', () => setDirty(false));

  bar.querySelector('[data-discard]').addEventListener('click', () => {
    form.reset();
    setDirty(false);
  });
  bar.querySelector('[data-save]').addEventListener('click', () => {
    if (typeof form.requestSubmit === 'function') form.requestSubmit();
    else form.submit();
  });
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll('form[data-sticky-save]').forEach(initStickySaveBar);
});

// ==========================================================================
// Global functions (moved from inline base.html <script> block)
// These must be on window because they are called by inline onclick/onsubmit
// handlers in templates (e.g. navSearch, openSlideOver).
// ==========================================================================

window.openSlideOver = function(url, title) {
  var el = document.getElementById('detailSlideOver');
  var contentEl = document.getElementById('slideOverContent');
  var titleEl = document.getElementById('slideOverTitle');
  titleEl.textContent = title || 'Loading...';
  contentEl.innerHTML = '<div class="d-flex justify-content-center py-5"><div class="spinner-border text-primary"></div></div>';
  var bsOffcanvas = new bootstrap.Offcanvas(el);
  bsOffcanvas.show();
  fetch(url)
    .then(function(r) { return r.text(); })
    .then(function(html) { contentEl.innerHTML = html; })
    .catch(function(err) { contentEl.innerHTML = '<div class="alert alert-danger m-3">Error loading details: ' + err.message + '</div>'; });
};

// ==========================================================================
// MusicBrainz Release Picker (slide-over)
// --------------------------------------------------------------------------
// Opens the /api/musicbrainz/release-picker flyout for a release GROUP so
// the user can choose the exact physical release (15-track CD, 18-track
// deluxe, 5-track promo, ...) before anything is queued.  Queueing a chosen
// version posts its concrete release MBID, which resolve_release_id accepts
// as-is (no re-resolution to the biggest official release).
// ==========================================================================

window._releasePickerOnQueued = null;

window.openReleasePicker = function(releaseGroupId, title, artist, onQueued) {
  if (!releaseGroupId) {
    if (typeof showToast === 'function') showToast('Error', 'Missing Release Group ID', 'error');
    else alert('❌ Error: Missing Release Group ID');
    return;
  }
  window._releasePickerOnQueued = typeof onQueued === 'function' ? onQueued : null;
  var url = '/api/musicbrainz/release-picker?rg_id=' + encodeURIComponent(releaseGroupId) +
    '&artist=' + encodeURIComponent(artist || '') +
    '&album=' + encodeURIComponent(title || '');
  window.openSlideOver(url, 'Select Version: ' + (title || ''));
};

window.queueSpecificRelease = async function(releaseId, releaseTitle, artist) {
  if (!releaseId) return;
  try {
    var resp = await fetch('/api/musicbrainz/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        release_id: releaseId,
        release_title: releaseTitle,
        artist: artist,
        method: 'slskd',
        queue_items_only: true
      })
    });
    var data = await resp.json().catch(function () { return {}; });

    if (resp.ok && (data.success || data.tracking_id)) {
      if (typeof showToast === 'function') {
        showToast('Success', '📥 Queued ' + releaseTitle + ' (' + artist + ')', 'success');
      } else {
        alert('✅ Queued version "' + releaseTitle + '" to download queue!');
      }
      var cb = window._releasePickerOnQueued;
      window._releasePickerOnQueued = null;
      if (typeof cb === 'function') { try { cb(releaseId); } catch (e) { /* ignore */ } }

      var slideOverEl = document.getElementById('detailSlideOver');
      if (slideOverEl && window.bootstrap) {
        var inst = bootstrap.Offcanvas.getInstance(slideOverEl);
        if (inst) inst.hide();
      }
    } else {
      alert('❌ Error queuing release: ' + (data.error || 'Unknown error'));
    }
  } catch (e) {
    alert('❌ Network error while queuing release: ' + e.message);
  }
};

window.toggleReleaseTracklistPreview = function(releaseId) {
  var el = document.getElementById('preview-rel-' + releaseId);
  if (!el) return;
  if (el.classList.contains('d-none')) {
    el.classList.remove('d-none');
    el.innerHTML = '<div class="text-center py-2"><span class="spinner-border spinner-border-sm" role="status"></span></div>';
    fetch('/api/musicbrainz/release-picker?release_id=' + encodeURIComponent(releaseId))
      .then(function (r) { return r.text(); })
      .then(function (html) { el.innerHTML = html; })
      .catch(function () { el.innerHTML = '<div class="text-danger small">Failed to load tracklist.</div>'; });
  } else {
    el.classList.add('d-none');
  }
};

window.formatFilePath = function(filePath) {
  if (!filePath) return 'Not available';
  var musicMatch = filePath.match(/[\/\\]music[\/\\](.+)$/i);
  if (musicMatch) return '\\Music\\' + musicMatch[1].replace(/\//g, '\\');
  return filePath;
};

window.lookupMetadata = async function(type, identifier) {
  var modal = document.getElementById('metadataModal');
  var body = document.getElementById('metadataModalBody');
  var bsModal = new bootstrap.Modal(modal);
  body.innerHTML = '<div class="spinner-border" role="status"><span class="visually-hidden">Loading...</span></div>';
  bsModal.show();
  try {
    var response = await fetch('/api/metadata?type=' + encodeURIComponent(type) + '&id=' + encodeURIComponent(identifier));
    if (!response.ok) throw new Error('Metadata lookup failed');
    var data = await response.json();
    var fieldCategories = {
      'Track Info': ['title', 'artist', 'album', 'track', 'track_id', 'date', 'length'],
      'Tags': ['genre', 'comment', 'bpm'],
      'IDs': ['mbid', 'musicbrainz_id', 'isrc', 'ean', 'spotify_uri'],
      'Scores': ['spotify_score', 'lastfm_ratio', 'final_score'],
      'File Info': ['file_path'],
      'Notes': ['note']
    };
    var html = '';
    var processedKeys = new Set();
    for (var _i = 0, _a = Object.entries(fieldCategories); _i < _a.length; _i++) {
      var _b = _a[_i], category = _b[0], fields = _b[1];
      var categoryHtml = '';
      for (var _c = 0; _c < fields.length; _c++) {
        var field = fields[_c];
        if (field in data && !processedKeys.has(field)) {
          processedKeys.add(field);
          var value = data[field];
          var displayKey = field.replace(/_/g, ' ').replace(/\b\w/g, function(l) { return l.toUpperCase(); });
          var displayValue = value || '\u2014';
          if (field === 'file_path' && value) {
            displayValue = '<code style="word-break: break-all; font-size: 0.85rem;">' + window.escapeHtml(window.formatFilePath(value)) + '</code>';
          } else if (Array.isArray(value)) {
            displayValue = value.join(', ');
          } else if (typeof displayValue === 'string') {
            displayValue = window.escapeHtml(displayValue);
          }
          categoryHtml += '<div class="metadata-row"><div class="metadata-label">' + displayKey + '</div><div class="metadata-value">' + displayValue + '</div></div>';
        }
      }
      if (category === 'Notes') {
        for (var key in data) {
          if (!processedKeys.has(key)) {
            processedKeys.add(key);
            var dk = key.replace(/_/g, ' ').replace(/\b\w/g, function(l) { return l.toUpperCase(); });
            var dv = Array.isArray(data[key]) ? data[key].join(', ') : (data[key] || '\u2014');
            categoryHtml += '<div class="metadata-row"><div class="metadata-label">' + dk + '</div><div class="metadata-value">' + window.escapeHtml(String(dv)) + '</div></div>';
          }
        }
      }
      if (categoryHtml) html += '<div class="mb-4"><h6 class="text-spotify-green mb-2" style="border-bottom:1px solid var(--border-color);padding-bottom:0.5rem;">' + category + '</h6>' + categoryHtml + '</div>';
    }
    body.innerHTML = html || '<div class="alert alert-info">No metadata available</div>';
  } catch (error) {
    body.innerHTML = '<div class="alert alert-danger">Error loading metadata: ' + error.message + '</div>';
  }
};

window.escapeHtml = function(text) {
  var div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
};

// Global toast helper — works on every page without page-specific markup.
// Pages that define their own showToast (config, bookmarks, discover, ...)
// keep it — their scripts run later and shadow this one; every other page
// finally gets working toasts instead of silent no-ops.
window.showToast = function(title, message, type) {
  var wrap = document.getElementById('popularrToastWrap');
  if (!wrap) {
    wrap = document.createElement('div');
    wrap.id = 'popularrToastWrap';
    wrap.style.cssText = 'position:fixed;bottom:1rem;left:1rem;z-index:2200;' +
      'display:flex;flex-direction:column;gap:0.5rem;max-width:min(90vw,420px);';
    document.body.appendChild(wrap);
  }
  var el = document.createElement('div');
  var kind = type === 'success' ? 'bg-success'
    : (type === 'error' || type === 'danger') ? 'bg-danger'
    : type === 'warning' ? 'bg-warning text-dark' : 'bg-dark';
  var icon = type === 'success' ? 'bi-check-circle-fill'
    : (type === 'error' || type === 'danger') ? 'bi-x-circle-fill'
    : type === 'warning' ? 'bi-exclamation-triangle-fill' : 'bi-info-circle-fill';
  el.className = 'shadow-sm ' + kind;
  el.style.cssText = 'padding:0.65rem 1rem;border-radius:0.5rem;font-size:0.85rem;' +
    'display:flex;align-items:center;gap:0.5rem;opacity:0;transition:opacity 0.2s ease;';
  el.innerHTML = '<i class="bi ' + icon + '"></i><span></span>';
  // textContent — user-supplied titles/messages must never be injected as HTML.
  el.querySelector('span').textContent = title && message
    ? title + ': ' + message
    : (message || title || '');
  wrap.appendChild(el);
  requestAnimationFrame(function () { el.style.opacity = '1'; });
  setTimeout(function () {
    el.style.opacity = '0';
    setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 250);
  }, 4000);
};

window.addBookmark = function(type, name, artist, album, trackId) {
  fetch('/api/bookmarks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ type: type, name: name, artist: artist, album: album, track_id: trackId })
  })
  .then(function(r) { return r.json(); })
  .then(function(data) {
    if (data.success) {
      if (typeof showToast === 'function') showToast('Success', 'Bookmark added', 'success');
      else alert('Bookmark added successfully');
    } else {
      if (typeof showToast === 'function') showToast('Info', data.error || 'Bookmark may already exist', 'warning');
      else alert(data.error || 'Bookmark may already exist');
    }
  })
  .catch(function(error) {
    if (typeof showToast === 'function') showToast('Error', 'Failed to add bookmark: ' + error.message, 'error');
    else alert('Failed to add bookmark: ' + error.message);
  });
};

// Toggle the track favourite heart (bookmarks-backed, no page reload).
window.toggleTrackFavourite = function(trackId) {
  var icon = document.getElementById('trackFavouriteIcon');
  if (!icon || !trackId) return;

  function setState(fav) {
    icon.classList.remove(fav ? 'bi-heart' : 'bi-heart-fill');
    icon.classList.add(fav ? 'bi-heart-fill' : 'bi-heart');
  }

  fetch('/api/track/favourite?track_id=' + encodeURIComponent(trackId))
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (data.is_favourite) {
        return fetch('/api/track/favourite?track_id=' + encodeURIComponent(trackId), { method: 'DELETE' })
          .then(function () { setState(false); });
      }
      return fetch('/api/track/favourite', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ track_id: trackId })
      }).then(function () { setState(true); });
    })
    .catch(function (error) {
      console.error('Error toggling track favourite:', error);
    });
};

// Toggle the album favourite heart (bookmarks-backed, no page reload).
window.toggleAlbumFavourite = function(artistName, albumName) {
  var icon = document.getElementById('albumFavouriteIcon');
  if (!icon) return;

  function setState(fav) {
    icon.classList.remove(fav ? 'bi-heart' : 'bi-heart-fill');
    icon.classList.add(fav ? 'bi-heart-fill' : 'bi-heart');
  }

  fetch('/api/bookmarks')
    .then(function (r) { return r.json(); })
    .then(function (data) {
      var existing = (data.bookmarks || []).filter(function (b) {
        return b.type === 'album' && b.name === albumName
          && (!b.artist_name || b.artist_name === artistName);
      })[0];
      if (existing) {
        return fetch('/api/bookmarks/' + existing.id, { method: 'DELETE' })
          .then(function () { setState(false); });
      }
      return fetch('/api/bookmarks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type: 'album', name: albumName, artist: artistName, album: albumName })
      }).then(function () { setState(true); });
    })
    .catch(function (error) {
      console.error('Error toggling album favourite:', error);
    });
};

window.updateAlbumWithBeets = function(artist, album) {
  if (!confirm('Update "' + album + '" by "' + artist + '" with beets?')) return;
  var btn = event.target.closest('button');
  var originalText = btn.innerHTML;
  btn.innerHTML = '<i class="bi bi-hourglass-split"></i> Updating...';
  btn.disabled = true;
  fetch('/api/beets/update-album', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ artist: artist, album: album })
  })
  .then(function(r) { return r.json(); })
  .then(function(data) {
    if (data.success) {
      btn.innerHTML = '<i class="bi bi-check-circle"></i> Updated!';
      btn.classList.remove('btn-outline-warning');
      btn.classList.add('btn-success');
      if (typeof showToast === 'function') showToast('Success', 'Album updated with beets', 'success');
      else alert('Album updated successfully!');
      setTimeout(function() { location.reload(); }, 2000);
    } else {
      btn.innerHTML = originalText;
      btn.disabled = false;
      if (typeof showToast === 'function') showToast('Error', 'Failed to update album: ' + data.error, 'error');
      else alert('Error: ' + data.error);
    }
  })
  .catch(function(error) {
    btn.innerHTML = originalText;
    btn.disabled = false;
    if (typeof showToast === 'function') showToast('Error', 'Failed to update album: ' + error.message, 'error');
    else alert('Error: ' + error.message);
  });
};

window.updateArtistAlbumsWithBeets = function(artist) {
  if (!confirm('Update all albums by "' + artist + '" with beets?')) return;
  fetch('/api/beets/album-folders/' + encodeURIComponent(artist))
  .then(function(r) { return r.json(); })
  .then(function(data) {
    if (data.success && data.album_folders && data.album_folders.length > 0) {
      var folders = data.album_folders;
      var btn = event.target.closest('button');
      var originalText = btn.innerHTML;
      btn.innerHTML = '<i class="bi bi-hourglass-split"></i> Updating ' + folders.length + ' album(s)...';
      btn.disabled = true;
      window.updateAlbumsSequentially(folders, 0, function() {
        btn.innerHTML = '<i class="bi bi-check-circle"></i> Complete!';
        btn.classList.remove('btn-outline-warning');
        btn.classList.add('btn-success');
        if (typeof showToast === 'function') showToast('Success', 'Updated ' + folders.length + ' album(s)', 'success');
        setTimeout(function() { location.reload(); }, 2000);
      });
    } else {
      if (typeof showToast === 'function') showToast('Info', 'No albums found', 'info');
      else alert('No albums found with folder information');
    }
  })
  .catch(function(error) {
    if (typeof showToast === 'function') showToast('Error', 'Failed to get album folders: ' + error.message, 'error');
    else alert('Error: ' + error.message);
  });
};

window.updateAlbumsSequentially = function(folders, index, onComplete) {
  if (index >= folders.length) { onComplete(); return; }
  var folder = folders[index];
  fetch('/api/beets/update-album', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ folder: folder })
  })
  .then(function(r) { return r.json(); })
  .then(function() {
    window.updateAlbumsSequentially(folders, index + 1, onComplete);
  })
  .catch(function() {
    window.updateAlbumsSequentially(folders, index + 1, onComplete);
  });
};

window.navSearch = function(event) {
  event.preventDefault();
  var form = event && event.target ? event.target : null;
  var activeInput = form ? form.querySelector('input[type="text"], input[type="search"]') : null;
  var fallbackInput = document.getElementById('navSearchInput');
  var fallbackSearchInput = document.getElementById('dashboardTopSearchInput');
  var query = ((activeInput && activeInput.value) || (fallbackInput && fallbackInput.value) || (fallbackSearchInput && fallbackSearchInput.value) || '').trim();
  if (query) window.location.href = '/search?q=' + encodeURIComponent(query);
};

// ==========================================================================
// Global MusicBrainz search modal (canonical implementation)
// ==========================================================================
// Opens the MusicBrainz search modal included from
// components/_musicbrainz_search_modal.html (loaded via base.html). Fills
// the 4-field form (artist/album/track/year), wires the selection callback,
// and auto-runs the search. This is the single source of truth — pages that
// once defined their own copy (base.html inline block, downloads.js) are
// consolidated here.

window.openGlobalMbSearch = function(artist, album, callback, track, year) {
    const modalEl = document.getElementById('musicBrainzModal');
    if (!modalEl) {
        console.error("MusicBrainz modal not found in DOM.");
        return;
    }

    // Auto-fill the 4 search fields
    const artistEl = document.getElementById('mbSearchArtist');
    const albumEl = document.getElementById('mbSearchAlbum');
    const trackEl = document.getElementById('mbSearchTrack');
    const yearEl = document.getElementById('mbSearchYear');
    if (artistEl && artist) artistEl.value = artist;
    if (albumEl && album) albumEl.value = album;
    if (trackEl && track) trackEl.value = track;
    if (yearEl && year) yearEl.value = year;

    // Assign callback to global window object so the component can trigger it
    window._mbSearchCallback = callback;

    // Show Modal
    const modal = bootstrap.Modal.getInstance(modalEl) || new bootstrap.Modal(modalEl);
    modal.show();

    // Auto-search after modal opens
    setTimeout(function() {
        if (typeof window.performMbSearch === 'function') {
            window.performMbSearch();
        }
    }, 500);
};

// ==========================================================================
// Encoded-argument MusicBrainz search (shared by artist/album detail pages)
// ==========================================================================
// Decodes URL-encoded inline arguments and opens the canonical shared modal.
// Some pages (album_detail.js) render buttons calling this with encoded args.

// Self-contained Soulseek download helper for pages that don't load
// downloads.js (artist/album pages) — mirrors _addMbDownloadToSession.
window.downloadReleaseViaSoulseek = function(releaseId, title, artist) {
    if (!confirm(`Download "${title}" by ${artist} via Soulseek?`)) return;
    fetch('/api/musicbrainz/download', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            release_id: releaseId,
            release_title: title,
            artist: artist,
            method: 'slskd',
            persistent_search: false,
            max_retries: 3,
            session_id: null,
            queue_items_only: true
        })
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        if (data.error) { alert('Error: ' + data.error); return; }
        alert(`Download queued: ${title}\nTracking ID: ${data.tracking_id || 'N/A'}`);
    })
    .catch(function(err) { alert('Network error: ' + err.message); });
};

window.searchMusicBrainzReleaseFromEncoded = function(event, artistEnc, albumEnc) {
    const decode = function(value, fallback) {
        try {
            const decoded = decodeURIComponent(value || '');
            try { return JSON.parse(decoded); } catch (_e) { return decoded || fallback; }
        } catch (_e) { return fallback; }
    };
    const artist = decode(artistEnc, '');
    const album = decode(albumEnc, '');
    if (event && event.preventDefault) event.preventDefault();
    if (event && event.stopPropagation) event.stopPropagation();
    window.openGlobalMbSearch(artist, album, function(selectedRelease) {
        if (!selectedRelease) return;
        if (typeof window.downloadMbRelease === 'function') {
            window.downloadMbRelease(selectedRelease.id, selectedRelease.title, selectedRelease.artist, 'slskd');
        } else if (typeof window.downloadReleaseViaSoulseek === 'function') {
            window.downloadReleaseViaSoulseek(selectedRelease.id, selectedRelease.title, selectedRelease.artist);
        }
    });
};
