// ===== ALBUM DETAIL PAGE JAVASCRIPT =====

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

// "Lookup on MusicBrainz"
function openAlbumLookupModal() {
    const artist = _pageData.artistName || '';
    const album = _pageData.albumName || '';

    window._mbSearchCallback = function (selected) {
        window._mbSearchCallback = null;
        if (!selected) return;
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
        } else if (rgMbid && concreteReleases.length === 1 && typeof populateAlbumFields === 'function') {
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
    if (typeof window.populateMusicBrainzSearch === 'function') {
        window.populateMusicBrainzSearch(artist, album, '', '');
    }
    if (typeof window.showMusicBrainzModal === 'function') {
        window.showMusicBrainzModal();
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
    if (!resultsDiv) return;
    resultsDiv.innerHTML = '<div class="spinner-border spinner-border-sm" role="status"><span class="visually-hidden">Loading...</span></div> Searching MusicBrainz...';

    fetch('/api/album/musicbrainz', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ album: album, artist: artist, existing_mbid: (document.getElementById('album_mbid') || {}).value || null })
    })
    .then(response => {
        if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
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
        if (source === 'musicbrainz') {
            const album = _pageData.albumName;
            const artist = _pageData.artistName;
            resultsDiv.innerHTML = `
                <div class="alert alert-warning">
                    <p><strong>No MusicBrainz matches found for "${escapeHtml(album)}" by ${escapeHtml(artist)}</strong></p>
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
                <div class="list-group-item list-group-item-action bg-dark text-light border-secondary" style="cursor: pointer; padding: 1rem; ${itemStyle}">
                    <div class="d-flex gap-3">
                        ${result.cover_art_url ? `
                            <img src="${result.cover_art_url}" alt="${escapeHtml(result.title)}" style="width: 80px; height: 80px; object-fit: cover; border-radius: 4px;" onerror="this.style.display='none'">
                        ` : ''}
                        <div class="flex-grow-1">
                            <div class="d-flex justify-content-between align-items-start mb-2">
                                <div>
                                    <h6 class="mb-1">${storedBadge}${escapeHtml(result.title)}</h6>
                                    <small class="text-muted">by ${escapeHtml(result.artist)}</small>
                                </div>
                                <span class="badge bg-${confidenceClass}">${confidencePercent}% match</span>
                            </div>
                            <div class="small text-muted mb-2">
                                <span class="badge bg-info me-1">${escapeHtml(typeLabel)}</span>
                                ${year ? `<span class="badge bg-secondary">${escapeHtml(year)}</span>` : ''}
                            </div>
                            <div class="d-flex gap-2 flex-wrap">
                                <button class="btn btn-sm btn-primary" onclick="populateAlbumFields('${escapeJsString(result.title)}', '${year}', '${combinedType}', '${result.mbid}', '', '${escapeJsString(result.cover_art_url || '')}', '')">
                                    <i class="bi bi-check-circle"></i> Use This Album
                                </button>
                            </div>
                        </div>
                    </div>
                </div>
            `;
        });
    }
    html += '</div>';
    resultsDiv.innerHTML = html;
}

function populateAlbumFields(title, year, albumType, mbid, discogsId, coverArtUrl, genres, releaseGroupMbid, albumArtist) {
    const lookupEl = document.getElementById('albumLookupModal');
    if (lookupEl) {
        const lookupModal = bootstrap.Modal.getInstance(lookupEl);
        if (lookupModal) lookupModal.hide();
    }
    
    if (title) {
        const titleEl = document.getElementById('album_title');
        if (titleEl) titleEl.value = title;
    }
    if (albumArtist) {
        const albumArtistEl = document.getElementById('album_artist');
        if (albumArtistEl) albumArtistEl.value = albumArtist;
    }
    if (year) {
        const yearEl = document.getElementById('release_year');
        if (yearEl) yearEl.value = year;
    }
    if (mbid) {
        const mbidEl = document.getElementById('album_mbid');
        if (mbidEl) mbidEl.value = mbid;
    }
    if (coverArtUrl) {
        const coverEl = document.getElementById('cover_art_url');
        if (coverEl) coverEl.value = coverArtUrl;
    }
    
    const trackTable = document.querySelector('.table-responsive');
    if (trackTable) {
        trackTable.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
}

function escapeHtml(str) {
    return String(str || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function escapeJsString(str) {
    if (!str) return '';
    return str.replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/"/g, '\\"');
}

// Init mobile tabs and components safely on load
document.addEventListener('DOMContentLoaded', function () {
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
});
