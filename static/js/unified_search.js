/**
 * Unified hybrid search — All / In Library / MusicBrainz.
 *
 * Backs the global modal in components/_unified_search_modal.html
 * (included from base.html) and the tap-to-open navbar / dashboard search
 * inputs. Local scopes hit POST /api/search; the MusicBrainz scope reuses
 * the shared component's searchMusicBrainzReleases() (single free-text
 * query path) so the two MB search UIs stay consistent.
 *
 * Results render in the artist-page discography structure: ARTISTS, ALBUMS,
 * COMPILATIONS, LIVE ALBUMS, EPS, SINGLES, TRACKS.  Local and MusicBrainz
 * releases merge into the same buckets (year DESC → local 🟢 before
 * external 🟡 → title ASC), with quick-queue buttons on external releases.
 *
 * Optional advanced filters (Artist / Album / Track / Year) and the release
 * type dropdown map to the structured fields the backend already supports
 * (artist / releasegroup / recording / date / primarytype+secondarytype), so
 * precision queries avoid the 1 req/sec Lucene free-text path.
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
  var MB_LIMIT_ALL_TAB = 12;   // compact MB section inside the "All" tab
  var MB_LIMIT_MB_TAB = 25;    // full MusicBrainz tab
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
      .then(function (res) { return res.ok ? res.json() : { artists: [], albums: [], compilations: [], live_albums: [], eps: [], singles: [], tracks: [] }; })
      .catch(function () { return { artists: [], albums: [], compilations: [], live_albums: [], eps: [], singles: [], tracks: [] }; });
  }

  function getTypeFilter() {
    var el = document.getElementById('unifiedSearchType');
    return el ? el.value : '';
  }

  function getAdvancedFilters() {
    function val(id) {
      var el = document.getElementById(id);
      return el ? el.value.trim() : '';
    }
    return {
      artist: val('unifiedFilterArtist'),
      album: val('unifiedFilterAlbum'),
      track: val('unifiedFilterTrack'),
      year: val('unifiedFilterYear')
    };
  }

  function fetchMb(query, limit, opts) {
    opts = opts || {};
    var hasAdvanced = opts.artist || opts.album || opts.track || opts.year;

    // Plain free-text query with no structured filters — reuse the shared MB
    // component internals so the two search UIs stay consistent.
    if (!hasAdvanced && !opts.type && typeof window.searchMusicBrainzReleases === 'function') {
      return window.searchMusicBrainzReleases(query, '', limit)
        .then(function (data) { return data.releases || []; })
        .catch(function () { return []; });
    }

    var payload = {};
    if (hasAdvanced) {
      payload.artist = opts.artist || '';
      payload.album = opts.album || '';
      payload.track = opts.track || '';
      payload.year = opts.year || '';
      // Artist-only search means "release groups BY the artist", not groups
      // whose title happens to match the artist name (matches the MB modal).
      if (opts.artist && !opts.album && !opts.track && !opts.year) payload.artist_only = true;
    } else {
      payload.query = query;
    }
    if (opts.type) payload.type = opts.type;

    return fetch('/api/musicbrainz/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
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
          '<span class="us-row-title d-block">🟢 ' + esc(a.name) + '</span>' +
          '<span class="us-row-sub d-block">' + a.track_count + ' track' + (a.track_count === 1 ? '' : 's') + ' · ' + a.album_count + ' album' + (a.album_count === 1 ? '' : 's') + '</span>' +
        '</span>' +
        '<span class="us-row-meta badge bg-secondary-subtle text-secondary-emphasis">Artist</span>' +
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

  // Release buckets mirror the artist page discography: Albums (studio),
  // Compilations, Live Albums, EPs, Singles.  MusicBrainz releases are
  // classified into the SAME buckets so the "All" scope merges local 🟢 and
  // external 🟡 results into one consistent structure.
  var SECTION_DEFS = [
    { key: 'albums', label: 'Albums', localLabel: 'Studio Album' },
    { key: 'compilations', label: 'Compilations', localLabel: 'Compilation' },
    { key: 'live_albums', label: 'Live Albums', localLabel: 'Live Album' },
    { key: 'eps', label: 'EPs', localLabel: 'EP' },
    { key: 'singles', label: 'Singles', localLabel: 'Single' }
  ];

  function classifyMbRelease(r) {
    var secondary = (r.secondary_types || []).map(function (s) { return String(s).toLowerCase(); });
    if (secondary.indexOf('live') !== -1) return 'live_albums';
    if (secondary.indexOf('compilation') !== -1) return 'compilations';
    if (secondary.indexOf('ep') !== -1) return 'eps';
    if (secondary.indexOf('single') !== -1) return 'singles';
    var pt = String(r.category || r.primary_type || '').toLowerCase();
    if (pt === 'ep') return 'eps';
    if (pt === 'single') return 'singles';
    return 'albums';
  }

  function bucketTypeLabel(bucketKey) {
    for (var i = 0; i < SECTION_DEFS.length; i++) {
      if (SECTION_DEFS[i].key === bucketKey) return SECTION_DEFS[i].localLabel;
    }
    return 'Release';
  }

  function releaseRows(items, withQueue) {
    var html = '';
    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      var yearSuffix = it.year ? ' (' + esc(it.year) + ')' : '';
      if (it.local) {
        var href = '/album/' + encodeURIComponent(it.artist) + '/' + encodeURIComponent(it.title);
        html += '<a class="us-row" href="' + href + '">' +
          '<span class="us-row-icon"><i class="bi bi-disc"></i></span>' +
          '<span class="us-row-main">' +
            '<span class="us-row-title d-block">🟢 ' + esc(it.title) + yearSuffix + '</span>' +
            '<span class="us-row-sub d-block">' + esc(it.typeLabel || 'Album') + ' • In Library</span>' +
          '</span>' +
        '</a>';
      } else {
        var id = it.id || '';
        var queued = !!_queuedIds[id];
        _mbIndex[id] = it.release || null;
        html += '<div class="us-row">' +
          '<span class="us-row-icon"><i class="bi bi-hexagon"></i></span>' +
          '<span class="us-row-main">' +
            '<span class="us-row-title d-block">🟡 ' + esc(it.title) + yearSuffix + '</span>' +
            '<span class="us-row-sub d-block">MusicBrainz ' + esc(it.typeLabel || 'Release') + ' • ' + esc(it.artist) + '</span>' +
          '</span>' +
          (withQueue ? (queued
            ? '<button class="btn btn-sm btn-success" disabled title="Already queued"><i class="bi bi-check2"></i> Queued</button>'
            : '<button class="btn btn-sm btn-outline-primary us-queue-btn" data-mbid="' + esc(id) + '" title="Queue download via Soulseek"><i class="bi bi-download"></i> Queue</button>')
            : '') +
        '</div>';
      }
    }
    return html;
  }

  function buildBuckets(local, mbReleases) {
    var buckets = { albums: [], compilations: [], live_albums: [], eps: [], singles: [] };
    (local.albums || []).forEach(function (al) {
      var bucket = buckets[al.type] ? al.type : 'albums';
      buckets[bucket].push({
        title: al.album, artist: al.artist, year: al.year || null,
        typeLabel: al.type_label || 'Album', local: true
      });
    });
    (mbReleases || []).forEach(function (r) {
      var bucket = classifyMbRelease(r);
      buckets[bucket].push({
        title: r.title, artist: mbReleaseArtist(r), year: mbReleaseYear(r),
        typeLabel: bucketTypeLabel(bucket), local: false, id: r.id || '', release: r
      });
    });
    // Sort each bucket: Release Year (newest → oldest, unknown last) →
    // ownership (local 🟢 before external 🟡) → Title (ASC).
    Object.keys(buckets).forEach(function (k) {
      buckets[k].sort(function (a, b) {
        var ay = (a.year === '?' || a.year === null || a.year === undefined) ? 0 : Number(a.year) || 0;
        var by = (b.year === '?' || b.year === null || b.year === undefined) ? 0 : Number(b.year) || 0;
        if ((ay > 0) !== (by > 0)) return ay > 0 ? -1 : 1;
        if (ay !== by) return by - ay;
        if (a.local !== b.local) return a.local ? -1 : 1;
        return String(a.title || '').toLowerCase() < String(b.title || '').toLowerCase() ? -1 : 1;
      });
    });
    return buckets;
  }

  function renderReleaseSections(buckets, withQueue) {
    var html = '';
    for (var i = 0; i < SECTION_DEFS.length; i++) {
      var def = SECTION_DEFS[i];
      var items = buckets[def.key] || [];
      if (!items.length) continue;
      html += section(
        def.label + ' <span class="badge bg-secondary ms-1">' + items.length + '</span>',
        releaseRows(items, withQueue)
      );
    }
    return html;
  }

  function renderBucketedResults(local, mbReleases, query, opts) {
    opts = opts || {};
    var html = '';
    var artists = local.artists || [];
    if (artists.length) html += section('Artists', artistRows(artists));

    html += renderReleaseSections(buildBuckets(local, mbReleases), opts.withQueue);

    if (opts.allMbButton && mbReleases.length) {
      html += '<div class="text-center my-2">' +
        '<button type="button" class="btn btn-sm btn-outline-info" onclick="openUnifiedSearch(\'mb\')">' +
        '<i class="bi bi-search"></i> All MusicBrainz results</button></div>';
    }

    var tracks = local.tracks || [];
    if (tracks.length) html += section('Tracks', trackRows(tracks));
    if (!artists.length && !html) {
      html += '<div class="text-center text-muted py-4 small">No library matches for "' + esc(query) + '"</div>';
    }
    return html;
  }

  function renderMbTab(releases, query) {
    if (!releases.length) {
      return '<div class="text-center text-muted py-4"><i class="bi bi-hexagon" style="font-size:2rem;"></i>' +
        '<p class="mt-2 mb-0 small">No MusicBrainz releases found for "' + esc(query) + '"</p></div>';
    }
    return renderReleaseSections(buildBuckets({}, releases), true);
  }

  // ===== Search execution =====

  function runSearch() {
    var input = getInputEl();
    var resultsEl = getResultsEl();
    if (!input || !resultsEl) return;

    var query = input.value.trim();
    var adv = getAdvancedFilters();
    var hasAdvanced = adv.artist || adv.album || adv.track || adv.year;
    var seq = ++_runSeq;

    if (query.length < MIN_QUERY_LENGTH && !hasAdvanced) {
      resultsEl.innerHTML = '<div class="text-center text-muted py-5">' +
        '<i class="bi bi-search" style="font-size: 2rem;"></i>' +
        '<p class="mt-2 mb-0 small">Type at least ' + MIN_QUERY_LENGTH + ' characters</p></div>';
      if (getMetaEl()) getMetaEl().textContent = '';
      return;
    }

    resultsEl.innerHTML = '<div class="text-center py-5"><div class="spinner-border text-primary" role="status"><span class="visually-hidden">Loading...</span></div></div>';

    var mbOpts = {
      type: getTypeFilter(),
      artist: adv.artist,
      album: adv.album,
      track: adv.track,
      year: adv.year
    };

    if (_scope === SCOPE_MB) {
      fetchMb(query, MB_LIMIT_MB_TAB, mbOpts)
        .then(function (releases) {
          if (seq !== _runSeq) return;
          if (getMetaEl()) getMetaEl().textContent = releases.length + ' musicbrainz result' + (releases.length === 1 ? '' : 's');
          resultsEl.innerHTML = renderMbTab(releases, query);
        });
      return;
    }

    var localPromise = query.length >= MIN_QUERY_LENGTH
      ? fetchLibrary(query)
      : Promise.resolve({ artists: [], albums: [], compilations: [], live_albums: [], eps: [], singles: [], tracks: [] });
    var mbPromise = _scope === SCOPE_ALL ? fetchMb(query, MB_LIMIT_ALL_TAB, mbOpts) : Promise.resolve([]);
    Promise.all([localPromise, mbPromise])
      .then(function (results) {
        if (seq !== _runSeq) return;
        var local = results[0];
        var mbReleases = results[1];
        var releaseCount = ['albums', 'compilations', 'live_albums', 'eps', 'singles'].reduce(function (n, k) {
          return n + ((local[k] || []).length);
        }, 0);
        var counts = [
          (local.artists || []).length + ' artist' + ((local.artists || []).length === 1 ? '' : 's'),
          releaseCount + ' album' + (releaseCount === 1 ? '' : 's'),
          (local.tracks || []).length + ' track' + ((local.tracks || []).length === 1 ? '' : 's')
        ];
        if (_scope === SCOPE_ALL) counts.push(mbReleases.length + ' musicbrainz');
        if (getMetaEl()) getMetaEl().textContent = counts.join(' · ');
        resultsEl.innerHTML = renderBucketedResults(local, mbReleases, query, {
          withQueue: true,
          allMbButton: _scope === SCOPE_ALL
        });
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

    // Advanced filters toggle (slide-down panel).
    var filtersToggle = document.getElementById('unifiedFiltersToggle');
    var filtersPanel = document.getElementById('unifiedAdvancedFilters');
    if (filtersToggle && filtersPanel) {
      filtersToggle.addEventListener('click', function () {
        var shown = filtersPanel.classList.toggle('show');
        filtersToggle.setAttribute('aria-expanded', shown ? 'true' : 'false');
      });
    }

    // Advanced filter fields + type dropdown re-run the search.
    var filterInputs = document.querySelectorAll('#unifiedAdvancedFilters input, #unifiedSearchType');
    for (var i = 0; i < filterInputs.length; i++) {
      filterInputs[i].addEventListener('input', function () {
        clearTimeout(_debounceTimer);
        _debounceTimer = setTimeout(runSearch, _scope === SCOPE_MB ? MB_DEBOUNCE_MS : LOCAL_DEBOUNCE_MS);
      });
      filterInputs[i].addEventListener('change', function () {
        clearTimeout(_debounceTimer);
        runSearch();
      });
    }

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