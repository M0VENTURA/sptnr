/**
 * Dashboard page JavaScript — scan controls, recent scans, upcoming releases, unified log.
 * Loaded separately from the esbuild bundle because it is page-specific.
 */

// Global error handler for development debugging
window.onerror = function (msg, url, line) {
  console.error("Error: " + msg + " on line " + line + " in " + url);
};

// ===== Shared helpers =====
async function postJSON(u, b) {
  const r = await fetch(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}) });
  return r.json().catch(() => ({}));
}

function fE(s) {
  if (!s) return "";
  return " — " + Math.floor(s / 60) + "m " + Math.floor(s % 60) + "s";
}

function escapeHtml(str) {
  if (!str) return "";
  var d = document.createElement("div");
  d.appendChild(document.createTextNode(str));
  return d.innerHTML;
}

// ===== Scan Card Polling =====
(async function () {
  try {
    var r = await fetch("/api/navidrome/scan/status"),
      d = await r.json(),
      e = document.getElementById("nav-connectivity");
    if (d.success !== false) {
      e.className = "badge bg-success ms-1";
      e.title = "OK";
      e.textContent = "\u2713";
    } else {
      e.className = "badge bg-danger ms-1";
      e.title = "Unreachable";
      e.textContent = "\u2717";
    }
  } catch (_) {}
})();

async function startPopularityScan(m) {
  await postJSON("/api/popularity/run", { mode: m || "popularity" });
}

async function stopPopularityScan() {
  await fetch("/scan/stop-popularity", { method: "POST" });
}

async function pollPopularityStatus() {
  var r = await fetch("/api/popularity/status"),
    d = await r.json();
  if (!d.success) return;
  var e = document.getElementById("pop-status"),
    b = document.getElementById("pop-progress-bar"),
    t = document.getElementById("pop-progress-text");
  if (d.running) {
    e.innerText = (d.message || "Running...") + fE(d.elapsed_seconds);
    e.className = "text-primary small";
    var p = Math.min(d.progress || 0, 100);
    b.style.width = p + "%";
    t.innerText = (d.mode || "popularity") + " \u2014 " + p + "% (" + (d.processed_items || 0) + "/" + (d.total_items || "?") + ")";
  } else {
    e.innerText = d.message || "Idle";
    e.className = "text-muted small";
    b.style.width = "0%";
    t.innerText = "";
  }
}

async function startNavidromeImport() {
  var d = await postJSON("/api/navidrome/import", { mode: "all" }),
    e = document.getElementById("nav-status");
  if (d.success) {
    e.innerText = d.message || "Started";
    e.className = "text-success small";
    e.dataset.started = String(Date.now());
  } else {
    e.innerText = d.error || "Failed";
    e.className = "text-danger small";
  }
}

async function startNavidromeServerScan() {
  var d = await postJSON("/api/navidrome/scan/start"),
    e = document.getElementById("nav-status");
  e.innerText = d.success ? "Server scan triggered" : "Failed";
  e.className = d.success ? "text-success small" : "text-danger small";
}

async function stopNavidromeSync() {
  await fetch("/scan/stop-navidrome", { method: "POST" });
}

async function pollNavidromeStatus() {
  var e = document.getElementById("nav-status"),
    b = document.getElementById("nav-progress-bar"),
    t = document.getElementById("nav-progress-text"),
    pd = await (await fetch("/api/scan-progress")).json().catch(() => ({})),
    ns = (pd.active_scans || []).find(function (s) { return s.scan_type === "navidrome_scan"; });
  if (ns && ns.is_running) {
    e.innerText = (ns.message || "Importing...") + fE(ns.elapsed_seconds);
    e.className = "text-primary small";
    var p = Math.min(ns.percent_complete || 0, 100);
    b.style.width = p + "%";
    t.innerText = (ns.mode || "import") + " \u2014 " + p + "%";
    if (e.dataset.started) { delete e.dataset.started; }
    return;
  }
  var sd = await (await fetch("/api/navidrome/scan/status")).json().catch(() => ({}));
  if (sd.scanning) {
    e.innerText = "Server scanning...";
    e.className = "text-warning small";
    b.style.width = "100%";
    t.innerText = "Count: " + (sd.count || 0);
    if (e.dataset.started) { delete e.dataset.started; }
  } else if (e.dataset.started) {
    var elapsed = Date.now() - parseInt(e.dataset.started, 10);
    if (elapsed < 120000) {
      e.innerText = "Importing...";
      e.className = "text-primary small";
    } else {
      delete e.dataset.started;
      e.innerText = "Idle";
      e.className = "text-muted small";
      b.style.width = "0%";
      t.innerText = "";
    }
  } else {
    e.innerText = "Idle";
    e.className = "text-muted small";
    b.style.width = "0%";
    t.innerText = "";
  }
}

