"""
db/repositories/__init__.py

Central export point for repository helpers (function-based).
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "queue": ".queue",
    "artists": ".artists",
    "bookmarks": ".bookmarks",
    "genres": ".genres",
    "library": ".library",
    "navidrome": ".navidrome",
    "metadata": ".metadata",
    "tracks": ".tracks",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module = import_module(_EXPORTS[name], __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value