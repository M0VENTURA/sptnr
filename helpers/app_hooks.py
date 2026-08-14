"""Flask application lifecycle hooks.

Registers ``before_request`` and ``after_request`` handlers on the Flask app.
Provides:
- Request timing (duration logged as ``X-Request-Duration`` header).
- Basic security headers on every response.
- Authentication gate: every route requires a logged-in session EXCEPT a
  small public allow-list (static assets, the login page and the first-run
  setup wizard + its APIs).  While Navidrome is unconfigured (first run),
  all other routes redirect to the setup wizard so a brand-new user can
  reach it; once configured, unauthenticated page requests redirect to the
  login page and unauthenticated API calls return 401 JSON.
- First-run interceptor: redirects to setup if Navidrome is unconfigured.

Called once during app factory setup.
"""

import time
import logging
import traceback

from quart import g, jsonify, redirect, request, session, url_for
from werkzeug.exceptions import HTTPException

logger = logging.getLogger(__name__)


# Endpoints reachable WITHOUT a session at all times (static assets + the
# login/logout pages).  Everything else is gated by ``before_request``.
_ALWAYS_PUBLIC_ENDPOINTS = frozenset({
    "static",
    "ui.login",
    "ui.logout",
})

# Endpoints the first-run setup wizard needs.  These are public ONLY while
# ``needs_setup()`` is true (Navidrome not configured yet) so a brand-new
# user can complete the wizard; once configured they require a session.
_SETUP_PUBLIC_ENDPOINTS = frozenset({
    "ui.setup",
    "ui.api_test_navidrome_connection",
    "ui.api_setup_save",
    "ui.api_setup_save_partial",
    "misc_api.api_essentia_download_status",
    "misc_api.api_essentia_download_models",
    "scans.api_navidrome_import",
})


def _is_api_request() -> bool:
    """True when the current request targets a JSON API endpoint."""
    return request.path.startswith("/api/")


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
        try:
            request_ctx = f"{request.method} {request.path}"
            if request.query_string:
                request_ctx += f"?{request.query_string.decode('utf-8', 'replace')[:200]}"
        except Exception:
            request_ctx = "(no request context)"
        logger.error("Unhandled exception [%s]: %s: %s", request_ctx, type(exc).__name__, exc)
        logger.error(traceback.format_exc())
        return jsonify({"success": False, "error": "An internal server error occurred. Please try again."}), 500

    @app.before_request
    def before_request():
        g.start_time = time.time()

        endpoint = request.endpoint or ""

        # ── Always-public endpoints (static, login, logout) ──────────────
        if endpoint in _ALWAYS_PUBLIC_ENDPOINTS or endpoint.endswith(".static"):
            return

        from helpers.config_helpers import needs_setup

        # ── First run: setup wizard + its APIs are public ────────────────
        # While Navidrome is unconfigured a brand-new user must be able to
        # reach the setup wizard (and the login page, via _ALWAYS_PUBLIC).
        # Everything else redirects to setup (pages) or returns 401 (APIs).
        if needs_setup():
            if endpoint in _SETUP_PUBLIC_ENDPOINTS:
                return
            if _is_api_request():
                return jsonify({"success": False, "error": "Setup required"}), 401
            return redirect(url_for("ui.setup"))

        # ── Configured app: require a session for everything else ────────
        if session.get("username"):
            return

        if _is_api_request():
            return jsonify({"success": False, "error": "Authentication required"}), 401
        return redirect(url_for("ui.login"))

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