async function startLibrarySync() {
  await fetch("/api/library/sync", { method: "POST" });
}

async function stopLibrarySync() {
  await fetch("/scan/stop-all", { method: "POST" });
}

async function pollLibraryStatus() {
  var r = await fetch("/api/library/status"),
    d = await r.json();
  if (!d.success) return;
  var e = document.getElementById("lib-status"),
    b = document.getElementById("lib-progress-bar"),
    t = document.getElementById("lib-progress-text");
  if (d.running) {
    var es = d.started_at ? " \u2014 " + Math.floor((Date.now() / 1000 - d.started_at) / 60) + "m" : "";
    e.innerText = (d.message || "Sync...") + es;
    e.className = "text-primary small";
    var tot = d.artists_total || 0,
      pro = d.artists_processed || 0,
      pct = tot > 0 ? Math.min(Math.round((pro / tot) * 100), 100) : 0;
    b.style.width = pct + "%";
    t.innerText = "diff \u2014 " + pct + "% (" + pro + "/" + tot + " artists, " + (d.tracks_attempted || 0) + " tracks)";
  } else {
    e.innerText = d.message || "Idle";
    e.className = "text-muted small";
    b.style.width = "0%";
    t.innerText = "";
  }
}

async function startEssentiaScan() {
  await fetch("/api/essentia/run", { method: "POST" });
}

async function stopEssentiaScan() {
  await fetch("/scan/stop-essentia-mood", { method: "POST" });
}

async function pollEssentiaStatus() {
  var e = document.getElementById("ess-status"),
    b = document.getElementById("ess-progress-bar"),
    t = document.getElementById("ess-progress-text"),
    pd = await (await fetch("/api/scan-progress")).json().catch(() => ({})),
    es = (pd.active_scans || []).find(function (s) { return s.scan_type === "essentia_mood_scan"; });
  if (es && es.is_running) {
    e.innerText = (es.message || "Running...") + fE(es.elapsed_seconds);
    e.className = "text-primary small";
    var p = Math.min(es.percent_complete || 0, 100);
    b.style.width = p + "%";
    t.innerText = "essentia \u2014 " + p + "%";
  } else {
    e.innerText = "Idle";
    e.className = "text-muted small";
    b.style.width = "0%";
    t.innerText = "";
  }
}

async function stopAllScans() {
  await fetch("/scan/stop-all", { method: "POST" });
}

// ===== Recent Scans (Dynamic) =====
var SCAN_TYPE_DISPLAY_NAMES = {
  // Session-level scan types (recorded by popularity_pipeline.py)
  navidrome: "Navidrome Import",
  metadata: "Metadata Scan",
  popularity: "Popularity Scan",
  singles: "Singles Detection",
  singles_detection: "Singles Detection",
  essentia: "Essentia Mood Scan",
  mood: "Mood Scan",
  combined: "Combined Scan",
  all: "Full Scan (All)",
  // Scan runner progress-file types (from run_scan / scan_stage_runner.py)
  navidrome_scan: "Navidrome Import",
  metadata_lookup_scan: "Metadata Scan",
  popularity_scan: "Popularity Scan",
  singles_scan: "Singles Detection",
  mood_scan: "Mood Scan",
  essentia_mood_scan: "Essentia Mood Scan",
  combined_scan: "Combined Scan",
  missing_releases_scan: "Missing Releases Scan",
};

