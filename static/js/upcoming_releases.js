/**
 * UpcomingReleasesService — shared front-end logic for the upcoming releases
 * UI (dedicated page, download monitor card, dashboard).
 *
 * Single source of truth for:
 *   - fetching paginated releases from /api/upcoming-releases
 *   - triggering the Wikipedia scrape + background MusicBrainz refresh
 *   - live progress polling (renderProgressBadge + /scrape/status)
 *   - matching releases to MusicBrainz (server-side fallback when no MBID)
 *   - queueing released albums (POST /api/downloads/queue-upcoming)
 *   - rendering the month-grouped accordion table
 */
(function () {
  'use strict';

  var state = {
    items: [],
    filters: { source: 'all', page: 1, limit: 50 },
    total: 0,
    hasMore: false,
  };

  /* A page can hook this to re-render after service mutations (match/queue). */
  var onRefresh = null;

  function escapeHtml(text) {
    if (text === null || text === undefined) return '';
    var div = document.createElement('div');
    div.appendChild(document.createTextNode(String(text)));
    return div.innerHTML;
  }

  function encodeInlineArg(value) {
    return encodeURIComponent(JSON.stringify(value === null || value === undefined ? '' : value));
  }

  function decodeInlineArg(value, fallback) {
    try {
      var decoded = decodeURIComponent(value || '');
      try { return JSON.parse(decoded); } catch (_e) { return decoded || fallback; }
    } catch (_e) { return fallback; }
  }

  async function fetchJson(url, options, timeoutMs) {
    var opts = options || {};
    var controller = null;
    if (timeoutMs) {
      controller = new AbortController();
      opts = Object.assign({}, opts, { signal: controller.signal });
      setTimeout(function () { controller.abort(); }, timeoutMs);
    }
    var resp = await fetch(url, opts);
    var data = null;
    try { data = await resp.json(); } catch (_e) { /* no body */ }
    if (!resp.ok) {
      throw new Error((data && data.error) || ('Request failed (HTTP ' + resp.status + ')'));
    }
    return data;
  }

  // ------------------------------------------------------------------
  // API
  // ------------------------------------------------------------------

  async function fetchReleases(params) {
    var filters = Object.assign({}, state.filters, params || {});
    var qs = new URLSearchParams();
    if (filters.filter && filters.filter !== 'all') qs.set('filter', filters.filter);
    if (filters.include_queue) qs.set('include_queue', 'true');
    qs.set('page', filters.page || 1);
    qs.set('limit', filters.limit || 50);

    var data = await fetchJson('/api/upcoming-releases?' + qs.toString(), {}, 30000);
    state.items = data.releases || [];
    state.total = data.total || 0;
    state.hasMore = !!data.has_more;
    state.filters = filters;
    return data;
  }

  async function triggerScrape() {
    return fetchJson('/api/upcoming-releases/scrape', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    }, 120000);
  }

  async function fetchScrapeStatus() {
    return fetchJson('/api/upcoming-releases/scrape/status', {}, 10000);
  }

  /**
   * Match a release to a MusicBrainz release-group.
   * When `mbid` is null the server falls back to its own MB search.
   */
  async function matchRelease(releaseId, mbid) {
    var body = mbid ? { release_group_mbid: mbid } : {};
    var resp = await fetch('/api/upcoming-releases/' + releaseId + '/match', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    var data = null;
    try { data = await resp.json(); } catch (_e) { /* empty body */ }
    if (!resp.ok || !data || !data.success) {
      throw new Error((data && data.error) || 'Match failed');
    }
    return data;
  }

  /** Queue a released album (album-typed download_queue item). */
  async function queueDownload(id) {
    var data = await fetchJson('/api/downloads/queue-upcoming', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ upcoming_release_id: id }),
    }, 30000);
    if (!data.success) throw new Error(data.error || 'Queue failed');
    return data;
  }

  // ------------------------------------------------------------------
  // Row rendering
  // ------------------------------------------------------------------

  function sourceBadge(release) {
    var isMusicBrainz = String(release.source || '').toLowerCase().includes('musicbrainz');
    return isMusicBrainz
      ? '<span class="badge bg-info"><i class="bi bi-hexagon-fill"></i> MusicBrainz</span>'
      : '<span class="badge bg-secondary"><i class="bi bi-wikipedia"></i> Wikipedia</span>';
  }

  /** True when the album is out (release_date <= today) and thus queueable. */
  function isReleased(release) {
    var dateText = (release.release_date || '').trim();
    if (!/^\d{4}-\d{2}-\d{2}/.test(dateText)) return false;
    return dateText <= new Date().toISOString().slice(0, 10);
  }

  function renderReleaseRow(release) {
    var releaseDate = release.release_date || 'TBA';
    var dateBadge = isReleased(release)
      ? '<span class="badge bg-primary">Out now</span>'
      : '<span class="badge bg-success">Upcoming</span>';

    var artistEnc = encodeInlineArg(release.artist_name || '');
    var albumEnc = encodeInlineArg(release.album_name || '');
    var releaseId = Number(release.id) || 0;

    var actions = [];
    actions.push(
      '<button type="button" class="btn btn-sm btn-outline-info" ' +
      'onclick="UpcomingReleasesService.searchFromEncoded(\'' + artistEnc + '\', \'' + albumEnc + '\')" ' +
      'title="Search / Download on MusicBrainz"><i class="bi bi-search"></i></button>'
    );

    if (!release.release_group_mbid) {
      actions.push(
        '<button type="button" class="btn btn-sm btn-outline-warning" ' +
        'onclick="UpcomingReleasesService.autoMatchById(' + releaseId + ', this)" ' +
        'title="Auto-match with MusicBrainz"><i class="bi bi-magic"></i></button>'
      );
    }

    if (release.in_queue) {
      actions.push('<span class="badge bg-info text-dark align-middle">In Queue</span>');
    } else if (isReleased(release)) {
      actions.push(
        '<button type="button" class="btn btn-sm btn-success" ' +
        'onclick="UpcomingReleasesService.queueById(' + releaseId + ', this)" ' +
        'title="Queue download (release is out)"><i class="bi bi-download"></i> Queue</button>'
      );
    }

    var mbidCell = release.release_group_mbid
      ? '<code class="small">' + escapeHtml(String(release.release_group_mbid).slice(0, 8)) + '…</code>'
      : '<span class="text-muted small">unmatched</span>';

    return '<tr>' +
      '<td>' + escapeHtml(release.artist_name || '') + '</td>' +
      '<td>' + escapeHtml(release.album_name || '') + '</td>' +
      '<td><small>' + escapeHtml(releaseDate) + ' ' + dateBadge + '</small></td>' +
      '<td>' + sourceBadge(release) + '</td>' +
      '<td>' + mbidCell + '</td>' +
      '<td><div class="d-flex gap-1 flex-wrap">' + actions.join('') + '</div></td>' +
      '</tr>';
  }

  /**
   * Render the month-grouped accordion table into a container element.
   * Compatible with the legacy monitor.js markup (accordion ids, table rows).
   */
  function renderTable(containerId, items, options) {
    var container = document.getElementById(containerId);
    if (!container) return;
    options = options || {};
    var releases = items || [];

    if (releases.length === 0) {
      container.innerHTML = options.emptyHtml ||
        '<div class="text-center py-4"><p class="text-muted mb-0">' +
        (options.emptyMessage || 'No upcoming releases found.') + '</p></div>';
      return;
    }

    var grouped = {};
    releases.forEach(function (r) {
      var month = (r.release_date || 'Unknown Date').substring(0, 7);
      if (!grouped[month]) grouped[month] = [];
      grouped[month].push(r);
    });
    var sortedMonths = Object.keys(grouped).sort();

    var html = '<div class="accordion" id="upcomingReleaseAccordion">';
    sortedMonths.forEach(function (month, idx) {
      var monthReleases = grouped[month];
      var monthLabel = new Date(month + '-01').toLocaleDateString('en-US', { year: 'numeric', month: 'long' });
      html += '<div id="upcomingMonthCard' + idx + '" class="accordion-item">' +
        '<h2 class="accordion-header">' +
        '<button class="accordion-button collapsed" type="button" data-bs-toggle="collapse" data-bs-target="#ucm' + idx + '">' +
        '<strong>' + escapeHtml(monthLabel) + '</strong>' +
        '<span class="badge bg-primary ms-2">' + monthReleases.length + '</span>' +
        '</button></h2>' +
        '<div id="ucm' + idx + '" class="accordion-collapse collapse">' +
        '<div class="accordion-body p-0"><div class="table-responsive">' +
        '<table class="table table-hover table-striped table-dark table-sm mb-0">' +
        '<thead><tr><th>Artist</th><th>Album</th><th>Date</th><th>Source</th><th>MBID</th><th>Action</th></tr></thead>' +
        '<tbody>';
      monthReleases.forEach(function (release) {
        html += renderReleaseRow(release);
      });
      html += '</tbody></table></div></div></div></div>';
    });
    html += '</div>';
    container.innerHTML = html;
  }

  /**
   * Badge HTML for the scrape/refresh status — e.g.
   * `Refreshing... (142/500) · Mudvayne`. Empty when not running.
   */
  function renderProgressBadge(statusData) {
    var s = statusData || {};
    if (s.status !== 'running') return '';
    var progress = s.total > 0
      ? Math.min(Number(s.progress) || 0, Number(s.total)) + '/' + s.total
      : String(s.progress || 0);
    var artist = s.current_artist ? ' · ' + escapeHtml(s.current_artist) : '';
    return '<span class="badge bg-info text-dark"><i class="bi bi-arrow-repeat"></i> Refreshing... (' + progress + ')' + artist + '</span>';
  }

  // ------------------------------------------------------------------
  // UI actions (onclick targets)
  // ------------------------------------------------------------------

  function searchFromEncoded(artistEnc, albumEnc) {
    var fn = window.searchMusicBrainzReleaseFromEncoded ||
      function () { console.warn('searchMusicBrainzReleaseFromEncoded unavailable'); };
    fn(null, artistEnc, albumEnc);
  }

  async function autoMatchById(releaseId, buttonEl) {
    if (!releaseId) return;
    if (buttonEl) {
      buttonEl.disabled = true;
      buttonEl.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
    }
    try {
      await matchRelease(releaseId, null);
    } catch (error) {
      if (buttonEl) {
        buttonEl.disabled = false;
        buttonEl.innerHTML = '<i class="bi bi-magic"></i>';
      }
      alert('Error matching: ' + error.message);
      return;
    }
    if (onRefresh) onRefresh();
  }

  async function queueById(releaseId, buttonEl) {
    if (!releaseId) return;
    if (buttonEl) {
      buttonEl.disabled = true;
      buttonEl.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
    }
    try {
      var result = await queueDownload(releaseId);
      if (buttonEl && result.already_queued) {
        alert('Already in queue.');
      }
    } catch (error) {
      if (buttonEl) {
        buttonEl.disabled = false;
        buttonEl.innerHTML = '<i class="bi bi-download"></i> Queue';
      }
      alert('Error queueing: ' + error.message);
      return;
    }
    if (onRefresh) onRefresh();
  }

  window.UpcomingReleasesService = {
    state: state,
    set onRefresh(fn) { onRefresh = fn; },
    fetchReleases: fetchReleases,
    triggerScrape: triggerScrape,
    fetchScrapeStatus: fetchScrapeStatus,
    matchRelease: matchRelease,
    queueDownload: queueDownload,
    renderTable: renderTable,
    renderProgressBadge: renderProgressBadge,
    searchFromEncoded: searchFromEncoded,
    autoMatchById: autoMatchById,
    queueById: queueById,
    isReleased: isReleased,
    escapeHtml: escapeHtml,
    encodeInlineArg: encodeInlineArg,
    decodeInlineArg: decodeInlineArg,
  };
})();