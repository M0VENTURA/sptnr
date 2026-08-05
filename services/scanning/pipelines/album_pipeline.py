"""Album-level scan pipeline.

Extracted from the large routes/scans.py file. This keeps the route handler
small while preserving the original workflow:

1. Resolve the Navidrome artist ID where possible.
2. Import Navidrome metadata for the album.
3. Run metadata enrichment.
4. Run popularity scoring.
5. Trigger album type detection when available.
"""

from __future__ import annotations

import logging
import threading

from db.repositories.scan_repository import lookup_artist_id, lookup_track_artist_count
from helpers.logging_config import log_unified
from services.scanning.navidrome_import import scan_artist_to_db

# In-process guard: prevents the same album pipeline from running twice
# concurrently (double form submits, dashboard + album page triggers). Two
# overlapping runs double the Last.fm/ListenBrainz API load per track, which
# triggers rate limits and yields inconsistent listener data.
_pipeline_lock = threading.Lock()
_running_albums: set[tuple[str, str]] = set()


def _try_claim(artist_name: str, album_name: str) -> bool:
    """Claim the album for this pipeline run. False if already running."""
    global _running_albums
    key = (artist_name.strip().lower(), album_name.strip().lower())
    with _pipeline_lock:
        if key in _running_albums:
            return False
        _running_albums.add(key)
        return True


def _release(artist_name: str, album_name: str) -> None:
    global _running_albums
    key = (artist_name.strip().lower(), album_name.strip().lower())
    with _pipeline_lock:
        _running_albums.discard(key)


def _maybe_auto_detect_album_type(artist_name: str, album_name: str) -> None:
    """Run album type detection if the legacy helper is available."""
    try:
        from album_type_detector import auto_detect_album_type
        auto_detect_album_type(artist_name, album_name)
    except Exception as exc:
        logging.debug("Album type detection skipped for %s - %s: %s", artist_name, album_name, exc)


def run_album_pipeline(artist_name: str, album_name: str, force: bool = False) -> None:
    """Run the complete scan pipeline for one album."""
    album_display = f"{artist_name} - {album_name}"

    if not _try_claim(artist_name, album_name):
        log_unified(f"⏭️ Album scan already running for: {album_display} — skipping duplicate trigger")
        return

    try:
        log_unified(f"💿 Album scan pipeline started for: {album_display}")

        artist_id = lookup_artist_id(artist_name)

        if not artist_id:
            try:
                # Rebuild artist index using NavidromeClient directly
                from api_clients.navidrome import NavidromeClient
                from helpers.config_helpers import get_navidrome_config
                
                nav_config = get_navidrome_config()
                if nav_config:
                    nav_client = NavidromeClient(
                        base_url=nav_config.get("base_url"),
                        username=nav_config.get("user"),
                        password=nav_config.get("pass"),
                    )
                    artist_index = nav_client.build_artist_index()
                    artist_data = (artist_index or {}).get(artist_name, {})
                    artist_id = artist_data.get("id") if isinstance(artist_data, dict) else None
            except Exception as exc:
                logging.debug("Could not rebuild artist index for %s: %s", artist_name, exc)

        if artist_id:
            log_unified(f"Step 1/3: Navidrome import for album '{album_display}'")
            scan_artist_to_db(
                artist_name,
                artist_id,
                verbose=True,
                force=force,
                album_filter=album_name,
            )
        else:
            track_count = lookup_track_artist_count(artist_name)
            if track_count == 0:
                log_unified(f"❌ Scan aborted: no tracks found for '{album_display}'")
                return
            log_unified(f"Navidrome import skipped for '{album_display}' because only local track rows were found")

        from services.popularity.pipeline import run_popularity_scan as popularity_scan

        log_unified(f"Step 2/3: Metadata enrichment for album '{album_display}'")
        popularity_scan(
            verbose=True,
            force=force,
            artist_filter=artist_name,
            album_filter=album_name,
            metadata_only=True,
            progress_file="popularity_scan",
        )

        # Popularity scoring for THIS album only (album-filtered), so a
        # single-album scan does not walk the artist's entire catalogue.
        # Artist-wide scans are still available from the dashboard.
        log_unified(f"Step 2b/3: Popularity scan for album '{album_display}'")
        popularity_scan(
            verbose=True,
            force=force,
            artist_filter=artist_name,
            album_filter=album_name,
            progress_file="popularity_scan",
        )

        log_unified(f"Step 3/3: Auto-detecting album type for '{album_display}'")
        _maybe_auto_detect_album_type(artist_name, album_name)

        log_unified(f"✅ Scan complete for album '{album_display}'")

    except Exception as exc:
        log_unified(f"❌ Album scan failed for {album_display}: {exc}")
        logging.error("Album pipeline failed for %s", album_display, exc_info=True)
        raise
    finally:
        _release(artist_name, album_name)