var runningScans = new Set();
var _lastRecentScansPayload = null;
var _currentScanningAlbum = null;
var previousScanStates = {};

function parseScanTimestamp(ts) {
  if (!ts && ts !== 0) return null;
  if (ts instanceof Date) return isNaN(ts.getTime()) ? null : ts;
  if (typeof ts === "number") { var d = new Date(ts); return isNaN(d.getTime()) ? null : d; }
  var text = String(ts).trim();
  if (!text) return null;
  if (/^\d+$/.test(text)) {
    var n = Number(text);
    if (!Number.isNaN(n)) { var e = new Date(text.length <= 10 ? n * 1000 : n); if (!isNaN(e.getTime())) return e; }
  }
  var norm = text.replace(" ", "T").replace(/\.(\d{3})\d+/, ".$1").replace(/([+-]\d{2})(\d{2})$/, "$1:$2");
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,3})?$/.test(norm)) norm += "Z";
  var d2 = new Date(norm);
  return isNaN(d2.getTime()) ? null : d2;
}

function formatScanTimestamp(ts) {
  var d = parseScanTimestamp(ts);
  if (!d) return "\u2014";
  var now = Date.now();
  var diffMs = now - d.getTime();
  var diffSec = Math.floor(diffMs / 1000);
  var diffMin = Math.floor(diffSec / 60);
  var diffHr = Math.floor(diffMin / 60);
  var diffDay = Math.floor(diffHr / 24);
  var relative;
  if (diffSec < 60) relative = diffSec + "s ago";
  else if (diffMin < 60) relative = diffMin + "m ago";
  else if (diffHr < 24) relative = diffHr + "h ago";
  else if (diffDay < 7) relative = diffDay + "d ago";
  else relative = d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  var full = d.toLocaleString();
  return '<span title="' + escapeHtml(full) + '">' + escapeHtml(relative) + "</span>";
}

function _scanTs(scan) {
  return scan.scan_timestamp || scan.started_at || scan.timestamp || null;
}

