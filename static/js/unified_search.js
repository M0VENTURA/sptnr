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
  var _counts = { all: 0, library: 0, mb: 0 };  // live scope-pill counts
  var _lastQuery = null;    // query + scope of the last completed render
  var _lastScope = null;    // (used to restore state instantly on reopen)

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

  // ===== Scope counts / state memory =====

  function countLibrary(local) {
    var albumBuckets = (local.albums || []).length + (local.compilations || []).length +
      (local.live_albums || []).length + (local.eps || []).length + (local.singles || []).length;
    return (local.artists || []).length + albumBuckets + (local.tracks || []).length;
  }

  // Live counts inside the scope selector pills (All / In Library / MusicBrainz).
  function updateScopeCounts() {
    var labels = { all: _counts.all, library: _counts.library, mb: _counts.mb };
    var tabs = document.querySelectorAll('#unifiedScopeTabs .nav-link');
    for (var i = 0; i < tabs.length; i++) {
      var span = tabs[i].querySelector('.us-scope-count');
      if (!span) continue;
      var n = labels[tabs[i].getAttribute('data-scope')] || 0;
      if (n > 0) {
        span.textContent = n;
        span.classList.remove('d-none');
      } else {
        span.classList.add('d-none');
      }
    }
  }

  function markRendered(query) {
    _lastQuery = query;
    _lastScope = _scope;
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
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      });
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

  var _IMG_PLACEHOLDER = 'data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 width=%2244%22 height=%2244%22%3E%3Crect fill=%22%232a2a2a%22 width=%2244%22 height=%2244%22/%3E%3C/svg%3E';

  // 44px row thumbnail with a dark placeholder fallback on load errors.
  function thumbHtml(src, cls, alt) {
    if (!src) src = _IMG_PLACEHOLDER;
    return '<img src="' + esc(src) + '" alt="' + esc(alt || '') + '" loading="lazy" ' +
      'class="' + cls + ' flex-shrink-0" ' +
      'style="width:44px;height:44px;object-fit:cover;" ' +
      'onerror="this.onerror=null;this.src=\'' + _IMG_PLACEHOLDER + '\';">';
  }

  // Thumbnail + source badge overlaid on its top-right corner: green ♫ for
  // library-owned rows, amber ⬡ for MusicBrainz rows (matches the scope-tab
  // accents) — no bullet characters cluttering the text column.
  function thumbWithBadge(thumb, badgeIcon, badgeClass) {
    return '<span class="us-thumb-wrap">' + thumb +
      '<span class="us-thumb-badge ' + badgeClass + '">' + badgeIcon + '</span></span>';
  }

  function fmtDuration(seconds) {
    var s = Number(seconds);
    if (!isFinite(s) || s <= 0) return '';
    var m = Math.round(s / 60);
    if (m >= 60) return Math.floor(m / 60) + 'h ' + (m % 60) + 'm';
    return m + 'm';
  }

  // ===== Result rendering =====

  // Max rows shown per section before the inline "Show All N ▼" toggle.
  var SECTION_INLINE_LIMIT = 5;

  // Build a section with an inline Show All / Show Less toggle when the row
  // count exceeds the inline limit.  ``rower`` maps an item array to row HTML.
  function buildSection(title, items, rower) {
    var count = items.length;
    var visible = items.slice(0, SECTION_INLINE_LIMIT);
    var hidden = items.slice(SECTION_INLINE_LIMIT);
    var toggle = hidden.length
      ? '<button type="button" class="btn btn-sm btn-link us-section-toggle py-0 ms-auto text-decoration-none" data-count="' + count + '" onclick="toggleUsSection(this)">' +
        'Show All ' + count + ' <i class="bi bi-chevron-down"></i></button>'
      : '';
    return '<div class="us-section mb-1">' +
      '<div class="us-section-title d-flex align-items-center gap-2">' + title + toggle + '</div>' +
      rower(visible) +
      (hidden.length ? '<div class="us-section-more d-none">' + rower(hidden) + '</div>' : '') +
      '</div>';
  }

  function toggleUsSection(btn) {
    var hiddenEl = btn.closest('.us-section').querySelector('.us-section-more');
    if (!hiddenEl) return;
    var expanded = !hiddenEl.classList.contains('d-none');
    hiddenEl.classList.toggle('d-none', expanded);
    btn.innerHTML = expanded
      ? 'Show All ' + (btn.dataset.count || '') + ' <i class="bi bi-chevron-down"></i>'
      : 'Show Less <i class="bi bi-chevron-up"></i>';
  }

  // Keep only the bucket(s) matching the active Type filter; artists and
  // tracks are suppressed whenever a specific type is selected.
  function filterLocalByType(local, type) {
    if (!type) return local;
    var bucketMap = {
      album: ['albums'],
      single: ['singles'],
      ep: ['eps'],
      compilation: ['compilations'],
      live: ['live_albums'],
      soundtrack: ['albums', 'compilations'],
      remix: ['albums']
    };
    var keep = bucketMap[type] || [];
    var out = { artists: [], albums: [], compilations: [], live_albums: [], eps: [], singles: [], tracks: [] };
    keep.forEach(function (k) { out[k] = local[k] || []; });
    return out;
  }

  function artistRows(artists) {
    return artists.map(function (a) {
      // "Various Artists" is the library-wide compilation placeholder — flag
      // it so it isn't mistaken for a solo musician (the backend now merges
      // all casing variants into this single row).
      var isVarious = String(a.name || '').trim().toLowerCase() === 'various artists';
      return '<a class="us-row" href="/artist/' + encodeURIComponent(a.name) + '">' +
        thumbHtml('/api/artist/image?name=' + encodeURIComponent(a.name), 'rounded-circle border border-secondary', a.name) +
        '<span class="us-row-main">' +
          '<span class="us-row-title d-block">' + esc(a.name) + '</span>' +
          '<span class="us-row-sub d-block">' + a.track_count + ' track' + (a.track_count === 1 ? '' : 's') + ' - ' + a.album_count + ' album' + (a.album_count === 1 ? '' : 's') + '</span>' +
        '</span>' +
        '<span class="us-row-meta badge bg-secondary-subtle text-secondary-emphasis">Artist</span>' +
        (isVarious ? '<span class="us-row-meta badge bg-secondary ms-1" title="Compilation placeholder entity">Compilation</span>' : '') +
      '</a>';
    }).join('');
  }

  function trackRows(tracks) {
    return tracks.map(function (t) {
      return '<a class="us-row" href="/track/' + encodeURIComponent(t.id) + '">' +
        '<span class="us-row-icon"><i class="bi bi-music-note"></i></span>' +
        '<span class="us-row-main">' +
          '<span class="us-row-title d-block">' + esc(t.title) + '</span>' +
          '<span class="us-row-sub d-block">' + esc(t.artist) + (t.album ? ' - ' + esc(t.album) : '') + '</span>' +
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
      // 3rd line: track count (+ total duration) — instant promo-vs-full check.
      var trackLine = '';
      if (it.track_count) {
        trackLine = '<span class="us-row-sub d-block">' + it.track_count + ' track' + (it.track_count === 1 ? '' : 's') + (it.duration_total ? ' - ' + fmtDuration(it.duration_total) : '') + '</span>';
      }
      if (it.local) {
        var yearPath = it.year ? '/' + it.year : '';
        var href = '/album/' + encodeURIComponent(it.artist) + '/' + encodeURIComponent(it.title) + yearPath;
        // Local album art endpoint (same pattern as the album page hero).
        var localArt = '/api/album/' + encodeURIComponent(it.artist) + '/' + encodeURIComponent(it.title) + '/art';
        html += '<a class="us-row" href="' + href + '">' +
          thumbWithBadge(thumbHtml(localArt, 'rounded border border-secondary', it.title),
            '<i class="bi bi-music-note-fill"></i>', 'accent-library-bg') +
          '<span class="us-row-main">' +
            '<span class="us-row-title d-block">' + esc(it.title) + yearSuffix + '</span>' +
            '<span class="us-row-sub d-block"><span class="us-artist">By ' + esc(it.artist) + '</span> · <span class="us-type">' + esc(it.typeLabel || 'Album') + '</span></span>' +
            trackLine +
          '</span>' +
        '</a>';
      } else if (it.owned) {
        // MusicBrainz release that is ALREADY in the collection: no queue
        // button — selecting it goes to the album page in the library.
        var yearPath = it.year ? '/' + it.year : '';
        var ownedHref = '/album/' + encodeURIComponent(it.artist) + '/' + encodeURIComponent(it.title) + yearPath;
        var ownedArt = '/api/album/' + encodeURIComponent(it.artist) + '/' + encodeURIComponent(it.title) + '/art';
        html += '<a class="us-row" href="' + ownedHref + '">' +
          thumbWithBadge(thumbHtml(ownedArt, 'rounded border border-secondary', it.title),
            '<i class="bi bi-music-note-fill"></i>', 'accent-library-bg') +
          '<span class="us-row-main">' +
            '<span class="us-row-title d-block">' + esc(it.title) + yearSuffix + '</span>' +
            '<span class="us-row-sub d-block"><span class="us-artist">By ' + esc(it.artist) + '</span> · <span class="us-type">' + esc(it.typeLabel || 'Album') + '</span></span>' +
            trackLine +
          '</span>' +
        '</a>';
      } else {
        var id = it.id || '';
        var queued = !!_queuedIds[id];
        _mbIndex[id] = it.release || null;
        // Cover Art Archive thumbnail — the MB search payload already carries
        // cover_art_url; fall back to building it from the release-group id.
        var rel = it.release || {};
        var cover = rel.cover_art_url || (id ? 'https://coverartarchive.org/release-group/' + id + '/front-250' : '');
        // The row links to the MusicBrainz release page; the Queue button
        // stops the navigation via the delegated handler's preventDefault.
        var mbUrl = id ? 'https://musicbrainz.org/release-group/' + encodeURIComponent(id) : '#';
        html += '<a class="us-row" href="' + mbUrl + '" target="_blank" rel="noopener">' +
          thumbWithBadge(thumbHtml(cover, 'rounded border border-secondary', it.title),
            '<i class="bi bi-hexagon-fill"></i>', 'accent-mb-bg') +
          '<span class="us-row-main">' +
            '<span class="us-row-title d-block">' + esc(it.title) + yearSuffix + '</span>' +
            '<span class="us-row-sub d-block"><span class="us-artist">By ' + esc(it.artist) + '</span> · <span class="us-type">' + esc(it.typeLabel || 'Release') + '</span></span>' +
            trackLine +
          '</span>' +
          (withQueue ? (queued
            ? '<button class="btn btn-sm btn-success" disabled title="Already queued"><i class="bi bi-check2"></i> Queued</button>'
            : '<button class="btn btn-sm btn-outline-primary us-queue-btn" data-mbid="' + esc(id) + '" title="Queue download via Soulseek"><i class="bi bi-download"></i> Queue</button>')
            : '') +
        '</a>';
      }
    }
    return html;
  }

  // Owned-key: normalized artist::album used to detect MB releases that are
  // already in the collection (parenthetical noise stripped, case/space
  // insensitive) — those get no queue button and link to the local album.
  function _ownedKey(artist, title) {
    function norm(s) {
      return String(s || '').toLowerCase().replace(/\([^)]*\)/g, ' ').replace(/\s+/g, ' ').trim();
    }
    return norm(artist) + '::' + norm(title);
  }

  function buildBuckets(local, mbReleases) {
    var buckets = { albums: [], compilations: [], live_albums: [], eps: [], singles: [] };
    var owned = {};
    (local.albums || []).forEach(function (al) {
      var bucket = buckets[al.type] ? al.type : 'albums';
      buckets[bucket].push({
        title: al.album, artist: al.artist, year: al.year || null,
        typeLabel: al.type_label || 'Album', local: true,
        track_count: al.track_count || null, duration_total: al.duration_total || null
      });
      owned[_ownedKey(al.artist, al.album)] = true;
    });
    (mbReleases || []).forEach(function (r) {
      var artist = mbReleaseArtist(r);
      var bucket = classifyMbRelease(r);
      buckets[bucket].push({
        title: r.title, artist: artist, year: mbReleaseYear(r),
        typeLabel: bucketTypeLabel(bucket), local: false, id: r.id || '', release: r,
        track_count: r.track_count || null,
        owned: !!owned[_ownedKey(artist, r.title)]
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
      html += buildSection(
        def.label + ' <span class="badge bg-secondary ms-1">' + items.length + '</span>',
        items,
        function (list) { return releaseRows(list, withQueue); }
      );
    }
    return html;
  }

  function renderBucketedResults(local, mbReleases, query, opts) {
    opts = opts || {};
    var html = '';
    var artists = local.artists || [];
    if (artists.length) html += buildSection('Artists', artists, artistRows);

    html += renderReleaseSections(buildBuckets(local, mbReleases), opts.withQueue);

    if (opts.allMbButton && mbReleases.length) {
      html += '<div class="text-center my-2">' +
        '<button type="button" class="btn btn-sm btn-outline-info" onclick="openUnifiedSearch(\'mb\')">' +
        '<i class="bi bi-search"></i> All MusicBrainz results</button></div>';
    }

    var tracks = local.tracks || [];
    if (tracks.length) html += buildSection('Tracks', tracks, trackRows);
    if (!artists.length && !html) {
      html += '<div class="text-center text-muted py-4 small">No library matches for "' + esc(query) + '"</div>';
    }
    return html;
  }

  function renderMbTab(local, releases, query) {
    if (!releases.length) {
      return '<div class="text-center text-muted py-4"><i class="bi bi-hexagon" style="font-size:2rem;"></i>' +
        '<p class="mt-2 mb-0 small">No MusicBrainz releases found for "' + esc(query) + '"</p></div>';
    }
    return renderReleaseSections(buildBuckets(local || {}, releases), true);
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
      markRendered(query);
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

    // Advanced filters drive MusicBrainz discovery, but they must also feed
    // the library search: when the main bar is empty, the first populated
    // field (artist → album → track → year) becomes the local query so an
    // artist-only filter still returns library artists (previously the
    // library search never ran and showed 'No library matches for ""').
    var localQuery = query;
    if (localQuery.length < MIN_QUERY_LENGTH && hasAdvanced) {
      localQuery = adv.artist || adv.album || adv.track || adv.year || '';
    }
    var localPromise = localQuery.length >= MIN_QUERY_LENGTH
      ? fetchLibrary(localQuery).catch(function (e) { return { error: e.message }; })
      : Promise.resolve({ artists: [], albums: [], compilations: [], live_albums: [], eps: [], singles: [], tracks: [] });

    var mbPromise;
    if (_scope === SCOPE_ALL) {
      mbPromise = fetchMb(query, MB_LIMIT_ALL_TAB, mbOpts);
    } else if (_scope === SCOPE_LIBRARY) {
      // Badge-only MB count so the scope pills stay live without blocking
      // the local render or the fast keystroke debounce.
      mbPromise = fetchMb(query, MB_LIMIT_MB_TAB, mbOpts)
        .then(function (r) {
          if (seq !== _runSeq) return [];
          _counts.mb = r.length;
          _counts.all = _counts.library + _counts.mb;
          updateScopeCounts();
          return [];
        })
        .catch(function () { return []; });
    } else {
      // MusicBrainz scope — the tab IS the full MB result list, so the
      // search must actually run here (previously it resolved to [] and the
      // tab always showed "No MusicBrainz releases found").
      mbPromise = fetchMb(query, MB_LIMIT_MB_TAB, mbOpts);
    }

    Promise.all([localPromise, mbPromise])
      .then(function (results) {
        if (seq !== _runSeq) return;
        var local = results[0];
        var mbReleases = results[1];

        // The Type filter applies to local results too: artists/tracks are
        // suppressed and only matching album buckets are kept, so the scope
        // pill counts reflect the filtered subset.
        local = filterLocalByType(local, getTypeFilter());

        _counts.library = countLibrary(local);
        if (_scope !== SCOPE_LIBRARY) _counts.mb = mbReleases.length;
        _counts.all = _counts.library + _counts.mb;
        updateScopeCounts();

        // Empty-state messages should quote the query that actually ran
        // (the filter-derived one when the main bar was empty).
        var displayQuery = localQuery || query;

        if (_scope === SCOPE_MB) {
          if (getMetaEl()) getMetaEl().textContent = mbReleases.length + ' musicbrainz result' + (mbReleases.length === 1 ? '' : 's');
          resultsEl.innerHTML = renderMbTab(local, mbReleases, displayQuery);
          markRendered(query);
          return;
        }

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
        var warnHtml = local.error
          ? '<div class="alert alert-warning py-2 small mb-2"><i class="bi bi-exclamation-triangle-fill"></i> Library search failed (' + esc(local.error) + ') — showing MusicBrainz only.</div>'
          : '';
        resultsEl.innerHTML = warnHtml + renderBucketedResults(local, mbReleases, displayQuery, {
          withQueue: true,
          allMbButton: _scope === SCOPE_ALL
        });
        markRendered(query);
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
    var artist = mbReleaseArtist(rel);

    // Open the Release Picker flyout first — the user picks the exact
    // version; the onQueued callback then marks this row as queued.
    if (typeof window.openReleasePicker === 'function') {
      window.openReleasePicker(id, rel.title || '', artist, function () {
        _queuedIds[id] = true;
        btn.classList.replace('btn-outline-primary', 'btn-success');
        btn.innerHTML = '<i class="bi bi-check2"></i> Queued';
        btn.disabled = true;
      });
      return;
    }

    _queuedIds[id] = true;

    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status"></span>';

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
        var total = out.data.total_tracks || out.data.queued_tracks || 0;
        btn.classList.replace('btn-outline-primary', 'btn-success');
        btn.innerHTML = '<i class="bi bi-check2"></i> Queued';
        btn.title = total ? ('Queued ' + total + ' track' + (total === 1 ? '' : 's') + ': ' + (rel.title || '')) : ('Queued: ' + (rel.title || ''));
        if (typeof window.showQueueToast === 'function') window.showQueueToast(rel.title || 'Release');
        // The backend falls back to a single search-item when the MusicBrainz
        // data can't be fetched — surface that so it's not mistaken for a
        // full tracklist queue.
        var msg = String(out.data.message || '');
        if (!total && msg.indexOf('simple search') !== -1) notifyError(msg);
      })
      .catch(function (e) {
        _queuedIds[id] = false;
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-plus-lg"></i> Queue';
        notifyError('Queue failed: ' + e.message);
      });
  }

  // ===== Modal lifecycle / public API =====

  // Advanced-filters panel visibility (flyout always renders it; collapse/
  // expand is driven by the open/Enter/toggle paths).  Defined at IIFE scope
  // so ``window.openUnifiedSearch`` can call it — a nested declaration inside
  // the DOMContentLoaded callback is not in its scope chain.
  function setAdvancedFiltersVisible(visible) {
    var panel = document.getElementById('unifiedAdvancedFilters');
    var toggle = document.getElementById('unifiedFiltersToggle');
    var icon = document.getElementById('unifiedFiltersToggleIcon');
    if (!panel) return;
    panel.classList.toggle('d-none', !visible);
    if (icon) icon.className = 'bi bi-chevron-' + (visible ? 'up' : 'right');
    if (toggle) toggle.setAttribute('aria-expanded', visible ? 'true' : 'false');
  }

  window.openUnifiedSearch = function (scope, prefill) {
    var modalEl = getModalEl();
    var input = getInputEl();
    if (!modalEl || !input) return;

    if (prefill === undefined || prefill === null) {
      var navEl = document.getElementById('navSearchInput');
      var dashEl = document.getElementById('dashboardTopSearchInput');
      prefill = (navEl && navEl.value) || (dashEl && dashEl.value) || '';
    }

    // Preserve the active scope across close/reopen (only full page loads
    // reset search state — exactly as the "navigate away" rule intends).
    selectScope(scope || _scope);
    input.value = prefill;
    // Advanced filters default to EXPANDED each time the panel opens.
    setAdvancedFiltersVisible(true);
    if (getErrorEl()) getErrorEl().classList.add('d-none');

    // Anchor the flyout directly under the fixed navbar (measured so the
    // two-row header never overlaps the panel).
    var nav = document.querySelector('nav.navbar.fixed-top') || document.querySelector('nav.navbar');
    if (nav && nav.offsetHeight > 0) modalEl.style.top = nav.offsetHeight + 'px';
    var backdrop = document.getElementById('searchBackdrop');
    modalEl.classList.remove('d-none');
    if (backdrop) backdrop.classList.remove('d-none');

    // Focus the pill itself — it IS the search entry now; the flyout input
    // mirrors it so existing key handlers (Enter, debounce) keep working.
    var navInput = document.getElementById('navSearchInput');
    if (navInput && navInput !== document.activeElement) navInput.focus();

    // Instant return: the previous query + scope are still rendered in the
    // DOM — skip the refetch (and its spinner) entirely.
    if (prefill === _lastQuery && _scope === _lastScope) return;
    clearTimeout(_debounceTimer);
    runSearch();
  };

  window.closeUnifiedSearch = function () {
    var modalEl = getModalEl();
    if (!modalEl) return;
    modalEl.classList.add('d-none');
    var backdrop = document.getElementById('searchBackdrop');
    if (backdrop) backdrop.classList.add('d-none');
  };

  // The navbar/dashboard pill is the live search entry: mirror its value
  // into the flyout input and run the debounced query (the flyout stays
  // open underneath so results appear directly below the navbar).
  window.syncNavSearchQuery = function () {
    var navEl = document.getElementById('navSearchInput');
    var dashEl = document.getElementById('dashboardTopSearchInput');
    var input = getInputEl();
    if (!input) return;
    var value = (navEl && navEl.value) || (dashEl && dashEl.value) || '';
    if (input.value !== value) input.value = value;
    if (getErrorEl()) getErrorEl().classList.add('d-none');
    clearTimeout(_debounceTimer);
    scheduleSearch();
  };

  // ===== Wiring =====

  document.addEventListener('DOMContentLoaded', function () {
    var modalEl = getModalEl();
    var input = getInputEl();
    var resultsEl = getResultsEl();
    if (!modalEl || !input || !resultsEl) return;

    // Escape closes the flyout (no Bootstrap modal to do it for us anymore).
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !modalEl.classList.contains('d-none')) {
        closeUnifiedSearch();
        input.blur();
      }
    });

    // Debounced input (per-scope rate handled in scheduleSearch).
    input.addEventListener('input', function () {
      // Keep the navbar pill in sync when typing inside the flyout.
      var navEl = document.getElementById('navSearchInput');
      if (navEl && navEl.value !== input.value) navEl.value = input.value;
      var dashEl = document.getElementById('dashboardTopSearchInput');
      if (dashEl && dashEl.value !== input.value) dashEl.value = input.value;
      scheduleSearch();
    });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        clearTimeout(_debounceTimer);
        runSearch();
        // Submit collapses the advanced filters so results own the panel.
        setAdvancedFiltersVisible(false);
      }
    });

    // Auto-select existing text on focus so a fresh query is one Backspace
    // away (Esc / backdrop / X close WITHOUT clearing — state is preserved).
    input.addEventListener('focus', function () { this.select(); });

    // Advanced filter fields + type dropdown re-run the search (filters are
    // always visible in the flyout — no collapse toggle).
    var filterInputs = document.querySelectorAll('#unifiedAdvancedFilters input, #unifiedSearchType');
    for (var i = 0; i < filterInputs.length; i++) {
      // Auto-select text on focus — inputs only; the type <select> has no
      // select() method (this.select would throw TypeError).
      filterInputs[i].addEventListener('focus', function () {
        if (typeof this.select === 'function') this.select();
      });
      filterInputs[i].addEventListener('input', function () {
        clearTimeout(_debounceTimer);
        _debounceTimer = setTimeout(runSearch, _scope === SCOPE_MB ? MB_DEBOUNCE_MS : LOCAL_DEBOUNCE_MS);
      });
      filterInputs[i].addEventListener('change', function () {
        clearTimeout(_debounceTimer);
        runSearch();
      });
      // Enter in a filter field submits AND collapses the advanced panel.
      filterInputs[i].addEventListener('keydown', function (e) {
        if (e.key === 'Enter') {
          e.preventDefault();
          clearTimeout(_debounceTimer);
          runSearch();
          setAdvancedFiltersVisible(false);
        }
      });
    }

    // ── Advanced filters collapse / expand ──────────────────────────────
    var filtersToggle = document.getElementById('unifiedFiltersToggle');
    if (filtersToggle) {
      filtersToggle.addEventListener('click', function () {
        var panel = document.getElementById('unifiedAdvancedFilters');
        setAdvancedFiltersVisible(!panel || panel.classList.contains('d-none'));
      });
    }

    // ── Release-type filter bottom sheet (replaces the old Type row) ────
    function updateFilterButtonState() {
      var select = document.getElementById('unifiedSearchType');
      var btn = document.getElementById('unifiedFilterBtn');
      var badge = document.getElementById('unifiedFilterBadge');
      if (!select || !btn) return;
      var active = select.value !== '';
      btn.classList.toggle('active', active);
      if (badge) {
        if (active) {
          var opt = select.options[select.selectedIndex];
          badge.textContent = opt ? opt.text : '';
          badge.classList.remove('d-none');
        } else {
          badge.classList.add('d-none');
        }
      }
    }

    function closeUnifiedFilterSheet() {
      var sheet = document.getElementById('usFilterSheet');
      var backdrop = document.getElementById('usFilterBackdrop');
      if (sheet) sheet.remove();
      if (backdrop) backdrop.remove();
    }
    window.closeUnifiedFilterSheet = closeUnifiedFilterSheet;

    function openUnifiedFilterSheet() {
      var select = document.getElementById('unifiedSearchType');
      if (!select) return;
      closeUnifiedFilterSheet();

      var backdrop = document.createElement('div');
      backdrop.className = 'us-filter-backdrop';
      backdrop.id = 'usFilterBackdrop';
      backdrop.addEventListener('click', closeUnifiedFilterSheet);

      var sheet = document.createElement('div');
      sheet.className = 'us-filter-sheet';
      sheet.id = 'usFilterSheet';
      var html = '<div class="us-filter-sheet-header d-flex justify-content-between align-items-center px-3 py-2">' +
        '<strong><i class="bi bi-funnel me-1"></i>Release Type</strong>' +
        '<button type="button" class="btn-close" aria-label="Close" onclick="closeUnifiedFilterSheet()"></button></div>';
      Array.prototype.forEach.call(select.options, function (opt) {
        var value = opt.value || '';
        var active = select.value === value ? ' active' : '';
        html += '<button type="button" class="us-filter-option' + active + '" data-type="' + value + '">' +
          '<span>' + opt.text + '</span>' +
          (value === '' ? '' : '<i class="bi bi-check-lg us-filter-check"></i>') +
          '</button>';
      });
      sheet.innerHTML = html;
      document.body.appendChild(backdrop);
      document.body.appendChild(sheet);
      requestAnimationFrame(function () { sheet.classList.add('show'); });

      sheet.querySelectorAll('.us-filter-option').forEach(function (btn) {
        btn.addEventListener('click', function () {
          select.value = btn.getAttribute('data-type') || '';
          updateFilterButtonState();
          closeUnifiedFilterSheet();
          clearTimeout(_debounceTimer);
          runSearch();
        });
      });
    }
    window.openUnifiedFilterSheet = openUnifiedFilterSheet;
    var filterBtn = document.getElementById('unifiedFilterBtn');
    if (filterBtn) filterBtn.addEventListener('click', openUnifiedFilterSheet);
    updateFilterButtonState();

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