"""Flask application lifecycle hooks.

Registers ``before_request`` and ``after_request`` handlers on the Flask app.
Provides:
- Request timing (duration logged as ``X-Request-Duration`` header).
- Basic security headers on every response.
- Placeholder for future rate-limiting or session validation.

Called once during app factory setup.
"""

import time
import logging

from flask import g, request

logger = logging.getLogger(__name__)


def register_app_hooks(app):
    @app.before_request
    def before_request():
        g.start_time = time.time()

    @app.after_request
    def after_request(response):
        # Request timing
        if hasattr(g, "start_time"):
            duration_ms = int((time.time() - g.start_time) * 1000)
            response.headers["X-Request-Duration-Ms"] = str(duration_ms)
            if duration_ms > 1000:
                logger.info(
                    "Slow request: %s %s (%dms)",
                    request.method,
                    request.path,
                    duration_ms,
                )

        # Security headers
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")

        return response