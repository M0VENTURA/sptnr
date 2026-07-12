"""Top-level scanning entry point.

Main orchestration loop for library scanning. Iterates through all artists,
handles resume capability via checkpoint progress tracking, and delegates
actual scan work to the artist-level scanner.

Key Functions:
    - run_scan(): Full library scan with optional artist filter, resume
      capability, and force mode.

Architecture:
    Replaces the main loop previously embedded in ``popularity_scan()``.
    Scan progress is saved after each artist for resume support.
    Delegates per-artist work to ``services.scanning.artist_scanner``.
"""


import logging
from services.scanning.artist_scanner import scan_artist
from services.scanning.scan_state import get_resume_artist, save_progress
from db.repositories.library import get_all_artists

logger = logging.getLogger(__name__)


def run_scan(artist_filter=None, resume=True, force=False):
    """
    Run a full scan of the music library.

    Args:
        artist_filter (str | None):
            If provided, only this artist will be scanned.
        resume (bool):
            If True, resumes from last saved artist using scan_state.
        force (bool):
            If True, bypasses skip logic and forces re-processing.

    Flow:
        1. Determine resume point
        2. Fetch all artists
        3. Iterate artist-by-artist
        4. Delegate work to scan_artist()
        5. Persist progress after each artist

    Notes:
        - This function intentionally DOES NOT perform:
            - metadata lookups
            - scoring logic
            - DB updates beyond progress tracking
    """

    # Determine resume point
    resume_artist = get_resume_artist() if resume else None

    # Fetch all artists (DB layer responsibility)
    artists = get_all_artists()

    for artist in artists:

        # Resume logic – skip artists already processed
        if resume_artist and artist < resume_artist:
            continue

        # Optional targeted scan
        if artist_filter and artist != artist_filter:
            continue

        logger.info("Scanning artist: %s", artist)

        try:
            # Delegate actual scanning to artist-level service
            scan_artist(artist, force=force)

            # Persist checkpoint after successful artist scan
            save_progress(artist)

        except Exception as e:
            logger.error("Error scanning artist %s: %s", artist, e, exc_info=True)

    logger.info("[SCANNER] Full library scan completed")