function renderRecentScans(scans) {
  var body = document.getElementById("recent-scans-body");
  if (!body) return;
  if (!scans || scans.length === 0) {
    body.innerHTML = '<tr><td colspan="5" class="text-center text-muted">No recent scans yet</td></tr>';
    return;
  }
  var grouped = {};
  var inProgress = [];
  scans.forEach(function (scan) {
    var ts = _scanTs(scan);
    if (scan._inProgress) {
      inProgress.push({
        artist: scan.artist,
        album: scan.album || "\u2026",
        scan_types: [{ type: scan.scan_type, timestamp: ts, _inProgress: true }],
        latest_timestamp_obj: new Date(),
        _inProgress: true,
      });
      return;
    }
    var key = scan.artist + "|" + scan.album;
    if (!grouped[key]) {
      grouped[key] = {
        artist: scan.artist,
        album: scan.album,
        status: scan.status,
        scan_types: [],
        latest_timestamp_obj: parseScanTimestamp(ts),
      };
    }
    // Keep the most recent status (started → completed → failed)
    if (scan.status === "failed" || scan.status === "error") {
      grouped[key].status = "failed";
    } else if (scan.status === "completed" || scan.status === "complete") {
      if (grouped[key].status !== "failed") grouped[key].status = "completed";
    } else if (scan.status === "started" && !grouped[key].status) {
      grouped[key].status = "started";
    } else if (scan.status === "stopped" && grouped[key].status !== "failed" && grouped[key].status !== "completed") {
      grouped[key].status = "stopped";
    }
    if (!grouped[key].scan_types.find(function (s) { return s.type === scan.scan_type; })) {
      grouped[key].scan_types.push({ type: scan.scan_type, timestamp: ts });
    }
    var scanTime = parseScanTimestamp(ts);
    var currentLatest = grouped[key].latest_timestamp_obj;
    if (scanTime && (!currentLatest || scanTime > currentLatest)) {
      grouped[key].latest_timestamp = ts;
      grouped[key].latest_timestamp_obj = scanTime;
    }
  });
  var entries = inProgress.concat(
    Object.values(grouped).sort(function (a, b) {
      var aTs = a.latest_timestamp_obj ? a.latest_timestamp_obj.getTime() : 0;
      var bTs = b.latest_timestamp_obj ? b.latest_timestamp_obj.getTime() : 0;
      return bTs - aTs;
    })
  );
  var rows = entries
    .map(function (group) {
      if (group.artist === "_SCAN_SESSION_") {
        var typeKey = group.album || group.scan_types?.[0]?.type || "";
        var typeName = SCAN_TYPE_DISPLAY_NAMES[typeKey] || escapeHtml(typeKey);
        var typeIcon =
          typeKey === "navidrome" ? "bi-cloud"
          : typeKey === "popularity" ? "bi-graph-up"
          : typeKey === "singles" || typeKey === "singles_detection" ? "bi-star"
          : typeKey === "mood" || typeKey === "essentia" || typeKey === "essentia_mood" ? "bi-emoji-smile"
          : typeKey === "metadata" || typeKey === "metadata_lookup_scan" ? "bi-info-circle"
          : typeKey === "combined" || typeKey === "all" ? "bi-lightning-fill"
          : "bi-lightning-fill";
        var scanStatus = group.status || "started";
        var isRunning = scanStatus === "started";
        var statusIcon = isRunning ? "bi-hourglass-split text-primary" : scanStatus === "failed" ? "bi-x-circle-fill text-danger" : "bi-check-circle-fill text-success";
        var statusLabel = isRunning ? "running" : scanStatus === "failed" ? "failed" : "completed";
        var rowClass = isRunning ? ' class="table-primary"' : scanStatus === "failed" ? ' class="table-danger"' : ' class="table-success bg-opacity-10"';
        var spinnerHtml = isRunning ? '<span class="spinner-border spinner-border-sm me-1" style="width:.75em;height:.75em;"></span>' : '';
        return '<tr' + rowClass + '><td colspan="4" class="py-1 ps-3"><small class="' + (isRunning ? 'text-primary' : scanStatus === 'failed' ? 'text-danger' : 'text-success') + '">' + spinnerHtml + '<i class="bi ' + statusIcon + ' me-1"></i><strong>' + typeName + '</strong> ' + statusLabel + ' \u2014 ' + formatScanTimestamp(group.latest_timestamp) + '<span class="badge bg-' + (isRunning ? 'primary' : scanStatus === 'failed' ? 'danger' : 'success') + ' ms-2"><i class="bi ' + typeIcon + '"></i></span></small></td><td></td></tr>';
      }
      var artistUrl = "/artist/" + encodeURIComponent(group.artist);
      var albumUrl =
        group.album && group.album !== "\u2026"
          ? "/album/" + encodeURIComponent(group.artist) + "/" + encodeURIComponent(group.album)
          : artistUrl;
      var badges = group.scan_types
        .filter(function (st) { return st.type !== "covers"; })
        .map(function (st) {
          var badgeHtml =
            '<span class="badge bg-secondary" title="' + escapeHtml(st.timestamp || "") + '">' + escapeHtml(st.type) + "</span>";
          if (st._inProgress) {
            badgeHtml = '<span class="badge bg-primary"><span class="spinner-border spinner-border-sm me-1" style="width:.75em;height:.75em;"></span> Scanning\u2026</span>';
          } else if (st.type === "navidrome") {
            badgeHtml = '<span class="badge bg-primary" title="' + escapeHtml(st.timestamp || "") + '"><i class="bi bi-cloud"></i> Navidrome</span>';
          } else if (st.type === "popularity") {
            badgeHtml = '<span class="badge bg-success" title="' + escapeHtml(st.timestamp || "") + '"><i class="bi bi-graph-up"></i> Popularity</span>';
          } else if (st.type === "metadata" || st.type === "metadata_lookup_scan") {
            badgeHtml = '<span class="badge bg-info text-dark" title="' + escapeHtml(st.timestamp || "") + '"><i class="bi bi-info-circle"></i> Metadata</span>';
          } else if (st.type === "singles" || st.type === "singles_scan" || st.type === "single_detection") {
            badgeHtml = '<span class="badge bg-warning text-dark" title="' + escapeHtml(st.timestamp || "") + '"><i class="bi bi-star"></i> Singles</span>';
          } else if (st.type === "mood" || st.type === "mood_scan") {
            badgeHtml = '<span class="badge bg-secondary" title="' + escapeHtml(st.timestamp || "") + '"><i class="bi bi-emoji-smile"></i> Mood</span>';
          } else if (st.type === "essentia-mood" || st.type === "essentia_mood_scan") {
            badgeHtml = '<span class="badge" style="background-color:#6f42c1" title="' + escapeHtml(st.timestamp || "") + '"><i class="bi bi-cpu"></i> Essentia</span>';
          } else if (st.type === "combined") {
            badgeHtml = '<span class="badge bg-info text-dark" title="' + escapeHtml(st.timestamp || "") + '"><i class="bi bi-lightning-fill"></i> Combined</span>';
          } else if (st.type === "unified") {
            badgeHtml = '<span class="badge bg-secondary" title="' + escapeHtml(st.timestamp || "") + '"><i class="bi bi-layers"></i> Unified</span>';
          } else if (st.type === "metadata" || st.type === "metadata_scan" || st.type === "metadata_lookup_scan") {
            badgeHtml = '<span class="badge badge-musicbrainz" title="' + escapeHtml(st.timestamp || "") + '">Metadata</span>';
          }
          return badgeHtml;
        })
        .join(" ");
      var rowClass = group._inProgress ? ' class="table-active"' : "";
      return (
        "<tr" +
        rowClass +
        "><td><a href='" +
        artistUrl +
        "'>" +
        escapeHtml(group.artist) +
        "</a></td><td>" +
        (group.album && group.album !== "\u2026"
          ? "<a href='" + albumUrl + "'>" + escapeHtml(group.album) + "</a>"
          : '<span class="text-muted fst-italic">\u2026</span>') +
        "</td><td><div class='d-flex flex-wrap gap-2'>" +
        badges +
        "</div></td><td class='text-muted text-end'><small>" +
        (group._inProgress ? "now" : formatScanTimestamp(group.latest_timestamp)) +
        "</small></td><td class='text-center'>" +
        (!group._inProgress
          ? "<a href='" + albumUrl + "' class='btn btn-sm btn-outline-primary' title='View album'><i class='bi bi-arrow-right'></i></a>"
          : "") +
        "</td></tr>"
      );
    })
    .join("");
  body.innerHTML = rows;
}

