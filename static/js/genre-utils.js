/**
 * Genre Management Utilities
 * Shared functions for genre removal, Navidrome scan monitoring, and UI feedback
 */

// Monitor Navidrome scan progress
function monitorNavidromeScan() {
    updateScanProgress('Starting Navidrome scan...');

    let scanAttempts = 0;
    const maxAttempts = 120; // 2 minutes with 1s polling

    const pollInterval = setInterval(() => {
        scanAttempts++;

        if (scanAttempts > maxAttempts) {
            clearInterval(pollInterval);
            updateScanProgress('⏱️ Scan timed out. Please check Navidrome for progress.');
            setTimeout(closeScanProgressModal, 5000);
            return;
        }

        fetch('/api/navidrome/scan/status')
            .then(r => r.json())
            .then(data => {
                if (!data.success) {
                    clearInterval(pollInterval);
                    updateScanProgress('❌ Could not connect to Navidrome');
                    setTimeout(closeScanProgressModal, 3000);
                    return;
                }

                if (data.scanning) {
                    updateScanProgress(`Scanning Navidrome library... ${data.count || '0'} items processed`);
                } else {
                    // Scan completed
                    clearInterval(pollInterval);
                    updateScanProgress('✅ Navidrome scan completed! Genres are now updated.');
                    setTimeout(() => {
                        closeScanProgressModal();
                        location.reload();
                    }, 3000);
                }
            })
            .catch(error => {
                // Navidrome may not be configured or accessible
                if (scanAttempts > 5) {
                    clearInterval(pollInterval);
                    updateScanProgress('⚠️ Could not verify scan status. Genres have been updated.');
                    setTimeout(() => {
                        closeScanProgressModal();
                        location.reload();
                    }, 3000);
                }
            });
    }, 1000);
}

// Modal management for scan progress
function showScanProgressModal() {
    const modalHtml = `
        <div class="modal fade" id="scanProgressModal" tabindex="-1" data-bs-backdrop="static" data-bs-keyboard="false">
            <div class="modal-dialog modal-dialog-centered">
                <div class="modal-content" style="background-color: #1a1a1a; border-color: #333;">
                    <div class="modal-header border-secondary">
                        <h5 class="modal-title">
                            <i class="bi bi-hourglass-split"></i> Updating Genres
                        </h5>
                    </div>
                    <div class="modal-body text-center py-4">
                        <div class="spinner-border text-info mb-3" role="status">
                            <span class="visually-hidden">Loading...</span>
                        </div>
                        <p id="scanProgressText" class="text-muted mb-0">Updating tracks...</p>
                    </div>
                </div>
            </div>
        </div>
    `;

    // Remove existing modal if present
    const existingModal = document.getElementById('scanProgressModal');
    if (existingModal) existingModal.remove();

    // Add new modal to DOM
    document.body.insertAdjacentHTML('beforeend', modalHtml);

    // Show modal
    const modal = new bootstrap.Modal(document.getElementById('scanProgressModal'), {
        backdrop: 'static',
        keyboard: false
    });
    modal.show();
}

function updateScanProgress(message) {
    const progressText = document.getElementById('scanProgressText');
    if (progressText) {
        progressText.textContent = message;
    }
}

function closeScanProgressModal() {
    const modal = document.getElementById('scanProgressModal');
    if (modal) {
        const bsModal = bootstrap.Modal.getInstance(modal);
        if (bsModal) bsModal.hide();
        modal.remove();
    }
}

/**
 * Call the genre removal API for selected genres
 * @param {string} artistName - Artist name
 * @param {string|null} albumName - Album name (optional, null for artist-level removal)
 * @param {string[]} genres - Array of genres to remove
 * @returns {Promise<Object>} API response
 */
function callGenreRemovalAPI(artistName, albumName, genres) {
    const payload = {
        artist_name: artistName,
        genres: genres
    };

    if (albumName) {
        payload.album_name = albumName;
    }

    return fetch('/api/genres/remove', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    }).then(r => r.json());
}

