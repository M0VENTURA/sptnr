/**
 * Dashboard page JavaScript — scan controls, recent scans, upcoming releases, unified log.
 * Loaded separately from the esbuild bundle because it is page-specific.
 */

// Global error handler for development debugging
window.onerror = function (msg, url, line) {
  console.error(`Error: ${msg} on line ${line} in ${url}`);
};

// ===== Shared helpers =====
async function postJSON(u, b) {
  const r = await fetch(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}) });
  return r.json().catch(() => ({}));
}

function fE(s) {
  if (!s) return "";
  return ` — ${Math.floor(s / 60)}m ${Math.floor(s % 60)}s`;
}

function escapeHtml(str) {
  if (!str) return "";
  const d = document.createElement("div");
  d.appendChild(document.createTextNode(str));
  return d.innerHTML;
}

// ===== Scan Card Polling =====
(async function () {
  try {
    const r = await fetch("/api/navidrome/scan/status");
    const d = await r.json();
    const e = document.getElementById("nav-connectivity");
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

async function startPopularityScan(m, force) {
  await postJSON("/api/popularity/run", { mode: m || "popularity", force: !!force });
}

async function stopPopularityScan() {
  await fetch("/scan/stop-popularity", { method: "POST" });
}

// Runs the popularity scan selected in the dashboard selector with the
// current Force checkbox state.  No scan starts until Run is pressed.
async function runDashboardPopularityScan() {
  const mode = document.getElementById("popScanSelector")?.value || "popularity";
  const force = !!document.getElementById("popScanForce")?.checked;
  const btn = document.getElementById("popScanRunBtn");
  const original = btn ? btn.innerHTML : "";
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span> Starting…';
  }
  try {
    await startPopularityScan(mode, force);
  } catch (e) {
    console.error("Error starting popularity scan:", e);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = original;
    }
  }
}

async function pollPopularityStatus() {
  const r = await fetch("/api/popularity/status");
  const d = await r.json();
  if (!d.success) return;
  const e = document.getElementById("pop-status");
  if (!e) return;
  if (d.running) {
    e.innerText = `${d.message || "Running..."}${fE(d.elapsed_seconds)}`;
    e.className = "text-primary small";
  } else {
    e.innerText = d.message || "Idle";
    e.className = "text-muted small";
  }
}

async function startNavidromeImport() {
  // "Forced" = mode:force → serial full re-import of every artist, ignoring
  // the new-items/changed-album skips.  Unchecked = mode:all → normal import
  // that skips unchanged albums.
  const force = !!document.getElementById("navImportForce")?.checked;
  const d = await postJSON("/api/navidrome/import", { mode: force ? "force" : "all" });
  const e = document.getElementById("nav-status");
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
  const d = await postJSON("/api/navidrome/scan/start");
  const e = document.getElementById("nav-status");
  e.innerText = d.success ? "Server scan triggered" : "Failed";
  e.className = d.success ? "text-success small" : "text-danger small";
}

async function stopNavidromeSync() {
  await fetch("/scan/stop-navidrome", { method: "POST" });
}

async function pollNavidromeStatus() {
  const e = document.getElementById("nav-status");
  if (!e) return;
  const pd = await (await fetch("/api/scan-progress")).json().catch(() => ({}));
  const ns = (pd.active_scans || []).find(s => s.scan_type === "navidrome_scan");
  
  if (ns && ns.is_running) {
    e.innerText = `${ns.message || "Importing..."}${fE(ns.elapsed_seconds)}`;
    e.className = "text-primary small";
    if (e.dataset.started) delete e.dataset.started;
    return;
  }
  
  const sd = await (await fetch("/api/navidrome/scan/status")).json().catch(() => ({}));
  if (sd.scanning) {
    e.innerText = "Server scanning...";
    e.className = "text-warning small";
    if (e.dataset.started) delete e.dataset.started;
  } else if (e.dataset.started) {
    const elapsed = Date.now() - parseInt(e.dataset.started, 10);
    if (elapsed < 120000) {
      e.innerText = "Importing...";
      e.className = "text-primary small";
    } else {
      delete e.dataset.started;
      e.innerText = "Idle";
      e.className = "text-muted small";
    }
  } else {
    e.innerText = "Idle";
    e.className = "text-muted small";
  }
}

async function startEssentiaScan() {
  await fetch("/api/essentia/run", { method: "POST" });
}

async function stopEssentiaScan() {
  await fetch("/scan/stop-essentia-mood", { method: "POST" });
}

async function pollEssentiaStatus() {
  const e = document.getElementById("ess-status");
  if (!e) return;
  const pd = await (await fetch("/api/scan-progress")).json().catch(() => ({}));
  const es = (pd.active_scans || []).find(s => s.scan_type === "essentia_mood_scan");
  if (es && es.is_running) {
    e.innerText = `${es.message || "Running..."}${fE(es.elapsed_seconds)}`;
    e.className = "text-primary small";
  } else {
    e.innerText = "Idle";
    e.className = "text-muted small";
  }
}

async function stopAllScans() {
  await fetch("/scan/stop-all", { method: "POST" });
}

// ===== Recent Scans (Dynamic) =====
const SCAN_TYPE_DISPLAY_NAMES = {
  navidrome: "Navidrome Import",
  metadata: "Metadata Scan",
  popularity: "Popularity Scan",
  singles: "Singles Detection",
  singles_detection: "Singles Detection",
  essentia: "Essentia Mood Scan",
  mood: "Mood Scan",
  combined: "Combined Scan",
  all: "Full Scan (All)",
  navidrome_scan: "Navidrome Import",
  metadata_lookup_scan: "Metadata Scan",
  popularity_scan: "Popularity Scan",
  singles_scan: "Singles Detection",
  mood_scan: "Mood Scan",
  essentia_mood_scan: "Essentia Mood Scan",
  combined_scan: "Combined Scan",
  missing_releases_scan: "Missing Releases Scan",
  artist: "Artist Scan",
  artist_scan: "Artist Scan",
  full: "Full Scan",
  full_scan: "Full Scan",
  force: "Forced Full Scan",
};

function parseScanTimestamp(ts) {
  if (!ts && ts !== 0) return null;
  if (ts instanceof Date) return isNaN(ts.getTime()) ? null : ts;
  if (typeof ts === "number") { const d = new Date(ts); return isNaN(d.getTime()) ? null : d; }
  let text = String(ts).trim();
  if (!text) return null;
  if (/^\d+$/.test(text)) {
    const n = Number(text);
    if (!Number.isNaN(n)) { const e = new Date(text.length <= 10 ? n * 1000 : n); if (!isNaN(e.getTime())) return e; }
  }
  // Try a native parse first (handles RFC 1123 / HTTP-date strings such as
  // "Sun, 02 Aug 2026 10:00:00 GMT" emitted by older JSON serializers).
  const native = new Date(text);
  if (!isNaN(native.getTime())) return native;
  let norm = text.replace(" ", "T").replace(/\.(\d{3})\d+/, ".$1").replace(/([+-]\d{2})(\d{2})$/, "$1:$2");
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,3})?$/.test(norm)) norm += "Z";
  const d2 = new Date(norm);
  return isNaN(d2.getTime()) ? null : d2;
}