function buildRecentScansWithInProgress(scans) {
  if (!_currentScanningAlbum) return scans;
  return [_currentScanningAlbum].concat(scans);
}

function updateRecentScans() {
  var endpoint = "/api/recent-scans";
  fetch(endpoint + "?_ts=" + Date.now(), { cache: "no-store" })
    .then(function (r) { return r.json(); })
    .then(function (data) {
      var payload = data.scans || [];
      if (payload.length > 0) {
        _lastRecentScansPayload = payload;
        renderRecentScans(buildRecentScansWithInProgress(payload));
      } else if (_lastRecentScansPayload && _lastRecentScansPayload.length > 0) {
        renderRecentScans(buildRecentScansWithInProgress(_lastRecentScansPayload));
      } else {
        renderRecentScans([]);
      }
    })
    .catch(function (error) {
      console.error("Error fetching recent scans:", error);
      if (_lastRecentScansPayload) {
        renderRecentScans(buildRecentScansWithInProgress(_lastRecentScansPayload));
      }
    });
}

// ===== Upcoming Releases Table helpers =====
async function addUpcomingReleaseToQueueDashboard(encodedArtist, encodedAlbum, encodedDate, buttonEl) {
  try {
    var artist = decodeURIComponent(encodedArtist || "").trim();
    var album = decodeURIComponent(encodedAlbum || "").trim();
    var dateText = decodeURIComponent(encodedDate || "").trim();
    if (!artist || !album) return;
    var year = /^\d{4}/.test(dateText) ? dateText.slice(0, 4) : "";
    if (buttonEl) { buttonEl.disabled = true; buttonEl.innerHTML = '<span class="spinner-border spinner-border-sm" role="status"></span>'; }
    var result = await postJSON("/api/queue/add", {
      artist: artist,
      title: album,
      album: album,
      import_type: "album",
      source: "qbittorrent",
      release_source: "dashboard_upcoming",
      priority: 5,
      is_album: true,
      year: year || null,
    });
    if (buttonEl) {
      buttonEl.disabled = true;
      buttonEl.classList.remove("btn-outline-primary");
      buttonEl.classList.add("btn-outline-success");
      buttonEl.innerHTML = '<i class="bi bi-check2"></i>';
      buttonEl.title = (result && result.message) || "Added to queue";
    }
  } catch (error) {
    console.error("Error adding dashboard upcoming release to queue:", error);
    if (buttonEl) { buttonEl.disabled = false; buttonEl.innerHTML = '<i class="bi bi-plus-circle"></i>'; buttonEl.title = "Failed to add to queue"; }
  }
}

