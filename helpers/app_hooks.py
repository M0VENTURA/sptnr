"""Flask application lifecycle hooks.

Registers ``before_request`` and ``after_request`` handlers on the Flask app.
Provides:
- Request timing (duration logged as ``X-Request-Duration`` header).
- Basic security headers on every response.
- First-run interceptor: redirects to setup if Navidrome is unconfigured.
- Placeholder for future rate-limiting or session validation.

Called once during app factory setup.
"""

import time
import logging
import traceback

from quart import g, jsonify, redirect, request, url_for
from werkzeug.exceptions import HTTPException

logger = logging.getLogger(__name__)


def register_app_hooks(app):
    @app.errorhandler(Exception)
    def handle_unhandled_exception(exc):
        """Return JSON for escaped exceptions instead of Quart's default HTML 500 page.

        Without this, API clients receive an HTML error page and report
        'Server returned HTML instead of JSON (HTTP 500)'. Mirrors the legacy
        system's handler: HTTP exceptions (404/405/...) keep their default
        behaviour; anything else becomes a JSON 500 with a logged traceback.
        """
        if isinstance(exc, HTTPException):
            return exc
        logger.error("Unhandled exception: %s: %s", type(exc).__name__, exc)
        logger.error(traceback.format_exc())
        return jsonify({"success": False, "error": "An internal server error occurred. Please try again."}), 500

    @app.before_request
    def before_request():
        g.start_time = time.time()

        # ── First-run interceptor ─────────────────────────────────────
        # Redirect to setup if Navidrome is not yet configured.
        # Skip static assets and auth pages to avoid redirect loops.
        allowed_endpoints = {
            "ui.setup", "ui.login", "static",
            "scans_bp.static", "navidrome_api.static",
        }
        if request.endpoint and request.endpoint in allowed_endpoints:
            return

        # Only intercept HTML page requests, not API calls
        if request.endpoint and not request.endpoint.startswith("ui."):
            return

        # Also skip API routes that happen to live under the ui blueprint
        if request.path.startswith("/api/"):
            return

        try:
            from helpers.config_helpers import get_config
            cfg = get_config()
            nav_users = cfg.get("navidrome_users", [])
            nav = cfg.get("navidrome", {}) or {}
            has_config = bool(
                nav_users or nav.get("base_url") or nav.get("user") or nav.get("pass")
            )
            if not has_config:
                return redirect(url_for("ui.setup"))
        except Exception:
            pass

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