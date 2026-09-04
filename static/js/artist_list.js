// ===== ARTIST LIST PAGE JAVASCRIPT =====

// Open the Windows Phone style Metro jump grid overlay
function openJumpPicker() {
    const overlay = document.getElementById('metroJumpOverlay');
    if (overlay) overlay.classList.remove('d-none');
}

// Close the jump grid overlay when clicking backdrop or close button
function closeJumpPicker(event) {
    if (event && event.target !== event.currentTarget && !event.target.closest('.btn-close')) return;
    const overlay = document.getElementById('metroJumpOverlay');
    if (overlay) overlay.classList.add('d-none');
}

// Jump smoothly to the selected letter section
function jumpToLetter(letter) {
    closeJumpPicker();
    const sectionId = letter === '#' ? 'section-num' : 'section-' + letter;
    const targetEl = document.getElementById(sectionId);
    if (targetEl) {
        targetEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
}

// ===== Scan from letter =====

async function scanLetterArtists(letter, scanMode) {
    var fullScan = scanMode === 'forced';
    var scanModeValue = fullScan ? 'forced' : 'changes';
    var message = 'Start ' + (fullScan ? 'Full (Forced)' : 'Changes') + ' scan from letter "' + letter + '"?\n\nThis will resolve the first matching artist from your local library and scan from there.';

    if (!confirm(message)) {
        return;
    }

    try {
        var response = await fetch('/api/scan/from-artist', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                letter: letter,
                scan_mode: scanModeValue
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

// Bind Escape key to close the jump overlay
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        const overlay = document.getElementById('metroJumpOverlay');
        if (overlay && !overlay.classList.contains('d-none')) {
            overlay.classList.add('d-none');
        }
    }
});