// ===== Upcoming Releases Table =====
var dashboardTableFilter = "all";

function renderUpcomingReleasesTable(releases) {
  var body = document.getElementById("upcoming-releases-body");
  if (!body) return;

  var countEl = document.getElementById("upcomingTableCount");
  if (countEl) countEl.textContent = releases.length + " release(s)";

  if (!releases || releases.length === 0) {
    body.innerHTML = '<tr><td colspan="5" class="text-center text-muted">No upcoming releases found.</td></tr>';
    return;
  }

  var rows = releases
    .map(function (r) {
      var releaseDate = r.release_date || "TBA";
      var sourceRaw = String(r.source || "").toLowerCase();
      var sourceBadge =
        sourceRaw.indexOf("wiki") !== -1
          ? '<span class="upcoming-source-chip upcoming-source-wikipedia"><i class="bi bi-wikipedia"></i> Wikipedia</span>'
          : '<span class="upcoming-source-chip upcoming-source-musicbrainz"><i class="bi bi-hexagon-fill"></i> MusicBrainz</span>';

      var isUpcoming = new Date(releaseDate) > new Date();
      var dateBadge = isUpcoming
        ? '<span class="badge bg-success">Upcoming</span>'
        : '<span class="badge bg-primary">Recent</span>';

      var colBadge = r.artist_in_collection
        ? '<span class="badge bg-success ms-1" title="Artist in collection"><i class="bi bi-check"></i></span>'
        : "";
      var recBadge = r.artist_in_recommended
        ? '<span class="badge bg-warning text-dark ms-1" title="Recommended artist"><i class="bi bi-star"></i></span>'
        : "";
      var inQueue = r.in_queue === true || r.queue_status === "queued";

      var eArtist = encodeURIComponent(r.artist_name || "");
      var eAlbum = encodeURIComponent(r.album_name || "");
      var eDate = encodeURIComponent(r.release_date || "");
      var sArtist = JSON.stringify(String(r.artist_name || ""));
      var sAlbum = JSON.stringify(String(r.album_name || ""));
      var searchBtn =
        '<button class="btn btn-sm btn-outline-info" title="Search on MusicBrainz" onclick="showMusicBrainzModal();setTimeout(function(){populateMusicBrainzSearch(' +
        sArtist +
        ", " +
        sAlbum +
        ')},300)"><i class="bi bi-search"></i></button>';
      var queueBtn = inQueue
        ? '<button class="btn btn-sm btn-outline-success" disabled title="Already in queue"><i class="bi bi-check2-circle"></i></button>'
        : '<button class="btn btn-sm btn-outline-primary" title="Add to queue" onclick="addUpcomingReleaseToQueueDashboard(\'' +
          eArtist +
          "', '" +
          eAlbum +
          "', '" +
          eDate +
          "', this)\"><i class=\"bi bi-plus-circle\"></i></button>";

      return (
        "<tr>" +
        "<td>" +
        escapeHtml(r.artist_name) +
        colBadge +
        recBadge +
        "</td>" +
        "<td>" +
        escapeHtml(r.album_name) +
        "</td>" +
        "<td>" +
        escapeHtml(releaseDate) +
        " " +
        dateBadge +
        "</td>" +
        "<td>" +
        sourceBadge +
        "</td>" +
        '<td class="text-center"><div class="d-flex gap-1 justify-content-center">' +
        searchBtn +
        queueBtn +
        "</div></td>" +
        "</tr>"
      );
    })
    .join("");

  body.innerHTML = rows;
}