function formatScanTimestamp(ts) {
  const d = parseScanTimestamp(ts);
  if (!d) return "—";
  const diffSec = Math.floor((Date.now() - d.getTime()) / 1000);
  const diffMin = Math.floor(diffSec / 60);
  const diffHr = Math.floor(diffMin / 60);
  const diffDay = Math.floor(diffHr / 24);
  
  let relative;
  if (diffSec < 60) relative = `${diffSec}s ago`;
  else if (diffMin < 60) relative = `${diffMin}m ago`;
  else if (diffHr < 24) relative = `${diffHr}h ago`;
  else if (diffDay < 7) relative = `${diffDay}d ago`;
  else relative = d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  
  return `<span title="${escapeHtml(d.toLocaleString())}">${escapeHtml(relative)}</span>`;
}

function renderRecentScans(scans) {
  const body = document.getElementById("recent-scans-body");
  if (!body) return;
  if (!scans || scans.length === 0) {
    body.innerHTML = '<tr><td colspan="5" class="text-center text-muted">No recent scans yet</td></tr>';
    return;
  }

  const grouped = {};
  const inProgress = [];
  
  scans.forEach(scan => {
    const ts = scan.scan_timestamp || scan.started_at || scan.timestamp || null;
    if (scan._inProgress) {
      inProgress.push({
        artist: scan.artist,
        album: scan.album || "…",
        scan_types: [{ type: scan.scan_type, timestamp: ts, _inProgress: true }],
        latest_timestamp: ts,
        latest_timestamp_obj: new Date(),
        _inProgress: true,
      });
      return;
    }
    const key = `${scan.artist}|${scan.album}`;
    if (!grouped[key]) {
      grouped[key] = {
        artist: scan.artist,
        album: scan.album,
        status: scan.status,
        scan_types: [],
        latest_timestamp: ts,
        latest_timestamp_obj: parseScanTimestamp(ts),
      };
    }
    
    if (scan.status === "failed" || scan.status === "error") grouped[key].status = "failed";
    else if (scan.status === "completed" || scan.status === "complete") {
      if (grouped[key].status !== "failed") grouped[key].status = "completed";
    } else if (scan.status === "started" && !grouped[key].status) grouped[key].status = "started";
    else if (scan.status === "stopped" && grouped[key].status !== "failed" && grouped[key].status !== "completed") grouped[key].status = "stopped";
    
    if (!grouped[key].scan_types.find(s => s.type === scan.scan_type)) {
      grouped[key].scan_types.push({ type: scan.scan_type, timestamp: ts });
    }
    
    const scanTime = parseScanTimestamp(ts);
    if (scanTime && (!grouped[key].latest_timestamp_obj || scanTime > grouped[key].latest_timestamp_obj)) {
      grouped[key].latest_timestamp = ts;
      grouped[key].latest_timestamp_obj = scanTime;
    }
  });

  const entries = inProgress.concat(
    Object.values(grouped).sort((a, b) => (b.latest_timestamp_obj?.getTime() || 0) - (a.latest_timestamp_obj?.getTime() || 0))
  );

  body.innerHTML = entries.map(group => {
    if (group.artist === "_SCAN_SESSION_") {
      const typeKey = group.album || group.scan_types?.[0]?.type || "";
      const typeName = SCAN_TYPE_DISPLAY_NAMES[typeKey] || escapeHtml(typeKey);
      const isRunning = group.status === "started";
      const statusIcon = isRunning ? "bi-hourglass-split text-primary" : group.status === "failed" ? "bi-x-circle-fill text-danger" : "bi-check-circle-fill text-success";
      const statusLabel = isRunning ? "running" : group.status === "failed" ? "failed" : "completed";
      const rowClass = isRunning ? 'table-primary' : group.status === "failed" ? 'table-danger' : 'table-success bg-opacity-10';
      const spinnerHtml = isRunning ? '<span class="spinner-border spinner-border-sm me-1" style="width:.75em;height:.75em;"></span>' : '';
      return `<tr class="${rowClass}"><td colspan="4" class="py-1 ps-3"><small class="${isRunning ? 'text-primary' : group.status === 'failed' ? 'text-danger' : 'text-success'}">${spinnerHtml}<i class="bi ${statusIcon} me-1"></i><strong>${typeName}</strong> ${statusLabel} — ${formatScanTimestamp(group.latest_timestamp)}</small></td><td></td></tr>`;
    }

    const artistUrl = `/artist/${encodeURIComponent(group.artist)}`;
    const albumUrl = group.album && group.album !== "…" ? `/album/${encodeURIComponent(group.artist)}/${encodeURIComponent(group.album)}` : artistUrl;
    
    const badges = group.scan_types.filter(st => st.type !== "covers").map(st => {
      if (st._inProgress) return '<span class="badge bg-primary"><span class="spinner-border spinner-border-sm me-1" style="width:.75em;height:.75em;"></span> Scanning…</span>';
      const typeMap = {
        navidrome: '<i class="bi bi-cloud"></i> Navidrome',
        navidrome_scan: '<i class="bi bi-cloud"></i> Navidrome',
        popularity: '<i class="bi bi-graph-up"></i> Popularity',
        popularity_scan: '<i class="bi bi-graph-up"></i> Popularity',
        metadata: '<i class="bi bi-info-circle"></i> Metadata',
        metadata_lookup_scan: '<i class="bi bi-info-circle"></i> Metadata',
        singles: '<i class="bi bi-star"></i> Singles',
        singles_scan: '<i class="bi bi-star"></i> Singles',
        singles_detection: '<i class="bi bi-star"></i> Singles',
        mood: '<i class="bi bi-emoji-smile"></i> Mood',
        mood_scan: '<i class="bi bi-emoji-smile"></i> Mood',
        "essentia-mood": '<i class="bi bi-cpu"></i> Essentia',
        essentia: '<i class="bi bi-cpu"></i> Essentia',
        essentia_mood_scan: '<i class="bi bi-cpu"></i> Essentia',
        combined: '<i class="bi bi-lightning-fill"></i> Combined',
        combined_scan: '<i class="bi bi-lightning-fill"></i> Combined',
        unified: '<i class="bi bi-layers"></i> Unified',
        artist: '<i class="bi bi-person"></i> Artist',
        artist_scan: '<i class="bi bi-person"></i> Artist',
        full: '<i class="bi bi-collection"></i> Full',
        full_scan: '<i class="bi bi-collection"></i> Full',
        force: '<i class="bi bi-collection"></i> Forced',
        all: '<i class="bi bi-collection"></i> Full',
        missing_releases_scan: '<i class="bi bi-calendar-plus"></i> Missing Releases'
      };
      const badgeClass = { navidrome: 'bg-primary', navidrome_scan: 'bg-primary', popularity: 'bg-success', popularity_scan: 'bg-success', metadata: 'bg-info text-dark', metadata_lookup_scan: 'bg-info text-dark', singles: 'bg-warning text-dark', singles_scan: 'bg-warning text-dark', singles_detection: 'bg-warning text-dark', mood: 'bg-secondary', mood_scan: 'bg-secondary', "essentia-mood": 'bg-purple', essentia: 'bg-purple', essentia_mood_scan: 'bg-purple', combined: 'bg-info text-dark', combined_scan: 'bg-info text-dark', unified: 'bg-secondary', artist: 'bg-primary', artist_scan: 'bg-primary', full: 'bg-secondary', full_scan: 'bg-secondary', force: 'bg-secondary', all: 'bg-secondary', missing_releases_scan: 'bg-info text-dark' }[st.type] || 'bg-secondary';
      return `<span class="badge ${badgeClass}" title="${escapeHtml(st.timestamp || '')}">${typeMap[st.type] || escapeHtml(st.type)}</span>`;
    }).join(" ");

    return `<tr ${group._inProgress ? 'class="table-active"' : ''}>
      <td><a href='${artistUrl}'>${escapeHtml(group.artist)}</a></td>
      <td>${group.album && group.album !== "…" ? `<a href='${albumUrl}'>${escapeHtml(group.album)}</a>` : '<span class="text-muted fst-italic">…</span>'}</td>
      <td><div class='d-flex flex-wrap gap-2'>${badges}</div></td>
      <td class='text-muted text-end'><small>${group._inProgress ? "now" : formatScanTimestamp(group.latest_timestamp)}</small></td>
    </tr>`;
  }).join("");
}

