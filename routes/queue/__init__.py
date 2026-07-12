
"""
Queue route package.

Registers all queue-related blueprints.
"""

from __future__ import annotations

from .processing_routes import queue_processing_bp
from .matching_routes import queue_matching_bp
from .cleanup_routes import queue_cleanup_bp
from .diagnostics_routes import queue_diagnostics_bp


def register_queue_routes(app):
    """Register all queue route blueprints."""
    for bp in [
        queue_processing_bp,
        queue_matching_bp,
        queue_cleanup_bp,
        queue_diagnostics_bp,
    ]:
        app.register_blueprint(bp)