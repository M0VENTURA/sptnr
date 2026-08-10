// Artist List Page JS
// Extracted from templates/pages/artist_list.html

// ===== Add Artist Modal =====

function showAddArtistModal() {
    const modal = new bootstrap.Modal(document.getElementById('addArtistModal'));
    document.getElementById('artistNameInput').value = '';
    document.getElementById('addArtistStatus').style.display = 'none';
    modal.show();

    setTimeout(function () {
        document.getElementById('artistNameInput').focus();
    }, 500);
}

function addArtist() {
    var artistName = document.getElementById('artistNameInput').value.trim();
    var statusDiv = document.getElementById('addArtistStatus');

    if (!artistName) {
        statusDiv.className = 'alert alert-warning';
        statusDiv.textContent = 'Please enter an artist name';
        statusDiv.style.display = 'block';
        return;
    }

    statusDiv.style.display = 'none';

    var addArtistModalEl = document.getElementById('addArtistModal');
    var addArtistModal = bootstrap.Modal.getInstance(addArtistModalEl);
    if (addArtistModal) {
        addArtistModal.hide();
    }

    if (typeof searchMusicBrainzRelease === 'function') {
        searchMusicBrainzRelease(null, artistName, '');
    } else {
        alert('Release search is not available on this page. Please refresh and try again.');
    }
}

// ===== Letter navigation =====

function jumpToLetter(letter) {
    var list = document.getElementById('artistsList');
    var items = list.querySelectorAll(':scope > .list-group-item');

    if (letter === 'all') {
        items.forEach(function (item) { item.style.display = ''; });
        list.scrollIntoView({ behavior: 'smooth', block: 'start' });
        return;
    }

    items.forEach(function (item) { item.style.display = 'none'; });

    var headerItem = null;
    items.forEach(function (item) {
        var itemLetter = item.getAttribute('data-letter');
        var hasHeader = item.getAttribute('data-letter-header');
        if (hasHeader === letter) {
            item.style.display = '';
            headerItem = item;
        } else if (itemLetter === letter) {
            item.style.display = '';
        }
    });

    if (headerItem) {
        headerItem.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
}

// ===== Live search + health filter =====

function applyArtistFilters() {
    var list = document.getElementById('artistsList');
    if (!list) return;

    var query = (document.getElementById('artistSearchInput')?.value || '').trim().toLowerCase();
    var activeFilter = document.querySelector('.artist-health-filter.active');
    var filter = activeFilter ? activeFilter.getAttribute('data-filter') : 'all';

    var rows = list.querySelectorAll(':scope > .list-group-item');
    var anyVisible = false;

    rows.forEach(function (row) {
        var isHeader = row.hasAttribute('data-letter-header');
        if (isHeader) {
            row.classList.add('filtered-out');
            return;
        }
        var name = (row.getAttribute('data-artist-name') || row.getAttribute('data-artist') || '').toLowerCase();
        var matchesQuery = !query || name.indexOf(query) !== -1;
        var matchesFilter = filter !== 'issues' || row.getAttribute('data-has-corrections') === '1';
        var visible = matchesQuery && matchesFilter;
        row.classList.toggle('filtered-out', !visible);
        if (visible) anyVisible = true;
    });

    if (!anyVisible) {
        var noResults = document.getElementById('artistNoResults');
        if (noResults) noResults.classList.remove('d-none');
        return;
    }

    // Re-show the letter headers that still have visible artists beneath them.
    var showHeader = false;
    rows.forEach(function (row) {
        if (row.hasAttribute('data-letter-header')) {
            showHeader = false;
            row.classList.add('filtered-out');
            return;
        }
        if (!row.classList.contains('filtered-out')) {
            if (!showHeader && row.previousElementSibling &&
                row.previousElementSibling.hasAttribute('data-letter-header')) {
                row.previousElementSibling.classList.remove('filtered-out');
            }
            showHeader = true;
        }
    });

    var noResults = document.getElementById('artistNoResults');
    if (noResults) noResults.classList.add('d-none');
}

function setupArtistFilters() {
    var searchInput = document.getElementById('artistSearchInput');
    if (searchInput) {
        searchInput.addEventListener('input', applyArtistFilters);
    }
    document.querySelectorAll('.artist-health-filter').forEach(function (btn) {
        btn.addEventListener('click', function () {
            document.querySelectorAll('.artist-health-filter').forEach(function (b) { b.classList.remove('active'); });
            btn.classList.add('active');
            applyArtistFilters();
        });
    });
}

// ===== Scan from letter =====

async function scanLetterArtists(letter) {
    var forceCheck = document.querySelector('.letter-force-check[data-letter="' + letter + '"]');
    var fullScan = !!(forceCheck && forceCheck.checked);
    var scanMode = fullScan ? 'forced' : 'changes';
    var message = 'Start ' + (fullScan ? 'Full' : 'Changes') + ' scan from letter "' + letter + '"?\n\nThis will resolve the first matching artist from your local library and scan from there.';

    if (!confirm(message)) {
        return;
    }

    try {
        var response = await fetch('/api/scan/from-artist', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                letter: letter,
                scan_mode: scanMode
            })
        });

        var data = await response.json();
        if (data.success) {
            alert('Scan started successfully (' + data.mode + ')\n\nStarting from: ' + data.artist + '\n\nCheck the dashboard for progress.');
        } else {
            alert('Error: ' + data.error);
        }
    } catch (error) {
        console.error('Error starting scan:', error);
        alert('Error starting scan: ' + error.message);
    }
}

