"""Analytics dashboard routes.

Provides genre/mood analytics page for the WebUI.
Delegates data aggregation to ``services.catalog.analytics_service``.
"""

from flask import Blueprint, render_template, jsonify, request
import logging

from services.catalog.analytics_service import get_genre_mood_analytics

analytics_bp = Blueprint("analytics", __name__)


@analytics_bp.route("/analytics/genres-moods")
def analytics_genres_moods_page():
    genres, moods, combos = get_genre_mood_analytics(top_n=50)

    return render_template(
        "pages/analytics.html",
        top_genres=genres,
        top_moods=moods,
        top_combos=combos
    )


@analytics_bp.route("/api/analytics/genres-moods")
def api_analytics_genres_moods():
    try:
        limit = request.args.get("limit", 50, type=int)
        limit = max(1, min(limit, 100))

        genres, moods, combos = get_genre_mood_analytics(top_n=limit)

        return jsonify({
            "genres": genres,
            "moods": moods,
            "combos": combos
        })

    except Exception as e:
        logging.error(f"Failed to fetch genres/moods analytics: {e}")
        return jsonify({
            "genres": [],
            "moods": [],
            "combos": [],
            "error": str(e)
        }), 500