"""Incremental Navidrome library sync service (compatibility shim).

This module is a legacy duplicate of ``services.library.library_sync_service``.
To avoid two divergent implementations (and the latent NameErrors from the old
inline copy referencing helpers that were never imported here), it now simply
re-exports the canonical implementation.

Canonical source of truth:
    services.library.library_sync_service
        - request_library_sync()
        - perform_library_sync()
        - get_library_sync_state()

The canonical implementation performs delta-only syncs: it skips entirely when
the Navidrome scan marker + ``lastScan`` timestamp are unchanged, and otherwise
only processes artists whose albums/songs changed since the previous run.
"""

from __future__ import annotations

from typing import Any

from services.library.library_sync_service import (
    get_library_sync_state as get_library_sync_state,
    perform_library_sync as perform_library_sync,
    request_library_sync as request_library_sync,
)

__all__ = [
    "request_library_sync",
    "perform_library_sync",
    "get_library_sync_state",
]


def _compat_state() -> dict[str, Any]:
    """Compatibility snapshot (legacy callers may expect ``artists_processed``)."""
    return dict(get_library_sync_state())