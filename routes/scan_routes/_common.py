"""Shared helpers for scan route modules.

Keep Flask route files thin: parse request data, call services, and return a
response. Reusable helpers for booleans, redirects, and background thread
launching live here.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from quart import redirect, url_for


TRUE_VALUES = {"1", "true", "yes", "on"}


def form_bool(value: Any) -> bool:
    """Return True for common HTML checkbox/query string truthy values."""
    return str(value or "").strip().lower() in TRUE_VALUES


def run_async(target: Callable, *args, daemon: bool = True, **kwargs) -> threading.Thread:
    """Run a callable in a background thread and return the thread object.

    Routes use this to stay non-blocking. Long-running scan code belongs in
    ``services.scanning`` modules, not in the route functions.
    """
    thread = threading.Thread(target=target, args=args, kwargs=kwargs, daemon=daemon)
    thread.start()
    return thread


def is_process_alive(process_ref: Any) -> bool:
    """Handle the project’s mixed process/thread/dict scan references."""
    if process_ref is None:
        return False

    if isinstance(process_ref, dict):
        process_ref = process_ref.get("thread")

    if process_ref is None:
        return False

    if hasattr(process_ref, "is_alive"):
        return bool(process_ref.is_alive())

    if hasattr(process_ref, "poll"):
        return process_ref.poll() is None

    return False


def redirect_for_artist(artist: str):
    """Redirect to an artist page, falling back to the dashboard."""
    if artist:
        return redirect(url_for("ui.artist_detail", name=artist))
    return redirect(url_for("ui.dashboard"))


def redirect_for_album(artist: str, album: str):
    """Redirect to an album page, falling back to the artist/dashboard page."""
    if artist and album:
        return redirect(url_for("ui.album_detail", artist=artist, album=album))
    return redirect_for_artist(artist)
