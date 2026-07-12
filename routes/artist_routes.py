"""Artist-related API routes.

Handles:
- Artist corrections (merge, rename).
- Missing release detection and import.
- Artist metadata display.
"""

from flask import Blueprint, request, jsonify
from routes.utils import json_response as _json_response
from services.metadata import artist_service as corrections
from services.metadata.release_service import get_cached_missing_releases
from services.metadata.artist_scan_service import (
    get_missing_releases as scan_get_missing_releases,
    import_release as scan_import_release,
    scan_all_missing_releases as scan_all_missing,
    add_artist as scan_add_artist,
)
from services.metadata import artist_metadata_service as metadata

artist_bp = Blueprint("artist", __name__)


# =============================
# CORRECTIONS
# =============================

@artist_bp.route("/api/artist/corrections/delete-track", methods=["POST"])
def api_artist_corrections_delete_track():
    payload = request.get_json(silent=True) or {}

    return _json_response(
        corrections.delete_track(
            track_id=payload.get("track_id", ""),
            delete_file=payload.get("delete_file", True),
        )
    )


@artist_bp.route("/api/artist/corrections/clear-disc-number", methods=["POST"])
def api_artist_corrections_clear_disc_number():
    payload = request.get_json(silent=True) or {}
    data, code = corrections.clear_disc_number(
        artist=payload.get("artist", ""),
        album=payload.get("album", ""),
        force=payload.get("force_clear", False),
    )
    return jsonify(data), code


@artist_bp.route("/api/artist/corrections/apply-album-mbid", methods=["POST"])
def api_artist_corrections_apply_album_mbid():
    payload = request.get_json(silent=True) or {}
    data, code = corrections.apply_album_mbid(
        payload
    )
    return jsonify(data), code


@artist_bp.route("/api/artist/corrections/merge-albums", methods=["POST"])
def api_artist_corrections_merge_albums():
    payload = request.get_json(silent=True) or {}
    data, code = corrections.merge_albums(
        artist=payload.get("artist", ""),
        source_albums=payload.get("source_albums", []),
        canonical_name=payload.get("canonical_name", ""),
    )
    return jsonify(data), code


@artist_bp.route("/api/artist/corrections-albums")
def api_artist_corrections_albums():
    artist = request.args.get("artist")
    data, code = corrections.get_correction_albums(artist or "")
    return jsonify(data), code


# =============================
# METADATA
# =============================




@artist_bp.route("/api/artist/exists")
def api_artist_exists():
    artist = request.args.get("artist")

    if not artist:
        return jsonify({
            "success": False,
            "error": "artist required"
        }), 400

    data, code = corrections.artist_exists(artist)
    return jsonify(data), code

@artist_bp.route("/api/artist/cached-missing-releases")
def api_cached_missing_releases():
    artist = request.args.get("artist", "").strip()
    data, code = get_cached_missing_releases(artist)
    return jsonify(data), code



@artist_bp.route("/api/artist/cleanup-false-positive-missing", methods=["POST"])
def api_cleanup_false_positive_missing():
    payload = request.get_json() or {}
    data, code = metadata.cleanup_false_positive_missing(payload.get("artist", ""))
    return jsonify(data), code


@artist_bp.route("/api/artist/bio")
def api_artist_bio():
    artist = request.args.get("name", "")
    data, code = metadata.get_artist_bio(artist)
    return jsonify(data), code


@artist_bp.route("/api/artist/singles-count")
def api_artist_singles_count():
    artist = request.args.get("name", "")
    data, code = metadata.get_singles_count(artist)
    return jsonify(data), code


@artist_bp.route("/api/artist/covered-by")
def api_artist_covered_by():
    artist = request.args.get("artist", "")
    data, code = metadata.get_covered_by(artist)
    return jsonify(data), code


@artist_bp.route("/api/artist/favourite", methods=["GET", "POST", "DELETE"])
def api_artist_favourite():
    data, code = metadata.artist_favourite(request)
    return jsonify(data), code


@artist_bp.route("/api/artist/image")
def api_artist_image():
    artist = request.args.get("name", "")
    return metadata.get_artist_image(artist)  # returns Response


@artist_bp.route("/api/artist/search-images")
def api_artist_search_images():
    artist = request.args.get("name", "")
    source = request.args.get("source", "")
    data, code = metadata.search_images(artist, source)
    return jsonify(data), code


@artist_bp.route("/api/artist/set-image", methods=["POST"])
def api_artist_set_image():
    payload = request.json or {}
    data, code = metadata.set_image(payload)
    return jsonify(data), code


@artist_bp.route("/api/artist/update-ids", methods=["POST"])
def api_artist_update_ids():
    payload = request.get_json(silent=True) or {}
    data, code = metadata.update_ids(payload)
    return jsonify(data), code


@artist_bp.route("/api/artist/lookup-ids", methods=["POST"])
def api_artist_lookup_ids():
    payload = request.get_json(silent=True) or {}
    data, code = metadata.lookup_ids(payload)
    return jsonify(data), code


@artist_bp.route("/api/artist/<path:artist>/similar")
def api_get_similar_artists(artist):
    data, code = metadata.get_similar_artists(artist, request.args)
    return jsonify(data), code


@artist_bp.route("/api/artist/compilations")
def api_artist_compilations():
    artist = request.args.get("name", "")
    data, code = metadata.get_compilations(artist)
    return jsonify(data), code


@artist_bp.route("/api/artist/main-tracks")
def api_artist_main_tracks():
    artist = request.args.get("name", "")
    data, code = metadata.get_main_tracks(artist)
    return jsonify(data), code


@artist_bp.route("/api/artist/stats")
def api_artist_stats():
    artist = request.args.get("name", "")
    data, code = metadata.get_stats(artist)
    return jsonify(data), code


@artist_bp.route("/api/artist/apply-genres", methods=["POST"])
def api_artist_apply_genres():
    payload = request.get_json()
    data, code = metadata.apply_genres(payload)
    return jsonify(data), code


@artist_bp.route("/api/artist/genre-recommendations")
def api_artist_genre_recommendations():
    artist = request.args.get("artist", "")
    data, code = metadata.genre_recommendations(artist)
    return jsonify(data), code


@artist_bp.route("/api/artist/genre-management/save", methods=["POST"])
def api_artist_genre_management_save():
    payload = request.get_json(silent=True) or {}
    data, code = metadata.genre_management(payload)
    return jsonify(data), code


# =============================
# SCAN / HEAVY LOGIC
# =============================

@artist_bp.route("/api/artist/missing-releases")
def api_artist_missing_releases():
    artist = request.args.get("artist")
    background = request.args.get("background", "0")
    data, code = scan_get_missing_releases(artist or "")
    return jsonify(data), code


@artist_bp.route("/api/artist/import-release", methods=["POST"])
def api_import_release():
    payload = request.json or {}
    artist = (payload.get("artist") or "").strip()
    release_id = (payload.get("release_id") or "").strip()
    title = (payload.get("title") or "").strip()
    data, code = scan_import_release(artist, release_id, title)
    return jsonify(data), code


@artist_bp.route("/api/artist/scan-all-missing-releases", methods=["POST"])
def api_scan_all_missing_releases():
    data, code = scan_all_missing()
    return jsonify(data), code


@artist_bp.route("/api/artist/add", methods=["POST"])
def api_add_artist():
    payload = request.json or {}
    artist = (payload.get("artist") or "").strip()
    data, code = scan_add_artist(artist)
    return _json_response(data)

