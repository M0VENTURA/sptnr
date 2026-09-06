// Replace the entire "Fixed Filtering Logic" section at the bottom of artist_detail.js with this:

window.setArtistFilter = function(filter) {
    document.querySelectorAll('.artist-filter-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.filter === filter);
    });
    
    const mainContainer = document.getElementById('artistMainPageContainer');
    if (!mainContainer) return;

    // Reset container classes
    mainContainer.classList.remove('filter-hide-missing', 'filter-hide-library');

    if (filter === 'library') {
        mainContainer.classList.add('filter-hide-missing');
    } else if (filter === 'missing') {
        mainContainer.classList.add('filter-hide-library');
    }

    // Hide empty category sections based on active filter
    document.querySelectorAll('.category-section').forEach(section => {
        const hasMissing = section.querySelector('.missing-album-item') !== null;
        const hasLibrary = section.querySelector('.library-album-item') !== null;

        let isVisible = true;
        if (filter === 'library' && !hasLibrary) isVisible = false;
        if (filter === 'missing' && !hasMissing) isVisible = false;

        section.style.display = isVisible ? 'block' : 'none';
    });
};

window.toggleMissingReleasesForCategory = function(btn, catId) {
    const section = document.getElementById(`${catId}-section`);
    if (!section) return;
    
    const isHidden = btn.getAttribute('data-hidden') === 'true';

    if (isHidden) {
        section.classList.remove('category-hide-missing');
        btn.setAttribute('data-hidden', 'false');
        btn.innerHTML = '<i class="bi bi-eye-slash me-1"></i><span>Hide Missing</span>';
    } else {
        section.classList.add('category-hide-missing');
        btn.setAttribute('data-hidden', 'true');
        btn.innerHTML = '<i class="bi bi-eye me-1"></i><span>Show Missing</span>';
    }
};