let _lastRecentScansPayload = null;
let _currentScanningAlbum = null;

function updateRecentScans() {
  fetch(`/api/recent-scans?_ts=${Date.now()}`, { cache: "no-store" })
    .then(r => r.json())
    .then(data => {
      const payload = data.scans || [];
      if (payload.length > 0) {
        _lastRecentScansPayload = payload;
        renderRecentScans(_currentScanningAlbum ? [_currentScanningAlbum].concat(payload) : payload);
      }
    })
    .catch(error => console.error("Error fetching recent scans:", error));
}

// ===== Upcoming Releases =====
var dashboardTableFilter = "all";

async function addUpcomingReleaseToQueueDashboard(encodedArtist, encodedAlbum, encodedDate, buttonEl) {
  try {
    const artist = decodeURIComponent(encodedArtist || "").trim();
    const album = decodeURIComponent(encodedAlbum || "").trim();
    const dateText = decodeURIComponent(encodedDate || "").trim();
    if (!artist || !album) return;
    
    if (buttonEl) { buttonEl.disabled = true; buttonEl.innerHTML = '<span class="spinner-border spinner-border-sm" role="status"></span>'; }
    
    const result = await postJSON("/api/queue/add", {
      artist, title: album, album,
      import_type: "album", source: "qbittorrent", release_source: "dashboard_upcoming",
      priority: 5, is_album: true, year: /^\d{4}/.test(dateText) ? dateText.slice(0, 4) : null,
    });
    
    if (buttonEl) {
      buttonEl.disabled = true;
      buttonEl.classList.replace("btn-outline-primary", "btn-outline-success");
      buttonEl.innerHTML = '<i class="bi bi-check2"></i>';
      buttonEl.title = (result && result.message) || "Added to queue";
    }
  } catch (error) {
    if (buttonEl) { buttonEl.disabled = false; buttonEl.innerHTML = '<i class="bi bi-plus-circle"></i>'; }
  }
}