// ===== Artist Correction Badges =====

async function loadArtistCorrections() {
    try {
        var response = await fetch('/api/artists/corrections');
        if (!response.ok) {
            console.debug('Could not load artist corrections, status:', response.status);
            return;
        }
        var data = await response.json();
        var corrections = data.corrections || {};

        document.querySelectorAll('#artistsList > .list-group-item[data-artist]').forEach(function (row) {
            var artistName = row.dataset.artist;
            var info = corrections[artistName.toLowerCase()] || corrections[artistName];
            if (!info) return;

            var correctionsUrl = info.corrections_url || '#';

            if (info.needs_correction) {
                row.setAttribute('data-has-corrections', '1');
                var parts = [];
                if (info.duplicate_track_count > 0) parts.push(info.duplicate_track_count + ' duplicate track(s)');
                if (info.disc_inconsistent_count > 0) parts.push(info.disc_inconsistent_count + ' album(s) with disc number issues');
                if (info.mbid_inconsistent_count > 0) parts.push(info.mbid_inconsistent_count + ' album(s) with multiple MBIDs');
                if (info.missing_tracks_count > 0) parts.push(info.missing_tracks_count + ' album(s) with missing tracks');
                var titleText = parts.join(', ');

                function makeBadgeToggle(extraClass) {
                    var toggle = document.createElement('span');
                    toggle.className = extraClass + ' correction-toggle text-decoration-none';
                    toggle.style.cursor = 'pointer';
                    toggle.title = titleText + ' — click to expand';
                    var badge = document.createElement('span');
                    badge.className = 'badge bg-danger';
                    badge.textContent = 'Needs Correcting';
                    toggle.appendChild(badge);
                    toggle.addEventListener('click', function (e) {
                        e.preventDefault();
                        toggleCorrectionDetail(row, artistName, info, toggle);
                    });
                    return toggle;
                }

                var desktopSpan = row.querySelector('.correction-badge-desktop');
                if (desktopSpan) { desktopSpan.innerHTML = ''; desktopSpan.appendChild(makeBadgeToggle('ms-2')); }

                var mobileSpan = row.querySelector('.correction-badge-mobile');
                if (mobileSpan) { mobileSpan.innerHTML = ''; mobileSpan.appendChild(makeBadgeToggle('ms-1')); }

                var actionSpan = row.querySelector('.correction-action-btns');
                if (actionSpan) {
                    actionSpan.innerHTML = '';
                    var toolBtn = document.createElement('button');
                    toolBtn.className = 'btn btn-outline-danger correction-toggle-btn';
                    toolBtn.title = titleText + ' — click to expand';
                    toolBtn.innerHTML = '<i class="bi bi-tools"></i>';
                    toolBtn.addEventListener('click', function () {
                        toggleCorrectionDetail(row, artistName, info, toolBtn);
                    });
                    actionSpan.appendChild(toolBtn);
                    if (info.duplicate_artist_count > 0) {
                        var mergeBtn = document.createElement('a');
                        mergeBtn.href = correctionsUrl + '#duplicate-artists-section';
                        mergeBtn.className = 'btn btn-outline-warning';
                        mergeBtn.title = 'Open artist merge options';
                        mergeBtn.innerHTML = '<i class="bi bi-merge"></i>';
                        actionSpan.appendChild(mergeBtn);
                    }
                }
            } else if (info.duplicate_artist_count > 0) {
                row.setAttribute('data-has-corrections', '1');
                var actionSpan = row.querySelector('.correction-action-btns');
                if (actionSpan) {
                    actionSpan.innerHTML = '';
                    var mergeBtn = document.createElement('a');
                    mergeBtn.href = correctionsUrl + '#duplicate-artists-section';
                    mergeBtn.className = 'btn btn-outline-warning';
                    mergeBtn.title = 'Open artist merge options';
                    mergeBtn.innerHTML = '<i class="bi bi-merge"></i>';
                    actionSpan.appendChild(mergeBtn);
                }
            }
        });
    } catch (e) {
        console.debug('Could not load artist corrections:', e);
    }
}

