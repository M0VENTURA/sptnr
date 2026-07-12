"""
Text utility functions for artist name normalization and file path resolution.

These helpers were migrated from the legacy monolithic codebase into
dedicated functions so they can be imported by ``navidrome_import.py``,
``scan_repository.py``, and other scanning modules without circular
dependency issues.
"""

from __future__ import annotations

import logging
import os
import re

from helpers.normalization_service import clean_artist_name_for_storage

logger = logging.getLogger(__name__)

# Re-export for callers that expect the underscore-prefixed name.
_clean_artist_name_for_storage = clean_artist_name_for_storage


def _normalize_artist_key(value: str) -> str:
    """Normalize an artist name to a stable key for grouping / deduplication.

    Strips whitespace, lower-cases, removes common noise like "the " and
    trailing punctuation, and collapses internal whitespace.  The result is
    suitable as a dictionary key or for ``GROUP BY``-style comparisons.

    Examples::

        "The Beatles"      → "beatles"
        "Pink Floyd "      → "pink floyd"
        "AC/DC"            → "acdc"
        "Steely Dan!"      → "steely dan"
        "   David Bowie  " → "david bowie"
    """
    if not value:
        return ""
    text = str(value).strip().lower()
    text = re.sub(r"\bthe\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.strip()


def _resolve_navidrome_file_path_for_storage(
    file_path: str | None,
    music_folder: str | None = None,
) -> str:
    """Resolve a Navidrome-style file path into a stable storage path.

    Navidrome sometimes returns file paths relative to its music folder
    (e.g. ``Pink Floyd/The Dark Side of the Moon/01 - Speak to Me.flac``).
    When the file doesn't exist at the given path and a *music_folder* is
    known, this function joins them and returns the absolute form.

    Falls back to returning the original path unchanged when the file
    already exists at the given path or when no *music_folder* is available.

    Returns the resolved path string (may still not exist on disk — that
    is the caller's responsibility to check).
    """
    path = str(file_path or "").strip()
    if not path:
        return ""

    # If the path is already absolute and the file exists, use it as-is.
    if os.path.isabs(path) and os.path.exists(path):
        return path

    # If a music folder is configured, join it with the relative path.
    root = (music_folder or os.environ.get("MUSIC_ROOT") or os.environ.get("MUSIC_FOLDER") or "").strip()
    if root and not os.path.isabs(path):
        candidate = os.path.normpath(os.path.join(root, path.lstrip("/")))
        return candidate

    return path