function renderUpcomingReleasesTable(releases) {
  const body = document.getElementById("upcoming-releases-body");
  if (!body) return;

  const countEl = document.getElementById("upcomingTableCount");
  if (countEl) {
    countEl.textContent =
      total && total > releases.length
        ? `${releases.length} of ${total} (±7d)`
        : `${releases.length} release(s)`;
  }

  if (!releases || releases.length === 0) {
    body.innerHTML = '<tr><td colspan="5" class="text-center text-muted">No upcoming releases found.</td></tr>';
    const cardsEl = document.getElementById("upcoming-releases-cards");
    if (cardsEl) cardsEl.innerHTML = '<div class="text-center text-muted py-4 small">No upcoming releases found.</div>';
    return;
  }

  const rows = releases.map(r => {
    const releaseDate = r.release_date || "TBA";
    // The Wikipedia scraper stores the configured source display names (e.g.
    // "2026 Albums", "Heavy Metal 2026") — they never contain the literal
    // "wiki".  Only MusicBrainz-sourced rows should show the MB chip, so
    // treat anything that isn't explicitly MusicBrainz as Wikipedia.
    // Determine source: explicitly check for the known MusicBrainz source name
    // (stored as "MusicBrainz Daily Collection" in the database). Any other source
    // is treated as Wikipedia (e.g., "2026 Albums", "Heavy Metal 2026").
    const sourceStr = String(r.source || "").trim();
    const sourceKey = String(r.source_key || "").trim();
    const isMusicBrainz = sourceStr.toLowerCase().includes("musicbrainz daily collection");
    const sourceBadge = sourceKey
      ? `<span class="source-key-badge" title="Scraper rule: ${escapeHtml(sourceKey)}"><i class="bi bi-wikipedia"></i> ${escapeHtml(sourceKey)}</span>`
      : (isMusicBrainz
          ? '<span class="upcoming-source-chip upcoming-source-musicbrainz"><i class="bi bi-hexagon-fill"></i> MusicBrainz</span>'
          : '<span class="upcoming-source-chip upcoming-source-wikipedia"><i class="bi bi-wikipedia"></i> Wikipedia</span>');

    const dateBadge = new Date(releaseDate) > new Date() ? '<span class="badge bg-success">Upcoming</span>' : '<span class="badge bg-primary">Recent</span>';
    const colBadge = r.artist_in_collection ? '<span class="badge bg-success ms-1" title="Artist in collection"><i class="bi bi-check"></i></span>' : "";
    
    const eArtist = encodeURIComponent(r.artist_name || "");
    const eAlbum = encodeURIComponent(r.album_name || "");
    const sArtist = JSON.stringify(String(r.artist_name || ""));
    const sAlbum = JSON.stringify(String(r.album_name || ""));

    // NEW UNIFIED SEARCH TRIGGER
    const searchBtn = `<button class="btn btn-sm btn-outline-info" title="Search on MusicBrainz" onclick='window.openGlobalMbSearch(${sArtist}, ${sAlbum}, function(selected){ if(typeof downloadMbRelease === "function") downloadMbRelease(selected.id, selected.title, selected.artist, "slskd"); })'><i class="bi bi-search"></i></button>`;
    
    const inQueue = r.in_queue === true || r.queue_status === "queued";
    const queueBtn = inQueue
      ? '<button class="btn btn-sm btn-outline-success" disabled title="Already in queue"><i class="bi bi-check2-circle"></i></button>'
      : `<button class="btn btn-sm btn-outline-primary" title="Add to queue" onclick="addUpcomingReleaseToQueueDashboard('${eArtist}', '${eAlbum}', '${encodeURIComponent(r.release_date || "")}', this)"><i class="bi bi-plus-circle"></i></button>`;

    return {
      html: `<tr>
        <td>${escapeHtml(r.artist_name)}${colBadge}</td>
        <td>${escapeHtml(r.album_name)}</td>
        <td>${escapeHtml(releaseDate)} ${dateBadge}</td>
        <td>${sourceBadge}</td>
        <td class="text-center"><div class="d-flex gap-1 justify-content-center">${searchBtn}${queueBtn}</div></td>
      </tr>`,
      // Stacked mobile card (shown below lg — the table is hidden on small screens)
      card: `<div class="upcoming-card p-2 mb-2 rounded border">
        <div class="d-flex justify-content-between align-items-start gap-2">
          <div class="min-w-0">
            <div class="fw-semibold text-truncate">${escapeHtml(r.artist_name)}${colBadge}</div>
            <div class="small text-muted text-truncate">${escapeHtml(r.album_name)}</div>
          </div>
          <div class="d-flex gap-1 flex-shrink-0">${searchBtn}${queueBtn}</div>
        </div>
        <div class="d-flex align-items-center gap-2 mt-1 flex-wrap small">
          <span class="text-muted"><i class="bi bi-calendar"></i> ${escapeHtml(releaseDate)}</span>${dateBadge}${sourceBadge}
        </div>
      </div>`
    };
  });

  body.innerHTML = rows.map(x => x.html).join("");
  const cardsEl = document.getElementById("upcoming-releases-cards");
  if (cardsEl) cardsEl.innerHTML = rows.map(x => x.card).join("");
}

