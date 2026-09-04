// ===== ALBUM DETAIL PAGE JAVASCRIPT =====

var _pageData = window._pageData || {};

let currentEditTrackId = null;
let currentEditTrackTitle = null;

if (typeof addGenreModalInstance === 'undefined') {
    var addGenreModalInstance = null;
}

let currentAlbumGenres = new Set();

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

    const rows = document.querySelectorAll(
        '#albumTracksTbody tr'
    );

    const tracks = [];

    rows.forEach(row => {

        const trackId =
            row.dataset.trackId;

        if (!trackId) {
            return;
        }

        tracks.push({
            id: trackId,
            title:
                row.dataset.trackTitle || '',
            artist:
                row.dataset.trackArtist || '',
            albumArtUrl:
                document.getElementById('albumArtImage')?.src || ''
        });

    });

    if (!tracks.length) {

        alert(
            'No tracks available on this album.'
        );

        return;
    }

    Player.playQueue(tracks);
}

// ─────────────────────────────────────────────────────────────
// MusicBrainz Album Lookup
// ─────────────────────────────────────────────────────────────

function openAlbumLookupModal() {

    const artist =
        _pageData.artistName || '';

    const album =
        _pageData.albumName || '';

    window._mbSearchCallback =
        function (selected) {

            window._mbSearchCallback =
                null;

            if (!selected) {
                return;
            }

            const group =
                selected.release || {};

            const rgMbid =
                group.id || selected.id;

            const releaseMbid =
                (
                    selected.id &&
                    selected.id !== rgMbid
                )
                    ? selected.id
                    : '';

            const year =
                (
                    group.first_release_date || ''
                )
                .toString()
                .split('-')[0] || '';

            const albumType =
                _buildAlbumType(
                    group.primary_type,
                    group.secondary_types
                );

            const cover =
                group.cover_art_url || '';

            const albumArtist =
                selected.artist ||
                group.artist ||
                '';

            const concreteReleases =
                Array.isArray(group.releases)
                    ? group.releases
                    : [];

            if (releaseMbid) {

                populateAlbumFields(
                    selected.title ||
                    group.title ||
                    album,
                    year,
                    albumType,
                    releaseMbid,
                    '',
                    cover,
                    '',
                    rgMbid,
                    albumArtist
                );

            } else if (
                rgMbid &&
                concreteReleases.length === 1
            ) {

                const single =
                    concreteReleases[0];

                populateAlbumFields(
                    single.title ||
                    selected.title ||
                    group.title ||
                    album,
                    String(
                        single.date || ''
                    ).slice(0, 4) ||
                    year,
                    albumType,
                    single.id,
                    '',
                    single.cover_art_url ||
                        cover,
                    '',
                    rgMbid,
                    albumArtist
                );

            } else if (
                rgMbid &&
                typeof openReleasePickerModal ===
                    'function'
            ) {

                openReleasePickerModal(
                    rgMbid,
                    selected.title ||
                        group.title ||
                        album,
                    year,
                    albumType,
                    concreteReleases.length > 1
                        ? concreteReleases
                        : null,
                    null,
                    cover,
                    albumArtist
                );

            } else {

                populateAlbumFields(
                    selected.title ||
                        group.title ||
                        album,
                    year,
                    albumType,
                    releaseMbid,
                    '',
                    cover,
                    '',
                    rgMbid,
                    albumArtist
                );
            }
        };

    window._mbSearchIncludeOwned =
        true;

    window._mbSearchWithReleases =
        true;

    if (
        typeof
            window.populateMusicBrainzSearch ===
        'function'
    ) {

        window.populateMusicBrainzSearch(
            artist,
            album,
            '',
            ''
        );
    }

    if (
        typeof window.showMusicBrainzModal ===
        'function'
    ) {

        window.showMusicBrainzModal();
    }
}

function runAlbumLookup() {

    const artist =
        (
            document.getElementById(
                'albumLookupArtist'
            )?.value || ''
        )
            .trim() ||
        _pageData.artistName;

    const album =
        (
            document.getElementById(
                'albumLookupAlbum'
            )?.value || ''
        )
            .trim() ||
        _pageData.albumName;

    if (!artist || !album) {

        alert(
            'Please enter both artist and album names.'
        );

        return;
    }

    const resultsDiv =
        document.getElementById(
            'albumLookupResults'
        );

    if (!resultsDiv) {
        return;
    }

    resultsDiv.innerHTML =
        '<div class="spinner-border spinner-border-sm"></div> Searching MusicBrainz...';

    fetch('/api/album/musicbrainz', {
        method: 'POST',
        headers: {
            'Content-Type':
                'application/json'
        },
        body: JSON.stringify({
            album: album,
            artist: artist,
            existing_mbid:
                document.getElementById(
                    'album_mbid'
                )?.value || null
        })
    })
        .then(response => {
            if (!response.ok) {
                throw new Error(
                    'HTTP error ' +
                        response.status
                );
            }
            return response.json();
        })
        .then(data => {

            if (data.error) {

                resultsDiv.innerHTML =
                    '<div class="alert alert-danger">' +
                    escapeHtml(data.error) +
                    '</div>';

                return;
            }

            displayAlbumResults(
                data.results,
                'musicbrainz'
            );
        })
        .catch(error => {

            resultsDiv.innerHTML =
                '<div class="alert alert-danger">' +
                escapeHtml(
                    error.message
                ) +
                '</div>';
        });
}

// ─────────────────────────────────────────────────────────────
// Utility Functions
// ─────────────────────────────────────────────────────────────

function _buildAlbumType(
    primaryType,
    secondaryTypes
) {

    const primary =
        (primaryType || 'album')
            .toLowerCase()
            .trim();

    const secondary =
        (secondaryTypes || []).map(
            s =>
                s
                    .toLowerCase()
                    .trim()
        );

    const displayable =
        secondary