/**
 * Generic genre removal handler with scan monitoring
 * @param {string} artistName - Artist name
 * @param {string|null} albumName - Album name (optional)
 * @param {string[]} genres - Genres to remove
 * @param {string} contextType - 'artist' or 'album' for context
 */
function handleGenreRemoval(artistName, albumName, genres, contextType) {
    const genreList = genres.join(', ');
    const contextMsg = albumName 
        ? `the album "${albumName}"`
        : `all tracks by "${artistName}"`;
    
    if (!confirm(`Remove "${genreList}" from ${contextMsg}?\n\nThis will update MP3/FLAC files and trigger a Navidrome scan.`)) {
        return;
    }

    showScanProgressModal();

    callGenreRemovalAPI(artistName, albumName, genres)
        .then(data => {
            if (data.success) {
                const trackWord = data.affected_tracks === 1 ? 'track' : 'tracks';
                updateScanProgress(`Removed "${genreList}" from ${data.affected_tracks} ${trackWord}!`);

                if (data.scan_triggered) {
                    monitorNavidromeScan();
                } else {
                    setTimeout(() => {
                        closeScanProgressModal();
                        location.reload();
                    }, 2000);
                }
            } else {
                updateScanProgress(`❌ Error: ${data.error}`);
                setTimeout(closeScanProgressModal, 3000);
            }
        })
        .catch(error => {
            updateScanProgress(`❌ Network error: ${error.message}`);
            setTimeout(closeScanProgressModal, 3000);
        });
}

/**
 * Toggle checkbox for batch genre selection
 * @param {string} containerId - ID of container with checkboxes
 * @param {string} buttonId - ID of the remove button
 */
function toggleGenreCheckbox(containerId, buttonId) {
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
}

/**
 * Get selected genres from checkboxes
 * @param {string} containerId - ID of container with checkboxes
 * @returns {string[]} Array of selected genre names
 */
function getSelectedGenres(containerId) {
    const container = document.getElementById(containerId);
    if (!container) return [];

    return Array.from(container.querySelectorAll('input[type="checkbox"]:checked'))
        .map(cb => cb.value);
}

// Remove selected genres from artist
function removeSelectedArtistGenres() {
    const selectedGenres = getSelectedGenres('currentArtistGenres');
    if (selectedGenres.length === 0) return;
    
    if (!confirm(`Remove ${selectedGenres.length} genre(s)?`)) return;
    
    const artistName = document.querySelector('[data-artist-name]')?.dataset.artistName || '';
    const btn = document.getElementById('removeSelectedArtistGenresBtn');
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
    
    handleGenreRemoval(artistName, null, selectedGenres, 'artist');
}

// Apply selected genres (for artist genre management)
function applySelectedArtistGenres() {
    const selectedGenres = getSelectedGenres('recommendedGenres');
    if (selectedGenres.length === 0) {
        alert('Please select at least one genre');
        return;
    }
    
    const artistName = document.querySelector('[data-artist-name]')?.dataset.artistName || '';
    const btn = document.getElementById('applyArtistGenresBtn');
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
    
    fetch('/api/genres/apply', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            artist_name: artistName,
            genres: selectedGenres
        })
    })
    .then(r => r.json())
    .then(data => {
        if (data.error) {
            alert('❌ Error: ' + data.error);
        } else {
            alert(`✅ Applied ${selectedGenres.length} genre(s)`);
            setTimeout(() => location.reload(), 1000);
        }
    })
    .catch(err => {
        alert('❌ Error: ' + err.message);
    })
    .finally(() => {
        btn.disabled = false;
        btn.textContent = originalText;
    });
}

// Remove individual album genre
function removeAlbumGenre(genre, artistName, albumName) {
    if (!artistName || !albumName) {
        artistName = document.querySelector('[data-artist-name]')?.dataset.artistName || '';
        albumName = document.querySelector('[data-album-name]')?.dataset.albumName || '';
    }
    
    if (!artistName || !albumName) {
        alert('❌ Error: Could not determine artist or album');
        return;
    }
    
    handleGenreRemoval(artistName, albumName, [genre], 'album');
}