function _renderUpcomingTableFilterButtons() {
  ["all", "collection"].forEach(key => {
    const btn = document.getElementById(`upcomingTableFilter${key.charAt(0).toUpperCase() + key.slice(1)}`);
    if (!btn) return;
    const active = key === dashboardTableFilter;
    btn.classList.toggle("btn-info", active);
    btn.classList.toggle("btn-outline-info", !active);
  });
}

function setUpcomingTableFilter(filter) {
  dashboardTableFilter = ["all", "collection"].includes(filter) ? filter : "all";
  _renderUpcomingTableFilterButtons();
  sessionStorage.setItem("dashboardTableFilter", dashboardTableFilter);
  loadUpcomingReleasesTable();
}

// ===== Upcoming Releases: ±7-day window (client-side) =====
let dashboardUpcomingAll = [];
let dashboardUpcomingShowAll = false;

function _inSevenDayWindow(release) {
  const raw = String(release.release_date || "").slice(0, 10);
  if (raw.length !== 10) return false;
  const d = new Date(raw + "T00:00:00");
  if (isNaN(d.getTime())) return false;
  const now = new Date();
  const start = new Date(now.getTime() - 7 * 86400000);
  const end = new Date(now.getTime() + 7 * 86400000);
  return d >= start && d <= end;
}

