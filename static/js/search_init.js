/**
 * Unified search page initialization — event wiring, Soulseek auto-search from URL.
 * Depends on: downloads.js, playlist_import.js
 */

// ── Event Delegation (XSS-safe) ──
document.addEventListener('click', function (e) {
  var addBtn = e.target.closest('.js-add-track-btn');
  if (addBtn) {
    var p = _trackPayloads[addBtn.dataset.pid];
    if (p) addSelectedTrack(p.title, p.artist, p.album);
    return;
  }
  var slskdBtn = e.target.closest('.js-slskd-search-btn');
  if (slskdBtn) {
    var p2 = _trackPayloads[slskdBtn.dataset.pid];
    if (p2) searchTrackInSoulseek(p2.query, slskdBtn);
    return;
  }
  var replaceBtn = e.target.closest('.js-replace-btn');
  if (replaceBtn) {
    var p3 = _trackPayloads[replaceBtn.dataset.pid];
    if (p3) openReplacementTrackModal(p3.artist, p3.title, p3.album);
    return;
  }
});

// ── Initialization ──
document.addEventListener('DOMContentLoaded', function () {
  // MusicBrainz tab: wire up buttons
  var mbSearchBtn = document.getElementById('mbSearchBtn');
  if (mbSearchBtn) mbSearchBtn.addEventListener('click', performMbSearch);

  var mbRefreshBtn = document.getElementById('mbRefreshBtn');
  if (mbRefreshBtn) mbRefreshBtn.addEventListener('click', refreshMbDownloads);

  // Soulseek: auto-search from URL query parameter
  var params = new URLSearchParams(window.location.search);
  var queryParam = params.get('q');
  if (queryParam) {
    var searchInput = document.getElementById('slskdSearchQuery');
    if (searchInput) {
      searchInput.value = typeof normalizeSoulseekQuery === 'function'
        ? normalizeSoulseekQuery(queryParam)
        : queryParam;
      setTimeout(function () {
        var form = document.getElementById('slskdSearchForm');
        if (form) form.dispatchEvent(new Event('submit'));
      }, 100);
    }
  }
});
