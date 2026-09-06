// ===== Album Detail Page JS =====

window.toggleAlbumFavourite = function(artist, album) {
    fetch('/api/album/favourite', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ artist: artist, album: album })
    })
    .then(r => r.json())
    .then(data => {
        const icon = document.getElementById('albumFavouriteIcon');
        if (icon && data.success) {
            if (data.is_favourite) {
                icon.classList.remove('bi-heart');
                icon.classList.add('bi-heart-fill');
            } else {
                icon.classList.remove('bi-heart-fill');
                icon.classList.add('bi-heart');
            }
        }
    })
    .catch(err => console.error('Error toggling album favourite:', err));
};

window.playAlbum = function() {
    const playBtns = document.querySelectorAll('.player-play-btn');
    if (playBtns.length === 0) {
        alert('No playable tracks found for this album.');
        return;
    }
    playBtns[0].click();
};

window.playTrackFromAlbum = function(btn) {
    const trackId = btn.getAttribute('data-track-id');
    const title = btn.getAttribute('data-title');
    const artist = btn.getAttribute('data-artist');
    const art = btn.getAttribute('data-art');
    if (typeof Player !== 'undefined' && typeof Player.playTrack === 'function') {
        Player.playTrack({ id: trackId, title: title, artist: artist, albumArtUrl: art });
    } else {
        alert(`Playing track: ${title}`);
    }
};

window.openEditTrackFromAlbum = function(trackId) {
    const modalEl = document.getElementById('trackEditModal');
    if (modalEl && typeof bootstrap !== 'undefined') {
        const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
        modal.show();
    } else {
        window.location.href = `/track/${trackId}/edit`;
    }
};