function renderUpcomingFromStore() {
  const all = dashboardUpcomingAll;
  const windowed = all.filter(_inSevenDayWindow);
  const visible = dashboardUpcomingShowAll ? all : windowed;
  renderUpcomingReleasesTable(visible, all.length);

  const btn = document.getElementById("upcomingShowAllBtn");
  if (!btn) return;
  if (dashboardUpcomingShowAll) {
    btn.classList.remove("d-none");
    btn.innerHTML = '<i class="bi bi-chevron-up"></i> ±7 days only';
  } else if (all.length > windowed.length) {
    btn.classList.remove("d-none");
    btn.innerHTML = `<i class="bi bi-chevron-down"></i> Show all ${all.length} releases`;
  } else {
    btn.classList.add("d-none");
  }
}

function toggleUpcomingShowAll() {
  dashboardUpcomingShowAll = !dashboardUpcomingShowAll;
  renderUpcomingFromStore();
}

async function loadUpcomingReleasesTable() {
  try {
    const data = await UpcomingReleasesService.fetchReleases({
      filter: dashboardTableFilter === "collection" ? "collection" : undefined,
      include_queue: true,
    });
    let releases = data.releases || [];

    if (dashboardTableFilter === "collection") releases = releases.filter(r => r.artist_in_collection);

    releases.sort((a, b) => (a.release_date || "9999-12-31").localeCompare(b.release_date || "9999-12-31"));
    dashboardUpcomingAll = releases;
    renderUpcomingFromStore();
  } catch (error) {
    console.error("Error loading upcoming releases table:", error);
  }
}

