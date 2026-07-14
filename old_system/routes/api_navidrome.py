from flask import Blueprint, jsonify, request, session
from helpers.logging_config import log_unified, log_error
from api_clients.navidrome import NavidromeClient
from helpers.config_loader import load_config

# 1. Define the blueprint
navidrome_bp = Blueprint('navidrome_api', __name__)

# Helper function to avoid repeating the client setup
def get_navidrome_client():
    cfg = load_config() or {}
    nav_users = cfg.get("navidrome_users", []) or []
    
    # ... (Your existing logic to find the correct user credentials based on session) ...
    
    if nav_users:
        base_url = nav_users[0].get("base_url", "")
        user = nav_users[0].get("user", nav_users[0].get("username", ""))
        password = nav_users[0].get("pass", nav_users[0].get("password", ""))
        return NavidromeClient(base_url, user, password)
    return None

# 2. Move your routes here (Notice we use @navidrome_bp instead of @app)
@navidrome_bp.route('/playlists', methods=['GET'])
def get_playlists():
    """Fetch all Navidrome playlists."""
    client = get_navidrome_client()
    if not client:
        return jsonify({"error": "Navidrome configuration not found."}), 404

    try:
        playlists = client.get_playlists()
        return jsonify({"playlists": playlists}), 200
    except Exception as e:
        log_error(f"Failed to fetch playlists: {e}")
        return jsonify({"error": str(e)}), 500

# Move all other Navidrome-specific routes here:
# @navidrome_bp.route('/playlist/<id>', methods=['GET'])
# @navidrome_bp.route('/scan/start', methods=['POST'])