window.deleteTrack = function(trackId) {
    if (!confirm('Are you sure you want to delete this track?')) return;
    fetch(`/api/tracks/${trackId}`, { method: 'DELETE' })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                location.reload();
            } else {
                alert('Failed to delete track: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(err => alert('Network error: ' + err.message));
};

window.populateMajorityArtist = function() {
    const rows = document.querySelectorAll('#albumTracksTbody tr');
    let artists = [];
    rows.forEach(r => {
        const artist = r.getAttribute('data-track-artist');
        if (artist) artists.push(artist);
    });
    if (artists.length === 0) return;
    const counts = artists.reduce((acc, curr) => (acc[curr] = (acc[curr] || 0) + 1, acc), {});
    const majority = Object.keys(counts).reduce((a, b) => counts[a] > counts[b] ? a : b);
    const input = document.getElementById('track_artist');
    if (input) input.value = majority;
};

window.addAlbumGenre = function() {
    const input = document.getElementById('newAlbumGenreInput');
    const container = document.getElementById('albumGenresContainer');
    const hiddenInput = document.getElementById('album_genres');
    if (!input || !container || !hiddenInput) return;
    
    const genre = input.value.trim();
    if (!genre) return;
    
    const badge = document.createElement('span');
    badge.className = 'badge bg-primary me-1 mb-1';
    badge.innerHTML = `${genre} <button type="button" class="btn-close btn-close-white ms-1" style="font-size: 0.6rem;" onclick="stageRemoveAlbumGenre(this)"></button>`;
    container.appendChild(badge);
    
    input.value = '';
    updateHiddenGenres();
};

window.stageRemoveAlbumGenre = function(element) {
    if (typeof element === 'string') {
        const badges = document.querySelectorAll('#albumGenresContainer .badge');
        badges.forEach(b => {
            if (b.textContent.trim().startsWith(element)) b.remove();
        });
    } else if (element && element.closest) {
        element.closest('.badge').remove();
    }
    updateHiddenGenres();
};

function updateHiddenGenres() {
    const container = document.getElementById('albumGenresContainer');
    const hiddenInput = document.getElementById('album_genres');
    if (!container || !hiddenInput) return;
    const genres = Array.from(container.querySelectorAll('.badge')).map(b => b.textContent.replace('×', '').trim());
    hiddenInput.value = genres.join(', ');
}

window.goToAlbumGenres = function() {
    const btn = document.querySelector('#albumPageTabs [data-bs-target="#tab-genres"]');
    if (btn) bootstrap.Tab.getOrCreateInstance(btn).show();
};

window.fetchGenreRecommendations = function() {
    const btn = document.getElementById('fetchGenresBtn');
    const section = document.getElementById('recommendedGenresSection');
    const container = document.getElementById('recommendedGenres');
    if (!btn || !section || !container) return;
    
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Fetching...';
    
    const artist = window._pageData ? window._pageData.artistName : '';
    const album = window._pageData ? window._pageData.albumName : '';
    
    fetch(`/api/album/recommend-genres?artist=${encodeURIComponent(artist)}&album=${encodeURIComponent(album)}`)
        .then(r => r.json())
        .then(data => {
            btn.disabled = false;
            btn.innerHTML = '<i class="bi bi-cloud-download me-1"></i> Get Online Suggestions';
            if (data.success && data.genres) {
                section.style.display = 'block';
                container.innerHTML = data.genres.map(g => `<span class="badge bg-secondary" style="cursor:pointer;" onclick="addRecommendedGenre('${g}')">${g} +</span>`).join(' ');
            } else {
                alert('No recommendations found.');
            }
        })
        .catch(err => {
            btn.disabled = false;
            btn.innerHTML = '<i class="bi bi-cloud-download me-1"></i> Get Online Suggestions';
            alert('Network error: ' + err.message);
        });
};

window.addRecommendedGenre = function(genre) {
    const input = document.getElementById('newAlbumGenreInput');
    if (input) {
        input.value = genre;
        addAlbumGenre();
    }
};

window.applySelectedAlbumSourceTags = function() {
    const checks = document.querySelectorAll('.album-source-tag-check:checked');
    checks.forEach(c => {
        const input = document.getElementById('newAlbumGenreInput');
        if (input) {
            input.value = c.value;
            addAlbumGenre();
        }
        c.checked = false;
    });
};

document.addEventListener('change', function(e) {
    if (e.target.classList.contains('album-source-tag-check')) {
        const pane = e.target.closest('.tab-pane');
        if (pane) {
            const anyChecked = pane.querySelectorAll('.album-source-tag-check:checked').length > 0;
            const applyBtn = pane.querySelector('.album-apply-source-tags-btn');
            if (applyBtn) applyBtn.style.display = anyChecked ? 'inline-block' : 'none';
        }
    }
});

// ===== MusicBrainz Album Lookup Integrations =====

window.openAlbumLookupModal = function() {
    const artistName = window._pageData ? window._pageData.artistName : '';
    const albumName = window._pageData ? window._pageData.albumName : '';
    
    const modalEl = document.getElementById('musicBrainzModal');
    if (!modalEl) {
        alert('MusicBrainz search modal not found. Please ensure components/_musicbrainz_search_modal.html is included in your HTML.');
        return;
    }
    
    const searchArtist = document.getElementById('mbSearchArtist');
    const searchAlbum = document.getElementById('mbSearchAlbum');
    if (searchArtist) searchArtist.value = artistName;
    if (searchAlbum) searchAlbum.value = albumName;
    
    const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
    modal.show();
    
    if (typeof performMbSearch === 'function') {
        performMbSearch();
    } else {
        console.warn("performMbSearch is not defined. Ensure _musicbrainz_search_functions.html is included.");
    }
};

window.confirmReleaseSelection = function() {
    const selectedMbid = document.getElementById('mbSelectedReleaseId')?.value || window._selectedMbReleaseId;
    
    if (!selectedMbid) {
        alert('No release selected or MBID not found.');
        return;
    }
    
    const formMbid = document.getElementById('album_mbid');
    if (formMbid) {
        formMbid.value = selectedMbid;
        
        formMbid.style.transition = 'background-color 0.3s';
        formMbid.style.backgroundColor = '#198754';
        setTimeout(() => formMbid.style.backgroundColor = '', 500);
    }
    
    const modalEl = document.getElementById('musicBrainzModal');
    if (modalEl) {
        bootstrap.Modal.getInstance(modalEl).hide();
    }
    
    alert('MusicBrainz ID applied! Click "Save Metadata" to persist changes.');
};