// ===== Unified Log =====
let logPaused = false;
let logModalVisible = false;

function openUnifiedLogModal() {
  const modalEl = document.getElementById("unifiedLogModal");
  if (!modalEl) return;
  bootstrap.Modal.getOrCreateInstance(modalEl).show();
}

function updateUnifiedLog() {
  if (logPaused) return;
  // Self-healing poll: a hung request must not freeze the log panel forever.
  // Abort after 8s so the next poll tick recovers on its own.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 8000);
  fetch("/api/unified-log?lines=200&last_hour=1", { signal: controller.signal })
    .then(r => r.json())
    .then(data => {
      const logEl = document.getElementById("unifiedLog");
      if (logEl && Array.isArray(data && data.lines)) {
        logEl.textContent = data.lines.join("\n");
        logEl.scrollTop = logEl.scrollHeight;
      }
    })
    .catch(() => {})
    .finally(() => clearTimeout(timer));
}

// ===== Active Scans Progress Panel =====
/* One-line summary for the sticky bottom status bar. */
function updateScanStatusBar(active) {
  const line = document.getElementById("scanStatusLine");
  const icon = document.getElementById("scanStatusIcon");
  if (!line || !icon) return;
  if (!active || active.length === 0) {
    line.textContent = "Idle — no scan running";
    icon.className = "scan-status-idle";
    icon.innerHTML = '<i class="bi bi-circle"></i>';
    return;
  }
  const scan = active[0];
  const name = SCAN_TYPE_DISPLAY_NAMES[scan.scan_type] || scan.scan_type;
  const pct = Math.min(scan.progress || 0, 100);
  line.textContent = `${name} — ${pct}%` + (scan.current_item ? ` · ${scan.current_item}` : "");
  icon.className = "scan-status-active";
  icon.innerHTML = '<i class="bi bi-activity"></i>';
}