// Remove genre from a specific track
function removeTrackGenre(trackId, genre, element) {
    if (!confirm(`Remove "${genre}" from this track?`)) return;

    fetch(`/api/track/${trackId}`)
    .then(r => r.json())
    .then(trackData => {
        let currentGenres = [];
        
        if (trackData.genre) {
            currentGenres = trackData.genre
                .split(/[;,\\\/]/g)
                .map(g => g.trim())
                .filter(g => g && g !== genre);
        }
        
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
            if (element) {
                element.style.opacity = '0.5';
                setTimeout(() => {
                    element.remove();
                }, 300);
            }
            alert(`✅ Genre removed${data.file_synced ? ' and MP3 updated' : ''}`);
        } else {
            alert('❌ Error: ' + (data.error || 'Failed to remove genre'));
        }
    })
    .catch(err => {
        alert('❌ Network error: ' + err.message);
    });
}

// Edit track artist name
function editTrackArtist(trackId, currentArtist) {
    const isAlbumPage = document.getElementById('editTrackModal') !== null;
    
    if (isAlbumPage) {
        const modal = new bootstrap.Modal(document.getElementById('editTrackModal'));
        document.getElementById('editTrackId').value = trackId;
        document.getElementById('editTrackCurrentField').value = 'artist';
        document.getElementById('editTrackLabel').textContent = 'Track Artist';
        const displayValue = currentArtist && currentArtist !== '—' ? currentArtist : '';
        document.getElementById('editTrackValue').value = displayValue;
        document.getElementById('editTrackValue').focus();
        modal.show();
    } else {
        const newArtist = prompt('Enter new artist name:', currentArtist || '');
        if (!newArtist) return;
        
        fetch('/api/track/update-metadata', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                track_id: trackId,
                artist: newArtist
            })
        })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                alert('✅ Track artist updated');
                location.reload();
            } else {
                alert('❌ Error: ' + (data.error || 'Failed to update'));
            }
        })
        .catch(err => {
            alert('❌ Network error: ' + err.message);
        });
    }
}

// Fetch artist genre recommendations
function fetchArtistGenreRecommendations() {
    const artistName = document.querySelector('[data-artist-name]')?.dataset.artistName || '';
    if (!artistName) {
        alert('Error: Could not determine artist name');
        return;
    }
    
    const btn = document.getElementById('fetchArtistGenresBtn');
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
    
    fetch(`/api/genres/recommendations?artist=${encodeURIComponent(artistName)}`)
    .then(r => r.json())
    .then(data => {
        const recommendedContainer = document.getElementById('recommendedGenres');
        
        if (data.error) {
            recommendedContainer.innerHTML = `<p class="text-danger">${escapeHtml(data.error)}</p>`;
            btn.disabled = false;
            btn.textContent = originalText;
            return;
        }
        
        const genres = data.genres || [];
        if (genres.length === 0) {
            recommendedContainer.innerHTML = '<p class="text-muted">No recommendations found</p>';
            btn.disabled = false;
            btn.textContent = originalText;
            return;
        }
        
        let html = '';
        genres.forEach(genre => {
            const safeId = escapeHtml(genre).replace(/\s+/g, '_').replace(/[^a-zA-Z0-9_-]/g, '');
            html += `
                <div class="form-check">
                    <input class="form-check-input" type="checkbox" value="${escapeHtml(genre)}" id="genre-${safeId}">
                    <label class="form-check-label" for="genre-${safeId}">
                        ${escapeHtml(genre)}
                    </label>
                </div>
            `;
        });
        
        recommendedContainer.innerHTML = html;
        document.getElementById('applyArtistGenresBtn').style.display = 'inline-block';
        
        btn.disabled = false;
        btn.textContent = originalText;
    })
    .catch(err => {
        document.getElementById('recommendedGenres').innerHTML = `<p class="text-danger">Error: ${err.message}</p>`;
        btn.disabled = false;
        btn.textContent = originalText;
    });
}

// Escape HTML special chars
function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}
