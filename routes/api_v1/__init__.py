"""API v1 blueprint.

All stable API routes live under ``/api/v1/``.

Endpoints return consistent JSON responses using ``_ok()`` / ``_fail()``
helpers.  See each module for details.
"""

from quart import Blueprint

api_v1_bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")

# Import sub-modules to register their routes
from . import tracks, artists
