"""Flask blueprint registration.

Central import-and-register for all API blueprints used by the Flask app.
Called once during app factory setup (``helpers.app_bootstrap.register_all_blueprints``).

Architecture rules:
- Every new route module with a ``Blueprint`` must be added here.
- Blueprints can set ``url_prefix`` at registration time.
- No business logic is stored in this file.
"""

from routes.downloads import downloads_bp
from routes.playlists import playlists_bp
from routes.favourites import favourites_bp
from routes.album_routes import album_bp
from routes.artist_routes import artist_bp
from routes.analytics import analytics_bp
from routes.logs import logs_bp
from routes.metadata import metadata_bp
from routes.track_routes import track_bp
from routes.download_search_routes import slskd_bp, slsk_bp
from routes.musicbrainz_routes import mb_bp
from routes.social_routes import listenbrainz_bp, lastfm_bp, weekly_bp
from routes.upcoming_releases_routes import upcoming_bp
from routes.misc_routes import misc_api_bp
from routes.ui_routes import ui_bp
from routes.api_v1 import api_v1_bp
from routes.navidrome import navidrome_bp
from routes.scan_routes import scans_bp
from routes.scan_routes.library_routes import library_bp
from routes.queue import (
    queue_processing_bp,
    queue_matching_bp,
    queue_cleanup_bp,
    queue_diagnostics_bp,
)


def register_all_blueprints(app):
    app.register_blueprint(downloads_bp)
    app.register_blueprint(playlists_bp)
    app.register_blueprint(favourites_bp)
    app.register_blueprint(album_bp, url_prefix="/api/album")
    app.register_blueprint(artist_bp)
    app.register_blueprint(analytics_bp)
    app.register_blueprint(logs_bp)
    app.register_blueprint(metadata_bp)
    app.register_blueprint(track_bp)
    app.register_blueprint(slskd_bp)
    app.register_blueprint(slsk_bp)
    app.register_blueprint(mb_bp)
    app.register_blueprint(listenbrainz_bp)
    app.register_blueprint(lastfm_bp)
    app.register_blueprint(weekly_bp)
    app.register_blueprint(upcoming_bp)
    app.register_blueprint(misc_api_bp)
    app.register_blueprint(api_v1_bp)
    app.register_blueprint(ui_bp)
    app.register_blueprint(navidrome_bp)
    app.register_blueprint(scans_bp)
    app.register_blueprint(library_bp)
    app.register_blueprint(queue_processing_bp)
    app.register_blueprint(queue_matching_bp)
    app.register_blueprint(queue_cleanup_bp)
    app.register_blueprint(queue_diagnostics_bp)