// ===== Correction Detail Toggle =====

async function toggleCorrectionDetail(row, artistName, info, triggerEl) {
    var detailRow = row.nextElementSibling;
    if (!detailRow || !detailRow.classList.contains('correction-detail-row')) return;

    var isHidden = detailRow.classList.contains('d-none');
    if (!isHidden) {
        detailRow.classList.add('d-none');
        return;
    }

    detailRow.classList.remove('d-none');
    var body = detailRow.querySelector('.correction-detail-body');
    var loading = detailRow.querySelector('.correction-detail-loading');

    if (body.dataset.loaded) return;

    loading.classList.remove('d-none');
    body.innerHTML = '';

    try {
        var resp = await fetch('/api/artist/corrections-albums?artist=' + encodeURIComponent(artistName));
        var data = await resp.json();

        loading.classList.add('d-none');

        if (!data.success) {
            body.innerHTML = '<div class="alert alert-warning py-2 small mb-0">' + (data.error || 'Failed to load') + '</div>';
            return;
        }

        var albums = data.albums || [];
        var html = '';

        // Artist-level corrections
        var hasArtistCorrections = false;

        if (info.duplicate_track_count > 0) {
            hasArtistCorrections = true;
            html += '<div class="d-flex justify-content-between align-items-center mb-2 p-2 rounded" style="background:var(--tertiary-bg);">' +
                '<span><i class="bi bi-exclamation-triangle text-warning me-2"></i><strong>' + info.duplicate_track_count + '</strong> duplicate track(s)</span>' +
                '<span class="small text-muted">Artist-level — check track list</span></div>';
        }

        if (info.disc_inconsistent_count > 0) {
            hasArtistCorrections = true;
            html += '<div class="d-flex justify-content-between align-items-center mb-2 p-2 rounded" style="background:var(--tertiary-bg);">' +
                '<span><i class="bi bi-disc text-info me-2"></i><strong>' + info.disc_inconsistent_count + '</strong> album(s) with disc number issues</span>' +
                '<button class="btn btn-sm btn-outline-info clear-disc-all-btn" data-artist="' + artistName.replace(/"/g, '&quot;') + '"><i class="bi bi-eraser me-1"></i>Clear All Disc Numbers</button></div>';
        }

        if (info.mbid_inconsistent_count > 0) {
            hasArtistCorrections = true;
            html += '<div class="d-flex justify-content-between align-items-center mb-2 p-2 rounded" style="background:var(--tertiary-bg);">' +
                '<span><i class="bi bi-music-note-list text-danger me-2"></i><strong>' + info.mbid_inconsistent_count + '</strong> album(s) with missing MBIDs</span>' +
                '<span class="small text-muted">See per-album Fix MBID buttons below</span></div>';
        }

        if (info.missing_tracks_count > 0) {
            hasArtistCorrections = true;
            html += '<div class="d-flex justify-content-between align-items-center mb-2 p-2 rounded" style="background:var(--tertiary-bg);">' +
                '<span><i class="bi bi-file-earmark-x text-secondary me-2"></i><strong>' + info.missing_tracks_count + '</strong> track(s) with missing file paths</span>' +
                '<span class="small text-muted">May need re-scan</span></div>';
        }

        if (hasArtistCorrections) {
            html += '<hr class="my-2 border-secondary opacity-25">';
        }

        // Album list
        if (albums.length > 0) {
            html += '<div class="small text-secondary mb-2 fw-semibold"><i class="bi bi-disc me-1"></i>Albums</div>';

            var sorted = [...albums].sort(function (a, b) {
                var aNeeds = (a.disc_issues ? 1 : 0) + (a.mbid_issues ? 1 : 0) + (a.missing_tracks ? 1 : 0);
                var bNeeds = (b.disc_issues ? 1 : 0) + (b.mbid_issues ? 1 : 0) + (b.missing_tracks ? 1 : 0);
                return bNeeds - aNeeds;
            });

            sorted.forEach(function (a) {
                var issueBadges = '';
                if (a.disc_issues) issueBadges += '<span class="badge bg-info text-dark me-1" title="Disc number issues"><i class="bi bi-disc"></i> Disc</span>';
                if (a.mbid_issues) issueBadges += '<button class="btn btn-sm btn-outline-danger fix-mbid-btn me-1" title="Link this album to a MusicBrainz release" data-artist="' + artistName.replace(/"/g, '&quot;') + '" data-album="' + escHtml(a.album).replace(/"/g, '&quot;') + '"><i class="bi bi-link-45deg"></i> Link MBID</button>';
                if (a.missing_tracks) issueBadges += '<span class="badge bg-secondary me-1" title="Missing file paths"><i class="bi bi-file-earmark-x"></i> Missing</span>';
                if (!issueBadges) issueBadges = '<span class="badge bg-success me-1"><i class="bi bi-check-circle"></i> OK</span>';

                html += '<div class="d-flex flex-wrap justify-content-between align-items-center py-2 px-2 mb-1 rounded correction-album-row">' +
                    '<div class="d-flex align-items-center gap-2 flex-wrap">' +
                    '<span class="small fw-medium">' + escHtml(a.album) + '</span>' +
                    '<span class="small text-muted">' + a.track_count + ' tracks</span>' +
                    '<span class="d-flex gap-1 flex-wrap">' + issueBadges + '</span>' +
                    '</div>' +
                    '<div class="d-flex gap-1 flex-wrap mt-1 mt-sm-0">' +
                    (a.disc_issues ? '<button class="btn btn-sm btn-outline-info clear-disc-btn" data-artist="' + artistName.replace(/"/g, '&quot;') + '" data-album="' + escHtml(a.album).replace(/"/g, '&quot;') + '"><i class="bi bi-eraser"></i> Clear Disc#</button>' : '') +
                    (a.missing_tracks ? '<button class="btn btn-sm btn-outline-secondary" onclick="alert(\'Re-scan this album to recover missing tracks.\')"><i class="bi bi-arrow-repeat"></i> Re-scan</button>' : '') +
                    '</div></div>';
            });
        } else {
            html += '<div class="text-secondary small py-2"><i class="bi bi-check-circle me-1"></i>No albums found for this artist.</div>';
        }

        if (!html) {
            html = '<div class="text-secondary small py-2"><i class="bi bi-check-circle me-1"></i>No specific issues found for this artist.</div>';
        }

        body.innerHTML = html;
        body.dataset.loaded = '1';

        // Wire button handlers
        body.querySelectorAll('.clear-disc-all-btn').forEach(function (btn) {
            btn.addEventListener('click', async function () {
                var artist = btn.dataset.artist;
                if (!confirm('Clear ALL disc numbers for ' + artist + '?')) return;
                try {
                    var resp = await fetch('/api/artist/corrections/clear-disc-number', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ artist: artist, force_clear: true })
                    });
                    var result = await resp.json();
                    if (result.success) {
                        btn.closest('.d-flex').remove();
                        setTimeout(loadArtistCorrections, 2000);
                    } else {
                        alert('Error: ' + (result.error || 'Failed'));
                    }
                } catch (err) {
                    alert('Request failed: ' + err);
                }
            });
        });

        body.querySelectorAll('.clear-disc-btn').forEach(function (btn) {
            btn.addEventListener('click', async function () {
                var artist = btn.dataset.artist;
                var album = btn.dataset.album || '';
                if (!confirm('Clear disc numbers for "' + album + '" by ' + artist + '?')) return;
                try {
                    var resp = await fetch('/api/artist/corrections/clear-disc-number', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ artist: artist, album: album, force_clear: true })
                    });
                    var result = await resp.json();
                    if (result.success) {
                        var rowDiv = btn.closest('.correction-album-row');
                        var discBadge = rowDiv.querySelector('.badge.bg-info');
                        if (discBadge) discBadge.remove();
                        btn.remove();
                        var remaining = rowDiv.querySelectorAll('.badge.bg-info, .badge.bg-danger, .badge.bg-secondary');
                        if (remaining.length === 0) {
                            var badgeContainer = rowDiv.querySelector('.d-flex.gap-1.flex-wrap');
                            if (badgeContainer) badgeContainer.innerHTML = '<span class="badge bg-success">OK</span>';
                        }
                        setTimeout(loadArtistCorrections, 2000);
                    } else {
                        alert('Error: ' + (result.error || 'Failed'));
                    }
                } catch (err) {
                    alert('Request failed: ' + err);
                }
            });
        });

        body.querySelectorAll('.fix-mbid-btn').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var artist = btn.dataset.artist;
                var album = btn.dataset.album;
                if (typeof populateMusicBrainzSearch === 'function') {
                    showMusicBrainzModal();
                    setTimeout(function () { populateMusicBrainzSearch(artist, album); }, 400);
                } else if (typeof searchMusicBrainzRelease === 'function') {
                    searchMusicBrainzRelease(null, artist, album, null);
                } else {
                    alert('MusicBrainz search is not available on this page.');
                }
            });
        });

    } catch (err) {
        loading.classList.add('d-none');
        body.innerHTML = '<div class="alert alert-danger py-2 small mb-0">Failed to load: ' + err.message + '</div>';
    }
}