function updateActiveScans() {
  fetch(`/api/scan-progress?_ts=${Date.now()}`, { cache: "no-store" })
    .then(r => r.json())
    .then(data => {
      const panel = document.getElementById("activeScansPanel");
      const body = document.getElementById("activeScansBody");
      const badge = document.getElementById("scannerStatusBadge");
      const active = data.active_scans || [];
      updateScanStatusBar(active);
      if (badge) {
        badge.textContent = active.length ? "Active" : "Idle";
        badge.className = "badge " + (active.length ? "bg-success" : "bg-secondary");
        badge.style.fontSize = "0.65rem";
      }
      if (!panel || !body) return;
      if (active.length === 0) {
        panel.style.display = "none";
        return;
      }

      panel.style.display = "";
      body.innerHTML = active.map(scan => {
        const pct = Math.min(scan.progress || 0, 100);
        return `
          <div class="mb-2">
            <div class="d-flex justify-content-between align-items-center mb-1">
              <span><i class="bi bi-activity me-1"></i><strong>${escapeHtml(SCAN_TYPE_DISPLAY_NAMES[scan.scan_type] || scan.scan_type)}</strong>
              ${scan.message ? `<span class="text-muted small ms-2">${escapeHtml(scan.message)}</span>` : ''}</span>
              <span class="small text-muted">${scan.processed_items || 0}/${scan.total_items || "?"}</span>
            </div>
            ${scan.current_item ? `<div class="small text-muted mb-1 text-truncate" style="max-width:600px;">${escapeHtml(scan.current_item)}</div>` : ''}
            <div class="progress" style="height:8px;">
              <div class="progress-bar progress-bar-striped progress-bar-animated bg-success" style="width:${pct}%;"></div>
            </div>
          </div>`;
      }).join("");
    }).catch(() => {});
}

// ===== Initialization =====
function updateAll() {
  pollPopularityStatus();
  pollNavidromeStatus();
  pollEssentiaStatus();
  updateActiveScans();
  updateRecentScans();
}

document.addEventListener("DOMContentLoaded", function () {
  renderRecentScans((window._pd && window._pd.recentScans) || []);

  try {
    const storedTableFilter = sessionStorage.getItem("dashboardTableFilter");
    if (storedTableFilter) dashboardTableFilter = storedTableFilter;
  } catch (e) {}
  
  _renderUpcomingTableFilterButtons();
  setTimeout(loadUpcomingReleasesTable, 400);
  setInterval(loadUpcomingReleasesTable, 30 * 60 * 1000);

  const pauseBtn = document.getElementById("pauseLogBtn");
  if (pauseBtn) {
    pauseBtn.addEventListener("click", function () {
      logPaused = !logPaused;
      pauseBtn.innerHTML = logPaused ? '<i class="bi bi-play"></i> Resume' : '<i class="bi bi-pause"></i> Pause';
    });
  }

  updateAll();
  setInterval(updateAll, 5000);

  // Full-screen log modal: only fetch log lines while it is visible.
  const logModalEl = document.getElementById("unifiedLogModal");
  if (logModalEl) {
    logModalEl.addEventListener("shown.bs.modal", function () {
      logModalVisible = true;
      updateUnifiedLog();
    });
    logModalEl.addEventListener("hidden.bs.modal", function () {
      logModalVisible = false;
    });
  }
  setInterval(function () { if (logModalVisible) updateUnifiedLog(); }, 5000);

  // Lift the sticky scan status bar above the global player bar when it appears.
  setInterval(function () {
    const playerBar = document.getElementById("globalPlayerBar");
    if (playerBar) {
      document.body.classList.toggle("player-visible", !playerBar.classList.contains("d-none"));
    }
  }, 2000);
});