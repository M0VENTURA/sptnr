"""
services/popularity/scanner.py

Backward-compatible entry point for popularity scanning.

The canonical orchestration lives in ``services.popularity.scan_stage_runner``
(``run_scan``), which the WebUI/scheduler pipeline invokes.  This module is a
thin compatibility wrapper so legacy imports of ``popularity_scan`` keep
working and the pipeline's ``_resolve_scanner_callable`` fallback (which looks
for ``popularity_scan`` when ``run_scan`` is absent) resolves to a valid callable.
"""

from __future__ import annotations

from typing import Any


def popularity_scan(*args: Any, **kwargs: Any):
    """Backward-compatible popularity-scan entry point.

    Delegates to the staged runner. Accepts and forwards arbitrary keyword
    arguments (artist_filter, album_filter, force, singles_only, metadata_only,
    popularity_only, resume_from, progress_file, ...).
    """
    from services.popularity.scan_stage_runner import run_scan

    return run_scan(*args, **kwargs)