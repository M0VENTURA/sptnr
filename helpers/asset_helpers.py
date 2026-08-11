"""Versioned static asset helper — automatic cache-busting via file mtime.

Usage in templates::

    {{ versioned_static('css/popularr.css') }}
    {{ versioned_static('js/unified_search.js') }}

The query string tracks each file's last modification time, so deployed
instances and mobile clients always pull the newest asset after a deploy —
no more manual ``?v=N`` bumps (the old ``?v=3``-style links are converted
in base.html and the page templates).
"""

from __future__ import annotations

import os
from typing import Any

# filename -> mtime version.  Static files change rarely; the cache avoids a
# stat per template render while still picking up edits (mtime changes).
_mtime_cache: dict[str, int] = {}


def register_asset_helpers(app: Any) -> None:
    """Register the ``versioned_static`` template global on a Quart app."""

    @app.context_processor
    def _inject_asset_version() -> dict[str, Any]:
        def versioned_static(filename: str) -> str:
            from quart import url_for

            version: int | None = _mtime_cache.get(filename)
            try:
                filepath = os.path.join(str(app.static_folder or ""), filename)
                mtime = int(os.path.getmtime(filepath))
                if version != mtime:
                    _mtime_cache[filename] = mtime
                    version = mtime
            except (OSError, TypeError, ValueError):
                version = None

            url = url_for("static", filename=filename)
            return f"{url}?v={version}" if version else url

        return {"versioned_static": versioned_static}