function _upcomingTableFilterEndpoint() {
  var params = new URLSearchParams();
  if (dashboardTableFilter === "collection") params.set("collection", "true");
  else if (dashboardTableFilter === "recommended") params.set("recommended", "true");
  params.set("include_queue", "true");
  return "/api/upcoming-releases?" + params.toString();
}

function _renderUpcomingTableFilterButtons() {
  var ids = { all: "upcomingTableFilterAll", collection: "upcomingTableFilterCollection", recommended: "upcomingTableFilterRecommended" };
  Object.keys(ids).forEach(function (key) {
    var btn = document.getElementById(ids[key]);
    if (!btn) return;
    var active = key === dashboardTableFilter;
    btn.classList.toggle("btn-info", active);
    btn.classList.toggle("btn-outline-info", !active);
    btn.setAttribute("aria-pressed", active ? "true" : "false");
  });
}

function setUpcomingTableFilter(filter) {
  var allowed = ["all", "collection", "recommended"];
  dashboardTableFilter = allowed.indexOf(filter) !== -1 ? filter : "all";
  _renderUpcomingTableFilterButtons();
  try { sessionStorage.setItem("dashboardTableFilter", dashboardTableFilter); } catch (e) {}
  loadUpcomingReleasesTable();
}

async function loadUpcomingReleasesTable() {
  try {
    var endpoint = _upcomingTableFilterEndpoint();
    var resp = await fetch(endpoint);
    var data = await resp.json();
    var releases = data.releases || [];

    if (dashboardTableFilter === "collection" && releases.some(function (r) { return r.artist_in_collection !== undefined; })) {
      releases = releases.filter(function (r) { return r.artist_in_collection; });
    } else if (dashboardTableFilter === "recommended" && releases.some(function (r) { return r.artist_in_recommended !== undefined; })) {
      releases = releases.filter(function (r) { return r.artist_in_recommended; });
    }

    releases.sort(function (a, b) {
      return (a.release_date || "9999-12-31").localeCompare(b.release_date || "9999-12-31");
    });

    renderUpcomingReleasesTable(releases);
  } catch (error) {
    console.error("Error loading upcoming releases table:", error);
    var body = document.getElementById("upcoming-releases-body");
    if (body) body.innerHTML = '<tr><td colspan="5" class="text-center text-danger">Error loading releases</td></tr>';
  }
}

// ===== Unified Log =====
var logPaused = false;

function toggleLog() {
  var body = document.getElementById("logBody");
  var btn = document.getElementById("toggleLogBtn");
  if (!body || !btn) return;
  body.classList.toggle("collapsed");
  btn.innerHTML = body.classList.contains("collapsed")
    ? '<i class="bi bi-chevron-down"></i>'
    : '<i class="bi bi-chevron-up"></i>';
}

function updateUnifiedLog() {
  if (logPaused) return;
  fetch("/api/unified-log?lines=150")
    .then(function (r) { return r.json(); })
    .then(function (data) {
      var logEl = document.getElementById("unifiedLog");
      if (logEl && data && data.lines) {
        logEl.textContent = data.lines.join("\n");
        logEl.scrollTop = logEl.scrollHeight;
      }
    })
    .catch(function () { /* silent */ });
}

