// ===== ALBUM DETAIL PAGE JAVASCRIPT =====

var _pageData = window._pageData || {};

let currentEditTrackId = null;
let currentEditTrackTitle = null;

if (typeof addGenreModalInstance === 'undefined') {
    var addGenreModalInstance = null;
}

let currentAlbumGenres = new Set();

// ─────────────────────────────────────────────────────────────
// Admin & Scan Utilities
// ─────────────────────────────────────────────────────────────

function forceAlbumMetadataRefresh() {
    const form = document.getElementById('albumScanForm');
    if (!form) return;
    const force = document.getElementById('albumForceScan');
    if (force) force.checked = true;
    form.submit();
}

// ─────────────────────────────────────────────────────────────
// Player Helpers
// ─────────────────────────────────────────────────────────────

function playTrackFromAlbum(btn) {
    if (typeof Player === 'undefined') {
        return;
    }
    Player.playTrack(
        btn.dataset.trackId,
        btn.dataset.title,
        btn.dataset.artist,
        btn.dataset.art
    );
}

function playAlbum() {
    if (typeof Player === 'undefined') {
        return;
    }

    const rows = document.querySelectorAll('#albumTracksTbody tr');
    const tracks = [];

    rows.forEach(row => {
        const trackId = row.dataset.trackId;
        if (!trackId) {
            return;
        }

        tracks.push({
            id: trackId,
            title: row.dataset.trackTitle || '',
            artist: row.dataset.trackArtist || '',
            albumArtUrl: document.getElementById('albumArtImage')?.src || ''
        });
    });

    if (!tracks.length) {
        alert('No tracks available on this album.');
        return;
    }

    Player.playQueue(tracks);
}

// ─────────────────────────────────────────────────────────────
// MusicBrainz Album Lookup
// ─────────────────────────────────────────────────────────────

function openAlbumLookupModal() {
    const artist = _pageData.artistName || '';
    const album = _pageData.albumName || '';

    window._mbSearchCallback = function (selected) {
        window._mbSearchCallback = null;
        if (!selected) {
            return;
        }

        const group = selected.release || {};
        const rgMbid = group.id || selected.id;
        const releaseMbid = (selected.id && selected.id !== rgMbid) ? selected.id : '';
        const year = (group.first_release_date || '').toString().split('-')[0] || '';
        const albumType = _buildAlbumType(group.primary_type, group.secondary_types);
        const cover = group.cover_art_url || '';
        const albumArtist = selected.artist || group.artist || '';

        const concreteReleases = Array.isArray(group.releases) ? group.releases : [];

        if (releaseMbid) {
            populateAlbumFields(selected.title || group.title || album, year, albumType, releaseMbid, '', cover, '', rgMbid, albumArtist);
        } else if (rgMbid && concreteReleases.length === 1) {
            const single = concreteReleases[0];
            populateAlbumFields(single.title || selected.title || group.title || album, String(single.date || '').slice(0, 4) || year, albumType, single.id, '', single.cover_art_url || cover, '', rgMbid, albumArtist);
        } else if (rgMbid && typeof openReleasePickerModal === 'function') {
            openReleasePickerModal(rgMbid, selected.title || group.title || album, year, albumType, concreteReleases.length > 1 ? concreteReleases : null, null, cover, albumArtist);
        } else {
            populateAlbumFields(selected.title || group.title || album, year, albumType, releaseMbid, '', cover, '', rgMbid, albumArtist);
        }
    };

    window._mbSearchIncludeOwned = true;
    window._mbSearchWithReleases = true;

    // Use Global MB Search if available (from main.js), otherwise fallback to local Album Lookup Modal
    if (typeof window.openGlobalMbSearch === 'function') {
        window.openGlobalMbSearch(artist, album, window._mbSearchCallback, '', '');
    } else {
        const lookupEl = document.getElementById('albumLookupModal');
        if (lookupEl) {
            const modal = bootstrap.Modal.getInstance(lookupEl) || new bootstrap.Modal(lookupEl);
            modal.show();
            if(document.getElementById('albumLookupAlbum')) {
                document.getElementById('albumLookupAlbum').value = album;
                document.getElementById('albumLookupArtist').value = artist;
                runAlbumLookup();
            }
        }
    }
}

