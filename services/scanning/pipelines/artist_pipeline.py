"""Artist-level scan pipeline.

This wraps the existing artist pipeline implementation so routes can call a
stable service function without importing old helpers directly.
"""

from __future__ import annotations

import logging

from helpers.logging_config import log_unified


def run_artist_pipeline(artist_name: str, force: bool = False) -> None:
    """Run the complete scan pipeline for one artist.

    The preferred implementation is ``services.scanning.pipeline``. A legacy
    fallback is kept so the refactor can be introduced without breaking older
    deployments that still expose ``helpers.scan_tasks``.
    """
    try:
        from services.scanning.pipeline import run_artist_scan_pipeline
        run_artist_scan_pipeline(artist_name, force=force)
        return
    except Exception as exc:
        logging.debug("Primary artist pipeline unavailable, trying legacy fallback: %s", exc)

    try:
        from helpers.scan_tasks import run_artist_scan_pipeline
        run_artist_scan_pipeline(artist_name, force=force)
        return
    except Exception:
        log_unified(f"❌ Artist scan failed for {artist_name}")
        raise
