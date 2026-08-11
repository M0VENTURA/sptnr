// Artist List Page JS
// Extracted from templates/pages/artist_list.html

// ===== Windows Phone Metro Jump Picker =====

function openJumpPicker() {
    const overlay = document.getElementById('metroJumpOverlay');
    if (overlay) overlay.classList.remove('d-none');
}

function closeJumpPicker(event) {
    const overlay = document.getElementById('metroJumpOverlay');
    if (overlay) overlay.classList.add('d-none');
}

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