function runAlbumLookup() {
    const artist = (document.getElementById('albumLookupArtist')?.value || '').trim() || _pageData.artistName;
    const album = (document.getElementById('albumLookupAlbum')?.value || '').trim() || _pageData.albumName;

    if (!artist || !album) {
        alert('Please enter both artist and album names.');
        return;
    }

    const resultsDiv = document.getElementById('albumLookupResults');
    if (!resultsDiv) {
        return;
    }

    resultsDiv.innerHTML = '<div class="spinner-border spinner-border-sm"></div> Searching MusicBrainz...';

    fetch('/api/album/musicbrainz', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({
            album: album,
            artist: artist,
            existing_mbid: document.getElementById('album_mbid')?.value || null
        })
    })
    .then(response => {
        if (!response.ok) {
            throw new Error('HTTP error ' + response.status);
        }
        return response.json();
    })
    .then(data => {
        if (data.error) {
            resultsDiv.innerHTML = '<div class="alert alert-danger">' + escapeHtml(data.error) + '</div>';
            return;
        }
        displayAlbumResults(data.results, 'musicbrainz');
    })
    .catch(error => {
        resultsDiv.innerHTML = '<div class="alert alert-danger">' + escapeHtml(error.message) + '</div>';
    });
}

// ─────────────────────────────────────────────────────────────
// Utility Functions
// ─────────────────────────────────────────────────────────────

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
    if (!resultsDiv) return;

    if (!results || results.length === 0) {
        resultsDiv.innerHTML = '<div class="alert alert-warning">No matches found.</div>';
        return;
    }

    let html = '<div class="list-group">';
    results.forEach(result => {
        const year = (result.first_release_date || '').slice(0, 4);
        const combinedType = _buildAlbumType(result.primary_type, result.secondary_types);
        
        html += `
            <div class="list-group-item list-group-item-action bg-dark text-light border-secondary p-3">
                <div class="d-flex justify-content-between align-items-center">
                    <div>
                        <h6 class="mb-1">${escapeHtml(result.title)}</h6>
                        <small class="text-muted">by ${escapeHtml(result.artist)}</small>
                        <div class="small mt-1">
                            <span class="badge bg-info text-dark">${escapeHtml(combinedType)}</span>
                            ${year ? `<span class="badge bg-secondary">${escapeHtml(year)}</span>` : ''}
                        </div>
                    </div>
                    <button class="btn btn-sm btn-primary" onclick="populateAlbumFields('${escapeJsString(result.title)}', '${year}', '${combinedType}', '${result.mbid}', '', '${escapeJsString(result.cover_art_url || '')}', '', '${result.id || ''}', '${escapeJsString(result.artist || '')}')">
                        Use This
                    </button>
                </div>
            </div>
        `;
    });
    html += '</div>';
    resultsDiv.innerHTML = html;
}

function populateAlbumFields(title, year, albumType, mbid, discogsId, coverArtUrl, genres, releaseGroupMbid, albumArtist) {
    const lookupEl = document.getElementById('albumLookupModal');
    if (lookupEl) {
        const lookupModal = bootstrap.Modal.getInstance(lookupEl);
        if (lookupModal) lookupModal.hide();
    }

    const musicBrainzEl = document.getElementById('musicBrainzModal');
    if (musicBrainzEl) {
        const mbModal = bootstrap.Modal.getInstance(musicBrainzEl);
        if (mbModal) mbModal.hide();
    }
    
    if (title) document.getElementById('album_title').value = title;
    if (albumArtist) document.getElementById('album_artist').value = albumArtist;
    if (year) document.getElementById('release_year').value = year;
    if (mbid) document.getElementById('album_mbid').value = mbid;
    if (coverArtUrl) document.getElementById('cover_art_url').value = coverArtUrl;
}

function escapeHtml(str) {
    return String(str || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function escapeJsString(str) {
    if (!str) return '';
    return str.replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/"/g, '\\"');
}

// Init Form Submission safety
document.addEventListener('DOMContentLoaded', function () {
    const albumForm = document.getElementById('albumMetadataForm');
    if (albumForm) {
        albumForm.addEventListener('submit', function() {
            const saveBtn = albumForm.querySelector('button[type="submit"]');
            if (saveBtn) {
                saveBtn.disabled = true;
                saveBtn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status"></span> Saving...';
            }
        });
    }
});
