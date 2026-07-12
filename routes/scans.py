"""Compatibility entrypoint for scan routes.

Keep this file so existing imports continue to work:

    from routes.scans import scans_bp

The route handlers themselves now live under ``routes/scan_routes``. This
avoids a risky one-shot migration in the rest of the project while still
letting the large original ``scans.py`` be split into maintainable modules.
"""

from routes.scan_routes import scans_bp

__all__ = ["scans_bp"]
