// Album Detail Page JS
// Extracted from templates/pages/album_detail.html

var _pageData = window._pageData || {};

    let currentEditTrackId = null;
    let currentEditTrackTitle = null;
    if (typeof addGenreModalInstance === 'undefined') {
        var addGenreModalInstance = null;
    }
    let currentAlbumGenres = new Set();

    // ── Built-in player helpers ───────────────────────────────────────────────
    function playTrackFromAlbum(btn) {
        if (typeof Player === 'undefined') return;
        const trackId  = btn.dataset.trackId;
        const title    = btn.dataset.title;
        const artist   = btn.dataset.artist;
        const artUrl   = btn.dataset.art;
        Player.playTrack(trackId, title, artist, artUrl);
    }

    function playAlbum() {
        if (typeof Player === 'undefined') return;
        const btns = Array.from(document.querySelectorAll('.player-play-btn'));
        if (!btns.length) {
            alert('No downloaded tracks found on this album.');
            return;
        }
        const tracks = btns.map(function (btn) {
            return {
                id: btn.dataset.trackId,
                title: btn.dataset.title,
                artist: btn.dataset.artist,
                albumArtUrl: btn.dataset.art
            };
        });
        Player.playQueue(tracks);
    }
    // ─────────────────────────────────────────────────────────────────────────

    // "Lookup on MusicBrainz" — reuses the shared MusicBrainz release search
    // component (the same one wired into the download queue's matched-folders
    // flow) so every lookup in the app follows the same selection contract.
    // On selection the release-group MBID is applied to the album form and the
    // official tracklist comparison is triggered.
    function openAlbumLookupModal() {
        const artist = _pageData.artistName || '';
        const album = _pageData.albumName || '';

        window._mbSearchCallback = function (selected) {
            window._mbSearchCallback = null;
            if (!selected) return;
            const group = selected.release || {};
            // The shared component hands back the release-GROUP MBID in
            // selected.id (or selected.release.id); a concrete release id is
            // only present when a specific release was chosen from the list.
            const rgMbid = group.id || selected.id;
            const releaseMbid = (selected.id && selected.id !== rgMbid) ? selected.id : '';
            const year = (group.first_release_date || '').toString().split('-')[0] || '';
            const albumType = _buildAlbumType(group.primary_type, group.secondary_types);
            const cover = group.cover_art_url || '';
            populateAlbumFields(
                selected.title || group.title || album,
                year,
                albumType,
                releaseMbid,
                '',
                cover,
                '',
                rgMbid
            );
        };

        // The shared /api/musicbrainz/search endpoint strips release-groups
        // already in the library by default (discovery mode). This lookup
        // targets the album we're already viewing, so include owned releases
        // — otherwise every match is filtered out and the modal shows
        // "No results found".
        window._mbSearchIncludeOwned = true;
        if (typeof window.populateMusicBrainzSearch === 'function') {
            window.populateMusicBrainzSearch(artist, album, '', '');
        }
        if (typeof window.showMusicBrainzModal === 'function') {
            window.showMusicBrainzModal();
        }
    }

    // Search button of the album lookup modal — uses the modal's artist/album
    // inputs (falling back to the page context) and renders MusicBrainz
    // release matches via displayAlbumResults().
    function runAlbumLookup() {
        const artist = (document.getElementById('albumLookupArtist').value || '').trim() || _pageData.artistName;
        const album = (document.getElementById('albumLookupAlbum').value || '').trim() || _pageData.albumName;
        if (!artist || !album) {
            alert('Please enter both artist and album names.');
            return;
        }
        const resultsDiv = document.getElementById('albumLookupResults');
        resultsDiv.innerHTML = '<div class="spinner-border spinner-border-sm" role="status"><span class="visually-hidden">Loading...</span></div> Searching MusicBrainz...';

        fetch('/api/album/musicbrainz', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ album: album, artist: artist, existing_mbid: (document.getElementById('album_mbid') || {}).value || null })
        })
        .then(response => {
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            return response.json();
        })
        .then(data => {
            if (data.error) {
                resultsDiv.innerHTML = '<div class="alert alert-danger">Error: ' + escapeHtml(data.error) + '</div>';
                return;
            }
            displayAlbumResults(data.results, 'musicbrainz');
        })
        .catch(error => {
            resultsDiv.innerHTML = '<div class="alert alert-danger">Network error: ' + escapeHtml(error.message) + '</div>';
        });
    }

    function lookupAlbumMusicBrainz() {
        const album = _pageData.albumName;
        const artist = _pageData.artistName;
        // Pass the existing stored MBID (release MBID) so the API can do a direct
        // lookup for it and include it in results for comparison.
        const existingMbid = (document.getElementById('album_mbid') || {}).value || '';
        
        const resultsDiv = document.getElementById('albumLookupResults');
        resultsDiv.innerHTML = '<div class="spinner-border spinner-border-sm" role="status"><span class="visually-hidden">Loading...</span></div> Searching MusicBrainz...';
        
        fetch('/api/album/musicbrainz', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ album: album, artist: artist, existing_mbid: existingMbid || null })
        })
        .then(response => {
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            const contentType = response.headers.get('content-type');
            if (!contentType || !contentType.includes('application/json')) {
                throw new Error('Response is not JSON');
            }
            return response.json();
        })
        .then(data => {
            if (data.error) {
                resultsDiv.innerHTML = '<div class="alert alert-danger">Error: ' + data.error + '</div>';
                return;
            }
            displayAlbumResults(data.results, 'musicbrainz');
        })
        .catch(error => {
            resultsDiv.innerHTML = '<div class="alert alert-danger">Network error: ' + error.message + '</div>';
        });
    }

    function lookupAlbumMusicBrainzManual() {
        const artist = (document.getElementById('mbManualArtist').value || '').trim();
        const album = (document.getElementById('mbManualAlbum').value || '').trim();
        if (!artist || !album) {
            alert('Please enter both artist and album names.');
            return;
        }
        const resultsDiv = document.getElementById('albumLookupResults');
        resultsDiv.innerHTML = '<div class="spinner-border spinner-border-sm" role="status"><span class="visually-hidden">Loading...</span></div> Searching MusicBrainz...';

        fetch('/api/album/musicbrainz', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ album: album, artist: artist, existing_mbid: null })
        })
        .then(response => response.json())
        .then(data => {
            if (data.error) {
                resultsDiv.innerHTML = '<div class="alert alert-danger">Error: ' + escapeHtml(data.error) + '</div>';
                return;
            }
            displayAlbumResults(data.results, 'musicbrainz');
        })
        .catch(error => {
            resultsDiv.innerHTML = '<div class="alert alert-danger">Network error: ' + escapeHtml(error.message) + '</div>';
        });
    }

    function lookupAlbumDiscogs() {
        const album = _pageData.albumName;
        const artist = _pageData.artistName;
        
        const resultsDiv = document.getElementById('albumLookupResults');
        resultsDiv.innerHTML = '<div class="spinner-border spinner-border-sm" role="status"><span class="visually-hidden">Loading...</span></div> Searching Discogs...';
        
        fetch('/api/album/discogs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ album: album, artist: artist })
        })
        .then(response => {
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            const contentType = response.headers.get('content-type');
            if (!contentType || !contentType.includes('application/json')) {
                throw new Error('Response is not JSON');
            }
            return response.json();
        })
        .then(data => {
            if (data.error) {
                resultsDiv.innerHTML = '<div class="alert alert-danger">Error: ' + data.error + '</div>';
                return;
            }
            displayAlbumResults(data.results, 'discogs');
        })
        .catch(error => {
            resultsDiv.innerHTML = '<div class="alert alert-danger">Network error: ' + error.message + '</div>';
        });
    }

    function _buildAlbumType(primaryType, secondaryTypes) {
        const primary = (primaryType || 'album').toLowerCase().trim();
        const secondary = (secondaryTypes || []).map(s => s.toLowerCase().trim());
        const displayable = secondary.find(s => ['compilation', 'live', 'remix', 'soundtrack', 'spokenword', 'demo', 'dj-mix', 'mixtape/street'].includes(s));
        if (displayable && primary === 'album') {
            let norm = displayable;
            if (norm === 'spokenword') norm = 'spoken word';
            if (norm === 'dj-mix') norm = 'dj mix';
            return `${primary}+${norm}`;
        }
        return primary;
    }

    function displayAlbumResults(results, source) {
        const resultsDiv = document.getElementById('albumLookupResults');
        
        if (!results || results.length === 0) {
            if (source === 'musicbrainz') {
                // Offer to submit to MusicBrainz if no results found
                const album = _pageData.albumName;
                const artist = _pageData.artistName;
                resultsDiv.innerHTML = `
                    <div class="alert alert-warning">
                        <p><strong>No MusicBrainz matches found for "${album}" by ${artist}</strong></p>
                        <p>This album might not exist on MusicBrainz yet. You can help by submitting it!</p>
                        <button class="btn btn-sm btn-primary" onclick="showMusicBrainzSubmission('${escapeJsString(album)}', '${escapeJsString(artist)}')">
                            <i class="bi bi-cloud-upload"></i> Submit Album to MusicBrainz
                        </button>
                    </div>
                `;
            } else {
                resultsDiv.innerHTML = '<div class="alert alert-info">No results found</div>';
            }
            return;
        }
        
        let html = '<div class="list-group">';
        if (source === 'musicbrainz') {
            results.forEach(result => {
                const confidencePercent = (result.confidence * 100).toFixed(1);
                const confidenceClass = result.confidence > 0.8 ? 'success' : result.confidence > 0.5 ? 'warning' : 'secondary';
                const year = (result.first_release_date || '').slice(0, 4);
                const isStoredMbid = result.is_stored_mbid === true;
                const storedBadge = isStoredMbid ? '<span class="badge bg-dark me-2"><i class="bi bi-bookmark-fill me-1"></i>Currently stored</span>' : '';
                const itemStyle = isStoredMbid ? 'border-left: 3px solid #6c757d; background: rgba(108,117,125,0.05);' : '';
                const combinedType = _buildAlbumType(result.primary_type, result.secondary_types);
                const typeLabel = result.secondary_types && result.secondary_types.length > 0
                    ? `${result.primary_type || 'Album'} (${result.secondary_types.join(', ')})`
                    : (result.primary_type || 'Album');
                
                html += `
                    <div class="list-group-item list-group-item-action" style="cursor: pointer; padding: 1rem; ${itemStyle}">
                        <div class="d-flex gap-3">
                            ${result.cover_art_url ? `
                                <img src="${result.cover_art_url}" 
                                     alt="${result.title}" 
                                     style="width: 80px; height: 80px; object-fit: cover; border-radius: 4px;"
                                     onerror="this.style.display='none'">
                            ` : ''}
                            <div class="flex-grow-1">
                                <div class="d-flex justify-content-between align-items-start mb-2">
                                    <div>
                                        <h6 class="mb-1">${storedBadge}${result.title}</h6>
                                        <small class="text-muted">by ${result.artist}</small>
                                    </div>
                                    <span class="badge bg-${confidenceClass}">${confidencePercent}% match</span>
                                </div>
                                <div class="small text-muted mb-2">
                                    <span class="badge bg-info me-1">${typeLabel}</span>
                                    ${year ? `<span class="badge bg-secondary">${year}</span>` : ''}
                                    ${isStoredMbid ? `<span class="badge bg-light text-dark border me-1">${result.mbid_type === 'release' ? 'Release ID' : 'Release Group ID'}</span>` : ''}
                                </div>
                                <div class="small text-muted mb-2">
                                    <strong>MBID:</strong> <code style="font-size: 0.75rem;">${result.mbid}</code>
                                </div>
                                 <div class="d-flex gap-2 flex-wrap">
                                    ${result.mbid_type === 'release'
                                        ? `<button class="btn btn-sm btn-primary" onclick="populateAlbumFields('${escapeJsString(result.title)}', '${year}', '${combinedType}', '${result.mbid}', '', '${result.cover_art_url || ''}', '')">
                                            <i class="bi bi-check-circle"></i> Use This Album
                                        </button>`
                                        : `<button class="btn btn-sm btn-primary" onclick="autoPickRelease('${result.mbid}', '${escapeJsString(result.title)}', '${year}', '${combinedType}', '${result.cover_art_url || ''}')">
                                            <i class="bi bi-check-circle"></i> Use This Album
                                        </button>`}
                                    <button class="btn btn-sm btn-outline-secondary" onclick="openReleasePickerModal('${result.mbid}', '${escapeJsString(result.title)}', '${year}', '${combinedType}')" title="Show all specific releases in this release group">
                                        <i class="bi bi-list-ul"></i> Pick Release
                                    </button>
                                </div>
                            </div>
                        </div>
                    </div>
                `;
            });
        } else {
            results.forEach(result => {
                const genres = (result.genre || []).join(', ');
                const styles = (result.style || []).join(', ');
                const confidencePercent = result.confidence ? (result.confidence * 100).toFixed(1) : 0;
                const confidenceClass = result.confidence > 0.8 ? 'success' : result.confidence > 0.5 ? 'warning' : 'secondary';
                
                html += `
                    <div class="list-group-item">
                        <div style="padding: 1rem;">
                            <div class="d-flex justify-content-between align-items-start mb-2">
                                <div>
                                    <h6 class="mb-1">${result.title}</h6>
                                </div>
                                <span class="badge bg-${confidenceClass}">${confidencePercent}% match</span>
                            </div>
                            <p class="text-muted mb-2" style="font-size: 0.85rem;">
                                <strong>Year:</strong> ${result.year || '—'}<br>
                                <strong>Format:</strong> ${(result.format || []).join(', ') || '—'}<br>
                                ${result.discogs_id ? '<strong>Discogs ID:</strong> ' + result.discogs_id + '<br>' : ''}
                            </p>
                            ${genres ? '<p class="mb-2" style="font-size: 0.85rem;"><strong>Genres:</strong> ' + genres + '</p>' : ''}
                            ${styles ? '<p class="mb-2" style="font-size: 0.85rem;"><strong>Styles:</strong> ' + styles + '</p>' : ''}
                            <div class="gap-2">
                                ${(() => {
                                    // Infer album type from Discogs format
                                    const formats = (result.format || []).map(f => f.toLowerCase().trim());
                                    let inferredType = '';
                                    
                                    // Check if it's explicitly a Single
                                    if (formats.some(f => f.includes('single'))) {
                                        inferredType = 'single';
                                    }
                                    // Check if it's explicitly an EP
                                    else if (formats.some(f => f.includes('ep'))) {
                                        inferredType = 'ep';
                                    }
                                    // Check if it's a Compilation
                                    else if (formats.some(f => f.includes('compilation'))) {
                                        inferredType = 'album+compilation';
                                    }
                                    // Default to album if ambiguous
                                    else {
                                        inferredType = 'album';
                                    }
                                    
                                     return `<button class="btn btn-sm btn-primary" onclick="populateAlbumFields('${escapeJsString(result.title)}', '${result.year || ''}', '${inferredType}', '', '${result.discogs_id || ''}', '', '${genres.replace(/'/g, "\\'")}', '')" style="width: 100%;">
                                        <i class="bi bi-check-circle"></i> Use This Album
                                    </button>`;
                                })()}
                            </div>
                        </div>
                    </div>
                `;
            });
        }
        html += '</div>';
        
        resultsDiv.innerHTML = html;
    }

    // ── Auto-pick release from a release group ──────────────────────────────
    async function autoPickRelease(rgMbid, title, year, albumType, coverArtUrl) {
        const artist = _pageData.artistName;
        const album = _pageData.albumName;

        try {
            const resp = await fetch('/api/album/musicbrainz/best-release', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ release_group_mbid: rgMbid, artist, album })
            });
            const data = await resp.json();

            if (!data.success) {
                openReleasePickerModal(rgMbid, title, year, albumType);
                return;
            }

            // If we have a confident match, use it directly
            if (data.best_release && data.confidence >= 0.8) {
                const r = data.best_release;
                populateAlbumFields(
                    r.title || title,
                    (r.date ? r.date.slice(0, 4) : year) || year,
                    albumType,
                    r.id,
                    '',
                    r.cover_art_url || coverArtUrl || '',
                    '',
                    rgMbid
                );
                return;
            }

            // Not confident enough — open the picker modal
            openReleasePickerModal(rgMbid, title, year, albumType, data.releases, data.best_release);
        } catch (e) {
            openReleasePickerModal(rgMbid, title, year, albumType);
        }
    }

    // ── Release Picker Modal ────────────────────────────────────────────────
    let _releasePickerCache = null; // { rgMbid, title, year, albumType, releases, bestRelease }

    async function openReleasePickerModal(rgMbid, title, year, albumType, preloadedReleases = null, preloadedBest = null) {
        const statusEl = document.getElementById('releasePickerStatus');
        const errorEl = document.getElementById('releasePickerError');
        const resultsEl = document.getElementById('releasePickerResults');

        statusEl.style.display = 'block';
        errorEl.style.display = 'none';
        resultsEl.innerHTML = '';

        const modal = new bootstrap.Modal(document.getElementById('releasePickerModal'));
        modal.show();

        let releases = preloadedReleases;
        let bestRelease = preloadedBest;

        if (!releases) {
            try {
                const resp = await fetch('/api/album/musicbrainz/release-group/releases', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ release_group_mbid: rgMbid })
                });
                const data = await resp.json();
                if (!data.success || !data.releases || data.releases.length === 0) {
                    statusEl.style.display = 'none';
                    errorEl.textContent = 'No specific releases found for this release group.';
                    errorEl.style.display = 'block';
                    return;
                }
                releases = data.releases;
            } catch (e) {
                statusEl.style.display = 'none';
                errorEl.textContent = 'Error fetching releases: ' + e.message;
                errorEl.style.display = 'block';
                return;
            }
        }

        _releasePickerCache = { rgMbid, title, year, albumType, releases, bestRelease };
        statusEl.style.display = 'none';
        _renderReleasePickerResults(releases, year, albumType, bestRelease);
    }

    function _renderReleasePickerResults(releases, year, albumType, bestRelease) {
        const resultsEl = document.getElementById('releasePickerResults');
        let html = '<div class="list-group list-group-flush">';

        for (let idx = 0; idx < releases.length; idx++) {
            const r = releases[idx];
            const isBest = bestRelease && r.id === bestRelease.id;
            const fmt = escapeHtml(r.formats.join(' + ') || 'Unknown format');
            const discs = r.disc_count > 1 ? ` · ${r.disc_count} discs` : '';
            const country = r.country ? ` · ${escapeHtml(r.country)}` : '';
            const disambiguation = r.disambiguation ? ` (${escapeHtml(r.disambiguation)})` : '';
            const safeTitle = escapeHtml(r.title);
            const safeId = escapeHtml(r.id);
            const bestBadge = isBest ? '<span class="badge bg-success ms-2">Best Match</span>' : '';

            html += `
                <div class="list-group-item">
                    <div class="d-flex justify-content-between align-items-center flex-wrap gap-2">
                        <div>
                            <strong>${safeTitle}${disambiguation}</strong>${bestBadge}
                            <div class="text-muted small">
                                ${escapeHtml(r.date || '?')}${country} · ${fmt} · ${r.track_count} tracks${discs}
                                ${r.status ? ' · <span class="badge bg-light text-dark border">' + escapeHtml(r.status) + '</span>' : ''}
                            </div>
                        </div>
                        <div class="d-flex gap-2 align-items-center">
                            <button class="btn btn-sm btn-outline-info" onclick="toggleReleaseTracks('${safeId}', ${idx})" id="btn-toggle-tracks-${safeId}">
                                <i class="bi bi-music-note-list"></i> Tracks
                            </button>
                            <button class="btn btn-sm btn-primary" onclick="populateAlbumFields('${escapeJsString(r.title)}', '${escapeHtml(r.date ? r.date.slice(0,4) : year)}', '${escapeHtml(albumType)}', '${safeId}', '', '${escapeJsString(r.cover_art_url || '')}', '', '${escapeJsString(_releasePickerCache.rgMbid || '')}')">
                                <i class="bi bi-check-circle"></i> Select
                            </button>
                        </div>
                    </div>
                    <div id="release-tracks-${safeId}" class="mt-2" style="display:none;">
                        <div class="text-muted small ps-3">
                            <span class="spinner-border spinner-border-sm" role="status"></span> Loading tracks…
                        </div>
                    </div>
                </div>
            `;
        }
        html += '</div>';
        resultsEl.innerHTML = html;
    }

    async function toggleReleaseTracks(releaseMbid, idx) {
        const container = document.getElementById('release-tracks-' + releaseMbid);
        const btn = document.getElementById('btn-toggle-tracks-' + releaseMbid);
        if (!container || !btn) return;

        if (container.style.display !== 'none') {
            container.style.display = 'none';
            btn.innerHTML = '<i class="bi bi-music-note-list"></i> Tracks';
            return;
        }

        container.style.display = '';
        btn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status"></span>';

        try {
            const resp = await fetch('/api/album/musicbrainz/release/tracks', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ release_mbid: releaseMbid })
            });
            const data = await resp.json();
            btn.innerHTML = '<i class="bi bi-music-note-list"></i> Tracks';

            if (!data.success || !data.tracks || data.tracks.length === 0) {
                container.innerHTML = '<div class="text-muted small ps-3">No track data available.</div>';
                return;
            }

            let html = '<div class="small ps-3"><ol class="mb-0">';
            for (const t of data.tracks) {
                const dur = t.duration_ms ? ` (${Math.round(t.duration_ms / 1000)}s)` : '';
                html += `<li>${escapeHtml(t.title)}${dur}</li>`;
            }
            html += '</ol></div>';
            container.innerHTML = html;
        } catch (e) {
            btn.innerHTML = '<i class="bi bi-music-note-list"></i> Tracks';
            container.innerHTML = `<div class="text-danger small ps-3">Error: ${escapeHtml(e.message)}</div>`;
        }
    }

    function applyAlbumMBID(mbid, title, coverArtUrl) {
        // Update all tracks in this album with the MBID and cover art
        fetch('/api/album/apply-mbid', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ 
                artist: _pageData.artistName, 
                album: _pageData.albumName,
                mbid: mbid,
                cover_art_url: coverArtUrl
            })
        })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                alert('✅ Applied MusicBrainz metadata to album!');
                location.reload();
            } else {
                alert('❌ Error: ' + (data.error || 'Failed to apply'));
            }
        })
        .catch(error => {
            alert('❌ Network error: ' + error.message);
        });
    }

    function applyAlbumDiscogsID(discogsID) {
        // Update all tracks in this album with the Discogs ID
        fetch('/api/album/apply-discogs-id', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ 
                artist: _pageData.artistName, 
                album: _pageData.albumName,
                discogs_id: discogsID
            })
        })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                alert('✅ Applied Discogs ID to album!');
                location.reload();
            } else {
                alert('❌ Error: ' + (data.error || 'Failed to apply'));
            }
        })
        .catch(error => {
            alert('❌ Network error: ' + error.message);
        });
    }

    function applyAlbumGenres(genres) {
        alert('Applied Discogs Genres: ' + genres);
    }

    function showMusicBrainzSubmission(albumName, artistName) {
        // Generate submission URL with pre-filled data
        fetch('/api/album/submit-musicbrainz', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                album: albumName,
                artist: artistName
            })
        })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                // Show modal with submission URL
                const modalHtml = `
                    <div class="modal fade" id="mbSubmissionModal" tabindex="-1">
                        <div class="modal-dialog modal-lg">
                            <div class="modal-content">
                                <div class="modal-header">
                                    <h5 class="modal-title"><i class="bi bi-cloud-upload"></i> Submit to MusicBrainz</h5>
                                    <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                                </div>
                                <div class="modal-body">
                                    <div class="alert alert-info">
                                        <strong>Help improve MusicBrainz!</strong> This album doesn't exist in MusicBrainz yet.
                                    </div>
                                    <div class="mb-3">
                                        <label class="form-label"><strong>Step 1: Pre-filled Information</strong></label>
                                        <div class="card bg-light p-3">
                                            <p class="mb-1"><strong>Album:</strong> ${data.message.split("submitting").length > 1 ? albumName : albumName}</p>
                                            <p class="mb-0"><strong>Artist:</strong> ${artistName}</p>
                                        </div>
                                    </div>
                                    <div class="mb-3">
                                        <label class="form-label"><strong>Step 2: Open MusicBrainz</strong></label>
                                        <p class="text-muted">Click the button below to open MusicBrainz. Your album and artist information will be pre-filled.</p>
                                        <button class="btn btn-primary w-100" onclick="window.open('${data.submission_url}', '_blank')">
                                            <i class="bi bi-box-arrow-up-right"></i> Open MusicBrainz Submission Page
                                        </button>
                                    </div>
                                    <div class="mb-3">
                                        <label class="form-label"><strong>Step 3: Complete the Form</strong></label>
                                        <ul class="text-muted small">
                                            <li>Fill in the release date (if known)</li>
                                            <li>Add barcode (if available)</li>
                                            <li>Select the correct release type (Album/Single/EP)</li>
                                            <li>Add track list (copy from Spotify or Discogs)</li>
                                            <li>Click "Submit" to create an edit</li>
                                        </ul>
                                    </div>
                                    <div class="alert alert-success">
                                        <strong>💡 Tip:</strong> After submission, your edit will be reviewed by MusicBrainz editors. You can reference Spotify or Discogs information to help with verification.
                                    </div>
                                </div>
                                <div class="modal-footer">
                                    <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Close</button>
                                    <button type="button" class="btn btn-primary" onclick="window.open('${data.submission_url}', '_blank'); document.getElementById('mbSubmissionModal').modal.hide?.();">
                                        <i class="bi bi-cloud-upload"></i> Open & Close
                                    </button>
                                </div>
                            </div>
                        </div>
                    </div>
                `;
                
                document.body.insertAdjacentHTML('beforeend', modalHtml);
                const modal = new bootstrap.Modal(document.getElementById('mbSubmissionModal'));
                modal.show();
            } else {
                alert('❌ Error: ' + (data.error || 'Failed to generate submission link'));
            }
        })
        .catch(error => {
            alert('❌ Network error: ' + error.message);
        });
    }

    function openSlskdSearchAlbum(artist, album) {
        const query = `${artist} ${album}`
            .replace(/\\u0026/gi, ' ')
            .replace(/&amp;/gi, ' ')
            .replace(/&/g, ' ')
            .replace(/\s+/g, ' ')
            .trim();
        // Manual Soulseek search page — the query is prefilled (editable)
        // and the search runs on load (search_init.js reads the q= param).
        window.location.href = `/downloads/search?q=${encodeURIComponent(query)}`;
    }

    function openAlbumArtModal() {
        const artistName = _pageData.artistName;
        const albumName = _pageData.albumName;
        
        const modalHtml = `
            <div class="modal fade" id="albumArtModal" tabindex="-1">
                <div class="modal-dialog modal-lg">
                    <div class="modal-content">
                        <div class="modal-header">
                            <h5 class="modal-title"><i class="bi bi-image"></i> Change Album Art</h5>
                            <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                        </div>
                        <div class="modal-body">
                            <ul class="nav nav-tabs mb-3" id="albumArtTabs" role="tablist">
                                <li class="nav-item" role="presentation">
                                    <button class="nav-link active" id="albumArtUrlTab" data-bs-toggle="tab" data-bs-target="#albumArtUrlPane" type="button" role="tab">
                                        <i class="bi bi-link-45deg"></i> URL
                                    </button>
                                </li>
                                <li class="nav-item" role="presentation">
                                    <button class="nav-link" id="albumArtUploadTab" data-bs-toggle="tab" data-bs-target="#albumArtUploadPane" type="button" role="tab">
                                        <i class="bi bi-upload"></i> Upload Image
                                    </button>
                                </li>
                                <li class="nav-item" role="presentation">
                                    <button class="nav-link" id="albumArtSearchTab" data-bs-toggle="tab" data-bs-target="#albumArtSearchPane" type="button" role="tab">
                                        <i class="bi bi-search"></i> Search
                                    </button>
                                </li>
                            </ul>
                            <div class="tab-content">
                                <div class="tab-pane fade show active" id="albumArtUrlPane" role="tabpanel">
                                    <div class="mb-3">
                                        <label for="manualAlbumArtUrl" class="form-label">Image URL</label>
                                        <input type="text" class="form-control" id="manualAlbumArtUrl" placeholder="https://example.com/album-art.jpg">
                                    </div>
                                    <button class="btn btn-primary" onclick="applyManualAlbumArt('${escapeHtml(artistName)}', '${escapeHtml(albumName)}')">
                                        <i class="bi bi-check"></i> Apply URL
                                    </button>
                                </div>
                                <div class="tab-pane fade" id="albumArtUploadPane" role="tabpanel">
                                    <div class="mb-3">
                                        <label for="manualAlbumArtFile" class="form-label">Select Image File</label>
                                        <input type="file" class="form-control" id="manualAlbumArtFile" accept="image/*">
                                    </div>
                                    <div id="albumArtUploadPreview" class="mb-3" style="display:none;">
                                        <img id="albumArtPreviewImg" src="" alt="Preview" style="max-height:200px; max-width:100%; border-radius:0.375rem;">
                                    </div>
                                    <button class="btn btn-primary" onclick="uploadAlbumArtFile('${escapeHtml(artistName)}', '${escapeHtml(albumName)}')">
                                        <i class="bi bi-upload"></i> Upload Image
                                    </button>
                                </div>
                                <div class="tab-pane fade" id="albumArtSearchPane" role="tabpanel">
                                    <div class="d-flex gap-2 mb-3 flex-wrap">
                                        <button class="btn btn-secondary" onclick="searchAlbumArt('${escapeHtml(artistName)}', '${escapeHtml(albumName)}', 'musicbrainz')">
                                            <i class="bi bi-search"></i> MusicBrainz
                                        </button>
                                        <button class="btn btn-secondary" onclick="searchAlbumArt('${escapeHtml(artistName)}', '${escapeHtml(albumName)}', 'discogs')">
                                            <i class="bi bi-disc"></i> Discogs
                                        </button>
                                        <button class="btn btn-secondary" onclick="searchAlbumArt('${escapeHtml(artistName)}', '${escapeHtml(albumName)}', 'applemusic')">
                                            <i class="bi bi-apple"></i> Apple Music
                                        </button>
                                    </div>
                                    <div id="albumArtResults"></div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        `;
        
        // Remove existing modal if any
        const existingModal = document.getElementById('albumArtModal');
        if (existingModal) existingModal.remove();
        
        document.body.insertAdjacentHTML('beforeend', modalHtml);

        // Wire up file input preview
        const fileInput = document.getElementById('manualAlbumArtFile');
        if (fileInput) {
            fileInput.addEventListener('change', function() {
                const file = this.files[0];
                const preview = document.getElementById('albumArtUploadPreview');
                const previewImg = document.getElementById('albumArtPreviewImg');
                if (file && file.type.startsWith('image/')) {
                    const reader = new FileReader();
                    reader.onload = e => {
                        previewImg.src = e.target.result;
                        preview.style.display = 'block';
                    };
                    reader.readAsDataURL(file);
                } else {
                    preview.style.display = 'none';
                    previewImg.src = '';
                }
            });
        }

        const modal = new bootstrap.Modal(document.getElementById('albumArtModal'));
        modal.show();
    }

    function applyManualAlbumArt(artistName, albumName) {
        const url = document.getElementById('manualAlbumArtUrl').value;
        if (!url) {
            alert('Please enter an image URL');
            return;
        }
        applyAlbumArt(artistName, albumName, url);
    }

    function uploadAlbumArtFile(artistName, albumName) {
        const fileInput = document.getElementById('manualAlbumArtFile');
        if (!fileInput || !fileInput.files || !fileInput.files[0]) {
            alert('Please select an image file');
            return;
        }
        const file = fileInput.files[0];
        const formData = new FormData();
        formData.append('artist', artistName);
        formData.append('album', albumName);
        formData.append('image', file);

        fetch('/api/album/upload-art', {
            method: 'POST',
            body: formData
        })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                const modal = bootstrap.Modal.getInstance(document.getElementById('albumArtModal'));
                if (modal) modal.hide();
                const img = document.getElementById('albumArtImage');
                if (img) {
                    img.src = img.src.split('?')[0] + '?t=' + Date.now();
                }
                const filesUpdated = typeof data.files_updated === 'number' ? data.files_updated : null;
                const msg = filesUpdated !== null
                    ? `✅ Album art uploaded successfully! Embedded in ${filesUpdated} track file(s).`
                    : '✅ Album art uploaded successfully!';
                alert(msg);
            } else {
                alert('❌ Error: ' + (data.error || 'Failed to upload album art'));
            }
        })
        .catch(error => {
            alert('❌ Network error: ' + error.message);
        });
    }

    function searchAlbumArt(artistName, albumName, source) {
        const resultsDiv = document.getElementById('albumArtResults');
        resultsDiv.innerHTML = '<div class="text-center"><span class="spinner-border"></span> Searching...</div>';

        fetch(`/api/album/search-art?artist=${encodeURIComponent(artistName)}&album=${encodeURIComponent(albumName)}&source=${source}`)
            .then(r => r.json())
            .then(data => {
                if (data.error || !data.images || data.images.length === 0) {
                    resultsDiv.innerHTML = '<div class="alert alert-info">No album art found</div>';
                    return;
                }

                let html = '<div class="row g-3">';
                data.images.forEach(img => {
                    html += `
                        <div class="col-6 col-md-4">
                            <div class="card">
                                <img src="${escapeHtml(img.url)}" class="card-img-top" style="height: 200px; object-fit: cover;" 
                                     onerror="this.parentElement.parentElement.style.display='none'">
                                <div class="card-body p-2">
                                    <small class="text-muted d-block mb-1">${escapeHtml(img.title || '')} - ${escapeHtml(img.artist || '')}</small>
                                    <button class="btn btn-sm btn-primary w-100" onclick="applyAlbumArt('${escapeHtml(artistName)}', '${escapeHtml(albumName)}', '${escapeHtml(img.url)}')">
                                        <i class="bi bi-check"></i> Use This
                                    </button>
                                </div>
                            </div>
                        </div>
                    `;
                });
                html += '</div>';
                resultsDiv.innerHTML = html;
            })
            .catch(error => {
                resultsDiv.innerHTML = `<div class="alert alert-danger">Error: ${escapeHtml(error.message)}</div>`;
            });
    }

    function applyAlbumArt(artistName, albumName, imageUrl) {
        fetch('/api/album/set-art', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ 
                artist: artistName, 
                album: albumName,
                image_url: imageUrl
            })
        })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                // Close modal
                const modal = bootstrap.Modal.getInstance(document.getElementById('albumArtModal'));
                if (modal) modal.hide();
                
                // Reload album art with cache busting
                const img = document.getElementById('albumArtImage');
                if (img) {
                    img.src = img.src.split('?')[0] + '?t=' + Date.now();
                }
                
                const filesUpdated = typeof data.files_updated === 'number' ? data.files_updated : null;
                const msg = filesUpdated !== null
                    ? `✅ Album art updated successfully! Embedded in ${filesUpdated} track file(s).`
                    : '✅ Album art updated successfully!';
                alert(msg);
            } else {
                alert('❌ Error: ' + (data.error || 'Failed to update album art'));
            }
        })
        .catch(error => {
            alert('❌ Network error: ' + error.message);
        });
    }

    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    function ignoreMissingTrack(btn) {
        const albumArtist = _pageData.artistName;
        const albumName = _pageData.albumName;
        const missingId = btn.dataset.missingId || '';
        const title = btn.dataset.title || '';
        const discNumber = parseInt(btn.dataset.discNumber || '1', 10);

        if (!confirm(`Hide "${title}" from the missing tracks list?\n\nYou can restore it by running a new MusicBrainz comparison.`)) return;

        const row = btn.closest('tr');

        fetch('/api/album/ignore-missing-track', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                id: missingId || undefined,
                artist: albumArtist,
                album: albumName,
                title: title,
                disc_number: discNumber,
            })
        })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                if (row) row.remove();
            } else {
                alert('❌ Error: ' + (data.error || 'Failed to ignore track'));
            }
        })
        .catch(err => {
            alert('❌ Network error: ' + err.message);
        });
    }

    // Genre recommendation functions
    let selectedGenres = new Set();

    function fetchGenreRecommendations() {
        const artistName = _pageData.artistName;
        const albumName = _pageData.albumName;
        const btn = document.getElementById('fetchGenresBtn');
        const container = document.getElementById('recommendedGenres');
        const section = document.getElementById('recommendedGenresSection');
        
        // Show the recommendations section
        section.style.display = 'block';
        
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Fetching...';
        container.innerHTML = '<span class="text-muted small">Loading recommendations...</span>';
        
        // Fetch from MusicBrainz and Discogs (Spotify was removed).
        Promise.all([
            fetch('/api/album/musicbrainz', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ album: albumName, artist: artistName })
            }).then(r => r.json()).catch(() => ({results: []})),
            
            fetch('/api/album/discogs', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ album: albumName, artist: artistName })
            }).then(r => r.json()).catch(() => ({results: []}))
        ])
        .then(([mbData, discogsData]) => {
            const genres = new Set();
            
            // Extract genres from Discogs results
            if (discogsData.results && discogsData.results.length > 0) {
                discogsData.results.forEach(result => {
                    if (result.genre) {
                        result.genre.forEach(g => genres.add(g));
                    }
                    if (result.style) {
                        result.style.forEach(s => genres.add(s));
                    }
                });
            }
            
            if (genres.size === 0) {
                container.innerHTML = '<span class="text-muted small">No genre recommendations found from external sources</span>';
                btn.disabled = false;
                btn.innerHTML = '<i class="bi bi-cloud-download"></i> Get Online Suggestions';
                return;
            }
            
            // Display genres as selectable badges - clicking adds to form
            let html = '';
            Array.from(genres).sort().forEach(genre => {
                const isAlreadyAdded = currentAlbumGenres.has(genre);
                html += `<span class="badge ${isAlreadyAdded ? 'bg-success' : 'badge-outline-primary'} genre-badge" 
                    onclick="toggleGenreSelection('${escapeHtml(genre)}')" 
                    data-genre="${escapeHtml(genre)}"
                    style="cursor: pointer; border: 2px solid #0d6efd; ${isAlreadyAdded ? 'background-color: #198754 !important; color: #fff !important; border-color: #198754 !important;' : 'background-color: transparent; color: #0d6efd;'}; padding: 0.35rem 0.65rem;">
                    ${escapeHtml(genre)}
                </span>`;
            });
            container.innerHTML = html;
            
            btn.disabled = false;
            btn.innerHTML = '<i class="bi bi-cloud-download"></i> Get Online Suggestions';
        })
        .catch(error => {
            container.innerHTML = '<span class="text-danger small">Error: ' + error.message + '</span>';
            btn.disabled = false;
            btn.innerHTML = '<i class="bi bi-cloud-download"></i> Get Online Suggestions';
        });
    }

    function toggleGenreSelection(genre) {
        const badge = document.querySelector(`[data-genre="${escapeHtml(genre)}"]`);
        if (!badge) return;
        
        // Add genre to the album genres list
        if (!currentAlbumGenres.has(genre)) {
            currentAlbumGenres.add(genre);
            updateAlbumGenresDisplay();
            
            // Visual feedback - highlight the selected badge
            badge.style.backgroundColor = '#0d6efd';
            badge.style.color = '#fff';
        } else {
            // Remove if already selected
            currentAlbumGenres.delete(genre);
            updateAlbumGenresDisplay();
            
            badge.style.backgroundColor = 'transparent';
            badge.style.color = '#0d6efd';
        }
    }

    // Remove single genre from all album tracks (uses shared utilities)
    function removeGenreFromAlbum(genre) {
        const artistName = _pageData.artistName;
        const albumName = _pageData.albumName;
        handleGenreRemoval(artistName, albumName, [genre], 'album');
    }

    // Remove selected genres from all album tracks (batch removal)
    function removeSelectedAlbumGenres() {
        const artistName = _pageData.artistName;
        const albumName = _pageData.albumName;
        const selectedGenres = getSelectedGenres('albumGenresForRemoval');
        
        if (selectedGenres.length === 0) {
            alert('No genres selected');
            return;
        }
        
        handleGenreRemoval(artistName, albumName, selectedGenres, 'album');
    }
    
    // Load track-level recommendations when page loads
    async function loadTrackRecommendations() {
        const artistName = _pageData.artistName;
        const albumName = _pageData.albumName;
        
        try {
            const response = await fetch(`/api/album/${encodeURIComponent(artistName)}/${encodeURIComponent(albumName)}/track-recommendations`);
            const data = await response.json();
            
            if (!response.ok || !data.success) {
                console.debug('Could not load track recommendations:', data.error);
                return;
            }
            
            // Display recommendations for each track
            const recs = data.recommendations;
            for (const trackId in recs) {
                const trackRecs = recs[trackId];
                if (trackRecs.recommendations && trackRecs.recommendations.length > 0) {
                    displayTrackRecommendations(trackId, trackRecs.recommendations);
                }
            }
        } catch (error) {
            console.debug('Error loading track recommendations:', error);
        }
    }
    
    function displayTrackRecommendations(trackId, recommendations) {
        const suggestionsDiv = document.getElementById(`suggestions-${trackId}`);
        if (!suggestionsDiv) return;
        
        // Show the suggestions section
        suggestionsDiv.style.display = 'flex';
        
        // Build recommendation badges
        let html = '<small class="text-muted me-2" style="white-space: nowrap;">Suggested:</small>';
        recommendations.forEach(genre => {
            html += `
                <span class="badge bg-info bg-opacity-75" style="font-size: 0.7rem; cursor: pointer; position: relative;" 
                      title="Click to add to this track" onclick="quickAddGenre('${trackId}', '${escapeHtml(genre)}')">
                    ${escapeHtml(genre)}
                    <i class="bi bi-plus ms-1" style="font-size: 0.6rem;"></i>
                </span>
            `;
        });
        
        suggestionsDiv.innerHTML = html;
    }
    
    function quickAddGenre(trackId, genre) {
        // Add the genre to the track's genres
        const genresDiv = document.getElementById(`genres-${trackId}`);
        if (!genresDiv) return;
        
        // Check if genre already exists
        if (genresDiv.textContent.includes(genre)) {
            return; // Genre already added
        }
        
        // Make API call to add genre to track
        fetch('/api/tags/track/' + trackId, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                genre: genre,
                action: 'add',
                sync_to_file: true
            })
        })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                // Reload the page to show updated genres
                location.reload();
            } else {
                alert('Error adding genre: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(error => {
            alert('Network error: ' + error.message);
        });
    }
    
    // Album metadata form population function
    function populateAlbumFields(title, year, albumType, mbid, discogsId, coverArtUrl, genres, releaseGroupMbid) {
        // Close any open lookup modals (the shared MB modal lives in base.html;
        // the legacy albumLookupModal include was removed when lookups moved to
        // the shared component — guard in case a stale build still has it).
        const lookupEl = document.getElementById('albumLookupModal');
        if (lookupEl) {
            const lookupModal = bootstrap.Modal.getInstance(lookupEl);
            if (lookupModal) lookupModal.hide();
        }
        const pickerModal = bootstrap.Modal.getInstance(document.getElementById('releasePickerModal'));
        if (pickerModal) pickerModal.hide();
        
        // Populate form fields
        if (title) {
            document.getElementById('album_title').value = title;
        }
        if (year) {
            document.getElementById('release_year').value = year;
        }
        if (albumType) {
            // Normalize album type - convert standalone "compilation" to "album+compilation"
            // and "album (live)" format to "album+live"
            let normalizedType = albumType.toLowerCase().trim();
            if (normalizedType === 'compilation') {
                normalizedType = 'album+compilation';
            } else if (normalizedType.startsWith('album ') || normalizedType.startsWith('ep ') || normalizedType.startsWith('single ')) {
                // Handle formats like "album (live)" or "album (compilation)"
                const match = normalizedType.match(/^(\w+)\s*\(([^)]+)\)$/);
                if (match) {
                    const primary = match[1];
                    let secondary = match[2].trim();
                    if (secondary === 'spokenword') secondary = 'spoken word';
                    if (secondary === 'dj-mix') secondary = 'dj mix';
                    normalizedType = `${primary}+${secondary}`;
                }
            }
            const albumTypeSelect = document.getElementById('album_type');
            if (albumTypeSelect) {
                const validValues = Array.from(albumTypeSelect.options).map(o => o.value);
                if (validValues.includes(normalizedType)) {
                    albumTypeSelect.value = normalizedType;
                }
            }
        }
        if (mbid) {
            document.getElementById('album_mbid').value = mbid;
        }
        if (releaseGroupMbid) {
            const rgField = document.getElementById('album_release_group_mbid');
            if (rgField) rgField.value = releaseGroupMbid;
        }
        if (discogsId) {
            const discogsField = document.getElementById('album_discogs_id');
            if (discogsField) discogsField.value = discogsId;
        }
        if (coverArtUrl) {
            document.getElementById('cover_art_url').value = coverArtUrl;
        }
        
        // Handle genres - support both backslash (Navidrome) and comma separators
        if (genres) {
            // Split on either backslash or comma, then trim and filter
            const genreList = genres.split(/[\\,]+/).map(g => g.trim()).filter(g => g && g.length > 0);
            genreList.forEach(genre => {
                if (!currentAlbumGenres.has(genre)) {
                    currentAlbumGenres.add(genre);
                }
            });
            updateAlbumGenresDisplay();
        }
        
        // Scroll to the track list to show the comparison
        const trackTable = document.querySelector('.table-responsive');
        if (trackTable) {
            trackTable.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }

        // If a MusicBrainz release group MBID is provided, compare MB tracks with library
        const mbCompareId = releaseGroupMbid || mbid;
        if (mbCompareId) {
            compareMBTracksWithLibrary(mbCompareId);
        }
    }

    // ── MusicBrainz Track Comparison ─────────────────────────────────────────
    let mbComparisonData = null;

    async function compareMBTracksWithLibrary(releaseGroupMbid) {
        const artist = _pageData.artistName;
        const album = _pageData.albumName;

        _showMBCompareBanner(
            '<span class="spinner-border spinner-border-sm me-2" role="status"></span> Comparing tracks with MusicBrainz…',
            'info',
            false
        );

        try {
            const resp = await fetch('/api/album/musicbrainz/compare', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ release_group_mbid: releaseGroupMbid, artist: artist, album: album })
            });
            const data = await resp.json();

            if (!data.success) {
                _showMBCompareBanner(
                    '<i class="bi bi-exclamation-triangle-fill me-2"></i>Could not compare tracks: ' + escapeHtml(data.error || 'Unknown error'),
                    'warning',
                    true
                );
                return;
            }

            mbComparisonData = data;
            _displayMBComparison(data);
        } catch (err) {
            _showMBCompareBanner(
                '<i class="bi bi-x-circle-fill me-2"></i>Network error while comparing tracks: ' + escapeHtml(err.message),
                'danger',
                true
            );
        }
    }

    function _getMBCompareBannerContainer() {
        const tableResponsive = document.querySelector('.table-responsive');
        if (!tableResponsive) return null;
        return tableResponsive.parentElement;
    }

    function _showMBCompareBanner(htmlContent, type, showDismiss) {
        let banner = document.getElementById('mb-compare-banner');
        if (!banner) {
            const container = _getMBCompareBannerContainer();
            if (!container) return;
            banner = document.createElement('div');
            banner.id = 'mb-compare-banner';
            const tableResponsive = document.querySelector('.table-responsive');
            container.insertBefore(banner, tableResponsive);
        }
        const dismissBtn = showDismiss
            ? `<button type="button" class="btn-close ms-auto" onclick="clearMBComparison()" aria-label="Dismiss"></button>`
            : '';
        banner.innerHTML = `<div class="alert alert-${escapeHtml(type)} d-flex align-items-center mb-2 py-2">${htmlContent}${dismissBtn}</div>`;
    }

    function _injectMissingTrackRows(missingTracks, data) {
        const albumArtist = _pageData.artistName;
        const albumName = _pageData.albumName;
        const mbYear = escapeHtml(String(data.mb_year || ''));
        const releaseId = escapeHtml(String(data.release_group_mbid || ''));

        for (const track of missingTracks) {
            const safeTitle = escapeHtml(track.mb_title || '');
            const safeArtist = escapeHtml(track.mb_artist || albumArtist);
            const safeAlbumArtist = escapeHtml(albumArtist);
            const safeAlbum = escapeHtml(albumName);
            const safeTrackNum = escapeHtml(String(track.mb_track_number || ''));
            const safeDiscNum = escapeHtml(String(track.mb_disc_number || 1));
            const safeRecordingMbid = escapeHtml(String(track.mb_recording_id || ''));
            const safeDuration = track.mb_duration_sec != null ? String(track.mb_duration_sec) : '';

            const row = document.createElement('tr');
            row.className = 'text-muted missing-track-row mb-missing-row';
            row.style.opacity = '0.6';
            row.innerHTML = `
                <td></td>
                <td class="fst-italic">${safeTrackNum || '?'}</td>
                <td colspan="4" class="fst-italic">
                    ${safeTitle}
                    <span class="badge bg-warning text-dark ms-2" style="font-size: 0.65rem; vertical-align: middle;">Missing</span>
                </td>
                <td class="text-end">
                    <div class="btn-group btn-group-sm">
                        <button class="btn btn-outline-success queue-missing-btn"
                            title="Add to download queue"
                            data-artist="${safeArtist}"
                            data-album-artist="${safeAlbumArtist}"
                            data-title="${safeTitle}"
                            data-album="${safeAlbum}"
                            data-track-number="${safeTrackNum}"
                            data-disc-number="${safeDiscNum}"
                            data-year="${mbYear}"
                            data-release-id="${releaseId}"
                            data-recording-mbid="${safeRecordingMbid}"
                            data-duration="${safeDuration}"
                            onclick="queueMissingTrack(this)">
                            <i class="bi bi-download"></i>
                        </button>
                        <button class="btn btn-outline-primary"
                            title="Match to an existing song in the library"
                            data-title="${safeTitle}"
                            data-track-number="${safeTrackNum}"
                            data-release-id="${releaseId}"
                            data-artist="${safeArtist}"
                            data-album="${safeAlbum}"
                            onclick="openAlbumMatchModal(this)">
                            <i class="bi bi-link-45deg"></i>
                        </button>
                        <button class="btn btn-outline-secondary"
                            title="Ignore – hide this track from the missing list"
                            data-missing-id=""
                            data-title="${safeTitle}"
                            data-disc-number="${safeDiscNum}"
                            onclick="ignoreMissingTrack(this)">
                            <i class="bi bi-x-lg"></i>
                        </button>
                    </div>
                </td>`;

            const missingDisc = track.mb_disc_number != null ? Number(track.mb_disc_number) : 1;
            let insertAfterRow = null;

            // Strategy 1: find the last matched track row on the SAME disc in the comparison array
            const idx = data.comparison.indexOf(track);
            for (let i = idx - 1; i >= 0; i--) {
                const prev = data.comparison[i];
                const prevDisc = prev.mb_disc_number != null ? Number(prev.mb_disc_number) : 1;
                if (prevDisc !== missingDisc) continue;  // skip rows from a different disc
                if (prev.matched && prev.library_track_id) {
                    const candidate = document.querySelector(`[data-track-id="${CSS.escape(String(prev.library_track_id))}"]`);
                    if (candidate) { insertAfterRow = candidate; break; }
                }
            }

            // Strategy 2: if no same-disc matched track was found, find the disc header row
            // (only rendered for multi-disc albums: <tr data-disc="N">)
            if (!insertAfterRow) {
                const discHeader = document.querySelector(`tr[data-disc="${CSS.escape(String(missingDisc))}"]`);
                if (discHeader) {
                    insertAfterRow = discHeader;
                }
            }

            // Strategy 3: fall back to the last matched track on any preceding disc
            if (!insertAfterRow) {
                for (let i = idx - 1; i >= 0; i--) {
                    const prev = data.comparison[i];
                    if (prev.matched && prev.library_track_id) {
                        const candidate = document.querySelector(`[data-track-id="${CSS.escape(String(prev.library_track_id))}"]`);
                        if (candidate) { insertAfterRow = candidate; break; }
                    }
                }
            }

            if (insertAfterRow) {
                // Skip any update/missing rows already placed right after insertAfterRow
                let next = insertAfterRow.nextElementSibling;
                while (next && (next.classList.contains('mb-update-row') || next.classList.contains('mb-missing-row'))) {
                    insertAfterRow = next;
                    next = next.nextElementSibling;
                }
                insertAfterRow.insertAdjacentElement('afterend', row);
            } else {
                // Last resort: prepend inside tbody
                const tbody = document.querySelector('.table-responsive table tbody');
                if (tbody) {
                    const firstTrackRow = tbody.querySelector('[data-track-id]');
                    if (firstTrackRow) {
                        firstTrackRow.insertAdjacentElement('beforebegin', row);
                    } else {
                        tbody.appendChild(row);
                    }
                }
            }
        }
    }

    // Collect unique track comparison objects from server-rendered .mb-update-row elements.
    // Used when mbComparisonData is not available (i.e. rows were pre-rendered by the server).
    function _getTracksFromDOM() {
        const seen = new Set();
        const tracks = [];
        document.querySelectorAll('.mb-update-row').forEach(row => {
            const trackId = row.dataset.trackId;
            if (trackId && !seen.has(trackId)) {
                seen.add(trackId);
                try {
                    const comp = JSON.parse(row.dataset.mbComp || '{}');
                    if (comp.library_track_id) tracks.push(comp);
                } catch (_) {}
            }
        });
        return tracks;
    }

    // Mark library tracks that are not part of the MusicBrainz release with an
    // "Extra" sub-row and a dynamic badge so the user knows they are outside the
    // official track listing.
    function _markExtraTracks(extraTracks) {
        for (const extra of extraTracks) {
            const trackId = String(extra.library_track_id);
            const trackRow = document.querySelector(`[data-track-id="${CSS.escape(trackId)}"]`);
            if (!trackRow) continue;

            // Add a dynamic "Extra" badge to any title link/span inside the row
            // (skip rows that already have a server-rendered badge).
            if (!trackRow.querySelector('.mb-extra-badge')) {
                const titleLink = trackRow.querySelector('a[href*="/track/"]') || trackRow.querySelector('.track-title-display-' + trackId);
                if (titleLink) {
                    const badge = document.createElement('span');
                    badge.className = 'badge bg-secondary ms-1 mb-extra-badge mb-extra-badge-dynamic';
                    badge.title = 'This track was not found in the MusicBrainz metadata for this album';
                    badge.textContent = 'Extra';
                    titleLink.insertAdjacentElement('afterend', badge);
                }
            }

            // Inject a sub-row with more detail, similar to mb-update-row
            const subRow = document.createElement('tr');
            subRow.className = 'mb-extra-row';
            subRow.innerHTML = `
                <td colspan="7" style="padding: 0.3rem 0.75rem; border-top: none;">
                    <div class="d-flex align-items-center gap-2 flex-wrap rounded px-2 py-1"
                         style="background: rgba(108,117,125,0.12); border: 1px solid rgba(108,117,125,0.35);">
                        <small class="text-secondary">
                            <i class="bi bi-question-circle-fill me-1"></i><strong>MusicBrainz:</strong>
                        </small>
                        <small class="text-muted">Not found in the MusicBrainz metadata for this release</small>
                    </div>
                </td>`;

            // Insert after the track row, skipping any already-injected helper rows
            let insertAfter = trackRow;
            let sib = insertAfter.nextElementSibling;
            while (sib && (sib.classList.contains('mb-update-row') || sib.classList.contains('mb-missing-row') || sib.classList.contains('mb-extra-row'))) {
                insertAfter = sib;
                sib = sib.nextElementSibling;
            }
            insertAfter.insertAdjacentElement('afterend', subRow);
        }
    }

    function _displayMBComparison(data) {
        // Remove any stale update/missing/extra rows from a previous comparison
        document.querySelectorAll('.mb-update-row').forEach(el => el.remove());
        document.querySelectorAll('.mb-missing-row').forEach(el => el.remove());
        document.querySelectorAll('.mb-extra-row').forEach(el => el.remove());
        // Remove client-injected extra badges (server-rendered ones stay until page refresh)
        document.querySelectorAll('.mb-extra-badge-dynamic').forEach(el => el.remove());

        const needsUpdate = data.comparison.filter(c => c.needs_update && c.library_track_id);
        const missingTracks = data.comparison.filter(c => !c.matched);
        const extraTracks = data.extra_tracks || [];

        if (needsUpdate.length === 0 && missingTracks.length === 0 && extraTracks.length === 0) {
            _showMBCompareBanner(
                `<i class="bi bi-check-circle-fill text-success me-2"></i>All ${escapeHtml(String(data.total_tracks))} tracks match MusicBrainz metadata — no updates needed.`,
                'success',
                true
            );
            return;
        }

        // Inject missing track rows into the table
        if (missingTracks.length > 0) {
            _injectMissingTrackRows(missingTracks, data);
        }

        // Mark extra tracks (in library but not in MB release) with a sub-row and badge
        if (extraTracks.length > 0) {
            _markExtraTracks(extraTracks);
        }

        if (needsUpdate.length === 0) {
            let onlyMsg = '';
            if (missingTracks.length > 0 && extraTracks.length > 0) {
                onlyMsg = `<strong>${escapeHtml(String(missingTracks.length))}</strong> ${missingTracks.length === 1 ? 'track is' : 'tracks are'} missing from the library and <strong>${escapeHtml(String(extraTracks.length))}</strong> ${extraTracks.length === 1 ? 'track is' : 'tracks are'} not in the MusicBrainz metadata.`;
            } else if (missingTracks.length > 0) {
                onlyMsg = `<strong>${escapeHtml(String(missingTracks.length))} of ${escapeHtml(String(data.total_tracks))} tracks</strong> are missing from the library.`;
            } else {
                onlyMsg = `<strong>${escapeHtml(String(extraTracks.length))}</strong> ${extraTracks.length === 1 ? 'track in the library is' : 'tracks in the library are'} not found in the MusicBrainz metadata for this release.`;
            }
            _showMBCompareBanner(
                `<i class="bi bi-exclamation-triangle-fill me-2"></i>${onlyMsg}`,
                'warning',
                true
            );
            return;
        }

        // Summary banner with "Update All" action
        let banner = document.getElementById('mb-compare-banner');
        if (!banner) {
            const container = _getMBCompareBannerContainer();
            if (!container) return;
            banner = document.createElement('div');
            banner.id = 'mb-compare-banner';
            const tableResponsive = document.querySelector('.table-responsive');
            container.insertBefore(banner, tableResponsive);
        }
        let bannerMsg = `<strong>${escapeHtml(String(needsUpdate.length))} of ${escapeHtml(String(data.total_tracks))} tracks</strong> have metadata that can be updated from MusicBrainz.`;
        if (missingTracks.length > 0) {
            bannerMsg += ` <strong>${escapeHtml(String(missingTracks.length))}</strong> ${missingTracks.length === 1 ? 'track is' : 'tracks are'} missing from the library.`;
        }
        if (extraTracks.length > 0) {
            bannerMsg += ` <strong>${escapeHtml(String(extraTracks.length))}</strong> ${extraTracks.length === 1 ? 'track is' : 'tracks are'} not in the MusicBrainz metadata.`;
        }
        banner.innerHTML = `
            <div class="alert alert-warning d-flex align-items-center gap-2 mb-2 py-2 flex-wrap">
                <i class="bi bi-exclamation-triangle-fill"></i>
                <div>${bannerMsg}</div>
                <button class="btn btn-warning btn-sm ms-auto" onclick="updateAllTracksFromMB()">
                    <i class="bi bi-arrow-repeat"></i> Update All
                </button>
                <button type="button" class="btn-close" onclick="clearMBComparison()" aria-label="Dismiss"></button>
            </div>`;

        // Inject an update hint row below each matching track row — one row per diff field
        for (const trackComp of data.comparison) {
            if (!trackComp.needs_update || !trackComp.library_track_id) continue;

            const trackId = String(trackComp.library_track_id);
            const trackRow = document.querySelector(`[data-track-id="${CSS.escape(trackId)}"]`);
            if (!trackRow) continue;

            // Build one row per diff field
            const fieldLabels = {
                title: () => `Title: <em>${escapeHtml(trackComp.library_title)}</em> → <strong>${escapeHtml(trackComp.mb_title)}</strong>`,
                track_number: () => `Track#: ${escapeHtml(String(trackComp.library_track_number))} → ${escapeHtml(String(trackComp.mb_track_number))}`,
                year: () => `Year: ${escapeHtml(trackComp.library_year || '—')} → ${escapeHtml(String(trackComp.mb_year))}`,
                mbid: () => `MusicBrainz ID: <em>missing</em> → <strong>added</strong>`,
                duration: () => `Length: ${escapeHtml(trackComp.library_duration || '—')} → ${escapeHtml(trackComp.mb_duration || '—')}`,
                disc_number: () => trackComp.cross_disc_match
                    ? `Move to Disc ${escapeHtml(String(trackComp.mb_disc_number))} (track ${escapeHtml(String(trackComp.mb_track_number || '?'))})`
                    : `Disc: ${escapeHtml(String(trackComp.library_disc_number || 1))} → ${escapeHtml(String(trackComp.mb_disc_number))}`,
            };

            let insertAfter = trackRow;
            // Skip any already-injected rows
            let sib = insertAfter.nextElementSibling;
            while (sib && (sib.classList.contains('mb-update-row') || sib.classList.contains('mb-missing-row'))) {
                insertAfter = sib;
                sib = sib.nextElementSibling;
            }

            for (const field of trackComp.diff_fields) {
                if (!fieldLabels[field]) continue;
                const updateRow = document.createElement('tr');
                updateRow.className = 'mb-update-row';
                updateRow.dataset.trackId = trackId;
                updateRow.dataset.mbComp = JSON.stringify(trackComp);
                updateRow.dataset.mbField = field;
                updateRow.innerHTML = `
                    <td colspan="7" style="padding: 0.3rem 0.75rem; border-top: none;">
                        <div class="d-flex align-items-center gap-2 flex-wrap rounded px-2 py-1"
                             style="background: rgba(255,193,7,0.12); border: 1px solid rgba(255,193,7,0.35);">
                            <small class="text-warning-emphasis">
                                <i class="bi bi-lightning-fill me-1"></i><strong>MusicBrainz:</strong>
                            </small>
                            <small class="text-muted">${fieldLabels[field]()}</small>
                            <button class="btn btn-warning btn-sm ms-auto py-0 px-2"
                                    style="font-size: 0.75rem; white-space: nowrap;"
                                    onclick="applyMBField(this)">
                                <i class="bi bi-check-lg"></i> Apply
                            </button>
                            <button class="btn btn-outline-secondary btn-sm py-0 px-2"
                                    style="font-size: 0.75rem; white-space: nowrap;"
                                    onclick="ignoreMBField(this)">
                                <i class="bi bi-x-lg"></i> Ignore
                            </button>
                        </div>
                    </td>`;
                insertAfter.insertAdjacentElement('afterend', updateRow);
                insertAfter = updateRow;
            }
        }
    }

    // Apply a single MusicBrainz field update for one track row
    async function applyMBField(btn) {
        const updateRow = btn.closest('.mb-update-row');
        const trackComp = JSON.parse(updateRow.dataset.mbComp || '{}');
        const trackId = String(trackComp.library_track_id || updateRow.dataset.trackId || '');
        const field = updateRow.dataset.mbField || '';

        const origHtml = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status"></span>';

        // Check if this is the last pending row for this track so we can clear the pending flag
        const siblingsForTrack = document.querySelectorAll(`.mb-update-row[data-track-id="${CSS.escape(trackId)}"]`);
        const isLastField = siblingsForTrack.length === 1;

        const payload = { track_id: trackId, sync_to_file: true };
        if (isLastField) payload.clear_mb_pending = true;

        if (field === 'title') payload.title = trackComp.mb_title;
        else if (field === 'track_number') payload.track_number = String(trackComp.mb_track_number);
        else if (field === 'year') payload.year = String(trackComp.mb_year);
        else if (field === 'mbid' && trackComp.mb_recording_id) payload.mbid = trackComp.mb_recording_id;
        else if (field === 'disc_number') payload.disc_number = trackComp.mb_disc_number;

        try {
            const resp = await fetch('/api/track/update-metadata', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const result = await resp.json();
            if (result.success) {
                updateRow.remove();
                if (field === 'title') {
                    document.querySelectorAll(`.track-title-display-${CSS.escape(trackId)}`).forEach(el => {
                        el.textContent = trackComp.mb_title;
                    });
                }
                if (!document.querySelector('.mb-update-row')) {
                    _showMBCompareBanner(
                        '<i class="bi bi-check-circle-fill text-success me-2"></i>All MusicBrainz suggestions have been applied.',
                        'success', true
                    );
                }
            } else {
                btn.disabled = false;
                btn.innerHTML = origHtml;
                alert('Failed to apply MusicBrainz update: ' + (result.error || 'Unknown error'));
            }
        } catch (err) {
            btn.disabled = false;
            btn.innerHTML = origHtml;
            alert('Network error: ' + err.message);
        }
    }

    // Permanently ignore a single MusicBrainz field suggestion for one track
    async function ignoreMBField(btn) {
        const updateRow = btn.closest('.mb-update-row');
        const trackComp = JSON.parse(updateRow.dataset.mbComp || '{}');
        const trackId = String(trackComp.library_track_id || updateRow.dataset.trackId || '');
        const field = updateRow.dataset.mbField || '';

        const origHtml = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status"></span>';

        const siblingsForTrack = document.querySelectorAll(`.mb-update-row[data-track-id="${CSS.escape(trackId)}"]`);
        const isLastField = siblingsForTrack.length === 1;

        try {
            const resp = await fetch('/api/track/ignore-mb-field', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ track_id: trackId, field, clear_mb_pending: isLastField })
            });
            const result = await resp.json();
            if (result.success) {
                updateRow.remove();
                if (!document.querySelector('.mb-update-row')) {
                    _showMBCompareBanner(
                        '<i class="bi bi-check-circle-fill text-success me-2"></i>All MusicBrainz suggestions have been handled.',
                        'success', true
                    );
                }
            } else {
                btn.disabled = false;
                btn.innerHTML = origHtml;
                alert('Failed to ignore MusicBrainz field: ' + (result.error || 'Unknown error'));
            }
        } catch (err) {
            btn.disabled = false;
            btn.innerHTML = origHtml;
            alert('Network error: ' + err.message);
        }
    }

    async function updateSingleTrackFromMB(btn) {
        const updateRow = btn.closest('.mb-update-row');
        const trackComp = JSON.parse(updateRow.dataset.mbComp || '{}');
        const trackId = String(trackComp.library_track_id || updateRow.dataset.trackId || '');

        const origHtml = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status"></span>';

        const payload = { track_id: trackId, sync_to_file: true, clear_mb_pending: true };
        if (trackComp.diff_fields && trackComp.diff_fields.includes('title'))
            payload.title = trackComp.mb_title;
        if (trackComp.diff_fields && trackComp.diff_fields.includes('track_number'))
            payload.track_number = String(trackComp.mb_track_number);
        if (trackComp.diff_fields && trackComp.diff_fields.includes('year'))
            payload.year = String(trackComp.mb_year);
        if (trackComp.mb_recording_id)
            payload.mbid = trackComp.mb_recording_id;
        if (trackComp.diff_fields && trackComp.diff_fields.includes('disc_number'))
            payload.disc_number = trackComp.mb_disc_number;

        try {
            const resp = await fetch('/api/track/update-metadata', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const result = await resp.json();
            if (result.success) {
                // Remove the update row
                updateRow.remove();
                // Update visible title on the page if it changed
                if (trackComp.diff_fields && trackComp.diff_fields.includes('title')) {
                    document.querySelectorAll(`.track-title-display-${CSS.escape(trackId)}`).forEach(el => {
                        el.textContent = trackComp.mb_title;
                    });
                }
                // Check if all update rows have been resolved
                if (!document.querySelector('.mb-update-row')) {
                    _showMBCompareBanner(
                        '<i class="bi bi-check-circle-fill text-success me-2"></i>All tracks updated successfully.',
                        'success',
                        true
                    );
                }
            } else {
                btn.disabled = false;
                btn.innerHTML = origHtml;
                alert('Error updating track: ' + (result.error || 'Unknown error'));
            }
        } catch (err) {
            btn.disabled = false;
            btn.innerHTML = origHtml;
            alert('Network error: ' + err.message);
        }
    }

    async function updateAllTracksFromMB() {
        const tracksToUpdate = mbComparisonData
            ? mbComparisonData.comparison.filter(c => c.needs_update && c.library_track_id)
            : _getTracksFromDOM();
        if (tracksToUpdate.length === 0) return;
        if (!confirm(`Update metadata for ${tracksToUpdate.length} track(s) from MusicBrainz?\n\nThis will update the database and MP3 file tags.`)) return;

        let banner = document.getElementById('mb-compare-banner');
        if (banner) {
            banner.innerHTML = `<div class="alert alert-info d-flex align-items-center gap-2 mb-2 py-2">
                <span class="spinner-border spinner-border-sm me-2" role="status"></span>
                Updating tracks… <span id="mb-update-progress">0</span> / ${escapeHtml(String(tracksToUpdate.length))}
            </div>`;
        }

        let updated = 0, failed = 0;
        for (const trackComp of tracksToUpdate) {
            const trackId = String(trackComp.library_track_id);
            const payload = { track_id: trackId, sync_to_file: true, clear_mb_pending: true };
            if (trackComp.diff_fields.includes('title')) payload.title = trackComp.mb_title;
            if (trackComp.diff_fields.includes('track_number')) payload.track_number = String(trackComp.mb_track_number);
            if (trackComp.diff_fields.includes('year')) payload.year = String(trackComp.mb_year);
            if (trackComp.mb_recording_id) payload.mbid = trackComp.mb_recording_id;
            if (trackComp.diff_fields.includes('disc_number')) payload.disc_number = trackComp.mb_disc_number;

            try {
                const resp = await fetch('/api/track/update-metadata', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const result = await resp.json();
                if (result.success) {
                    updated++;
                    document.querySelectorAll(`.mb-update-row[data-track-id="${CSS.escape(trackId)}"]`).forEach(el => el.remove());
                    if (trackComp.diff_fields.includes('title')) {
                        document.querySelectorAll(`.track-title-display-${CSS.escape(trackId)}`).forEach(el => {
                            el.textContent = trackComp.mb_title;
                        });
                    }
                } else {
                    failed++;
                }
            } catch (_e) {
                failed++;
            }

            const progressEl = document.getElementById('mb-update-progress');
            if (progressEl) progressEl.textContent = updated + failed;
        }

        const type = failed > 0 ? 'warning' : 'success';
        const icon = failed > 0 ? 'exclamation-triangle-fill' : 'check-circle-fill';
        const msg = `Updated ${updated} track(s)` + (failed > 0 ? `, ${failed} failed.` : ' successfully.');
        _showMBCompareBanner(`<i class="bi bi-${icon} me-2"></i>${escapeHtml(msg)}`, type, true);

        if (updated > 0) {
            setTimeout(() => location.reload(), 2000);
        }
    }

    function clearMBComparison() {
        document.querySelectorAll('.mb-update-row').forEach(el => el.remove());
        document.querySelectorAll('.mb-missing-row').forEach(el => el.remove());
        document.querySelectorAll('.mb-extra-row').forEach(el => el.remove());
        document.querySelectorAll('.mb-extra-badge-dynamic').forEach(el => el.remove());
        const banner = document.getElementById('mb-compare-banner');
        if (banner) banner.remove();
        mbComparisonData = null;
    }


    function populateMajorityArtist() {
        const album = _pageData.albumName;
        const artist = _pageData.artistName;
        const trackArtistInput = document.getElementById('track_artist');
        
        // Show loading state
        const originalText = trackArtistInput.value;
        trackArtistInput.disabled = true;
        trackArtistInput.placeholder = 'Loading...';
        
        fetch('/api/album/majority-artist', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ album: album, artist: artist })
        })
        .then(response => {
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            return response.json();
        })
        .then(data => {
            if (data.majority_artist) {
                trackArtistInput.value = data.majority_artist;
                trackArtistInput.disabled = false;
                trackArtistInput.placeholder = 'Artist name to apply to all track tags';
            } else {
                throw new Error('No artist data returned');
            }
        })
        .catch(error => {
            alert('❌ Error loading majority artist: ' + error.message);
            trackArtistInput.disabled = false;
            trackArtistInput.placeholder = 'Artist name to apply to all track tags';
            trackArtistInput.value = originalText;
        });
    }
    
    // Album genre management (currentAlbumGenres declared at top of script)
    
    // Initialize album genres
    document.addEventListener('DOMContentLoaded', function() {
        const albumGenresInput = document.getElementById('album_genres');
        if (albumGenresInput && albumGenresInput.value) {
            // Split on either backslash (Navidrome) or comma (user-entered), then trim and filter
            const genres = albumGenresInput.value.split(/[\\,]+/).map(g => g.trim()).filter(g => g && g.length > 0);
            currentAlbumGenres = new Set(genres);
            // Redisplay genres in proper format
            updateAlbumGenresDisplay();
        }
        
        // Add Enter key support
        const genreInput = document.getElementById('newAlbumGenreInput');
        if (genreInput) {
            genreInput.addEventListener('keypress', function(e) {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    addAlbumGenre();
                }
            });
        }

        // Reorder key sections to Overview -> Similar Artists -> Edit Album.
        // The similar-artists card renders AFTER the edit/overview row; move it
        // INTO the row between the two columns (wrapped in a full-width column)
        // so the page reads Overview -> Similar Artists -> Edit Album.  The old
        // approach moved the card's ORIGINAL wrapper before the row, which
        // crashed with a HierarchyRequestError — the wrapper is an ancestor of
        // the row, and insertBefore cannot move an ancestor before its own
        // descendant.
        const overview = document.getElementById('album-overview-section');
        const similar = document.getElementById('album-similar-section');
        const edit = document.getElementById('album-information-section');
        if (overview && similar && edit) {
            const row = edit.closest('.row');
            const editCol = edit.closest('.col-12');
            if (row && !row.contains(similar)) {
                const similarWrap = document.createElement('div');
                similarWrap.className = 'col-12 order-2';
                row.insertBefore(similarWrap, editCol || row.lastElementChild);
                similarWrap.appendChild(similar);
                // Keep the edit column after the new section regardless of the
                // original col order classes (overview stays order-1).
                if (editCol) {
                    editCol.classList.add('order-3');
                }
                // The similar card now lives inside the album edit <form> —
                // any button without an explicit type would default to
                // "submit" and POST the form on click.
                similarWrap.querySelectorAll('button').forEach(function (btn) {
                    if (!btn.type) btn.type = 'button';
                });
            }
        }

        // Show a visible loading state when saving album edits.
        const albumForm = document.getElementById('albumMetadataForm');
        if (albumForm) {
            albumForm.addEventListener('submit', function() {
                const saveBtn = albumForm.querySelector('button[type="submit"]');
                if (saveBtn) {
                    saveBtn.disabled = true;
                    saveBtn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status" style="width:0.8rem;height:0.8rem;"></span> Saving...';
                }
            });
        }
        
        // Load track-level genre recommendations
        loadTrackRecommendations();

        // Show "Update All" banner if the server pre-rendered any MusicBrainz update rows
        const existingUpdateRows = document.querySelectorAll('.mb-update-row');
        if (existingUpdateRows.length > 0) {
            const trackIds = new Set([...existingUpdateRows].map(r => r.dataset.trackId).filter(Boolean));
            const count = trackIds.size;
            let banner = document.getElementById('mb-compare-banner');
            if (!banner) {
                const container = _getMBCompareBannerContainer();
                if (container) {
                    banner = document.createElement('div');
                    banner.id = 'mb-compare-banner';
                    const tableResponsive = document.querySelector('.table-responsive');
                    container.insertBefore(banner, tableResponsive);
                }
            }
            if (banner) {
                banner.innerHTML = `
                    <div class="alert alert-warning d-flex align-items-center gap-2 mb-2 py-2 flex-wrap">
                        <i class="bi bi-exclamation-triangle-fill"></i>
                        <div><strong>${escapeHtml(String(count))}</strong> track${count !== 1 ? 's have' : ' has'} metadata that can be updated from MusicBrainz.</div>
                        <button class="btn btn-warning btn-sm ms-auto" onclick="updateAllTracksFromMB()">
                            <i class="bi bi-arrow-repeat"></i> Update All
                        </button>
                        <button type="button" class="btn-close" onclick="clearMBComparison()" aria-label="Dismiss"></button>
                    </div>`;
            }
        }
    });
    
    // Toggle apply button when album source-tag checkboxes change
    document.addEventListener('change', function(e) {
        if (e.target && e.target.classList.contains('album-source-tag-check')) {
            const pane = e.target.closest('.tab-pane');
            if (!pane) return;
            const applyBtn = pane.querySelector('.album-apply-source-tags-btn');
            if (!applyBtn) return;
            const anyChecked = pane.querySelectorAll('.album-source-tag-check:checked').length > 0;
            applyBtn.style.display = anyChecked ? '' : 'none';
        }
    });

    function applySelectedAlbumSourceTags() {
        const checked = document.querySelectorAll('.album-source-tag-check:checked');
        const selected = Array.from(checked).map(cb => cb.value).filter(Boolean);
        if (selected.length === 0) { alert('No tags selected'); return; }
        selected.forEach(genre => {
            const normalized = String(genre || '').trim();
            if (normalized) currentAlbumGenres.add(normalized);
        });
        updateAlbumGenresDisplay();
        checked.forEach(cb => { cb.checked = false; });
        document.querySelectorAll('.album-apply-source-tags-btn').forEach(b => { b.style.display = 'none'; });
        alert('Selected tags added. Press Save in Edit Album to apply to all tracks.');
    }

    function addAlbumGenre() {
        const input = document.getElementById('newAlbumGenreInput');
        const genre = input.value.trim();
        
        if (!genre) return;
        
        if (currentAlbumGenres.has(genre)) {
            alert('Genre already added');
            input.value = '';
            return;
        }
        
        currentAlbumGenres.add(genre);
        updateAlbumGenresDisplay();
        input.value = '';
        input.focus();
    }
    
    function stageRemoveAlbumGenre(genre) {
        if (!genre) return;
        currentAlbumGenres.delete(String(genre));
        updateAlbumGenresDisplay();
    }

    function updateAlbumGenresDisplay() {
        const container = document.getElementById('albumGenresContainer');
        const hiddenInput = document.getElementById('album_genres');
        
        if (currentAlbumGenres.size === 0) {
            container.innerHTML = '<span class="text-muted small">No genres set</span>';
            hiddenInput.value = '';
        } else {
            let html = '';
            Array.from(currentAlbumGenres).sort().forEach(genre => {
                html += `
                    <span class="badge bg-primary me-1 mb-1" style="font-size: 0.9rem;">
                        ${escapeHtml(genre)}
                        <button type="button" class="btn-close btn-close-white ms-1" style="font-size: 0.6rem;" onclick='stageRemoveAlbumGenre("${escapeHtml(genre)}")' aria-label="Remove"></button>
                    </span>
                `;
            });
            
            container.innerHTML = html;
            hiddenInput.value = Array.from(currentAlbumGenres).join(', ');
        }
        // Chip edits are programmatic — mark the sticky save bar dirty explicitly.
        if (window.markFormDirty) window.markFormDirty('albumMetadataForm');
    }
    
    function escapeJsString(str) {
        if (!str) return '';
        return str.replace(/\\/g, '\\\\')
                  .replace(/'/g, "\\'")
                  .replace(/"/g, '\\"')
                  .replace(/\n/g, '\\n')
                  .replace(/\r/g, '\\r');
    }

    function openEditAlbumIdsModal() {
        const artistName = _pageData.artistName;
        const albumName = _pageData.albumName;
        const musicbrainzId = (document.getElementById('album_mbid') || {}).value || '';
        const releaseGroupId = (document.getElementById('album_release_group_mbid') || {}).value || '';
        const discogsId = (document.getElementById('discogs_album_id') || {}).value || '';
        
        const modalHtml = `
            <div class="modal fade" id="editAlbumIdsModal" tabindex="-1">
                <div class="modal-dialog">
                    <div class="modal-content">
                        <div class="modal-header">
                            <h5 class="modal-title"><i class="bi bi-pencil"></i> Edit Album/Release IDs</h5>
                            <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                        </div>
                        <div class="modal-body">
                            <div class="mb-3">
                                <label for="editMusicbrainzReleaseId" class="form-label">MusicBrainz Release ID</label>
                                <input type="text" class="form-control" id="editMusicbrainzReleaseId" value="${escapeHtml(musicbrainzId)}" placeholder="e.g., a74b1b7f-71a5-4011-9441-d0b5e4122711">
                                <div class="form-text">
                                    <a href="https://musicbrainz.org/search?query=${encodeURIComponent(artistName + ' ' + albumName)}&type=release" target="_blank">Search MusicBrainz</a> to find the release ID
                                </div>
                            </div>
                            <div class="mb-3">
                                <label for="editMusicbrainzRgId" class="form-label">MusicBrainz Album (Release Group) ID</label>
                                <input type="text" class="form-control" id="editMusicbrainzRgId" value="${escapeHtml(releaseGroupId)}" placeholder="e.g., a74b1b7f-71a5-4011-9441-d0b5e4122711">
                                <div class="form-text">
                                    <a href="https://musicbrainz.org/search?query=${encodeURIComponent(artistName + ' ' + albumName)}&type=release_group" target="_blank">Search MusicBrainz</a> to find the album ID
                                </div>
                            </div>
                            <div class="mb-3">
                                <label for="editDiscogsReleaseId" class="form-label">Discogs Release ID</label>
                                <input type="text" class="form-control" id="editDiscogsReleaseId" value="${escapeHtml(discogsId)}" placeholder="e.g., 123456">
                                <div class="form-text">
                                    <a href="https://www.discogs.com/search/?q=${encodeURIComponent(artistName + ' ' + albumName)}&type=release" target="_blank">Search Discogs</a> to find the release ID
                                </div>
                            </div>
                        </div>
                        <div class="modal-footer">
                            <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
                            <button type="button" class="btn btn-primary" onclick="saveAlbumIds()">
                                <i class="bi bi-save"></i> Save Changes
                            </button>
                        </div>
                    </div>
                </div>
            </div>
        `;
        
        // Remove existing modal if any
        const existingModal = document.getElementById('editAlbumIdsModal');
        if (existingModal) existingModal.remove();
        
        document.body.insertAdjacentHTML('beforeend', modalHtml);
        const modal = new bootstrap.Modal(document.getElementById('editAlbumIdsModal'));
        modal.show();
    }

    function saveAlbumIds() {
        const artistName = _pageData.artistName;
        const albumName = _pageData.albumName;
        const musicbrainzId = document.getElementById('editMusicbrainzReleaseId').value.trim();
        const releaseGroupId = document.getElementById('editMusicbrainzRgId').value.trim();
        const discogsId = document.getElementById('editDiscogsReleaseId').value.trim();
        
        fetch('/api/album/update-ids', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                artist: artistName,
                album: albumName,
                musicbrainz_release_id: musicbrainzId,
                musicbrainz_release_group_id: releaseGroupId,
                discogs_release_id: discogsId
            })
        })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                // Update the readonly fields when present
                const albumMbidEl = document.getElementById('album_mbid');
                const albumRgMbidEl = document.getElementById('album_release_group_mbid');
                if (albumMbidEl) albumMbidEl.value = musicbrainzId;
                if (albumRgMbidEl) albumRgMbidEl.value = releaseGroupId;
                
                bootstrap.Modal.getInstance(document.getElementById('editAlbumIdsModal'))?.hide();
                alert('✅ Album IDs updated successfully!');
                
                // Reload page to update links
                setTimeout(() => location.reload(), 1000);
            } else {
                alert('❌ Error: ' + (data.error || 'Failed to update IDs'));
            }
        })
        .catch(err => alert('❌ Network error: ' + err.message));
    }
    
    // ============================================================================
    // BULK TRACK OPERATIONS
    // ============================================================================
    
    function updateBulkActionsUI() {
        const checkboxes = document.querySelectorAll('.track-checkbox:checked');
        const toolbar = document.getElementById('bulkActionsToolbar');
        const countSpan = document.getElementById('selectedCount');

        const selected = checkboxes.length > 0;
        if (toolbar) {
            toolbar.classList.toggle('d-none', !selected);
            toolbar.style.display = selected ? 'flex' : 'none';
        }
        // Hide the Link/Align actions while a selection is active so the bulk
        // bar owns the header's right side (contextual swap).
        const linkAlign = document.getElementById('albumHeaderLinkAlign');
        if (linkAlign) linkAlign.classList.toggle('d-none', selected);
        // Give the last track clearance above the floating bar when active.
        document.body.classList.toggle('bulk-actions-visible', selected);
        if (countSpan) countSpan.textContent = checkboxes.length;
    }

    /** Filter the tracklist to rows missing a MusicBrainz Recording ID. */
    function toggleMissingMbidFilter() {
        const tbody = document.getElementById('albumTracksTbody');
        const badge = document.getElementById('albumMissingMbidBadge');
        if (!tbody || !badge) return;
        const active = tbody.classList.toggle('filter-missing-mbid');
        badge.classList.toggle('active', active);
        badge.setAttribute('aria-pressed', String(active));
    }
    
    function toggleSelectAll(checkbox) {
        const allCheckboxes = document.querySelectorAll('.track-checkbox');
        allCheckboxes.forEach(cb => {
            cb.checked = checkbox.checked;
        });
        updateBulkActionsUI();
    }
    
    function selectAllTracks() {
        document.querySelectorAll('.track-checkbox').forEach(cb => cb.checked = true);
        document.getElementById('selectAllCheckbox').checked = true;
        updateBulkActionsUI();
    }
    
    function clearAllTracks() {
        document.querySelectorAll('.track-checkbox').forEach(cb => cb.checked = false);
        document.getElementById('selectAllCheckbox').checked = false;
        updateBulkActionsUI();
    }
    
    function getSelectedTracks() {
        const checkboxes = document.querySelectorAll('.track-checkbox:checked');
        return Array.from(checkboxes).map(cb => cb.dataset.trackId);
    }
    
    function openBulkGenreTaggerModal() {
        const selectedCount = document.querySelectorAll('.track-checkbox:checked').length;
        document.getElementById('selectedCountModal').textContent = selectedCount;
        document.getElementById('bulkGenreInput').value = '';
        const modal = new bootstrap.Modal(document.getElementById('bulkGenreTaggerModal'));
        modal.show();
    }
    
    function addGenreTag(tag) {
        const input = document.getElementById('bulkGenreInput');
        const current = input.value.trim();
        if (current) {
            input.value = current + ', ' + tag;
        } else {
            input.value = tag;
        }
    }
    
    function applyBulkGenreTags() {
        const genresInput = document.getElementById('bulkGenreInput').value.trim();
        if (!genresInput) {
            alert('Please enter at least one genre tag');
            return;
        }
        
        const trackIds = getSelectedTracks();
        if (trackIds.length === 0) {
            alert('Please select at least one track');
            return;
        }
        
        const genres = genresInput.split(',').map(g => g.trim());
        const btn = event.target.closest('button');
        const originalText = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Adding...';
        
        fetch('/api/album/bulk-tag', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                track_ids: trackIds,
                genres: genres,
                artist: _pageData.artistName,
                album: _pageData.albumName
            })
        })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                alert(`✅ Added genre tags to ${data.updated_count} track(s)`);
                bootstrap.Modal.getInstance(document.getElementById('bulkGenreTaggerModal')).hide();
                clearAllTracks();
                // Optionally reload page to show updated genres
                setTimeout(() => location.reload(), 1000);
            } else {
                alert('❌ Error: ' + (data.error || 'Failed to add tags'));
            }
        })
        .catch(err => {
            alert('❌ Network error: ' + err.message);
        })
        .finally(() => {
            btn.disabled = false;
            btn.innerHTML = originalText;
        });
    }
    
    // Track ids staged for deletion by the confirmation modal (used by both
    // the single-track ⋮ menu and the bulk "Delete Selected" toolbar button).
    let _pendingDeleteIds = [];

    function openDeleteTrackModal(trackIds) {
        trackIds = (trackIds || []).filter(Boolean);
        if (!trackIds.length) {
            alert('Please select at least one track');
            return;
        }
        _pendingDeleteIds = trackIds;
        document.getElementById('deleteTrackCount').textContent = trackIds.length;

        // List the actual file names being targeted (up to 5, then "+N more")
        // so the user knows exactly what the action affects.
        const files = trackIds
            .map(id => (window._pageData && _pageData.trackFiles && _pageData.trackFiles[id]) || '')
            .filter(Boolean);
        const listEl = document.getElementById('deleteTrackFiles');
        const visible = files.slice(0, 5);
        const rest = files.length - visible.length;
        listEl.innerHTML = files.length
            ? visible.map(f => `<li><i class="bi bi-file-earmark-music me-1"></i>${escapeHtml(f)}</li>`).join('')
              + (rest > 0 ? `<li class="text-muted fst-italic">…and ${rest} more</li>` : '')
            : '<li class="fst-italic">(no local audio files found for the selected tracks)</li>';

        const modal = new bootstrap.Modal(document.getElementById('bulkDeleteModal'));
        modal.show();
    }

    function confirmBulkDeleteTracks() {
        openDeleteTrackModal(getSelectedTracks());
    }
    
    function deleteDatabaseOnly() {
        const trackIds = _pendingDeleteIds.length ? _pendingDeleteIds : getSelectedTracks();
        const modal = bootstrap.Modal.getInstance(document.getElementById('bulkDeleteModal'));
        modal.hide();
        
        performBulkDelete(trackIds, false, 'database');
    }
    
    function deleteWithFiles() {
        const trackIds = _pendingDeleteIds.length ? _pendingDeleteIds : getSelectedTracks();
        const modal = bootstrap.Modal.getInstance(document.getElementById('bulkDeleteModal'));
        modal.hide();
        
        performBulkDelete(trackIds, true, 'files and database');
    }
    
    function performBulkDelete(trackIds, deleteFiles, deleteType) {
        fetch('/api/album/bulk-delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                track_ids: trackIds,
                artist: _pageData.artistName,
                album: _pageData.albumName,
                delete_files: deleteFiles
            })
        })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                // Remove the affected rows immediately — no page reload.
                removeTrackRows(trackIds);
                clearAllTracks();
                mergeMissingTracks();
                if (typeof showToast === 'function') {
                    showToast('Success', `${data.deleted_count} track(s) deleted from ${deleteType}`, 'success');
                }
            } else {
                alert('❌ Error: ' + (data.error || 'Failed to delete tracks'));
            }
        })
        .catch(err => {
            alert('❌ Network error: ' + err.message);
        })
        .finally(() => {
            _pendingDeleteIds = [];
        });
    }

    function removeTrackRows(trackIds) {
        const ids = (trackIds || []).map(String);
        ids.forEach(trackId => {
            document.querySelectorAll(`tr[data-track-id="${CSS.escape(trackId)}"]`).forEach(r => r.remove());
            document.querySelectorAll('.mb-update-row, .mb-extra-row').forEach(r => {
                if (r.dataset.trackId === trackId) r.remove();
            });
        });
    }

    function confirmBulkRenameTracks() {
        const trackIds = getSelectedTracks();
        if (trackIds.length === 0) {
            alert('Please select at least one track');
            return;
        }
        if (!confirm(`Rename ${trackIds.length} track file(s) using the format configured in Settings?\n\nFiles will be moved to match the configured naming pattern.`)) {
            return;
        }
        performBulkRename(trackIds);
    }

    async function performBulkRename(trackIds) {
        let renamed = 0, skipped = 0, errors = [];
        // Every track is rendered in BOTH a desktop row and a mobile row, so
        // "select all" checks each track twice. Dedupe so a track is never
        // renamed twice — double-renaming re-renders the same destination and
        // produces " (1)"-suffixed files that look like deletions.
        const uniqueIds = [...new Set(trackIds)];
        for (const trackId of uniqueIds) {
            try {
                const r = await fetch(`/api/track/${trackId}/rename-file`, { method: 'POST' });
                const data = await r.json();
                if (data.success) {
                    if (data.unchanged) skipped++;
                    else renamed++;
                } else {
                    errors.push(`Track ${trackId}: ${data.error || data.message || 'Failed'}`);
                }
            } catch (err) {
                errors.push(`Track ${trackId}: ${err.message}`);
            }
        }
        let msg = `✅ Renamed: ${renamed}`;
        if (skipped) msg += `, Already correct: ${skipped}`;
        if (errors.length) msg += `\n❌ Errors (${errors.length}):\n` + errors.slice(0, 5).join('\n');
        alert(msg);
        if (renamed > 0) location.reload();
    }

    // ============================================
    // GENRE MANAGEMENT IN COVERAGE TABLE
    // ============================================

    // Fallback functions if genre-utils.js fails to load
    if (typeof toggleGenreCheckbox === 'undefined') {
        window.toggleGenreCheckbox = function(containerId, buttonId) {
            const button = document.getElementById(buttonId);
            const container = document.getElementById(containerId);
            if (!container || !button) return;
            const checkboxes = container.querySelectorAll('input[type="checkbox"]');
            const checkedBoxes = Array.from(checkboxes).filter(cb => cb.checked);
            if (checkedBoxes.length > 0) {
                button.style.display = 'inline-block';
                button.textContent = `Remove ${checkedBoxes.length} Selected Genre${checkedBoxes.length > 1 ? 's' : ''}`;
            } else {
                button.style.display = 'none';
            }
        };
    }

    if (typeof getSelectedGenres === 'undefined') {
        window.getSelectedGenres = function(containerId) {
            const container = document.getElementById(containerId);
            if (!container) return [];
            return Array.from(container.querySelectorAll('input[type="checkbox"]:checked')).map(cb => cb.value);
        };
    }

    if (typeof handleGenreRemoval === 'undefined') {
        window.handleGenreRemoval = function(artistName, albumName, genres, contextType) {
            // Fallback handler for genre removal
            alert(`Would remove genres: ${genres.join(', ')}`);
        };
    }

    function removeTrackGenre(trackId, genre, element) {
        if (!confirm(`Remove "${genre}" from this track?`)) return;

        // Fetch current track data to get current genres
        fetch(`/api/track/${trackId}`)
        .then(r => r.json())
        .then(trackData => {
            let currentGenres = [];
            
            // Parse current genres - handle semicolon, comma, or backslash separators
            if (trackData.genre) {
                // Split on semicolon, comma, or backslash
                currentGenres = trackData.genre
                    .split(/[;,\\\/]/g)
                    .map(g => g.trim())
                    .filter(g => g && g !== genre);  // Remove the genre being deleted
            }
            
            // Update the database and sync to file
            return fetch(`/api/tags/track/${trackId}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    tags: {
                        genre: currentGenres.join(';')
                    },
                    sync_to_file: true
                })
            });
        })
        .then(r => r.json())
        .then(data => {
            if (data.success || data.message) {
                // Remove the badge element
                element.style.opacity = '0.5';
                setTimeout(() => {
                    element.remove();
                    // Check if no genres left
                    const container = document.getElementById(`genres-${trackId}`);
                    if (container && container.querySelectorAll('.genre-badge').length === 0) {
                        container.innerHTML = '<span class="text-muted small">No album genres</span><button class="btn btn-sm btn-link p-0" style="font-size: 0.75rem; text-decoration: none;" onclick="openAddGenreModal(\'' + trackId + '\', \'' + (currentEditTrackTitle || 'Track') + '\')"><i class="bi bi-plus-circle"></i> Add</button>';
                    }
                }, 300);
                alert(`✅ Genre removed${data.file_synced ? ' and MP3 updated' : ''}`);
            } else {
                alert('❌ Error: ' + (data.error || 'Failed to remove genre'));
            }
        })
        .catch(err => {
            alert('❌ Network error: ' + err.message);
        });
    }

    // Set up event listeners for genre badges
    document.addEventListener('DOMContentLoaded', function() {
        // Delegate event handler for genre removal
        document.addEventListener('click', function(e) {
            if (e.target.closest('.genre-badge')) {
                const badge = e.target.closest('.genre-badge');
                const trackId = badge.getAttribute('data-track-id');
                const genre = badge.getAttribute('data-genre');
                if (trackId && genre) {
                    removeTrackGenre(trackId, genre, badge);
                }
            }
        });
    });

    function openAddGenreModal(trackId, trackTitle) {
        currentEditTrackId = trackId;
        currentEditTrackTitle = trackTitle;
        
        // Update modal title
        document.getElementById('modalTrackTitle').textContent = trackTitle;
        
        // Get current genres for this track from the badges
        const genesContainer = document.getElementById(`genres-${trackId}`);
        if (!genesContainer) {
            alert('Error: Could not find track genres container');
            return;
        }
        
        const currentGenres = Array.from(genesContainer.querySelectorAll('.genre-badge')).map(el => {
            let text = el.textContent.trim();
            // Remove the X icon if present
            return text.replace(/\s*$/, '').replace(/×|\⨯/, '').trim();
        });
        
        // Build genre options from album genres
        const albumGenresArray = Array.from(currentAlbumGenres);
        let genreHtml = '';
        
        albumGenresArray.forEach(genre => {
            genre = genre.trim();
            const isChecked = currentGenres.includes(genre);
            const isDisabled = isChecked;
            genreHtml += `
                <div class="form-check">
                    <input class="form-check-input genre-checkbox" type="checkbox" id="genre-${genre}" value="${genre}" ${isChecked ? 'checked disabled' : ''}>
                    <label class="form-check-label" for="genre-${genre}">
                        ${genre} ${isChecked ? '<small class="text-muted">(already added)</small>' : ''}
                    </label>
                </div>
            `;
        });
        
        document.getElementById('genreOptionsContainer').innerHTML = genreHtml;
        
        // Get or create modal instance (reuse if already exists)
        const modalElement = document.getElementById('addGenreModal');
        if (!addGenreModalInstance) {
            addGenreModalInstance = new bootstrap.Modal(modalElement, {
                backdrop: true,
                keyboard: true
            });
        }
        
        // Show the modal
        addGenreModalInstance.show();
    }

    function addSelectedGenresToTrack() {
        if (!currentEditTrackId) return;
        
        const selected = Array.from(document.querySelectorAll('.genre-checkbox:checked:not(:disabled)'))
            .map(el => el.value);
        
        if (selected.length === 0) {
            alert('Please select at least one genre to add');
            return;
        }

        // Fetch current track to get existing genres
        fetch(`/api/track/${currentEditTrackId}`)
        .then(r => r.json())
        .then(trackData => {
            let allGenres = [];
            
            // Parse current genres - handle semicolon, comma, or backslash separators
            if (trackData.genre) {
                allGenres = trackData.genre
                    .split(/[;,\\\/]/g)
                    .map(g => g.trim())
                    .filter(g => g);
            }
            
            // Add new genres (avoid duplicates)
            selected.forEach(genre => {
                if (!allGenres.includes(genre)) {
                    allGenres.push(genre);
                }
            });
            
            // Update the track and sync to file
            return fetch(`/api/tags/track/${currentEditTrackId}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    tags: {
                        genre: allGenres.join(';')
                    },
                    sync_to_file: true
                })
            });
        })
        .then(r => r.json())
        .then(data => {
            if (data.success || data.message) {
                alert(`✅ Added ${selected.length} genre(s) to the track${data.file_synced ? ' and updated MP3 file' : ''}`);
                
                // Properly close the modal
                if (addGenreModalInstance) {
                    addGenreModalInstance.hide();
                }
                
                // Reload to show updated genres after a short delay
                setTimeout(() => {
                    location.reload();
                }, 500);
            } else {
                alert('❌ Error: ' + (data.error || 'Failed to add genres'));
            }
        })
        .catch(err => {
            alert('❌ Network error: ' + err.message);
        });
    }

    function addAlbumToMissingReleases(artist, album, year) {
        /**
         * Add the current album to the missing releases tracking list
         */
        if (!artist || !album) {
            alert('❌ Artist and album information are required');
            return;
        }

        fetch('/api/album/add-to-missing-releases', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                artist: artist,
                album: album,
                year: year || ''
            })
        })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                alert('✅ ' + data.message);
            } else {
                alert('❌ Error: ' + (data.error || 'Failed to add album to missing releases'));
            }
        })
        .catch(err => {
            alert('❌ Network error: ' + err.message);
        });
    }

    // Note: Scan monitoring functions are now provided by shared genre-utils.js

    // Track inline editing functions
    function editTrackTitle(trackId, currentTitle) {
        const modal = new bootstrap.Modal(document.getElementById('simpleEditTrackModal'));
        document.getElementById('editTrackId').value = trackId;
        document.getElementById('editTrackCurrentField').value = 'title';
        document.getElementById('editTrackLabel').textContent = 'Track Title';
        document.getElementById('editTrackValue').value = currentTitle;
        document.getElementById('editTrackValue').focus();
        modal.show();
    }

    function editTrackArtist(trackId, currentArtist) {
        const modal = new bootstrap.Modal(document.getElementById('simpleEditTrackModal'));
        document.getElementById('editTrackId').value = trackId;
        document.getElementById('editTrackCurrentField').value = 'artist';
        document.getElementById('editTrackLabel').textContent = 'Track Artist';
        const displayValue = currentArtist && currentArtist !== '—' ? currentArtist : '';
        document.getElementById('editTrackValue').value = displayValue;
        document.getElementById('editTrackValue').focus();
        modal.show();
    }

    function saveEditedTrack() {
        const trackId = document.getElementById('editTrackId').value;
        const field = document.getElementById('editTrackCurrentField').value;
        const value = document.getElementById('editTrackValue').value.trim();
        
        if (!trackId || !field) return;
        
        const payload = {
            track_id: trackId,
            [field]: value,
            sync_to_file: true
        };
        
        fetch('/api/track/update-metadata', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
        })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                if (field === 'title') {
                    document.querySelectorAll(`.track-title-display-${trackId}`).forEach(el => {
                        el.textContent = value;
                    });
                } else if (field === 'artist') {
                    document.querySelectorAll(`.track-artist-display-${trackId}`).forEach(el => {
                        el.textContent = value || '—';
                    });
                }
                bootstrap.Modal.getInstance(document.getElementById('simpleEditTrackModal')).hide();
                if (data.file_synced === false) {
                    alert('⚠️ Track metadata saved to database, but file tags were not updated. Check file permissions/path and logs.');
                } else {
                    alert('✅ Track metadata updated (database + file tags)');
                }
            } else {
                alert('❌ Error: ' + (data.error || 'Failed to update'));
            }
        })
        .catch(err => {
            alert('❌ Network error: ' + err.message);
        });
    }

    let editTrackCurrentGenres = [];
    let editTrackModalInstance = null;

    // Open comprehensive track edit modal
    function openComprehensiveEditTrackModal(trackId, trackData) {
        // Null-safe field helpers: a stale cached JS file or a modal template
        // missing a field must not crash the dialog ("Cannot set properties
        // of null (setting 'value')") — missing elements are skipped.
        const setVal = (id, val) => { const el = document.getElementById(id); if (el) el.value = val; };
        const setChecked = (id, val) => { const el = document.getElementById(id); if (el) el.checked = val; };

        // Set track ID
        setVal('editTrackId', trackId);
        
        // Populate form with track data
        const titleEl = document.getElementById('editTrackTitle');
        if (titleEl) titleEl.textContent = trackData.title || 'Unknown';
        setVal('editTrackTitleField', trackData.title || '');
        setVal('editTrackArtistField', trackData.artist || '');
        setVal('editTrackAlbumField', trackData.album || '');
        setVal('editTrackYearField', trackData.year || '');
        setVal('editTrackStarsField', trackData.stars || 0);
        setVal('editTrackSingleField', trackData.is_single || 0);
        setVal('editTrackConfidenceField', trackData.single_confidence || 'low');
        setVal('editTrackAlbumArtistField', trackData.album_artist || '');
        let writerVal = trackData.writer || '';
        if (writerVal) {
            try {
                const parsed = JSON.parse(writerVal);
                if (Array.isArray(parsed)) {
                    writerVal = parsed.filter(w => w && typeof w === 'string' && w.trim()).join(', ');
                }
            } catch (e) { /* keep original value */ }
        }
        setVal('editTrackWriterField', writerVal);
        setVal('editTrackWorkField', trackData.work || '');
        setVal('editTrackTrackNumberField', trackData.track_number || '');
        setVal('editTrackDiscNumberField', trackData.disc_number || '');
        setVal('editTrackISRCField', trackData.isrc || '');
        setVal('editTrackMBIDField', trackData.mbid || '');
        setVal('editTrackMBAlbumIdField', trackData.musicbrainz_albumid || '');
        setVal('editTrackMBArtistIdField', trackData.musicbrainz_artistid || '');
        setVal('editTrackMBAlbumArtistIdField', trackData.musicbrainz_albumartistid || '');
        setVal('editTrackMBReleaseGroupIdField', trackData.musicbrainz_releasegroupid || '');
        setVal('editTrackMBReleaseTrackIdField', trackData.musicbrainz_releasetrackid || '');
        setVal('editTrackMBWorkIdField', trackData.musicbrainz_workid || '');
        setChecked('editTrackIsCoverField', trackData.is_cover == 1);
        setChecked('editTrackAlternateTakeField', trackData.alternate_take == 1);
        setChecked('editTrackIsCompilationField', trackData.is_compilation == 1);
        setChecked('editTrackIsLiveField', trackData.is_live == 1);
        setChecked('editTrackIsAcousticField', trackData.is_acoustic == 1);
        setChecked('editTrackIsRemixField', trackData.is_remix == 1);
        
        // Handle genres
        editTrackCurrentGenres = [];
        if (trackData.genres) {
            editTrackCurrentGenres = trackData.genres.split(/[;,\\\/]/).map(g => g.trim()).filter(g => g);
        }
        updateEditTrackGenresDisplay();
        
        // Load and display recommended genres
        loadRecommendedGenresForAlbumTrack(trackData.artist, trackId);
        
        // Show modal
        const modalEl = document.getElementById('editTrackModal');
        if (!modalEl) return;
        if (!editTrackModalInstance) {
            editTrackModalInstance = new bootstrap.Modal(modalEl);
        }
        editTrackModalInstance.show();
    }

    function loadRecommendedGenresForAlbumTrack(artist, trackId) {
        const section = document.getElementById('recommendedGenresSection');
        const display = document.getElementById('recommendedGenresDisplay');
        
        if (!section || !display) return;
        
        // Fetch genres from all sources for this track
        fetch(`/api/genres/track/${trackId}`)
          .then(r => r.json())
          .then(data => {
            if (!data.genres) return;
            
            // Collect all unique genres from Last.fm and Discogs sources
            const recommendedGenres = new Map();
            
            // Collect from Last.fm tags
            if (data.genres.lastfm_tags) {
              data.genres.lastfm_tags.forEach(genre => {
                const name = typeof genre === 'object' ? genre.name : genre;
                recommendedGenres.set(name, (recommendedGenres.get(name) || 0) + 1);
              });
            }
            
            // Collect from Discogs genres
            if (data.genres.discogs_genres) {
              data.genres.discogs_genres.forEach(genre => {
                const name = typeof genre === 'object' ? genre.name : genre;
                recommendedGenres.set(name, (recommendedGenres.get(name) || 0) + 1);
              });
            }
            
            if (recommendedGenres.size > 0) {
              section.style.display = 'block';
              
              let genresHtml = '';
              recommendedGenres.forEach((count, genre) => {
                genresHtml += `<button type="button" class="btn btn-sm btn-outline-info" onclick="addGenreFromRecommended('${escapeJsString(genre)}')" title="Add to track genres">
                  ${escapeHtml(genre)}
                  <small class="text-muted ms-1">(${count})</small>
                </button>`;
              });
              
              display.innerHTML = genresHtml;
            } else {
              section.style.display = 'none';
            }
          })
          .catch(err => {
            section.style.display = 'none';
            console.warn('Could not load recommended genres:', err);
          });
    }

    function addGenreFromRecommended(genre) {
        if (!editTrackCurrentGenres.includes(genre)) {
          editTrackCurrentGenres.push(genre);
          updateEditTrackGenresDisplay();
        }
    }

    function updateEditTrackGenresDisplay() {
        const container = document.getElementById('editTrackGenresDisplay');
        container.innerHTML = '';
        
        if (editTrackCurrentGenres.length === 0) {
            container.innerHTML = '<span class="text-muted small">No genres set</span>';
        } else {
            editTrackCurrentGenres.forEach(genre => {
                const badge = document.createElement('span');
                badge.className = 'badge bg-primary me-1 mb-1';
                badge.style.fontSize = '0.9rem';
                badge.innerHTML = `${genre} <button type="button" class="btn-close btn-close-white ms-1" style="font-size: 0.6rem;" onclick="removeEditTrackGenre('${escapeJsString(genre)}')" aria-label="Remove"></button>`;
                container.appendChild(badge);
            });
        }
        const genresField = document.getElementById('editTrackGenresField');
        if (genresField) genresField.value = editTrackCurrentGenres.join('\\');
    }

    function addEditTrackGenre() {
        const input = document.getElementById('editTrackGenreInput');
        const genre = input.value.trim();
        
        if (!genre) return;
        
        if (!editTrackCurrentGenres.includes(genre)) {
            editTrackCurrentGenres.push(genre);
            updateEditTrackGenresDisplay();
        }
        
        input.value = '';
        input.focus();
    }

    function removeEditTrackGenre(genre) {
        editTrackCurrentGenres = editTrackCurrentGenres.filter(g => g !== genre);
        updateEditTrackGenresDisplay();
    }

    function escapeJsString(str) {
        return str.replace(/'/g, "\\'").replace(/"/g, '\\"');
    }

    function saveComprehensiveEditedTrack() {
        const trackId = document.getElementById('editTrackId').value;
        
        if (!trackId) {
            alert('❌ Error: No track ID');
            return;
        }
        
        // Build payload from form
        const payload = {
            track_id: trackId,
            title: document.getElementById('editTrackTitleField').value.trim(),
            artist: document.getElementById('editTrackArtistField').value.trim(),
            album: document.getElementById('editTrackAlbumField').value.trim(),
            year: document.getElementById('editTrackYearField').value.trim() || null,
            stars: parseInt(document.getElementById('editTrackStarsField').value) || 0,
            is_single: parseInt(document.getElementById('editTrackSingleField').value) === 1,
            single_confidence: document.getElementById('editTrackConfidenceField').value,
            genres: editTrackCurrentGenres.join('\\'),
            album_artist: document.getElementById('editTrackAlbumArtistField').value.trim() || null,
            writer: document.getElementById('editTrackWriterField').value.trim() || null,
            work: document.getElementById('editTrackWorkField').value.trim() || null,
            track_number: document.getElementById('editTrackTrackNumberField').value.trim() || null,
            disc_number: document.getElementById('editTrackDiscNumberField').value.trim() || null,
            isrc: document.getElementById('editTrackISRCField').value.trim() || null,
            mbid: document.getElementById('editTrackMBIDField').value.trim() || null,
            musicbrainz_albumid: document.getElementById('editTrackMBAlbumIdField').value.trim() || null,
            musicbrainz_artistid: document.getElementById('editTrackMBArtistIdField').value.trim() || null,
            musicbrainz_albumartistid: document.getElementById('editTrackMBAlbumArtistIdField').value.trim() || null,
            musicbrainz_releasegroupid: document.getElementById('editTrackMBReleaseGroupIdField').value.trim() || null,
            musicbrainz_releasetrackid: document.getElementById('editTrackMBReleaseTrackIdField').value.trim() || null,
            musicbrainz_workid: document.getElementById('editTrackMBWorkIdField').value.trim() || null,
            is_cover: document.getElementById('editTrackIsCoverField').checked,
            alternate_take: document.getElementById('editTrackAlternateTakeField').checked,
            is_compilation: document.getElementById('editTrackIsCompilationField').checked,
            is_live: document.getElementById('editTrackIsLiveField').checked,
            is_acoustic: document.getElementById('editTrackIsAcousticField').checked,
            is_remix: document.getElementById('editTrackIsRemixField').checked,
            sync_to_file: true
        };
        
        // Validate required fields
        if (!payload.title) {
            alert('❌ Error: Title is required');
            return;
        }
        
        // Send to API
        fetch('/api/track/update-metadata', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
        })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                if (editTrackModalInstance) {
                    editTrackModalInstance.hide();
                }
                if (data.file_synced === false) {
                    alert('⚠️ Track metadata saved to database, but file tags were not updated. Check file permissions/path and logs.');
                } else {
                    alert('✅ Track metadata updated successfully (database + file tags)');
                }
                
                // Reload to show updated data
                setTimeout(() => {
                    location.reload();
                }, 500);
            } else {
                alert('❌ Error: ' + (data.error || 'Failed to update'));
            }
        })
        .catch(err => {
            alert('❌ Network error: ' + err.message);
        });
    }

    // Fetch track data and open comprehensive edit modal
    function openEditTrackFromAlbum(trackId) {
        fetch(`/api/track/${trackId}`)
            .then(r => {
                if (!r.ok) throw new Error('Track not found');
                return r.json();
            })
            .then(trackData => {
                openComprehensiveEditTrackModal(trackId, trackData);
            })
            .catch(err => {
                alert('❌ Error loading track: ' + err.message);
            });
    }

    // Genre sources are now pre-rendered server-side from database data populated during scans.
    // External source fetching only happens during the popularity/singles detection scan.

    // ===== Unified inline tracklist =====
    // Missing MusicBrainz tracks are merged INTO the main track table at their
    // Disc#/Track# position (dimmed, ⚠️ Missing badge, queue/match/ignore
    // actions) instead of a separate card.

    function _buildInlineMissingRow(mt, artist, album) {
        const num = escapeHtml(mt.track_number != null ? String(mt.track_number) : '?');
        const disc = Number(mt.disc_number || 1);
        const title = escapeHtml(mt.title || '—');
        const sArtist = escapeJsString(artist);
        const sAlbum = escapeJsString(album);
        const sTitle = escapeJsString(mt.title || '');
        const sNum = escapeJsString(mt.track_number != null ? String(mt.track_number) : '');
        const recMbid = escapeHtml(String(mt.recording_mbid || ''));

        const queueBtn = '<button class="btn btn-outline-success queue-missing-btn" title="Add to download queue" ' +
            'data-artist="' + sArtist + '" data-album-artist="' + sArtist + '" data-title="' + sTitle + '" ' +
            'data-album="' + sAlbum + '" data-track-number="' + sNum + '" data-disc-number="' + disc + '" ' +
            'data-recording-mbid="' + recMbid + '" onclick="queueMissingTrack(this)"><i class="bi bi-download"></i></button>';
        const matchBtn = '<button class="btn btn-outline-primary" title="Match to an existing song in the library" ' +
            'data-title="' + sTitle + '" data-track-number="' + sNum + '" data-artist="' + sArtist + '" ' +
            'data-album="' + sAlbum + '" onclick="openAlbumMatchModal(this)"><i class="bi bi-link-45deg"></i></button>';
        const ignoreBtn = '<button class="btn btn-outline-secondary" title="Ignore – hide this track from the missing list" ' +
            'data-title="' + sTitle + '" data-disc-number="' + disc + '" onclick="ignoreMissingTrack(this)"><i class="bi bi-x-lg"></i></button>';

        const row = document.createElement('tr');
        row.className = 'text-muted missing-track-row mb-inline-missing';
        row.style.opacity = '0.6';
        row.dataset.discNumber = String(disc);
        row.dataset.trackNumber = mt.track_number != null ? String(mt.track_number) : '';
        row.innerHTML =
            '<td class="d-md-none" colspan="7" style="padding: 0.5rem 0.75rem; border: none;">' +
                '<div class="d-flex align-items-center gap-2">' +
                    '<span class="text-muted fw-bold flex-shrink-0" style="min-width: 1.25rem; font-size: 0.85rem;">' + num + '</span>' +
                    '<span class="fst-italic flex-grow-1 text-truncate" style="min-width: 0;">' + title + '</span>' +
                    '<span class="badge bg-warning text-dark text-nowrap flex-shrink-0"><i class="bi bi-exclamation-triangle me-1"></i>Missing</span>' +
                '</div>' +
                '<div class="d-flex align-items-center gap-1 mt-1 ps-2">' + queueBtn + matchBtn + ignoreBtn + '</div>' +
            '</td>' +
            '<td class="d-none d-md-table-cell"></td>' +
            '<td class="d-none d-md-table-cell fst-italic">' + num + '</td>' +
            '<td class="d-none d-md-table-cell fst-italic">' + title + '</td>' +
            '<td class="d-none d-md-table-cell text-center text-muted small">--:--</td>' +
            '<td class="d-none d-md-table-cell text-center text-muted">—</td>' +
            '<td class="d-none d-md-table-cell"><span class="badge bg-warning text-dark"><i class="bi bi-exclamation-triangle me-1"></i>Missing</span></td>' +
            '<td class="d-none d-md-table-cell text-end"><div class="btn-group btn-group-sm">' + queueBtn + matchBtn + ignoreBtn + '</div></td>';
        return row;
    }

    async function mergeMissingTracks() {
        const tbody = document.getElementById('albumTracksTbody');
        const badge = document.getElementById('albumMissingHeaderBadge');
        if (!tbody) return;
        const artist = (_pageData && _pageData.artistName) || '';
        const album = (_pageData && _pageData.albumName) || '';
        if (!artist || !album) return;

        let data;
        try {
            const resp = await fetch(`/api/album/missing-tracks?artist=${encodeURIComponent(artist)}&album=${encodeURIComponent(album)}`);
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            data = await resp.json();
        } catch (err) {
            if (badge) badge.classList.add('d-none');
            return;
        }

        const missing = data.missing_tracks || [];
        if (badge) {
            if (missing.length > 0) {
                badge.textContent = missing.length + ' Missing';
                badge.classList.remove('d-none');
            } else {
                badge.classList.add('d-none');
            }
        }

        // Clear previously injected inline rows, then merge by Disc#/Track#.
        tbody.querySelectorAll('.mb-inline-missing').forEach(r => r.remove());
        if (missing.length === 0) return;

        const localRows = Array.prototype.filter.call(
            tbody.querySelectorAll('tr[data-track-id]'),
            r => r.dataset.trackId
        );
        const occupied = new Map();
        localRows.forEach(r => {
            const d = r.dataset.discNumber || '1';
            const t = r.dataset.trackNumber;
            if (t) occupied.set(d + ':' + t, r);
        });

        function rowKey(r) {
            const d = parseInt(r.dataset.discNumber || '1', 10) || 1;
            const t = parseInt(r.dataset.trackNumber || '9999', 10) || 9999;
            return d * 1000 + Math.min(t, 999);
        }

        for (const mt of missing) {
            const disc = String(Number(mt.disc_number || 1));
            const num = mt.track_number != null ? String(mt.track_number) : '';
            const rowEl = _buildInlineMissingRow(mt, artist, album);

            // Position already occupied by a local track → insert right after
            // it (album order preserved; title discrepancies surface through
            // the MusicBrainz comparison rows instead of a duplicate row).
            const existing = num ? occupied.get(disc + ':' + num) : null;
            if (existing) {
                let after = existing;
                let sib = after.nextElementSibling;
                while (sib && (sib.classList.contains('mb-update-row') || sib.classList.contains('mb-missing-row'))) {
                    after = sib;
                    sib = sib.nextElementSibling;
                }
                after.insertAdjacentElement('afterend', rowEl);
                continue;
            }

            // Otherwise insert sorted before the first row with a greater key.
            const key = rowKey(rowEl);
            let insertBefore = null;
            for (const r of localRows) {
                if (rowKey(r) > key) { insertBefore = r; break; }
            }
            if (insertBefore) tbody.insertBefore(rowEl, insertBefore);
            else tbody.appendChild(rowEl);
        }
    }

    // Hero overflow: "Download Missing Tracks" → jump to the tracklist and
    // (re)run the merge so the ⚠️ rows are always current.
    function downloadMissingTracks() {
        const section = document.getElementById('album-tracks-section');
        if (section) section.scrollIntoView({ behavior: 'smooth' });
        mergeMissingTracks();
    }

    async function rescanTrack(trackId) {
        if (!trackId) return;
        if (!confirm('Rescan this track from MusicBrainz / metadata sources?')) return;
        try {
            const resp = await fetch(`/api/track/${trackId}/rescan-single`, { method: 'POST' });
            const data = await resp.json().catch(() => ({}));
            if (data && data.success) {
                alert('✅ Track rescanned.');
            } else {
                alert('❌ Error: ' + ((data && data.error) || 'Rescan failed'));
            }
        } catch (err) {
            alert('❌ Network error: ' + err.message);
        }
    }

    /** Bulk-rescan every selected track with a single confirmation. */
    async function rescanSelectedTracks() {
        const ids = getSelectedTracks();
        if (!ids.length) return;
        if (!confirm(`Rescan ${ids.length} selected track(s) from MusicBrainz / metadata sources?`)) return;
        let ok = 0, failed = 0;
        for (const id of ids) {
            try {
                const resp = await fetch(`/api/track/${id}/rescan-single`, { method: 'POST' });
                const data = await resp.json().catch(() => ({}));
                if (data && data.success) ok += 1; else failed += 1;
            } catch (err) {
                failed += 1;
            }
        }
        alert(`✅ Rescanned ${ok} track(s).${failed ? ` ${failed} failed.` : ''}`);
    }

    function deleteTrack(trackId) {
        if (!trackId) return;
        // Route the single-track delete through the same confirmation modal
        // as bulk deletion so the scope (record-only vs file removal) is
        // always explicit before anything is deleted.
        openDeleteTrackModal([trackId]);
    }

    document.addEventListener('DOMContentLoaded', function() {
        const artist = _pageData.artistName;
        // Similar artists load lazily the first time the section is expanded.
        let similarLoaded = false;
        const similarCollapse = document.getElementById('album-similar-collapse');
        if (similarCollapse) {
            similarCollapse.addEventListener('shown.bs.collapse', function () {
                if (!similarLoaded && artist && artist.length > 0) {
                    similarLoaded = true;
                    loadSimilarArtistsForAlbum(artist);
                }
            });
        } else if (artist && artist.length > 0) {
            loadSimilarArtistsForAlbum(artist);
        }
        // Populate the unified inline tracklist on page load (missing MB
        // tracks merged at their Disc#/Track# positions).
        mergeMissingTracks();
    });

    // Similar Artists Loading
    async function loadSimilarArtistsForAlbum(artist) {
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 15000); // 15 second timeout
        
        try {
            const url = `/api/artist/${encodeURIComponent(artist)}/similar`;
            console.log('[SIMILAR ARTISTS] Fetching from:', url);
            const response = await fetch(url, {
                signal: controller.signal
            });
            clearTimeout(timeoutId);
            
            console.log('[SIMILAR ARTISTS] Response status:', response.status);
            
            if (!response.ok) {
                const errorText = `Server returned ${response.status}${response.statusText ? ': ' + response.statusText : ''}`;
                console.error('[SIMILAR ARTISTS] Error response:', errorText);
                showAlbumSimilarArtistsError(`Failed to load similar artists (${errorText})`);
                return;
            }
            
            const data = await response.json();
            console.log('[SIMILAR ARTISTS] Received data:', data);
            displayAlbumSimilarArtists(data);
        } catch (error) {
            clearTimeout(timeoutId);
            console.error('[SIMILAR ARTISTS] Exception:', error.name, error.message);
            if (error.name === 'AbortError') {
                console.warn('Similar artists loading timeout (15s)');
                showAlbumSimilarArtistsError('Similar artists loading timed out after 15 seconds');
            } else {
                console.error('Error loading similar artists:', error);
                showAlbumSimilarArtistsError('Error loading similar artists: ' + error.message);
            }
        }
    }

    function displayAlbumSimilarArtists(data) {
        const container = document.getElementById('albumSimilarArtistsContainer');

        const normalizeSimilarArtist = (entry, source) => {
            if (typeof entry === 'string') {
                const name = entry.trim();
                return name ? { name, match: 0, source, in_collection: false } : null;
            }
            if (!entry || typeof entry !== 'object') return null;
            const name = String(entry.name || entry.artist || '').trim();
            if (!name) return null;
            const matchValue = Number(entry.match ?? entry.score ?? 0);
            return {
                ...entry,
                name,
                match: Number.isFinite(matchValue) ? matchValue : 0,
                source,
                in_collection: !!entry.in_collection
            };
        };

        const lastfmArtists = (data.similar_artists && data.similar_artists.lastfm) || [];
        const listenbrainzArtists = (data.similar_artists && data.similar_artists.listenbrainz) || [];

        const allArtists = [
            ...lastfmArtists.map(a => normalizeSimilarArtist(a, 'lastfm')).filter(Boolean),
            ...listenbrainzArtists.map(a => normalizeSimilarArtist(a, 'listenbrainz')).filter(Boolean)
        ];

        // Deduplicate by name, accumulating the sources that recommended it
        const seen = new Map();
        allArtists.forEach(a => {
            const key = a.name.toLowerCase();
            if (seen.has(key)) {
                seen.get(key).sources.add(a.source);
            } else {
                seen.set(key, { ...a, sources: new Set([a.source]) });
            }
        });
        const uniqueArtists = Array.from(seen.values());

        // Only recommend artists NOT already in the collection — the section is
        // a discovery list, so owned artists are filtered out.
        const recommendations = uniqueArtists.filter(a => !a.in_collection);

        if (recommendations.length === 0) {
            container.innerHTML = '<div class="alert alert-success mb-0"><i class="bi bi-check-circle"></i> All recommended similar artists are already in your collection!</div>';
            return;
        }

        const sourceBadge = (a) => {
            const parts = [];
            if (a.sources.has('lastfm')) parts.push('<span class="badge bg-danger-subtle text-danger-emphasis">Last.fm</span>');
            if (a.sources.has('listenbrainz')) parts.push('<span class="badge bg-info-subtle text-info-emphasis">ListenBrainz</span>');
            return parts.join(' ');
        };

        let html = '<div class="row g-3">';

        recommendations.forEach(artist => {
            const matchPercent = artist.match ? Math.round(artist.match * 100) : 0;
            const matchBadge = matchPercent > 0 ? `<span class="badge bg-success position-absolute" style="top: 8px; right: 8px;">${matchPercent}%</span>` : '';
            const artistImageUrl = `/api/artist/image?name=${encodeURIComponent(artist.name)}`;
            html += `
                <div class="col-6 col-md-4 col-lg-3">
                    <div class="card h-100 overflow-hidden" style="position: relative;">
                        ${matchBadge}
                        <div style="aspect-ratio: 1; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); display: flex; align-items: center; justify-content: center; position: relative;">
                            <img src="${artistImageUrl}" alt="${escapeHtml(artist.name)}" style="width: 100%; height: 100%; object-fit: cover;" onerror="this.style.display='none'; this.nextElementSibling.style.display='block';">
                            <i class="bi bi-person-circle" style="font-size: 3rem; color: white; opacity: 0.8; display: none;" aria-label="Artist placeholder icon"></i>
                        </div>
                        <div class="card-body d-flex flex-column">
                            <h6 class="card-title mb-2 fw-bold text-truncate" title="${escapeHtml(artist.name)}">${escapeHtml(artist.name)}</h6>
                            <div class="mb-2">${sourceBadge(artist)}</div>
                            <div class="btn-group-vertical btn-group-sm mt-auto" role="group">
                                <button type="button" class="btn btn-outline-secondary btn-sm" onclick="searchMusicBrainzReleaseFromEncoded(null, '${encodeURIComponent(artist.name)}', '')" title="Search MusicBrainz for releases">
                                    <i class="bi bi-download"></i> Find Releases
                                </button>
                                <a href="https://www.last.fm/music/${encodeURIComponent(artist.name)}" target="_blank" class="btn btn-outline-info btn-sm" title="View on Last.fm">
                                    <i class="bi bi-box-arrow-up-right"></i> Last.fm
                                </a>
                            </div>
                        </div>
                    </div>
                </div>
            `;
        });
        html += '</div>';

        container.innerHTML = html;
    }

    function showAlbumSimilarArtistsError(message) {
        const container = document.getElementById('albumSimilarArtistsContainer');
        container.innerHTML = `<div class="alert alert-danger mb-0"><i class="bi bi-exclamation-circle"></i> ${escapeHtml(message)}</div>`;
    }

    // Bulk update track artists for album
    function bulkUpdateTrackArtists() {
        const newArtist = document.getElementById('bulkArtistEditInput').value.trim();
        if (!newArtist) {
            alert('Please enter a new artist name.');
            return;
        }
        const album = _pageData.albumName;
        const artist = _pageData.artistName;
        fetch(`/api/tags/album/${encodeURIComponent(album)}/${encodeURIComponent(artist)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tags: { artist: newArtist } })
        })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                alert('✅ All track artists updated successfully!');
                setTimeout(() => location.reload(), 500);
            } else {
                alert('❌ Error: ' + (data.error || 'Failed to update track artists.'));
            }
        })
        .catch(err => {
            alert('❌ Network error: ' + err.message);
        });
    }

    function renameAlbumFiles(artist, album) {
        /**
         * Rename all files in an album based on current metadata.
         * Calls the /api/album/rename-files endpoint to:
         * 1. Calculate new file paths from current metadata
         * 2. Move files to new locations
         * 3. Update database entries
         */
        if (!confirm(`Are you sure you want to rename all files in "${album}" by ${artist}?\n\nFiles will be renamed and organized according to the "Default Naming Convention" in Settings → File Management.\n\nThis operation cannot be undone.`)) {
            return;
        }

        const button = event.target;
        button.disabled = true;
        button.textContent = '🔄 Renaming...';

        const progressContainer = document.createElement('div');
        progressContainer.className = 'alert alert-info mt-3';
        progressContainer.innerHTML = '<div class="spinner-border spinner-border-sm me-2" role="status"><span class="visually-hidden">Loading...</span></div> Renaming album files...';
        
        const detailsButton = document.querySelector('.card-header');
        if (detailsButton) {
            detailsButton.parentElement.insertBefore(progressContainer, detailsButton.nextSibling);
        }

        fetch(`/api/album/${encodeURIComponent(artist)}/${encodeURIComponent(album)}/rename-files`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        })
        .then(response => {
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }
            return response.json();
        })
        .then(data => {
            button.disabled = false;
            button.textContent = '📁 Rename Files';

            progressContainer.className = data.success ? 'alert alert-success mt-3' : 'alert alert-danger mt-3';
            
            let html = `<strong>${data.success ? '✅ Rename Complete!' : '❌ Rename Failed!'}</strong><br>`;
            html += `Files renamed: ${data.renamed_count}<br>`;
            html += `Database updated: ${data.updated_db_count}<br>`;
            
            if (data.errors && data.errors.length > 0) {
                html += `<br><strong>Errors:</strong><ul class="mb-0">`;
                data.errors.forEach(err => {
                    html += `<li>${err}</li>`;
                });
                html += `</ul>`;
            }
            
            if (data.details && data.details.length > 0) {
                html += `<br><strong>Files Renamed (${data.details.length}):</strong><ul class="small mb-0" style="max-height: 300px; overflow-y: auto;">`;
                data.details.forEach(detail => {
                    html += `<li><strong>${detail.track}:</strong><br>`;
                    html += `  <code style="font-size: 0.85rem; color: #666;">${detail.old_path}</code><br>`;
                    html += `  <code style="font-size: 0.85rem; color: #0d6efd;">→ ${detail.new_path}</code></li>`;
                });
                html += `</ul>`;
            }
            
            progressContainer.innerHTML = html;
            
            // Auto-reload after 3 seconds if successful
            if (data.success) {
                setTimeout(() => {
                    window.location.reload();
                }, 3000);
            }
        })
        .catch(error => {
            button.disabled = false;
            button.textContent = '📁 Rename Files';
            progressContainer.className = 'alert alert-danger mt-3';
            progressContainer.innerHTML = `<strong>❌ Error:</strong> ${error.message}`;
        });
    }
    async function queueMissingTrack(btn) {
        const artist = btn.dataset.artist || btn.dataset.albumArtist;
        const title = btn.dataset.title;
        const album = btn.dataset.album;
        const trackNumber = btn.dataset.trackNumber || null;
        const discNumber = btn.dataset.discNumber || null;
        const year = btn.dataset.year || null;
        const recordingMbid = btn.dataset.recordingMbid || null;
        const duration = btn.dataset.duration ? parseInt(btn.dataset.duration, 10) : null;
        // Fall back to the album's currently-assigned MBID when the track row
        // doesn't carry one (e.g. the user just selected a new release via the
        // Metadata lookup but hasn't saved yet).
        const releaseId = btn.dataset.releaseId
            || (document.getElementById('album_mbid') || {}).value
            || (document.getElementById('musicbrainzReleaseId') || {}).value
            || null;

        const origHtml = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status"></span>';

        try {
            const resp = await fetch('/api/queue/add', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    artist: artist,
                    title: title,
                    album: album,
                    album_artist: btn.dataset.albumArtist || null,
                    track_number: trackNumber,
                    disc_number: discNumber,
                    year: year,
                    release_id: releaseId,
                    // release_mbid fills a separate column used for MB-specific duplicate
                    // detection and cross-source overwrite merge in add_to_queue.
                    release_mbid: releaseId,
                    release_source: releaseId ? 'musicbrainz' : null,
                    recording_mbid: recordingMbid,
                    duration: duration,
                    source: 'soulseek'
                })
            });
            const data = await resp.json();
            if (data.success) {
                btn.innerHTML = '<i class="bi bi-check-lg"></i>';
                btn.classList.remove('btn-outline-success');
                btn.classList.add('btn-success');
                btn.title = 'Added to download queue';
            } else {
                btn.disabled = false;
                btn.innerHTML = origHtml;
                alert('Failed to queue track: ' + (data.error || 'Unknown error'));
            }
        } catch (e) {
            btn.disabled = false;
            btn.innerHTML = origHtml;
            alert('Network error: ' + e.message);
        }
    }

    // ── Delete Queued Track from Album Page ───────────────────────────────────
    async function deleteQueuedTrackOnAlbumPage(btn) {
        const queueId = btn.dataset.queueId;
        const trackId = btn.dataset.trackId;
        if (!queueId || queueId === '') {
            alert('Cannot determine queue ID for this track.');
            return;
        }
        if (!confirm('Remove this track from the download queue? The stub will be removed from the album view.')) {
            return;
        }
        const origHtml = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status"></span>';
        try {
            const resp = await fetch(`/api/queue/${queueId}/delete`, { method: 'DELETE' });
            const data = await resp.json();
            if (data.success) {
                // Remove the row from the table
                const row = btn.closest('tr');
                if (row) row.remove();
            } else {
                btn.disabled = false;
                btn.innerHTML = origHtml;
                alert('Failed to remove from queue: ' + (data.error || 'Unknown error'));
            }
        } catch (e) {
            btn.disabled = false;
            btn.innerHTML = origHtml;
            alert('Network error: ' + e.message);
        }
    }

    // ── Match Missing Track to Existing Song ──────────────────────────────────
    let _matchMbTrack = null;

    async function openAlbumMatchModal(btn) {
        _matchMbTrack = {
            title: btn.dataset.title,
            track_number: btn.dataset.trackNumber,
            release_id: btn.dataset.releaseId,
            artist: btn.dataset.artist,
            album: btn.dataset.album,
        };
        document.getElementById('albumMatchMbTitle').textContent = _matchMbTrack.title;
        document.getElementById('albumMatchMbTrackNum').textContent = _matchMbTrack.track_number || '?';

        const tbody = document.getElementById('albumMatchCandidatesTbody');
        tbody.innerHTML = '<tr><td colspan="4" class="text-center"><span class="spinner-border spinner-border-sm"></span> Loading…</td></tr>';

        const modalEl = document.getElementById('albumMatchTrackModal');
        new bootstrap.Modal(modalEl).show();

        // Fetch library tracks for this album
        try {
            const artistName = _pageData.artistName;
            const albumName = _pageData.albumName;
            const resp = await fetch(`/api/album/library-tracks?artist=${encodeURIComponent(artistName)}&album=${encodeURIComponent(albumName)}`);
            const data = await resp.json();
            const tracks = data.tracks || [];
            if (tracks.length === 0) {
                tbody.innerHTML = '<tr><td colspan="4" class="text-center text-muted">No library tracks found for this album.</td></tr>';
            } else {
                tbody.innerHTML = tracks.map(t => `
                    <tr>
                        <td class="text-muted">${t.track_number || '—'}</td>
                        <td>${escapeHtml(t.title || '—')}</td>
                        <td class="small text-muted">${escapeHtml(t.file_path || '—')}</td>
                        <td class="text-center">
                            <button class="btn btn-sm btn-primary" onclick="doAlbumMatchTrack('${t.id}')">
                                <i class="bi bi-check-lg"></i> Match
                            </button>
                        </td>
                    </tr>`).join('');
            }
        } catch (err) {
            tbody.innerHTML = `<tr><td colspan="4" class="text-danger text-center">Error: ${escapeHtml(err.message)}</td></tr>`;
        }
    }

    async function doAlbumMatchTrack(trackId) {
        if (!_matchMbTrack) return;
        if (!confirm(`Sync MB metadata to this track?\n\nNew title: "${_matchMbTrack.title}"\n\nThis will update the database and the MP3 file tags.`)) return;

        try {
            const resp = await fetch('/api/track/match-missing', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    track_id: String(trackId),
                    mb_title: _matchMbTrack.title,
                    mb_track_number: _matchMbTrack.track_number,
                    mb_release_id: _matchMbTrack.release_id,
                })
            });
            const result = await resp.json();
            bootstrap.Modal.getInstance(document.getElementById('albumMatchTrackModal')).hide();
            if (result.success) {
                alert(`✓ Matched!\n"${result.old_title}" → "${result.new_title}"${result.updated_file ? '\nMP3 tags updated.' : ''}`);
                location.reload();
            } else {
                alert(`✗ Error: ${result.error}`);
            }
        } catch (err) {
            alert(`✗ Error: ${err.message}`);
        }
    }

    function escapeHtml(str) {
        return String(str || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }





function openAlbumTrackMbReleaseModal(trackId) {
    _albumMbReleaseTargetTrackId = String(trackId);
    const statusEl = document.getElementById('albumTrackMbReleaseStatus');
    const errorEl = document.getElementById('albumTrackMbReleaseError');
    const resultsEl = document.getElementById('albumTrackMbReleaseResults');

    statusEl.style.display = 'block';
    errorEl.style.display = 'none';
    resultsEl.innerHTML = '';

    const modal = new bootstrap.Modal(document.getElementById('albumTrackMbReleaseModal'));
    modal.show();

    fetch('/api/track/' + encodeURIComponent(trackId) + '/mb-releases')
        .then(r => r.json())
        .then(data => {
            statusEl.style.display = 'none';
            if (data.error) {
                errorEl.textContent = data.error;
                errorEl.style.display = 'block';
                return;
            }
            renderAlbumTrackMbReleases(data.releases || []);
        })
        .catch(err => {
            statusEl.style.display = 'none';
            errorEl.textContent = 'Network error: ' + err.message;
            errorEl.style.display = 'block';
        });
}

function renderAlbumTrackMbReleases(releases) {
    const resultsEl = document.getElementById('albumTrackMbReleaseResults');
    if (!releases.length) {
        resultsEl.innerHTML = '<div class="alert alert-info">No releases found for this recording on MusicBrainz.</div>';
        return;
    }

    function _escHtml(str) {
        const d = document.createElement('div');
        d.textContent = str || '';
        return d.innerHTML;
    }

    let html = '<div class="list-group">';
    releases.forEach(rel => {
        const typeLabel = rel.type ? `<span class="badge bg-secondary me-1" style="font-size:0.7rem;">${_escHtml(rel.type)}</span>` : '';
        const statusLabel = rel.status ? `<span class="badge bg-${rel.status.toLowerCase() === 'official' ? 'success' : 'warning'} me-1" style="font-size:0.7rem;">${_escHtml(rel.status)}</span>` : '';
        const countryLabel = rel.country ? `<span class="badge bg-info me-1" style="font-size:0.7rem;">${_escHtml(rel.country)}</span>` : '';
        const trackPosLabel = rel.track_position ? `<span class="text-muted" style="font-size:0.8rem;">Track ${_escHtml(String(rel.track_position))}</span>` : '';

        html += `
            <div class="list-group-item" style="padding:0.85rem;">
                <div class="d-flex justify-content-between align-items-start gap-2 flex-wrap">
                    <div class="flex-grow-1">
                        <div class="fw-semibold mb-1">${_escHtml(rel.title)}</div>
                        <div class="mb-1">${typeLabel}${statusLabel}${countryLabel}${trackPosLabel}</div>
                        <div class="text-muted" style="font-size:0.75rem;">
                            ${rel.year ? '<strong>' + _escHtml(rel.year) + '</strong> &nbsp;' : ''}
                            MBID: <code style="font-size:0.7rem;">${_escHtml(rel.release_mbid)}</code>
                        </div>
                    </div>
                    <div class="flex-shrink-0">
                        <a href="https://musicbrainz.org/release/${encodeURIComponent(rel.release_mbid)}" target="_blank" class="btn btn-sm btn-outline-secondary me-1" title="Open on MusicBrainz">
                            <i class="bi bi-box-arrow-up-right"></i>
                        </a>
                        <button class="btn btn-sm btn-primary album-mb-apply-btn"
                                data-release-mbid="${_escHtml(rel.release_mbid)}"
                                data-release-group-mbid="${_escHtml(rel.release_group_mbid || '')}"
                                data-release-title="${_escHtml(rel.title)}">
                            <i class="bi bi-check-circle"></i> Select
                        </button>
                    </div>
                </div>
            </div>`;
    });
    html += '</div>';
    resultsEl.innerHTML = html;

    resultsEl.querySelectorAll('.album-mb-apply-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            applyAlbumTrackMbRelease(btn.dataset.releaseMbid, btn.dataset.releaseGroupMbid, btn.dataset.releaseTitle);
        });
    });
}

function applyAlbumTrackMbRelease(releaseMbid, releaseGroupMbid, releaseTitle) {
    if (!_albumMbReleaseTargetTrackId) return;

    fetch('/api/track/' + encodeURIComponent(_albumMbReleaseTargetTrackId) + '/apply-mb-release', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ release_mbid: releaseMbid, release_group_mbid: releaseGroupMbid })
    })
    .then(r => r.json())
    .then(data => {
        if (!data.success) {
            alert('Failed to apply release: ' + (data.error || 'Unknown error'));
            return;
        }
        const modal = bootstrap.Modal.getInstance(document.getElementById('albumTrackMbReleaseModal'));
        if (modal) modal.hide();
        setTimeout(() => {
            alert('✅ Release matched for track!\nMusicBrainz Album ID updated to:\n' + releaseTitle + '\n' + releaseMbid);
            location.reload();
        }, 150);
    })
    .catch(err => alert('Network error: ' + err.message));
}

// ---------------------------------------------------------------------------
// Live queue-status polling
// Polls /api/album/queue-status every 8 seconds while there are still tracks
// with an active download status.  Updates badges in-place so the user can see
// real-time progress (Queued → Searching → Downloading → Completed) without a
// full page reload.
// ---------------------------------------------------------------------------
(function () {
    const _albumArtist = _pageData.artistName;
    const _albumName   = _pageData.albumName;

    // Collect all queue-status badge spans on the page at startup.
    // Each queued track row has data-track-id on the <tr> and badge spans
    // with class "queue-status-badge" that we populate below.
    const POLL_INTERVAL_MS = 8000;
    const ACTIVE_STATUSES = new Set(['queued', 'searching', 'downloading', 'unmatched', 'moving', 'queried', 'copy_recommended']);
    let _pollTimer = null;
    let _sawActiveStatus = false;

    // Map existing "In Queue" static badges to JS-controlled spans so we can
    // update them without touching anything else.
    document.querySelectorAll('.badge.bg-warning[title="This track is currently in the download queue"]').forEach(badge => {
        // Find the parent <tr data-track-id="...">
        const row = badge.closest('tr[data-track-id]');
        if (!row) return;
        badge.classList.add('queue-status-badge');
        badge.dataset.trackId = row.dataset.trackId;
    });

    const allBadges = () => document.querySelectorAll('.queue-status-badge[data-track-id]');

    function hasActiveBadges() {
        for (const b of allBadges()) {
            if (ACTIVE_STATUSES.has(b.dataset.currentStatus || 'queued')) return true;
        }
        return false;
    }

    function applyStatusToBadge(badge, info) {
        badge.dataset.currentStatus = info.status;
        badge.className = `badge ${info.css} ms-1 queue-status-badge`;
        // Build label with icon
        badge.innerHTML = `<i class="bi bi-${info.icon} me-1"></i>${escapeHtml(info.label)}`;
        badge.title = `Download status: ${info.label}`;
        if (ACTIVE_STATUSES.has(info.status)) {
            _sawActiveStatus = true;
        }
    }

    async function pollQueueStatus() {
        const badges = allBadges();
        if (badges.length === 0) {
            stopPolling();
            return;
        }

        try {
            const resp = await fetch(
                `/api/album/queue-status?artist=${encodeURIComponent(_albumArtist)}&album=${encodeURIComponent(_albumName)}`
            );
            if (!resp.ok) return;
            const data = await resp.json();
            if (!data.success || !data.tracks) return;

            for (const badge of badges) {
                const trackId = badge.dataset.trackId;
                const info = data.tracks[trackId];
                if (!info) continue;

                applyStatusToBadge(badge, info);

                // If a track completed, trigger a page reload after a short delay
                // so the track row is replaced with the real file-based row.
                if (info.status === 'completed') {
                    badge.innerHTML = `<i class="bi bi-check-circle-fill me-1"></i>Completed`;
                    badge.className = 'badge bg-success ms-1 queue-status-badge';
                }
            }

            if (!hasActiveBadges()) {
                stopPolling();
                // Only reload if we actually saw an active status during this polling
                // session and they have since become terminal.  This prevents an
                // infinite reload loop when a track is already terminal (e.g. failed,
                // completed, imported) but its tracks row still carries the
                // __queued_for_download__ marker.
                if (_sawActiveStatus) {
                    const reloadKey = `album_reload_${_albumArtist}_${_albumName}`;
                    const lastReload = sessionStorage.getItem(reloadKey);
                    const now = Date.now();
                    if (!lastReload || (now - parseInt(lastReload, 10)) > 60000) {
                        sessionStorage.setItem(reloadKey, String(now));
                        setTimeout(() => location.reload(), 2500);
                    }
                }
            }
        } catch (_e) {
            // Silently ignore network errors during polling
        }
    }

    function stopPolling() {
        if (_pollTimer !== null) {
            clearInterval(_pollTimer);
            _pollTimer = null;
        }
    }

    // Only start polling if there are actually queued tracks on this page.
    if (allBadges().length > 0) {
        // Kick off an immediate check, then repeat.
        pollQueueStatus();
        _pollTimer = setInterval(pollQueueStatus, POLL_INTERVAL_MS);
    }
})();

// ═══════════════════════════════════════════════════════════════════════════
// Reorganisation Plan (#867) — album hero, mobile tabs, correction banner
// actions, genre tag clamp.
// ═══════════════════════════════════════════════════════════════════════════

// Mobile 4-tab navigation on <lg viewports.
// Mobile 4-tab navigation now runs from the shared engine in main.js
// (``[data-mobile-tabs]`` bars) — see initMobileTabs().

// Toggle the hero "Top Genres" +X more popover (album genre tag management).
// "+N more" genres link in the hero → jump to the full Genres tab (mobile
// tab engine) or scroll to the Genres card (desktop, all sections visible).
function goToAlbumGenres() {
  var bar = document.getElementById('albumMobileTabBar');
  var btn = bar && bar.querySelector('[data-tab="genres"]');
  if (btn && window.innerWidth < 992) {
    btn.click();
    return;
  }
  var section = document.getElementById('album-genres-section');
  if (section) section.scrollIntoView({ behavior: 'smooth' });
}

// Clamp per-track genre badges to the top 3, hiding the rest behind "+X more".
function initAlbumGenreClamp() {
    document.querySelectorAll('.genre-badge').forEach(function (badge) {
        const container = badge.closest('.d-flex.align-items-center');
        if (!container) return;
        const badges = Array.from(container.querySelectorAll('.genre-badge'));
        if (badges.length <= 3) return;
        // Hide extras, append a "+N more" toggle.
        badges.slice(3).forEach(function (b) { b.style.display = 'none'; });
        const more = document.createElement('button');
        more.type = 'button';
        more.className = 'btn btn-link p-0 genre-badge-more';
        more.style.textDecoration = 'none';
        more.textContent = `+${badges.length - 3} more`;
        more.addEventListener('click', function () {
            const showing = more.dataset.open === '1';
            badges.slice(3).forEach(function (b) { b.style.display = showing ? 'none' : ''; });
            more.textContent = showing ? `+${badges.length - 3} more` : 'less';
            more.dataset.open = showing ? '0' : '1';
        });
        container.insertBefore(more, badges[3].nextSibling);
    });
}

// Auto-Link All MBIDs: resolve unlinked tracks against the official release
// tracklist and persist the Recording IDs.
async function autoLinkAllMbids() {
    const btn = document.getElementById('albumAutoLinkBtn');
    const orig = btn ? btn.innerHTML : '';
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status"></span>';
    }
    try {
        const releaseId = (document.getElementById('album_mbid') || {}).value || '';
        const resp = await fetch('/api/musicbrainz/link-album-mbids', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                artist: _pageData.artistName || document.getElementById('album_artist')?.value || '',
                album: _pageData.albumName || document.getElementById('album_title')?.value || '',
                release_id: releaseId
            })
        });
        const data = await resp.json();
        if (data.success) {
            alert(`✅ ${data.message || 'Tracks linked.'}`);
            window.location.reload();
        } else {
            alert(`❌ ${data.error || 'Failed to link MBIDs'}`);
        }
    } catch (e) {
        alert('❌ Network error: ' + e.message);
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = orig || '<i class="bi bi-link-45deg"></i> Link';
        }
    }
}

// Align Tracklist: open the MusicBrainz lookup modal so the official release
// tracklist can be compared and applied.
function alignTracklist() {
    if (typeof openAlbumLookupModal === 'function') {
        openAlbumLookupModal();
    }
}

document.addEventListener('DOMContentLoaded', function () {
    // Mobile tabs are driven by the shared engine in main.js
    // ([data-mobile-tabs] bar) — see initMobileTabs().
    initAlbumGenreClamp();
});

// ============================================================================
// PER-USER TRACK HEARTS (Navidrome star sync)
// ============================================================================

// Hearted track IDs for the ACTIVE user (loaded once per page view).
let _heartedTrackIds = new Set();

async function loadHeartedTrackIds() {
    try {
        const resp = await fetch('/api/favourites/ids?entity_type=track');
        const data = await resp.json();
        if (data && data.success && Array.isArray(data.ids)) {
            _heartedTrackIds = new Set(data.ids);
        }
    } catch (err) {
        console.error('Failed to load hearted tracks:', err);
    }
}

function _heartIcon(hearted) {
    return hearted ? '<i class="bi bi-heart-fill text-danger"></i>' : '<i class="bi bi-heart"></i>';
}

function refreshTrackHeartButtons() {
    document.querySelectorAll('.track-heart-btn').forEach(function (btn) {
        const trackId = btn.getAttribute('data-track-id');
        const hearted = _heartedTrackIds.has(trackId);
        btn.innerHTML = _heartIcon(hearted);
        btn.classList.toggle('hearted', hearted);
        btn.setAttribute('aria-pressed', hearted ? 'true' : 'false');
    });
}

async function toggleTrackHeart(btn) {
    const trackId = btn.getAttribute('data-track-id');
    if (!trackId) return;

    const currentlyHearted = _heartedTrackIds.has(trackId);
    const nextHearted = !currentlyHearted;

    // Optimistic update.
    if (nextHearted) {
        _heartedTrackIds.add(trackId);
    } else {
        _heartedTrackIds.delete(trackId);
    }
    refreshTrackHeartButtons();

    try {
        const resp = await fetch('/api/favourites/toggle', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                entity_type: 'track',
                entity_id: trackId,
                navidrome_id: trackId,
                is_favourite: nextHearted,
            }),
        });
        const data = await resp.json();
        if (!resp.ok || !data || !data.success) {
            // Revert the optimistic update on failure.
            if (nextHearted) {
                _heartedTrackIds.delete(trackId);
            } else {
                _heartedTrackIds.add(trackId);
            }
            refreshTrackHeartButtons();
            const msg = (data && data.error) || 'Failed to update heart';
            if (typeof showToastMsg === 'function') {
                showToastMsg(msg, true);
            } else {
                alert(msg);
            }
            return;
        }
        // If Navidrome sync failed, surface a gentle warning but keep the
        // local heart state (it will re-sync on the next background pull).
        if (data.navidrome_synced === false) {
            console.warn('Heart saved locally but Navidrome sync failed for track', trackId);
        }
    } catch (err) {
        // Network error — revert.
        if (nextHearted) {
            _heartedTrackIds.delete(trackId);
        } else {
            _heartedTrackIds.add(trackId);
        }
        refreshTrackHeartButtons();
        console.error('Toggle heart error:', err);
    }
}

document.addEventListener('click', function (e) {
    const btn = e.target.closest('.track-heart-btn');
    if (btn) {
        e.preventDefault();
        toggleTrackHeart(btn);
    }
});

document.addEventListener('DOMContentLoaded', function () {
    loadHeartedTrackIds().then(refreshTrackHeartButtons);
});