// ===== Active Scans Progress Panel =====
var ACTIVE_SCAN_CONFIG = {
  popularity_scan:     { icon: "bi-graph-up",        label: "Popularity",   color: "bg-primary" },
  navidrome_scan:      { icon: "bi-cloud-arrow-down", label: "Navidrome",   color: "bg-secondary" },
  library_scan:        { icon: "bi-server",           label: "Library Sync", color: "bg-warning" },
  essentia_mood_scan:  { icon: "bi-cpu",              label: "Essentia",    color: "bg-info" },
  metadata_lookup_scan:{ icon: "bi-info-circle",      label: "Metadata",    color: "bg-info" },
  singles_scan:        { icon: "bi-star",             label: "Singles",     color: "bg-warning" },
  missing_releases_scan:{icon: "bi-flag",             label: "Missing",     color: "bg-secondary" },
};

function updateActiveScans() {
  fetch("/api/scan-progress?_ts=" + Date.now(), { cache: "no-store" })
    .then(function (r) { return r.json(); })
    .then(function (data) {
      var panel = document.getElementById("activeScansPanel");
      var body = document.getElementById("activeScansBody");
      if (!panel || !body) return;

      var active = data.active_scans || [];
      if (active.length === 0) {
        panel.style.display = "none";
        return;
      }

      panel.style.display = "";
      var html = "";
      active.forEach(function (scan) {
        var cfg = ACTIVE_SCAN_CONFIG[scan.scan_type] || { icon: "bi-lightning", label: scan.scan_type, color: "bg-secondary" };
        var pct = Math.min(scan.progress || 0, 100);
        var items = scan.processed_items || 0;
        var total = scan.total_items || "?";
        var current = scan.current_item || "";
        var message = scan.message || "";

        html += '<div class="mb-2">';
        html += '<div class="d-flex justify-content-between align-items-center mb-1">';
        html += '<span><i class="bi ' + cfg.icon + ' me-1"></i><strong>' + cfg.label + '</strong>';
        if (message) html += ' <span class="text-muted small ms-2">' + escapeHtml(message) + '</span>';
        html += '</span>';
        html += '<span class="small text-muted">' + items + '/' + total + '</span>';
        html += '</div>';
        if (current) {
          html += '<div class="small text-muted mb-1 text-truncate" style="max-width:600px;" title="' + escapeHtml(current) + '">' + escapeHtml(current) + '</div>';
        }
        html += '<div class="progress" style="height:8px;">';
        html += '<div class="progress-bar progress-bar-striped progress-bar-animated ' + cfg.color + '" style="width:' + pct + '%;"></div>';
        html += '</div>';
        html += '</div>';
      });

      body.innerHTML = html;
    })
    .catch(function () { /* silent */ });
}

// ===== Polling Orchestration =====
function updateAll() {
  pollPopularityStatus();
  pollNavidromeStatus();
  pollLibraryStatus();
  pollEssentiaStatus();
  updateActiveScans();
  updateRecentScans();
}

var _pi = null;
document.addEventListener("DOMContentLoaded", function () {
  // Render initial recent scans from server-provided data
  renderRecentScans((window._pd && window._pd.recentScans) || []);

  // Upcoming releases table
  try {
    var storedTableFilter = sessionStorage.getItem("dashboardTableFilter");
    if (storedTableFilter) dashboardTableFilter = storedTableFilter;
  } catch (e) {}
  _renderUpcomingTableFilterButtons();
  setTimeout(loadUpcomingReleasesTable, 400);
  setInterval(loadUpcomingReleasesTable, 30 * 60 * 1000);

  // Pause / Resume log button
  var pauseBtn = document.getElementById("pauseLogBtn");
  if (pauseBtn) {
    pauseBtn.addEventListener("click", function () {
      logPaused = !logPaused;
      pauseBtn.innerHTML = logPaused
        ? '<i class="bi bi-play"></i> Resume'
        : '<i class="bi bi-pause"></i> Pause';
    });
  }

  // Start polling
  updateAll();
  _pi = setInterval(updateAll, 5000);

  // Unified log refreshes less frequently
  updateUnifiedLog();
  setInterval(updateUnifiedLog, 10000);
});
