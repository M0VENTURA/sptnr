"""Metadata lookup routes.

Provides API endpoints for querying MusicBrainz release metadata
by MBID. Used by the download matching and album detail UIs.
"""

from flask import Blueprint, jsonify
from services.metadata.release_service import get_release_details
metadata_bp = Blueprint("metadata", __name__)

@metadata_bp.route("/api/musicbrainz/release/<release_id>")
def api_get_release_details(release_id):
    result = get_release_details(release_id)

    if not result:
        return jsonify({"error": "Release not found"}), 404

    return jsonify({"success": True, **result})