// ===== HTML Escape Helper =====

function escHtml(str) {
    if (!str) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

// ===== Expandable Album Rows =====

document.addEventListener('DOMContentLoaded', function () {
    setupArtistFilters();
    document.querySelectorAll('.btn-expand-albums').forEach(function (btn) {
        btn.addEventListener('click', function () {
            var artist = this.getAttribute('data-artist');
            if (!artist) return;

            var detailRow = document.querySelector('.albums-detail-row[data-artist="' + artist.replace(/"/g, '\\"') + '"]');
            if (!detailRow) return;

            var isHidden = detailRow.classList.contains('d-none');

            document.querySelectorAll('.albums-detail-row:not(.d-none)').forEach(function (row) {
                row.classList.add('d-none');
                var otherBtn = document.querySelector('.btn-expand-albums[data-artist="' + row.getAttribute('data-artist') + '"]');
                if (otherBtn) otherBtn.innerHTML = '<i class="bi bi-collection"></i>';
            });

            if (isHidden) {
                detailRow.classList.remove('d-none');
                this.innerHTML = '<i class="bi bi-collection-fill"></i>';

                var loading = detailRow.querySelector('.albums-loading');
                var body = detailRow.querySelector('.albums-body');

                if (!body.hasAttribute('data-loaded')) {
                    loading.classList.remove('d-none');
                    body.innerHTML = '';

                    fetch('/api/artist/corrections-albums?artist=' + encodeURIComponent(artist))
                        .then(function (r) { return r.json(); })
                        .then(function (data) {
                            loading.classList.add('d-none');
                            body.setAttribute('data-loaded', 'true');

                            var albums = data.albums || [];
                            if (albums.length === 0) {
                                body.innerHTML = '<div class="text-muted small py-2">No albums found.</div>';
                                return;
                            }

                            var TYPE_ORDER = ['album', 'ep', 'single', 'compilation', 'live_album', 'remix_album'];
                            var TYPE_LABELS = {
                                album: 'Albums', ep: 'EPs', single: 'Singles',
                                compilation: 'Compilations', live_album: 'Live Albums', remix_album: 'Remix Albums'
                            };
                            var TYPE_ICONS = {
                                album: 'bi-vinyl-fill', ep: 'bi-disc', single: 'bi-music-note-beamed',
                                compilation: 'bi-collection', live_album: 'bi-broadcast', remix_album: 'bi-soundwave'
                            };

                            var groups = {};
                            albums.forEach(function (a) {
                                var t = a.album_type || 'album';
                                if (!groups[t]) groups[t] = [];
                                groups[t].push(a);
                            });

                            Object.keys(groups).forEach(function (t) {
                                groups[t].sort(function (a, b) {
                                    var aMiss = a.is_missing ? 1 : 0;
                                    var bMiss = b.is_missing ? 1 : 0;
                                    if (aMiss !== bMiss) return aMiss - bMiss;
                                    var aY = a.album_year || 0;
                                    var bY = b.album_year || 0;
                                    if (aY !== bY) return bY - aY;
                                    return (a.album || '').localeCompare(b.album || '');
                                });
                            });

                            var html = '';
                            TYPE_ORDER.forEach(function (typeKey) {
                                var list = groups[typeKey] || [];
                                if (list.length === 0) return;
                                var label = TYPE_LABELS[typeKey] || typeKey;
                                var icon = TYPE_ICONS[typeKey] || 'bi-disc';
                                html += '<div class="mb-3">';
                                html += '<h6 class="mb-2 text-muted border-bottom pb-1"><i class="' + icon + ' me-1"></i> ' + label + ' <span class="badge bg-secondary">' + list.length + '</span></h6>';
                                html += '<div class="d-flex flex-column gap-1">';
                                list.forEach(function (album) {
                                    var yearStr = album.album_year || '—';
                                    var missingClass = album.is_missing ? 'opacity-50' : '';
                                    var missingBadge = album.is_missing ? '<span class="badge bg-warning text-dark ms-2">Missing</span>' : '';
                                    var trackBadge = '<span class="badge bg-secondary me-2">' + album.track_count + ' tracks</span>';
                                    var corrBadges = '';
                                    if (album.disc_issues) corrBadges += '<span class="badge bg-info text-dark me-1" title="Disc number issues"><i class="bi bi-disc"></i> Disc</span>';
                                    if (album.mbid_issues) corrBadges += '<button class="btn btn-sm btn-outline-danger fix-mbid-btn me-1" title="Link this album to a MusicBrainz release" data-artist="' + escHtml(artist).replace(/"/g, '&quot;') + '" data-album="' + escHtml(album.album).replace(/"/g, '&quot;') + '"><i class="bi bi-link-45deg"></i> Link MBID</button>';
                                    if (album.missing_tracks && !album.is_missing) corrBadges += '<span class="badge bg-secondary me-1" title="Missing file paths"><i class="bi bi-file-earmark-x"></i> Missing Tracks</span>';
                                    if (!corrBadges) corrBadges = '<span class="badge bg-success me-1"><i class="bi bi-check-circle"></i> OK</span>';

                                    html += '<div class="d-flex flex-wrap align-items-center gap-2 p-2 rounded ' + missingClass + '" style="background:var(--tertiary-bg);">';
                                    html += '<a href="/album/' + encodeURIComponent(artist) + '/' + encodeURIComponent(album.album) + '" class="text-decoration-none fw-semibold flex-grow-1" style="min-width:150px;">';
                                    html += escHtml(album.album) + missingBadge + '</a>';
                                    html += '<span class="text-muted small" style="width:40px;text-align:center;">' + yearStr + '</span>';
                                    html += trackBadge;
                                    html += '<span class="d-flex flex-wrap gap-1 align-items-center">' + corrBadges + '</span>';
                                    html += '<span class="d-flex gap-1 flex-shrink-0">';
                                    if (album.disc_issues) {
                                        html += '<button class="btn btn-sm btn-outline-info clear-disc-btn" title="Clear disc numbers" data-artist="' + escHtml(artist).replace(/"/g, '&quot;') + '" data-album="' + escHtml(album.album).replace(/"/g, '&quot;') + '"><i class="bi bi-eraser"></i></button>';
                                    }
                                    html += '<a href="/album/' + encodeURIComponent(artist) + '/' + encodeURIComponent(album.album) + '" class="btn btn-sm btn-outline-secondary" title="View album"><i class="bi bi-arrow-right"></i></a>';
                                    html += '</span></div>';
                                });
                                html += '</div></div>';
                            });

                            body.innerHTML = html;

                            // Event delegation for correction buttons
                            body.addEventListener('click', function (e) {
                                var clearBtn = e.target.closest('.clear-disc-btn');
                                var fixBtn = e.target.closest('.fix-mbid-btn');
                                if (clearBtn) {
                                    e.preventDefault();
                                    var artistVal = clearBtn.getAttribute('data-artist');
                                    var albumVal = clearBtn.getAttribute('data-album') || '';
                                    if (!confirm('Clear disc numbers for "' + albumVal + '" by ' + artistVal + '?')) return;
                                    fetch('/api/artist/corrections/clear-disc-number', {
                                        method: 'POST',
                                        headers: { 'Content-Type': 'application/json' },
                                        body: JSON.stringify({ artist: artistVal, album: albumVal, force_clear: true })
                                    })
                                        .then(function (r) { return r.json(); })
                                        .then(function (result) {
                                            if (result.success) {
                                                var rowDiv = clearBtn.closest('.d-flex.flex-wrap.align-items-center');
                                                if (rowDiv) {
                                                    var discBadge = rowDiv.querySelector('.badge.bg-info');
                                                    if (discBadge) discBadge.remove();
                                                    clearBtn.remove();
                                                    var remaining = rowDiv.querySelectorAll('.badge.bg-info, .badge.bg-danger, .badge.bg-secondary');
                                                    if (remaining.length === 0) {
                                                        var corrSpan = rowDiv.querySelector('.d-flex.flex-wrap.gap-1.align-items-center');
                                                        if (corrSpan) corrSpan.innerHTML = '<span class="badge bg-success me-1"><i class="bi bi-check-circle"></i> OK</span>';
                                                    }
                                                }
                                            } else {
                                                alert('Error: ' + (result.error || 'Failed'));
                                            }
                                        })
                                        .catch(function (err) { alert('Request failed: ' + err); });
                                }
                                if (fixBtn) {
                                    e.preventDefault();
                                    var artistVal2 = fixBtn.getAttribute('data-artist');
                                    var albumVal2 = fixBtn.getAttribute('data-album');
                                    if (typeof populateMusicBrainzSearch === 'function') {
                                        showMusicBrainzModal();
                                        setTimeout(function () { populateMusicBrainzSearch(artistVal2, albumVal2); }, 400);
                                    } else if (typeof searchMusicBrainzRelease === 'function') {
                                        searchMusicBrainzRelease(null, artistVal2, albumVal2, null);
                                    } else {
                                        alert('MusicBrainz search is not available on this page.');
                                    }
                                }
                            });
                        })
                        .catch(function (err) {
                            loading.classList.add('d-none');
                            body.innerHTML = '<div class="text-danger small py-2">Failed to load albums: ' + err.message + '</div>';
                        });
                }
            } else {
                detailRow.classList.add('d-none');
                this.innerHTML = '<i class="bi bi-collection"></i>';
            }
        });
    });
});
