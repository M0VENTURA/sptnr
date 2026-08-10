/**
 * Unified hybrid search — All / In Library / MusicBrainz.
 *
 * Backs the global modal in components/_unified_search_modal.html
 * (included from base.html) and the tap-to-open navbar / dashboard search
 * inputs. Local scopes hit POST /api/search; the MusicBrainz scope reuses
 * the shared component's searchMusicBrainzReleases() (single free-text
 * query path) so the two MB search UIs stay consistent.
 *
 * Debounce: 50ms local scopes / 400ms MusicBrainz.
 */
(function () {
  'use strict';

  var SCOPE_ALL = 'all';
  var SCOPE_LIBRARY = 'library';
  var SCOPE_MB = 'mb';
  var LOCAL_DEBOUNCE_MS = 50;
  var MB_DEBOUNCE_MS = 400;
  var MB_LIMIT_ALL_TAB = 5;   // compact MB section inside the "All" tab
  var MB_LIMIT_MB_TAB = 25;   // full MusicBrainz tab
  var MIN_QUERY_LENGTH = 2;

  var _scope = SCOPE_ALL;
  var _debounceTimer = null;
  var _runSeq = 0;          // guards against out-of-order responses
  var _queuedIds = {};      // release-group id -> true once queued
  var _mbIndex = {};        // release id -> release (for queue buttons)

  function getModalEl() { return document.getElementById('unifiedSearchModal'); }
  function getInputEl() { return document.getElementById('unifiedSearchInput'); }
  function getResultsEl() { return document.getElementById('unifiedSearchResults'); }
  function getMetaEl() { return document.getElementById('usResultMeta'); }
  function getErrorEl() { return document.getElementById('unifiedSearchError'); }

  function esc(text) {
    if (text === undefined || text === null) return '';
    return String(text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // ===== Scope tabs =====

  function selectScope(scope) {
    _scope = (scope === SCOPE_MB || scope === SCOPE_LIBRARY) ? scope : SCOPE_ALL;
    var tabs = document.querySelectorAll('#unifiedScopeTabs .nav-link');
    for (var i = 0; i < tabs.length; i++) {
      var active = tabs[i].getAttribute('data-scope') === _scope;
      tabs[i].classList.toggle('active', active);
      tabs[i].setAttribute('aria-selected', active ? 'true' : 'false');
    }
  }

  // ===== Data fetching =====

  function fetchLibrary(query) {
    return fetch('/api/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: query })
    })
      .then(function (res) { return res.ok ? res.json() : { artists: [], albums: [], tracks: [] }; })
      .catch(function () { return { artists: [], albums: [], tracks: [] }; });
  }

  function fetchMb(query, limit) {
    // Reuse the shared MB component internals when available (base.html
    // loads _musicbrainz_search_component.html globally).
    if (typeof window.searchMusicBrainzReleases === 'function') {
      return window.searchMusicBrainzReleases(query, '', limit)
        .then(function (data) { return data.releases || []; })
        .catch(function () { return []; });
    }
    return fetch('/api/musicbrainz/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: query })
    })
      .then(function (res) { return res.ok ? res.json() : { releases: [] }; })
      .then(function (data) { return (data.releases || []).slice(0, limit); })
      .catch(function () { return []; });
  }

  // ===== Result rendering =====

  function section(title, rowsHtml) {
    return '<div class="us-section mb-1">' +
      '<div class="us-section-title d-flex align-items-center gap-2">' + title + '</div>' +
      rowsHtml +
      '</div>';
  }

  function artistRows(artists) {
    return artists.map(function (a) {
      return '<a class="us-row" href="/artist/' + encodeURIComponent(a.name) + '">' +
        '<span class="us-row-icon"><i class="bi bi-person-badge"></i></span>' +
        '<span class="us-row-main">' +
          '<span class="us-row-title d-block">' + esc(a.name) + '</span>' +
          '<span class="us-row-sub d-block">' + a.track_count + ' track' + (a.track_count === 1 ? '' : 's') + ' · ' + a.album_count + ' album' + (a.album_count === 1 ? '' : 's') + '</span>' +
        '</span>' +
        '<span class="us-row-meta badge bg-secondary-subtle text-secondary-emphasis">Artist</span>' +
      '</a>';
    }).join('');
  }

  function albumRows(albums) {
    return albums.map(function (al) {
      return '<a class="us-row" href="/album/' + encodeURIComponent(al.artist) + '/' + encodeURIComponent(al.album) + '">' +
        '<span class="us-row-icon"><i class="bi bi-disc"></i></span>' +
        '<span class="us-row-main">' +
          '<span class="us-row-title d-block">' + esc(al.album) + '</span>' +
          '<span class="us-row-sub d-block">' + esc(al.artist) + '</span>' +
        '</span>' +
        '<span class="us-row-meta badge bg-secondary-subtle text-secondary-emphasis">Album</span>' +
      '</a>';
    }).join('');
  }

  function trackRows(tracks) {
    return tracks.map(function (t) {
      return '<a class="us-row" href="/track/' + encodeURIComponent(t.id) + '">' +
        '<span class="us-row-icon"><i class="bi bi-music-note"></i></span>' +
        '<span class="us-row-main">' +
          '<span class="us-row-title d-block">' + esc(t.title) + '</span>' +
          '<span class="us-row-sub d-block">' + esc(t.artist) + (t.album ? ' · ' + esc(t.album) : '') + '</span>' +
        '</span>' +
        (t.stars ? '<span class="us-row-meta text-warning"><i class="bi bi-star-fill"></i> ' + esc(t.stars) + '</span>' : '') +
      '</a>';
    }).join('');
  }

  function mbReleaseArtist(r) {
    if (r.artist) return r.artist;
    if (r['artist-credit']) {
      return r['artist-credit'].map(function (a) {
        return typeof a === 'string' ? a : (a.name || a.artist || '');
      }).join(', ');
    }
    return 'Unknown Artist';
  }

  function mbReleaseYear(r) {
    return String(r.first_release_date || r.date || r.year || '').split('-')[0] || '?';
  }

  function mbRows(releases, withQueue) {
    var html = '';
    for (var i = 0; i < releases.length; i++) {
      var r = releases[i];
      var id = r.id || '';
      var queued = !!_queuedIds[id];
      _mbIndex[id] = r;
      var topTypes = ['album', 'single', 'ep', 'compilation', 'live', 'remix'];
      var type = String(r.category || r.primary_type || '').toLowerCase();
      if (topTypes.indexOf(type) === -1) type = 'release';

      html += '<div class="us-row">' +
        '<span class="us-row-icon"><i class="bi bi-hexagon"></i></span>' +
        '<span class="us-row-main">' +
          '<span class="us-row-title d-block">' + esc(r.title) + '</span>' +
          '<span class="us-row-sub d-block">' + esc(mbReleaseArtist(r)) + '</span>' +
        '</span>' +
        '<span class="us-row-meta text-nowrap">' + esc(mbReleaseYear(r)) + '</span>' +
        '<span class="badge bg-info-subtle text-info-emphasis text-nowrap">' + esc(type) + '</span>';

      if (withQueue) {
        if (queued) {
          html += '<button class="btn btn-sm btn-success" disabled title="Already queued"><i class="bi bi-check2"></i> Queued</button>';
        } else {
          html += '<button class="btn btn-sm btn-outline-primary us-queue-btn" data-mbid="' + esc(id) + '" title="Queue download via Soulseek"><i class="bi bi-plus-lg"></i> Queue</button>';
        }
      }
      html += '</div>';
    }
    return html;
  }

  function renderLocalSections(local, mbReleases, query) {
    var html = '';
    var artists = local.artists || [];
    var albums = local.albums || [];
    var tracks = local.tracks || [];

    if (artists.length) html += section('Artists', artistRows(artists));
    if (albums.length) html += section('Albums', albumRows(albums));
    if (tracks.length) html += section('Tracks', trackRows(tracks));
    if (!artists.length && !albums.length && !tracks.length) {
      html += '<div class="text-center text-muted py-4 small">No library matches for "' + esc(query) + '"</div>';
    }

    if (_scope === SCOPE_ALL) {
      if (mbReleases.length) {
        html += section(
          'MusicBrainz <span class="badge bg-secondary ms-1">' + mbReleases.length + '</span>',
          mbRows(mbReleases, true)
        );
        html += '<div class="text-center my-2">' +
          '<button type="button" class="btn btn-sm btn-outline-info" onclick="openUnifiedSearch(\'mb\')">' +
          '<i class="bi bi-search"></i> All MusicBrainz results</button></div>';
      } else {
        html += '<div class="text-center text-muted py-3 small"><i class="bi bi-hexagon"></i> No MusicBrainz matches for "' + esc(query) + '"</div>';
      }
    }
    return html;
  }

  function renderMbTab(releases, query) {
    if (!releases.length) {
      return '<div class="text-center text-muted py-4"><i class="bi bi-hexagon" style="font-size:2rem;"></i>' +
        '<p class="mt-2 mb-0 small">No MusicBrainz releases found for "' + esc(query) + '"</p></div>';
    }
    return section(
      'MusicBrainz releases <span class="badge bg-secondary ms-1">' + releases.length + '</span>',
      mbRows(releases, true)
    );
  }

  // ===== Search execution =====

  function runSearch() {
    var input = getInputEl();
    var resultsEl = getResultsEl();
    if (!input || !resultsEl) return;

    var query = input.value.trim();
    var seq = ++_runSeq;

    if (query.length < MIN_QUERY_LENGTH) {
      resultsEl.innerHTML = '<div class="text-center text-muted py-5">' +
        '<i class="bi bi-search" style="font-size: 2rem;"></i>' +
        '<p class="mt-2 mb-0 small">Type at least ' + MIN_QUERY_LENGTH + ' characters</p></div>';
      if (getMetaEl()) getMetaEl().textContent = '';
      return;
    }

    resultsEl.innerHTML = '<div class="text-center py-5"><div class="spinner-border text-primary" role="status"><span class="visually-hidden">Loading...</span></div></div>';

    if (_scope === SCOPE_MB) {
      fetchMb(query, MB_LIMIT_MB_TAB)
        .then(function (releases) {
          if (seq !== _runSeq) return;
          if (getMetaEl()) getMetaEl().textContent = releases.length + ' musicbrainz result' + (releases.length === 1 ? '' : 's');
          resultsEl.innerHTML = renderMbTab(releases, query);
        });
      return;
    }

    var localPromise = fetchLibrary(query);
    var mbPromise = _scope === SCOPE_ALL ? fetchMb(query, MB_LIMIT_ALL_TAB) : Promise.resolve([]);
    Promise.all([localPromise, mbPromise])
      .then(function (results) {
        if (seq !== _runSeq) return;
        var local = results[0];
        var mbReleases = results[1];
        var counts = [
          (local.artists || []).length + ' artist' + ((local.artists || []).length === 1 ? '' : 's'),
          (local.albums || []).length + ' album' + ((local.albums || []).length === 1 ? '' : 's'),
          (local.tracks || []).length + ' track' + ((local.tracks || []).length === 1 ? '' : 's')
        ];
        if (_scope === SCOPE_ALL) counts.push(mbReleases.length + ' musicbrainz');
        if (getMetaEl()) getMetaEl().textContent = counts.join(' · ');
        resultsEl.innerHTML = renderLocalSections(local, mbReleases, query);
      });
  }

  function scheduleSearch() {
    clearTimeout(_debounceTimer);
    var delay = _scope === SCOPE_MB ? MB_DEBOUNCE_MS : LOCAL_DEBOUNCE_MS;
    _debounceTimer = setTimeout(runSearch, delay);
  }

  // ===== Quick queue (MusicBrainz release -> Soulseek) =====

  function notifyError(message) {
    var errEl = getErrorEl();
    if (!errEl) return;
    errEl.textContent = message;
    errEl.classList.remove('d-none');
    clearTimeout(notifyError._timer);
    notifyError._timer = setTimeout(function () { errEl.classList.add('d-none'); }, 5000);
  }

  function queueRelease(rel, btn) {
    var id = rel.id || '';
    if (!id || _queuedIds[id]) return;
    _queuedIds[id] = true;

    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status"></span>';

    var artist = mbReleaseArtist(rel);
    fetch('/api/musicbrainz/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        release_id: id,
        release_title: rel.title || '',
        artist: artist,
        method: 'slskd',
        queue_items_only: true
      })
    })
      .then(function (res) { return res.json().catch(function () { return {}; }).then(function (data) { return { ok: res.ok, data: data }; }); })
      .then(function (out) {
        if (!out.ok) throw new Error(out.data.error || 'Queue request failed');
        btn.classList.replace('btn-outline-primary', 'btn-success');
        btn.innerHTML = '<i class="bi bi-check2"></i> Queued';
        btn.title = 'Queued: ' + (rel.title || '');
      })
      .catch(function (e) {
        _queuedIds[id] = false;
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-plus-lg"></i> Queue';
        notifyError('Queue failed: ' + e.message);
      });
  }

  // ===== Modal lifecycle / public API =====

  window.openUnifiedSearch = function (scope, prefill) {
    var modalEl = getModalEl();
    var input = getInputEl();
    if (!modalEl || !input) return;

    if (prefill === undefined || prefill === null) {
      var navEl = document.getElementById('navSearchInput');
      var dashEl = document.getElementById('dashboardTopSearchInput');
      prefill = (navEl && navEl.value) || (dashEl && dashEl.value) || '';
    }

    selectScope(scope || SCOPE_ALL);
    input.value = prefill;
    if (getErrorEl()) getErrorEl().classList.add('d-none');

    bootstrap.Modal.getOrCreateInstance(modalEl, { focus: true }).show();

    clearTimeout(_debounceTimer);
    setTimeout(function () { input.focus(); }, 350);
    runSearch();
  };

  window.closeUnifiedSearch = function () {
    var modalEl = getModalEl();
    if (!modalEl) return;
    var instance = bootstrap.Modal.getInstance(modalEl);
    if (instance) instance.hide();
  };

  // ===== Wiring =====

  document.addEventListener('DOMContentLoaded', function () {
    var modalEl = getModalEl();
    var input = getInputEl();
    var resultsEl = getResultsEl();
    if (!modalEl || !input || !resultsEl) return;

    // Debounced input (per-scope rate handled in scheduleSearch).
    input.addEventListener('input', scheduleSearch);
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        clearTimeout(_debounceTimer);
        runSearch();
      }
    });

    // Scope tabs — switching re-runs immediately with the current query.
    var tabs = document.querySelectorAll('#unifiedScopeTabs .nav-link');
    for (var i = 0; i < tabs.length; i++) {
      tabs[i].addEventListener('click', function () {
        selectScope(this.getAttribute('data-scope'));
        clearTimeout(_debounceTimer);
        runSearch();
      });
    }

    // Queue buttons (delegated — rows are re-rendered on every search).
    resultsEl.addEventListener('click', function (e) {
      var btn = e.target.closest ? e.target.closest('.us-queue-btn') : null;
      if (!btn) return;
      e.preventDefault();
      var rel = _mbIndex[btn.getAttribute('data-mbid')];
      if (rel) queueRelease(rel, btn);
    });
  });
})();