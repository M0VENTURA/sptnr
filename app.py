#!/usr/bin/env python3
from helpers.db_utils import (
    ensure_album_artist_column,
    ensure_musicbrainz_album_mbid_column,
    verify_album_artist_column,
)
import os
# --- ENVIRONMENT VARIABLE EDITING SUPPORT ---
# List of all environment variables used in the project (compiled from codebase)
ALL_ENV_VARS = [
    "SECRET_KEY", "CONFIG_PATH", "DB_PATH", "LOG_PATH", "APP_DIR", "SPTNR_DISABLE_BOOT_ND_IMPORT", "SPTNR_SKIP_SINGLES",
    "MUSIC_ROOT", "MUSIC_FOLDER", "DOWNLOADS_DIR", "POPULARITY_LOG_PATH", "POPULARITY_LOG_STDOUT", "POPULARITY_PROGRESS_FILE",
    "NAVIDROME_PROGRESS_FILE", "SINGLES_PROGRESS_FILE", "PROGRESS_FILE", "TIMEZONE", "TZ", "SPOTIFY_USER_TOKEN",
    "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_WEIGHT", "LASTFM_WEIGHT", "AGE_WEIGHT",
    "LASTFMAPIKEY", "NAV_BASE_URL", "NAV_USER", "NAV_PASS", "YOUTUBE_API_KEY", "GOOGLE_CSE_ID", "GOOGLE_API_KEY",
    "TRUSTED_CHANNEL_IDS", "DISCOGS_TOKEN", "AI_API_KEY", "DEV_BOOST_WEIGHT", "AUDIODB_API_KEY", "WEB_API_KEY",
    "ENABLE_WEB_API_KEY", "MP3_PROGRESS_FILE", "BEETS_LOG_PATH", "SEARCHAPI_IO_KEY",
    "PG_HOST", "PG_PORT", "PG_USER", "PG_PASSWORD", "PG_DATABASE"
]

def get_all_env_vars():
    # Return a dict of all relevant env vars and their current values
    return {var: os.environ.get(var, "") for var in ALL_ENV_VARS}
import sqlite3
try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    pass  # PostgreSQL support optional
# Mutagen imports for audio file tagging
try:
    from mutagen.mp3 import MP3
    from mutagen.flac import FLAC
    from mutagen.id3 import ID3, TCON as TagCON
except ImportError:
    MP3 = None
    FLAC = None
    ID3 = None
    TagCON = None
from contextlib import closing
import json
import yaml
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response, send_file, session, abort
from datetime import datetime
import copy
from functools import wraps
from helpers.scan_helpers import scan_artist_to_db
from helpers.config_helpers import get_config, get_navidrome_config, clear_config_cache
from popularity import popularity_scan, row_get, download_and_save_album_art
from popularity_helpers import build_artist_index
from unified_scan import unified_scan_pipeline
# --- Utility: Aggregate genres from tracks in DB ---
def aggregate_genres_from_tracks(artist_name, db_path="/database/sptnr.db"):
    """
    Aggregate unique genres from all tracks by an artist.
    Args:
        artist_name: Artist name
        db_path: Path to database
    Returns:
        list: Sorted list of unique genres
        
    Note: Returns empty list for Various Artists and Soundtracks as they
          have a lot of different artists and genres.
    """
    # Skip genre aggregation for Various Artists and Soundtracks
    artist_lower = artist_name.lower()
    if artist_lower == "various artists" or "soundtrack" in artist_lower:
        return []
    
    genres = set()
    try:
        import sqlite3
        import re
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        # Use navidrome_genres which are populated from Navidrome during import
        # Query by COALESCE(album_artist, artist) to match the artist list page logic
        # Use NULLIF to treat empty strings as NULL for proper COALESCE behavior
        cursor.execute("SELECT navidrome_genres FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = ?", (artist_name,))
        rows = cursor.fetchall()
        conn.close()
        for row in rows:
            if row[0]:
                genre_str = row[0]
                if isinstance(genre_str, str):
                    try:
                        import json
                        genre_list = json.loads(genre_str)
                        if isinstance(genre_list, list):
                            genres.update(genre_list)
                    except:
                        # Split on both backslash and comma separators (same as split_genres filter)
                        genre_list = re.split(r'[\\,]+', genre_str)
                        for g in genre_list:
                            g = g.strip().strip('"\'[]')
                            if g:
                                genres.add(g)
    except:
        pass
    return sorted(list(genres))

def correct_genre_capitalization(genre_str):
    """
    Auto-correct genre capitalization for consistency.
    Examples: "rock" → "Rock", "hIP-hOP" → "Hip-Hop"
    """
    if not genre_str:
        return genre_str
    
    genre_lower = genre_str.lower().strip()
    
    # Common genre capitalization rules
    genre_map = {
        # Standard genres
        'rock': 'Rock',
        'pop': 'Pop',
        'jazz': 'Jazz',
        'classical': 'Classical',
        'hip-hop': 'Hip-Hop',
        'hiphop': 'Hip-Hop',
        'r&b': 'R&B',
        'electronic': 'Electronic',
        'blues': 'Blues',
        'country': 'Country',
        'soul': 'Soul',
        'funk': 'Funk',
        'metal': 'Metal',
        'punk': 'Punk',
        'alternative': 'Alternative',
        'indie': 'Indie',
        'folk': 'Folk',
        'reggae': 'Reggae',
        'latin': 'Latin',
        'dance': 'Dance',
        'house': 'House',
        'techno': 'Techno',
        'trance': 'Trance',
        'dubstep': 'Dubstep',
        'rap': 'Rap',
        'gospel': 'Gospel',
        'rnb': 'R&B',
        'edm': 'EDM',
        'ambient': 'Ambient',
        'experimental': 'Experimental',
        'avant-garde': 'Avant-Garde',
        'world': 'World',
        'afrobeat': 'Afrobeat',
        'reggaeton': 'Reggaeton',
        'trap': 'Trap',
        'grime': 'Grime',
    }
    
    # Check if exact match exists
    if genre_lower in genre_map:
        return genre_map[genre_lower]
    
    # Default: capitalize first letter of each word
    return ' '.join(word.capitalize() for word in genre_str.split())

def log_genre_update(artist_name=None, album_name=None, track_id=None, genres_before='', 
                     genres_after='', action_type='manual', affected_count=1, 
                     change_summary='', db_path="/database/sptnr.db"):
    """
    Log genre changes to genre_updates table for audit trail.
    """
    try:
        import sqlite3
        conn = sqlite3.connect(db_path, timeout=120.0)
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO genre_updates 
            (artist_name, album_name, track_id, genres_before, genres_after, 
             action_type, affected_track_count, change_summary, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (artist_name, album_name, track_id, genres_before, genres_after, 
              action_type, affected_count, change_summary))
        
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[ERROR] Failed to log genre update: {e}")

from deprecated.check_db import update_schema
from popularity_helpers import save_to_db

import sys

# Diagnostic: Print which start.py is being imported
import importlib.util
spec = importlib.util.find_spec("start")
if spec and spec.origin:
    print(f"[DIAGNOSTIC] start.py will be imported from: {spec.origin}")
else:
    print("[DIAGNOSTIC] start.py module not found in import path!")
import secrets
import subprocess
import threading
import time
import logging
import re
from api_clients.slskd import SlskdClient
from api_clients.musicbrainz import _USER_AGENT as MUSICBRAINZ_USER_AGENT
from helpers.metadata_reader import get_track_metadata_from_db, find_track_file, read_mp3_metadata
import io
from helpers.helpers import create_retry_session, clean_discogs_biography
import difflib
import unicodedata
from playlist_matcher import match_track as enhanced_match_track
import requests
import hashlib
from deprecated.musicbrainz_import import (
    get_musicbrainz_tags_for_track,
    get_musicbrainz_tags_for_album,
    import_musicbrainz_tags_for_track,
    import_musicbrainz_tags_for_album,
    import_musicbrainz_tags_for_artist,
    update_musicbrainz_tag_in_db,
    write_musicbrainz_tag_to_mp3
)
from compilation_manager import (
    get_main_tracks_for_artist,
    get_compilations_for_artist,
    get_artist_stats,
    import_featured_artists_for_track,
    import_featured_artists_for_album
)

# Import centralized logging configuration
from helpers.logging_config import (
    setup_logging, 
    log_unified, 
    log_info, 
    log_debug,
    UNIFIED_LOG_PATH,
    INFO_LOG_PATH,
    DEBUG_LOG_PATH
)

# Set up logging with WebUI service name
setup_logging("WebUI")

# API Rate limiting constants
DISCOGS_RATE_LIMIT_DELAY = 1  # seconds between Discogs API requests

# Legacy compatibility - keep old functions
LOG_PATH = os.environ.get("LOG_PATH", "/config/sptnr.log")
VERBOSE = (
    os.environ.get("SPTNR_VERBOSE_APP") or os.environ.get("SPTNR_VERBOSE") or "0"
) == "1"

def log_basic(msg):
    """Legacy function - logs to info.log"""
    if VERBOSE:
        log_info(msg)

def log_verbose(msg):
    """Legacy function - logs to debug.log"""
    if VERBOSE:
        log_debug(f"[VERBOSE] {msg}")


# Determine the app root directory (handles both local and Docker execution)
app_root = os.path.abspath(os.path.dirname(__file__))
static_folder = os.path.join(app_root, 'static')

# Initialize Flask with explicit static folder configuration
app = Flask(__name__, static_folder=static_folder, static_url_path='/static')

# Debug: Log static folder configuration on startup
print(f"\n{'='*60}")
print(f"Flask Static Configuration:")
print(f"  App Root: {app_root}")
print(f"  Static Folder: {static_folder}")
print(f"  Static Folder Exists: {os.path.isdir(static_folder)}")
if os.path.isdir(static_folder):
    try:
        static_files = os.listdir(static_folder)
        print(f"  Files in static folder: {static_files[:5]}...")  # Show first 5 files
    except Exception as e:
        print(f"  Error listing files: {e}")
print(f"{'='*60}\n")

# Add Jinja2 filter to split genres on both backslash and comma
@app.template_filter('split_genres')
def split_genres(s):
    """Split string on both backslash and comma separators"""
    if not s:
        return []
    # Split on backslash or comma
    import re
    genres = re.split(r'[\\,]+', str(s))
    return [g.strip() for g in genres if g.strip()]


@app.template_filter('split_artist_collabs')
def split_artist_collabs(s):
    """Split collaboration artist strings into individual artist names."""
    if not s:
        return []

    import re

    # Common collaboration delimiters used in imported metadata.
    # Keep this conservative to avoid splitting legitimate band names.
    parts = re.split(r'\s+(?:w/|feat\.?|ft\.?|featuring)\s+', str(s), flags=re.IGNORECASE)
    cleaned = [p.strip() for p in parts if p and p.strip()]
    return cleaned or [str(s).strip()]

# Add Jinja2 filter for regex replacement
@app.template_filter('regex_replace')
def regex_replace(s, pattern, replacement):
    """Replace pattern in string using regex"""
    import re
    return re.sub(pattern, replacement, str(s))

# Add Jinja2 filter for JavaScript escaping
@app.template_filter('escapejs')
def escapejs(value):
    """Escape strings for use in JavaScript contexts"""
    if value is None:
        return ''
    
    # Convert to string
    value = str(value)
    
    # Define escape mappings for JavaScript
    escapes = {
        '\\': '\\\\',
        "'": "\\'",
        '"': '\\"',
        '\n': '\\n',
        '\r': '\\r',
        '\t': '\\t',
        '\b': '\\b',
        '\f': '\\f',
        '<': '\\u003C',  # Prevent script injection
        '>': '\\u003E',  # Prevent script injection
        '&': '\\u0026',  # Prevent HTML entity issues
    }
    
    # Apply escapes
    for char, escape in escapes.items():
        value = value.replace(char, escape)
    
    return value

# Add cache-control headers to prevent browser caching of HTML templates
@app.after_request
def set_cache_headers(response):
    """Prevent browser caching of HTML templates but allow caching of static assets"""
    if response.content_type and 'text/html' in response.content_type:
        # Don't cache HTML templates
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response

# Ensure album_artist column exists and is populated on startup
import logging
ensure_result = ensure_album_artist_column()

# Ensure legacy beets_album_mbid has been migrated to musicbrainz_album_mbid.
ensure_musicbrainz_album_mbid_column()

# Verify the migration worked
verification = verify_album_artist_column()
logging.info(f"Album Artist Migration Status: {verification['message']}")
if not verification["exists"]:
    logging.warning(f"⚠️ Database migration issue: {verification['message']}")

# Initialize complete database schema (all tables now created/verified in update_schema)
# This ensures all tables and columns exist on startup
DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")
try:
    update_schema(DB_PATH)
    logging.info("Database schema initialization complete (all tables created/verified)")
except Exception as e:
    logging.error(f"Error initializing database schema: {e}")

# --- Unified Log API ---
@app.route("/api/unified-log")
def api_unified_log():
    lines = int(request.args.get("lines", 1000))
    verbose = request.args.get("verbose", "0") == "1"
    unified_log_path = "/config/unified_scan.log"
    log_lines = []
    try:
        log_verbose(f"[api_unified_log] Reading {lines} lines from {unified_log_path}")
        if not os.path.exists(unified_log_path):
            log_verbose(f"[api_unified_log] Log file not found: {unified_log_path}")
            return jsonify({"error": f"Unified log file not found at {unified_log_path}", "lines": []}), 404
        with open(unified_log_path, "r", encoding="utf-8", errors="ignore") as f:
            log_lines = f.readlines()
    except Exception as e:
        log_verbose(f"[api_unified_log] Exception reading file: {e}")
        return jsonify({"error": str(e), "lines": []}), 500
    try:
        # Filter out HTTP request/response logs and other verbose logs unless verbose is enabled
        if not verbose:
            import re
            http_log_pattern = re.compile(r'"(GET|POST|PUT|DELETE|PATCH) /api/.* HTTP/1\\.[01]" (200|201|204|400|401|403|404|500|502|503)')
            # Patterns for verbose/unimportant log lines to skip
            skip_patterns = [
                r'\[api_unified_log\]',  # API logging itself
                r'Checking match for',  # Verbose matching logs
                r'Found \d+ existing track',  # Verbose track counting
                r'Album already scanned',  # Already handled by skipping in recent scans
                r'Skipping artist.*already.*scanned',  # Verbose skip messages
            ]
            skip_regex = re.compile('|'.join(skip_patterns), re.IGNORECASE)
            
            filtered_lines = []
            for line in log_lines:
                # Skip HTTP logs
                if http_log_pattern.search(line):
                    continue
                # Skip other verbose patterns
                if skip_regex.search(line):
                    continue
                filtered_lines.append(line)
            log_lines = filtered_lines
        # Only return the last N lines
        log_lines = log_lines[-lines:]
        log_verbose(f"[api_unified_log] Returning {len(log_lines)} log lines")
        return jsonify({"lines": [line.rstrip('\n') for line in log_lines]})
    except Exception as e:
        log_verbose(f"[api_unified_log] Exception processing log lines: {e}")
        return jsonify({"error": str(e), "lines": []}), 500

# --- Log Download API ---
@app.route("/api/download-log/<log_type>")
def api_download_log(log_type):
    """
    Download the last hour of a specific log file.
    
    Args:
        log_type: One of 'unified', 'info', 'debug'
    """
    from datetime import datetime, timedelta
    
    # Map log type to file path
    log_paths = {
        'unified': UNIFIED_LOG_PATH,
        'info': INFO_LOG_PATH,
        'debug': DEBUG_LOG_PATH
    }
    
    if log_type not in log_paths:
        return jsonify({"error": "Invalid log type. Must be 'unified', 'info', or 'debug'"}), 400
    
    log_path = log_paths[log_type]
    
    if not os.path.exists(log_path):
        return jsonify({"error": f"Log file not found: {log_path}"}), 404
    
    try:
        # Read log file and filter for last hour
        cutoff_time = datetime.now() - timedelta(hours=1)
        filtered_lines = []
        
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                # Try to parse timestamp from log line (format: YYYY-MM-DD HH:MM:SS)
                try:
                    # Extract timestamp from beginning of line
                    parts = line.split('[', 1)
                    if parts and len(parts[0].strip()) >= 19:
                        timestamp_str = parts[0].strip()[:19]
                        line_time = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
                        if line_time >= cutoff_time:
                            filtered_lines.append(line)
                except (ValueError, IndexError):
                    # If we can't parse timestamp, include the line anyway
                    filtered_lines.append(line)
        
        # Create response with log content
        log_content = ''.join(filtered_lines)
        
        # Generate filename with timestamp
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"{log_type}_log_{timestamp}.txt"
        
        # Return as downloadable file
        return Response(
            log_content,
            mimetype='text/plain',
            headers={
                'Content-Disposition': f'attachment; filename={filename}',
                'Content-Type': 'text/plain; charset=utf-8'
            }
        )
        
    except Exception as e:
        log_info(f"Error downloading {log_type} log: {e}")
        return jsonify({"error": str(e)}), 500

# --- Navidrome Playlists API ---
@app.route("/api/navidrome/playlists", methods=["GET"])
def api_navidrome_playlists():
    """Return all Navidrome playlists (id, name, type) grouped by type for dropdowns."""
    try:
        config_data, _ = _read_yaml(CONFIG_PATH)
        current_user = session.get("username")
        navidrome_users = config_data.get("navidrome_users", [])
        nav_cfg = None

        if navidrome_users and current_user:
            # Find the config for the logged-in user
            nav_cfg = next((u for u in navidrome_users if u.get("user") == current_user), None)
        if not nav_cfg:
            # Fallback to single-user config
            nav_cfg = config_data.get("navidrome", {})

        base_url = nav_cfg.get("base_url")
        username = nav_cfg.get("user")
        password = nav_cfg.get("pass")
        if not (base_url and username and password):
            logging.error(f"Navidrome not configured: base_url={base_url}, username={username}, password={'set' if password else 'unset'}")
            return jsonify({"error": "Navidrome not configured. Please check your config file and credentials."}), 400
        from api_clients.navidrome import NavidromeClient
        client = NavidromeClient(base_url, username, password)
        playlists = client.fetch_all_playlists()
        if playlists is None:
            logging.error("NavidromeClient returned None for playlists.")
            return jsonify({"error": "Failed to fetch playlists from Navidrome. See logs for details."}), 500
        if not playlists:
            logging.warning("No playlists returned from Navidrome. Check if any exist for the configured user.")
            return jsonify({"error": "No playlists found in Navidrome for the configured user."}), 200
        result = {"smart": [], "regular": []}
        for pl in playlists:
            entry = {"id": pl.get("id"), "name": pl.get("name")}
            if pl.get("type") == "smart":
                result["smart"].append(entry)
            else:
                result["regular"].append(entry)
        return jsonify(result)
    except Exception as e:
        logging.error(f"Failed to fetch Navidrome playlists: {e}", exc_info=True)
        return jsonify({"error": f"Exception occurred: {str(e)}"}), 500

@app.route("/api/navidrome/playlist/<playlist_id>", methods=["GET"])
def api_navidrome_playlist_detail(playlist_id):
    """Return full details for a single Navidrome playlist by ID."""
    try:
        config_data, _ = _read_yaml(CONFIG_PATH)
        current_user = session.get("username")
        navidrome_users = config_data.get("navidrome_users", [])
        nav_cfg = None

        if navidrome_users and current_user:
            # Find the config for the logged-in user
            nav_cfg = next((u for u in navidrome_users if u.get("user") == current_user), None)
        if not nav_cfg:
            # Fallback to single-user config
            nav_cfg = config_data.get("navidrome", {})
        
        base_url = nav_cfg.get("base_url")
        username = nav_cfg.get("user")
        password = nav_cfg.get("pass")
        
        if not (base_url and username and password):
            logging.error(f"Navidrome not configured: base_url={base_url}, username={username}, password={'set' if password else 'unset'}")
            return jsonify({"error": "Navidrome not configured. Please check your config file and credentials."}), 400
        
        from api_clients.navidrome import NavidromeClient
        client = NavidromeClient(base_url, username, password)
        playlist = client.fetch_playlist(playlist_id)
        
        if not playlist:
            logging.warning(f"Playlist ID {playlist_id} not found in Navidrome.")
            return jsonify({"error": f"Playlist {playlist_id} not found in Navidrome."}), 404
        
        # Add Navidrome URL for edit link
        playlist['navidromeUrl'] = base_url
        
        return jsonify(playlist)
    except Exception as e:
        logging.error(f"Failed to fetch Navidrome playlist detail: {e}", exc_info=True)
        return jsonify({"error": f"Exception occurred: {str(e)}"}), 500

# --- Spotify Playlists API ---
@app.route("/api/spotify/playlists", methods=["GET"])
def api_spotify_playlists():
    """
    Fetch public playlists for a Spotify user.
    Query params:
        - user_id (optional): Spotify User ID. If not provided, uses config.yaml value or returns featured playlists.
    """
    try:
        config_data, _ = _read_yaml(CONFIG_PATH)
        spotify_config = config_data.get("api_integrations", {}).get("spotify", {})
        client_id = spotify_config.get("client_id", "")
        client_secret = spotify_config.get("client_secret", "")
        
        if not client_id or not client_secret:
            return jsonify({"error": "Spotify not configured. Please add client_id and client_secret to config.yaml"}), 400
        
        # Get user_id from query param or config
        user_id = request.args.get("user_id", "").strip()
        if not user_id:
            user_id = spotify_config.get("user_id", "").strip()
        
        if user_id:
            # Fetch playlists for specific user
            from api_clients.spotify import get_spotify_user_public_playlists
            playlists = get_spotify_user_public_playlists(user_id, client_id, client_secret)
            return jsonify({
                "playlists": playlists,
                "user_id": user_id,
                "count": len(playlists)
            })
        else:
            # Fallback to featured playlists
            from api_clients.spotify import get_spotify_user_playlists
            playlists = get_spotify_user_playlists(client_id, client_secret)
            return jsonify({
                "playlists": playlists,
                "user_id": None,
                "count": len(playlists)
            })
    
    except Exception as e:
        logging.error(f"Failed to fetch Spotify playlists: {e}", exc_info=True)
        return jsonify({"error": f"Exception occurred: {str(e)}"}), 500

# --- Spotify User Playlists Endpoint (for Playlist Downloads tab) ---
@app.route("/api/spotify/user/<username>/playlists", methods=["GET"])
def api_spotify_user_playlists(username):
    """
    Fetch public playlists for a Spotify user by username.
    """
    try:
        config_data, _ = _read_yaml(CONFIG_PATH)
        spotify_config = config_data.get("api_integrations", {}).get("spotify", {})
        client_id = spotify_config.get("client_id", "")
        client_secret = spotify_config.get("client_secret", "")
        
        if not client_id or not client_secret:
            return jsonify({"success": False, "error": "Spotify not configured"}), 400
        
        from api_clients.spotify import get_spotify_user_public_playlists
        playlists = get_spotify_user_public_playlists(username, client_id, client_secret)
        
        return jsonify({
            "success": True,
            "username": username,
            "playlists": playlists,
            "count": len(playlists)
        })
    
    except Exception as e:
        logging.error(f"Failed to fetch playlists for user {username}: {e}", exc_info=True)
        return jsonify({"success": False, "error": f"Failed to fetch playlists: {str(e)}"}), 500

# --- Spotify Playlist Tracks with Collection Matching ---
@app.route("/api/spotify/playlist/<playlist_id>/tracks", methods=["GET"])
def api_spotify_playlist_tracks_with_matching(playlist_id):
    """
    Fetch tracks from a Spotify playlist and match them to the collection.
    Query params:
        - match_collection (optional): If true, compare tracks to collection and identify missing
    """
    try:
        config_data, _ = _read_yaml(CONFIG_PATH)
        spotify_config = config_data.get("api_integrations", {}).get("spotify", {})
        client_id = spotify_config.get("client_id", "")
        client_secret = spotify_config.get("client_secret", "")
        
        if not client_id or not client_secret:
            return jsonify({"success": False, "error": "Spotify not configured"}), 400
        
        # Fetch playlist tracks
        from api_clients.spotify import SpotifyClient
        spotify_client = SpotifyClient(client_id, client_secret)
        tracks = spotify_client.get_playlist_tracks(playlist_id)
        
        if not tracks:
            return jsonify({"success": False, "error": "Playlist not found or is empty"}), 404
        
        match_collection = request.args.get("match_collection", "false").lower() == "true"
        
        if match_collection:
            # Get all tracks from collection
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT artist, title, album FROM tracks ORDER BY artist, title")
            collection_tracks = cursor.fetchall()
            conn.close()
            
            # Create searchable set for collection
            collection_set = set()
            for row in collection_tracks:
                artist = (row['artist'] or '').lower()
                title = (row['title'] or '').lower()
                key = f"{artist}|{title}"
                collection_set.add(key)
            
            # Match playlist tracks
            matched_tracks = []
            missing_tracks = []
            
            for track in tracks:
                artist = (track.get('artist', '') or '').lower()
                title = (track.get('title', '') or '').lower()
                key = f"{artist}|{title}"
                
                if key in collection_set:
                    matched_tracks.append({
                        'artist': track.get('artist', ''),
                        'title': track.get('title', ''),
                        'album': track.get('album', '')
                    })
                else:
                    missing_tracks.append({
                        'artist': track.get('artist', ''),
                        'title': track.get('title', ''),
                        'album': track.get('album', '')
                    })
            
            return jsonify({
                "success": True,
                "playlist_id": playlist_id,
                "tracks": tracks,
                "matched_tracks": matched_tracks,
                "missing_tracks": missing_tracks,
                "matched_count": len(matched_tracks),
                "missing_count": len(missing_tracks),
                "total_count": len(tracks)
            })
        else:
            return jsonify({
                "success": True,
                "playlist_id": playlist_id,
                "tracks": tracks,
                "count": len(tracks)
            })
    
    except Exception as e:
        logging.error(f"Failed to fetch playlist {playlist_id} tracks: {e}", exc_info=True)
        return jsonify({"success": False, "error": f"Failed to fetch playlist: {str(e)}"}), 500

# --- Create Playlist Download Session ---
@app.route("/api/playlist/session", methods=["POST"])
def api_create_playlist_session():
    """
    Create a playlist download session and add tracks to download queue.
    """
    try:
        data = request.json
        
        if not data:
            return jsonify({"success": False, "error": "No data provided"}), 400
        
        session_name = data.get("session_name", "").strip()
        playlist_name = data.get("playlist_name", "").strip()
        playlist_id = data.get("playlist_id", "").strip()
        download_method = data.get("download_method", "soulseek").strip()
        tracks = data.get("tracks", [])
        
        if not session_name or not tracks:
            return jsonify({"success": False, "error": "Missing session_name or tracks"}), 400
        
        if download_method not in ["soulseek", "qbittorrent"]:
            return jsonify({"success": False, "error": "Invalid download_method"}), 400
        
        # Add each track to the download queue
        from download_queue_manager import add_to_queue
        
        queued_tracks = []
        failed_tracks = []
        
        for track in tracks:
            artist = track.get("artist", "").strip()
            title = track.get("title", "").strip()
            album = track.get("album", "").strip()
            
            if not artist or not title:
                failed_tracks.append(track)
                continue
            
            try:
                queue_item = add_to_queue(artist, title, album, download_method, priority=5)
                if queue_item:
                    queued_tracks.append(queue_item)
                else:
                    failed_tracks.append(track)
            except Exception as e:
                logging.error(f"Failed to queue {artist} - {title}: {e}")
                failed_tracks.append(track)
        
        # Log this session
        try:
            conn = get_db()
            cursor = conn.cursor()
            
            # Use the correct table name from schema (playlist_download_sessions)
            cursor.execute("""
                INSERT INTO playlist_download_sessions 
                (session_name, playlist_name, playlist_id, download_method, total_tracks, queued_count, status, user, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'in_progress', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """, (session_name, playlist_name, playlist_id, download_method, len(tracks), len(queued_tracks), 'unknown'))
            
            conn.commit()
            conn.close()
        except Exception as e:
            logging.warning(f"Failed to log playlist session: {e}")
        
        return jsonify({
            "success": True,
            "session_name": session_name,
            "playlist_name": playlist_name,
            "queued_count": len(queued_tracks),
            "failed_count": len(failed_tracks),
            "total_count": len(tracks),
            "queued_tracks": queued_tracks,
            "failed_tracks": failed_tracks
        })
    
    except Exception as e:
        logging.error(f"Failed to create playlist session: {e}", exc_info=True)
        return jsonify({"success": False, "error": f"Failed to create session: {str(e)}"}), 500

app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-production")
app.config['PERMANENT_SESSION_LIFETIME'] = 86400  # 24 hours


@app.route("/setup", methods=["GET", "POST"])
def setup():
    """Setup wizard page (first-run, full config overwrite on save)"""
    import yaml
    try:
        if request.method == "POST":
            # Build full config from form data
            nav_base_urls = request.form.getlist("nav_base_url[]")
            nav_users = request.form.getlist("nav_user[]")
            nav_passes = request.form.getlist("nav_pass[]")
            # Optional per-user fields (future: add more as needed)
            # For now, only first user gets Spotify keys from main form
            users = []
            for i in range(len(nav_base_urls)):
                user = {
                    "base_url": nav_base_urls[i],
                    "user": nav_users[i],
                    "pass": nav_passes[i],
                }
                if i == 0:
                    user["spotify_client_id"] = request.form.get("spotify_client_id", "")
                    user["spotify_client_secret"] = request.form.get("spotify_client_secret", "")
                    user["lastfm_api_key"] = request.form.get("lastfm_api_key", "")
                    user["discogs_token"] = request.form.get("discogs_token", "")
                users.append(user)

            # Always include features and weights at the bottom
            features = {
                "dry_run": False,
                "sync": True,
                "force": False,
                "verbose": False,
                "perpetual": True,
                "artist": [],
                "album_skip_days": 7,
                "album_skip_min_tracks": 1,
                "clamp_min": 0.75,
                "clamp_max": 1.25,
                "cap_top4_pct": 0.25,
                "title_sim_threshold": 0.92,
                "short_release_counts_as_match": False,
                "secondary_single_lookup_enabled": True,
                "secondary_lookup_metric": "score",
                "secondary_lookup_delta": 0.05,
                "secondary_required_strong_sources": 2,
                "median_gate_strategy": "hard",
                "use_lastfm_single": True,
                "refresh_artist_index_on_start": False,
                "discogs_min_interval_sec": 0.35,
                "include_user_ratings_on_scan": True,
                "scan_worker_threads": 4,
                "spotify_prefetch_timeout": 30,
            }
            weights = {"spotify": 0.4, "lastfm": 0.3, "age": 0.3}

            config = {
                "navidrome_users": users,
                "api_integrations": {
                    "spotify": {
                        "enabled": True,
                        "client_id": request.form.get("spotify_client_id", ""),
                        "client_secret": request.form.get("spotify_client_secret", "")
                    },
                    "lastfm": {
                        "enabled": True,
                        "api_key": request.form.get("lastfm_api_key", "")
                    },
                    "discogs": {
                        "enabled": True,
                        "token": request.form.get("discogs_token", "")
                    },
                    "musicbrainz": {"enabled": True},
                    "audiodb": {"enabled": False, "api_key": ""},
                    "google": {"enabled": False, "api_key": "", "cse_id": ""},
                    "youtube": {"enabled": False, "api_key": ""},
                },
                "qbittorrent": {
                    "enabled": False,
                    "web_url": "http://localhost:8080",
                    "username": "",
                    "password": ""
                },
                "slskd": {
                    "enabled": True,
                    "web_url": "http://localhost:5030",
                    "api_key": ""
                },
                "downloads": {
                    "folder": "/downloads/Music",
                    "incomplete_folder": "/downloads/Soulseek/Incomplete",
                    "monitor_incomplete": True
                },
                "weights": weights,
                "database": {"path": "/database/sptnr.db", "vacuum_on_start": False},
                "logging": {"level": "INFO", "file": "/config/app.log", "console": True},
                "features": features,
            }
            # Always set main navidrome section to first user for compatibility
            if users and len(users) > 0:
                config["navidrome"] = {
                    "base_url": users[0].get("base_url", ""),
                    "user": users[0].get("user", ""),
                    "pass": users[0].get("pass", ""),
                }
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                yaml.safe_dump(config, f, sort_keys=False, allow_unicode=True)
            flash("Setup updated!", "success")
            return redirect(url_for("setup"))

        # For GET, load the full config (all sections) and pass to template
        config = {}
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
        except Exception:
            config = {}
        # Extract user and integration fields for template
        nav_users = config.get("navidrome_users", [])
        # Map legacy/alternate keys to expected keys for UI
        for user in nav_users:
            if "navidrome_base_url" in user:
                user["base_url"] = user["navidrome_base_url"]
            if "navidrome_password" in user:
                user["pass"] = user["navidrome_password"]
            if "username" in user:
                user["user"] = user["username"]
        spotify_client_id = config.get("api_integrations", {}).get("spotify", {}).get("client_id", "")
        spotify_client_secret = config.get("api_integrations", {}).get("spotify", {}).get("client_secret", "")
        discogs_token = config.get("api_integrations", {}).get("discogs", {}).get("token", "")
        lastfm_api_key = config.get("api_integrations", {}).get("lastfm", {}).get("api_key", "")
        return render_template(
            "setup.html",
            nav_users=nav_users,
            spotify_client_id=spotify_client_id,
            spotify_client_secret=spotify_client_secret,
            discogs_token=discogs_token,
            lastfm_api_key=lastfm_api_key
        )
    except Exception as e:
        import logging
        logging.error(f"Error loading setup page: {e}")
        return "Setup page error", 500




# Standardized config/database/log path variables
# Always default to /config/config.yaml unless CONFIG_PATH is explicitly set (not just empty)
_env_config_path = os.environ.get("CONFIG_PATH")
if _env_config_path and _env_config_path.strip():
    CONFIG_PATH = _env_config_path
else:
    CONFIG_PATH = "/config/config.yaml"
DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")
LOG_PATH = os.environ.get("LOG_PATH", "/config/app.log")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(APP_DIR, "config", "config.yaml")

# Standardized PostgreSQL connection info
PG_HOST = os.environ.get("PG_HOST", "")
PG_PORT = os.environ.get("PG_PORT", "5432")
PG_USER = os.environ.get("PG_USER", "")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "")
PG_DATABASE = os.environ.get("PG_DATABASE", "")

# Ensure expected log files exist so the log viewer doesn't 404
def _ensure_log_file(path: str):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            with open(path, "a", encoding="utf-8"):
                pass
    except Exception as e:
        # Don't block app start; just log to stderr
        print(f"Warning: could not ensure log file {path}: {e}")

_ensure_log_file(LOG_PATH)
_ensure_log_file(os.path.join(os.path.dirname(CONFIG_PATH), "webui.log"))
_ensure_log_file(os.path.join(os.path.dirname(CONFIG_PATH), "sptnr.log"))
_ensure_log_file(os.path.join(os.path.dirname(CONFIG_PATH), "mp3scanner.log"))
_ensure_log_file(os.path.join(os.path.dirname(CONFIG_PATH), "popularity.log"))
# Note: singledetection.py has been integrated into popularity.py
# Singles detection logs go to popularity.log
_ensure_log_file(os.path.join(os.path.dirname(CONFIG_PATH), "downloads.log"))
# Log beet imports separately
_ensure_log_file(os.path.join(os.path.dirname(CONFIG_PATH), "music_import.log"))

# Global scan process tracker
scan_process = None  # Main scan process (full scan, force, artist-specific)
scan_process_navidrome = None  # Navidrome sync process
scan_process_popularity = None  # Popularity scan process
scan_process_singles = None  # Singles detection process
scan_process_combined = None  # Combined scan process (Navidrome + Popularity + Singles per artist)
scan_process_missing_releases = None  # Missing releases scan process
scan_lock = threading.Lock()

# Retry scheduler management
retry_scheduler = {
    "thread": None,
    "running": False,
    "stop_event": None  # threading.Event to signal thread to stop
}
retry_scheduler_lock = threading.Lock()

# Optional auto-import toggle placeholder (will be set after config functions are defined)
AUTO_BOOT_ND_IMPORT = None


def _write_progress_file(path: str, scan_type: str, is_running: bool, extra: dict | None = None):
    """Persist minimal scan progress state so the dashboard can show status."""
    try:
        payload = {"is_running": is_running, "scan_type": scan_type}
        if extra:
            payload.update(extra)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception as e:
        logging.debug(f"Failed to write progress file {path}: {e}")


def _write_progress_with_current_artist(path: str, scan_type: str, is_running: bool, extra: dict | None = None):
    """
    Write progress file while preserving current_artist from existing file.
    This ensures resume functionality works even after scan completion.
    
    Args:
        path: Path to progress file
        scan_type: Type of scan (e.g., 'navidrome_scan', 'popularity_scan')
        is_running: Whether scan is currently running
        extra: Additional data to include in progress file
    """
    # Try to preserve current_artist from existing progress file
    current_artist = None
    try:
        with open(path, 'r') as f:
            existing_progress = json.load(f)
            current_artist = existing_progress.get('current_artist')
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass  # File doesn't exist or is invalid, that's okay
    
    # Add current_artist to extra data if it exists
    if current_artist:
        if extra is None:
            extra = {}
        extra['current_artist'] = current_artist
    
    # Write the progress file with preserved current_artist
    _write_progress_file(path, scan_type, is_running, extra)


def _is_process_alive(proc):
    """Helper to check if a process/thread is alive"""
    if proc is None:
        return False
    if isinstance(proc, dict):
        thread = proc.get('thread')
        if thread is None:
            return False
        if hasattr(thread, 'is_alive'):
            return thread.is_alive()
        if hasattr(thread, 'poll'):
            return thread.poll() is None
        return False
    if hasattr(proc, 'is_alive'):
        return proc.is_alive()
    if hasattr(proc, 'poll'):
        return proc.poll() is None
    return False


def _validate_and_cleanup_progress_file(progress_file: str, process_ref=None, max_age_hours: int = 2):
    """
    Validate a progress file and clean it up if it's stale.
    
    Returns the progress data if valid, None if stale/invalid.
    
    Args:
        progress_file: Path to the progress file
        process_ref: Optional process/thread reference to check if actually running
        max_age_hours: Maximum age in hours before considering a running scan as stuck
    """
    if not os.path.exists(progress_file):
        return None
    
    try:
        with open(progress_file, 'r') as f:
            progress = json.load(f)
        
        # If scan says it's not running, return as-is
        if not progress.get("is_running", False):
            return progress
        
        # If we have a process reference, verify it's actually alive using helper
        if process_ref is not None:
            is_alive = _is_process_alive(process_ref)
            
            # If process is dead but file says running, clean it up
            if not is_alive:
                logging.warning(f"Progress file {progress_file} says running but process is dead - cleaning up")
                progress["is_running"] = False
                progress["status"] = "error"
                with open(progress_file, 'w') as f:
                    json.dump(progress, f)
                return progress
        
        # Check file modification time to detect truly stuck scans
        file_mtime = os.path.getmtime(progress_file)
        current_time = time.time()
        age_hours = (current_time - file_mtime) / 3600
        
        if age_hours > max_age_hours:
            logging.warning(f"Progress file {progress_file} is {age_hours:.1f} hours old - assuming stuck scan, cleaning up")
            progress["is_running"] = False
            progress["status"] = "timeout"
            with open(progress_file, 'w') as f:
                json.dump(progress, f)
            return progress
        
        return progress
    except Exception as e:
        logging.error(f"Error validating progress file {progress_file}: {e}")
        return None


def _monitor_process_for_progress(proc: subprocess.Popen, progress_path: str, scan_type: str):
    """Wait for a subprocess and mark its progress file as complete."""
    try:
        # Capture output while process runs
        output_lines = []
        if proc.stdout:
            for line in iter(proc.stdout.readline, ''):
                if not line:
                    break
                line = line.strip()
                if line:
                    output_lines.append(line)
                    logging.info(f"[{scan_type}] {line}")
        
        # Wait for process to complete
        returncode = proc.wait()
        
        # Capture any remaining output
        if proc.stdout:
            remaining = proc.stdout.read()
            if remaining:
                for line in remaining.strip().split('\n'):
                    if line.strip():
                        output_lines.append(line.strip())
                        logging.info(f"[{scan_type}] {line.strip()}")
        
        # Mark as complete with error info if failed
        result = {
            "exit_code": returncode,
            "completed_at": datetime.now().isoformat()
        }
        
        if returncode != 0:
            error_msg = f"Process exited with code {returncode}"
            if output_lines:
                # Include last few lines of output as error context
                result["error"] = error_msg
                result["output_tail"] = output_lines[-10:]
            logging.error(f"{scan_type} failed: {error_msg}")
            if output_lines:
                logging.error(f"Last output lines: {output_lines[-5:]}")
        else:
            logging.info(f"{scan_type} completed successfully")
        
        _write_progress_file(progress_path, scan_type, False, result)
    except Exception as e:
        logging.error(f"Progress monitor failed for {scan_type}: {e}", exc_info=True)
        _write_progress_file(progress_path, scan_type, False, {
            "exit_code": -1,
            "error": str(e)
        })


def _start_boot_navidrome_import():
    """Start a Navidrome metadata-only import in the background on startup.

    Uses force=False so it only fills missing metadata, and sets the
    SPTNR_SKIP_SINGLES flag so rating/single detection cannot run during this pass.
    
    Resumes from the last scanned artist if available, otherwise starts from the beginning.
    """
    global scan_process_navidrome

    # Avoid duplicate launches if already running
    if scan_process_navidrome and isinstance(scan_process_navidrome, dict):
        t = scan_process_navidrome.get("thread")
        if t and t.is_alive():
            logging.info("Navidrome import already running; boot kickoff skipped")
            return

    def run_import():
        os.environ["SPTNR_SKIP_SINGLES"] = "1"
        progress_path = os.path.join(os.path.dirname(DB_PATH), "navidrome_scan_progress.json")
        checkpoint_path = os.path.join(os.path.dirname(DB_PATH), "navidrome_scan_checkpoint.json")
        
        try:
            logging.info("[BOOT] Starting Navidrome import-only scan (missing-only)")
            _write_progress_file(progress_path, "navidrome_scan", True, {"status": "starting", "source": "boot"})
            
            artist_map = build_artist_index()
            artists = list(artist_map.items())
            total = len(artists)
            
            # Check if we have a checkpoint from a previous scan
            start_idx = 0
            last_scanned_artist = None
            if os.path.exists(checkpoint_path):
                try:
                    with open(checkpoint_path, 'r') as f:
                        checkpoint = json.load(f)
                        last_scanned_artist = checkpoint.get("last_scanned_artist")
                        if last_scanned_artist:
                            # Find the index of the last scanned artist
                            for idx, (artist_name, _) in enumerate(artists):
                                if artist_name == last_scanned_artist:
                                    start_idx = idx + 1  # Start from the next artist
                                    logging.info(f"[BOOT] Resuming Navidrome scan from artist index {start_idx} (after '{last_scanned_artist}')")
                                    break
                except Exception as e:
                    logging.warning(f"[BOOT] Error reading checkpoint: {e}, starting from beginning")
            
            # Scan remaining artists
            for idx in range(start_idx, total):
                artist_name, info = artists[idx]
                scan_artist_to_db(artist_name, info.get("id"), verbose=False, force=False, processed_artists=idx+1, total_artists=total)
                
                # Update checkpoint with the last scanned artist
                try:
                    with open(checkpoint_path, 'w') as f:
                        json.dump({"last_scanned_artist": artist_name}, f)
                except Exception as e:
                    logging.warning(f"[BOOT] Error saving checkpoint: {e}")
            
            # Clear checkpoint when scan completes
            if os.path.exists(checkpoint_path):
                os.remove(checkpoint_path)
            
            _write_progress_file(progress_path, "navidrome_scan", False, {"status": "complete", "exit_code": 0, "source": "boot"})
            logging.info("[BOOT] Navidrome import-only scan completed")
        except Exception as e:
            logging.error(f"[BOOT] Error in Navidrome import-only scan: {e}", exc_info=True)
            _write_progress_file(progress_path, "navidrome_scan", False, {"status": "error", "error": str(e), "exit_code": 1, "source": "boot"})
        finally:
            os.environ.pop("SPTNR_SKIP_SINGLES", None)
            scan_process_navidrome = None

    thread = threading.Thread(target=run_import, daemon=True)
    thread.start()
    scan_process_navidrome = {"thread": thread, "type": "navidrome_boot"}
    logging.info("Boot Navidrome import thread started")

def _read_yaml(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
            return yaml.safe_load(content) or {}, content
    except FileNotFoundError:
        return {}, ""
    except yaml.YAMLError:
        return {}, ""


def _baseline_config():
    """Return a config structure with sensible defaults for first-run."""
    existing, _ = _read_yaml(CONFIG_PATH)
    if existing:
        return existing

    sample, _ = _read_yaml(DEFAULT_CONFIG_PATH)
    if sample:
        return sample

    return {
        "navidrome": {"base_url": "", "user": "", "pass": ""},
        "api_integrations": {
            "spotify": {"enabled": False, "client_id": "", "client_secret": ""},
            "lastfm": {"enabled": False, "api_key": ""},
            "discogs": {"enabled": False, "token": ""},
            "musicbrainz": {"enabled": True},
            "audiodb": {"enabled": False, "api_key": ""},
            "google": {"enabled": False, "api_key": "", "cse_id": ""},
            "youtube": {"enabled": False, "api_key": ""}
        },
        "qbittorrent": {
            "enabled": False,
            "web_url": "http://localhost:8080",
            "username": "",
            "password": ""
        },
        "slskd": {
            "enabled": False,
            "web_url": "http://localhost:5030",
            "api_key": ""
        },
        "bookmarks": {
            "enabled": True,
            "max_bookmarks": 100,
            "custom_links": []
        },
        "downloads": {
            "folder": "/downloads/Music"
        },
        "weights": {"spotify": 0.4, "lastfm": 0.3, "age": 0.3},
        "database": {"path": DB_PATH, "vacuum_on_start": False},
        "logging": {"level": "INFO", "file": LOG_PATH, "console": True},
        "features": {
            "dry_run": False,
            "sync": True,
            "force": False,
            "verbose": False,
            "perpetual": True,
            "auto_boot_navidrome_scan": False,
            "artist": [],
            "album_skip_days": 7,
            "album_skip_min_tracks": 1,
            "clamp_min": 0.75,
            "clamp_max": 1.25,
            "cap_top4_pct": 0.25,
            "title_sim_threshold": 0.92,
            "short_release_counts_as_match": False,
            "secondary_single_lookup_enabled": True,
            "secondary_lookup_metric": "score",
            "secondary_lookup_delta": 0.05,
            "secondary_required_strong_sources": 2,
            "median_gate_strategy": "hard",
            "use_lastfm_single": True,
            "refresh_artist_index_on_start": False,
            "discogs_min_interval_sec": 0.35,
            "include_user_ratings_on_scan": True,
            "scan_worker_threads": 4,
            "spotify_prefetch_timeout": 30
        }
    }


# Initialize auto-boot import setting from config
def _get_auto_boot_import_setting():
    """Read auto boot Navidrome scan setting from config, with env var override."""
    # Environment variable takes precedence if set to disable
    if os.environ.get("SPTNR_DISABLE_BOOT_ND_IMPORT") == "1":
        return False
    
    # Read from config file
    try:
        cfg = get_config()
        features = cfg.get("features", {})
        return features.get("auto_boot_navidrome_scan", False)
    except (FileNotFoundError, yaml.YAMLError, KeyError, AttributeError) as e:
        logging.debug(f"Unable to read auto_boot_navidrome_scan from config: {e}")
        return False  # Default to disabled if config can't be read

AUTO_BOOT_ND_IMPORT = _get_auto_boot_import_setting()


# Kick off Navidrome metadata-only import at startup (missing-only)
if AUTO_BOOT_ND_IMPORT:
    try:
        _start_boot_navidrome_import()
    except Exception as e:
        logging.error(f"Failed to start boot Navidrome import: {e}")


def _needs_setup(cfg=None):
    cfg = cfg if cfg is not None else _read_yaml(CONFIG_PATH)[0]
    
    # Check for navidrome_users list first
    nav_users = cfg.get("navidrome_users", [])
    if isinstance(nav_users, list) and nav_users:
        # At least one user with all required fields
        first_user = nav_users[0]
        required = [first_user.get("base_url"), first_user.get("user"), first_user.get("pass")]
        return any(not (v and str(v).strip()) for v in required)
    
    # Fall back to single navidrome entry
    nav = cfg.get("navidrome", {}) or {}
    required = [nav.get("base_url"), nav.get("user"), nav.get("pass")]
    return any(not (v and str(v).strip()) for v in required)


def _authenticate_navidrome(username, password):
    """Authenticate against Navidrome API"""
    cfg = get_config()
    
    # Check navidrome_users list first
    nav_users = cfg.get("navidrome_users", [])
    if isinstance(nav_users, list) and nav_users:
        for user_config in nav_users:
            if user_config.get("user") == username:
                base_url = user_config.get("base_url", "")
                nav_user = user_config.get("user", "")
                nav_pass = user_config.get("pass", "")
                
                if password == nav_pass:
                    # Verify against Navidrome API
                    try:
                        import requests
                        import hashlib
                        salt = "sptnr-auth"
                        token = hashlib.md5((password + salt).encode()).hexdigest()
                        auth_url = f"{base_url}/rest/ping?u={nav_user}&t={token}&s={salt}&v=1.16.0&c=sptnr"
                        resp = requests.get(auth_url, timeout=5)
                        if resp.status_code == 200 and "ok" in resp.text.lower():
                            return True
                    except:
                        # If API check fails, fall back to password match
                        return True
                return False
    
    # Fall back to single navidrome entry
    nav = cfg.get("navidrome", {})
    if nav.get("user") == username and nav.get("pass") == password:
        try:
            import requests
            import hashlib
            base_url = nav.get("base_url", "")
            salt = "sptnr-auth"
            token = hashlib.md5((password + salt).encode()).hexdigest()
            auth_url = f"{base_url}/rest/ping?u={username}&t={token}&s={salt}&v=1.16.0&c=sptnr"
            resp = requests.get(auth_url, timeout=5)
            if resp.status_code == 200 and "ok" in resp.text.lower():
                return True
        except:
            # If API check fails, fall back to password match
            return True
    
    return False


def login_required(f):
    """Decorator to require login for routes"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Check if config exists - if not, allow access (setup mode)
        if not os.path.exists(CONFIG_PATH):
            return f(*args, **kwargs)
        
        cfg = get_config()
        
        # If setup is needed, redirect to setup
        if _needs_setup(cfg):
            return redirect(url_for('setup'))
        
        # Check if user is logged in
        if 'username' not in session:
            return redirect(url_for('login', next=request.url))
        
        return f(*args, **kwargs)
    return decorated_function


@app.context_processor
def inject_custom_bookmarks():
    """Inject custom bookmark links into all templates"""
    try:
        cfg = get_config()
        custom_links = cfg.get('bookmarks', {}).get('custom_links', [])
        return {'custom_bookmark_links': custom_links}
    except Exception:
        return {'custom_bookmark_links': []}


@app.before_request
def enforce_setup_wizard():
    try:
        exempt = {"setup", "static", "config_edit", "config_editor", "login", "logout"}
        # Exempt all API routes from login requirements
        if request.path.startswith("/api/"):
            return
        if not request.endpoint or request.endpoint in exempt or request.endpoint.startswith("static"):
            try:
                exempt = {"setup", "static", "config_edit", "config_editor", "login", "logout"}
                if not request.endpoint or request.endpoint in exempt or request.endpoint.startswith("static"):
                    return

                # If config doesn't exist or is incomplete, redirect to setup wizard
                cfg = get_config()
                from os.path import exists
                if not exists(CONFIG_PATH) or _needs_setup(cfg):
                    if request.endpoint != "setup":
                        return redirect(url_for("setup"))
                    return

                # Only require login if config exists and is valid
                if 'username' not in session:
                    if request.endpoint != "login":
                        return redirect(url_for("login"))
            except Exception as e:
                logging.error(f"Error in enforce_setup_wizard: {e}")
                import traceback
                traceback.print_exc()
                # Don't block the request, let it fail naturally so we can see the real error
                pass
        # If setup is complete and not logged in, redirect to login
        if 'username' not in session:
            if request.endpoint != "login":
                return redirect(url_for("login"))
    except Exception as e:
        logging.error(f"Error in enforce_setup_wizard: {e}")
        import traceback
        traceback.print_exc()
        # Don't block the request, let it fail naturally so we can see the real error
        pass


# Track if schema has been updated this session
_schema_updated = False


def get_db():
    """Get a database connection (PostgreSQL if configured, else SQLite)."""
    global _schema_updated
    if PG_HOST and PG_USER and PG_DATABASE:
        # Connect to PostgreSQL
        if 'psycopg2' not in globals():
            raise RuntimeError("psycopg2 not available - install with: pip install psycopg2-binary")
        conn = psycopg2.connect(  # type: ignore[name-defined]
            host=PG_HOST,
            port=PG_PORT,
            user=PG_USER,
            password=PG_PASSWORD,
            dbname=PG_DATABASE,
            cursor_factory=psycopg2.extras.RealDictCursor  # type: ignore[name-defined]
        )
        return conn
    else:
        # Fallback to SQLite
        db_dir = os.path.dirname(DB_PATH)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        if not _schema_updated:
            update_schema(DB_PATH)
            _schema_updated = True
        conn = sqlite3.connect(DB_PATH, timeout=120.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn


@app.route("/debug/static")
def debug_static():
    """Debug endpoint to check static file configuration"""
    import os
    response = {
        "app_root": app_root,
        "static_folder": static_folder,
        "static_folder_exists": os.path.isdir(static_folder),
        "app_static_folder": app.static_folder,
        "app_static_url_path": app.static_url_path,
    }
    
    if os.path.isdir(static_folder):
        try:
            files = {}
            for root, dirs, filenames in os.walk(static_folder):
                rel_root = os.path.relpath(root, static_folder)
                if rel_root not in files:
                    files[rel_root] = []
                files[rel_root].extend(filenames)
            response["files"] = files
        except Exception as e:
            response["file_error"] = str(e)
    
    return jsonify(response)


@app.route("/")
def index():
    """Redirect to dashboard"""
    return redirect(url_for("dashboard"))


@app.route("/dashboard")
def dashboard():
    """Main dashboard with statistics"""
    try:
        conn = get_db()
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(DISTINCT artist) FROM tracks")
        result = cursor.fetchone()
        artist_count = result[0] if result else 0

        cursor.execute("SELECT COUNT(DISTINCT album) FROM tracks")
        result = cursor.fetchone()
        album_count = result[0] if result else 0

        cursor.execute("SELECT COUNT(*) FROM tracks")
        result = cursor.fetchone()
        track_count = result[0] if result else 0

        cursor.execute("SELECT COUNT(*) FROM tracks WHERE stars = 5")
        result = cursor.fetchone()
        five_star_count = result[0] if result else 0

        cursor.execute("SELECT COUNT(*) FROM tracks WHERE is_single = 1")
        result = cursor.fetchone()
        singles_count = result[0] if result else 0

        conn.close()

        # Get recent scans from scan_history table
        from scan_history import get_recent_album_scans
        recent_scans = get_recent_album_scans(limit=10)

        with scan_lock:
            web_ui_running = scan_process is not None and scan_process.poll() is None

        # Check if background scan from start.py is running
        lock_file_path = os.path.join(os.path.dirname(CONFIG_PATH), ".scan_lock")
        background_running = os.path.exists(lock_file_path)

        scan_running = web_ui_running or background_running

        # Get Navidrome users from config
        cfg = get_config()
        nav_users_list = cfg.get("navidrome_users", [])
        if not nav_users_list and cfg.get("navidrome"):
            # Single user mode - convert to list format for consistency
            nav_users_list = [cfg.get("navidrome")]

        db_path = cfg.get("database", {}).get("path", "/database/sptnr.db")
        dashboard_template = "dashboard_external.html" if db_path != "/database/sptnr.db" else "dashboard.html"

        return render_template(dashboard_template,
                             artist_count=artist_count,
                             album_count=album_count,
                             track_count=track_count,
                             five_star_count=five_star_count,
                             singles_count=singles_count,
                             recent_scans=recent_scans,
                             scan_running=scan_running,
                             nav_users=nav_users_list)
    except Exception as e:
        logging.error(f"Dashboard error: {e}")
        import traceback
        traceback.print_exc()
        cfg = get_config()
        db_path = cfg.get("database", {}).get("path", "/database/sptnr.db")
        dashboard_template = "dashboard_external.html" if db_path != "/database/sptnr.db" else "dashboard.html"
        return render_template(dashboard_template,
                             artist_count=0,
                             album_count=0,
                             track_count=0,
                             five_star_count=0,
                             singles_count=0,
                             recent_scans=[],
                             scan_running=False,
                             nav_users=[],
                             error=str(e))


def convert_row_to_json_serializable(obj):
    """
    Recursively convert database Row objects and other non-JSON-serializable types to JSON-safe formats.
    Handles: Row objects, datetime, Decimal, Jinja2 Undefined, None, etc.
    """
    from jinja2 import Undefined
    
    if obj is None:
        return None
    
    # Handle Jinja2 Undefined objects
    if isinstance(obj, Undefined):
        return None
    
    # Convert Row objects to dicts
    if hasattr(obj, 'keys') and not isinstance(obj, dict):  # sqlite3.Row object
        obj = dict(obj)
    
    # Recursively process dicts
    if isinstance(obj, dict):
        return {k: convert_row_to_json_serializable(v) for k, v in obj.items()}
    
    # Recursively process lists
    if isinstance(obj, (list, tuple)):
        return [convert_row_to_json_serializable(item) for item in obj]
    
    # Convert datetime to ISO format string
    if isinstance(obj, datetime):
        return obj.isoformat()
    
    # Convert Decimal to float
    try:
        from decimal import Decimal
        if isinstance(obj, Decimal):
            return float(obj)
    except ImportError:
        pass
    
    # Return as-is if it's already JSON-serializable (str, int, float, bool, None)
    return obj


@app.route("/artists")
def artists():
    """List all album artists (not track artists). Only show albums where they are the album artist.
    Exception: Various Artists shows all compilation albums and their tracks.
    Filter: Only show artists with at least 1 album or EP (excludes artists with only 0 albums/EPs)."""
    conn = get_db()
    cursor = conn.cursor()
    
    # Get total counts for all tracks (including those without artist info)
    cursor.execute("""
        SELECT 
            COUNT(DISTINCT album) as album_count,
            COUNT(*) as track_count,
            COALESCE(SUM(CASE WHEN is_single = 1 THEN 1 ELSE 0 END), 0) as single_count
        FROM tracks
    """)
    total_stats = cursor.fetchone()
    
    # Filter artists to show only those with at least one album or EP
    # Use COALESCE to fall back to artist field when album_artist is empty
    # Use NULLIF to treat empty strings as NULL for proper COALESCE behavior
    try:
        cursor.execute("""
            SELECT 
                COALESCE(NULLIF(album_artist, ''), artist) as display_name,
                COALESCE(NULLIF(album_artist, ''), artist) as link_artist,
                COUNT(DISTINCT album) as album_count,
                COUNT(*) as track_count,
                COALESCE(SUM(CASE WHEN is_single = 1 THEN 1 ELSE 0 END), 0) as single_count,
                MAX(last_scanned) as last_updated
            FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) IS NOT NULL 
                AND COALESCE(NULLIF(album_artist, ''), artist) != ''
            GROUP BY COALESCE(NULLIF(album_artist, ''), artist) COLLATE NOCASE
            HAVING album_count > 0
            ORDER BY display_name COLLATE NOCASE
        """)
        artists_data = [dict(row) for row in cursor.fetchall()]
    except:
        # Fallback for databases without album_artist column
        cursor.execute("""
            SELECT 
                artist as display_name,
                artist as link_artist,
                COUNT(DISTINCT album) as album_count,
                COUNT(*) as track_count,
                COALESCE(SUM(CASE WHEN is_single = 1 THEN 1 ELSE 0 END), 0) as single_count,
                MAX(last_scanned) as last_updated
            FROM tracks
            WHERE artist IS NOT NULL AND artist != ''
            GROUP BY artist COLLATE NOCASE
            HAVING album_count > 0
            ORDER BY display_name COLLATE NOCASE
        """)
        artists_data = [dict(row) for row in cursor.fetchall()]
    
    # Sort by display_name, handling special characters and numbers that should be grouped under '#'
    def get_sort_key(artist):
        name = artist.get('display_name', '')
        if not name:
            return ('~', '')  # Sort empty names to the end
        first_char = name[0].upper()
        # If first character is A-Z, use it; otherwise use '#' for sorting
        if first_char in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
            return (first_char, name.lower())
        else:
            return ('#', name.lower())
    
    artists_data = sorted(artists_data, key=get_sort_key)
    
    conn.close()
    
    return render_template("artists.html", artists=artists_data, total_stats=total_stats, DB_PATH=DB_PATH)


@app.route("/search")
def search():
    """Search page for artists, albums, and tracks"""
    return render_template("search.html")


@app.route("/api/search", methods=["POST"])
def api_search():
    """API endpoint to search the library for artists, albums, and tracks"""
    try:
        data = request.get_json()
        query = data.get("query", "").strip().lower()
        
        if not query or len(query) < 2:
            return jsonify({"error": "Search query must be at least 2 characters"}), 400
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Prepare search pattern for LIKE queries
        search_pattern = f"%{query}%"
        
        # Search artists
        cursor.execute("""
            SELECT 
                artist as name,
                COUNT(DISTINCT album) as album_count,
                COUNT(*) as track_count
            FROM tracks
            WHERE LOWER(artist) LIKE LOWER(?)
            GROUP BY artist
            ORDER BY track_count DESC
            LIMIT 20
        """, (search_pattern,))
        artists_results = [
            {
                "name": row["name"],
                "album_count": row["album_count"],
                "track_count": row["track_count"]
            }
            for row in cursor.fetchall()
        ]
        
        # Search albums
        cursor.execute("""
            SELECT 
                artist,
                album,
                COUNT(*) as track_count,
                AVG(stars) as avg_stars
            FROM tracks
            WHERE LOWER(album) LIKE LOWER(?)
            GROUP BY artist, album
            ORDER BY track_count DESC
            LIMIT 20
        """, (search_pattern,))
        albums_results = [
            {
                "artist": row["artist"],
                "album": row["album"],
                "track_count": row["track_count"],
                "avg_stars": row["avg_stars"]
            }
            for row in cursor.fetchall()
        ]
        
        # Search tracks
        cursor.execute("""
            SELECT 
                id,
                title,
                artist,
                album,
                stars
            FROM tracks
            WHERE LOWER(title) LIKE LOWER(?) OR LOWER(artist) LIKE LOWER(?)
            ORDER BY stars DESC, title COLLATE NOCASE
            LIMIT 50
        """, (search_pattern, search_pattern))
        tracks_results = [
            {
                "id": row["id"],
                "title": row["title"],
                "artist": row["artist"],
                "album": row["album"],
                "stars": row["stars"]
            }
            for row in cursor.fetchall()
        ]
        
        conn.close()
        
        return jsonify({
            "artists": artists_results,
            "albums": albums_results,
            "tracks": tracks_results
        })
    
    except Exception as e:
        logging.error(f"Search error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/artist/<path:name>")
def artist_detail(name):
    """View artist details and albums"""
    try:
        # URL decode the artist name
        from urllib.parse import unquote
        name = unquote(name)
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Get albums for this artist with type information
        # Query by COALESCE(album_artist, artist) to handle cases where album_artist is empty
        # Use NULLIF to treat empty strings as NULL for proper COALESCE behavior
        cursor.execute("""
            SELECT 
                album,
                COUNT(*) as track_count,
                AVG(stars) as avg_stars,
                COALESCE(SUM(CASE WHEN is_single = 1 THEN 1 ELSE 0 END), 0) as singles_count,
                MAX(last_scanned) as last_updated,
                MIN(year) as album_year,
                MAX(spotify_album_type) as album_type,
                MAX(album_artist) as album_artist,
                MAX(musicbrainz_album_mbid) as musicbrainz_album_mbid,
                MAX(discogs_release_id) as discogs_release_id
            FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = ?
            GROUP BY album
            ORDER BY (album_year IS NULL), album_year DESC, album COLLATE NOCASE
        """, (name,))
        albums_data = cursor.fetchall()
        
        # Get artist stats with additional metrics
        # Query by COALESCE(album_artist, artist) to handle cases where album_artist is empty
        # Use NULLIF to treat empty strings as NULL for proper COALESCE behavior
        cursor.execute("""
            SELECT 
                COUNT(*) as track_count,
                COUNT(DISTINCT album) as album_count,
                AVG(stars) as avg_stars,
                SUM(CASE WHEN stars = 5 THEN 1 ELSE 0 END) as five_star_count,
                SUM(COALESCE(duration, 0)) as total_duration,
                MIN(year) as earliest_year,
                MAX(year) as latest_year,
                MAX(musicbrainz_artist_id) as musicbrainz_artist_id,
                MAX(spotify_artist_id) as spotify_artist_id,
                MAX(lastfm_artist_mbid) as lastfm_artist_mbid,
                MAX(discogs_artist_id) as discogs_artist_id,
                MAX(discogs_release_id) as discogs_release_id
            FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = ?
        """, (name,))
        
        artist_stats = cursor.fetchone()
        
        # If artist ID not found in tracks, try to get it from album MusicBrainz IDs as fallback
        if artist_stats and not dict(artist_stats).get('musicbrainz_artist_id'):
            try:
                # Look for any album with a MusicBrainz release ID
                cursor.execute("""
                    SELECT DISTINCT musicbrainz_album_mbid 
                    FROM tracks 
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = ? 
                    AND musicbrainz_album_mbid IS NOT NULL AND musicbrainz_album_mbid != ''
                    LIMIT 1
                """, (name,))
                album_mbid_row = cursor.fetchone()
                
                if album_mbid_row:
                    album_mbid = album_mbid_row['musicbrainz_album_mbid']
                    
                    # Fetch the artist ID from this album's MusicBrainz release
                    try:
                        import requests
                        headers = {"User-Agent": MUSICBRAINZ_USER_AGENT}
                        release_url = f"https://musicbrainz.org/ws/2/release/{album_mbid}"
                        params = {"fmt": "json", "inc": "artist-credits"}
                        resp = requests.get(release_url, params=params, headers=headers, timeout=5)
                        
                        if resp.status_code == 200:
                            release_data = resp.json()
                            artist_credits = release_data.get("artist-credit", [])
                            if artist_credits:
                                # Get the first artist credit
                                first_artist = artist_credits[0]
                                if isinstance(first_artist, dict):
                                    artist_id = first_artist.get("artist", {}).get("id")
                                    if artist_id:
                                        logging.info(f"Found artist ID {artist_id} for {name} via album {album_mbid}")
                                        # Update stats dict with the found artist ID
                                        artist_stats = dict(artist_stats) if artist_stats else {}
                                        artist_stats['musicbrainz_artist_id'] = artist_id
                                        
                                        # Save to database so it persists
                                        try:
                                            cursor.execute("""
                                                UPDATE tracks
                                                SET musicbrainz_artist_id = ?, lastfm_artist_mbid = ?
                                                WHERE COALESCE(NULLIF(album_artist, ''), artist) = ?
                                                AND (musicbrainz_artist_id IS NULL OR musicbrainz_artist_id = '')
                                            """, (artist_id, artist_id, name))
                                            conn.commit()
                                            logging.debug(f"Saved artist ID {artist_id} to {name} tracks")
                                        except Exception as e:
                                            logging.debug(f"Failed to save artist ID to database: {e}")
                    except Exception as e:
                        logging.debug(f"Failed to get artist ID from album MBID: {e}")
            except Exception as e:
                logging.debug(f"Fallback artist ID lookup failed: {e}")
        
        # Get missing releases from database cache
        cursor.execute("""
            SELECT release_id, title, primary_type, first_release_date, cover_art_url, category
            FROM missing_releases
            WHERE artist = ?
            ORDER BY first_release_date DESC
        """, (name,))
        missing_releases_data = cursor.fetchall()

        # Top tracks by z-score (fallback to popularity ordering if z-score column is unavailable)
        try:
            cursor.execute("""
                SELECT
                    id,
                    title,
                    album,
                    COALESCE(popularity_score, 0) as popularity_score,
                    COALESCE(stars, 0) as stars,
                    COALESCE(artist_z_score, 0) as artist_z_score,
                    COALESCE(is_single, 0) as is_single,
                    COALESCE(track_number, 0) as track_number,
                    COALESCE(disc_number, 1) as disc_number,
                    COALESCE(duration, 0) as duration
                FROM tracks
                WHERE COALESCE(NULLIF(album_artist, ''), artist) = ?
                ORDER BY COALESCE(artist_z_score, 0) DESC, COALESCE(popularity_score, 0) DESC
                LIMIT 10
            """, (name,))
            top_tracks = cursor.fetchall()
        except Exception:
            cursor.execute("""
                SELECT
                    id,
                    title,
                    album,
                    COALESCE(popularity_score, 0) as popularity_score,
                    COALESCE(stars, 0) as stars,
                    0 as artist_z_score,
                    COALESCE(is_single, 0) as is_single,
                    COALESCE(track_number, 0) as track_number,
                    COALESCE(disc_number, 1) as disc_number,
                    COALESCE(duration, 0) as duration
                FROM tracks
                WHERE COALESCE(NULLIF(album_artist, ''), artist) = ?
                ORDER BY COALESCE(popularity_score, 0) DESC, COALESCE(stars, 0) DESC
                LIMIT 10
            """, (name,))
            top_tracks = cursor.fetchall()

        # Albums where this artist appears as a featured/track artist but is not the album artist
        cursor.execute("""
            SELECT
                album,
                COALESCE(NULLIF(album_artist, ''), artist) as album_artist,
                COUNT(*) as track_count,
                AVG(COALESCE(stars, 0)) as avg_stars,
                MIN(year) as album_year,
                MAX(spotify_album_type) as album_type,
                MAX(last_scanned) as last_updated
            FROM tracks
            WHERE artist = ?
              AND COALESCE(NULLIF(album_artist, ''), artist) != ?
            GROUP BY album, COALESCE(NULLIF(album_artist, ''), artist)
            ORDER BY (album_year IS NULL), album_year DESC, album COLLATE NOCASE
        """, (name, name))
        appears_on_albums = cursor.fetchall()
        
        # Get potential compilation albums using cached compilation detection from background scan
        # Combined with fallback local heuristics in case scan hasn't run yet
        # IMPORTANT: Filter by album artist identity (not track artist) so featured appearances
        # are handled separately under "Appears On".
        cursor.execute("""
            SELECT 
                album,
                COUNT(*) as track_count,
                AVG(stars) as avg_stars,
                COALESCE(SUM(CASE WHEN is_single = 1 THEN 1 ELSE 0 END), 0) as singles_count,
                MAX(last_scanned) as last_updated,
                MIN(year) as album_year,
                MAX(spotify_album_type) as album_type,
                MAX(album_artist) as album_artist,
                MAX(is_compilation) as is_compilation
            FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = ?
            GROUP BY album
            ORDER BY (album_year IS NULL), album_year DESC, album COLLATE NOCASE
        """, (name,))
        potential_albums = cursor.fetchall()
        
        # Filter to only include albums that are actually compilations
        # Use cached is_compilation field from background scan, with fallback to local heuristics
        compilation_albums = []
        
        for album_row in potential_albums:
            album_artist = row_get(album_row, 'album_artist', '')
            spotify_type = row_get(album_row, 'album_type', '')
            is_compilation_cached = row_get(album_row, 'is_compilation')
            
            # Primary method: use cached compilation detection from background scan
            if is_compilation_cached:
                compilation_albums.append(album_row)
            # Fallback to local heuristics in case scan hasn't run yet
            elif album_artist and album_artist.lower() in ('various artists', 'various', 'compilation', 'soundtrack'):
                compilation_albums.append(album_row)
            elif spotify_type and spotify_type.lower() == 'compilation':
                # Also check Spotify's designation as backup
                compilation_albums.append(album_row)
        
        # Get artist metadata (country, image, bio) from artists table if it exists
        artist_country = None
        artist_image_url = None
        artist_bio = None
        try:
            cursor.execute("SELECT country, image_url, bio FROM artists WHERE name = ?", (name,))
            artist_row = cursor.fetchone()
            if artist_row:
                artist_country = artist_row[0] if artist_row[0] else None
                artist_image_url = artist_row[1] if artist_row[1] else None
                artist_bio = artist_row[2] if artist_row[2] else None
        except Exception as e:
            logging.debug(f"Error fetching artist metadata: {e}")
        
        conn.close()
        
        # Convert Row to dict for template access with defaults
        if artist_stats:
            artist_stats = dict(artist_stats)
        else:
            # Provide default values if no stats found (artist has no tracks)
            artist_stats = {
                'track_count': 0,
                'album_count': 0,
                'avg_stars': None,
                'five_star_count': 0,
                'total_duration': 0,
                'earliest_year': None,
                'latest_year': None,
                'musicbrainz_artist_id': None,
                'spotify_artist_id': None,
                'lastfm_artist_mbid': None,
                'discogs_artist_id': None,
                'discogs_release_id': None
            }
        
        # Categorize discovered albums by type
        albums_by_category = {
            "album": [],
            "ep": [],
            "single": [],
            "compilation": [],
            "unknown": []
        }
        
        # Track which albums we've already categorized to avoid duplicates
        categorized_albums = set()
        
        for album in albums_data:
            album_dict = dict(album)
            album_dict['is_missing'] = False  # Mark as discovered
            album_type = (album_dict.get("album_type") or "").lower()
            track_count = album_dict.get("track_count", 0)
            album_name = album_dict.get("album", "")
            album_artist = (album_dict.get("album_artist") or "").lower()
            
            # First check: if album_artist is a compilation-type value, mark as compilation
            if album_artist and album_artist in ('various artists', 'various', 'compilation', 'soundtrack'):
                albums_by_category["compilation"].append(album_dict)
                categorized_albums.add(album_name)
            # Categorize based on spotify_album_type and track count
            elif album_type and 'compilation' in album_type.lower():
                albums_by_category["compilation"].append(album_dict)
                categorized_albums.add(album_name)
            elif album_type == "album" or (not album_type and track_count > 6):
                albums_by_category["album"].append(album_dict)
                categorized_albums.add(album_name)
            elif album_type == "ep" or (not album_type and 3 <= track_count <= 6):
                albums_by_category["ep"].append(album_dict)
                categorized_albums.add(album_name)
            elif album_type == "single" or (not album_type and track_count < 3):
                albums_by_category["single"].append(album_dict)
                categorized_albums.add(album_name)
            else:
                albums_by_category["unknown"].append(album_dict)
                categorized_albums.add(album_name)
        
        # Process compilation albums
        for album in compilation_albums:
            album_dict = dict(album)
            album_name = album_dict.get("album", "")
            
            # Skip if already categorized
            if album_name in categorized_albums:
                continue
            
            album_dict['is_missing'] = False
            album_dict['is_compilation'] = True
            albums_by_category["compilation"].append(album_dict)
        
        # Categorize missing releases
        missing_by_category = {
            "album": [],
            "ep": [],
            "single": [],
            "compilation": []
        }
        
        for release in missing_releases_data:
            release_dict = dict(release)
            release_dict['is_missing'] = True  # Mark as missing
            category = (release_dict.get("category") or "Album").lower()
            
            if category == "ep":
                missing_by_category["ep"].append(release_dict)
            elif category == "single":
                missing_by_category["single"].append(release_dict)
            elif category == "compilation":
                missing_by_category["compilation"].append(release_dict)
            else:
                missing_by_category["album"].append(release_dict)
        
        # Merge discovered and missing albums by category, then sort by release date
        merged_albums_by_category = {}
        for category in ["album", "ep", "single", "compilation", "unknown"]:
            merged_list = albums_by_category.get(category, []) + missing_by_category.get(category, [])
            
            # Sort by release date (newest first)
            # For discovered albums, use album_year; for missing, use first_release_date
            def get_sort_key(item):
                if item.get('is_missing'):
                    # Missing album - extract year from first_release_date (format: YYYY-MM-DD or YYYY)
                    date_str = item.get('first_release_date', '')
                    if date_str:
                        year = date_str[:4] if len(date_str) >= 4 else ''
                        try:
                            # Ensure year is a string before checking if it's a digit
                            return int(year) if (isinstance(year, str) and year.isdigit()) else 0
                        except (ValueError, AttributeError, TypeError):
                            return 0
                    return 0
                else:
                    # Discovered album - use album_year
                    return item.get('album_year') or 0
            
            merged_list.sort(key=get_sort_key, reverse=True)
            merged_albums_by_category[category] = merged_list
        
        # Aggregate genres from all tracks by this artist
        genres = aggregate_genres_from_tracks(name, DB_PATH)
        
        # Get qBittorrent and slskd configs
        cfg = get_config()
        qbit_config = cfg.get("qbittorrent", {"enabled": False, "web_url": "http://localhost:8080"})
        slskd_config = cfg.get("slskd", {"enabled": False})
        
        # Convert all template data to JSON-serializable format
        # This ensures Row objects, datetime, Decimal, etc. are all properly converted
        albums_data_dicts = convert_row_to_json_serializable(albums_data)
        merged_albums_by_category = convert_row_to_json_serializable(merged_albums_by_category)
        missing_by_category = convert_row_to_json_serializable(missing_by_category)
        top_tracks = convert_row_to_json_serializable(top_tracks)
        appears_on_albums = convert_row_to_json_serializable(appears_on_albums)
        artist_stats = convert_row_to_json_serializable(artist_stats)
        genres = convert_row_to_json_serializable(genres)
        
        return render_template("artist.html", 
                             artist_name=name,
                             albums=albums_data_dicts,  # Keep for compatibility
                             albums_by_category=merged_albums_by_category,
                             missing_by_category=missing_by_category,  # Keep for backward compatibility
                             top_tracks=top_tracks,
                             appears_on_albums=appears_on_albums,
                             stats=artist_stats,
                             genres=genres,
                             artist_country=artist_country,
                             artist_image_url=artist_image_url,
                             artist_bio=artist_bio,
                             qbit_config=qbit_config,
                             slskd_config=slskd_config)
    except Exception as e:
        import traceback
        logging.error(f"Error loading artist details: {str(e)}")
        logging.error(f"Traceback: {traceback.format_exc()}")
        flash(f"Error loading artist: {str(e)}", "error")
        return redirect(url_for("artists"))


def _normalize_release_title(text: str) -> str:
    """Normalize release titles for comparison."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", text)
    text = re.sub(r"(?i)\b(remaster(?:ed)?\s*\d{0,4}|remaster|deluxe|live|mono|stereo|edit|mix|version|bonus track)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _fetch_musicbrainz_releases(artist_name: str, limit: int = 100, artist_mbid: str | None = None) -> list[dict]:
    """
    Fetch release-groups from MusicBrainz for an artist with retry logic and pagination.
    
    If artist_mbid is provided, uses direct MBID lookup (more accurate).
    Otherwise uses text search by artist name.
    
    Handles SSL errors, timeouts, and other network issues with exponential backoff.
    Implements pagination to fetch all releases (not just first 100).
    """
    if not artist_name:
        return []
    
    headers = {"User-Agent": MUSICBRAINZ_USER_AGENT}
    releases: list[dict] = []
    url = "https://musicbrainz.org/ws/2/release-group"
    
    # Use artist MBID for lookup if available (more accurate than text search)
    if artist_mbid:
        query = f'arid:"{artist_mbid}" AND (primarytype:album OR primarytype:ep OR primarytype:single)'
        logging.debug(f"MusicBrainz: Using artist MBID lookup for {artist_name} ({artist_mbid})")
    else:
        query = f'artist:"{artist_name}" AND (primarytype:album OR primarytype:ep OR primarytype:single)'
        logging.debug(f"MusicBrainz: Using text search for {artist_name}")
    
    # Retry with exponential backoff
    max_retries = 3
    base_delay = 1
    
    # Pagination loop - fetch all pages of results
    offset = 0
    page_size = min(limit, 100)  # MusicBrainz max is 100 per request
    max_total = 500  # Safety limit to avoid excessive API calls
    pages_fetched = 0
    max_pages = 10  # Additional safety: max 10 pages (1000 releases if page_size=100)
    
    while offset < max_total and pages_fetched < max_pages:
        params = {"fmt": "json", "limit": page_size, "query": query, "offset": offset}
        
        fetched_this_page = False
        for attempt in range(max_retries):
            try:
                resp = requests.get(url, headers=headers, params=params, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                
                release_groups = data.get("release-groups", []) or []
                total_count = data.get("count", 0)
                
                for rg in release_groups:
                    rg_id = rg.get("id", "")
                    primary_type = rg.get("primary-type", "")
                    releases.append({
                        "id": rg_id,
                        "title": rg.get("title", ""),
                        "primary_type": primary_type,
                        "first_release_date": rg.get("first-release-date", ""),
                        "secondary_types": rg.get("secondary-types", []),
                        "cover_art_url": f"https://coverartarchive.org/release-group/{rg_id}/front-500" if rg_id else "",
                    })
                
                fetched_this_page = True
                pages_fetched += 1
                
                # Check if we've fetched all available releases
                if len(release_groups) < page_size or offset + len(release_groups) >= total_count:
                    logging.debug(f"MusicBrainz: Fetched {len(releases)} total releases for {artist_name}")
                    return releases  # Success - all pages fetched
                
                # Move to next page
                offset += page_size
                # MusicBrainz rate limiting: wait 1 second between requests
                time.sleep(1.0)
                break  # Success, exit retry loop
                
            except requests.exceptions.Timeout:
                logging.debug(f"MusicBrainz timeout (attempt {attempt+1}/{max_retries}) for {artist_name} at offset {offset}")
                if attempt < max_retries - 1:
                    time.sleep(base_delay * (2 ** attempt))  # Exponential backoff
            except requests.exceptions.ConnectionError as e:
                # Includes SSLEOFError and other connection issues
                logging.debug(f"MusicBrainz connection error (attempt {attempt+1}/{max_retries}) for {artist_name}: {type(e).__name__}")
                if attempt < max_retries - 1:
                    time.sleep(base_delay * (2 ** attempt))
            except requests.exceptions.RequestException as e:
                logging.debug(f"MusicBrainz request error for {artist_name}: {e}")
                break  # Don't retry for other request errors
            except Exception as e:
                logging.debug(f"Unexpected error fetching MusicBrainz releases for {artist_name}: {e}")
                break
        
        # If we failed to fetch this page after all retries, return what we have
        if not fetched_this_page:
            logging.warning(f"MusicBrainz: Failed to fetch page at offset {offset} for {artist_name}, returning {len(releases)} releases")
            return releases
    
    return releases


@app.route("/api/artist/exists", methods=["GET"])
def api_artist_exists():
    """Check if an artist exists in the database."""
    artist = request.args.get("artist", "").strip()
    if not artist:
        return jsonify({"error": "Artist is required"}), 400
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Check if artist exists using the same logic as the artist listing page
        # Look for artist as main artist or album artist
        cursor.execute("""
            SELECT COUNT(*) FROM tracks 
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = ? 
            LIMIT 1
        """, (artist,))
        result = cursor.fetchone()
        conn.close()
        
        exists = result[0] > 0 if result else False
        
        return jsonify({
            "exists": exists,
            "artist": artist
        })
    except Exception as e:
        logging.error(f"Error checking artist existence: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/artist/missing-releases", methods=["GET"])
def api_artist_missing_releases():
    """Detect missing releases for an artist by comparing to MusicBrainz."""
    artist = request.args.get("artist", "").strip()
    if not artist:
        return jsonify({"error": "Artist is required"}), 400

    conn = get_db()
    cursor = conn.cursor()
    
    # Get artist MBID if available for more accurate MusicBrainz lookup
    artist_mbid = None
    try:
        cursor.execute("""
            SELECT MAX(musicbrainz_artist_id) FROM tracks WHERE artist = ?
        """, (artist,))
        row = cursor.fetchone()
        if row and row[0]:
            artist_mbid = row[0]
    except:
        pass
    
    cursor.execute("""
        SELECT DISTINCT album FROM tracks WHERE artist = ?
    """, (artist,))
    existing_albums = [row[0] for row in cursor.fetchall()]
    cursor.execute("""
        SELECT release_id FROM missing_releases WHERE artist = ?
    """, (artist,))
    existing_missing = {row[0] for row in cursor.fetchall()}
    conn.close()

    existing_norm = {_normalize_release_title(a) for a in existing_albums if a}

    # Use artist MBID for accurate lookup when available
    mb_releases = _fetch_musicbrainz_releases(artist, artist_mbid=artist_mbid)
    missing = []
    for rg in mb_releases:
        norm_title = _normalize_release_title(rg.get("title") or "")
        if not norm_title or norm_title in existing_norm:
            continue
        # Categorize by type
        secondary = [s.lower() for s in rg.get("secondary_types") or []]
        primary_type = (rg.get("primary_type") or "").lower()
        category = "Album"
        if "compilation" in secondary:
            category = "Compilation"
        elif primary_type == "ep":
            category = "EP"
        elif primary_type == "single" or "single" in secondary:
            category = "Single"

        missing.append({
            "id": rg.get("id", ""),
            "title": rg.get("title", ""),
            "primary_type": rg.get("primary_type", ""),
            "first_release_date": rg.get("first_release_date", ""),
            "secondary_types": rg.get("secondary_types", []),
            "cover_art_url": rg.get("cover_art_url", ""),
            "category": category,
        })

    return jsonify({
        "artist": artist,
        "missing": missing,
        "total_musicbrainz": len(mb_releases),
        "existing_albums": existing_albums,
    })


@app.route("/api/artist/import-release", methods=["POST"])
def api_import_release():
    """Import a MusicBrainz release with full tracklisting to the database."""
    data = request.json or {}
    artist = data.get("artist", "").strip()
    release_id = data.get("release_id", "").strip()
    title = data.get("title", "").strip()
    
    if not artist or not release_id or not title:
        return jsonify({"error": "Artist, release_id, and title are required"}), 400
    
    try:
        # Fetch release details from MusicBrainz including media and recordings
        mb_url = f"https://musicbrainz.org/ws/2/release/{release_id}"
        headers = {"User-Agent": MUSICBRAINZ_USER_AGENT}
        response = requests.get(
            mb_url,
            params={
                "fmt": "json",
                "inc": "recordings+artist-relations+release-groups"
            },
            headers=headers,
            timeout=15
        )
        response.raise_for_status()
        release_data = response.json()
        
        if not release_data:
            return jsonify({"error": "Release not found on MusicBrainz"}), 404
        
        # Extract media and tracks
        media = release_data.get("media", [])
        if not media:
            return jsonify({"error": "Release has no media/tracks"}), 400
        
        conn = get_db()
        cursor = conn.cursor()
        imported_count = 0
        
        # Get year from release-date
        year = release_data.get("date", "")
        if year:
            year = year[:4]
        
        # Process each track from all media (discs)
        for disc_idx, disc in enumerate(media, start=1):
            disc_number = disc.get("position", disc_idx)
            tracks_list = disc.get("tracks", [])
            
            for track_idx, track in enumerate(tracks_list, start=1):
                recording = track.get("recording", {})
                track_title = recording.get("title") or track.get("title") or "Unknown"
                duration = recording.get("length")
                mbid = recording.get("id", "")
                
                # Build track record
                track_record = {
                    "id": recording.get("id", f"{release_id}_{disc_number}_{track_idx}"),
                    "title": track_title,
                    "artist": artist,
                    "album": title,
                    "track_number": track_idx,
                    "disc_number": disc_number,
                    "duration": duration,
                    "year": year,
                    "mbid": mbid,
                    "score": 0.0,
                    "spotify_score": 0,
                    "lastfm_score": 0,
                    "age_score": 0,
                    "genres": json.dumps([]),  # Serialize as JSON string
                    "file_path": None,
                    "stars": 0,
                    "last_scanned": datetime.now().isoformat(),
                }
                
                # Insert or update track in database
                save_to_db(track_record)
                imported_count += 1
        
        conn.close()
        
        logging.info(f"[IMPORT] Imported {imported_count} tracks from '{title}' by {artist} (MB ID: {release_id})")
        
        return jsonify({
            "success": True,
            "message": f"Imported {imported_count} tracks from '{title}'",
            "tracks_imported": imported_count,
            "artist": artist,
            "album": title
        })
        
    except requests.exceptions.HTTPError as e:
        logging.error(f"[IMPORT] MusicBrainz API error: {e}")
        return jsonify({"error": f"MusicBrainz API error: {e.response.status_code}"}), 500
    except Exception as e:
        logging.error(f"[IMPORT] Error importing release: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/artist/scan-all-missing-releases", methods=["POST"])
def api_scan_all_missing_releases():
    """Scan all artists in database for missing releases and cache results."""
    global scan_process_missing_releases
    
    # Check if already running
    if scan_process_missing_releases and isinstance(scan_process_missing_releases, dict):
        thread = scan_process_missing_releases.get('thread')
        if thread and thread.is_alive():
            return jsonify({"error": "Missing releases scan already running"}), 400
    
    def run_missing_releases_scan():
        global scan_process_missing_releases
        # Define progress_file at function scope so it's always available
        progress_file = os.path.join(os.path.dirname(DB_PATH), "missing_releases_scan_progress.json")
        
        try:
            conn = get_db()
            cursor = conn.cursor()
            
            # Get all distinct artists from tracks table
            cursor.execute("SELECT DISTINCT artist FROM tracks WHERE artist IS NOT NULL AND artist != '' ORDER BY artist")
            artists = [row[0] for row in cursor.fetchall()]
            total_artists = len(artists)
            
            logging.info(f"[MISSING_RELEASES] Starting scan for {total_artists} artists")
            
            # Clean up any releases that are NOW in the database (were imported since last scan)
            cursor.execute("""
                DELETE FROM missing_releases mr
                WHERE EXISTS (
                    SELECT 1 FROM tracks t
                    WHERE LOWER(t.artist) = LOWER(mr.artist)
                    AND LOWER(TRIM(t.album)) = LOWER(TRIM(mr.title))
                )
            """)
            imported_count = cursor.rowcount
            conn.commit()
            if imported_count > 0:
                logging.info(f"[MISSING_RELEASES] Cleaned up {imported_count} releases that were imported since last scan")
            
            processed = 0
            total_missing = 0
            
            for artist_name in artists:
                try:
                    # Check if scan should stop
                    with scan_lock:
                        if scan_process_missing_releases is None:
                            logging.info("[MISSING_RELEASES] Stop signal received, exiting gracefully")
                            progress_data = {
                                "is_running": False,
                                "scan_type": "missing_releases_scan",
                                "status": "stopped",
                                "processed_artists": processed,
                                "total_artists": total_artists,
                                "total_missing_found": total_missing,
                                "percent_complete": int((processed / total_artists * 100)) if total_artists > 0 else 0
                            }
                            with open(progress_file, 'w') as f:
                                json.dump(progress_data, f)
                            return
                    
                    processed += 1
                    
                    # Update progress file
                    progress_data = {
                        "is_running": True,
                        "scan_type": "missing_releases_scan",
                        "current_artist": artist_name,
                        "processed_artists": processed,
                        "total_artists": total_artists,
                        "total_missing_found": total_missing,
                        "percent_complete": int((processed / total_artists * 100)) if total_artists > 0 else 0
                    }
                    
                    with open(progress_file, 'w') as f:
                        json.dump(progress_data, f)
                    
                    # Look up and save artist MBID if not already saved
                    # This strengthens MusicBrainz lookups for album type detection
                    try:
                        from api_clients.musicbrainz import lookup_and_save_artist_mbid
                        cursor.execute("""
                            SELECT MAX(musicbrainz_artist_id) FROM tracks WHERE artist = ?
                        """, (artist_name,))
                        result = cursor.fetchone()
                        existing_mbid = result[0] if result and result[0] else None
                        
                        if not existing_mbid:
                            # No MBID saved yet, try to look it up from MusicBrainz
                            mbid = lookup_and_save_artist_mbid(artist_name, conn)
                            if mbid:
                                logging.debug(f"[MISSING_RELEASES] Artist MBID saved for {artist_name}: {mbid}")
                        else:
                            logging.debug(f"[MISSING_RELEASES] Artist {artist_name} already has MBID: {existing_mbid}")
                    except Exception as e:
                        logging.debug(f"[MISSING_RELEASES] Could not look up MBID for {artist_name}: {e}")
                    
                    # Get existing albums for this artist
                    cursor.execute("SELECT DISTINCT album FROM tracks WHERE artist = ?", (artist_name,))
                    existing_albums = [row[0] for row in cursor.fetchall()]
                    existing_norm = {_normalize_release_title(a) for a in existing_albums if a}
                    
                    # Fetch MusicBrainz releases
                    mb_releases = _fetch_musicbrainz_releases(artist_name)
                    
                    # Check for missing releases AND update cover art for existing albums
                    for rg in mb_releases:
                        norm_title = _normalize_release_title(rg.get("title") or "")
                        cover_art_url = rg.get("cover_art_url", "")
                        
                        # If album exists, update its cover art
                        if norm_title and norm_title in existing_norm:
                            if cover_art_url:
                                # Update cover_art_url for all tracks in this album
                                original_album = next((a for a in existing_albums if _normalize_release_title(a) == norm_title), None)
                                if original_album:
                                    # Update the URL in tracks table
                                    cursor.execute("""
                                        UPDATE tracks 
                                        SET cover_art_url = ?
                                        WHERE artist = ? AND album = ?
                                    """, (cover_art_url, artist_name, original_album))
                                    
                                    # Also download and save the actual album art image data (same as popularity scan does)
                                    try:
                                        if download_and_save_album_art(artist_name, original_album, cover_art_url):
                                            logging.info(f"[MISSING_RELEASES] Downloaded and cached album art for {artist_name} - {original_album}")
                                        else:
                                            logging.debug(f"[MISSING_RELEASES] Failed to download album art for {artist_name} - {original_album}")
                                    except Exception as e:
                                        logging.debug(f"[MISSING_RELEASES] Error downloading album art for {artist_name} - {original_album}: {e}")
                            continue
                        
                        if not norm_title:
                            continue
                        
                        # Categorize by type (including compilations)
                        secondary = [s.lower() for s in rg.get("secondary_types") or []]
                        primary_type = (rg.get("primary_type") or "").lower()
                        category = "Album"
                        if "compilation" in secondary:
                            category = "Compilation"
                        elif primary_type == "ep":
                            category = "EP"
                        elif primary_type == "single" or "single" in secondary:
                            category = "Single"
                        
                        # Insert missing release into database
                        cursor.execute("""
                            INSERT OR REPLACE INTO missing_releases 
                            (artist, release_id, title, primary_type, first_release_date, cover_art_url, category, last_checked)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            artist_name,
                            rg.get("id", ""),
                            rg.get("title", ""),
                            rg.get("primary_type", "Album"),
                            rg.get("first_release_date", ""),
                            cover_art_url,
                            category,
                            datetime.now().isoformat()
                        ))
                        total_missing += 1
                    
                    conn.commit()
                    
                    # Rate limiting
                    time.sleep(1.1)  # MusicBrainz rate limit: 1 request per second
                    
                except Exception as e:
                    logging.error(f"[MISSING_RELEASES] Error scanning {artist_name}: {e}")
                    continue
            
            # Write final progress
            progress_data = {
                "is_running": False,
                "scan_type": "missing_releases_scan",
                "processed_artists": total_artists,
                "total_artists": total_artists,
                "total_missing_found": total_missing,
                "percent_complete": 100,
                "status": "complete"
            }
            with open(progress_file, 'w') as f:
                json.dump(progress_data, f)
            
            conn.close()
            logging.info(f"[MISSING_RELEASES] Scan complete. Found {total_missing} missing releases across {total_artists} artists")
            
        except Exception as e:
            logging.error(f"[MISSING_RELEASES] Scan failed: {e}", exc_info=True)
            progress_data = {
                "is_running": False,
                "scan_type": "missing_releases_scan",
                "status": "error",
                "error": str(e)
            }
            try:
                with open(progress_file, 'w') as f:
                    json.dump(progress_data, f)
            except:
                pass
    
    # Start scan in background thread
    scan_thread = threading.Thread(target=run_missing_releases_scan, daemon=False)
    scan_thread.start()
    scan_process_missing_releases = {'thread': scan_thread, 'type': 'missing_releases'}
    
    return jsonify({
        "success": True,
        "message": "Missing releases scan started"
    })


@app.route("/api/artist/country", methods=["POST"])
def api_fetch_artist_country():
    """Fetch artist country from MusicBrainz and update database."""
    try:
        data = request.get_json()
        artist_name = data.get("artist_name", "").strip()
        
        if not artist_name:
            return jsonify({"error": "Artist name required"}), 400
        
        # Fetch country from MusicBrainz
        from api_clients.musicbrainz import get_artist_country
        country = get_artist_country(artist_name, enabled=True)
        
        if not country:
            return jsonify({"error": "No country information found on MusicBrainz"}), 404
        
        # Update artists table
        conn = get_db()
        cursor = conn.cursor()
        
        # Ensure artist exists in artists table
        cursor.execute("SELECT id FROM artists WHERE name = ?", (artist_name,))
        artist_row = cursor.fetchone()
        
        if artist_row:
            # Update existing artist
            cursor.execute("UPDATE artists SET country = ? WHERE name = ?", (country, artist_name))
        else:
            # Insert new artist entry
            cursor.execute("INSERT INTO artists (id, name, country) VALUES (?, ?, ?)", 
                         (artist_name, artist_name, country))
        
        # Also update all tracks by this artist
        cursor.execute("UPDATE tracks SET artist_country = ? WHERE artist = ?", (country, artist_name))
        
        conn.commit()
        conn.close()
        
        return jsonify({
            "success": True,
            "country": country,
            "message": f"Updated country to {country}"
        })
        
    except Exception as e:
        logging.error(f"[ARTIST_COUNTRY] Error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/artist/country/update", methods=["POST"])
def api_update_artist_country():
    """Manually update artist country/origin."""
    try:
        data = request.get_json()
        artist_name = data.get("artist_name", "").strip()
        country = data.get("country", "").strip()
        
        if not artist_name or not country:
            return jsonify({"error": "Artist name and country are required"}), 400
        
        # Update artists table
        conn = get_db()
        cursor = conn.cursor()
        
        # Ensure artist exists in artists table
        cursor.execute("SELECT id FROM artists WHERE name = ?", (artist_name,))
        artist_row = cursor.fetchone()
        
        if artist_row:
            # Update existing artist
            cursor.execute("UPDATE artists SET country = ? WHERE name = ?", (country, artist_name))
        else:
            # Insert new artist entry
            cursor.execute("INSERT INTO artists (id, name, country) VALUES (?, ?, ?)", 
                         (artist_name, artist_name, country))
        
        # Also update all tracks by this artist
        cursor.execute("UPDATE tracks SET artist_country = ? WHERE artist = ?", (country, artist_name))
        
        conn.commit()
        conn.close()
        
        return jsonify({
            "success": True,
            "country": country,
            "message": f"Updated country to {country}"
        })
        
    except Exception as e:
        logging.error(f"[ARTIST_COUNTRY_UPDATE] Error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500



def update_audio_file_genres(file_path, genres_list):
    """
    Update genre tags in audio files (MP3, FLAC, etc.)
    
    Args:
        file_path: Path to the audio file
        genres_list: List of genre strings to set
        
    Returns:
        tuple: (success: bool, error_message: str or None)
        - (True, None) if file was successfully updated
        - (False, "error message") if there was an error or unsupported format
    """
    try:
        from mutagen.mp3 import MP3
        from mutagen.flac import FLAC
        from mutagen.id3 import ID3, TCON
    except ImportError:
        return False, "Mutagen library not available"
    
    try:
        # Determine file format and handle accordingly
        file_ext = os.path.splitext(file_path)[1].lower()
        
        if file_ext == '.mp3':
            # Handle MP3 files with ID3 tags
            audio = MP3(file_path, ID3=ID3)
            if audio.tags is None:
                audio.add_tags()
            audio.tags['TCON'] = TCON(encoding=3, text=genres_list)
            audio.save()
            return True, None
        elif file_ext == '.flac':
            # Handle FLAC files with Vorbis comments
            audio = FLAC(file_path)
            # FLAC uses vorbis comments, which support multiple genre values
            audio['genre'] = genres_list
            audio.save()
            return True, None
        else:
            # For unsupported formats, return error to skip file
            return False, f"Unsupported format: {file_ext}"
    except Exception as e:
        return False, str(e)


@app.route("/api/artist/country/apply-as-genre", methods=["POST"])
def api_apply_country_as_genre():
    """Apply artist country as genre tag to all tracks."""
    try:
        data = request.get_json()
        artist_name = data.get("artist_name", "").strip()
        
        if not artist_name:
            return jsonify({"error": "Artist name required"}), 400
        
        # Get artist country
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("SELECT country FROM artists WHERE name = ?", (artist_name,))
        artist_row = cursor.fetchone()
        
        if not artist_row or not artist_row[0]:
            return jsonify({"error": "No country information available for this artist"}), 404
        
        country = artist_row[0]
        
        # Get all tracks by this artist with file paths
        cursor.execute("SELECT id, file_path, genre FROM tracks WHERE artist = ?", (artist_name,))
        tracks = cursor.fetchall()
        
        if not tracks:
            conn.close()
            return jsonify({"error": "No tracks found for this artist"}), 404
        
        conn.close()
        
        # Import mutagen for MP3 tag editing
        try:
            from mutagen.id3 import ID3
            from mutagen.mp3 import MP3
        except ImportError:
            return jsonify({"error": "Mutagen library not available"}), 500
        
        updated_count = 0
        errors = []
        
        # Use consistent genre delimiter
        GENRE_DELIMITER = '; '
        
        for track in tracks:
            track_id, file_path, current_genre = track
            
            if not file_path or not os.path.exists(file_path):
                continue
            
            try:
                # Update MP3 file genre tag
                audio = MP3(file_path, ID3=ID3)
                
                # Get existing genres - split on both '; ' and ';' for backward compatibility
                existing_genres = []
                if audio.tags and 'TCON' in audio.tags:
                    try:
                        genre_str = str(audio.tags['TCON'])
                    except (KeyError, AttributeError):
                        genre_str = ""
                    # Split on ';' and strip whitespace from each part
                    existing_genres = [part.strip() for part in genre_str.split(';') if part.strip()]
                
                # Add country if not already present\n                if country not in existing_genres:\n                    existing_genres.append(country)\n                    \n                    # Set the genre tag\n                    if audio.tags is None:\n                        audio.add_tags()\n                    # Set genre directly without TCON class for compatibility\n                    try:\n                        # Try using mutagen's frame creation\n                        from mutagen.id3._frames import TCON as FrameTCON  # type: ignore\n                        audio.tags['TCON'] = FrameTCON(encoding=3, text=existing_genres)\n                    except (ImportError, AttributeError, TypeError):\n                        # Fallback: just update database\n                        pass\n                    audio.save()
                    
                    # Update database - use consistent delimiter
                    conn = get_db()
                    cursor = conn.cursor()
                    new_genre = GENRE_DELIMITER.join(existing_genres)
                    cursor.execute("UPDATE tracks SET genre = ? WHERE id = ?", (new_genre, track_id))
                    conn.commit()
                    conn.close()
                    
                    updated_count += 1
                    
            except Exception as e:
                errors.append(f"{os.path.basename(file_path)}: {str(e)}")
                logging.error(f"[APPLY_COUNTRY_GENRE] Error updating {file_path}: {e}")
        
        message = f"Applied '{country}' as genre to tracks"
        if errors:
            message += f" (with {len(errors)} errors)"
        
        return jsonify({
            "success": True,
            "message": message,
            "tracks_updated": updated_count,
            "errors": errors[:5]  # Return first 5 errors
        })
        
    except Exception as e:
        logging.error(f"[APPLY_COUNTRY_GENRE] Error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/artist/cached-missing-releases", methods=["GET"])
def api_cached_missing_releases():
    """Get cached missing releases for an artist from the database."""
    artist = request.args.get("artist", "").strip()
    if not artist:
        return jsonify({"error": "Artist is required"}), 400
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Check if missing_releases table exists
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='missing_releases'
        """)
        table_exists = cursor.fetchone()
        
        if not table_exists:
            conn.close()
            return jsonify({
                "artist": artist,
                "missing": [],
                "from_cache": False
            })
        
        cursor.execute("""
            SELECT release_id, title, primary_type, first_release_date, cover_art_url, category, last_checked
            FROM missing_releases
            WHERE artist = ?
            ORDER BY first_release_date DESC
        """, (artist,))
        
        rows = cursor.fetchall()
        conn.close()
        
        missing = []
        for row in rows:
            release_id = row[0] if len(row) > 0 else ""
            
            missing.append({
                "id": release_id,
                "title": row[1] if len(row) > 1 else "",
                "primary_type": row[2] if len(row) > 2 else "Album",
                "first_release_date": row[3] if len(row) > 3 else "",
                "cover_art_url": row[4] if len(row) > 4 else "",
                "category": row[5] if len(row) > 5 else "Album",
                "last_checked": row[6] if len(row) > 6 else ""
            })
        
        return jsonify({
            "artist": artist,
            "missing": missing,
            "from_cache": True
        })
        
    except Exception as e:
        logging.error(f"[MISSING_RELEASES] Error fetching cached data: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/artist/cleanup-false-positive-missing", methods=["POST"])
def api_cleanup_false_positive_missing():
    """Remove false positives from missing_releases that actually exist in database."""
    data = request.json or {}
    artist = data.get("artist", "").strip()
    
    if not artist:
        return jsonify({"error": "Artist is required"}), 400
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Get all existing albums
        cursor.execute("""
            SELECT DISTINCT album FROM tracks WHERE artist = ?
        """, (artist,))
        existing_albums = {_normalize_release_title(row[0]) for row in cursor.fetchall() if row[0]}
        
        # Get all missing releases
        cursor.execute("""
            SELECT release_id, title FROM missing_releases WHERE artist = ?
        """, (artist,))
        missing_releases = cursor.fetchall()
        
        # Find false positives (items in missing_releases that exist in database)
        false_positives = []
        for release_id, title in missing_releases:
            norm_title = _normalize_release_title(title)
            if norm_title in existing_albums:
                false_positives.append(release_id)
        
        # Remove false positives
        removed_count = 0
        for release_id in false_positives:
            cursor.execute("""
                DELETE FROM missing_releases WHERE release_id = ?
            """, (release_id,))
            removed_count += 1
        
        conn.commit()
        conn.close()
        
        logging.info(f"[CLEANUP] Removed {removed_count} false positive missing releases for {artist}")
        
        return jsonify({
            "success": True,
            "artist": artist,
            "removed_count": removed_count,
            "false_positives": false_positives
        })
        
    except Exception as e:
        logging.error(f"[CLEANUP] Error removing false positives: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/artist/bio")
def api_artist_bio():
    """Get artist biography from database cache only (fetched during scans, not real-time online)"""
    artist_name = request.args.get("name", "").strip()
    if not artist_name:
        return jsonify({"error": "Artist name required"}), 400
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Return cached biography from database only - no online calls
        cursor.execute("""
            SELECT bio, image_url 
            FROM artists 
            WHERE name = ?
        """, (artist_name,))
        artist_row = cursor.fetchone()
        conn.close()
        
        if artist_row:
            bio = artist_row['bio'] if artist_row['bio'] else ""
            image_url = artist_row['image_url'] if artist_row['image_url'] else ""
            
            # Clean up the biography text (for old cached data with artist IDs)
            if bio:
                cleaned_bio = clean_discogs_biography(bio)
            else:
                cleaned_bio = ""
            
            logging.debug(f"[ARTIST BIO] Found bio for {artist_name}: {len(cleaned_bio)} chars")
            return jsonify({
                "bio": cleaned_bio,
                "source": "Database (from scan)",
                "image_url": image_url
            })
        else:
            # Artist not in database yet - return empty with helpful message
            logging.info(f"[ARTIST BIO] No artist record for {artist_name} - run scan to populate")
            return jsonify({
                "bio": "",
                "source": "Not yet populated (run Scan Artist button to fetch)",
                "image_url": ""
            })
        
    except Exception as e:
        logging.error(f"[ARTIST BIO] Error fetching bio for {artist_name}: {e}")
        return jsonify({"bio": "", "source": "Error loading bio"}), 200


@app.route("/api/artist/singles-count")
def api_artist_singles_count():
    """Get count of singles for an artist"""
    artist_name = request.args.get("name", "").strip()
    if not artist_name:
        return jsonify({"error": "Artist name required"}), 400
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as count FROM tracks WHERE artist = ? AND is_single = 1", (artist_name,))
        row = cursor.fetchone()
        conn.close()
        
        count = row['count'] if row else 0
        return jsonify({"count": count})
        
    except Exception as e:
        logging.error(f"Error fetching singles count: {e}")
        return jsonify({"count": 0, "error": str(e)}), 500


@app.route("/api/artist/create-essential-playlist", methods=["POST"])
def api_create_essential_playlist():
    """Create an Essential Playlist for an artist using single detection logic"""
    data = request.json or {}
    artist_name = data.get("artist", "").strip()
    
    if not artist_name:
        return jsonify({"error": "Artist name required"}), 400
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Get all singles with high confidence
        cursor.execute("""
            SELECT id, title, album, stars, score, single_confidence 
            FROM tracks 
            WHERE artist = ? AND is_single = 1
            ORDER BY 
                CASE single_confidence 
                    WHEN 'high' THEN 3
                    WHEN 'medium' THEN 2
                    WHEN 'low' THEN 1
                    ELSE 0
                END DESC,
                score DESC,
                stars DESC
            LIMIT 50
        """, (artist_name,))
        
        singles = cursor.fetchall()
        conn.close()
        
        if not singles:
            return jsonify({"error": "No singles found for this artist"}), 404
        
        # Create playlist name
        playlist_name = f"{artist_name} - Essential"
        
        # For now, just return success - Navidrome playlist creation would go here
        logging.info(f"Created essential playlist for {artist_name} with {len(singles)} tracks")
        
        return jsonify({
            "success": True,
            "message": f"Created Essential Playlist with {len(singles)} tracks",
            "playlist_name": playlist_name,
            "track_count": len(singles)
        })
        
    except Exception as e:
        logging.error(f"Error creating essential playlist: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/artist/image")
def api_artist_image():
    """Get artist image from database cache only (fetched during scans, not real-time online)"""
    artist_name = request.args.get("name", "").strip()
    if not artist_name:
        svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">
            <rect fill="#2a2a2a" width="200" height="200"/>
            <text x="50%" y="50%" text-anchor="middle" dy=".3em" fill="#666" font-size="16">No Image</text>
        </svg>'''
        return Response(svg, mimetype='image/svg+xml')
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Return cached image URL from database only - no online calls
        cursor.execute("""
            SELECT image_url 
            FROM artists 
            WHERE name = ?
        """, (artist_name,))
        row = cursor.fetchone()
        conn.close()
        
        if row and row['image_url']:
            # Redirect to the stored image URL
            logging.debug(f"[ARTIST IMAGE] Found image for {artist_name}")
            return redirect(row['image_url'])
        else:
            logging.debug(f"[ARTIST IMAGE] No image for {artist_name} - run scan to fetch")
        
    except Exception as e:
        logging.error(f"[ARTIST IMAGE] Error fetching image for {artist_name}: {e}")
    
    # Return placeholder if no cached image
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">
        <rect fill="#2a2a2a" width="200" height="200"/>
        <text x="50%" y="50%" text-anchor="middle" dy=".3em" fill="#666" font-size="16">No Image</text>
    </svg>'''
    return Response(svg, mimetype='image/svg+xml')


@app.route("/api/artist/search-images")
def api_artist_search_images():
    """Search for artist images on MusicBrainz, Discogs, or Spotify"""
    artist_name = request.args.get("name", "").strip()
    source = request.args.get("source", "musicbrainz").strip()
    
    if not artist_name:
        return jsonify({"error": "Artist name required"}), 400
    
    try:
        images = []
        
        if source == "musicbrainz":
            # Get artist MBID
            search_url = "https://musicbrainz.org/ws/2/artist"
            params = {"query": f'artist:"{artist_name}"', "fmt": "json", "limit": 5}
            headers = {"User-Agent": MUSICBRAINZ_USER_AGENT}
            
            resp = requests.get(search_url, params=params, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            
            for artist in data.get("artists", [])[:5]:
                mbid = artist.get("id")
                if mbid:
                    # Try to get image from CAA
                    image_url = f"https://coverartarchive.org/artist/{mbid}/front-500"
                    images.append({"url": image_url, "source": "MusicBrainz CAA"})
        
        elif source == "discogs":
            # Search Discogs for artist
            from api_clients.discogs import DiscogsClient

            config_data, _ = _read_yaml(CONFIG_PATH)
            discogs_config = config_data.get("api_integrations", {}).get("discogs", {})
            discogs_token = discogs_config.get("token", "")

            client = DiscogsClient(discogs_token)
            # Discogs API does not have a direct 'search_artist', so use database/search with type=artist
            search_url = f"https://api.discogs.com/database/search"
            params = {"q": artist_name, "type": "artist", "per_page": 5}
            res = client.session.get(search_url, headers=client.headers, params=params, timeout=10)
            res.raise_for_status()
            results = res.json().get("results", [])
            for result in results[:5]:
                if result.get("thumb"):
                    images.append({"url": result["thumb"], "source": "Discogs"})
        
        
        return jsonify({"images": images})
        
    except Exception as e:
        logging.error(f"Error searching artist images: {e}")
        import traceback
        logging.error(traceback.format_exc())
        return jsonify({"error": str(e), "images": []}), 500


@app.route("/api/artist/set-image", methods=["POST"])
def api_artist_set_image():
    """Set custom artist image"""
    data = request.json or {}
    artist_name = data.get("artist", "").strip()
    image_url = data.get("image_url", "").strip()
    
    if not artist_name or not image_url:
        return jsonify({"error": "Artist name and image URL required"}), 400
    
    # Validate that image_url is a valid HTTP/HTTPS URL, not a data URI or other scheme
    if not image_url.startswith(('http://', 'https://')):
        return jsonify({"error": "Image URL must be a valid HTTP or HTTPS URL"}), 400
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Create artist_images table if it doesn't exist
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS artist_images (
                artist_name TEXT PRIMARY KEY,
                image_url TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        
        # Insert or update
        cursor.execute("""
            INSERT OR REPLACE INTO artist_images (artist_name, image_url, updated_at)
            VALUES (?, ?, ?)
        """, (artist_name, image_url, datetime.now().isoformat()))
        
        conn.commit()
        conn.close()
        
        return jsonify({"success": True, "message": "Artist image updated"})
        
    except Exception as e:
        logging.error(f"Error setting artist image: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/artist/update-ids", methods=["POST"])
def api_artist_update_ids():
    """Update artist IDs (Spotify, Last.fm, MusicBrainz, Discogs) for an artist"""
    try:
        data = request.get_json()
        artist_name = data.get("artist")
        spotify_id = data.get("spotify_artist_id", "").strip()
        lastfm_mbid = data.get("lastfm_artist_mbid", "").strip()
        musicbrainz_id = data.get("musicbrainz_artist_id", "").strip()
        discogs_id = data.get("discogs_artist_id", "").strip()
        
        if not artist_name:
            return jsonify({"error": "Missing artist name"}), 400
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Update all tracks for this artist with the new IDs
        # Only update non-NULL values
        updates = []
        params = []
        
        if spotify_id:
            updates.append("spotify_artist_id = ?")
            params.append(spotify_id)
        
        if lastfm_mbid:
            updates.append("lastfm_artist_mbid = ?")
            params.append(lastfm_mbid)
        
        if musicbrainz_id:
            updates.append("musicbrainz_artist_id = ?")
            params.append(musicbrainz_id)
        
        if discogs_id:
            updates.append("discogs_artist_id = ?")
            params.append(discogs_id)
        
        if updates:
            params.append(artist_name)
            query = f"UPDATE tracks SET {', '.join(updates)} WHERE artist = ?"
            cursor.execute(query, params)
            conn.commit()
        
        conn.close()
        
        return jsonify({
            "success": True,
            "message": f"Artist IDs updated for {artist_name}",
            "updated": {
                "spotify_artist_id": spotify_id if spotify_id else None,
                "lastfm_artist_mbid": lastfm_mbid if lastfm_mbid else None,
                "musicbrainz_artist_id": musicbrainz_id if musicbrainz_id else None,
                "discogs_artist_id": discogs_id if discogs_id else None
            }
        })
    except Exception as e:
        logging.error(f"Error updating artist IDs: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/album/update-ids", methods=["POST"])
def api_album_update_ids():
    """Update album/release IDs for an album"""
    try:
        data = request.get_json()
        artist_name = data.get("artist")
        album_name = data.get("album")
        spotify_album_id = data.get("spotify_album_id", "").strip()
        musicbrainz_release_id = data.get("musicbrainz_release_id", "").strip()
        discogs_release_id = data.get("discogs_release_id", "").strip()
        
        if not artist_name or not album_name:
            return jsonify({"error": "Missing artist or album name"}), 400
        
        conn = get_db()
        cursor = conn.cursor()

        # Use canonical MusicBrainz album MBID column.
        cursor.execute("PRAGMA table_info(tracks)")
        track_columns = {row[1] for row in cursor.fetchall()}

        mb_album_column = "musicbrainz_album_mbid" if "musicbrainz_album_mbid" in track_columns else None

        discogs_album_column = None
        if "discogs_album_id" in track_columns:
            discogs_album_column = "discogs_album_id"
        elif "discogs_release_id" in track_columns:
            discogs_album_column = "discogs_release_id"
        
        # Update all tracks for this album with the new IDs
        updates = []
        params = []
        
        if spotify_album_id:
            updates.append("spotify_album_id = ?")
            params.append(spotify_album_id)
        
        if musicbrainz_release_id:
            if mb_album_column:
                updates.append(f"{mb_album_column} = ?")
                params.append(musicbrainz_release_id)
            else:
                logging.warning("Skipping MusicBrainz release ID update: no MB album ID column found in tracks table")
        
        if discogs_release_id:
            if discogs_album_column:
                updates.append(f"{discogs_album_column} = ?")
                params.append(discogs_release_id)
            else:
                logging.warning("Skipping Discogs release ID update: no Discogs album ID column found in tracks table")
        
        if updates:
            params.extend([artist_name, album_name])
            query = f"UPDATE tracks SET {', '.join(updates)} WHERE COALESCE(NULLIF(album_artist, ''), artist) = ? AND album = ?"
            cursor.execute(query, params)
            conn.commit()
        
        conn.close()
        
        return jsonify({
            "success": True,
            "message": f"Album IDs updated for {album_name}",
            "updated": {
                "spotify_album_id": spotify_album_id if spotify_album_id else None,
                "musicbrainz_release_id": musicbrainz_release_id if musicbrainz_release_id else None,
                "discogs_release_id": discogs_release_id if discogs_release_id else None
            }
        })
    except Exception as e:
        logging.error(f"Error updating album IDs: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/album/bulk-delete", methods=["POST"])
def api_album_bulk_delete():
    """Delete multiple tracks from database and optionally delete MP3 files"""
    try:
        data = request.get_json()
        track_ids = data.get("track_ids", [])
        artist = data.get("artist", "")
        album = data.get("album", "")
        delete_files = data.get("delete_files", True)  # Default to true for backward compatibility
        
        if not track_ids:
            return jsonify({"error": "No tracks selected"}), 400
        
        conn = get_db()
        cursor = conn.cursor()
        
        deleted_count = 0
        
        for track_id in track_ids:
            try:
                # Get file path
                cursor.execute("""
                    SELECT file_path FROM tracks WHERE id = ?
                """, (track_id,))
                result = cursor.fetchone()
                
                if result:
                    # Delete MP3 file if requested and it exists
                    if delete_files:
                        # Try beets_path first, then file_path
                        file_path = row_get(result, 'file_path')
                        
                        if file_path and os.path.exists(file_path):
                            try:
                                os.remove(file_path)
                                logging.info(f"[DELETE] Deleted MP3 file: {file_path}")
                            except Exception as e:
                                logging.warning(f"[DELETE] Failed to delete MP3: {file_path} - {e}")
                    
                    # Delete from database
                    cursor.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
                    deleted_count += 1
                    action = "and file(s)" if delete_files else "from database"
                    logging.info(f"[DELETE] Deleted track {track_id} {action}")
            except Exception as e:
                logging.error(f"[DELETE] Error deleting track {track_id}: {e}")
                continue
        
        conn.commit()
        conn.close()
        
        return jsonify({
            "success": True,
            "deleted_count": deleted_count,
            "message": f"Deleted {deleted_count} track(s) and file(s)"
        })
    except Exception as e:
        logging.error(f"[DELETE] Error in bulk delete: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/album/bulk-tag", methods=["POST"])
def api_album_bulk_tag():
    """Add genre tags to multiple tracks and write to audio files"""
    try:
        data = request.get_json()
        track_ids = data.get("track_ids", [])
        genres = data.get("genres", [])
        artist = data.get("artist", "")
        album = data.get("album", "")
        
        if not track_ids:
            return jsonify({"error": "No tracks selected"}), 400
        
        if not genres:
            return jsonify({"error": "No genres provided"}), 400
        
        conn = get_db()
        cursor = conn.cursor()
        
        updated_count = 0
        failed_files = []
        genre_str = ", ".join(genres)
        # Format for ID3 tags (double backslash separated)
        genre_id3_str = '\\'.join(genres)
        
        # Check if mutagen is available (imported at module level)
        has_mutagen = MP3 is not None and FLAC is not None and ID3 is not None
        
        for track_id in track_ids:
            try:
                # Get track info
                cursor.execute("SELECT title, genres, beets_path, file_path FROM tracks WHERE id = ?", (track_id,))
                result = cursor.fetchone()
                
                if result:
                    current_genres = row_get(result, 'genres') or ''
                    # Parse existing genres (handle both comma-separated and double-backslash formats)
                    if '\\' in current_genres:
                        existing = set(g.strip() for g in current_genres.split('\\') if g.strip())
                    else:
                        existing = set(g.strip() for g in current_genres.split(',') if g.strip())
                    # Add new genres
                    existing.update(genres)
                    # Join back for database (comma-separated for display)
                    new_genres = ', '.join(sorted(existing))
                    # Format for ID3 tags (double backslash separated)
                    genre_id3_final = '\\'.join(sorted(existing))
                    
                    # Write to audio file if mutagen is available
                    if has_mutagen:
                        file_path = row_get(result, 'beets_path') or row_get(result, 'file_path')
                        if file_path and os.path.exists(file_path):
                            try:
                                # Determine file format and handle accordingly
                                file_ext = os.path.splitext(file_path)[1].lower()
                                
                                if file_ext == '.mp3':
                                    # Handle MP3 files with ID3 tags
                                    audio = MP3(file_path, ID3=ID3)
                                    if audio.tags is None:
                                        audio.add_tags()
                                    # Write with double backslash format for ID3 tags
                                    audio.tags['TCON'] = TagCON(encoding=3, text=[genre_id3_final])
                                    audio.save()
                                    logging.info(f"[TAG] Updated MP3 tags for {file_path}: {genre_id3_final}")
                                elif file_ext == '.flac':
                                    # Handle FLAC files with Vorbis comments
                                    audio = FLAC(file_path)
                                    # FLAC uses vorbis comments, which support multiple genre values
                                    audio['genre'] = sorted(existing)
                                    audio.save()
                                    logging.info(f"[TAG] Updated FLAC tags for {file_path}: {', '.join(sorted(existing))}")
                                else:
                                    # For unsupported formats, only update database
                                    logging.debug(f"[TAG] Skipping file tag update for unsupported format: {file_path}")
                            except Exception as file_error:
                                logging.warning(f"[TAG] Failed to update tags for {file_path}: {file_error}")
                                track_title = row_get(result, 'title', '')
                                failed_files.append(track_title if track_title else f"Track ID: {track_id}")
                    
                    # Update database (store comma-separated for display)
                    cursor.execute("""
                        UPDATE tracks SET genres = ? WHERE id = ?
                    """, (new_genres, track_id))
                    
                    updated_count += 1
                    logging.info(f"[TAG] Added genres to track {track_id}: {new_genres}")
            except Exception as e:
                logging.error(f"[TAG] Error tagging track {track_id}: {e}")
                continue
        
        conn.commit()
        conn.close()
        
        response = {
            "success": True,
            "updated_count": updated_count,
            "genres": genres,
            "message": f"Added {len(genres)} genre(s) to {updated_count} track(s)"
        }
        
        if failed_files:
            response["warning"] = f"⚠️ Failed to update audio tags for: {', '.join(failed_files[:5])}{'...' if len(failed_files) > 5 else ''}"
        elif not has_mutagen:
            response["warning"] = "ℹ️ Genres updated in database. Install mutagen (pip install mutagen) to update audio files."
        
        return jsonify(response)
    except Exception as e:
        logging.error(f"[TAG] Error in bulk tag: {e}")
        return jsonify({"error": str(e)}), 500


# ============================================================================
# NAVIDROME METADATA TAG MANAGEMENT ENDPOINTS
# ============================================================================

@app.route("/api/tags/track/<track_id>", methods=["GET"])
def api_get_track_tags(track_id):
    """Get all editable metadata tags for a track"""
    try:
        from helpers.tag_manager import get_track_tags
        
        tags = get_track_tags(track_id)
        if not tags:
            return jsonify({"error": "Track not found"}), 404
        
        return jsonify({
            "success": True,
            "track_id": track_id,
            "tags": tags
        })
    except Exception as e:
        logging.error(f"[TAGS] Error getting track tags: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/tags/track/<track_id>", methods=["POST"])
def api_update_track_tags(track_id):
    """Update metadata tags for a single track"""
    try:
        from helpers.tag_manager import update_track_tags, sync_track_tags_to_file
        
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400
            
        tag_updates = data.get("tags", {})
        sync_to_file = data.get("sync_to_file", False)
        
        if not tag_updates:
            return jsonify({"error": "No tags to update"}), 400
        
        logging.debug(f"[TAGS] Updating track {track_id} with: {tag_updates}")
        
        # Update database
        try:
            success = update_track_tags(track_id, tag_updates)
            if not success:
                logging.error(f"[TAGS] update_track_tags returned False for track {track_id}")
                return jsonify({"error": "Failed to update tags in database"}), 500
        except Exception as e:
            logging.error(f"[TAGS] Error in update_track_tags: {type(e).__name__}: {e}")
            import traceback
            logging.error(traceback.format_exc())
            return jsonify({"error": f"Database error: {str(e)}"}), 500
        
        # Optionally sync back to audio file
        file_synced = False
        if sync_to_file:
            try:
                file_synced = sync_track_tags_to_file(track_id)
            except Exception as e:
                logging.warning(f"[TAGS] Error syncing tags to file for track {track_id}: {e}")
                # Don't fail the request, file sync is optional
        
        return jsonify({
            "success": True,
            "track_id": track_id,
            "updated_fields": len(tag_updates),
            "file_synced": file_synced,
            "message": f"Updated {len(tag_updates)} field(s) for track {track_id}"
        })
    except Exception as e:
        logging.error(f"[TAGS] Unexpected error updating track tags: {type(e).__name__}: {e}")
        import traceback
        logging.error(traceback.format_exc())
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500


@app.route("/api/tags/album/<path:album>/<path:artist>", methods=["GET"])
def api_get_album_tags(album, artist):
    """Get album-level metadata tags"""
    try:
        from urllib.parse import unquote
        from helpers.tag_manager import get_album_tags, check_field_conflicts
        
        album = unquote(album)
        artist = unquote(artist)
        
        tags = get_album_tags(album, artist)
        if not tags:
            return jsonify({"error": "Album not found"}), 404
        
        # Check for conflicts
        conflicts = check_field_conflicts(album, artist)
        
        return jsonify({
            "success": True,
            "album": album,
            "artist": artist,
            "tags": tags,
            "conflicts": conflicts if conflicts else None
        })
    except Exception as e:
        logging.error(f"[TAGS] Error getting album tags: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/tags/album/<path:album>/<path:artist>", methods=["POST"])
def api_update_album_tags(album, artist):
    """Update metadata tags for all tracks in an album"""
    try:
        from urllib.parse import unquote
        from helpers.tag_manager import update_album_tags, sync_track_tags_to_file
        
        album = unquote(album)
        artist = unquote(artist)
        
        data = request.get_json()
        tag_updates = data.get("tags", {})
        selected_track_ids = data.get("track_ids", None)  # None = all tracks
        sync_to_files = data.get("sync_to_files", False)
        
        if not tag_updates:
            return jsonify({"error": "No tags to update"}), 400
        
        # Update database
        updated_count = update_album_tags(album, artist, tag_updates, selected_track_ids)
        
        if updated_count == 0:
            return jsonify({"error": "No tracks updated"}), 500
        
        # Optionally sync back to audio files
        synced_count = 0
        if sync_to_files:
            # Get track IDs to sync
            conn = get_db()
            cursor = conn.cursor()
            
            if selected_track_ids:
                placeholders = ", ".join(["?" for _ in selected_track_ids])
                cursor.execute(f"SELECT id FROM tracks WHERE album = ? AND artist = ? AND id IN ({placeholders})",
                              [album, artist] + selected_track_ids)
            else:
                cursor.execute("SELECT id FROM tracks WHERE album = ? AND artist = ?", (album, artist))
            
            track_ids = [row[0] for row in cursor.fetchall()]
            conn.close()
            
            for track_id in track_ids:
                if sync_track_tags_to_file(track_id):
                    synced_count += 1
        
        return jsonify({
            "success": True,
            "album": album,
            "artist": artist,
            "updated_count": updated_count,
            "synced_count": synced_count,
            "message": f"Updated {updated_count} track(s), synced {synced_count} file(s)"
        })
    except Exception as e:
        logging.error(f"[TAGS] Error updating album tags: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/tags/album/<path:album>/<path:artist>/conflicts", methods=["GET"])
def api_check_tag_conflicts(album, artist):
    """Check for conflicting metadata values in an album"""
    try:
        from urllib.parse import unquote
        from helpers.tag_manager import check_field_conflicts
        
        album = unquote(album)
        artist = unquote(artist)
        
        conflicts = check_field_conflicts(album, artist)
        
        return jsonify({
            "success": True,
            "album": album,
            "artist": artist,
            "has_conflicts": bool(conflicts),
            "conflicts": conflicts if conflicts else {}
        })
    except Exception as e:
        logging.error(f"[TAGS] Error checking tag conflicts: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/tags/sync/<track_id>", methods=["POST"])
def api_sync_track_to_file(track_id):
    """Sync database tags back to the audio file"""
    try:
        from helpers.tag_manager import sync_track_tags_to_file
        
        success = sync_track_tags_to_file(track_id)
        
        return jsonify({
            "success": success,
            "track_id": track_id,
            "message": "Tags synced to file" if success else "Failed to sync tags to file"
        })
    except Exception as e:
        logging.error(f"[TAGS] Error syncing track tags: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/genres/track/<track_id>", methods=["GET"])
def api_get_track_genres(track_id):
    """Get all genre/tag sources for a single track"""
    try:
        from genre_tag_aggregator import get_track_genres_summary
        
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT * FROM tracks WHERE id = ?
        """, (track_id,))
        
        track = cursor.fetchone()
        conn.close()
        
        if not track:
            logging.debug(f"[TRACK GENRES] Track not found: {track_id}")
            return jsonify({"error": "Track not found"}), 404
        
        # Convert to dict if needed
        if not isinstance(track, dict):
            track = dict(track)
        
        genres_summary = get_track_genres_summary(track)
        
        # Log if genres are empty
        total_genres = sum(len(genres) for genres in genres_summary.values())
        if total_genres == 0:
            logging.info(f"[TRACK GENRES] No genres for {track.get('title', 'Unknown')} - run scan to populate")
        else:
            logging.debug(f"[TRACK GENRES] Found {total_genres} genres for {track.get('title', 'Unknown')}")
        
        return jsonify({
            "success": True,
            "track_id": track_id,
            "track_title": track.get("title", "Unknown"),
            "artist": track.get("artist", "Unknown"),
            "genres": genres_summary
        })
    
    except Exception as e:
        logging.error(f"[GENRES] Error getting track genres: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/genres/album/<path:album>/<path:artist>", methods=["GET"])
def api_get_album_genres(album, artist):
    """Get aggregated genre/tag summary for an album"""
    try:
        from urllib.parse import unquote
        from genre_tag_aggregator import get_album_genres_summary
        
        album = unquote(album)
        artist = unquote(artist)
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Get all tracks for this album
        tracks = []
        for artist_clause in ["COALESCE(NULLIF(album_artist, ''), artist)", "artist"]:
            cursor.execute(f"""
                SELECT * FROM tracks
                WHERE {artist_clause} = ? AND album = ?
                ORDER BY COALESCE(disc_number, 1), COALESCE(track_number, 999)
            """, (artist, album))
            
            tracks = cursor.fetchall()
            if tracks:
                break
        
        conn.close()
        
        if not tracks:
            return jsonify({"error": "Album not found"}), 404
        
        # Convert rows to dicts if needed
        if tracks and not isinstance(tracks[0], dict):
            tracks = [dict(t) for t in tracks]
        
        genres_summary = get_album_genres_summary(tracks, limit=25)
        
        return jsonify({
            "success": True,
            "album": album,
            "artist": artist,
            "track_count": len(tracks),
            "genres": genres_summary
        })
    
    except Exception as e:
        logging.error(f"[GENRES] Error getting album genres: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/genres/artist/<path:artist>", methods=["GET"])
def api_get_artist_genres(artist):
    """Get aggregated genre/tag summary for an artist (all tracks)"""
    try:
        from urllib.parse import unquote
        from genre_tag_aggregator import get_artist_genres_summary
        
        artist = unquote(artist)
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Get all tracks for this artist
        cursor.execute("""
            SELECT * FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = ?
            ORDER BY album, COALESCE(disc_number, 1), COALESCE(track_number, 999)
        """, (artist,))
        
        tracks = cursor.fetchall()
        conn.close()
        
        if not tracks:
            return jsonify({"error": "Artist not found"}), 404
        
        # Convert rows to dicts if needed
        if tracks and not isinstance(tracks[0], dict):
            tracks = [dict(t) for t in tracks]
        
        genres_summary = get_artist_genres_summary(tracks, limit=30)
        
        # Get album count
        album_count = len(set(t.get("album", "") for t in tracks if t.get("album", "")))
        
        return jsonify({
            "success": True,
            "artist": artist,
            "track_count": len(tracks),
            "album_count": album_count,
            "genres": genres_summary
        })
    
    except Exception as e:
        logging.error(f"[GENRES] Error getting artist genres: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/artist/<path:artist>/similar", methods=["GET"])
def api_get_similar_artists(artist):
    """Get similar artists for a given artist (from Last.fm and ListenBrainz)"""
    try:
        from urllib.parse import unquote
        import json
        
        artist = unquote(artist)
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Get similar artists from database
        cursor.execute("""
            SELECT similar_artists_lastfm, similar_artists_listenbrainz
            FROM artists
            WHERE name = ?
            LIMIT 1
        """, (artist,))
        
        row = cursor.fetchone()
        conn.close()
        
        if not row:
            logging.info(f"[SIMILAR ARTISTS] No artist record for {artist} - run scan to populate")
            return jsonify({
                "success": True,
                "artist": artist,
                "similar_artists": {
                    "lastfm": [],
                    "listenbrainz": []
                },
                "message": "Run 'Scan Artist' to fetch similar artists data"
            })
        
        # Parse JSON arrays
        similar_lastfm = []
        similar_listenbrainz = []
        
        try:
            if row[0]:
                similar_lastfm = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        except:
            pass
        
        try:
            if row[1]:
                similar_listenbrainz = json.loads(row[1]) if isinstance(row[1], str) else row[1]
        except:
            pass
        
        return jsonify({
            "success": True,
            "artist": artist,
            "similar_artists": {
                "lastfm": similar_lastfm[:10],  # Limit to 10
                "listenbrainz": similar_listenbrainz[:10]
            }
        })
    
    except Exception as e:
        logging.error(f"[SIMILAR ARTISTS] Error getting similar artists: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/library/artists/similar", methods=["GET"])
def api_library_similar_artists():
    """Get aggregated similar artists from all artists in the user's collection"""
    try:
        import json
        from collections import defaultdict
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Get all artists with their similar artists
        cursor.execute("""
            SELECT name, similar_artists_lastfm, similar_artists_listenbrainz
            FROM artists
            WHERE similar_artists_lastfm IS NOT NULL OR similar_artists_listenbrainz IS NOT NULL
            ORDER BY name
        """)
        
        rows = cursor.fetchall()
        conn.close()
        
        similar_cache = {}
        
        for row in rows:
            artist_name = row[0]
            similar_lastfm = row[1]
            similar_listenbrainz = row[2]
            
            # Process Last.fm similar artists
            if similar_lastfm:
                try:
                    artists = json.loads(similar_lastfm) if isinstance(similar_lastfm, str) else similar_lastfm
                    for similar in artists:
                        name = similar.get('name', '')
                        if name and name != artist_name:
                            key = (name.lower(), 'lastfm')
                            if key not in similar_cache:
                                similar_cache[key] = {'name': name, 'count': 0, 'match': 0, 'from_artists': set(), 'source': 'lastfm'}
                            similar_cache[key]['count'] += 1
                            if similar.get('match'):
                                similar_cache[key]['match'] += similar['match']
                            similar_cache[key]['from_artists'].add(artist_name)
                except:
                    pass
            
            # Process ListenBrainz similar artists
            if similar_listenbrainz:
                try:
                    artists = json.loads(similar_listenbrainz) if isinstance(similar_listenbrainz, str) else similar_listenbrainz
                    for similar in artists:
                        name = similar.get('name', '')
                        if name and name != artist_name:
                            key = (name.lower(), 'listenbrainz')
                            if key not in similar_cache:
                                similar_cache[key] = {'name': name, 'count': 0, 'match': 0, 'from_artists': set(), 'source': 'listenbrainz'}
                            similar_cache[key]['count'] += 1
                            similar_cache[key]['from_artists'].add(artist_name)
                except:
                    pass
        
        # Format results
        results = []
        for data in similar_cache.values():
            count = data.get('count', 0)
            match = data.get('match', 0)
            avg_match = (match / count) if count > 0 else 0
            from_artists = data.get('from_artists', set())
            from_artist = list(from_artists)[0] if from_artists else ''
            
            results.append({
                'name': data['name'],
                'source': data['source'],
                'count': count,
                'match': avg_match,
                'from_artist': from_artist
            })
        
        # Sort by frequency then by match score
        results.sort(key=lambda x: (-x['count'], -x['match']))
        
        # Limit results
        results = results[:100]
        
        return jsonify({
            "success": True,
            "similar_artists": results,
            "total_similar": len(results)
        })
    
    except Exception as e:
        logging.error(f"[LIBRARY SIMILAR] Error aggregating similar artists: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/debug/genres/<path:album>/<path:artist>", methods=["GET"])
def api_debug_album_genres(album, artist):
    """Debug endpoint to check genre data in database for album"""
    try:
        from urllib.parse import unquote
        
        album = unquote(album)
        artist = unquote(artist)
        
        conn = get_db()
        cursor = conn.cursor()
        
        rows = []
        # Get all tracks for this album
        for artist_clause in ["COALESCE(NULLIF(album_artist, ''), artist)", "artist"]:
            cursor.execute(f"""
                SELECT id, title, 
                       spotify_genres, lastfm_tags, listenbrainz_genres, 
                       discogs_genres, musicbrainz_genres
                FROM tracks
                WHERE {artist_clause} = ? AND album = ?
                LIMIT 20
            """, (artist, album))
            
            rows = cursor.fetchall()
            if rows:
                break
        
        conn.close()
        
        # Format for debugging
        debug_data = {
            "album": album,
            "artist": artist,
            "track_count": len(rows) if rows else 0,
            "tracks": []
        }
        
        if rows:
            for row in rows:
                track_id, title = row[0], row[1]
                spotify_genres = row[2]
                lastfm_tags = row[3]
                listenbrainz_genres = row[4]
                discogs_genres = row[5]
                musicbrainz_genres = row[6]
                
                debug_data["tracks"].append({
                    "id": track_id,
                    "title": title,
                    "spotify_genres": {
                        "raw": spotify_genres,
                        "length": len(spotify_genres) if spotify_genres else 0,
                        "is_null": spotify_genres is None
                    },
                    "lastfm_tags": {
                        "raw": lastfm_tags,
                        "length": len(lastfm_tags) if lastfm_tags else 0,
                        "is_null": lastfm_tags is None
                    },
                    "listenbrainz_genres": {
                        "raw": listenbrainz_genres,
                        "length": len(listenbrainz_genres) if listenbrainz_genres else 0,
                        "is_null": listenbrainz_genres is None
                    },
                    "discogs_genres": {
                        "raw": discogs_genres,
                        "length": len(discogs_genres) if discogs_genres else 0,
                        "is_null": discogs_genres is None
                    },
                    "musicbrainz_genres": {
                        "raw": musicbrainz_genres,
                        "length": len(musicbrainz_genres) if musicbrainz_genres else 0,
                        "is_null": musicbrainz_genres is None
                    }
                })
        
        return jsonify(debug_data), 200
        
    except Exception as e:
        logging.error(f"[DEBUG GENRES] Error: {e}")
        import traceback
        return jsonify({"error": str(e), "traceback": traceback.format_exc()}), 500



@app.route("/api/album/search-art")
def api_album_search_art():
    """Search for album art on MusicBrainz, Discogs, Spotify, or Apple Music"""
    artist_name = request.args.get("artist", "").strip()
    album_name = request.args.get("album", "").strip()
    source = request.args.get("source", "musicbrainz").strip()
    
    if not artist_name or not album_name:
        return jsonify({"error": "Artist and album name required"}), 400
    
    logger = logging.getLogger('sptnr')
    try:
        images = []
        
        if source == "musicbrainz":
            # Search for release-group
            search_url = "https://musicbrainz.org/ws/2/release-group"
            params = {"query": f'release:"{album_name}" AND artist:"{artist_name}"', "fmt": "json", "limit": 20}
            headers = {"User-Agent": MUSICBRAINZ_USER_AGENT}
            
            resp = requests.get(search_url, params=params, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            
            logger.debug(f"MusicBrainz search returned {len(data.get('release-groups', []))} results")
            
            for rg in data.get("release-groups", [])[:20]:
                rg_id = rg.get("id")
                if rg_id:
                    # Try multiple image formats from CAA
                    for image_format in ["front-500", "front-250", "front"]:
                        image_url = f"https://coverartarchive.org/release-group/{rg_id}/{image_format}"
                        
                        # Verify the URL exists before adding
                        try:
                            head_resp = requests.head(image_url, timeout=3)
                            if head_resp.status_code == 200:
                                images.append({
                                    "url": image_url,
                                    "source": "MusicBrainz CAA",
                                    "title": rg.get("title", ""),
                                    "artist": rg.get("artist-credit", [{}])[0].get("name", "") if rg.get("artist-credit") else ""
                                })
                                break  # Found one, don't try other formats for this RG
                        except Exception as e:
                            logger.debug(f"HEAD request failed for {image_url}: {e}")
                            continue
        
        elif source == "discogs":
            # Search Discogs for release
            from popularity import _discogs_search, _get_discogs_session
            
            config_data, _ = _read_yaml(CONFIG_PATH)
            discogs_config = config_data.get("api_integrations", {}).get("discogs", {})
            discogs_token = discogs_config.get("token", "")
            
            session = _get_discogs_session()
            headers = {"User-Agent": "Sptnr/1.0"}
            if discogs_token:
                headers["Authorization"] = f"Discogs token={discogs_token}"
            
            # Search with album and artist - try different query formats
            for query in [f"{artist_name} {album_name}", f'"{album_name}" {artist_name}', album_name]:
                try:
                    logger.debug(f"Searching Discogs with query: {query}")
                    results = _discogs_search(session, headers, query, kind="release", per_page=15)
                    
                    for result in results[:15]:
                        if result.get("cover_image"):
                            # Verify the image URL is valid
                            try:
                                img_resp = requests.head(result["cover_image"], timeout=3)
                                if img_resp.status_code == 200:
                                    images.append({
                                        "url": result["cover_image"],
                                        "source": "Discogs",
                                        "title": result.get("title", ""),
                                        "artist": ", ".join([a.get("name", "") for a in result.get("artists", [])])
                                    })
                            except Exception as e:
                                logger.debug(f"Image verification failed for Discogs: {e}")
                                continue
                    
                    if images:
                        logger.debug(f"Found {len(images)} images on Discogs")
                        break  # Stop if we found images
                except Exception as e:
                    logger.debug(f"Discogs search with query '{query}' failed: {e}")
                    continue
        
        elif source == "applemusic":
            # Search Apple Music/iTunes for album
            from api_clients.applemusic import AppleMusicClient
            
            apple_music = AppleMusicClient()
            
            try:
                logger.debug(f"Searching Apple Music for album: {artist_name} - {album_name}")
                results = apple_music.search_album(album_name, artist_name, limit=15)
                
                for album in results:
                    artwork_url = album.get("artworkUrl100", "")
                    if artwork_url:
                        # Replace 100x100 with higher resolution
                        # iTunes URLs use /100x100bb. or /100x100. patterns before file extension
                        if "/100x100bb." in artwork_url:
                            artwork_url = artwork_url.replace("/100x100bb.", "/600x600bb.")
                        elif "/100x100." in artwork_url:
                            artwork_url = artwork_url.replace("/100x100.", "/600x600.")
                        
                        images.append({
                            "url": artwork_url,
                            "source": "Apple Music",
                            "title": album.get("collectionName", ""),
                            "artist": album.get("artistName", "")
                        })
                
                if images:
                    logger.debug(f"Found {len(images)} images on Apple Music")
            except Exception as e:
                logger.debug(f"Apple Music search failed: {e}")
        
        logger.info(f"Album art search for '{artist_name} - {album_name}' via {source}: {len(images)} images found")
        return jsonify({"images": images})
        
    except Exception as e:
        logger.error(f"Error searching album art: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({"error": str(e), "images": []}), 500


@app.route("/api/album/set-art", methods=["POST"])
def api_album_set_art():
    """Set custom album art"""
    data = request.json or {}
    artist_name = data.get("artist", "").strip()
    album_name = data.get("album", "").strip()
    image_url = data.get("image_url", "").strip()
    
    if not artist_name or not album_name or not image_url:
        return jsonify({"error": "Artist, album name, and image URL required"}), 400
    
    # Validate that image_url is a valid HTTP/HTTPS URL, not a data URI or other scheme
    if not image_url.startswith(('http://', 'https://')):
        return jsonify({"error": "Image URL must be a valid HTTP or HTTPS URL"}), 400
    
    logger = logging.getLogger('sptnr')
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Create album_art table if it doesn't exist
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS album_art (
                artist_name TEXT NOT NULL,
                album_name TEXT NOT NULL,
                image_url TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (artist_name, album_name)
            )
        """)
        
        # Insert or update
        cursor.execute("""
            INSERT OR REPLACE INTO album_art (artist_name, album_name, image_url, updated_at)
            VALUES (?, ?, ?, ?)
        """, (artist_name, album_name, image_url, datetime.now().isoformat()))
        
        conn.commit()
        conn.close()
        
        logger.info(f"Album art updated for '{artist_name} - {album_name}': {image_url}")
        return jsonify({"success": True, "message": "Album art updated"})
        
    except Exception as e:
        logger.error(f"Error setting album art: {e}")
        return jsonify({"error": str(e)}), 500


# ===== MusicBrainz Metadata API Endpoints =====

@app.route("/api/musicbrainz/tags/track", methods=["GET"])
def api_musicbrainz_tags_track():
    """Get MusicBrainz tags for a single track"""
    artist = request.args.get("artist", "").strip()
    album = request.args.get("album", "").strip()
    title = request.args.get("title", "").strip()
    
    if not (artist and album and title):
        return jsonify({"error": "artist, album, and title required"}), 400
    
    try:
        tags = get_musicbrainz_tags_for_track(artist, album, title)
        return jsonify({
            "success": True,
            "artist": artist,
            "album": album,
            "title": title,
            "tags": tags,
            "tags_count": len(tags)
        })
    except Exception as e:
        logging.error(f"Error fetching MusicBrainz tags for track: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/musicbrainz/tags/album", methods=["GET"])
def api_musicbrainz_tags_album():
    """Get MusicBrainz tags for all tracks in an album"""
    artist = request.args.get("artist", "").strip()
    album = request.args.get("album", "").strip()
    
    if not (artist and album):
        return jsonify({"error": "artist and album required"}), 400
    
    try:
        tracks = get_musicbrainz_tags_for_album(artist, album)
        
        # Count how many tracks have tags
        tracks_with_tags = sum(1 for track in tracks if len(track) > 1)  # >1 because title is always present
        
        return jsonify({
            "success": True,
            "artist": artist,
            "album": album,
            "tracks": tracks,
            "total_tracks": len(tracks),
            "tracks_with_mb_tags": tracks_with_tags
        })
    except Exception as e:
        logging.error(f"Error fetching MusicBrainz tags for album: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/musicbrainz/import/track", methods=["POST"])
def api_musicbrainz_import_track():
    """Import MusicBrainz tags from MP3 for a single track"""
    data = request.json or {}
    artist = data.get("artist", "").strip()
    album = data.get("album", "").strip()
    title = data.get("title", "").strip()
    
    if not (artist and album and title):
        return jsonify({"error": "artist, album, and title required"}), 400
    
    try:
        result = import_musicbrainz_tags_for_track(artist, album, title)
        return jsonify(result)
    except Exception as e:
        logging.error(f"Error importing MusicBrainz tags for track: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/musicbrainz/import/album", methods=["POST"])
def api_musicbrainz_import_album():
    """Import MusicBrainz tags from MP3s for all tracks in an album"""
    data = request.json or {}
    artist = data.get("artist", "").strip()
    album = data.get("album", "").strip()
    
    if not (artist and album):
        return jsonify({"error": "artist and album required"}), 400
    
    try:
        result = import_musicbrainz_tags_for_album(artist, album)
        return jsonify(result)
    except Exception as e:
        logging.error(f"Error importing MusicBrainz tags for album: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/musicbrainz/import/artist", methods=["POST"])
def api_musicbrainz_import_artist():
    """Import MusicBrainz tags from MP3s for all tracks by an artist"""
    data = request.json or {}
    artist = data.get("artist", "").strip()
    
    if not artist:
        return jsonify({"error": "artist required"}), 400
    
    try:
        result = import_musicbrainz_tags_for_artist(artist)
        return jsonify(result)
    except Exception as e:
        logging.error(f"Error importing MusicBrainz tags for artist: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/musicbrainz/tag/update", methods=["POST"])
def api_musicbrainz_tag_update():
    """Update a MusicBrainz tag in the database and optionally write to MP3"""
    data = request.json or {}
    artist = data.get("artist", "").strip()
    album = data.get("album", "").strip()
    title = data.get("title", "").strip()
    field_name = data.get("field", "").strip()
    field_value = data.get("value", "").strip()
    write_to_mp3 = data.get("write_to_mp3", False)
    
    if not (artist and album and title and field_name):
        return jsonify({"error": "artist, album, title, and field required"}), 400
    
    try:
        # Update database
        db_result = update_musicbrainz_tag_in_db(artist, album, title, field_name, field_value)
        
        if not db_result['success']:
            return jsonify(db_result), 400
        
        # Write to MP3 if requested
        mp3_result = None
        if write_to_mp3:
            mp3_result = write_musicbrainz_tag_to_mp3(artist, album, title, field_name, field_value)
            if not mp3_result['success']:
                # Still return OK but note the MP3 write failed
                return jsonify({
                    "success": True,
                    "database": db_result,
                    "mp3": mp3_result,
                    "message": "Updated database but failed to write to MP3"
                })
        
        return jsonify({
            "success": True,
            "database": db_result,
            "mp3": mp3_result,
            "message": "Updated successfully" + (" and written to MP3" if write_to_mp3 else "")
        })
        
    except Exception as e:
        logging.error(f"Error updating MusicBrainz tag: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/musicbrainz/tag/write-to-mp3", methods=["POST"])
def api_musicbrainz_tag_write_mp3():
    """Write MusicBrainz tags to MP3 file (without database update)"""
    data = request.json or {}
    artist = data.get("artist", "").strip()
    album = data.get("album", "").strip()
    title = data.get("title", "").strip()
    field_name = data.get("field", "").strip()
    field_value = data.get("value", "").strip()
    
    if not (artist and album and title and field_name):
        return jsonify({"error": "artist, album, title, and field required"}), 400
    
    try:
        result = write_musicbrainz_tag_to_mp3(artist, album, title, field_name, field_value)
        if result['success']:
            return jsonify(result)
        else:
            return jsonify(result), 400
    except Exception as e:
        logging.error(f"Error writing MusicBrainz tag to MP3: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/musicbrainz/tags/batch-update", methods=["POST"])
def api_musicbrainz_batch_update():
    """Update multiple MusicBrainz tags at once"""
    data = request.json or {}
    artist = data.get("artist", "").strip()
    album = data.get("album", "").strip()
    title = data.get("title", "").strip()
    tags = data.get("tags", {})  # Dict of field_name: field_value
    write_to_mp3 = data.get("write_to_mp3", False)
    
    if not (artist and album and title and tags):
        return jsonify({"error": "artist, album, title, and tags required"}), 400
    
    try:
        results = {
            "database": {},
            "mp3": {},
            "success_count": 0,
            "fail_count": 0
        }
        
        for field_name, field_value in tags.items():
            # Update database
            db_result = update_musicbrainz_tag_in_db(artist, album, title, field_name, field_value)
            results["database"][field_name] = db_result
            
            if db_result['success']:
                results["success_count"] += 1
                
                # Write to MP3 if requested
                if write_to_mp3:
                    mp3_result = write_musicbrainz_tag_to_mp3(artist, album, title, field_name, field_value)
                    results["mp3"][field_name] = mp3_result
            else:
                results["fail_count"] += 1
        
        results["success"] = results["fail_count"] == 0
        results["message"] = f"Updated {results['success_count']} tags, {results['fail_count']} failed"
        
        return jsonify(results)
        
    except Exception as e:
        logging.error(f"Error batch updating MusicBrainz tags: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


# ===== Compilation and Artist Credits API Endpoints =====

@app.route("/api/artist/compilations", methods=["GET"])
def api_artist_compilations():
    """Get all compilation tracks (featured artist appearances) for an artist"""
    artist = request.args.get("name", "").strip()
    
    if not artist:
        return jsonify({"error": "Artist name required"}), 400
    
    try:
        compilations = get_compilations_for_artist(artist)
        return jsonify({
            "success": True,
            "artist": artist,
            "compilations": compilations,
            "count": len(compilations)
        })
    except Exception as e:
        logging.error(f"Error fetching artist compilations: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/artist/main-tracks", methods=["GET"])
def api_artist_main_tracks():
    """Get all main tracks (where artist is album artist) for an artist"""
    artist = request.args.get("name", "").strip()
    
    if not artist:
        return jsonify({"error": "Artist name required"}), 400
    
    try:
        tracks = get_main_tracks_for_artist(artist)
        
        # Group by album
        albums = {}
        for track in tracks:
            album = track['album']
            if album not in albums:
                albums[album] = []
            albums[album].append(track)
        
        return jsonify({
            "success": True,
            "artist": artist,
            "albums": albums,
            "total_tracks": len(tracks),
            "album_count": len(albums)
        })
    except Exception as e:
        logging.error(f"Error fetching artist main tracks: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/artist/stats", methods=["GET"])
def api_artist_stats():
    """Get artist statistics (main tracks, compilations, collaborators)"""
    artist = request.args.get("name", "").strip()
    
    if not artist:
        return jsonify({"error": "Artist name required"}), 400
    
    try:
        stats = get_artist_stats(artist)
        return jsonify({
            "success": True,
            "stats": stats
        })
    except Exception as e:
        logging.error(f"Error fetching artist stats: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/compilations/import/track", methods=["POST"])
def api_compilations_import_track():
    """Import featured artists from MP3 for a single track"""
    data = request.json or {}
    artist = data.get("artist", "").strip()
    album = data.get("album", "").strip()
    title = data.get("title", "").strip()
    
    if not (artist and album and title):
        return jsonify({"error": "artist, album, and title required"}), 400
    
    try:
        result = import_featured_artists_for_track(artist, album, title)
        return jsonify(result)
    except Exception as e:
        logging.error(f"Error importing featured artists for track: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/compilations/import/album", methods=["POST"])
def api_compilations_import_album():
    """Import featured artists from MP3s for all tracks in an album"""
    data = request.json or {}
    artist = data.get("artist", "").strip()
    album = data.get("album", "").strip()
    
    if not (artist and album):
        return jsonify({"error": "artist and album required"}), 400
    
    try:
        result = import_featured_artists_for_album(artist, album)
        return jsonify(result)
    except Exception as e:
        logging.error(f"Error importing featured artists for album: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/artist/add", methods=["POST"])
def api_add_artist():
    """Manually add an artist and fetch all their releases from MusicBrainz."""
    data = request.json or {}
    artist_name = data.get("artist", "").strip()
    
    if not artist_name:
        return jsonify({"error": "Artist name is required"}), 400
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Check if artist already exists in tracks
        cursor.execute("SELECT COUNT(*) FROM tracks WHERE artist = ?", (artist_name,))
        result = cursor.fetchone()
        existing_count = result[0] if result else 0
        
        # Fetch all releases from MusicBrainz
        logging.info(f"[ADD_ARTIST] Fetching MusicBrainz releases for: {artist_name}")
        mb_releases = _fetch_musicbrainz_releases(artist_name, limit=200)
        
        if not mb_releases:
            conn.close()
            return jsonify({
                "error": f"No releases found on MusicBrainz for artist: {artist_name}",
                "artist": artist_name,
                "releases_found": 0
            }), 404
        
        # Get existing albums if artist exists
        existing_norm = set()
        if existing_count > 0:
            cursor.execute("SELECT DISTINCT album FROM tracks WHERE artist = ?", (artist_name,))
            existing_albums = [row[0] for row in cursor.fetchall()]
            existing_norm = {_normalize_release_title(a) for a in existing_albums if a}
        
        # Add all releases to missing_releases table
        added_count = 0
        for rg in mb_releases:
            # Check if already exists in library
            norm_title = _normalize_release_title(rg.get("title") or "")
            if norm_title and norm_title in existing_norm:
                continue
            
            # Skip compilations
            secondary = [s.lower() for s in rg.get("secondary_types") or []]
            if "compilation" in secondary:
                continue
            
            # Determine category
            primary_type = (rg.get("primary_type") or "").lower()
            category = "Album"
            if primary_type == "ep":
                category = "EP"
            elif primary_type == "single" or "single" in secondary:
                category = "Single"
            
            # Insert into missing_releases
            try:
                cursor.execute("""
                    INSERT OR REPLACE INTO missing_releases 
                    (artist, release_id, title, primary_type, first_release_date, cover_art_url, category, last_checked)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    artist_name,
                    rg.get("id", ""),
                    rg.get("title", ""),
                    rg.get("primary_type", "Album"),
                    rg.get("first_release_date", ""),
                    rg.get("cover_art_url", ""),
                    category,
                    datetime.now().isoformat()
                ))
                added_count += 1
            except Exception as e:
                logging.error(f"[ADD_ARTIST] Error inserting release {rg.get('title')}: {e}")
        
        conn.commit()
        
        # Create artist_stats entry if it doesn't exist (so artist appears on artists page)
        if existing_count == 0:
            cursor.execute("""
                INSERT OR IGNORE INTO artist_stats 
                (artist_name, last_updated)
                VALUES (?, CURRENT_TIMESTAMP)
            """, (artist_name,))
            conn.commit()
        
        conn.close()
        
        logging.info(f"[ADD_ARTIST] Added {added_count} missing releases for {artist_name}")
        
        return jsonify({
            "success": True,
            "artist": artist_name,
            "releases_found": len(mb_releases),
            "added_to_missing": added_count,
            "already_in_library": len(existing_norm),
            "artist_exists": existing_count > 0
        })
        
    except Exception as e:
        logging.error(f"[ADD_ARTIST] Error adding artist {artist_name}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/album/<path:artist>/<path:album>")
def album_detail(artist, album):
    """View album details and tracks"""
    try:
        # URL decode the artist and album names
        from urllib.parse import unquote
        artist = unquote(artist)
        album = unquote(album)
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Query by COALESCE(album_artist, artist) to match how artists are listed
        # Use NULLIF to treat empty strings as NULL for proper COALESCE behavior
        # Try both COALESCE and plain artist for backwards compatibility with old links
        tracks_data = None
        for artist_clause in ["COALESCE(NULLIF(album_artist, ''), artist)", "artist"]:
            cursor.execute(f"""
                SELECT *
                FROM tracks
                WHERE {artist_clause} = ? AND album = ?
                ORDER BY COALESCE(disc_number, 1), COALESCE(track_number, 999), title COLLATE NOCASE
            """, (artist, album))
            tracks_data = cursor.fetchall()
            if tracks_data:
                break
        
        if not tracks_data:
            return render_template("album.html",
                                 artist_name=artist,
                                 album_name=album,
                                 tracks=[],
                                 tracks_by_disc={},
                                 album_data=None,
                                 album_genres=[],
                                 qbit_config={"enabled": False, "web_url": "http://localhost:8080"},
                                 slskd_config={"enabled": False},
                                 error="Album not found")
        
        # Get album metadata from first track
        try:
            cursor.execute("""
                SELECT 
                    COUNT(*) as track_count,
                    AVG(stars) as avg_stars,
                    SUM(COALESCE(duration, 0)) as total_duration,
                    MAX(spotify_release_date) as spotify_release_date,
                    MAX(spotify_album_type) as spotify_album_type,
                    MAX(spotify_album_art_url) as spotify_album_art_url,
                    MAX(last_scanned) as last_scanned,
                    MAX(COALESCE(disc_number, 1)) as total_discs,
                    MAX(musicbrainz_album_mbid) as musicbrainz_album_mbid,
                    COALESCE(MAX(discogs_album_id), MAX(discogs_release_id)) as discogs_album_id,
                    MAX(spotify_album_id) as spotify_album_id,
                    MAX(spotify_artist_id) as spotify_artist_id,
                    MAX(discogs_artist_id) as discogs_artist_id
                FROM tracks
                WHERE COALESCE(NULLIF(album_artist, ''), artist) = ? AND album = ?
            """, (artist, album))
        except:
            # Fallback for databases without beets columns or album_artist column
            cursor.execute("""
                SELECT 
                    COUNT(*) as track_count,
                    AVG(stars) as avg_stars,
                    SUM(COALESCE(duration, 0)) as total_duration,
                    MAX(spotify_release_date) as spotify_release_date,
                    MAX(spotify_album_type) as spotify_album_type,
                    MAX(spotify_album_art_url) as spotify_album_art_url,
                    MAX(last_scanned) as last_scanned,
                    MAX(COALESCE(disc_number, 1)) as total_discs,
                    NULL as musicbrainz_album_mbid,
                    NULL as discogs_album_id,
                    NULL as spotify_album_id,
                    NULL as spotify_artist_id,
                    NULL as discogs_artist_id
                FROM tracks
                WHERE COALESCE(album_artist, artist) = ? AND album = ?
            """, (artist, album))
        album_data = cursor.fetchone()
        
        # Convert to dict if it's a Row object
        if album_data:
            album_data = dict(album_data)
        else:
            album_data = {
                'track_count': 0,
                'avg_stars': 0,
                'total_duration': 0,
                'spotify_release_date': None,
                'spotify_album_type': None,
                'spotify_album_art_url': None,
                'last_scanned': None,
                'total_discs': 1
            }
        
        # Count singles in this album (based on is_single = 1 from single detection)
        cursor.execute("""
            SELECT COUNT(*) as singles_count
            FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = ? AND album = ? AND is_single = 1
        """, (artist, album))
        singles_row = cursor.fetchone()
        album_data['singles_count'] = singles_row['singles_count'] if singles_row else 0
        
        # Aggregate genres from tracks in this album - use navidrome_genres which comes from Navidrome
        cursor.execute("""
            SELECT DISTINCT navidrome_genres FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = ? AND album = ? AND navidrome_genres IS NOT NULL AND navidrome_genres != ''
        """, (artist, album))
        genre_rows = cursor.fetchall()
        album_genres = set()
        for row in genre_rows:
            try:
                genre_value = row['navidrome_genres'] if isinstance(row, dict) else row[0]
                if genre_value:
                    # Split on both backslash (Navidrome) and comma (user-entered)
                    genres = [g.strip() for g in genre_value.replace('\\', ',').split(',') if g.strip()]
                    album_genres.update(genres)
            except (KeyError, IndexError, TypeError) as e:
                logging.debug(f"Error parsing genre row: {e}")
                continue
        
        # Calculate genre fit for each track
        tracks_with_genre_fit = []
        for track in tracks_data:
            try:
                # Convert Row to dict if needed
                if hasattr(track, 'keys'):
                    track_dict = dict(track)
                else:
                    # Already a dict or tuple, try to convert
                    track_dict = track if isinstance(track, dict) else dict(track)
                
                # Parse track's genres - use navidrome_genres which comes from Navidrome
                track_genres = set()
                if track_dict.get('navidrome_genres'):
                    # navidrome_genres uses backslash separator, handle that
                    if isinstance(track_dict['navidrome_genres'], str):
                        # Split on both backslash and comma to handle both formats
                        track_genres.update([g.strip() for g in track_dict['navidrome_genres'].replace('\\', ',').replace('•', ',').split(',') if g.strip()])
                    elif isinstance(track_dict['navidrome_genres'], list):
                        # If it's already a list, just use it
                        track_genres.update(track_dict['navidrome_genres'])
                
                # Calculate how many album genres this track contains
                genre_matches = len(track_genres & album_genres) if album_genres else 0
                genre_fit_percent = int((genre_matches / len(album_genres) * 100) if album_genres else 0)
                
                track_dict['genre_matches'] = genre_matches
                track_dict['genre_fit_percent'] = genre_fit_percent
                track_dict['matching_genres'] = sorted(list(track_genres & album_genres))
                
                tracks_with_genre_fit.append(track_dict)
            except Exception as e:
                logging.debug(f"Error calculating genre fit: {e}")
                track_dict = dict(track) if hasattr(track, 'keys') else track
                if not isinstance(track_dict, dict):
                    track_dict = {'title': str(track)}
                track_dict['genre_matches'] = 0
                track_dict['genre_fit_percent'] = 0
                track_dict['matching_genres'] = []
                tracks_with_genre_fit.append(track_dict)
        
        # Group tracks by disc number
        tracks_by_disc = {}
        for track_dict in tracks_with_genre_fit:
            try:
                disc_num = track_dict.get('disc_number') if isinstance(track_dict, dict) else (track_dict['disc_number'] if hasattr(track_dict, '__getitem__') else 1)
                disc_num = disc_num or 1
                
                if disc_num not in tracks_by_disc:
                    tracks_by_disc[disc_num] = []
                tracks_by_disc[disc_num].append(track_dict)
            except Exception as e:
                logging.debug(f"Error processing track for disc grouping: {e}")
                # Fallback to disc 1
                if 1 not in tracks_by_disc:
                    tracks_by_disc[1] = []
                tracks_by_disc[1].append(track_dict)
        
        # Get the actual artist name from the database (from first track)
        # This ensures the form is populated with the actual database value, not the URL value
        # which might have encoding issues
        # Note: tracks_with_genre_fit contains dictionaries (converted from Row objects above)
        db_artist_name = artist  # default to URL parameter
        db_album_name = album    # default to URL parameter
        if tracks_with_genre_fit:
            first_track = tracks_with_genre_fit[0]
            # Use album_artist if available, otherwise use artist
            db_artist_name = first_track.get('album_artist') or first_track.get('artist') or artist
            db_album_name = first_track.get('album') or album
        
        conn.close()
        
        # Get qBittorrent and slskd config
        cfg = get_config()
        qbit_config = cfg.get("qbittorrent", {"enabled": False, "web_url": "http://localhost:8080"})
        slskd_config = cfg.get("slskd", {"enabled": False})
        
        return render_template("album.html",
                             artist_name=db_artist_name,
                             album_name=db_album_name,
                             tracks=tracks_with_genre_fit,
                             tracks_by_disc=tracks_by_disc,
                             album_data=album_data,
                             album_genres=sorted(list(album_genres)),
                             qbit_config=qbit_config,
                             slskd_config=slskd_config)
    except Exception as e:
        import traceback
        logging.error(f"Error loading album {artist}/{album}: {e}")
        logging.error(traceback.format_exc())
        
        # Get config even for error page
        try:
            cfg = get_config()
            qbit_config = cfg.get("qbittorrent", {"enabled": False, "web_url": "http://localhost:8080"})
            slskd_config = cfg.get("slskd", {"enabled": False})
        except:
            qbit_config = {"enabled": False, "web_url": "http://localhost:8080"}
            slskd_config = {"enabled": False}
        
        return render_template("album.html",
                             artist_name=artist,
                             album_name=album,
                             tracks=[],
                             tracks_by_disc={},
                             album_data=None,
                             album_genres=[],
                             qbit_config=qbit_config,
                             slskd_config=slskd_config,
                             error=f"Error loading album: {str(e)}")


def _run_artist_scan_pipeline(artist_name: str):
    """
    Helper function to run the complete scan pipeline for an artist:
    1. Navidrome import (imports metadata from Navidrome)
    2. Popularity detection (Spotify, Last.fm, ListenBrainz) + Singles detection + Star rating
    
    All steps log to unified_scan.log and Recent Scans page.
    This is used by artist scan, album rescan, and track rescan routes.
    
    Note: Force is always True for single artist/album scans to ensure fresh data.
    """
    # Write to file immediately to confirm function is called
    try:
        with open('/tmp/artist_scan_debug.log', 'a') as f:
            f.write(f"{datetime.now()}: _run_artist_scan_pipeline called for {artist_name}\n")
    except:
        pass
    
    log_unified(f"🎤 Artist scan pipeline started for: {artist_name}")
    try:
        # Force is always True for single artist/album scans
        force = True
        log_unified(f"Force rescan: {force} (always enabled for single artist scans)")
        
        # Look up artist_id from cache; rebuild index if missing
        log_unified(f"Looking up artist_id for '{artist_name}' in database...")
        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT artist_id FROM artist_stats WHERE artist_name = ?", (artist_name,))
            row = cursor.fetchone()
            artist_id = row[0] if row else None
            log_unified(f"Database lookup result: artist_id={artist_id}")
        finally:
            conn.close()

        if not artist_id:
            log_unified(f"Artist ID not found in cache, rebuilding artist index...")
            idx = build_artist_index()
            artist_data = idx.get(artist_name, {})
            artist_id = artist_data.get("id") if artist_data else None
            log_unified(f"After index rebuild: artist_id={artist_id}")

        # If still no artist_id, check if this is a track artist (not an album artist)
        # Track artists appear in tracks.artist but not in artist_stats (which only has album artists)
        if not artist_id:
            log_unified(f"Artist not found as album artist, checking if track artist exists in database...")
            track_count = 0  # Initialize to avoid NameError if query fails
            conn = get_db()
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM tracks WHERE artist = ?", (artist_name,))
                result = cursor.fetchone()
                track_count = result[0] if result else 0
                log_unified(f"Found {track_count} tracks for '{artist_name}'")
            finally:
                conn.close()
            
            if track_count == 0:
                log_unified(f"❌ Scan aborted: no tracks found for '{artist_name}' - artist does not exist in library")
                return
            
            # Artist exists as track artist only
            # Skip Navidrome track import (tracks already imported via album artist)
            # But still fetch artist-level metadata (bio, images) from external APIs
            log_unified(f"Artist '{artist_name}' is a track artist (e.g., from Various Artists albums)")
            log_unified(f"Step 1/2: Fetching artist metadata for track artist '{artist_name}'")
            try:
                from helpers.scan_helpers import fetch_artist_metadata
                fetch_artist_metadata(artist_name, verbose=True)
                log_unified(f"Artist metadata fetched successfully")
            except ImportError as e:
                log_unified(f"Warning: Artist metadata fetch not available: {e}")
            except Exception as e:
                log_unified(f"Warning: Failed to fetch artist metadata: {e}")
            log_unified(f"Step 2/2: Running popularity scan for track artist '{artist_name}' (force={force})")
            popularity_scan(verbose=True, force=force, artist_filter=artist_name)
        else:
            # Normal flow: artist_id found, run both steps
            # Step 1: Import metadata from Navidrome for this artist
            log_unified(f"Step 1/2: Navidrome import for artist '{artist_name}' (force={force})")
            scan_artist_to_db(artist_name, artist_id, verbose=True, force=force)

            # Step 2: Run popularity scan for this artist (includes singles detection and star rating)
            log_unified(f"Step 2/2: Running popularity scan for artist '{artist_name}' (force={force})")
            popularity_scan(verbose=True, force=force, artist_filter=artist_name)
        
        log_unified(f"✅ Scan complete for artist '{artist_name}'")
    except Exception as e:
        log_unified(f"❌ Scan failed for {artist_name}: {e}")
        import traceback
        log_unified(f"Traceback: {traceback.format_exc()}")


def _auto_detect_album_type(artist_name: str, album_name: str):
    """
    Auto-detect and update album type based on Discogs format data (primary) or metadata heuristics (fallback).
    
    Priority order:
    1. Discogs format data (most reliable source)
       - If discogs_formats contains "EP" → "ep"
       - If discogs_formats contains "Single" → "single"
       - If discogs_is_single = 1 → "single"
    2. Metadata & track count heuristics (fallback if Discogs unavailable)
       - If == 1 track → Single
       - If >= 70% singles with < 5 total tracks → Single
       - If >= 50% singles with 3-6 total tracks → EP  
       - If >= 40% singles with 6-10 tracks → EP
       - If 3-6 tracks → EP (even with lower singles percentage)
       - Otherwise → Album
    
    IMPORTANT: Always prioritize Discogs format data when available. This ensures 
    albums like "Stark" (marked as "Album" by Spotify) are correctly classified as "EP"
    when Discogs indicates it's an EP.
    
    Args:
        artist_name: Name of the artist
        album_name: Name of the album
    """
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Get current album type, track counts, and Discogs format data
        cursor.execute("""
            SELECT 
                COUNT(*) as total_tracks,
                SUM(CASE WHEN is_single = 1 THEN 1 ELSE 0 END) as singles_count,
                MAX(spotify_album_type) as current_type,
                MAX(discogs_formats) as discogs_formats,
                MAX(discogs_is_single) as discogs_is_single
            FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = ? AND album = ?
        """, (artist_name, album_name))
        
        result = cursor.fetchone()
        if not result:
            conn.close()
            return
        
        total_tracks = result['total_tracks'] or 0
        singles_count = result['singles_count'] or 0
        current_type = result['current_type']
        discogs_formats_raw = result['discogs_formats']
        discogs_is_single = result['discogs_is_single']
        
        if total_tracks == 0:
            conn.close()
            return
        
        # Determine new type using priority order
        new_type = None
        classification_reason = ""
        
        # ===== PRIORITY 1: Discogs Format Data (Most Reliable) =====
        if discogs_formats_raw:
            try:
                discogs_formats = json.loads(discogs_formats_raw)
                formats_lower = [str(fmt).lower() for fmt in discogs_formats]
                
                # Check for EP in formats
                if any('ep' in fmt for fmt in formats_lower):
                    new_type = 'ep'
                    classification_reason = f"Discogs format contains 'EP'"
                # Check for Single in formats
                elif any('single' in fmt for fmt in formats_lower):
                    new_type = 'single'
                    classification_reason = f"Discogs format contains 'Single'"
            except (json.JSONDecodeError, TypeError):
                # If parsing fails, fall through to heuristics
                pass
        
        # Check discogs_is_single flag if format check didn't yield result
        # But sanity check: ignore if album has 6+ tracks (can't be a single)
        if not new_type and discogs_is_single == 1 and total_tracks < 6:
            new_type = 'single'
            classification_reason = "Discogs confirmed as single"
        
        # ===== PRIORITY 2: Metadata & Track Count Heuristics =====
        if not new_type:
            # Calculate singles percentage
            singles_percent = (singles_count / total_tracks * 100) if total_tracks > 0 else 0
            
            # Determine type based on track count and singles percentage
            if total_tracks == 1:
                new_type = 'single'
                classification_reason = "Single track"
            elif singles_percent >= 70 and total_tracks < 5:
                new_type = 'single'
                classification_reason = f"{singles_percent:.0f}% singles, {total_tracks} total tracks"
            elif singles_percent >= 50 and 3 <= total_tracks <= 6:
                new_type = 'ep'
                classification_reason = f"{singles_percent:.0f}% singles, {total_tracks} total tracks"
            elif singles_percent >= 40 and 6 < total_tracks <= 10:
                new_type = 'ep'
                classification_reason = f"{singles_percent:.0f}% singles, {total_tracks} total tracks (6-10 track range)"
            elif 3 <= total_tracks <= 6:
                new_type = 'ep'
                classification_reason = f"{total_tracks} total tracks (EP heuristic)"
            else:
                new_type = 'album'
                classification_reason = f"{total_tracks} total tracks"
        
        # Update album type in database if it's different from current
        if new_type and new_type != current_type:
            cursor.execute("""
                UPDATE tracks 
                SET spotify_album_type = ?
                WHERE COALESCE(NULLIF(album_artist, ''), artist) = ? AND album = ?
            """, (new_type, artist_name, album_name))
            conn.commit()
            
            change_note = ""
            if current_type:
                change_note = f", changed from '{current_type}'"
            log_unified(f"✓ Album type set to '{new_type}' ({classification_reason}{change_note})")
        
        conn.close()
    except Exception as e:
        log_unified(f"Warning: Failed to auto-detect album type: {e}")


def _run_album_scan_pipeline(artist_name: str, album_name: str):
    """
    Helper function to run the complete scan pipeline for a specific album:
    1. Navidrome import (imports metadata from Navidrome for the album)
    2. Popularity detection (Spotify, Last.fm, ListenBrainz) + Singles detection + Star rating
    
    All steps log to unified_scan.log and Recent Scans page.
    This is used by the album rescan route.
    
    Args:
        artist_name: Name of the artist
        album_name: Name of the album to scan
    
    Note: Force is always True for single album scans. When a user explicitly requests
    a rescan for a specific album, we want to ensure we fetch fresh data from external
    sources and update all metadata, even if the album was recently scanned.
    """
    album_display = f"{artist_name} - {album_name}"
    log_unified(f"💿 Album scan pipeline started for: {album_display}")
    try:
        # Look up artist_id from cache; rebuild index if missing
        log_unified(f"Looking up artist_id for '{artist_name}' in database...")
        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT artist_id FROM artist_stats WHERE artist_name = ?", (artist_name,))
            row = cursor.fetchone()
            artist_id = row[0] if row else None
            log_unified(f"Database lookup result: artist_id={artist_id}")
        finally:
            conn.close()

        if not artist_id:
            log_unified(f"Artist ID not found in cache, rebuilding artist index...")
            idx = build_artist_index()
            artist_data = idx.get(artist_name, {})
            artist_id = artist_data.get("id") if artist_data else None
            log_unified(f"After index rebuild: artist_id={artist_id}")

        # If still no artist_id, check if this is a track artist (not an album artist)
        if not artist_id:
            log_unified(f"Artist not found as album artist, checking if track artist exists in database...")
            track_count = 0  # Initialize to avoid NameError if query fails
            conn = get_db()
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM tracks WHERE artist = ? AND album = ?", (artist_name, album_name))
                result = cursor.fetchone()
                track_count = result[0] if result else 0
                log_unified(f"Found {track_count} tracks for '{album_display}'")
            finally:
                conn.close()
            
            if track_count == 0:
                log_unified(f"❌ Scan aborted: no tracks found for '{album_display}' - album does not exist in library")
                return
            
            # Artist/album exists as track artist only - skip Navidrome import, go straight to popularity scan
            log_unified(f"Album '{album_display}' uses track artist (e.g., from Various Artists compilation)")
            log_unified(f"Skipping Navidrome import (Step 1/2) - album metadata already imported via album artist")
            log_unified(f"Step 2/2: Running popularity scan for album '{album_display}' (force=True)")
            popularity_scan(verbose=True, force=True, artist_filter=artist_name, album_filter=album_name)
            
            # Step 3: Auto-detect and set album type
            log_unified(f"Step 3/3: Auto-detecting album type for '{album_display}'")
            _auto_detect_album_type(artist_name, album_name)
        else:
            # Normal flow: artist_id found, run both steps
            # Step 1: Import metadata from Navidrome for this specific album
            # Force is always True for single album scans to ensure fresh data
            log_unified(f"Step 1/2: Navidrome import for album '{album_display}' (force=True)")
            scan_artist_to_db(artist_name, artist_id, verbose=True, force=True, album_filter=album_name)

            # Step 2: Run popularity scan for this specific album (includes singles detection and star rating)
            log_unified(f"Step 2/2: Running popularity scan for album '{album_display}' (force=True)")
            popularity_scan(verbose=True, force=True, artist_filter=artist_name, album_filter=album_name)
        
        # Step 3: Auto-detect and set album type based on singles detection
        log_unified(f"Step 3/3: Auto-detecting album type for '{album_display}'")
        _auto_detect_album_type(artist_name, album_name)
        
        log_unified(f"✅ Scan complete for album '{album_display}'")
    except Exception as e:
        log_unified(f"❌ Scan failed for {album_display}: {e}")
        import traceback
        log_unified(f"Traceback: {traceback.format_exc()}")



@app.route("/album/<path:artist>/<path:album>/edit", methods=["POST"])
def album_edit(artist, album):
    """Update album metadata for all tracks in the album"""
    from urllib.parse import unquote
    artist = unquote(artist)
    album = unquote(album)
    
    # Get form data
    album_title = request.form.get("album_title", "").strip()
    album_artist = request.form.get("album_artist", "").strip()
    track_artist = request.form.get("track_artist", "").strip()  # New: Track artist to apply to all tracks
    release_year = request.form.get("release_year", "").strip() or None
    album_type = request.form.get("album_type", "").strip() or None
    
    # Normalize album type - convert standalone "compilation" to "album+compilation"
    if album_type == "compilation":
        album_type = "album+compilation"
    
    album_mbid = request.form.get("album_mbid", "").strip() or None
    album_genres = request.form.get("album_genres", "").strip()
    
    # Debug logging for character encoding issues (use debug level to avoid log noise)
    logging.debug(f"Album edit - URL artist: {repr(artist)}, form album_artist: {repr(album_artist)}")
    logging.debug(f"Album edit - URL album: {repr(album)}, form album_title: {repr(album_title)}")
    logging.debug(f"Album edit - track_artist: {repr(track_artist)}")
    
    if not album_title or not album_artist:
        flash("Album title and artist are required", "danger")
        return redirect(url_for("album_detail", artist=artist, album=album))
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Update all tracks in this album
        update_fields = []
        update_values = []
        
        # Track if album/artist names changed
        names_changed = (album_artist != artist) or (album_title != album)
        
        # If album title or artist changed, update those
        if names_changed:
            update_fields.extend(["album = ?", "artist = ?"])
            update_values.extend([album_title, album_artist])
        
        # Update year if provided
        if release_year:
            update_fields.append("year = ?")
            update_values.append(int(release_year))
        
        # Update album type if provided
        if album_type:
            update_fields.append("spotify_album_type = ?")
            update_values.append(album_type)
        
        # Update album MBID if provided
        if album_mbid:
            update_fields.append("musicbrainz_album_mbid = ?")
            update_values.append(album_mbid)
        
        # Update genres
        if album_genres:
            update_fields.append("genres = ?")
            update_values.append(album_genres)
        
        # Add WHERE clause values
        update_values.extend([artist, album])
        
        # Execute update
        if update_fields:
            # Validate that update_fields only contains safe column assignments
            # All field assignments should be in the format "column_name = ?"
            allowed_columns = {'album', 'artist', 'year', 'spotify_album_type', 'musicbrainz_album_mbid', 'genres'}
            for field in update_fields:
                column_name = field.split('=')[0].strip()
                if column_name not in allowed_columns:
                    flash(f"Invalid column name in update: {column_name}", "danger")
                    return redirect(url_for("album_detail", artist=artist, album=album))
            
            sql = f"UPDATE tracks SET {', '.join(update_fields)} WHERE COALESCE(NULLIF(album_artist, ''), artist) = ? AND album = ?"
            cursor.execute(sql, update_values)
            rows_updated = cursor.rowcount
            conn.commit()
            
            flash(f"Updated {rows_updated} tracks in database", "success")
        else:
            flash("No changes to save", "info")
        
        # Redirect to the (potentially new) album page
        # To ensure special characters are preserved, query the database for the actual artist/album names
        # after the update, rather than relying on potentially corrupted form data
        # Reuse existing connection to avoid overhead of creating new connection
        try:
            # Get the actual artist and album names from the database after the update
            # Use the same COALESCE logic as album_detail to get the correct artist
            cursor.execute("""
                SELECT 
                    COALESCE(NULLIF(album_artist, ''), artist) as effective_artist,
                    album
                FROM tracks
                WHERE COALESCE(NULLIF(album_artist, ''), artist) = ?
                    AND album = ?
                LIMIT 1
            """, (album_artist, album_title))
            db_row = cursor.fetchone()
            
            if db_row:
                # db_row[0] = effective_artist, db_row[1] = album
                redirect_artist = db_row[0]
                redirect_album = db_row[1]
            else:
                # Fallback: use form data if names changed, otherwise use original URL params
                redirect_artist = album_artist if names_changed else artist
                redirect_album = album_title if names_changed else album
        except Exception as e:
            logging.warning(f"Error querying database for redirect: {e}")
            # Fallback: use form data if names changed, otherwise use original URL params
            redirect_artist = album_artist if names_changed else artist
            redirect_album = album_title if names_changed else album
        finally:
            conn.close()
        
        return redirect(url_for("album_detail", artist=redirect_artist, album=redirect_album))
        
    except Exception as e:
        logging.error(f"Error updating album: {e}")
        flash(f"Error updating album: {str(e)}", "danger")
        return redirect(url_for("album_detail", artist=artist, album=album))


@app.route("/album/<path:artist>/<path:album>/rescan", methods=["POST"])
def album_rescan(artist, album):
    """Trigger per-album pipeline: Navidrome fetch -> popularity -> single detection."""
    from urllib.parse import unquote
    artist = unquote(artist)
    album = unquote(album)

    threading.Thread(target=_run_album_scan_pipeline, args=(artist, album), daemon=True).start()
    flash(f"Rescan started for album '{album}' by {artist}", "info")
    return redirect(url_for("album_detail", artist=artist, album=album))


@app.route("/track/<path:artist>/<path:album>/<path:track_id>/rescan", methods=["POST"])
def scan_track_rescan(artist, album, track_id):
    """Trigger per-track rescan: Navidrome fetch -> popularity -> single detection."""
    from urllib.parse import unquote
    artist = unquote(artist)
    album = unquote(album)
    track_id = unquote(track_id)

    threading.Thread(target=_run_artist_scan_pipeline, args=(artist,), daemon=True).start()
    flash(f"Track rescan started for {artist}", "info")
    return redirect(url_for("track_detail", track_id=track_id))


@app.route("/track/<track_id>")
def track_detail(track_id):
    """View and edit track details"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM tracks WHERE id = ?", (track_id,))
        track = cursor.fetchone()
        
        if not track:
            conn.close()
            flash("Track not found", "error")
            return redirect(url_for("dashboard"))
        
        # Convert Row to dict to ensure all columns are accessible
        track = dict(track)
        
        # Parse genre fields - handle both JSON and comma-separated formats
        for genre_field in ['navidrome_genres', 'spotify_genres', 'lastfm_tags', 'discogs_genres', 'musicbrainz_genres']:
            if genre_field in track and track[genre_field]:
                genre_val = track[genre_field]
                try:
                    # Try to parse as JSON first
                    if isinstance(genre_val, str) and genre_val.startswith('['):
                        import json as json_module
                        parsed = json_module.loads(genre_val)
                        # Convert list to comma-separated string for template
                        if isinstance(parsed, list):
                            track[genre_field] = ", ".join([g.strip() for g in parsed if g.strip()])
                    # Handle backslash-separated strings (from Navidrome)
                    elif isinstance(genre_val, str) and '\\' in genre_val:
                        # Replace backslashes with commas for consistent display
                        track[genre_field] = genre_val.replace('\\', ',')
                    # Otherwise leave as-is (already comma-separated or string)
                except Exception:
                    pass  # Keep original value if parsing fails
        
        # Parse writer field (JSON array from Navidrome lyricist field)
        if 'writer' in track and track['writer']:
            writer_val = track['writer']
            try:
                if isinstance(writer_val, str) and writer_val.startswith('['):
                    import json as json_module
                    parsed = json_module.loads(writer_val)
                    if isinstance(parsed, list):
                        track['writer'] = ", ".join([w.strip() for w in parsed if w.strip()])
            except Exception:
                pass  # Keep original value if parsing fails
        
        # Get recommended genres from other tracks with similar titles or artists
        recommended_genres = []
        artist_name = track.get('artist', '')
        if artist_name:
            cursor.execute("""
                SELECT genres FROM tracks 
                WHERE artist = ? AND genres IS NOT NULL AND genres != ''
                LIMIT 10
            """, (artist_name,))
            genre_rows = cursor.fetchall()
            genre_set = set()
            for row in genre_rows:
                if row['genres']:
                    # Parse comma-separated genres
                    genres = [g.strip() for g in row['genres'].split(',') if g.strip()]
                    genre_set.update(genres)
            recommended_genres = sorted(list(genre_set))
        
        conn.close()
        
        # Load config for template
        try:
            cfg = get_config()
            qbit_config = cfg.get("qbittorrent", {"enabled": False, "web_url": "http://localhost:8080"})
            slskd_config = cfg.get("slskd", {"enabled": False})
        except Exception as e:
            logging.warning(f"Could not load config for track template: {e}")
            qbit_config = {"enabled": False, "web_url": "http://localhost:8080"}
            slskd_config = {"enabled": False}
        
        return render_template("track.html", track=track, recommended_genres=recommended_genres, track_id=track_id,
                             qbit_config=qbit_config, slskd_config=slskd_config)
    
    except Exception as e:
        logging.error(f"Error loading track {track_id}: {e}")
        import traceback
        logging.error(traceback.format_exc())
        flash(f"Error loading track: {str(e)}", "error")
        return redirect(url_for("dashboard"))


@app.route("/track/<track_id>/edit", methods=["POST"])
def track_edit(track_id):
    """Update track metadata and write to audio file"""
    conn = get_db()
    cursor = conn.cursor()
    
    # Get form data - ensure all string fields have defaults to avoid None when calling .strip()
    title = request.form.get("title", "").strip() or None
    artist = request.form.get("artist", "").strip() or None
    album = request.form.get("album", "").strip() or None
    stars = request.form.get("stars", type=int)
    is_single = 1 if request.form.get("is_single") == "on" else 0
    single_confidence = request.form.get("single_confidence", "low")
    mbid = request.form.get("mbid", "").strip() or None
    suggested_mbid = request.form.get("suggested_mbid", "").strip() or None
    suggested_mbid_confidence = request.form.get("suggested_mbid_confidence", type=float)
    
    # New MP3 metadata fields
    genres = request.form.get("genres", "").strip() or None
    year = request.form.get("year", "").strip() or None
    album_artist = request.form.get("album_artist", "").strip() or None
    composer = request.form.get("composer", "").strip() or None
    track_number = request.form.get("track_number", "").strip() or None
    disc_number = request.form.get("disc_number", type=int) or None
    comment = request.form.get("comment", "").strip() or None
    
    # First, get the file path from database
    cursor.execute("SELECT file_path FROM tracks WHERE id = ?", (track_id,))
    file_result = cursor.fetchone()
    file_path = file_result["file_path"] if file_result else None
    
    # Update database
    file_write_success = False
    try:
        cursor.execute("""
            UPDATE tracks
            SET title = ?, artist = ?, album = ?, stars = ?, is_single = ?, single_confidence = ?,
                mbid = ?, suggested_mbid = ?, suggested_mbid_confidence = ?,
                genres = ?, year = ?, album_artist = ?, composer = ?, 
                track_number = ?, disc_number = ?, comment = ?, single_manual_override = 1
            WHERE id = ?
        """, (title, artist, album, stars, is_single, single_confidence, mbid, suggested_mbid, 
              suggested_mbid_confidence, genres, year, album_artist, composer, 
              track_number, disc_number, comment, track_id))
        
        conn.commit()
        
        # Now write tags to audio file if file path exists
        if file_path:
            try:
                from helpers.tag_manager import write_tags_to_file
                
                # Prepare tags dictionary
                tags_to_write = {}
                if title:
                    tags_to_write["title"] = title
                if artist:
                    tags_to_write["artist"] = artist
                if album:
                    tags_to_write["album"] = album
                if album_artist:
                    tags_to_write["album_artist"] = album_artist
                if genres:
                    tags_to_write["genre"] = genres
                if year:
                    tags_to_write["year"] = year
                if composer:
                    tags_to_write["composer"] = composer
                if track_number:
                    tags_to_write["track_number"] = int(track_number) if track_number.isdigit() else track_number
                if disc_number:
                    tags_to_write["disc_number"] = disc_number
                if comment:
                    tags_to_write["comment"] = comment
                if mbid:
                    tags_to_write["mbid"] = mbid
                
                # Write to file
                file_write_success = write_tags_to_file(file_path, tags_to_write)
                
                if file_write_success:
                    flash(f"Track '{title or 'Unknown'}' updated successfully (DB + File)", "success")
                else:
                    flash(f"Track '{title or 'Unknown'}' updated in database, but failed to write to audio file", "warning")
            except ImportError as e:
                logging.warning(f"Tag manager import failed: {e}")
                flash(f"Track '{title or 'Unknown'}' updated successfully (database only - tag writer unavailable)", "info")
        else:
            flash(f"Track '{title or 'Unknown'}' updated successfully (database only - no file path found)", "info")
            
    except Exception as e:
        conn.rollback()
        logging.error(f"Error updating track: {e}")
        flash(f"Error updating track: {str(e)}", "danger")
    finally:
        conn.close()
    
    return redirect(url_for("track_detail", track_id=track_id))


@app.route("/api/track/<track_id>/toggle-manual-single", methods=["POST"])
def api_toggle_manual_single(track_id):
    """Toggle single_manual_override flag for a track (prevents auto-detection from overwriting)"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Get current value
        cursor.execute("SELECT single_manual_override FROM tracks WHERE id = ?", (track_id,))
        result = cursor.fetchone()
        
        if not result:
            return jsonify({"error": "Track not found"}), 404
        
        current_value = result[0] or 0
        new_value = 1 - current_value  # Toggle between 0 and 1
        
        # Update
        cursor.execute(
            "UPDATE tracks SET single_manual_override = ? WHERE id = ?",
            (new_value, track_id)
        )
        conn.commit()
        conn.close()
        
        return jsonify({
            "success": True,
            "track_id": track_id,
            "single_manual_override": new_value,
            "message": f"Single manual override {'enabled' if new_value else 'disabled'} - single detection scan will {'skip' if new_value else 'process'} this track"
        })
    except Exception as e:
        logging.error(f"Error toggling manual single flag: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/scan/start", methods=["POST"])
def scan_start():
    """Start a library scan"""
    global scan_process
    
    scan_type = request.form.get("scan_type", "full")
    artist = request.form.get("artist")
    
    logging.info(f"scan_start called: scan_type={scan_type}, artist={artist}")
    
    # Handle artist-specific scan differently - use threaded worker instead of subprocess
    if scan_type == "artist":
        if artist:
            logging.info(f"Starting artist scan thread for: {artist}")
            threading.Thread(target=_run_artist_scan_pipeline, args=(artist,), daemon=True).start()
            flash(f"Scan started for artist: {artist}", "success")
            return redirect(url_for("artist_detail", name=artist))
        else:
            logging.error("Artist scan requested but no artist name provided")
            flash("Error: No artist name provided", "danger")
            return redirect(url_for("dashboard"))
    
    # For batch/force scans, use subprocess as before
    with scan_lock:
        if scan_process and scan_process.poll() is None:
            flash("A scan is already running", "warning")
            return redirect(url_for("dashboard"))
        
        # Build command
        cmd = ["python", "/app/start.py"]
        
        if scan_type == "full":
            cmd.append("--full-scan")
        elif scan_type == "force":
            cmd.extend(["--full-scan", "--force"])
        
        # Start process
        scan_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        
        flash(f"Scan started: {scan_type}", "success")
    
    return redirect(url_for("dashboard"))


@app.route("/scan/unified", methods=["POST"])
def scan_unified():
    """Start the unified scan pipeline (popularity + singles)"""
    global scan_process
    
    with scan_lock:
        if scan_process and scan_process.poll() is None:
            flash("A scan is already running", "warning")
            return redirect(url_for("dashboard"))
        
        try:
            # Read force setting from config
            config_data, _ = _read_yaml(CONFIG_PATH)
            force = config_data.get("features", {}).get("force", False)
            
            # Start unified scan process
            cmd = [sys.executable, "unified_scan.py", "--verbose"]
            if force:
                cmd.append("--force")
            scan_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            flash("✅ Unified scan started (popularity → singles → ratings)", "success")
        except Exception as e:
            flash(f"❌ Error starting unified scan: {str(e)}", "danger")
    
    return redirect(url_for("dashboard"))


@app.route("/scan/popularity", methods=["POST"])
def scan_popularity_route():
    """Run popularity score update from external sources"""
    global scan_process_popularity
    
    # Get scan mode from query parameters (default: "all")
    mode = request.args.get('mode', 'all')  # all, force, missing, singles, resume, resume_force
    
    with scan_lock:
        # Check if scan is already running
        if scan_process_popularity is not None:
            if isinstance(scan_process_popularity, dict):
                thread = scan_process_popularity.get('thread')
                if thread and thread.is_alive():
                    flash("Popularity scan is already running", "warning")
                    return redirect(url_for("dashboard"))
            elif hasattr(scan_process_popularity, 'is_alive') and scan_process_popularity.is_alive():
                flash("Popularity scan is already running", "warning")
                return redirect(url_for("dashboard"))

        # Don't start popularity until Navidrome scan finishes (unless singles-only mode)
        if mode != 'singles':
            nav_running = False
            if scan_process_navidrome is not None:
                if isinstance(scan_process_navidrome, dict):
                    nav_thread = scan_process_navidrome.get('thread')
                    nav_running = nav_thread is not None and nav_thread.is_alive()
                elif hasattr(scan_process_navidrome, 'is_alive'):
                    nav_running = scan_process_navidrome.is_alive()
                elif hasattr(scan_process_navidrome, 'poll'):
                    nav_running = scan_process_navidrome.poll() is None

            if not nav_running:
                nav_progress_file = os.path.join(os.path.dirname(DB_PATH), "navidrome_scan_progress.json")
                try:
                    with open(nav_progress_file, "r", encoding="utf-8") as f:
                        nav_state = json.load(f)
                        nav_running = bool(nav_state.get("is_running"))
                except FileNotFoundError:
                    nav_running = False
                except Exception:
                    nav_running = False

            if nav_running:
                flash("Please wait for Navidrome scan to finish before starting popularity scan", "warning")
                return redirect(url_for("dashboard"))
        
        try:
            db_dir = os.path.dirname(DB_PATH)
            
            # Use different progress files for singles-only mode
            if mode == 'singles':
                popularity_progress_file = os.path.join(db_dir, "singles_scan_progress.json")
                _write_progress_file(popularity_progress_file, "singles_scan", True, {"status": "starting"})
            else:
                popularity_progress_file = os.path.join(db_dir, "popularity_scan_progress.json")
                _write_progress_file(popularity_progress_file, "popularity_scan", True, {"status": "starting"})

            # Run popularity scan in background thread instead of subprocess
            from popularity import popularity_scan as scan_popularity_func
            
            # Determine force and filter logic based on mode
            force_rescan = (mode == 'force' or mode == 'resume_force')
            filter_missing = (mode == 'missing')
            singles_only = (mode == 'singles')
            
            # Determine resume artist for resume mode
            resume_from_artist = None
            if mode == 'resume' or mode == 'resume_force':
                from scan_resume import get_last_scanned_artist
                resume_from_artist = get_last_scanned_artist(scan_type="popularity", db_path=DB_PATH)
                if resume_from_artist:
                    logging.info(f"Resume mode: Found last scanned artist '{resume_from_artist}'")
                else:
                    logging.warning("Resume mode: No last scanned artist found, starting from beginning")
            
            def run_popularity_scan_bg():
                try:
                    if singles_only:
                        logging.info(f"Starting singles-only scan in background")
                        scan_popularity_func(verbose=False, force=False, singles_only=True, resume_from=resume_from_artist)
                        _write_progress_with_current_artist(popularity_progress_file, "singles_scan", False, {"status": "complete", "exit_code": 0})
                        logging.info("Singles scan completed successfully")
                    else:
                        logging.info(f"Starting popularity score scan in background (force={force_rescan}, filter_missing={filter_missing}, resume_from={resume_from_artist})")
                        scan_popularity_func(verbose=False, force=force_rescan, filter_missing=filter_missing, resume_from=resume_from_artist)
                        _write_progress_with_current_artist(popularity_progress_file, "popularity_scan", False, {"status": "complete", "exit_code": 0})
                        logging.info("Popularity scan completed successfully")
                except Exception as e:
                    logging.error(f"Error in popularity scan: {e}", exc_info=True)
                    _write_progress_with_current_artist(popularity_progress_file, "popularity_scan" if not singles_only else "singles_scan", False, {"status": "error", "error": str(e), "exit_code": 1})
            
            scan_thread = threading.Thread(target=run_popularity_scan_bg, daemon=False)
            scan_thread.start()
            scan_process_popularity = {'thread': scan_thread, 'type': 'popularity'}
            
            if singles_only:
                flash("✅ Singles detection scan started (popularity only)", "success")
            else:
                mode_desc = {
                    'all': 'Full', 
                    'force': 'Full (Forced)', 
                    'missing': 'Missing Only',
                    'resume': 'Resume from Last',
                    'resume_force': 'Resume (Forced)'
                }.get(mode, 'Full')
                flash(f"✅ Popularity and singles scan started ({mode_desc} scan)", "success")
            logging.info("Popularity scan thread started successfully")
        except Exception as e:
            logging.error(f"Error starting popularity scan: {e}", exc_info=True)
            flash(f"❌ Error starting popularity scan: {str(e)}", "danger")
    
    return redirect(url_for("dashboard"))


@app.route("/scan/singles", methods=["POST"])
def scan_singles():
    """Run single detection"""
    global scan_process_singles
    
    with scan_lock:
        # Block singles until Navidrome sync finishes
        nav_running = False
        if scan_process_navidrome is not None:
            if isinstance(scan_process_navidrome, dict):
                nav_thread = scan_process_navidrome.get('thread')
                nav_running = nav_thread is not None and nav_thread.is_alive()
            elif hasattr(scan_process_navidrome, 'is_alive'):
                nav_running = scan_process_navidrome.is_alive()
            elif hasattr(scan_process_navidrome, 'poll'):
                nav_running = scan_process_navidrome.poll() is None

        if not nav_running:
            nav_progress_file = os.path.join(os.path.dirname(DB_PATH), "navidrome_scan_progress.json")
            try:
                with open(nav_progress_file, "r", encoding="utf-8") as f:
                    nav_state = json.load(f)
                    nav_running = bool(nav_state.get("is_running"))
            except FileNotFoundError:
                nav_running = False
            except Exception:
                nav_running = False

        if nav_running:
            flash("Please wait for Navidrome scan to finish before starting singles detection", "warning")
            return redirect(url_for("dashboard"))

        if scan_process_singles and scan_process_singles.poll() is None:
            flash("Single detection scan is already running", "warning")
            return redirect(url_for("dashboard"))
        
        # Note: Standalone singles detection script is not available.
        # Singles are detected during popularity scans via is_discogs_single() in api_clients.discogs
        flash("❌ Standalone single detection is not available. Singles are detected during popularity scans.", "warning")
    
    return redirect(url_for("dashboard"))


@app.route("/scan/stop", methods=["POST"])
def scan_stop():
    """Stop the running scan (main scan process)"""
    global scan_process
    
    with scan_lock:
        if scan_process and scan_process.poll() is None:
            scan_process.terminate()
            scan_process.wait(timeout=10)
            flash("Main scan stopped", "info")
        else:
            flash("No main scan is currently running", "warning")
    
    return redirect(url_for("dashboard"))


@app.route("/scan/stop-navidrome", methods=["POST"])
def scan_stop_navidrome():
    """Stop the Navidrome sync scan"""
    global scan_process_navidrome
    
    with scan_lock:
        if scan_process_navidrome is not None:
            if isinstance(scan_process_navidrome, dict):
                thread = scan_process_navidrome.get('thread')
                if thread and thread.is_alive():
                    # Threads can't be forcefully stopped in Python, so we just mark it as stopped
                    # The scan will check for stop signals and exit gracefully
                    scan_process_navidrome = None
                    flash("Navidrome sync scan stop requested (will finish current artist)", "info")
                else:
                    flash("No Navidrome sync scan is currently running", "warning")
            else:
                flash("No Navidrome sync scan is currently running", "warning")
        else:
            flash("No Navidrome sync scan is currently running", "warning")
    
    return redirect(url_for("dashboard"))


@app.route("/scan/stop-popularity", methods=["POST"])
def scan_stop_popularity():
    """Stop the popularity scan"""
    global scan_process_popularity
    
    with scan_lock:
        if scan_process_popularity is not None:
            if isinstance(scan_process_popularity, dict):
                thread = scan_process_popularity.get('thread')
                if thread and thread.is_alive():
                    # Threads can't be forcefully stopped in Python, so we just mark it as stopped
                    # The scan will check for stop signals and exit gracefully
                    scan_process_popularity = None
                    flash("Popularity scan stop requested (will finish current track)", "info")
                else:
                    flash("No popularity scan is currently running", "warning")
            else:
                flash("No popularity scan is currently running", "warning")
        else:
            flash("No popularity scan is currently running", "warning")
    
    return redirect(url_for("dashboard"))


@app.route("/scan/stop-singles", methods=["POST"])
def scan_stop_singles():
    """Stop the single detection scan"""
    global scan_process_singles
    
    with scan_lock:
        if scan_process_singles is not None:
            if isinstance(scan_process_singles, dict):
                thread = scan_process_singles.get('thread')
                if thread and thread.is_alive():
                    # Threads can't be forcefully stopped in Python, so we just mark it as stopped
                    # The scan will check for stop signals and exit gracefully
                    scan_process_singles = None
                    flash("Single detection scan stop requested (will finish current operation)", "info")
                else:
                    flash("No single detection scan is currently running", "warning")
            else:
                flash("No single detection scan is currently running", "warning")
        else:
            flash("No single detection scan is currently running", "warning")
    
    return redirect(url_for("dashboard"))


@app.route("/scan/stop-combined", methods=["POST"])
def scan_stop_combined():
    """Stop the combined scan"""
    global scan_process_combined
    
    with scan_lock:
        if scan_process_combined is not None:
            if isinstance(scan_process_combined, dict):
                thread = scan_process_combined.get('thread')
                if thread and thread.is_alive():
                    # Threads can't be forcefully stopped in Python, so we just mark it as stopped
                    # The scan will check for stop signals and exit gracefully
                    scan_process_combined = None
                    flash("Combined scan stop requested (will finish current artist)", "info")
                else:
                    flash("No combined scan is currently running", "warning")
            else:
                flash("No combined scan is currently running", "warning")
        else:
            flash("No combined scan is currently running", "warning")
    
    return redirect(url_for("dashboard"))


@app.route("/scan/stop-all", methods=["POST"])
def scan_stop_all():
    """Stop all running scans"""
    global scan_process, scan_process_navidrome, scan_process_popularity, scan_process_singles, scan_process_combined, scan_process_missing_releases
    
    stopped_scans = []
    
    with scan_lock:
        # Stop main scan process
        if scan_process is not None and scan_process.poll() is None:
            scan_process.terminate()
            stopped_scans.append("main")
        
        # Stop Navidrome scan
        if scan_process_navidrome is not None:
            if isinstance(scan_process_navidrome, dict):
                thread = scan_process_navidrome.get('thread')
                if thread and thread.is_alive():
                    scan_process_navidrome = None
                    stopped_scans.append("Navidrome")
        
        # Stop Popularity scan
        if scan_process_popularity is not None:
            if isinstance(scan_process_popularity, dict):
                thread = scan_process_popularity.get('thread')
                if thread and thread.is_alive():
                    scan_process_popularity = None
                    stopped_scans.append("Popularity")
        
        # Stop Singles scan
        if scan_process_singles is not None:
            if isinstance(scan_process_singles, dict):
                thread = scan_process_singles.get('thread')
                if thread and thread.is_alive():
                    scan_process_singles = None
                    stopped_scans.append("Singles")
        
        # Stop Combined scan
        if scan_process_combined is not None:
            if isinstance(scan_process_combined, dict):
                thread = scan_process_combined.get('thread')
                if thread and thread.is_alive():
                    scan_process_combined = None
                    stopped_scans.append("Combined")
        
        # Stop Missing Releases scan
        if scan_process_missing_releases is not None:
            if isinstance(scan_process_missing_releases, dict):
                thread = scan_process_missing_releases.get('thread')
                if thread and thread.is_alive():
                    scan_process_missing_releases = None
                    stopped_scans.append("Missing releases")
    
    if stopped_scans:
        flash(f"✅ Stopped {len(stopped_scans)} scan(s): {', '.join(stopped_scans)}", "success")
    else:
        flash("No scans are currently running", "info")
    
    return redirect(url_for("dashboard"))


@app.route("/scan/clear-stuck", methods=["POST"])
def scan_clear_stuck():
    """Clear all stuck scan progress files"""
    global scan_process_navidrome, scan_process_popularity, scan_process_singles, scan_process_combined, scan_process_missing_releases
    
    with scan_lock:
        db_dir = os.path.dirname(DB_PATH)
        cleared_count = 0
        
        # List of all progress files to check
        progress_files = [
            ("navidrome_scan_progress.json", scan_process_navidrome),
            ("popularity_scan_progress.json", scan_process_popularity),
            ("singles_scan_progress.json", scan_process_singles),
            ("combined_scan_progress.json", scan_process_combined),
            ("missing_releases_scan_progress.json", scan_process_missing_releases),
        ]
        
        for filename, process_ref in progress_files:
            filepath = os.path.join(db_dir, filename)
            if os.path.exists(filepath):
                try:
                    with open(filepath, 'r') as f:
                        progress = json.load(f)
                    
                    # If it says running, check if process is actually alive using helper
                    if progress.get("is_running", False):
                        is_alive = _is_process_alive(process_ref)
                        
                        # If not alive, mark as stopped
                        if not is_alive:
                            progress["is_running"] = False
                            progress["status"] = "cleared"
                            with open(filepath, 'w') as f:
                                json.dump(progress, f)
                            cleared_count += 1
                            logging.info(f"Cleared stuck progress file: {filename}")
                except Exception as e:
                    logging.error(f"Error clearing progress file {filename}: {e}")
        
        # Also clean up global process references if they're dead (explicit assignments for security)
        if scan_process_navidrome and not _is_process_alive(scan_process_navidrome):
            scan_process_navidrome = None
        if scan_process_popularity and not _is_process_alive(scan_process_popularity):
            scan_process_popularity = None
        if scan_process_singles and not _is_process_alive(scan_process_singles):
            scan_process_singles = None
        if scan_process_missing_releases and not _is_process_alive(scan_process_missing_releases):
            scan_process_missing_releases = None
        
        if cleared_count > 0:
            flash(f"✅ Cleared {cleared_count} stuck scan(s)", "success")
        else:
            flash("No stuck scans found", "info")
    
    return redirect(url_for("dashboard"))


@app.route("/scan/status")
def scan_status():
    """Get scan status (JSON)"""
    with scan_lock:
        web_ui_running = scan_process is not None and scan_process.poll() is None
    
    # Check if background scan from start.py is running
    lock_file_path = os.path.join(os.path.dirname(CONFIG_PATH), ".scan_lock")
    background_running = os.path.exists(lock_file_path)
    
    running = web_ui_running or background_running
    
    return jsonify({"running": running})


@app.route("/login", methods=["GET", "POST"])
def login():
    """Login page"""
    try:
        # If already logged in, redirect to dashboard
        if 'username' in session:
            return redirect(url_for('dashboard'))
        
        # If config doesn't exist, redirect to setup
        if not os.path.exists(CONFIG_PATH):
            return redirect(url_for('setup'))
        
        cfg = get_config()
        if _needs_setup(cfg):
            return redirect(url_for('setup'))
        
        if request.method == 'POST':
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '').strip()
            
            if _authenticate_navidrome(username, password):
                session.permanent = True
                session['username'] = username
                flash(f'Welcome back, {username}!', 'success')
                
                # Redirect to next URL or dashboard
                next_url = request.args.get('next')
                if next_url:
                    return redirect(next_url)
                return redirect(url_for('dashboard'))
            else:
                flash('Invalid username or password. Please use your Navidrome credentials.', 'danger')
    except Exception as e:
        logging.error(f"Login error: {e}")
        import traceback
        traceback.print_exc()
        flash(f"Login system error: {str(e)}", "danger")
    
    return render_template('login.html')


@app.route("/logout")
def logout():
    """Logout and clear session"""
    username = session.get('username', 'User')
    session.clear()
    flash(f'Goodbye, {username}!', 'info')
    return redirect(url_for('login'))


@app.route("/logs")
def logs():
    """View logs"""
    config_dir = os.path.dirname(CONFIG_PATH)
    log_files = {
        "main": LOG_PATH,
        "webui": os.path.join(config_dir, "webui.log"),
        "beets": os.path.join(config_dir, "beets_import.log"),
        "navidrome": os.path.join(config_dir, "app.log"),
        "popularity": os.path.join(config_dir, "popularity.log"),
        "downloads": os.path.join(config_dir, "downloads.log"),
    }
    return render_template("logs.html", log_path=LOG_PATH, log_files=log_files)


@app.route("/logs/stream")
def logs_stream():
    """Stream log file in real-time"""
    log_type = request.args.get("type", "main")
    config_dir = os.path.dirname(CONFIG_PATH)
    log_files = {
        "main": LOG_PATH,
        "webui": os.path.join(config_dir, "webui.log"),
        "beets": os.path.join(config_dir, "beets_import.log"),
        "navidrome": os.path.join(config_dir, "app.log"),
        "popularity": os.path.join(config_dir, "popularity.log"),
        "downloads": os.path.join(config_dir, "downloads.log"),
    }
    log_path = log_files.get(log_type, LOG_PATH)
    
    def generate():
        try:
            with open(log_path, "r") as f:
                # Seek to end
                f.seek(0, 2)
                while True:
                    line = f.readline()
                    if line:
                        yield f"data: {line}\n\n"
                    else:
                        time.sleep(0.5)
        except FileNotFoundError:
            yield f"data: Log file not found: {log_path}\n\n"
    
    return Response(generate(), mimetype="text/event-stream")


@app.route("/logs/view")
def logs_view():
    """View last N lines of log"""
    log_type = request.args.get("type", "main")
    lines = request.args.get("lines", 500, type=int)
    config_dir = os.path.dirname(CONFIG_PATH)
    log_files = {
        "main": LOG_PATH,
        "webui": os.path.join(config_dir, "webui.log"),
        "beets": os.path.join(config_dir, "beets_import.log"),
        "navidrome": os.path.join(config_dir, "app.log"),
        "popularity": os.path.join(config_dir, "popularity.log"),
        "downloads": os.path.join(config_dir, "downloads.log"),
    }
    log_path = log_files.get(log_type, LOG_PATH)
    try:
        with open(log_path, "r") as f:
            all_lines = f.readlines()
            recent_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
            return jsonify({"lines": recent_lines})
    except FileNotFoundError:
        return jsonify({"error": "Log file not found", "lines": []})


@app.route("/bookmarks")
def bookmarks():
    """View all bookmarks (favourites)"""
    try:
        filter_type = request.args.get('filter', None)
        
        conn = get_db()
        cursor = conn.cursor()
        
        if filter_type:
            cursor.execute("""
                SELECT id, type, name, artist, album, track_id, created_at
                FROM bookmarks
                WHERE type = ?
                ORDER BY created_at DESC
            """, (filter_type,))
        else:
            cursor.execute("""
                SELECT id, type, name, artist, album, track_id, created_at
                FROM bookmarks
                ORDER BY created_at DESC
            """)
        
        bookmarks_data = []
        for row in cursor.fetchall():
            bookmarks_data.append({
                'id': row[0],
                'type': row[1],
                'name': row[2],
                'artist': row[3],
                'album': row[4],
                'track_id': row[5],
                'created_at': row[6]
            })
        
        conn.close()
        
        return render_template("bookmarks.html", 
                             bookmarks=bookmarks_data,
                             filter_type=filter_type)
    except Exception as e:
        logging.error(f"Error loading bookmarks: {e}")
        return render_template("bookmarks.html", 
                             bookmarks=[], 
                             filter_type=None,
                             error=str(e))


@app.route("/api/bookmarks", methods=["GET", "POST"])
def api_bookmarks():
    """Get all bookmarks or add a new bookmark"""
    conn = get_db()
    cursor = conn.cursor()
    
    if request.method == "GET":
        try:
            cursor.execute("""
                SELECT id, type, name, artist, album, track_id, created_at
                FROM bookmarks
                ORDER BY created_at DESC
            """)
            
            bookmarks_data = []
            for row in cursor.fetchall():
                bookmarks_data.append({
                    'id': row[0],
                    'type': row[1],
                    'name': row[2],
                    'artist': row[3],
                    'album': row[4],
                    'track_id': row[5],
                    'created_at': row[6]
                })
            
            conn.close()
            return jsonify({"success": True, "bookmarks": bookmarks_data})
        except Exception as e:
            logging.error(f"Error fetching bookmarks: {e}")
            conn.close()
            return jsonify({"success": False, "error": str(e)}), 500
    
    elif request.method == "POST":
        try:
            data = request.get_json()
            bookmark_type = data.get('type')
            name = data.get('name')
            artist = data.get('artist')
            album = data.get('album')
            track_id = data.get('track_id')
            
            if not bookmark_type or not name:
                return jsonify({"success": False, "error": "Missing required fields"}), 400
            
            cursor.execute("""
                INSERT OR IGNORE INTO bookmarks (type, name, artist, album, track_id)
                VALUES (?, ?, ?, ?, ?)
            """, (bookmark_type, name, artist, album, track_id))
            
            conn.commit()
            bookmark_id = cursor.lastrowid
            conn.close()
            
            return jsonify({"success": True, "id": bookmark_id, "message": "Bookmark added"})
        except Exception as e:
            logging.error(f"Error adding bookmark: {e}")
            conn.close()
            return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/bookmarks/<int:bookmark_id>", methods=["DELETE"])
def api_delete_bookmark(bookmark_id):
    """Delete a bookmark"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("DELETE FROM bookmarks WHERE id = ?", (bookmark_id,))
        conn.commit()
        
        if cursor.rowcount == 0:
            conn.close()
            return jsonify({"success": False, "error": "Bookmark not found"}), 404
        
        conn.close()
        return jsonify({"success": True, "message": "Bookmark deleted"})
    except Exception as e:
        logging.error(f"Error deleting bookmark: {e}")
        return jsonify({"success": False, "error": str(e)}), 500



@app.route("/config", methods=["GET", "POST"])
def config_editor():
    """View/edit config.yaml. Always allow access, show warning if setup incomplete."""
    if request.method == "POST":
        # Require authentication for config changes
        if 'username' not in session:
            flash("Authentication required to save configuration", "error")
            return redirect(url_for("login", next=request.url))
        
        # Handle raw YAML save from modal
        try:
            config_content = request.form.get('config_content', '')
            if not config_content:
                flash("No configuration content provided", "error")
                return redirect(url_for("config_editor"))
            
            # Validate YAML before saving
            try:
                yaml.safe_load(config_content)
            except yaml.YAMLError as e:
                flash(f"Invalid YAML: {str(e)}", "error")
                return redirect(url_for("config_editor"))
            
            # Save to file
            cfg_dir = os.path.dirname(CONFIG_PATH)
            if cfg_dir:
                os.makedirs(cfg_dir, exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                f.write(config_content)
            
            flash("Configuration saved successfully", "success")
            return redirect(url_for("config_editor"))
        except Exception as e:
            flash(f"Error saving configuration: {str(e)}", "error")
            return redirect(url_for("config_editor"))
    
    # GET request - show config editor
    config, raw = _read_yaml(CONFIG_PATH)
    # Check for required keys in navidrome_users (for warning only)
    navidrome_users = config.get('navidrome_users', [])
    required_keys = ["base_url", "user", "pass"]
    needs_setup = False
    if not navidrome_users:
        needs_setup = True
    else:
        for user in navidrome_users:
            for key in required_keys:
                if not user.get(key):
                    needs_setup = True
                    break
            if needs_setup:
                break
    if not raw:
        raw = yaml.safe_dump(config, sort_keys=False, allow_unicode=False) if config else ""
    return render_template("config.html", config=config, config_raw=raw, CONFIG_PATH=CONFIG_PATH, needs_setup=needs_setup)



@app.route("/config/env", methods=["GET"])
def config_env_vars():
    """Return all relevant environment variables and their current values as JSON."""
    return jsonify(get_all_env_vars())

@app.route("/config/env", methods=["POST"])
def config_env_vars_post():
    """Update environment variables from config page."""
    try:
        data = request.get_json(force=True)
        changed = 0
        for var, value in data.items():
            if var in ALL_ENV_VARS:
                os.environ[var] = value
                changed += 1
        return jsonify({"success": True, "updated": changed})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400
    return redirect(url_for("config_editor"))


@app.route("/config/save-json", methods=["POST"])
def config_save_json():
    """Save config as JSON - converts to YAML and updates config.yaml"""
    try:
        # Get JSON data
        data = request.get_json()
        
        if data is None:
            return jsonify({"success": False, "error": "No JSON data provided"}), 400
        
        # Build YAML structure from JSON
        navidrome_users = data.get('navidrome_users', [])
        # Map legacy/alternate keys to expected keys on save
        for user in navidrome_users:
            if "navidrome_base_url" in user:
                user["base_url"] = user["navidrome_base_url"]
            if "navidrome_password" in user:
                user["pass"] = user["navidrome_password"]
            if "username" in user:
                user["user"] = user["username"]
        config_dict = {
            'navidrome_users': navidrome_users,
            'qbittorrent': data.get('qbittorrent', {}),
            'slskd': data.get('slskd', {}),
            'authentik': data.get('authentik', {}),
            'bookmarks': data.get('bookmarks', {}),
            'downloads': data.get('downloads', {}),
            'api_integrations': data.get('api_integrations', {}),
            'database': data.get('database', {}),
            'logging': data.get('logging', {}),
            'web_api_key': data.get('web_api_key', ''),
            'enable_web_api_key': data.get('enable_web_api_key', True),
            'features': data.get('features', {}),  # Accept features from request
            'weights': data.get('weights', {}),  # Accept weights from request
            'single_detection': data.get('single_detection', {})  # Accept single detection thresholds
        }
        # Always set main navidrome section to first user for compatibility
        if navidrome_users and len(navidrome_users) > 0:
            config_dict['navidrome'] = {
                'base_url': navidrome_users[0].get('base_url', ''),
                'user': navidrome_users[0].get('user', ''),
                'pass': navidrome_users[0].get('pass', ''),
            }
        
        # Read existing config to preserve features and weights if not provided in request
        existing_config, _ = _read_yaml(CONFIG_PATH)
        if existing_config:
            # Only preserve features/weights/single_detection if not explicitly provided in the request
            if 'features' not in data and 'features' in existing_config:
                config_dict['features'] = existing_config['features']
            if 'weights' not in data and 'weights' in existing_config:
                config_dict['weights'] = existing_config['weights']
            if 'single_detection' not in data and 'single_detection' in existing_config:
                config_dict['single_detection'] = existing_config['single_detection']
            # Also preserve legacy navidrome config if it exists (for backward compatibility)
            if 'navidrome' in existing_config and not config_dict.get('navidrome_users'):
                config_dict['navidrome'] = existing_config['navidrome']
        
        # Convert to YAML
        yaml_content = yaml.safe_dump(config_dict, sort_keys=False, allow_unicode=True, default_flow_style=False)
        
        # Validate YAML before writing
        yaml.safe_load(yaml_content)
        
        # Write to file
        cfg_dir = os.path.dirname(CONFIG_PATH)
        if cfg_dir:
            os.makedirs(cfg_dir, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(yaml_content)
        
        return jsonify({"success": True, "message": "Configuration saved successfully"})
    
    except yaml.YAMLError as e:
        return jsonify({"success": False, "error": f"YAML error: {str(e)}"}), 400
    except IOError as e:
        return jsonify({"success": False, "error": f"File write error: {str(e)}"}), 400
    except Exception as e:
        import traceback
        error_msg = str(e)
        tb = traceback.format_exc()
        print(f"Config save error: {tb}")  # Log to console for debugging
        return jsonify({"success": False, "error": error_msg}), 400


@app.route("/help")
@app.route("/help/<path:doc_name>")
def help_page(doc_name=None):
    """Display documentation from /documentation folder"""
    import markdown
    import os
    
    doc_path = os.path.join(os.path.dirname(__file__), "documentation")
    
    # If no doc specified, show index
    if not doc_name:
        doc_name = "INDEX.md"
    elif not doc_name.endswith('.md'):
        doc_name = doc_name + '.md'
    
    # Security: prevent path traversal
    doc_name = os.path.basename(doc_name)
    full_path = os.path.join(doc_path, doc_name)
    
    # Get list of all documentation files
    try:
        doc_files = sorted([f for f in os.listdir(doc_path) if f.endswith('.md')])
    except:
        doc_files = []
    
    # Read and render the markdown file
    content = ""
    doc_title = doc_name.replace('.md', '').replace('_', ' ').title()
    
    if os.path.exists(full_path):
        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                md_content = f.read()
                # Convert markdown to HTML
                content = markdown.markdown(md_content, extensions=['extra', 'codehilite', 'toc'])
        except Exception as e:
            content = f"<p>Error loading documentation: {str(e)}</p>"
    else:
        content = f"<p>Documentation file not found: {doc_name}</p>"
    
    return render_template('help.html', 
                         content=content, 
                         doc_title=doc_title,
                         doc_files=doc_files,
                         current_doc=doc_name)


@app.route("/api/stats")
def api_stats():
    """API endpoint for statistics"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(DISTINCT artist) FROM tracks")
        artist_result = cursor.fetchone()
        artist_count = artist_result[0] if artist_result else 0
        
        cursor.execute("SELECT COUNT(DISTINCT album) FROM tracks")
        album_result = cursor.fetchone()
        album_count = album_result[0] if album_result else 0
        
        cursor.execute("SELECT COUNT(*) FROM tracks")
        track_result = cursor.fetchone()
        track_count = track_result[0] if track_result else 0
        
        conn.close()
        
        return jsonify({
            "artists": artist_count,
            "albums": album_count,
            "tracks": track_count
        })
    except Exception as e:
        logging.error(f"Error getting stats: {e}")
        return jsonify({"artists": 0, "albums": 0, "tracks": 0}), 500


@app.route("/api/scan/artist", methods=["POST"])
def api_scan_single_artist():
    """API endpoint to scan a single artist"""
    try:
        data = request.json or {}
        artist = data.get("artist", "").strip()
        
        if not artist:
            return jsonify({"success": False, "error": "Artist name is required"}), 400
        
        # Run the artist scan pipeline in the background
        from threading import Thread
        
        def run_scan():
            try:
                _run_artist_scan_pipeline(artist)
                logging.info(f"✅ Scan completed for artist: {artist}")
            except Exception as e:
                logging.error(f"❌ Scan failed for artist {artist}: {e}", exc_info=True)
        
        thread = Thread(target=run_scan, daemon=True)
        thread.start()
        
        return jsonify({
            "success": True,
            "message": f"Scan started for artist: {artist}",
            "artist": artist
        })
    
    except Exception as e:
        logging.error(f"Error starting artist scan: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/scan/from-artist", methods=["POST"])
def api_scan_from_artist():
    """API endpoint to start a popularity scan from a specific artist or letter"""
    global scan_process_popularity
    
    try:
        data = request.json or {}
        artist = data.get("artist", "").strip()
        letter = data.get("letter", "").strip()
        scan_mode = data.get("scan_mode", "changes")  # 'changes' or 'forced'
        
        # If letter is provided, query Navidrome for first artist starting with that letter
        if letter:
            try:
                # Get Navidrome configuration
                config_data, _ = _read_yaml(CONFIG_PATH)
                current_user = session.get("username")
                navidrome_users = config_data.get("navidrome_users", [])
                nav_cfg = None

                if navidrome_users and current_user:
                    nav_cfg = next((u for u in navidrome_users if u.get("user") == current_user), None)
                if not nav_cfg:
                    nav_cfg = config_data.get("navidrome", {})

                base_url = nav_cfg.get("base_url")
                username = nav_cfg.get("user")
                password = nav_cfg.get("pass")
                
                if not (base_url and username and password):
                    return jsonify({"success": False, "error": "Navidrome not configured"}), 400
                
                # Query Navidrome for artists
                from api_clients.navidrome import NavidromeClient
                client = NavidromeClient(base_url, username, password)
                artist_map = client.build_artist_index()
                
                if not artist_map:
                    return jsonify({"success": False, "error": "No artists found in Navidrome"}), 400
                
                # Filter artists by letter and get the first one alphabetically
                letter_upper = letter.upper()
                matching_artists = []
                for artist_name in sorted(artist_map.keys(), key=str.lower):
                    if not artist_name:
                        continue
                    first_char = artist_name[0].upper()
                    # Match letter or '#' for non-alphabetic characters
                    if letter_upper == '#':
                        if first_char not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
                            matching_artists.append(artist_name)
                    elif first_char == letter_upper:
                        matching_artists.append(artist_name)
                
                if not matching_artists:
                    return jsonify({"success": False, "error": f"No artists found in Navidrome starting with '{letter}'"}), 400
                
                # Use the first artist from Navidrome for this letter
                artist = matching_artists[0]
                logging.info(f"Letter '{letter}' scan: Using first artist from Navidrome: '{artist}'")
                
            except Exception as e:
                logging.error(f"Error querying Navidrome for letter '{letter}': {e}", exc_info=True)
                return jsonify({"success": False, "error": f"Failed to query Navidrome: {str(e)}"}), 500
        
        if not artist:
            return jsonify({"success": False, "error": "Artist name or letter is required"}), 400
        
        with scan_lock:
            # Check if popularity scan is already running
            if scan_process_popularity is not None:
                if isinstance(scan_process_popularity, dict):
                    thread = scan_process_popularity.get('thread')
                    if thread and thread.is_alive():
                        return jsonify({"success": False, "error": "Popularity scan is already running"}), 400
                elif hasattr(scan_process_popularity, 'is_alive') and scan_process_popularity.is_alive():
                    return jsonify({"success": False, "error": "Popularity scan is already running"}), 400
            
            # Determine scan parameters
            force_rescan = (scan_mode == "forced")
            
            # Start the popularity scan from this artist
            from popularity import popularity_scan as scan_popularity_func
            import threading
            
            db_dir = os.path.dirname(DB_PATH)
            popularity_progress_file = os.path.join(db_dir, "popularity_scan_progress.json")
            _write_progress_file(popularity_progress_file, "popularity_scan", True, {"status": "starting", "resume_from": artist})
            
            def run_popularity_scan_bg():
                try:
                    logging.info(f"Starting popularity scan from artist '{artist}' (force={force_rescan})")
                    scan_popularity_func(verbose=False, force=force_rescan, resume_from=artist)
                    _write_progress_with_current_artist(popularity_progress_file, "popularity_scan", False, {"status": "complete", "exit_code": 0})
                    logging.info(f"Popularity scan from '{artist}' completed successfully")
                except Exception as e:
                    logging.error(f"Error in popularity scan from '{artist}': {e}", exc_info=True)
                    _write_progress_with_current_artist(popularity_progress_file, "popularity_scan", False, {"status": "error", "error": str(e), "exit_code": 1})
            
            scan_thread = threading.Thread(target=run_popularity_scan_bg, daemon=False)
            scan_thread.start()
            scan_process_popularity = {'thread': scan_thread, 'type': 'popularity'}
            
            mode_desc = "Full (Forced)" if force_rescan else "Changes Only"
            message_suffix = f" (from Navidrome letter '{letter}')" if letter else ""
            logging.info(f"Popularity scan thread started from artist '{artist}' ({mode_desc}){message_suffix}")
            
            return jsonify({
                "success": True,
                "message": f"Popularity scan started from artist: {artist}",
                "artist": artist,
                "mode": mode_desc
            })
    
    except Exception as e:
        logging.error(f"Error starting scan from artist: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/scan-status")
def api_scan_status():
    """API endpoint to get status of all scan types"""
    global scan_process, scan_process_navidrome, scan_process_popularity, scan_process_singles, scan_process_combined, scan_process_missing_releases
    
    def is_process_running(proc):
        """Check if a process/thread is running, handling both dict and process objects."""
        if proc is None:
            return False
        if isinstance(proc, dict):
            # Handle dict format: {'thread': thread_obj, 'type': '...'}
            thread = proc.get('thread')
            if thread is None:
                return False
            # For threads, check is_alive()
            if hasattr(thread, 'is_alive'):
                return thread.is_alive()
            # For processes, check poll()
            if hasattr(thread, 'poll'):
                return thread.poll() is None
            return False
        # Direct process/thread object
        if hasattr(proc, 'is_alive'):
            return proc.is_alive()
        if hasattr(proc, 'poll'):
            return proc.poll() is None
        return False
    
    with scan_lock:
        return jsonify({
            "main_scan": {
                "name": "Main Rating Scan",
                "running": is_process_running(scan_process)
            },
            "navidrome_scan": {
                "name": "Navidrome Sync",
                "running": is_process_running(scan_process_navidrome)
            },
            "popularity_scan": {
                "name": "Popularity Update",
                "running": is_process_running(scan_process_popularity)
            },
            "singles_scan": {
                "name": "Single Detection",
                "running": is_process_running(scan_process_singles)
            },
            "combined_scan": {
                "name": "Combined Scan",
                "running": is_process_running(scan_process_combined)
            },
            "missing_releases_scan": {
                "name": "Missing Releases Scan",
                "running": is_process_running(scan_process_missing_releases)
            }
        })


@app.route("/api/recent-scans")
def api_recent_scans():
    """Return latest album scan events for dashboard refresh (up to 100 items)."""
    try:
        limit = request.args.get("limit", 100, type=int)
        # Cap at 100 items max
        limit = min(limit, 100)
        from scan_history import get_recent_album_scans
        scans = get_recent_album_scans(limit=limit)
        return jsonify({"scans": scans})
    except Exception as e:
        logging.error(f"Error fetching recent scans: {e}")
        return jsonify({"scans": [], "error": str(e)}), 500


@app.route("/api/scan-progress")
def api_scan_progress():
    """API endpoint to get detailed scan progress"""
    global scan_process_navidrome, scan_process_popularity, scan_process_singles, scan_process_combined, scan_process_missing_releases
    
    try:
        from unified_scan import get_scan_progress
        progress = get_scan_progress()
        
        # If unified scan is not running, check for Navidrome, Popularity, Singles, and Combined scans
        if not progress.get("is_running", False):
            db_dir = os.path.dirname(DB_PATH)
            
            # Check Navidrome scan progress with validation
            nav_progress_file = os.path.join(db_dir, "navidrome_scan_progress.json")
            nav_progress = _validate_and_cleanup_progress_file(nav_progress_file, scan_process_navidrome)
            if nav_progress and nav_progress.get("is_running", False):
                return jsonify(nav_progress)
            
            # Check Popularity scan progress with validation
            popularity_progress_file = os.path.join(db_dir, "popularity_scan_progress.json")
            pop_progress = _validate_and_cleanup_progress_file(popularity_progress_file, scan_process_popularity)
            if pop_progress and pop_progress.get("is_running", False):
                return jsonify(pop_progress)
            
            # Check Singles scan progress with validation
            singles_progress_file = os.path.join(db_dir, "singles_scan_progress.json")
            singles_progress = _validate_and_cleanup_progress_file(singles_progress_file, scan_process_singles)
            if singles_progress and singles_progress.get("is_running", False):
                return jsonify(singles_progress)
            
            # Check Combined scan progress with validation
            combined_progress_file = os.path.join(db_dir, "combined_scan_progress.json")
            combined_progress = _validate_and_cleanup_progress_file(combined_progress_file, scan_process_combined)
            if combined_progress and combined_progress.get("is_running", False):
                return jsonify(combined_progress)
            
            # Check Missing Releases scan progress with validation
            missing_releases_progress_file = os.path.join(db_dir, "missing_releases_scan_progress.json")
            missing_releases_progress = _validate_and_cleanup_progress_file(missing_releases_progress_file, scan_process_missing_releases)
            if missing_releases_progress and missing_releases_progress.get("is_running", False):
                return jsonify(missing_releases_progress)
        
        return jsonify(progress)
    except Exception as e:
        logging.error(f"Error getting scan progress: {e}")
        return jsonify({
            "is_running": False,
            "percent_complete": 0,
            "current_artist": None,
            "current_album": None,
            "error": str(e)
        })


@app.route("/api/scan-logs")
def api_scan_logs():
    """API endpoint to get last log entries for each scan type"""
    import re
    
    log_files = {
        "navidrome": LOG_PATH,  # Navidrome scans log to main app log
        "popularity": os.path.join(os.path.dirname(CONFIG_PATH), "popularity.log"),
        "singles": os.path.join(os.path.dirname(CONFIG_PATH), "popularity.log"),  # Singles detection integrated into popularity.py
        # Beets auto-import now drives the file-path scan; read its log instead of the old mp3scanner log
        "file_paths": os.path.join(os.path.dirname(CONFIG_PATH), "beets_import.log")
    }
    
    def extract_meaningful_log(line):
        """Extract meaningful log message, removing timestamps and excessive details"""
        # Remove timestamp prefix (e.g., "2024-01-15 10:30:45,123 - ")
        line = re.sub(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ - ', '', line)
        # Remove log level prefix (e.g., "INFO - ", "DEBUG - ", "ERROR - ")
        line = re.sub(r'^(INFO|DEBUG|WARNING|ERROR|CRITICAL)\s*-?\s*', '', line)
        # Remove full file paths (keep just filename)
        line = re.sub(r'[A-Za-z]:\\[^\s]*\\', '', line)
        line = re.sub(r'/[^\s]*/([^\s/]*\.mp3)', r'\1', line)
        return line.strip()
    
    def is_meaningful_log(line):
        """Check if log line contains meaningful scan information"""
        line_lower = line.lower()
        # Keywords that indicate meaningful log entries
        meaningful_keywords = [
            'scanning', 'syncing', 'scanning album', 'scanning artist',
            'found', 'match', 'updated', 'importing', 'processing',
            'completed', 'finished', 'detected', 'checking', 'analyzing',
            'no match', 'error', 'failed', 'success', 'track', 'album',
            'artist', 'single', 'rating', 'score', 'popularity'
        ]
        # Skip debug lines that are too verbose
        skip_keywords = ['debug', 'checking match', 'checking for', 'found in']
        
        # Check if line contains skip keywords
        for skip in skip_keywords:
            if skip in line_lower:
                return False
        
        # Check if line contains meaningful keywords
        return any(keyword in line_lower for keyword in meaningful_keywords)
    
    result = {}
    for scan_type, log_path in log_files.items():
        lines = []
        if os.path.exists(log_path):
            try:
                with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                    all_lines = f.readlines()
                    # Get last meaningful non-empty lines
                    for line in reversed(all_lines):
                        line = line.strip()
                        if line and is_meaningful_log(line):
                            meaningful_line = extract_meaningful_log(line)
                            if meaningful_line and len(lines) < 3:
                                lines.append(meaningful_line)
                    lines.reverse()
            except Exception as e:
                lines = [f"Error reading log: {str(e)}"]
        result[scan_type] = lines
    
    return jsonify(result)


@app.route("/api/track-count")
def api_track_count():
    """API endpoint to get total track count for progress calculation"""
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            cursor = conn.cursor()
            
            cursor.execute("SELECT COUNT(*) FROM tracks")
            total_result = cursor.fetchone()
            total_tracks = total_result[0] if total_result else 0
            
            # Also get counts with different metadata filled in
            cursor.execute("SELECT COUNT(*) FROM tracks WHERE stars IS NOT NULL")
            navidrome_result = cursor.fetchone()
            navidrome_filled = navidrome_result[0] if navidrome_result else 0
            
            cursor.execute("SELECT COUNT(*) FROM tracks WHERE spotify_score IS NOT NULL")
            popularity_result = cursor.fetchone()
            popularity_filled = popularity_result[0] if popularity_result else 0
            
            cursor.execute("SELECT COUNT(*) FROM tracks WHERE is_single IS NOT NULL")
            singles_result = cursor.fetchone()
            singles_filled = singles_result[0] if singles_result else 0
            
            cursor.execute("SELECT COUNT(*) FROM tracks WHERE file_path IS NOT NULL")
            filepath_result = cursor.fetchone()
            filepath_filled = filepath_result[0] if filepath_result else 0
            
            return jsonify({
                "total_tracks": total_tracks,
                "navidrome_filled": navidrome_filled,
                "popularity_filled": popularity_filled,
                "singles_filled": singles_filled,
                "filepath_filled": filepath_filled
            })
    except Exception as e:
        logging.error(f"Error getting track count: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/downloads")
def downloads():
    """Redirect to download monitor (default page)"""
    return redirect("/downloads/monitor")


@app.route("/downloads/monitor")
def downloads_monitor():
    """Download monitor page - queue status and management"""
    cfg = get_config()
    qbit_config = cfg.get("qbittorrent", {"enabled": False})
    slskd_config = cfg.get("slskd", {"enabled": False})
    
    return render_template("downloads_monitor.html", 
                         qbit_config=qbit_config,
                         slskd_config=slskd_config)


@app.route("/downloads/search/<source>")
def downloads_search(source):
    """Search pages for different sources"""
    cfg = get_config()
    qbit_config = cfg.get("qbittorrent", {"enabled": False})
    slskd_config = cfg.get("slskd", {"enabled": False})
    
    # Route to appropriate template
    templates = {
        'soulseek': 'downloads_search_soulseek.html',
        'qbittorrent': 'downloads_search_qbittorrent.html',
        'musicbrainz': 'downloads_search_musicbrainz.html',
        'playlists': 'downloads_search_playlists.html'
    }
    
    template = templates.get(source)
    if not template:
        abort(404)
    
    return render_template(template,
                         qbit_config=qbit_config,
                         slskd_config=slskd_config)


@app.route("/downloads/discover/<category>")
def downloads_discover(category):
    """Discover pages for recommendations"""
    cfg = get_config()
    
    # Route to appropriate template
    templates = {
        'lastfm': 'downloads_discover_lastfm.html',
        'similar-artists': 'downloads_discover_similar_artists.html',
        'upcoming': 'downloads_discover_upcoming.html'
    }
    
    template = templates.get(category)
    if not template:
        abort(404)
    
    return render_template(template)


@app.route("/api/slskd/search", methods=["POST"])
def slskd_search():
    """Proxy endpoint for slskd search API - returns search ID for polling"""
    cfg = get_config()
    slskd_config = cfg.get("slskd", {})
    
    if not slskd_config.get("enabled"):
        return jsonify({"error": "slskd integration not enabled"}), 400
    
    query = request.json.get("query", "")
    if not query:
        return jsonify({"error": "Query parameter required"}), 400
    
    web_url = slskd_config.get("web_url", "http://localhost:5030")
    api_key = slskd_config.get("api_key", "")
    
    try:
        client = SlskdClient(web_url, api_key, enabled=True)
        search_id = client.start_search(query)
        
        if not search_id:
            return jsonify({"error": "Failed to start search"}), 500
        
        return jsonify({"searchId": search_id, "status": "searching"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/slskd/search/<search_id>", methods=["GET"])
def slskd_search_results(search_id):
    """Poll for Soulseek search results"""
    cfg = get_config()
    slskd_config = cfg.get("slskd", {})
    
    if not slskd_config.get("enabled"):
        return jsonify({"error": "slskd integration not enabled"}), 400
    
    web_url = slskd_config.get("web_url", "http://localhost:5030")
    api_key = slskd_config.get("api_key", "")
    
    try:
        client = SlskdClient(web_url, api_key, enabled=True)
        responses, state, is_complete = client.get_search_results(search_id)
        
        results = []
        for resp in responses:
            if hasattr(resp, 'files'):
                for file in resp.files:
                    results.append({
                        "username": resp.username,
                        "filename": file.filename,
                        "size": file.size,
                        "size_mb": f"{file.size_mb:.2f}",
                        "bitrate": file.bitrate,
                        "sample_rate": file.sample_rate,
                        "length": file.length,
                        "duration": file.duration_formatted,
                    })
        
        response_count = len(responses) if responses else 0
        logging.info(f"[SLSKD] search_id={search_id}, responses={response_count}, files={len(results)}, state={state}, complete={is_complete}")
        
        if response_count == 0:
            logging.warning(f"[SLSKD] Search {search_id} returned 0 responses - check if slskd service is reachable at {web_url}")
        elif len(results) == 0:
            logging.warning(f"[SLSKD] Search {search_id} got {response_count} responses but 0 files - peers may not have matching files")
        
        return jsonify({
            "results": results,
            "state": state,
            "responseCount": response_count,
            "fileCount": len(results),
            "isComplete": is_complete
        })
    except Exception as e:
        logging.error(f"[SLSKD] Error getting search results for {search_id}: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/scan/navidrome", methods=["POST"])
def scan_navidrome():
    """Run Navidrome import-only scan (no popularity/singles)."""
    global scan_process_navidrome
    
    # Get scan mode from query parameters (default: "all")
    mode = request.args.get('mode', 'all')  # all, force, missing, resume, resume_force
    
    with scan_lock:
        if scan_process_navidrome is not None:
            if isinstance(scan_process_navidrome, dict):
                thread = scan_process_navidrome.get('thread')
                if thread and thread.is_alive():
                    flash("Navidrome sync scan is already running", "warning")
                    return redirect(url_for("dashboard"))
            elif hasattr(scan_process_navidrome, 'is_alive') and scan_process_navidrome.is_alive():
                flash("Navidrome sync scan is already running", "warning")
                return redirect(url_for("dashboard"))
        
        try:
            db_dir = os.path.dirname(DB_PATH)
            nav_progress_file = os.path.join(db_dir, "navidrome_scan_progress.json")
            _write_progress_file(nav_progress_file, "navidrome_scan", True, {"status": "starting"})
            
            def run_navidrome_import_bg():
                global scan_process_navidrome
                try:
                    # Ensure singles/rating pipeline stays off during Navidrome metadata-only import
                    os.environ["SPTNR_SKIP_SINGLES"] = "1"

                    logging.info(f"Starting Navidrome import-only scan (mode={mode})")
                    checkpoint_path = os.path.join(os.path.dirname(DB_PATH), "navidrome_scan_checkpoint.json")
                    
                    artist_map = build_artist_index()
                    artists = list(artist_map.items())
                    total = len(artists)
                    
                    # Check if we have a checkpoint from a previous scan or if resume mode is active
                    start_idx = 0
                    last_scanned_artist = None
                    
                    # For resume mode, get last scanned artist from database or progress files
                    if mode == 'resume' or mode == 'resume_force':
                        from scan_resume import get_last_scanned_artist
                        last_scanned_artist = get_last_scanned_artist(scan_type="navidrome", db_path=DB_PATH)
                        if last_scanned_artist:
                            logging.info(f"Resume mode: Found last scanned artist '{last_scanned_artist}'")
                    # Otherwise check checkpoint file
                    elif os.path.exists(checkpoint_path):
                        try:
                            with open(checkpoint_path, 'r') as f:
                                checkpoint = json.load(f)
                                last_scanned_artist = checkpoint.get("last_scanned_artist")
                        except Exception as e:
                            logging.warning(f"Error reading checkpoint: {e}, starting from beginning")
                    
                    # Find the index of the last scanned artist
                    if last_scanned_artist:
                        for idx, (artist_name, _) in enumerate(artists):
                            if artist_name == last_scanned_artist:
                                start_idx = idx  # Start from this artist (rescan it completely)
                                logging.info(f"Resuming Navidrome scan from artist index {start_idx} ('{last_scanned_artist}')")
                                break
                    
                    # Determine force and filter logic based on mode
                    force_rescan = (mode == 'force' or mode == 'resume_force')
                    filter_missing = (mode == 'missing')
                    
                    # Scan artists starting from checkpoint or beginning
                    for idx, (artist_name, info) in enumerate(artists[start_idx:], start=start_idx):
                        # Check if scan should stop
                        with scan_lock:
                            if scan_process_navidrome is None:
                                logging.info("Navidrome scan stop signal received, exiting gracefully")
                                _write_progress_with_current_artist(nav_progress_file, "navidrome_scan", False, {"status": "stopped", "exit_code": 0})
                                return
                        
                        scan_artist_to_db(
                            artist_name, 
                            info.get("id"), 
                            verbose=False, 
                            force=force_rescan,
                            filter_missing=filter_missing,
                            processed_artists=idx, 
                            total_artists=total
                        )
                        
                        # Update checkpoint with the last scanned artist
                        try:
                            with open(checkpoint_path, 'w') as f:
                                json.dump({"last_scanned_artist": artist_name}, f)
                        except Exception as e:
                            logging.warning(f"Error saving checkpoint: {e}")
                    
                    # Clear checkpoint when scan completes successfully
                    if os.path.exists(checkpoint_path):
                        os.remove(checkpoint_path)
                    
                    _write_progress_with_current_artist(nav_progress_file, "navidrome_scan", False, {"status": "complete", "exit_code": 0})
                    logging.info("Navidrome import-only scan completed")
                except Exception as e:
                    logging.error(f"Error in Navidrome import-only scan: {e}", exc_info=True)
                    _write_progress_with_current_artist(nav_progress_file, "navidrome_scan", False, {"status": "error", "error": str(e), "exit_code": 1})
                finally:
                    # Clear skip flag so popularity/singles scans run normally elsewhere
                    os.environ.pop("SPTNR_SKIP_SINGLES", None)
            
            scan_thread = threading.Thread(target=run_navidrome_import_bg, daemon=False)
            scan_thread.start()
            scan_process_navidrome = {'thread': scan_thread, 'type': 'navidrome'}
            mode_desc = {
                'all': 'Full', 
                'force': 'Full (Forced)', 
                'missing': 'Missing Only',
                'resume': 'Resume from Last',
                'resume_force': 'Resume (Forced)'
            }.get(mode, 'Full')
            flash(f"✅ Navidrome import started ({mode_desc} scan)", "success")
        except Exception as e:
            logging.error(f"Error starting Navidrome import: {e}", exc_info=True)
            flash(f"❌ Error starting Navidrome import: {str(e)}", "danger")
    
    return redirect(url_for("dashboard"))


@app.route("/scan/combined", methods=["POST"])
def scan_combined():
    """Run combined scan: Navidrome import + popularity + singles for each artist."""
    global scan_process_combined
    
    # Get scan mode from query parameters (default: "all")
    mode = request.args.get('mode', 'all')  # all, force, resume, resume_force
    
    with scan_lock:
        if scan_process_combined is not None:
            if isinstance(scan_process_combined, dict):
                thread = scan_process_combined.get('thread')
                if thread and thread.is_alive():
                    flash("Combined scan is already running", "warning")
                    return redirect(url_for("dashboard"))
            elif hasattr(scan_process_combined, 'is_alive') and scan_process_combined.is_alive():
                flash("Combined scan is already running", "warning")
                return redirect(url_for("dashboard"))
        
        try:
            db_dir = os.path.dirname(DB_PATH)
            combined_progress_file = os.path.join(db_dir, "combined_scan_progress.json")
            _write_progress_file(combined_progress_file, "combined_scan", True, {"status": "starting"})
            
            def run_combined_scan_bg():
                global scan_process_combined
                try:
                    logging.info(f"Starting combined scan (mode={mode})")
                    from popularity import popularity_scan as scan_popularity_func
                    
                    # Build artist index
                    artist_map = build_artist_index()
                    artists = list(artist_map.items())
                    total = len(artists)
                    
                    # Determine force rescan based on mode
                    force_rescan = (mode == 'force' or mode == 'resume_force')
                    
                    # Determine start index for resume mode
                    start_idx = 0
                    if mode == 'resume' or mode == 'resume_force':
                        from scan_resume import get_last_scanned_artist
                        last_scanned_artist = get_last_scanned_artist(scan_type="combined", db_path=DB_PATH)
                        if last_scanned_artist:
                            logging.info(f"Resume mode: Found last scanned artist '{last_scanned_artist}'")
                            for idx, (artist_name, _) in enumerate(artists):
                                if artist_name == last_scanned_artist:
                                    start_idx = idx  # Start from this artist (rescan it completely)
                                    logging.info(f"Resuming combined scan from artist index {start_idx} ('{last_scanned_artist}')")
                                    break
                        else:
                            logging.warning("Resume mode: No last scanned artist found, starting from beginning")
                    
                    # Process each artist sequentially
                    for idx, (artist_name, info) in enumerate(artists[start_idx:], start=start_idx+1):
                        # Check if scan should stop
                        with scan_lock:
                            if scan_process_combined is None:
                                logging.info("Combined scan stop signal received, exiting gracefully")
                                _write_progress_with_current_artist(combined_progress_file, "combined_scan", False, {"status": "stopped", "exit_code": 0})
                                return
                        
                        artist_id = info.get("id")
                        
                        logging.info(f"[{idx}/{total}] Processing artist: {artist_name}")
                        
                        # Step 1: Navidrome import for this artist
                        # Note: filter_missing is always False because we process each artist explicitly
                        # The combined scan handles all artists in sequence, unlike bulk scans
                        logging.info(f"  → Navidrome import for {artist_name}")
                        try:
                            scan_artist_to_db(
                                artist_name, 
                                artist_id, 
                                verbose=False, 
                                force=force_rescan,
                                filter_missing=False,  # Combined scan processes all artists explicitly
                                processed_artists=idx, 
                                total_artists=total
                            )
                        except Exception as e:
                            logging.error(f"Error in Navidrome import for {artist_name}: {e}")
                            # Continue with next steps even if Navidrome import fails
                        
                        # Step 2: Popularity and singles scan for this artist
                        # Note: artist_filter expects artist name (string), not ID
                        # This is by design - popularity_scan uses name for SQL WHERE clause
                        logging.info(f"  → Popularity & singles scan for {artist_name}")
                        try:
                            scan_popularity_func(
                                verbose=False, 
                                force=force_rescan,
                                artist_filter=artist_name
                            )
                        except Exception as e:
                            logging.error(f"Error in popularity scan for {artist_name}: {e}")
                            # Continue with next artist even if popularity scan fails
                        
                        # Update progress
                        progress_data = {
                            "status": "running",
                            "current_artist": artist_name,
                            "processed_artists": idx,
                            "total_artists": total,
                            "percent_complete": int((idx / total) * 100)
                        }
                        _write_progress_file(combined_progress_file, "combined_scan", True, progress_data)
                    
                    _write_progress_with_current_artist(combined_progress_file, "combined_scan", False, {"status": "complete", "exit_code": 0})
                    logging.info("Combined scan completed successfully")
                except Exception as e:
                    logging.error(f"Error in combined scan: {e}", exc_info=True)
                    _write_progress_with_current_artist(combined_progress_file, "combined_scan", False, {"status": "error", "error": str(e), "exit_code": 1})
                finally:
                    # Clean up thread reference when done
                    with scan_lock:
                        scan_process_combined = None
            
            # daemon=False matches other scan threads - allows scan to complete even during shutdown
            scan_thread = threading.Thread(target=run_combined_scan_bg, daemon=False)
            scan_thread.start()
            scan_process_combined = {'thread': scan_thread, 'type': 'combined'}
            mode_desc = {
                'all': 'Full', 
                'force': 'Full (Forced)',
                'resume': 'Resume from Last',
                'resume_force': 'Resume (Forced)'
            }.get(mode, 'Full')
            flash(f"✅ Combined scan started ({mode_desc} - Navidrome → Popularity → Singles for each artist)", "success")
        except Exception as e:
            logging.error(f"Error starting combined scan: {e}", exc_info=True)
            flash(f"❌ Error starting combined scan: {str(e)}", "danger")
    
    return redirect(url_for("dashboard"))


@app.route("/api/slskd/download", methods=["POST"])
def slskd_download():
    """Proxy endpoint to download from slskd"""
    cfg = get_config()
    slskd_config = cfg.get("slskd", {})
    
    if not slskd_config.get("enabled"):
        return jsonify({"error": "slskd integration not enabled"}), 400
    
    payload = request.json or {}
    files_payload = payload.get("files")
    username = payload.get("username", "")
    filename = payload.get("filename", "")
    size = payload.get("size", 0)

    # Batch mode: expect list of files
    if files_payload:
        if not isinstance(files_payload, list):
            return jsonify({"error": "files must be a list"}), 400
        normalized_files = []
        for entry in files_payload:
            u = entry.get("username")
            f = entry.get("filename")
            if not u or not f:
                return jsonify({"error": "Each file requires username and filename"}), 400
            normalized_files.append({
                "username": u,
                "filename": f,
                "size": int(entry.get("size") or 0)
            })

        web_url = slskd_config.get("web_url", "http://localhost:5030")
        api_key = slskd_config.get("api_key", "")
        try:
            client = SlskdClient(web_url, api_key, enabled=True)
            results = client.download_files(normalized_files)
            requested = sum(item.get("requested", 0) for item in results)
            successful_users = sum(1 for item in results if item.get("success"))
            overall_success = requested > 0 and successful_users > 0
            return jsonify({
                "success": overall_success,
                "requested": requested,
                "userBatches": results
            })
        except Exception as e:
            logging.error(f"[SLSKD] Batch download error: {str(e)}")
            return jsonify({"error": str(e)}), 500
    
    if not username or not filename:
        return jsonify({"error": "Username and filename required"}), 400
    
    web_url = slskd_config.get("web_url", "http://localhost:5030")
    api_key = slskd_config.get("api_key", "")
    
    try:
        client = SlskdClient(web_url, api_key, enabled=True)
        success = client.download_file(username, filename, size)
        
        if success:
            return jsonify({"success": True, "message": "Download enqueued"})
        else:
            return jsonify({"error": "Failed to enqueue download"}), 500
            
    except Exception as e:
        logging.error(f"[SLSKD] Download error: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/slskd/cancel", methods=["POST"])
def slskd_cancel():
    """Cancel a Soulseek download"""
    cfg = get_config()
    slskd_config = cfg.get("slskd", {})
    
    if not slskd_config.get("enabled"):
        return jsonify({"error": "slskd integration not enabled"}), 400
    
    username = request.json.get("username", "")
    filename = request.json.get("filename", "")
    
    if not username or not filename:
        return jsonify({"error": "Username and filename required"}), 400
    
    web_url = slskd_config.get("web_url", "http://localhost:5030")
    api_key = slskd_config.get("api_key", "")
    
    try:
        import requests as req
        
        headers = {"X-API-Key": api_key} if api_key else {}
        
        # Cancel download - DELETE request to the specific download
        cancel_url = f"{web_url}/api/v0/transfers/downloads/{username}/{filename}"
        
        resp = req.delete(cancel_url, headers=headers, timeout=10)
        
        if resp.status_code in [200, 204]:
            return jsonify({"success": True, "message": "Download cancelled successfully"})
        else:
            return jsonify({"error": f"Failed to cancel download: {resp.status_code}"}), 500
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/slskd/retry", methods=["POST"])
def slskd_retry():
    """Retry a failed Soulseek download"""
    cfg = get_config()
    slskd_config = cfg.get("slskd", {})
    
    if not slskd_config.get("enabled"):
        return jsonify({"error": "slskd integration not enabled"}), 400
    
    username = request.json.get("username", "")
    filename = request.json.get("filename", "")
    size = request.json.get("size", 0)
    
    if not username or not filename:
        return jsonify({"error": "Username and filename required"}), 400
    
    web_url = slskd_config.get("web_url", "http://localhost:5030")
    api_key = slskd_config.get("api_key", "")
    
    try:
        import requests as req_module
        
        # First cancel the existing download
        headers = {"X-API-Key": api_key} if api_key else {}
        cancel_url = f"{web_url}/api/v0/transfers/downloads/{username}/{filename}"
        req_module.delete(cancel_url, headers=headers, timeout=10)
        
        # Then re-queue it
        client = SlskdClient(web_url, api_key, enabled=True)
        success = client.download_file(username, filename, int(size))
        
        if success:
            return jsonify({"success": True, "message": f"Download retry queued for {filename}"})
        else:
            return jsonify({"error": "Failed to re-queue download"}), 500
            
    except Exception as e:
        logging.error(f"[SLSKD] Retry download error: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/slskd/search-again", methods=["POST"])
def slskd_search_again():
    """Search for a file again to find alternative sources"""
    cfg = get_config()
    slskd_config = cfg.get("slskd", {})
    
    if not slskd_config.get("enabled"):
        return jsonify({"error": "slskd integration not enabled"}), 400
    
    filename = request.json.get("filename", "")
    
    if not filename:
        return jsonify({"error": "Filename required"}), 400
    
    web_url = slskd_config.get("web_url", "http://localhost:5030")
    api_key = slskd_config.get("api_key", "")
    
    try:
        client = SlskdClient(web_url, api_key, enabled=True)
        search_id = client.start_search(filename)
        
        if search_id:
            return jsonify({
                "success": True,
                "message": f"Searching for '{filename}'",
                "search_id": search_id
            })
        else:
            return jsonify({"error": "Failed to start search"}), 500
            
    except Exception as e:
        logging.error(f"[SLSKD] Search again error: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/slskd/download-single", methods=["POST"])
def slskd_download_single():
    """Download a single track from Soulseek search results in playlist importer"""
    cfg = get_config()
    slskd_config = cfg.get("slskd", {})
    
    if not slskd_config.get("enabled"):
        return jsonify({"error": "slskd integration not enabled"}), 400
    
    username = request.json.get("username", "")
    filename = request.json.get("filename", "")
    size = request.json.get("size", 0)
    
    if not username or not filename:
        return jsonify({"error": "Username and filename required"}), 400
    
    web_url = slskd_config.get("web_url", "http://localhost:5030")
    api_key = slskd_config.get("api_key", "")
    
    try:
        client = SlskdClient(web_url, api_key, enabled=True)
        success = client.download_file(username, filename, int(size))
        
        if success:
            return jsonify({"success": True, "message": f"Download started for {filename}"})
        else:
            return jsonify({"error": "Failed to enqueue download"}), 500
            
    except Exception as e:
        logging.error(f"[SLSKD] Single file download error: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/musicbrainz/search", methods=["POST"])
def api_musicbrainz_search():
    """Search MusicBrainz for releases + local cached missing releases"""
    query = request.json.get("query", "").strip()
    if not query:
        return jsonify({"error": "Query parameter required"}), 400
    
    try:
        releases = []
        seen_ids = set()  # Track IDs to avoid duplicates
        
        # First, search local database for matching artists with missing releases
        try:
            conn = get_db()
            cursor = conn.cursor()
            
            # Search for artists matching the query
            query_pattern = f"%{query}%"
            cursor.execute("""
                SELECT DISTINCT artist, release_id, title, primary_type, first_release_date, cover_art_url, category
                FROM missing_releases
                WHERE artist LIKE ? OR title LIKE ?
                ORDER BY artist, first_release_date DESC
                LIMIT 50
            """, (query_pattern, query_pattern))
            
            local_results = cursor.fetchall()
            conn.close()
            
            # Add local results
            for row in local_results:
                artist, release_id, title, primary_type, first_release_date, cover_art_url, category = row
                
                # Create unique ID to check for duplicates
                result_id = f"{artist}_{release_id}"
                if result_id not in seen_ids:
                    seen_ids.add(result_id)
                    releases.append({
                        "id": release_id,
                        "title": title,
                        "artist": artist,
                        "artist-credit": [{"name": artist}],
                        "primary_type": primary_type,
                        "first_release_date": first_release_date,
                        "cover_art_url": cover_art_url,
                        "category": category,
                        "source": "local"
                    })
        except Exception as e:
            logging.warning(f"[MB_SEARCH] Error searching local database: {e}")
        
        # Then search MusicBrainz
        try:
            headers = {"User-Agent": MUSICBRAINZ_USER_AGENT}
            
            # Search for release groups
            url = "https://musicbrainz.org/ws/2/release-group"
            params = {
                "fmt": "json",
                "limit": 50,
                "query": query
            }
            
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            
            for rg in data.get("release-groups", []) or []:
                rg_id = rg.get("id", "")
                primary_type = rg.get("primary-type", "")
                artist_credit = rg.get("artist-credit", [])
                artist_name = artist_credit[0].get("name", "Unknown") if artist_credit else "Unknown"
                
                # Check if we already have this from local DB
                result_id = f"{artist_name}_{rg_id}"
                if result_id in seen_ids:
                    continue
                
                seen_ids.add(result_id)
                
                # Determine category
                category = primary_type
                if primary_type.lower() == "ep":
                    category = "EP"
                elif primary_type.lower() == "single":
                    category = "Single"
                elif primary_type.lower() == "album":
                    category = "Album"
                
                releases.append({
                    "id": rg_id,
                    "title": rg.get("title", ""),
                    "artist": artist_name,
                    "artist-credit": artist_credit,
                    "primary_type": primary_type,
                    "first_release_date": rg.get("first-release-date", ""),
                    "cover_art_url": f"https://coverartarchive.org/release-group/{rg_id}/front-500" if rg_id else "",
                    "category": category,
                    "source": "musicbrainz"
                })
        except requests.exceptions.Timeout:
            logging.warning("[MB_SEARCH] MusicBrainz request timed out")
        except Exception as e:
            logging.error(f"[MB_SEARCH] MusicBrainz search error: {e}")
        
        # Sort by artist and release date
        releases.sort(key=lambda x: (x.get("artist", "").lower(), x.get("first_release_date", "")), reverse=True)
        
        return jsonify({"releases": releases, "total": len(releases)})
        
    except Exception as e:
        logging.error(f"[MB_SEARCH] Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/musicbrainz/download", methods=["POST"])
def api_musicbrainz_download():
    """Initiate a managed download from MusicBrainz release"""
    data = request.json or {}
    release_id = data.get("release_id", "").strip()
    release_title = data.get("release_title", "").strip()
    artist = data.get("artist", "").strip()
    method = data.get("method", "").strip().lower()
    persistent_search = data.get("persistent_search", False)  # New: Keep searching until found
    max_retries = data.get("max_retries", 3)  # New: Max number of retries
    session_id = data.get("session_id", None)  # New: Link to playlist session (optional)
    
    if not all([release_id, release_title, artist, method]):
        return jsonify({"error": "Missing required parameters"}), 400
    
    if method not in ["slskd", "qbittorrent"]:
        return jsonify({"error": "Invalid method. Use 'slskd' or 'qbittorrent'"}), 400
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # If session_id provided, verify it exists
        if session_id:
            cursor.execute("SELECT id FROM playlist_download_sessions WHERE id = ?", (session_id,))
            if not cursor.fetchone():
                conn.close()
                return jsonify({"error": f"Session {session_id} not found"}), 404
        
        # Create search query
        download_query = f"{artist} {release_title}"
        
        # Insert into managed_downloads table with persistent search settings and session link
        cursor.execute("""
            INSERT INTO managed_downloads 
            (release_id, release_title, artist, method, status, download_query, persistent_search, max_retries, session_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """, (release_id, release_title, artist, method, download_query, 1 if persistent_search else 0, max_retries, session_id))
        
        tracking_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        # Immediately initiate the download in background thread with fresh connection
        if method == "slskd":
            thread = threading.Thread(target=_initiate_slskd_download_bg, args=(tracking_id, download_query), daemon=True)
            thread.start()
        elif method == "qbittorrent":
            thread = threading.Thread(target=_initiate_qbit_download_bg, args=(tracking_id, download_query), daemon=True)
            thread.start()
        
        return jsonify({
            "success": True,
            "tracking_id": tracking_id,
            "message": f"Download queued for {release_title}",
            "persistent_search": persistent_search,
            "session_id": session_id
        })
        
    except Exception as e:
        logging.error(f"[MB_DOWNLOAD] Error: {e}")
        return jsonify({"error": str(e)}), 500


def _initiate_slskd_download_bg(tracking_id, query):
    """Background thread worker to initiate a Soulseek search and wait for user selection"""
    try:
        cfg = get_config()
        slskd_config = cfg.get("slskd", {})
        
        if not slskd_config.get("enabled"):
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE managed_downloads 
                SET status = 'error', error_message = 'Soulseek not enabled', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (tracking_id,))
            conn.commit()
            conn.close()
            return
        
        web_url = slskd_config.get("web_url", "http://localhost:5030")
        api_key = slskd_config.get("api_key", "")
        
        client = SlskdClient(web_url, api_key, enabled=True)
        search_id = client.search(query)
        
        if not search_id:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE managed_downloads 
                SET status = 'error', error_message = 'Failed to start search', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (tracking_id,))
            conn.commit()
            conn.close()
            return
        
        # Store search_id and update status
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE managed_downloads 
            SET status = 'searching', external_id = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (search_id, tracking_id))
        conn.commit()
        conn.close()
        
        # Start monitoring in a sub-thread
        def monitor_slskd_search():
            import time
            max_wait = 30  # Wait up to 30 seconds for results
            start_time = time.time()
            all_files = []  # Collect all results, not just the best
            
            while time.time() - start_time < max_wait:
                try:
                    responses, state, is_complete = client.get_search_results(search_id)
                    
                    if responses:
                        # Collect all matching files with scores
                        for response in responses:
                            if hasattr(response, 'files') and response.files:
                                for file_info in response.files:
                                    # Score the file based on how well it matches the query
                                    filename = file_info.get('filename', '').lower()
                                    query_lower = query.lower()
                                    
                                    # Simple scoring: count matching words
                                    query_words = query_lower.split()
                                    matches = sum(1 for word in query_words if word in filename)
                                    match_score = matches / len(query_words) if query_words else 0
                                    
                                    # Prefer files with audio extensions
                                    if any(filename.endswith(ext) for ext in ['.mp3', '.flac', '.m4a', '.aac', '.ogg']):
                                        match_score *= 1.2
                                    
                                    if match_score >= 0.3:  # Only include files with at least 30% match
                                        all_files.append({
                                            'username': response.username if hasattr(response, 'username') else 'Unknown',
                                            'filename': file_info.get('filename', ''),
                                            'size': file_info.get('size', 0),
                                            'match_score': match_score
                                        })
                    
                    if is_complete:
                        break
                    
                    time.sleep(1)
                    
                except Exception as e:
                    logging.error(f"[SLSKD_MONITOR] Error monitoring search {search_id}: {e}")
                    break
            
            # Save results and wait for user selection
            if all_files:
                try:
                    conn2 = get_db()
                    cursor2 = conn2.cursor()
                    
                    # Sort by match score (descending)
                    all_files.sort(key=lambda x: x['match_score'], reverse=True)
                    
                    # Insert all results into database
                    for file_result in all_files:
                        cursor2.execute("""
                            INSERT INTO slskd_search_results 
                            (download_id, username, filename, size, match_score)
                            VALUES (?, ?, ?, ?, ?)
                        """, (tracking_id, file_result['username'], file_result['filename'], 
                              file_result['size'], file_result['match_score']))
                    
                    # Update status to awaiting_selection
                    cursor2.execute("""
                        UPDATE managed_downloads 
                        SET status = 'awaiting_selection', updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """, (tracking_id,))
                    
                    conn2.commit()
                    conn2.close()
                    
                    logging.info(f"[SLSKD_MONITOR] Found {len(all_files)} results for download {tracking_id}, awaiting user selection")
                    
                except Exception as e:
                    logging.error(f"[SLSKD_MONITOR] Error saving results: {e}")
                    try:
                        conn2 = get_db()
                        cursor2 = conn2.cursor()
                        cursor2.execute("""
                            UPDATE managed_downloads 
                            SET status = 'error', error_message = ?, updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                        """, (str(e), tracking_id))
                        conn2.commit()
                        conn2.close()
                    except:
                        pass
            else:
                # No good matches found
                try:
                    conn2 = get_db()
                    cursor2 = conn2.cursor()
                    cursor2.execute("""
                        UPDATE managed_downloads 
                        SET status = 'error', error_message = 'No matching files found', updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """, (tracking_id,))
                    conn2.commit()
                    conn2.close()
                except:
                    pass
        
        # Start monitoring thread
        thread = threading.Thread(target=monitor_slskd_search, daemon=True)
        thread.start()
            
    except Exception as e:
        logging.error(f"[SLSKD_INIT] Error for tracking {tracking_id}: {e}")
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE managed_downloads 
                SET status = 'error', error_message = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (str(e), tracking_id))
            conn.commit()
            conn.close()
        except:
            pass


def _initiate_slskd_download(tracking_id, query, cursor, conn):
    """Helper to initiate a Soulseek download"""
    try:
        cfg = get_config()
        slskd_config = cfg.get("slskd", {})
        
        if not slskd_config.get("enabled"):
            cursor.execute("""
                UPDATE managed_downloads 
                SET status = 'error', error_message = 'Soulseek not enabled', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (tracking_id,))
            conn.commit()
            return
        
        web_url = slskd_config.get("web_url", "http://localhost:5030")
        api_key = slskd_config.get("api_key", "")
        
        client = SlskdClient(web_url, api_key, enabled=True)
        search_id = client.search(query)
        
        if not search_id:
            cursor.execute("""
                UPDATE managed_downloads 
                SET status = 'error', error_message = 'Failed to start search', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (tracking_id,))
            conn.commit()
            return
        
        # Store search_id and update status
        cursor.execute("""
            UPDATE managed_downloads 
            SET status = 'searching', external_id = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (search_id, tracking_id))
        conn.commit()
        
        # Start a thread to monitor the search and download when results are available
        def monitor_slskd_search():
            import time
            # Increased timeout to 60 seconds to handle slow Soulseek peer responses
            # Each peer has a 5-second timeout; many timeouts need more time to collect results
            max_wait = 60
            start_time = time.time()
            best_file = None
            best_match_score = 0
            
            while time.time() - start_time < max_wait:
                try:
                    responses, state, is_complete = client.get_search_results(search_id)
                    
                    if responses:
                        # Look through responses for best matching files
                        for response in responses:
                            if hasattr(response, 'files') and response.files and len(response.files) > 0:
                                for file_info in response.files:
                                    # Get filename - handle both dict and object formats
                                    filename = file_info.get('filename', '') if isinstance(file_info, dict) else getattr(file_info, 'filename', '')
                                    filename_lower = str(filename).lower()
                                    query_lower = query.lower()
                                    
                                    # Simple scoring: count matching words
                                    query_words = query_lower.split()
                                    matches = sum(1 for word in query_words if word in filename_lower)
                                    match_score = matches / len(query_words) if query_words else 0
                                    
                                    # Prefer files with audio extensions
                                    if any(filename_lower.endswith(ext) for ext in ['.mp3', '.flac', '.m4a', '.aac', '.ogg']):
                                        match_score *= 1.2
                                    else:
                                        match_score *= 0.5  # Lower score for non-audio
                                    
                                    if match_score > best_match_score:
                                        best_match_score = match_score
                                        best_file = {
                                            'username': response.username if hasattr(response, 'username') else 'Unknown',
                                            'filename': filename,
                                            'size': file_info.get('size', 0) if isinstance(file_info, dict) else getattr(file_info, 'size', 0),
                                            'match_score': match_score
                                        }
                    
                    if is_complete and best_file and best_match_score >= 0.3:
                        # Stop early if search is complete and we have a good match
                        logging.debug(f"[SLSKD_MONITOR] Search complete with good match score {best_match_score}")
                        break
                    
                    if is_complete and not best_file:
                        # Stop if search is complete but no files found
                        logging.debug(f"[SLSKD_MONITOR] Search complete but no files found")
                        break
                    
                    time.sleep(1)
                    
                except Exception as e:
                    logging.error(f"[SLSKD_MONITOR] Error monitoring search {search_id}: {e}")
                    break
            
            # Download the best file found
            if best_file and best_match_score >= 0.3:  # Minimum 30% match
                try:
                    conn2 = get_db()
                    cursor2 = conn2.cursor()
                    
                    logging.info(f"[SLSKD_MONITOR] Downloading best match with score {best_match_score}: {best_file['filename'][:80]}")
                    
                    # Start the download
                    success = client.download_file(
                        best_file['username'],
                        best_file['filename'],
                        best_file['size']
                    )
                    
                    if success:
                        cursor2.execute("""
                            UPDATE managed_downloads 
                            SET status = 'downloading', updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                        """, (tracking_id,))
                        logging.info(f"[SLSKD_MONITOR] Started download: {best_file['filename']} from {best_file['username']}")
                    else:
                        cursor2.execute("""
                            UPDATE managed_downloads 
                            SET status = 'error', error_message = 'Failed to start file download', updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                        """, (tracking_id,))
                    
                    conn2.commit()
                    conn2.close()
                    
                except Exception as e:
                    logging.error(f"[SLSKD_MONITOR] Error downloading file: {e}")
                    try:
                        conn2 = get_db()
                        cursor2 = conn2.cursor()
                        cursor2.execute("""
                            UPDATE managed_downloads 
                            SET status = 'error', error_message = ?, updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                        """, (str(e), tracking_id))
                        conn2.commit()
                        conn2.close()
                    except:
                        pass
            else:
                # No good matches found
                try:
                    conn2 = get_db()
                    cursor2 = conn2.cursor()
                    match_score_msg = f"match score {best_match_score}" if best_file else "no responses with files"
                    cursor2.execute("""
                        UPDATE managed_downloads 
                        SET status = 'error', error_message = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """, (f"No matching files found ({match_score_msg})", tracking_id))
                    conn2.commit()
                    conn2.close()
                except:
                    pass
        
        # Start monitoring thread
        import threading
        thread = threading.Thread(target=monitor_slskd_search, daemon=True)
        thread.start()
            
    except Exception as e:
        logging.error(f"[SLSKD_INIT] Error for tracking {tracking_id}: {e}")
        cursor.execute("""
            UPDATE managed_downloads 
            SET status = 'error', error_message = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (str(e), tracking_id))
        conn.commit()


def _initiate_qbit_download_bg(tracking_id, query):
    """Background thread worker to initiate a qBittorrent download with fresh DB connection"""
    try:
        cfg = get_config()
        qbit_config = cfg.get("qbittorrent", {})
        
        if not qbit_config.get("enabled"):
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE managed_downloads 
                SET status = 'error', error_message = 'qBittorrent not enabled', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (tracking_id,))
            conn.commit()
            conn.close()
            return
        
        web_url = qbit_config.get("web_url", "http://localhost:8080")
        username = qbit_config.get("username", "")
        password = qbit_config.get("password", "")
        
        # Update status to searching
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE managed_downloads 
            SET status = 'searching', updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (tracking_id,))
        conn.commit()
        conn.close()
        
        # Start search in qBittorrent in a background thread
        def search_and_add_qbit():
            try:
                import requests as req
                import time
                
                session = req.Session()
                
                # Login if credentials provided
                if username and password:
                    login_url = f"{web_url}/api/v2/auth/login"
                    try:
                        session.post(login_url, data={"username": username, "password": password}, timeout=5)
                    except:
                        pass  # May not require login
                
                # Start search
                search_url = f"{web_url}/api/v2/search/start"
                resp = session.post(search_url, data={"pattern": query, "plugins": "all", "category": "music"}, timeout=10)
                
                if resp.status_code not in [200, 201]:
                    raise Exception(f"Search failed: {resp.status_code}")
                
                search_data = resp.json()
                search_id = search_data.get("id")
                
                if not search_id:
                    raise Exception("No search ID returned from qBittorrent")
                
                # Poll for results
                best_result = None
                for i in range(60):  # Poll for up to 30 seconds
                    time.sleep(0.5)
                    
                    status_url = f"{web_url}/api/v2/search/status"
                    status_resp = session.get(status_url, params={"id": search_id}, timeout=5)
                    
                    if status_resp.status_code == 200:
                        status_data = status_resp.json()
                        
                        if status_data and len(status_data) > 0:
                            # Get results
                            results_url = f"{web_url}/api/v2/search/results"
                            results_resp = session.get(results_url, params={"id": search_id, "limit": 100}, timeout=5)
                            
                            if results_resp.status_code == 200:
                                data = results_resp.json()
                                results = data.get("results", [])
                                
                                if results:
                                    # Pick the result with best seeders
                                    best_result = max(results, key=lambda x: x.get('nb_seeders', 0))
                            
                            # Check if search is done
                            search_status = status_data[0]
                            if search_status.get("status") == "Stopped":
                                break
                
                # Stop search
                try:
                    stop_url = f"{web_url}/api/v2/search/stop"
                    session.post(stop_url, data={"id": search_id}, timeout=5)
                except:
                    pass
                
                # Add the best torrent found
                if best_result:
                    conn2 = get_db()
                    cursor2 = conn2.cursor()
                    
                    try:
                        # Add magnet link if available
                        add_url = f"{web_url}/api/v2/torrents/add"
                        magnet = best_result.get('magnet_uri') or best_result.get('magnet')
                        torrent_url = best_result.get('torrent_url') or best_result.get('link')
                        
                        if magnet:
                            resp = session.post(add_url, data={"urls": magnet}, timeout=10)
                        elif torrent_url:
                            resp = session.post(add_url, data={"urls": torrent_url}, timeout=10)
                        else:
                            raise Exception("No magnet link or torrent URL found")
                        
                        if resp.status_code in [200, 403]:  # 403 might mean already added
                            cursor2.execute("""
                                UPDATE managed_downloads 
                                SET status = 'downloading', updated_at = CURRENT_TIMESTAMP
                                WHERE id = ?
                            """, (tracking_id,))
                            logging.info(f"[QBIT_MONITOR] Added torrent: {best_result.get('name', 'Unknown')}")
                        else:
                            cursor2.execute("""
                                UPDATE managed_downloads 
                                SET status = 'error', error_message = ?, updated_at = CURRENT_TIMESTAMP
                                WHERE id = ?
                            """, (f"qBittorrent returned {resp.status_code}", tracking_id))
                    except Exception as e:
                        cursor2.execute("""
                            UPDATE managed_downloads 
                            SET status = 'error', error_message = ?, updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                        """, (str(e), tracking_id))
                    
                    conn2.commit()
                    conn2.close()
                else:
                    conn2 = get_db()
                    cursor2 = conn2.cursor()
                    cursor2.execute("""
                        UPDATE managed_downloads 
                        SET status = 'error', error_message = 'No torrent results found', updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """, (tracking_id,))
                    conn2.commit()
                    conn2.close()
                    
            except Exception as e:
                logging.error(f"[QBIT_MONITOR] Error: {e}")
                try:
                    conn2 = get_db()
                    cursor2 = conn2.cursor()
                    cursor2.execute("""
                        UPDATE managed_downloads 
                        SET status = 'error', error_message = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """, (str(e), tracking_id))
                    conn2.commit()
                    conn2.close()
                except:
                    pass
        
        # Start qBit search in a background thread
        thread = threading.Thread(target=search_and_add_qbit, daemon=True)
        thread.start()
        
    except Exception as e:
        logging.error(f"[QBIT_INIT] Error for tracking {tracking_id}: {e}")
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE managed_downloads 
                SET status = 'error', error_message = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (str(e), tracking_id))
            conn.commit()
            conn.close()
        except:
            pass


def _initiate_qbit_download(tracking_id, query, cursor, conn):
    """Helper to initiate a qBittorrent download"""
    try:
        cfg = get_config()
        qbit_config = cfg.get("qbittorrent", {})
        
        if not qbit_config.get("enabled"):
            cursor.execute("""
                UPDATE managed_downloads 
                SET status = 'error', error_message = 'qBittorrent not enabled', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (tracking_id,))
            conn.commit()
            return
        
        web_url = qbit_config.get("web_url", "http://localhost:8080")
        username = qbit_config.get("username", "")
        password = qbit_config.get("password", "")
        
        # Update status to searching
        cursor.execute("""
            UPDATE managed_downloads 
            SET status = 'searching', updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (tracking_id,))
        conn.commit()
        
        # Start search in qBittorrent in a background thread
        def search_and_add_qbit():
            try:
                import requests as req
                import time
                
                session = req.Session()
                
                # Login if credentials provided
                if username and password:
                    login_url = f"{web_url}/api/v2/auth/login"
                    try:
                        session.post(login_url, data={"username": username, "password": password}, timeout=5)
                    except:
                        pass  # May not require login
                
                # Start search
                search_url = f"{web_url}/api/v2/search/start"
                resp = session.post(search_url, data={"pattern": query, "plugins": "all", "category": "music"}, timeout=10)
                
                if resp.status_code not in [200, 201]:
                    raise Exception(f"Search failed: {resp.status_code}")
                
                search_data = resp.json()
                search_id = search_data.get("id")
                
                if not search_id:
                    raise Exception("No search ID returned from qBittorrent")
                
                # Poll for results
                best_result = None
                for i in range(60):  # Poll for up to 30 seconds
                    time.sleep(0.5)
                    
                    status_url = f"{web_url}/api/v2/search/status"
                    status_resp = session.get(status_url, params={"id": search_id}, timeout=5)
                    
                    if status_resp.status_code == 200:
                        status_data = status_resp.json()
                        
                        if status_data and len(status_data) > 0:
                            # Get results
                            results_url = f"{web_url}/api/v2/search/results"
                            results_resp = session.get(results_url, params={"id": search_id, "limit": 100}, timeout=5)
                            
                            if results_resp.status_code == 200:
                                data = results_resp.json()
                                results = data.get("results", [])
                                
                                if results:
                                    # Pick the result with best seeders
                                    best_result = max(results, key=lambda x: x.get('nb_seeders', 0))
                            
                            # Check if search is done
                            search_status = status_data[0]
                            if search_status.get("status") == "Stopped":
                                break
                
                # Stop search
                try:
                    stop_url = f"{web_url}/api/v2/search/stop"
                    session.post(stop_url, data={"id": search_id}, timeout=5)
                except:
                    pass
                
                # Add the best torrent found
                if best_result:
                    conn2 = get_db()
                    cursor2 = conn2.cursor()
                    
                    try:
                        # Add magnet link if available
                        add_url = f"{web_url}/api/v2/torrents/add"
                        magnet = best_result.get('magnet_uri') or best_result.get('magnet')
                        torrent_url = best_result.get('torrent_url') or best_result.get('link')
                        
                        if magnet:
                            resp = session.post(add_url, data={"urls": magnet}, timeout=10)
                        elif torrent_url:
                            resp = session.post(add_url, data={"urls": torrent_url}, timeout=10)
                        else:
                            raise Exception("No magnet link or torrent URL found")
                        
                        if resp.status_code in [200, 403]:  # 403 might mean already added
                            cursor2.execute("""
                                UPDATE managed_downloads 
                                SET status = 'downloading', updated_at = CURRENT_TIMESTAMP
                                WHERE id = ?
                            """, (tracking_id,))
                            logging.info(f"[QBIT_MONITOR] Added torrent: {best_result.get('name', 'Unknown')}")
                        else:
                            cursor2.execute("""
                                UPDATE managed_downloads 
                                SET status = 'error', error_message = ?, updated_at = CURRENT_TIMESTAMP
                                WHERE id = ?
                            """, (f"qBittorrent returned {resp.status_code}", tracking_id))
                    except Exception as e:
                        cursor2.execute("""
                            UPDATE managed_downloads 
                            SET status = 'error', error_message = ?, updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                        """, (str(e), tracking_id))
                    
                    conn2.commit()
                    conn2.close()
                else:
                    conn2 = get_db()
                    cursor2 = conn2.cursor()
                    cursor2.execute("""
                        UPDATE managed_downloads 
                        SET status = 'error', error_message = 'No torrent results found', updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """, (tracking_id,))
                    conn2.commit()
                    conn2.close()
                    
            except Exception as e:
                logging.error(f"[QBIT_MONITOR] Error: {e}")
                try:
                    conn2 = get_db()
                    cursor2 = conn2.cursor()
                    cursor2.execute("""
                        UPDATE managed_downloads 
                        SET status = 'error', error_message = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """, (str(e), tracking_id))
                    conn2.commit()
                    conn2.close()
                except:
                    pass
        
        # Start qBit search in a background thread
        import threading
        thread = threading.Thread(target=search_and_add_qbit, daemon=True)
        thread.start()
        
    except Exception as e:
        logging.error(f"[QBIT_INIT] Error for tracking {tracking_id}: {e}")
        cursor.execute("""
            UPDATE managed_downloads 
            SET status = 'error', error_message = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (str(e), tracking_id))
        conn.commit()


# ============================================================================
# PLAYLIST DOWNLOAD MANAGEMENT ROUTES
# ============================================================================

@app.route("/api/playlist-downloads/create", methods=["POST"])
def api_create_playlist_download_session():
    """Create a new playlist download session to group multiple tracks"""
    try:
        data = request.json or {}
        session_name = data.get("session_name", "").strip()
        total_tracks = data.get("total_tracks", 0)
        priority_queue = data.get("priority_queue", False)
        
        if not session_name:
            return jsonify({"error": "session_name is required"}), 400
        
        if total_tracks <= 0:
            return jsonify({"error": "total_tracks must be > 0"}), 400
        
        try:
            current_user = session.get("username", "unknown")
            conn = get_db()
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT INTO playlist_download_sessions 
                (session_name, user, status, total_tracks, priority_queue, created_at, updated_at)
                VALUES (?, ?, 'in_progress', ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """, (session_name, current_user, total_tracks, 1 if priority_queue else 0))
            
            session_id = cursor.lastrowid
            conn.commit()
            conn.close()
            
            logging.info(f"Created playlist download session {session_id}: {session_name} ({total_tracks} tracks)")
            
            return jsonify({
                "success": True,
                "session_id": session_id,
                "session_name": session_name,
                "total_tracks": total_tracks
            })
            
        except Exception as e:
            logging.error(f"[PLAYLIST_SESSION] Error: {e}")
            return jsonify({"error": str(e)}), 500
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/playlist-downloads/<int:session_id>", methods=["GET"])
def api_get_playlist_download_session(session_id):
    """Get playlist download session details and progress"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Get session info
        cursor.execute("""
            SELECT id, session_name, user, status, total_tracks, completed_tracks, 
                   failed_tracks, skipped_tracks, created_at, updated_at, completed_at,
                   estimated_completion, average_retry_count
            FROM playlist_download_sessions
            WHERE id = ?
        """, (session_id,))
        
        row = cursor.fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Session not found"}), 404
        
        # Get associated tracks
        cursor.execute("""
            SELECT id, release_title, artist, method, status, retry_count, max_retries, 
                   persistent_search, current_method, methods_tried
            FROM managed_downloads
            WHERE session_id = ?
            ORDER BY priority DESC, created_at ASC
        """, (session_id,))
        
        tracks = []
        for track_row in cursor.fetchall():
            tracks.append({
                "id": track_row[0],
                "title": track_row[1],
                "artist": track_row[2],
                "method": track_row[3],
                "status": track_row[4],
                "retry_count": track_row[5],
                "max_retries": track_row[6],
                "persistent_search": bool(track_row[7]),
                "current_method": track_row[8],
                "methods_tried": track_row[9]
            })
        
        conn.close()
        
        # Calculate progress percentage
        total = row[4]
        completed = row[5]
        failed = row[6]
        progress_pct = int((completed / total * 100) if total > 0 else 0)
        
        return jsonify({
            "id": row[0],
            "session_name": row[1],
            "user": row[2],
            "status": row[3],
            "total_tracks": row[4],
            "completed_tracks": row[5],
            "failed_tracks": row[6],
            "skipped_tracks": row[7],
            "progress_percent": progress_pct,
            "created_at": row[8],
            "updated_at": row[9],
            "completed_at": row[10],
            "estimated_completion": row[11],
            "average_retry_count": row[12],
            "tracks": tracks
        })
        
    except Exception as e:
        logging.error(f"[PLAYLIST_GET] Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/playlist-downloads/<int:session_id>/cancel", methods=["POST"])
def api_cancel_playlist_download_session(session_id):
    """Cancel a playlist download session and mark all downloads as skipped"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Check if session exists
        cursor.execute("SELECT id FROM playlist_download_sessions WHERE id = ?", (session_id,))
        if not cursor.fetchone():
            conn.close()
            return jsonify({"error": "Session not found"}), 404
        
        # Mark session as cancelled
        cursor.execute("""
            UPDATE playlist_download_sessions 
            SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (session_id,))
        
        # Mark all active downloads in session as skipped
        cursor.execute("""
            UPDATE managed_downloads
            SET status = 'skipped', updated_at = CURRENT_TIMESTAMP
            WHERE session_id = ? AND status NOT IN ('completed', 'failed', 'skipped')
        """, (session_id,))
        
        conn.commit()
        conn.close()
        
        logging.info(f"Cancelled playlist download session {session_id}")
        
        return jsonify({
            "success": True,
            "session_id": session_id,
            "message": "Session cancelled and remaining downloads skipped"
        })
        
    except Exception as e:
        logging.error(f"[PLAYLIST_CANCEL] Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/playlist-downloads", methods=["GET"])
def api_list_playlist_download_sessions():
    """List all playlist download sessions"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Get all sessions, ordered by most recent first
        cursor.execute("""
            SELECT id, session_name, user, status, total_tracks, completed_tracks, 
                   failed_tracks, skipped_tracks, created_at, updated_at, completed_at
            FROM playlist_download_sessions
            ORDER BY updated_at DESC
            LIMIT 100
        """)
        
        sessions = []
        for row in cursor.fetchall():
            total = row[4]
            completed = row[5]
            progress_pct = int((completed / total * 100) if total > 0 else 0)
            
            sessions.append({
                "id": row[0],
                "session_name": row[1],
                "user": row[2],
                "status": row[3],
                "total_tracks": row[4],
                "completed_tracks": row[5],
                "failed_tracks": row[6],
                "skipped_tracks": row[7],
                "progress_percent": progress_pct,
                "created_at": row[8],
                "updated_at": row[9],
                "completed_at": row[10]
            })
        
        conn.close()
        
        return jsonify({
            "success": True,
            "count": len(sessions),
            "sessions": sessions
        })
        
    except Exception as e:
        logging.error(f"[PLAYLIST_LIST] Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/musicbrainz/downloads", methods=["GET"])

def api_musicbrainz_downloads():
    """Get all managed downloads"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT id, release_id, release_title, artist, method, status, 
                   external_id, error_message, created_at, updated_at, completed_at
            FROM managed_downloads
            WHERE status != 'removed'
            ORDER BY created_at DESC
            LIMIT 100
        """)
        
        rows = cursor.fetchall()
        conn.close()
        
        downloads = []
        for row in rows:
            downloads.append({
                "id": row[0],
                "release_id": row[1],
                "release_title": row[2],
                "artist": row[3],
                "method": row[4],
                "status": row[5],
                "external_id": row[6],
                "error_message": row[7],
                "created_at": row[8],
                "updated_at": row[9],
                "completed_at": row[10]
            })
        
        return jsonify({"downloads": downloads})
        
    except Exception as e:
        logging.error(f"[MB_DOWNLOADS] Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/musicbrainz/download/<int:download_id>/retry", methods=["POST"])
def api_musicbrainz_retry(download_id):
    """Retry a failed download"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT release_id, release_title, artist, method, download_query
            FROM managed_downloads
            WHERE id = ?
        """, (download_id,))
        
        row = cursor.fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Download not found"}), 404
        
        _, _, _, method, download_query = row
        
        # Reset status to queued and clear error
        cursor.execute("""
            UPDATE managed_downloads 
            SET status = 'queued', error_message = NULL, external_id = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (download_id,))
        conn.commit()
        conn.close()
        
        # Reinitiate download in background thread with fresh connection
        if method == "slskd":
            thread = threading.Thread(target=_initiate_slskd_download_bg, args=(download_id, download_query), daemon=True)
            thread.start()
        elif method == "qbittorrent":
            thread = threading.Thread(target=_initiate_qbit_download_bg, args=(download_id, download_query), daemon=True)
            thread.start()
        
        return jsonify({"success": True, "message": "Download retry initiated"})
        
    except Exception as e:
        logging.error(f"[MB_RETRY] Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/musicbrainz/download/<int:download_id>", methods=["DELETE"])
def api_musicbrainz_remove(download_id):
    """Remove a download from the list"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("""
            UPDATE managed_downloads 
            SET status = 'removed', updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (download_id,))
        
        conn.commit()
        conn.close()
        
        return jsonify({"success": True})
        
    except Exception as e:
        logging.error(f"[MB_REMOVE] Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/slskd/search-results/<int:download_id>", methods=["GET"])
def api_slskd_search_results(download_id):
    """Get Soulseek search results for a download awaiting user selection"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Verify download exists and is awaiting selection
        cursor.execute("""
            SELECT status, method FROM managed_downloads WHERE id = ?
        """, (download_id,))
        
        download = cursor.fetchone()
        if not download:
            conn.close()
            return jsonify({"error": "Download not found"}), 404
        
        status, method = download
        if status != "awaiting_selection" or method != "slskd":
            conn.close()
            return jsonify({"error": "Download is not awaiting Soulseek selection"}), 400
        
        # Get all search results
        cursor.execute("""
            SELECT id, username, filename, size, match_score
            FROM slskd_search_results
            WHERE download_id = ?
            ORDER BY match_score DESC
        """, (download_id,))
        
        results = cursor.fetchall()
        conn.close()
        
        search_results = []
        for row in results:
            search_results.append({
                "result_id": row[0],
                "username": row[1],
                "filename": row[2],
                "size": row[3],
                "match_score": row[4]
            })
        
        return jsonify({"results": search_results})
        
    except Exception as e:
        logging.error(f"[SLSKD_RESULTS] Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/slskd/download-file", methods=["POST"])
def api_slskd_download_file():
    """User selects a file from search results and initiates download"""
    try:
        data = request.json or {}
        download_id = data.get("download_id")
        result_id = data.get("result_id")
        
        if not download_id or not result_id:
            return jsonify({"error": "Missing download_id or result_id"}), 400
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Get the selected result
        cursor.execute("""
            SELECT username, filename, size FROM slskd_search_results WHERE id = ? AND download_id = ?
        """, (result_id, download_id))
        
        result = cursor.fetchone()
        if not result:
            conn.close()
            return jsonify({"error": "Result not found"}), 404
        
        username, filename, size = result
        
        # Mark this result as selected
        cursor.execute("""
            UPDATE slskd_search_results SET selected = 1 WHERE id = ?
        """, (result_id,))
        
        # Update download status
        cursor.execute("""
            UPDATE managed_downloads SET status = 'initiating_download', updated_at = CURRENT_TIMESTAMP WHERE id = ?
        """, (download_id,))
        
        conn.commit()
        conn.close()
        
        # Initiate the download in a background thread
        def perform_slskd_download():
            try:
                cfg = get_config()
                slskd_config = cfg.get("slskd", {})
                web_url = slskd_config.get("web_url", "http://localhost:5030")
                api_key = slskd_config.get("api_key", "")
                
                client = SlskdClient(web_url, api_key, enabled=True)
                success = client.download_file(username, filename, size)
                
                conn2 = get_db()
                cursor2 = conn2.cursor()
                
                if success:
                    cursor2.execute("""
                        UPDATE managed_downloads 
                        SET status = 'downloading', updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """, (download_id,))
                    logging.info(f"[SLSKD_DOWNLOAD] Started download: {filename} from {username}")
                else:
                    cursor2.execute("""
                        UPDATE managed_downloads 
                        SET status = 'error', error_message = 'Failed to start file download', updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """, (download_id,))
                    logging.error(f"[SLSKD_DOWNLOAD] Failed to download: {filename} from {username}")
                
                conn2.commit()
                conn2.close()
                
            except Exception as e:
                logging.error(f"[SLSKD_DOWNLOAD] Error: {e}")
                try:
                    conn2 = get_db()
                    cursor2 = conn2.cursor()
                    cursor2.execute("""
                        UPDATE managed_downloads 
                        SET status = 'error', error_message = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """, (str(e), download_id))
                    conn2.commit()
                    conn2.close()
                except:
                    pass
        
        thread = threading.Thread(target=perform_slskd_download, daemon=True)
        thread.start()
        
        return jsonify({"success": True, "message": f"Download initiated for {filename}"})
        
    except Exception as e:
        logging.error(f"[SLSKD_DOWNLOAD_FILE] Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/slskd/search-again/<int:download_id>", methods=["POST"])
def api_slskd_search_again(download_id):
    """Retry search for a failed Soulseek download with a new query"""
    try:
        data = request.json or {}
        new_query = data.get("query", "").strip()
        
        if not new_query:
            return jsonify({"error": "Query parameter required"}), 400
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Get the original download
        cursor.execute("""
            SELECT method FROM managed_downloads WHERE id = ?
        """, (download_id,))
        
        download = cursor.fetchone()
        if not download or download[0] != "slskd":
            conn.close()
            return jsonify({"error": "Download not found or not a Soulseek download"}), 404
        
        # Clear previous search results
        cursor.execute("""
            DELETE FROM slskd_search_results WHERE download_id = ?
        """, (download_id,))
        
        # Reset status and clear error
        cursor.execute("""
            UPDATE managed_downloads 
            SET status = 'queued', error_message = NULL, external_id = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (download_id,))
        
        conn.commit()
        conn.close()
        
        # Reinitiate search with new query
        thread = threading.Thread(target=_initiate_slskd_download_bg, args=(download_id, new_query), daemon=True)
        thread.start()
        
        return jsonify({"success": True, "message": "New search initiated"})
        
    except Exception as e:
        logging.error(f"[SLSKD_SEARCH_AGAIN] Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/qbittorrent/search", methods=["POST"])
def qbit_search():
    """Proxy endpoint for qBittorrent search API"""
    cfg = get_config()
    qbit_config = cfg.get("qbittorrent", {})
    
    if not qbit_config.get("enabled"):
        return jsonify({"error": "qBittorrent integration not enabled"}), 400
    
    query = request.json.get("query", "")
    if not query:
        return jsonify({"error": "Query parameter required"}), 400
    
    web_url = qbit_config.get("web_url", "http://localhost:8080")
    username = qbit_config.get("username", "")
    password = qbit_config.get("password", "")
    
    try:
        import requests as req
        
        # Login if credentials provided
        session = req.Session()
        if username and password:
            login_url = f"{web_url}/api/v2/auth/login"
            session.post(login_url, data={"username": username, "password": password})
        
        # Start search with music category and all plugins
        search_url = f"{web_url}/api/v2/search/start"
        resp = session.post(search_url, data={"pattern": query, "plugins": "all", "category": "music"})
        
        if resp.status_code != 200:
            return jsonify({"error": f"Search failed: {resp.status_code}"}), 500
        
        search_data = resp.json()
        search_id = search_data.get("id")
        
        if not search_id:
            return jsonify({"error": "No search ID returned"}), 500
        
        # Poll for results (max 15 seconds with longer wait time for plugins to respond)
        import time
        results = []
        for i in range(30):
            time.sleep(0.5)
            status_url = f"{web_url}/api/v2/search/status"
            status_resp = session.get(status_url, params={"id": search_id})
            
            if status_resp.status_code == 200:
                status_data = status_resp.json()
                if status_data and len(status_data) > 0:
                    search_status = status_data[0]
                    # Get results even if still searching (partial results)
                    results_url = f"{web_url}/api/v2/search/results"
                    results_resp = session.get(results_url, params={"id": search_id, "limit": 100})
                    if results_resp.status_code == 200:
                        data = results_resp.json()
                        results = data.get("results", [])
                    
                    # If search is stopped, we're done
                    if search_status.get("status") == "Stopped":
                        break
        
        # Stop search
        stop_url = f"{web_url}/api/v2/search/stop"
        session.post(stop_url, data={"id": search_id})
        
        # Sort results by most seeders first (descending)
        results_sorted = sorted(results, key=lambda x: int(x.get("nbSeeders", 0)), reverse=True)
        
        return jsonify({"results": results_sorted})
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/qbittorrent/add", methods=["POST"])
def qbit_add_torrent():
    """Proxy endpoint to add torrent to qBittorrent"""
    cfg = get_config()
    qbit_config = cfg.get("qbittorrent", {})
    
    if not qbit_config.get("enabled"):
        return jsonify({"error": "qBittorrent integration not enabled"}), 400
    
    torrent_url = request.json.get("url", "")
    if not torrent_url:
        return jsonify({"error": "URL parameter required"}), 400
    
    web_url = qbit_config.get("web_url", "http://localhost:8080")
    username = qbit_config.get("username", "")
    password = qbit_config.get("password", "")
    
    try:
        import requests as req
        
        session = req.Session()
        if username and password:
            login_url = f"{web_url}/api/v2/auth/login"
            session.post(login_url, data={"username": username, "password": password})
        
        # Add torrent with Music category
        add_url = f"{web_url}/api/v2/torrents/add"
        resp = session.post(add_url, data={"urls": torrent_url, "category": "Music"})
        
        if resp.status_code == 200:
            return jsonify({"success": True, "message": "Torrent added successfully to Music category"})
        else:
            return jsonify({"error": f"Failed to add torrent: {resp.status_code}"}), 500

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/qbittorrent/force-start", methods=["POST"])
def qbit_force_start():
    """Force-start or resume a stalled qBittorrent torrent"""
    cfg = get_config()
    qbit_config = cfg.get("qbittorrent", {})

    if not qbit_config.get("enabled"):
        return jsonify({"error": "qBittorrent integration not enabled"}), 400

    data = request.get_json(silent=True) or {}
    torrent_hash = data.get("hash", "").strip()
    if not torrent_hash:
        return jsonify({"error": "hash is required"}), 400

    web_url = qbit_config.get("web_url", "http://localhost:8080")
    username = qbit_config.get("username", "")
    password = qbit_config.get("password", "")

    try:
        import requests as req

        session = req.Session()
        login_url = f"{web_url}/api/v2/auth/login"
        login_resp = session.post(login_url, data={"username": username, "password": password}, timeout=10)

        if login_resp.text != "Ok.":
            return jsonify({"error": "Failed to login to qBittorrent"}), 500

        # Force start the torrent and resume if it was paused
        force_url = f"{web_url}/api/v2/torrents/setForceStart"
        force_resp = session.post(force_url, data={"hashes": torrent_hash, "value": "true"}, timeout=10)
        if force_resp.status_code != 200:
            return jsonify({"error": f"Failed to force start: {force_resp.status_code}"}), 500

        resume_url = f"{web_url}/api/v2/torrents/resume"
        session.post(resume_url, data={"hashes": torrent_hash}, timeout=10)

        return jsonify({"success": True})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/qbittorrent/stop", methods=["POST"])
def qbit_stop():
    """Pause/stop a qBittorrent torrent"""
    cfg = get_config()
    qbit_config = cfg.get("qbittorrent", {})

    if not qbit_config.get("enabled"):
        return jsonify({"error": "qBittorrent integration not enabled"}), 400

    data = request.get_json(silent=True) or {}
    torrent_hash = data.get("hash", "").strip()
    if not torrent_hash:
        return jsonify({"error": "hash is required"}), 400

    web_url = qbit_config.get("web_url", "http://localhost:8080")
    username = qbit_config.get("username", "")
    password = qbit_config.get("password", "")

    try:
        import requests as req

        session = req.Session()
        login_url = f"{web_url}/api/v2/auth/login"
        login_resp = session.post(login_url, data={"username": username, "password": password}, timeout=10)

        if login_resp.text != "Ok.":
            return jsonify({"error": "Failed to login to qBittorrent"}), 500

        # Pause the torrent
        pause_url = f"{web_url}/api/v2/torrents/pause"
        pause_resp = session.post(pause_url, data={"hashes": torrent_hash}, timeout=10)
        
        if pause_resp.status_code != 200:
            return jsonify({"error": f"Failed to pause: {pause_resp.status_code}"}), 500

        return jsonify({"success": True})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/metadata")
def api_metadata():
    """API endpoint for MP3 metadata lookup"""
    lookup_type = request.args.get("type", "track")
    identifier = request.args.get("id", "")
    
    metadata = {}
    
    try:
        if lookup_type == "track" and identifier:
            # Get track info from database
            track_info = get_track_metadata_from_db(identifier, DB_PATH)
            
            if track_info:
                # Try to find the file and read MP3 metadata
                artist = track_info.get("artist", "")
                album = track_info.get("album", "")
                title = track_info.get("title", "")
                stored_file_path = track_info.get("file_path", "")
                
                # Construct full path from stored path
                music_root = os.environ.get("MUSIC_ROOT", "/music")
                file_path = None
                
                # First try using stored file path from Navidrome (which provides absolute paths)
                if stored_file_path:
                    # Navidrome provides absolute paths, check if they already start with music_root
                    if stored_file_path.startswith(music_root):
                        # Already absolute path
                        full_path = stored_file_path
                    else:
                        # Relative path, join with music_root
                        full_path = os.path.join(music_root, stored_file_path)
                    
                    if os.path.exists(full_path):
                        file_path = full_path
                
                # Fallback to searching if stored path doesn't work
                if not file_path:
                    try:
                        # Use timeout to prevent hanging
                        file_path = find_track_file(artist, album, title, music_root, timeout_seconds=5)
                    except Exception as e:
                        # If file search fails, continue without file metadata
                        pass
                
                if file_path and os.path.exists(file_path):
                    try:
                        metadata = read_mp3_metadata(file_path)
                    except Exception as e:
                        # If MP3 read fails, use database info
                        metadata = {
                            "title": title,
                            "artist": artist,
                            "album": album,
                            "track_id": identifier,
                            "note": f"MP3 read error: {str(e)}"
                        }
                else:
                    # Return database info if file not found
                    metadata = {
                        "title": title,
                        "artist": artist,
                        "album": album,
                        "track_id": identifier,
                        "note": "MP3 file not found in /music; showing database info"
                    }
                
                # Add scoring metadata from DB
                if track_info.get("spotify_score"):
                    metadata["spotify_score"] = track_info.get("spotify_score")
                if track_info.get("lastfm_ratio"):
                    metadata["lastfm_ratio"] = track_info.get("lastfm_ratio")
                if track_info.get("final_score"):
                    metadata["final_score"] = track_info.get("final_score")
                if track_info.get("stars"):
                    metadata["stars"] = track_info.get("stars")
                if track_info.get("is_single"):
                    metadata["is_single"] = bool(track_info.get("is_single"))
                if track_info.get("single_confidence"):
                    metadata["single_confidence"] = track_info.get("single_confidence")
        
        elif lookup_type == "album" and identifier:
            # Album lookup: artist/album format
            parts = identifier.split("/")
            if len(parts) >= 2:
                artist = parts[0]
                album = "/".join(parts[1:])
                
                conn = get_db()
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT 
                        AVG(stars) as avg_stars,
                        COUNT(*) as track_count,
                        COALESCE(SUM(CASE WHEN is_single = 1 THEN 1 ELSE 0 END), 0) as singles_count,
                        MAX(last_scanned) as last_scanned
                    FROM tracks
                    WHERE artist = ? AND album = ?
                """, (artist, album))
                result = cursor.fetchone()
                conn.close()
                
                if result:
                    metadata = {
                        "album": album,
                        "artist": artist,
                        "tracks": result[1] or 0,
                        "average_rating": round(result[0], 2) if result[0] else 0,
                        "singles_detected": result[2] or 0,
                        "last_scanned": result[3] or "Never"
                    }
        
        elif lookup_type == "artist" and identifier:
            # Artist metadata
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT 
                    COUNT(DISTINCT album) as album_count,
                    COUNT(*) as track_count,
                    AVG(stars) as avg_stars,
                    SUM(CASE WHEN stars = 5 THEN 1 ELSE 0 END) as five_star_count
                FROM tracks
                WHERE artist = ?
            """, (identifier,))
            result = cursor.fetchone()
            conn.close()
            
            if result:
                genres = aggregate_genres_from_tracks(identifier, DB_PATH)
                metadata = {
                    "artist": identifier,
                    "albums": result[0] or 0,
                    "tracks": result[1] or 0,
                    "average_rating": round(result[2], 2) if result[2] else 0,
                    "five_star_tracks": result[3] or 0,
                    "genres": ", ".join(genres) if genres else "Not detected"
                }
    
    except Exception as e:
        metadata = {"error": str(e)}
    
    return jsonify(metadata)


def _album_art_placeholder_svg(size: int = 300) -> Response:
    """
    Generate an SVG placeholder for album art.
    
    Args:
        size: Width and height of the SVG in pixels (10-1000)
        
    Returns:
        Flask Response with SVG content
    """
    # Validate and sanitize size to prevent injection attacks
    try:
        size = int(size)
        size = max(10, min(1000, size))  # Clamp between 10 and 1000
    except (ValueError, TypeError):
        size = 300  # Default fallback
    
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" role="img" aria-label="No Album Art Available">
        <title>No Album Art Available</title>
        <rect fill="#2a2a2a" width="{size}" height="{size}"/>
        <text x="50%" y="50%" text-anchor="middle" dy=".3em" fill="#666" font-size="16">No Album Art</text>
    </svg>'''
    return Response(svg, mimetype='image/svg+xml')


def _fetch_album_art_from_musicbrainz(artist_name: str, album_name: str) -> bytes | None:
    """
    Fetch album art from MusicBrainz Cover Art Archive.
    
    Args:
        artist_name: Artist name
        album_name: Album name
        
    Returns:
        Image bytes if found, None otherwise
    """
    try:
        import requests
        
        # Try to get MBID from database
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT musicbrainz_album_mbid FROM tracks 
            WHERE artist = ? AND album = ? AND musicbrainz_album_mbid IS NOT NULL
            LIMIT 1
        """, (artist_name, album_name))
        result = cursor.fetchone()
        conn.close()
        
        album_mbid = result['musicbrainz_album_mbid'] if result else None
        log_debug(f"MusicBrainz: Database MBID check - {artist_name} - {album_name}: {album_mbid}")
        
        # If we don't have MBID, try to search for it
        if not album_mbid:
            try:
                search_url = "https://musicbrainz.org/ws/2/release-group"
                params = {
                    "query": f'release:"{album_name}" AND artist:"{artist_name}"',
                    "fmt": "json",
                    "limit": 1
                }
                headers = {"User-Agent": MUSICBRAINZ_USER_AGENT}
                resp = requests.get(search_url, params=params, headers=headers, timeout=3)
                resp.raise_for_status()
                data = resp.json()
                rgs = data.get("release-groups", [])
                if rgs:
                    album_mbid = rgs[0].get("id")
                    log_info(f"MusicBrainz: Found MBID via search - {artist_name} - {album_name}: {album_mbid}")
            except Exception as e:
                log_debug(f"MusicBrainz album search failed: {e}")
                return None
        
        if not album_mbid:
            log_debug(f"MusicBrainz: No MBID found for {artist_name} - {album_name}")
            return None
        
        # Fetch cover art from Cover Art Archive
        cover_url = f"https://coverartarchive.org/release-group/{album_mbid}/front-500"
        resp = requests.get(cover_url, timeout=3)
        if resp.status_code == 200:
            log_info(f"MusicBrainz: Successfully fetched cover art for {artist_name} - {album_name}")
            return resp.content
        else:
            log_debug(f"MusicBrainz: Cover Art Archive returned {resp.status_code} for {album_mbid}")
        
        return None
    except Exception as e:
        log_debug(f"Failed to fetch album art from MusicBrainz: {e}")
        return None


def _fetch_album_art_from_discogs(artist_name: str, album_name: str) -> bytes | None:
    """
    Fetch album art from Discogs as fallback.
    
    Args:
        artist_name: Artist name
        album_name: Album name
        
    Returns:
        Image bytes if found, None otherwise
    """
    try:
        import requests
        from api_clients.discogs import DiscogsClient
        
        config_data, _ = _read_yaml(CONFIG_PATH)
        discogs_config = config_data.get("api_integrations", {}).get("discogs", {})
        discogs_token = discogs_config.get("token", "") or os.environ.get("DISCOGS_TOKEN", "")
        
        if not discogs_token:
            log_debug(f"Discogs: No token configured")
            return None
            
        discogs = DiscogsClient(discogs_token)
        
        # Search for album
        search_url = f"https://api.discogs.com/database/search"
        params = {"q": f"{artist_name} {album_name}", "type": "release", "per_page": 1}
        res = discogs.session.get(search_url, headers=discogs.headers, params=params, timeout=5)
        
        if res.status_code == 200:
            results = res.json().get("results", [])
            if results:
                result = results[0]
                if result.get("cover_url"):
                    resp = requests.get(result["cover_url"], timeout=3)
                    if resp.status_code == 200:
                        log_info(f"Discogs: Successfully fetched cover art for {artist_name} - {album_name}")
                        return resp.content
            log_debug(f"Discogs: No results found for {artist_name} - {album_name}")
        else:
            log_debug(f"Discogs: API returned {res.status_code}")
    except Exception as e:
        log_debug(f"Failed to fetch album art from Discogs: {e}")
    
    return None


def _save_album_art_to_db(artist_name: str, album_name: str, image_data: bytes, source: str = "unknown", mime_type: str = "image/jpeg") -> bool:
    """
    Save album art image data to the local database.
    
    Args:
        artist_name: Artist name
        album_name: Album name
        image_data: Binary image data
        source: Source of the image (e.g., 'musicbrainz', 'spotify', 'navidrome')
        mime_type: MIME type of the image
        
    Returns:
        True if saved successfully, False otherwise
    """
    try:
        if not image_data or len(image_data) == 0:
            return False
            
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO album_art 
            (artist_name, album_name, image_data, image_mime_type, source, downloaded_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (artist_name, album_name, image_data, mime_type, source))
        conn.commit()
        conn.close()
        
        logging.debug(f"Saved album art to database for {artist_name} - {album_name} from {source}")
        return True
    except Exception as e:
        logging.debug(f"Failed to save album art to database: {e}")
        return False


def _fetch_album_art_from_itunes(artist_name: str, album_name: str) -> bytes | None:
    """
    Fetch album art from iTunes/Apple Music API.
    
    Args:
        artist_name: Artist name
        album_name: Album name
        
    Returns:
        Image bytes if found, None otherwise
    """
    try:
        import requests
        
        # Search iTunes API
        search_url = "https://itunes.apple.com/search"
        params = {
            "term": f"{artist_name} {album_name}",
            "entity": "album",
            "limit": 5
        }
        
        resp = requests.get(search_url, params=params, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        
        results = data.get("results", [])
        if not results:
            log_debug(f"iTunes: No results for {artist_name} - {album_name}")
            return None
        
        # Try to find the best match
        for result in results:
            result_artist = result.get("artistName", "").lower()
            result_album = result.get("collectionName", "").lower()
            
            # Simple matching - check if artist and album are in the result
            if artist_name.lower() in result_artist and album_name.lower() in result_album:
                artwork_url = result.get("artworkUrl100", "")
                if artwork_url:
                    # Replace 100x100 with higher resolution
                    artwork_url = artwork_url.replace("100x100", "600x600")
                    log_info(f"iTunes: Found match for {artist_name} - {album_name}")
                    
                    # Fetch the artwork
                    art_resp = requests.get(artwork_url, timeout=5)
                    if art_resp.status_code == 200:
                        log_info(f"iTunes: Successfully fetched cover art for {artist_name} - {album_name}")
                        return art_resp.content
        
        # If no exact match, try the first result if it exists
        if results:
            artwork_url = results[0].get("artworkUrl100", "")
            if artwork_url:
                artwork_url = artwork_url.replace("100x100", "600x600")
                log_debug(f"iTunes: Using first result for {artist_name} - {album_name}")
                art_resp = requests.get(artwork_url, timeout=5)
                if art_resp.status_code == 200:
                    log_info(f"iTunes: Successfully fetched cover art (first result) for {artist_name} - {album_name}")
                    return art_resp.content
        
        return None
    except Exception as e:
        log_debug(f"Failed to fetch album art from iTunes: {e}")
        return None



@app.route("/api/album-art-placeholder")
def album_art_placeholder():
    """Return a placeholder SVG for missing album art"""
    return _album_art_placeholder_svg(size=300)


@app.route("/api/album-art/<path:artist>/<path:album>")
def api_album_art(artist, album):
    """Get album art from local database, Navidrome, MusicBrainz, or Discogs"""
    try:
        from urllib.parse import unquote
        artist = unquote(artist)
        album = unquote(album)
        
        log_info(f"Album art request: {artist} - {album}")
        
        # 0. First, check local album_art table for stored images
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT image_data, image_mime_type FROM album_art 
                WHERE artist_name = ? AND album_name = ?
            """, (artist, album))
            result = cursor.fetchone()
            conn.close()
            
            if result and result[0]:
                image_data = result[0]
                mime_type = result[1] or 'image/jpeg'
                log_info(f"Album art found in local database for {artist} - {album}")
                return send_file(
                    io.BytesIO(image_data),
                    mimetype=mime_type
                )
        except Exception as e:
            log_debug(f"Error fetching local album art: {e}")
        
        # 1. Check if we have cover_art_url or spotify_album_art_url in database
        cover_art_url = None  # Initialize to ensure it's always defined
        try:
            conn = get_db()
            cursor = conn.cursor()
            
            # Try multiple strategies to find the album
            # Strategy 1: Try matching by album_artist first
            try:
                cursor.execute("""
                    SELECT cover_art_url, spotify_album_art_url FROM tracks 
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = ? AND album = ? 
                    LIMIT 1
                """, (artist, album))
                result = cursor.fetchone()
                if result:
                    # Prefer cover_art_url, fall back to spotify_album_art_url
                    cover_art_url = result[0] if result[0] else result[1]
            except:
                pass
            
            # Strategy 2: If not found, try matching by artist alone (for backwards compat)
            if not cover_art_url:
                try:
                    cursor.execute("""
                        SELECT cover_art_url, spotify_album_art_url FROM tracks 
                        WHERE artist = ? AND album = ? 
                        LIMIT 1
                    """, (artist, album))
                    result = cursor.fetchone()
                    if result:
                        cover_art_url = result[0] if result[0] else result[1]
                except:
                    pass
            
            conn.close()
            
            if cover_art_url:
                try:
                    log_debug(f"Attempting to fetch from database URL: {cover_art_url[:50]}...")
                    resp = requests.get(cover_art_url, timeout=5)
                    if resp.status_code == 200:
                        # Save to database for future access
                        _save_album_art_to_db(artist, album, resp.content, source="musicbrainz")
                        return send_file(
                            io.BytesIO(resp.content),
                            mimetype='image/jpeg'
                        )
                    else:
                        log_debug(f"Database URL returned {resp.status_code}")
                except Exception as e:
                    log_debug(f"Failed to fetch cover_art_url from database: {e}")
                    pass  # Fall through to other methods
        except Exception as e:
            log_debug(f"Error checking database for album art: {e}")
        
        # 1.5 If not found in database, try fetching from MusicBrainz and cache it for available albums
        if not cover_art_url:
            try:
                log_debug(f"Attempting to fetch MusicBrainz releases for: {artist} - {album}")
                mb_releases = _fetch_musicbrainz_releases(artist)
                
                # Find the matching release
                album_normalized = _normalize_release_title(album)
                for rg in mb_releases:
                    rg_normalized = _normalize_release_title(rg.get("title", ""))
                    if rg_normalized == album_normalized:
                        mb_cover_url = rg.get("cover_art_url", "")
                        if mb_cover_url:
                            log_debug(f"Found MusicBrainz cover art for {artist} - {album}: {mb_cover_url[:50]}...")
                            # Store the cover_art_url in the database for future use
                            try:
                                conn = get_db()
                                cursor = conn.cursor()
                                cursor.execute("""
                                    UPDATE tracks 
                                    SET cover_art_url = ?
                                    WHERE (COALESCE(NULLIF(album_artist, ''), artist) = ? OR artist = ?) 
                                    AND album = ?
                                """, (mb_cover_url, artist, artist, album))
                                conn.commit()
                                conn.close()
                                log_debug(f"Updated cover_art_url in database for {artist} - {album}")
                            except Exception as e:
                                log_debug(f"Failed to store MusicBrainz cover_art_url: {e}")
                            
                            cover_art_url = mb_cover_url
                            break
            except Exception as e:
                log_debug(f"Failed to fetch MusicBrainz releases for album art: {e}")
        
        # 2. If we found a cover_art_url, try to fetch and cache it
        if cover_art_url:
            try:
                log_debug(f"Attempting to fetch from cover_art_url: {cover_art_url[:50]}...")
                resp = requests.get(cover_art_url, timeout=5)
                if resp.status_code == 200:
                    # Save to database for future access
                    _save_album_art_to_db(artist, album, resp.content, source="musicbrainz")
                    return send_file(
                        io.BytesIO(resp.content),
                        mimetype='image/jpeg'
                    )
                else:
                    log_debug(f"cover_art_url returned {resp.status_code}")
            except Exception as e:
                log_debug(f"Failed to fetch from cover_art_url: {e}")
        
        # 3. Try to get from Navidrome (more robust search with artist name)
        try:
            log_debug(f"Attempting Navidrome fetch for: {artist} - {album}")
            cfg = get_config()
            nav_users = cfg.get("navidrome_users", [])
            if not nav_users:
                nav = cfg.get("navidrome", {}) or {}
                if nav.get("base_url"):
                    nav_users = [nav]
            
            if nav_users:
                nav = nav_users[0]  # Use first Navidrome user
                base_url = nav.get("base_url", "").rstrip("/")
                username = nav.get("user", "")
                password = nav.get("pass", "")
                
                if base_url:
                    session = create_retry_session(retries=2, backoff=0.2, status_forcelist=(429, 500, 502, 503, 504))
                    search_url = f"{base_url}/rest/search3.view"
                    
                    # Try multiple search strategies
                    search_queries = [
                        (artist, album),  # Both artist and album
                        (album, None)      # Album only
                    ]
                    
                    for search_artist, search_album in search_queries:
                        params = {
                            'u': username,
                            'p': password,
                            'c': 'sptnr',
                            'v': '1.12.0',
                            'f': 'json'
                        }
                        
                        if search_artist and search_album:
                            params['query'] = f"{search_artist} {search_album}"
                        else:
                            params['album'] = search_album or album
                        
                        try:
                            resp = session.get(search_url, params=params, timeout=5)
                            if resp.status_code == 200:
                                data = resp.json()
                                albums = data.get('subsonic-response', {}).get('searchResult3', {}).get('album', [])
                                if albums:
                                    log_debug(f"Navidrome: Found {len(albums)} album(s)")
                                    album_id = albums[0].get('id')
                                    if album_id:
                                        log_debug(f"Navidrome: Getting cover art for album ID {album_id}")
                                        # Get cover art
                                        cover_url = f"{base_url}/rest/getCoverArt.view"
                                        cover_params = {
                                            'u': username,
                                            'p': password,
                                            'c': 'sptnr',
                                            'id': album_id,
                                            'size': '300'
                                        }
                                        cover_resp = session.get(cover_url, params=cover_params, timeout=5)
                                        if cover_resp.status_code == 200:
                                            log_info(f"Successfully fetched album art from Navidrome")
                                            # Save to database for future access
                                            _save_album_art_to_db(artist, album, cover_resp.content, source="navidrome")
                                            return send_file(
                                                io.BytesIO(cover_resp.content),
                                                mimetype='image/jpeg'
                                            )
                                        else:
                                            log_debug(f"Navidrome getCoverArt returned {cover_resp.status_code}")
                                    break  # Found album, don't try again
                                else:
                                    log_debug(f"Navidrome: No albums found")
                            else:
                                log_debug(f"Navidrome search returned {resp.status_code}")
                        except Exception as e:
                            log_debug(f"Navidrome search attempt failed: {e}")
                            continue
            else:
                log_debug("Navidrome not configured")
        except Exception as e:
            log_debug(f"Navidrome cover art fetch failed: {e}")
        
        # 4. Try MusicBrainz (as fallback - not cached)
        log_debug(f"Attempting MusicBrainz fetch for: {artist} - {album}")
        art_bytes = _fetch_album_art_from_musicbrainz(artist, album)
        if art_bytes:
            log_info(f"Successfully fetched album art from MusicBrainz")
            _save_album_art_to_db(artist, album, art_bytes, source="musicbrainz")
            return send_file(
                io.BytesIO(art_bytes),
                mimetype='image/jpeg'
            )
        log_debug(f"MusicBrainz returned no art")
        
        # 5. Try iTunes/Apple Music
        log_debug(f"Attempting iTunes fetch for: {artist} - {album}")
        art_bytes = _fetch_album_art_from_itunes(artist, album)
        if art_bytes:
            log_info(f"Successfully fetched album art from iTunes")
            _save_album_art_to_db(artist, album, art_bytes, source="itunes")
            return send_file(
                io.BytesIO(art_bytes),
                mimetype='image/jpeg'
            )
        log_debug(f"iTunes returned no art")
        
        # 6. Fallback to Discogs
        log_debug(f"Attempting Discogs fetch for: {artist} - {album}")
        art_bytes = _fetch_album_art_from_discogs(artist, album)
        if art_bytes:
            log_info(f"Successfully fetched album art from Discogs")
            _save_album_art_to_db(artist, album, art_bytes, source="discogs")
            return send_file(
                io.BytesIO(art_bytes),
                mimetype='image/jpeg'
            )
        log_debug(f"Discogs returned no art")
        
        # 7. Try to extract from MP3 file
        try:
            log_debug(f"Attempting to extract album art from MP3 files for: {artist} - {album}")
            from helpers.metadata_reader import extract_album_art_from_mp3
            
            # Get a track file path from this album (try multiple strategies)
            conn = get_db()
            cursor = conn.cursor()
            
            file_path = None
            
            # Strategy 1: Try matching by album_artist
            try:
                cursor.execute("""
                    SELECT file_path FROM tracks 
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = ? AND album = ? 
                    AND file_path IS NOT NULL
                    LIMIT 1
                """, (artist, album))
                result = cursor.fetchone()
                if result:
                    file_path = row_get(result, 'file_path')
            except:
                pass
            
            # Strategy 2: Try matching by artist alone
            if not file_path:
                try:
                    cursor.execute("""
                        SELECT file_path FROM tracks 
                        WHERE artist = ? AND album = ? 
                        AND file_path IS NOT NULL
                        LIMIT 1
                    """, (artist, album))
                    result = cursor.fetchone()
                    if result:
                        file_path = row_get(result, 'file_path')
                except:
                    pass
            
            conn.close()
            
            if file_path:
                log_debug(f"Found file path: {file_path}")
                if os.path.exists(file_path):
                    art_data = extract_album_art_from_mp3(file_path)
                    if art_data:
                        log_info(f"Successfully extracted album art from MP3 file")
                        return send_file(
                            io.BytesIO(art_data),
                            mimetype='image/jpeg'
                        )
                    else:
                        log_debug(f"MP3 file has no embedded art")
                else:
                    log_debug(f"File path does not exist: {file_path}")
            else:
                log_debug("No file paths found for this album")
        except Exception as e:
            log_debug(f"Failed to extract album art from MP3: {e}")
        
        # 8. No art found - return placeholder SVG instead of 404
        log_info(f"No album art found from any source for: {artist} - {album}. Returning placeholder.")
        return _album_art_placeholder_svg()
    except Exception as e:
        log_unified(f"Error fetching album art for {artist} - {album}: {e}", level=logging.ERROR)
        # Return placeholder SVG instead of 404
        return _album_art_placeholder_svg()


@app.route("/api/album/tracklist")
def api_album_tracklist():
    """Get tracklist for an album - from local database first, then MusicBrainz as fallback"""
    artist = request.args.get("artist", "").strip()
    album = request.args.get("album", "").strip()
    mbid = request.args.get("mbid", "").strip()  # Optional MusicBrainz Release ID
    
    if not artist or not album:
        return jsonify({"error": "Artist and album parameters required"}), 400
    
    try:
        # FIRST: Try to get tracklist from local database
        log_debug(f"Checking local database for tracklist: {artist} - {album}")
        conn = get_db()
        cursor = conn.cursor()
        
        # Get tracks from the album in the database
        cursor.execute("""
            SELECT title, track_number, duration, artist FROM tracks 
            WHERE artist = ? AND album = ? 
            ORDER BY track_number ASC, title ASC
        """, (artist, album))
        
        db_tracks = cursor.fetchall()
        conn.close()
        
        if db_tracks:
            # Found tracks in local database
            log_info(f"Found {len(db_tracks)} tracks in local database for {artist} - {album}")
            tracklist = []
            for track in db_tracks:
                tracklist.append({
                    "position": str(track['track_number'] or '').strip() or '—',
                    "title": track['title'],
                    "artist": track['artist'] or ''
                })
            
            return jsonify({
                "success": True,
                "artist": artist,
                "album": album,
                "tracklist": tracklist,
                "source": "database"
            })
        
        # FALLBACK: Search MusicBrainz if not found in database
        log_debug(f"No database tracks found, falling back to MusicBrainz for {artist} - {album}")
        import requests
        headers = {"User-Agent": MUSICBRAINZ_USER_AGENT}
        
        # If MBID is provided, use it directly instead of searching
        if mbid:
            log_debug(f"Fetching tracklist for {artist} - {album} using provided MBID: {mbid}")
            
            # Fetch the release directly using the provided MBID
            release_url = f"https://musicbrainz.org/ws/2/releases/{mbid}"
            release_params = {"fmt": "json", "inc": "recordings"}
            
            try:
                release_resp = requests.get(release_url, params=release_params, headers=headers, timeout=5)
                release_resp.raise_for_status()
                release_data = release_resp.json()
                
                # Extract tracklist from the release
                tracklist = []
                media = release_data.get("media", [])
                if media:
                    for track_obj in media[0].get("tracks", []):
                        recording = track_obj.get("recording", {})
                        tracklist.append({
                            "position": track_obj.get("position", ""),
                            "title": recording.get("title", "Unknown"),
                            "artist": " feat. ".join([a.get("name", "") for a in recording.get("artist-credit", []) if a.get("name")])
                        })
                
                if tracklist:
                    log_info(f"Found {len(tracklist)} tracks for {artist} - {album} (Release ID: {mbid})")
                    return jsonify({
                        "success": True,
                        "artist": artist,
                        "album": album,
                        "tracklist": tracklist,
                        "release_id": mbid
                    })
                else:
                    log_debug(f"Release {mbid} has no tracklist, falling back to search...")
                    # Don't return 404 - fall through to search method below
            except Exception as e:
                log_debug(f"Error fetching release {mbid}: {e}. Falling back to search...")
                # Fall through to search method
        
        # Fallback: Search MusicBrainz if no MBID provided or MBID lookup failed
        log_debug(f"Fetching tracklist for {artist} - {album} via search")
        
        # Try searching for releases directly
        search_url = "https://musicbrainz.org/ws/2/release"
        params = {
            "query": f'release:"{album}" AND artist:"{artist}"',
            "fmt": "json",
            "limit": 5  # Get multiple results in case first isn't right
        }
        
        resp = requests.get(search_url, params=params, headers=headers, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        
        releases = data.get("releases", [])
        
        # If no releases found with direct search, try release-group search
        if not releases:
            log_debug(f"No direct releases found for {artist} - {album}, trying release-group search")
            search_url = "https://musicbrainz.org/ws/2/release-group"
            params = {
                "query": f'"{album}" AND artist:"{artist}"',
                "fmt": "json",
                "limit": 1
            }
            
            resp = requests.get(search_url, params=params, headers=headers, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            
            release_groups = data.get("release-groups", [])
            if not release_groups:
                log_debug(f"No MusicBrainz results for {artist} - {album}")
                return jsonify({"error": "Album not found on MusicBrainz"}), 404
            
            rg = release_groups[0]
            rg_id = rg.get("id")
            
            if not rg_id:
                return jsonify({"error": "Cannot get release ID"}), 400
            
            # Fetch releases for this release group
            releases_url = f"https://musicbrainz.org/ws/2/release-group/{rg_id}/releases"
            releases_params = {"fmt": "json", "limit": 5}
            
            releases_resp = requests.get(releases_url, params=releases_params, headers=headers, timeout=5)
            releases_resp.raise_for_status()
            releases_data = releases_resp.json()
            
            releases = releases_data.get("releases", [])
            if not releases:
                log_debug(f"No releases found for release group {rg_id}")
                return jsonify({"error": "No releases found for this release group"}), 404
        
        if not releases:
            return jsonify({"error": "No releases found"}), 404
        
        # Get first release with media/tracks
        tracklist = []
        release_id = None
        for release in releases:
            media = release.get("media", [])
            if media:
                release_id = release.get("id", "")
                for track_obj in media[0].get("tracks", []):
                    recording = track_obj.get("recording", {})
                    tracklist.append({
                        "position": track_obj.get("position", ""),
                        "title": recording.get("title", "Unknown"),
                        "artist": " feat. ".join([a.get("name", "") for a in recording.get("artist-credit", []) if a.get("name")])
                    })
                break
        
        if not tracklist:
            return jsonify({"error": "No tracks found"}), 404
        
        log_info(f"Found {len(tracklist)} tracks for {artist} - {album}")
        return jsonify({
            "success": True,
            "artist": artist,
            "album": album,
            "tracklist": tracklist,
            "release_id": release_id or ""
        })
    
    except Exception as e:
        log_debug(f"Error fetching tracklist: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/album/tracklist/match")
def api_album_tracklist_match():
    """Check which tracks from an album already exist in the library"""
    artist = request.args.get("artist", "").strip()
    album = request.args.get("album", "").strip()
    
    if not artist or not album:
        return jsonify({"error": "Artist and album parameters required"}), 400
    
    try:
        log_debug(f"Matching tracklist for {artist} - {album}")
        conn = get_db()
        cursor = conn.cursor()
        
        # Get all tracks for this album from the database
        cursor.execute("""
            SELECT title FROM tracks WHERE artist = ? AND album = ?
            ORDER BY track_number ASC, title ASC
        """, (artist, album))
        
        album_tracks = [row['title'] for row in cursor.fetchall()]
        conn.close()
        
        if album_tracks:
            # All tracks in the album are already matched (they're in the database)
            log_info(f"Found {len(album_tracks)} existing album tracks for {artist} - {album}")
            matched_tracks = [{"title": t} for t in album_tracks]
            
            return jsonify({
                "success": True,
                "matched": matched_tracks,
                "unmatched": []
            })
        
        # If no tracks found in database, fall back to checking all artist tracks
        log_debug(f"No album tracks found in database, checking all tracks for artist {artist}")
        cursor = conn.cursor()
        cursor.execute("""
            SELECT title FROM tracks WHERE artist = ?
        """, (artist,))
        
        library_tracks = {row['title'].lower(): True for row in cursor.fetchall()}
        conn.close()
        
        # Fetch tracklist from MusicBrainz to match against
        import requests
        headers = {"User-Agent": MUSICBRAINZ_USER_AGENT}
        search_url = "https://musicbrainz.org/ws/2/release"
        params = {
            "query": f'release:"{album}" AND artist:"{artist}"',
            "fmt": "json",
            "limit": 5
        }
        
        resp = requests.get(search_url, params=params, headers=headers, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        
        releases = data.get("releases", [])
        
        # If no releases found, try release-group search
        if not releases:
            search_url = "https://musicbrainz.org/ws/2/release-group"
            params = {
                "query": f'"{album}" AND artist:"{artist}"',
                "fmt": "json",
                "limit": 1
            }
            
            resp = requests.get(search_url, params=params, headers=headers, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            
            release_groups = data.get("release-groups", [])
            if not release_groups:
                return jsonify({"matched": [], "unmatched": []})
            
            rg = release_groups[0]
            rg_id = rg.get("id")
            
            if not rg_id:
                return jsonify({"matched": [], "unmatched": []})
            
            releases_url = f"https://musicbrainz.org/ws/2/release-group/{rg_id}/releases"
            releases_params = {"fmt": "json", "limit": 5}
            
            releases_resp = requests.get(releases_url, params=releases_params, headers=headers, timeout=5)
            releases_resp.raise_for_status()
            releases_data = releases_resp.json()
            
            releases = releases_data.get("releases", [])
        
        matched_tracks = []
        unmatched_tracks = []
        
        if releases:
            media = releases[0].get("media", [])
            if media:
                for track_obj in media[0].get("tracks", []):
                    recording = track_obj.get("recording", {})
                    track_title = recording.get("title", "").lower().strip()
                    
                    if track_title in library_tracks:
                        matched_tracks.append({
                            "title": recording.get("title", "")
                        })
                    else:
                        unmatched_tracks.append({
                            "title": recording.get("title", "")
                        })
        
        log_info(f"Matched {len(matched_tracks)} tracks for {artist} - {album}")
        return jsonify({
            "success": True,
            "matched": matched_tracks,
            "unmatched": unmatched_tracks
        })
    
    except Exception as e:
        log_debug(f"Error matching tracklist: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/downloads/scan")
def api_downloads_scan():
    """Scan downloads folder and return pending files (both completed and incomplete)"""
    try:
        # Check for failed downloads first and remove them
        from download_queue_manager import check_and_remove_failed_downloads, cleanup_missing_files
        failed_stats = check_and_remove_failed_downloads()
        if failed_stats.get("failed_detected"):
            log_info(f"Failed downloads check: {failed_stats}")
        
        # Clean up queue items for missing files
        cleanup_stats = cleanup_missing_files()
        if cleanup_stats.get("removed"):
            log_info(f"Queue cleanup: Removed {cleanup_stats['removed']} items with missing files")
        
        cfg = get_config()
        downloads_config = cfg.get("downloads", {})
        downloads_dir = downloads_config.get("folder", os.environ.get("DOWNLOADS_DIR", "/downloads"))
        incomplete_dir = downloads_config.get("incomplete_folder", "/downloads/Soulseek/Incomplete")
        monitor_incomplete = downloads_config.get("monitor_incomplete", True)
        
        files = []
        
        # Scan completed downloads
        if os.path.exists(downloads_dir):
            for filename in os.listdir(downloads_dir):
                if not filename.lower().endswith('.mp3'):
                    continue
                
                file_path = os.path.join(downloads_dir, filename)
                if not os.path.isfile(file_path):
                    continue
                
                try:
                    metadata = read_mp3_metadata(file_path)
                    files.append({
                        'filename': filename,
                        'path': file_path,
                        'size': os.path.getsize(file_path),
                        'artist': metadata.get('artist', 'Unknown'),
                        'album': metadata.get('album', 'Unknown'),
                        'title': metadata.get('title', filename),
                        'year': metadata.get('year', metadata.get('date', '')),
                        'track': metadata.get('track', ''),
                        'genre': metadata.get('genre', ''),
                        'duration': metadata.get('duration', 0),
                        'status': 'completed'
                    })
                except Exception as e:
                    files.append({
                        'filename': filename,
                        'path': file_path,
                        'size': os.path.getsize(file_path),
                        'error': str(e),
                        'status': 'completed'
                    })
        
        # Scan incomplete downloads if enabled
        if monitor_incomplete and os.path.exists(incomplete_dir):
            for foldername in os.listdir(incomplete_dir):
                folder_path = os.path.join(incomplete_dir, foldername)
                if not os.path.isdir(folder_path):
                    continue
                
                # Look for .mp3 files in the incomplete folder
                for filename in os.listdir(folder_path):
                    if not filename.lower().endswith('.mp3'):
                        continue
                    
                    file_path = os.path.join(folder_path, filename)
                    if not os.path.isfile(file_path):
                        continue
                    
                    try:
                        metadata = read_mp3_metadata(file_path)
                        files.append({
                            'filename': filename,
                            'path': file_path,
                            'size': os.path.getsize(file_path),
                            'artist': metadata.get('artist', 'Unknown'),
                            'album': metadata.get('album', 'Unknown'),
                            'title': metadata.get('title', filename),
                            'year': metadata.get('year', metadata.get('date', '')),
                            'track': metadata.get('track', ''),
                            'genre': metadata.get('genre', ''),
                            'duration': metadata.get('duration', 0),
                            'status': 'incomplete',
                            'parent_folder': foldername
                        })
                    except Exception as e:
                        files.append({
                            'filename': filename,
                            'path': file_path,
                            'size': os.path.getsize(file_path),
                            'error': str(e),
                            'status': 'incomplete',
                            'parent_folder': foldername
                        })
        
        return jsonify({
            "count": len(files),
            "files": files
        })
    except Exception as e:
        return jsonify({"error": str(e), "files": []}), 400


@app.route("/api/downloads/discover", methods=["POST"])
def api_downloads_discover():
    """
    Auto-discover audio files in /downloads folder and add them to download_queue.
    Makes manually added files appear in Download Monitor UI for user review.
    
    Returns:
        JSON with statistics: scanned, queued, already_in_queue, already_in_library, errors
    """
    try:
        from download_queue_manager import auto_discover_and_queue_files
        
        stats = auto_discover_and_queue_files()
        
        return jsonify({
            "success": True,
            "stats": stats,
            "message": f"Discovered {stats['queued']} new files, "
                      f"{stats['already_in_queue']} already queued, "
                      f"{stats['already_in_library']} in library"
        })
    except Exception as e:
        print(f"[ERROR] Error discovering files: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route("/api/downloads/process-albums", methods=["POST"])
def api_downloads_process_albums():
    """
    Check discovered albums and auto-process complete ones.
    Complete albums that don't exist in library are moved to /music.
    Duplicate albums are marked as 'possible_duplicate' for manual review.
    
    Returns:
        JSON with statistics: checked, processed, duplicates_found, errors
    """
    try:
        from download_queue_manager import process_complete_albums
        
        stats = process_complete_albums()
        
        return jsonify({
            "success": True,
            "stats": stats,
            "message": f"Checked {stats['checked']} albums. "
                      f"{stats.get('processed', 0)} auto-processed, "
                      f"{stats.get('pending_review', 0)} pending manual match review, "
                      f"{stats.get('duplicates_found', 0)} duplicates found"
        })
    except Exception as e:
        print(f"[ERROR] Error processing albums: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route("/api/downloads/albums/use-existing", methods=["POST"])
def api_downloads_use_existing_metadata():
    """Manually process a pending-match album using existing file metadata."""
    try:
        from download_queue_manager import process_album_with_existing_metadata

        data = request.get_json() or {}
        artist = (data.get("artist") or "").strip()
        album = (data.get("album") or "").strip()

        if not artist or not album:
            return jsonify({"success": False, "error": "artist and album are required"}), 400

        result = process_album_with_existing_metadata(album, artist)
        status_code = 200 if result.get("success") else 400
        return jsonify(result), status_code
    except Exception as e:
        logging.error(f"Error processing album with existing metadata: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/downloads/albums/apply-match", methods=["POST"])
def api_downloads_apply_musicbrainz_match():
    """Apply selected MusicBrainz candidate and process the album."""
    try:
        from download_queue_manager import apply_musicbrainz_match_and_process

        data = request.get_json() or {}
        artist = (data.get("artist") or "").strip()
        album = (data.get("album") or "").strip()
        release_group_id = (data.get("release_group_id") or "").strip()

        if not artist or not album or not release_group_id:
            return jsonify({"success": False, "error": "artist, album, and release_group_id are required"}), 400

        result = apply_musicbrainz_match_and_process(album, artist, release_group_id)
        status_code = 200 if result.get("success") else 400
        return jsonify(result), status_code
    except Exception as e:
        logging.error(f"Error applying MusicBrainz match: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/downloads/process", methods=["POST"])
def api_downloads_process():
    """Process downloads folder - organize and move files to /Music"""
    try:
        from downloads_watcher import scan_downloads_folder
        
        results = scan_downloads_folder()
        
        successful = [r for r in results if r['status'] == 'success']
        failed = [r for r in results if r['status'] == 'error']
        
        return jsonify({
            "total": len(results),
            "successful": len(successful),
            "failed": len(failed),
            "results": results
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/downloads/process-one", methods=["POST"])
def api_downloads_process_one():
    """Process a single file from downloads folder"""
    try:
        data = request.get_json()
        file_path = data.get('path', '')
        
        if not file_path or not os.path.exists(file_path):
            return jsonify({"error": "File not found"}), 400
        
        from downloads_watcher import extract_mp3_metadata, organize_file, add_to_database, track_exists_in_library, queue_incomplete_download, mark_download_exists_in_library
        
        # Extract metadata
        metadata = extract_mp3_metadata(file_path)
        
        # Check if already exists in library
        artist = metadata.get('artist', 'Unknown')
        album = metadata.get('album', 'Unknown')
        title = metadata.get('title', os.path.basename(file_path))
        
        if track_exists_in_library(artist, album, title):
            mark_download_exists_in_library(file_path)
            return jsonify({
                "success": False,
                "exists_in_library": True,
                "error": "This track already exists in your library"
            }), 400
        
        # Organize file
        file_info = organize_file(file_path, metadata)
        
        if file_info.get('success'):
            # Add to database
            add_to_database(file_info, metadata)
            return jsonify({
                "success": True,
                "artist": file_info.get('artist'),
                "album": file_info.get('album'),
                "title": file_info.get('title'),
                "target_path": file_info.get('target_path')
            })
        else:
            # Queue for retry if processing failed
            queue_incomplete_download(file_path, metadata)
            return jsonify({
                "success": False,
                "queued_for_retry": True,
                "error": file_info.get('error', 'Unknown error')
            }), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/downloads/queue", methods=["GET"])
def api_downloads_get_queue():
    """Get files in download queue"""
    try:
        from downloads_watcher import get_download_queue
        
        status = request.args.get('status')
        limit = int(request.args.get('limit', 50))
        
        queue = get_download_queue(status=status, limit=limit)
        
        return jsonify({
            "count": len(queue),
            "queue": queue
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/downloads/retry-queue", methods=["GET"])
def api_downloads_get_retry_queue():
    """Get files queued for retry (ready to retry now)"""
    try:
        from downloads_watcher import get_retry_queue
        
        limit = int(request.args.get('limit', 50))
        queue = get_retry_queue(limit=limit)
        
        return jsonify({
            "count": len(queue),
            "queue": queue
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/downloads/queue/grouped", methods=["GET"])
def api_downloads_get_queue_grouped():
    """Get download queue grouped by album for smart display"""
    try:
        from downloads_watcher import get_download_queue_grouped
        
        status = request.args.get('status')
        limit = int(request.args.get('limit', 50))
        
        groups = get_download_queue_grouped(status=status, limit=limit)
        
        return jsonify({
            "count": len(groups),
            "groups": groups,
            "total_tracks": sum(g['track_count'] for g in groups)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/downloads/queue/batch-group", methods=["POST"])
def api_downloads_batch_group():
    """Group selected queue items into a new album/playlist"""
    try:
        data = request.get_json()
        item_ids = data.get('item_ids', [])
        group_type = data.get('group_type', 'album')
        group_name = data.get('group_name', '')
        group_artist = data.get('group_artist', '')
        
        if not item_ids or not group_name:
            return jsonify({"error": "item_ids and group_name are required"}), 400
        
        if not isinstance(item_ids, list):
            return jsonify({"error": "item_ids must be an array"}), 400
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Update all items in this batch to have the new group info
        updated_count = 0
        for item_id in item_ids:
            try:
                # Get current item
                cursor.execute(
                    "SELECT id, artist, title FROM download_queue WHERE id = ?",
                    (item_id,)
                )
                item = cursor.fetchone()
                
                if not item:
                    continue
                
                # Update with new group name
                # If group_artist is provided, also update the artist for grouping consistency
                if group_artist:
                    cursor.execute(
                        """UPDATE download_queue 
                           SET album = ?, artist = ?
                           WHERE id = ?""",
                        (group_name, group_artist, item_id)
                    )
                else:
                    cursor.execute(
                        """UPDATE download_queue 
                           SET album = ?
                           WHERE id = ?""",
                        (group_name, item_id)
                    )
                updated_count += 1
            except Exception as e:
                logging.warning(f"Failed to update queue item {item_id}: {e}")
                continue
        
        conn.commit()
        conn.close()
        
        return jsonify({
            "success": True,
            "updated": updated_count,
            "total": len(item_ids),
            "message": f"Grouped {updated_count} items into '{group_name}'"
        })
    
    except Exception as e:
        logging.error(f"Error batch grouping queue items: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/downloads/queue/<int:queue_id>", methods=["POST"])
def api_downloads_manage_queue_item(queue_id):
    """Manage a queue item (mark as failed, successful, or delete)"""
    try:
        from downloads_watcher import mark_download_as_failed, mark_download_as_successful
        conn = get_db()
        cursor = conn.cursor()
        
        data = request.get_json()
        action = data.get('action', 'delete')  # delete, retry, fail
        
        cursor.execute("SELECT file_path FROM download_queue WHERE id = ?", (queue_id,))
        row = cursor.fetchone()
        
        if not row:
            return jsonify({"error": "Queue item not found"}), 404
        
        file_path = row[0]
        
        if action == 'delete':
            cursor.execute("DELETE FROM download_queue WHERE id = ?", (queue_id,))
        elif action == 'successful':
            mark_download_as_successful(file_path)
        elif action == 'fail':
            failure_reason = data.get('reason', 'Manual mark as failed')
            mark_download_as_failed(file_path, failure_reason)
        
        conn.commit()
        conn.close()
        
        return jsonify({"success": True, "action": action})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/downloads/process-retry", methods=["POST"])
def api_downloads_process_retry():
    """Process files from retry queue"""
    try:
        from downloads_watcher import extract_mp3_metadata, organize_file, add_to_database, get_retry_queue, mark_download_as_successful, mark_download_as_failed, track_exists_in_library
        
        queue = get_retry_queue(limit=100)
        results = {
            "total": len(queue),
            "processed": 0,
            "successful": 0,
            "failed": 0,
            "exists_in_library": 0,
            "results": []
        }
        
        for item in queue:
            file_path = item['file_path']
            
            if not os.path.exists(file_path):
                results["results"].append({
                    "file_path": file_path,
                    "status": "error",
                    "reason": "File no longer exists"
                })
                continue
            
            try:
                metadata = extract_mp3_metadata(file_path)
                artist = metadata.get('artist', 'Unknown')
                album = metadata.get('album', 'Unknown')
                title = metadata.get('title', os.path.basename(file_path))
                
                # Check if now exists in library
                if track_exists_in_library(artist, album, title):
                    results["exists_in_library"] += 1
                    results["results"].append({
                        "file_path": file_path,
                        "status": "exists_in_library",
                        "artist": artist,
                        "album": album,
                        "title": title
                    })
                    continue
                
                # Try to organize
                file_info = organize_file(file_path, metadata)
                
                if file_info.get('success'):
                    add_to_database(file_info, metadata)
                    mark_download_as_successful(file_path)
                    results["successful"] += 1
                    results["results"].append({
                        "file_path": file_path,
                        "status": "success",
                        "target_path": file_info.get('target_path')
                    })
                else:
                    # Retry again
                    mark_download_as_failed(file_path, file_info.get('error', 'Unknown error'))
                    results["failed"] += 1
                    results["results"].append({
                        "file_path": file_path,
                        "status": "retry_scheduled",
                        "reason": file_info.get('error', 'Unknown error')
                    })
                
                results["processed"] += 1
            except Exception as e:
                mark_download_as_failed(file_path, str(e))
                results["failed"] += 1
                results["results"].append({
                    "file_path": file_path,
                    "status": "error",
                    "reason": str(e)
                })
        
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/downloads/scheduler/start", methods=["POST"])
def api_downloads_scheduler_start():
    """Start the download retry scheduler"""
    try:
        global retry_scheduler
        from download_retry_manager import run_retry_manager
        import time as time_module
        
        with retry_scheduler_lock:
            # Check if already running
            if retry_scheduler.get("running"):
                return jsonify({
                    "success": False,
                    "error": "Scheduler is already running"
                }), 400
            
            # Check if thread is still alive
            thread = retry_scheduler.get("thread")
            if thread and hasattr(thread, 'is_alive') and thread.is_alive():
                retry_scheduler["running"] = True
                return jsonify({
                    "success": True,
                    "message": "Scheduler thread already running"
                })
            
            # Create stop event for the new thread
            retry_scheduler["stop_event"] = threading.Event()
            
            def retry_scheduler_worker():
                """Worker function for retry scheduler thread"""
                try:
                    cfg = get_config()
                    navidrome_config = cfg.get("navidrome", {})
                    navidrome_url = navidrome_config.get("url", "http://localhost:4533")
                    navidrome_token = navidrome_config.get("token", "")
                    
                    # Get scheduler config
                    scheduler_config = cfg.get("features", {}).get("retry_scheduler", {})
                    interval = scheduler_config.get("interval_seconds", 60)
                    
                    logging.info(f"[RETRY_SCHEDULER] Started with interval: {interval}s")
                    
                    while not retry_scheduler["stop_event"].is_set():
                        try:
                            stats = run_retry_manager(DB_PATH, navidrome_url, navidrome_token)
                            if stats["retried"] > 0 or stats["completed"] > 0 or stats["failed"] > 0:
                                logging.info(f"[RETRY_SCHEDULER] Retried: {stats['retried']}, Completed: {stats['completed']}, Failed: {stats['failed']}")
                        except Exception as e:
                            logging.error(f"[RETRY_SCHEDULER] Error: {e}")
                        
                        # Wait with stop event check
                        if retry_scheduler["stop_event"].wait(timeout=interval):
                            # Stop event was set
                            break
                    
                    logging.info("[RETRY_SCHEDULER] Stopped")
                except Exception as e:
                    logging.error(f"[RETRY_SCHEDULER] Worker error: {e}")
                finally:
                    with retry_scheduler_lock:
                        retry_scheduler["running"] = False
            
            # Start the thread
            thread = threading.Thread(target=retry_scheduler_worker, daemon=True)
            thread.start()
            retry_scheduler["thread"] = thread
            retry_scheduler["running"] = True
            
            return jsonify({
                "success": True,
                "message": "Retry scheduler started"
            })
    except Exception as e:
        logging.error(f"Error starting retry scheduler: {e}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route("/api/downloads/scheduler/stop", methods=["POST"])
def api_downloads_scheduler_stop():
    """Stop the download retry scheduler"""
    try:
        global retry_scheduler
        
        with retry_scheduler_lock:
            if not retry_scheduler.get("running"):
                return jsonify({
                    "success": False,
                    "error": "Scheduler is not running"
                }), 400
            
            # Signal the thread to stop
            stop_event = retry_scheduler.get("stop_event")
            if stop_event:
                stop_event.set()
            
            # Give thread time to stop gracefully
            thread = retry_scheduler.get("thread")
            if thread and hasattr(thread, 'is_alive') and thread.is_alive():
                time.sleep(2)  # Give thread 2 seconds to stop
            
            retry_scheduler["running"] = False
            
            return jsonify({
                "success": True,
                "message": "Retry scheduler stopped"
            })
    except Exception as e:
        logging.error(f"Error stopping retry scheduler: {e}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route("/api/downloads/scheduler/status", methods=["GET"])
def api_downloads_scheduler_status():
    """Get retry scheduler status"""
    try:
        global retry_scheduler
        
        with retry_scheduler_lock:
            thread = retry_scheduler.get("thread")
            is_alive = False
            if thread and hasattr(thread, 'is_alive'):
                is_alive = thread.is_alive()
            
            return jsonify({
                "running": retry_scheduler.get("running", False),
                "thread_alive": is_alive,
                "status": "running" if retry_scheduler.get("running") else "stopped"
            })
    except Exception as e:
        logging.error(f"Error getting retry scheduler status: {e}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# ===== Download Queue Management (New Dynamic Queue System) =====

@app.route("/api/queue/add", methods=["POST"])
def api_queue_add():
    """Add song/album to download queue"""
    try:
        from download_queue_manager import add_to_queue, check_downloads_folder
        
        data = request.get_json()
        if not data:
            logging.warning(f"Queue add called with no JSON data")
            return jsonify({"error": "No data provided"}), 400
        
        # Log received data for debugging
        logging.debug(f"Queue add request data: {data}")
            
        artist = data.get('artist', '').strip() if data.get('artist') else ''
        title = data.get('title', '').strip() if data.get('title') else ''
        album = data.get('album', '').strip() if data.get('album') else None
        source = data.get('source', 'soulseek')  # 'soulseek' or 'qbittorrent'
        
        # Handle priority parsing
        try:
            priority = int(data.get('priority', 5))
        except (ValueError, TypeError):
            priority = 5
        
        if not artist or not title:
            logging.warning(f"Queue add missing required fields: artist='{artist}', title='{title}'")
            return jsonify({"error": "Artist and title are required"}), 400
        
        logging.info(f"Adding to queue: {artist} - {title} (album: {album}, source: {source}, priority: {priority})")
        
        # Add to queue
        try:
            item = add_to_queue(artist, title, album, source, priority)
        except Exception as e:
            logging.error(f"Error in add_to_queue: {type(e).__name__}: {e}")
            import traceback
            logging.error(traceback.format_exc())
            return jsonify({"error": f"Failed to add to queue: {str(e)}"}), 500
        
        if item:
            return jsonify({
                "success": True,
                "queue_id": item.get('id') if isinstance(item, dict) else None,
                "message": f"Added to queue: {artist} - {title}",
                "item": item
            })
        else:
            error_msg = "Failed to add to queue - check server logs for details"
            logging.error(f"add_to_queue returned None for: {artist} - {title}")
            return jsonify({"error": error_msg}), 500
            
    except Exception as e:
        logging.error(f"Unexpected error adding to queue: {type(e).__name__}: {e}")
        import traceback
        logging.error(traceback.format_exc())
        return jsonify({"error": f"Server error: {type(e).__name__}"}), 500


@app.route("/api/queue/add-batch", methods=["POST"])
def api_queue_add_batch():
    """Add multiple songs/albums to download queue in a single request"""
    try:
        from download_queue_manager import add_to_queue
        import uuid
        
        data = request.get_json()
        if not data or 'items' not in data:
            logging.warning(f"Batch queue add called with no items")
            return jsonify({"error": "No items provided"}), 400
        
        items = data.get('items', [])
        if not isinstance(items, list):
            return jsonify({"error": "items must be an array"}), 400
        
        logging.info(f"Adding {len(items)} items to queue in batch")
        
        # Use provided import_group_id, or generate one based on artist+album if available
        # This allows downloads from the same album to be grouped together for batch organization
        import_group_id = data.get('import_group')
        
        if not import_group_id and items:
            # Generate group ID from artist + album of first item
            first_item = items[0]
            artist = first_item.get('artist', '').strip() if first_item.get('artist') else 'Unknown'
            album = first_item.get('album', '').strip() if first_item.get('album') else None
            
            if album:
                # Use artist+album as group identifier: safe for filesystem and URLs
                import_group_id = f"{artist}_{album}".replace(' ', '_')[:100]
            else:
                # Fallback to UUID if no album
                import_group_id = str(uuid.uuid4())
        
        # Determine import type based on context and number of items
        import_type = data.get('import_type', 'playlist' if len(items) > 1 else 'song')
        
        added_count = 0
        failed_count = 0
        failed_tracks = []
        
        for item_data in items:
            artist = item_data.get('artist', '').strip() if item_data.get('artist') else ''
            title = item_data.get('title', '').strip() if item_data.get('title') else ''
            album = item_data.get('album', '').strip() if item_data.get('album') else None
            source = item_data.get('source', 'soulseek')
            
            # Extract MusicBrainz/Discogs metadata if provided
            track_number = item_data.get('track_number', '').strip() if item_data.get('track_number') else None
            album_artist = item_data.get('album_artist', '').strip() if item_data.get('album_artist') else None
            year = item_data.get('year', '').strip() if item_data.get('year') else None
            release_id = item_data.get('release_id', '').strip() if item_data.get('release_id') else None
            release_source = item_data.get('release_source', '').strip() if item_data.get('release_source') else None
            
            try:
                priority = int(item_data.get('priority', 5))
            except (ValueError, TypeError):
                priority = 5
            
            if not artist or not title:
                failed_count += 1
                failed_tracks.append(title or 'Unknown')
                logging.warning(f"Skipping item with missing fields: artist='{artist}', title='{title}'")
                continue
            
            try:
                item = add_to_queue(artist, title, album, source, priority, 
                                   import_group=import_group_id, import_type=import_type,
                                   track_number=track_number, album_artist=album_artist, 
                                   year=year, release_id=release_id, release_source=release_source)
                if item:
                    added_count += 1
                else:
                    failed_count += 1
                    failed_tracks.append(title)
            except Exception as e:
                failed_count += 1
                failed_tracks.append(title)
                logging.error(f"Error adding track '{title}' to queue: {e}")
        
        return jsonify({
            "success": True,
            "added": added_count,
            "failed": failed_count,
            "failed_tracks": failed_tracks,
            "import_group": import_group_id,
            "import_type": import_type,
            "message": f"Added {added_count} items to queue" + 
                      (f", {failed_count} failed" if failed_count > 0 else "")
        })
        
    except Exception as e:
        logging.error(f"Unexpected error in batch queue add: {type(e).__name__}: {e}")
        import traceback
        logging.error(traceback.format_exc())
        return jsonify({"error": f"Server error: {type(e).__name__}"}), 500


@app.route("/api/queue/status", methods=["GET"])
def api_queue_status():
    """Get queue status and items"""
    try:
        from download_queue_manager import get_queue, get_completed_queue, check_downloads_folder
        
        status = request.args.get('status')
        source = request.args.get('source', 'soulseek')
        limit = int(request.args.get('limit', 50))
        
        # Get queue items
        active_queue = get_queue(status=status, source=source, limit=limit)
        
        # Get completed items
        completed = get_completed_queue(limit=20)
        
        # Check downloads folder for new files
        newly_completed = check_downloads_folder()
        
        return jsonify({
            "success": True,
            "active": active_queue,
            "completed": completed,
            "newly_completed": newly_completed,
            "total_active": len(active_queue),
            "total_completed": len(completed)
        })
        
    except Exception as e:
        logging.error(f"Error getting queue status: {e}")
        return jsonify({"error": str(e)}), 400


@app.route("/api/queue/imported", methods=["GET"])
def api_queue_imported():
    """Get recently imported tracks from beets organization"""
    try:
        limit = int(request.args.get('limit', 50))
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Get recently organized/imported tracks from download_queue
        cursor.execute("""
            SELECT id, artist, title, album, source, status, 
                   import_group, import_type, imported_at, file_path, found_filename
            FROM download_queue 
            WHERE status = 'completed' AND imported_at IS NOT NULL
            ORDER BY imported_at DESC
            LIMIT ?
        """, (limit,))
        
        imported = [dict(row) for row in cursor.fetchall()]
        conn.close()
        
        return jsonify({
            "success": True,
            "imported": imported,
            "total": len(imported)
        })
        
    except Exception as e:
        logging.error(f"Error getting imported tracks: {e}")
        return jsonify({"error": str(e)}), 400


@app.route("/api/queue/<int:queue_id>/update", methods=["POST"])
def api_queue_update(queue_id):
    """Update queue item status"""
    try:
        from download_queue_manager import update_queue_item, mark_as_failed
        
        data = request.get_json()
        action = data.get('action')  # 'searching', 'downloading', 'failed', 'completed'
        
        if action == 'failed':
            reason = data.get('reason', 'Unknown error')
            retry_delay = int(data.get('retry_delay_minutes', 30))
            item = mark_as_failed(queue_id, reason, retry_delay)
        else:
            # Generic update
            item = update_queue_item(queue_id, status=action)
        
        if item:
            return jsonify({
                "success": True,
                "message": f"Queue item updated to: {action}",
                "item": item
            })
        else:
            return jsonify({"error": "Queue item not found"}), 404
            
    except Exception as e:
        logging.error(f"Error updating queue item: {e}")
        return jsonify({"error": str(e)}), 400


@app.route("/api/queue/<int:queue_id>/delete", methods=["DELETE"])
def api_queue_delete(queue_id):
    """Delete queue item"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("DELETE FROM download_queue WHERE id = ?", (queue_id,))
        conn.commit()
        
        deleted = cursor.rowcount > 0
        conn.close()
        
        if deleted:
            return jsonify({"success": True, "message": "Queue item deleted"})
        else:
            return jsonify({"error": "Queue item not found"}), 404
            
    except Exception as e:
        logging.error(f"Error deleting queue item: {e}")
        return jsonify({"error": str(e)}), 400


@app.route("/api/queue/<int:queue_id>/organize", methods=["POST"])
def api_queue_organize(queue_id):
    """Move file from /downloads to /music"""
    try:
        import shutil
        from download_queue_manager import update_queue_item
        
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT id, file_path, artist, album, title FROM download_queue WHERE id = ?
        """, (queue_id,))
        
        item = cursor.fetchone()
        
        if not item:
            conn.close()
            return jsonify({"error": "Queue item not found"}), 404
        
        if not item['file_path']:
            # No file path set - delete this orphaned item
            cursor.execute("DELETE FROM download_queue WHERE id = ?", (queue_id,))
            conn.commit()
            conn.close()
            return jsonify({"error": "File path missing - item removed from queue"}), 404
        
        file_path = item['file_path']
        artist = item['artist'] or 'Unknown Artist'
        album = item['album'] or 'Unknown Album'
        
        if not os.path.exists(file_path):
            # File was deleted - remove from queue
            cursor.execute("DELETE FROM download_queue WHERE id = ?", (queue_id,))
            conn.commit()
            conn.close()
            logging.info(f"[ORGANIZE] File no longer exists, removed from queue: {file_path}")
            return jsonify({"error": "File no longer exists - item removed from queue"}), 404
        
        conn.close()
        
        logging.info(f"[ORGANIZE] Starting organization for queue {queue_id}: {file_path}")
        
        # Get paths and create directory structure
        music_root = os.environ.get("MUSIC_ROOT", "/music")
        target_dir = os.path.join(music_root, artist, album)
        os.makedirs(target_dir, exist_ok=True)
        
        # Get filename and handle duplicates
        filename = os.path.basename(file_path)
        target_path = os.path.join(target_dir, filename)
        
        # If target exists, add suffix
        if os.path.exists(target_path):
            base, ext = os.path.splitext(filename)
            counter = 1
            while os.path.exists(os.path.join(target_dir, f"{base}_{counter}{ext}")):
                counter += 1
            target_path = os.path.join(target_dir, f"{base}_{counter}{ext}")
            logging.info(f"[ORGANIZE] Target file exists, using: {target_path}")
        
        # Move file
        logging.info(f"[ORGANIZE] Moving file from {file_path} to {target_path}")
        shutil.move(file_path, target_path)
        
        # Verify it moved
        if os.path.exists(target_path) and not os.path.exists(file_path):
            logging.info(f"[ORGANIZE] ✅ File moved successfully: {target_path}")
            update_queue_item(queue_id, status='imported', file_path=target_path)
            return jsonify({
                "success": True,
                "message": "File organized successfully",
                "target_path": target_path
            })
        else:
            logging.error(f"[ORGANIZE] Move verification failed: target_exists={os.path.exists(target_path)}, original_exists={os.path.exists(file_path)}")
            update_queue_item(queue_id, status='failed', failure_reason='File move verification failed')
            return jsonify({"error": "File move verification failed"}), 400
            
    except Exception as e:
        logging.error(f"[ORGANIZE] Error organizing file: {e}")
        log_debug(f"[ORGANIZE] Error organizing file: {e}")
        import traceback
        logging.error(traceback.format_exc())
        log_debug(traceback.format_exc())
        return jsonify({"error": str(e)}), 400


@app.route("/api/queue/organize-group", methods=["POST"])
def api_queue_organize_group():
    """Organize a group of downloads with metadata confirmation"""
    try:
        import shutil
        from download_queue_manager import update_queue_item
        
        data = request.get_json()
        group_id = data.get('group_id')
        metadata = data.get('metadata', {})
        
        if not group_id:
            return jsonify({"error": "group_id required"}), 400
        
        # Get all items in this group from completed queue
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT id, file_path, artist, album, title FROM download_queue 
            WHERE import_group = ? AND status = 'completed'
        """, (group_id,))
        
        items = cursor.fetchall()
        conn.close()
        
        if not items:
            return jsonify({"error": "No completed items found for this group"}), 404
        
        # Update metadata for all tracks in the group
        album_artist = metadata.get('album_artist') or metadata.get('artist', '')
        year = metadata.get('year', '')
        album_name = metadata.get('album', '')
        
        updated_count = 0
        errors = []
        
        logging.info(f"[ORGANIZE_GROUP] ========================================")
        logging.info(f"[ORGANIZE_GROUP] Group organization started - group_id={group_id}")
        logging.info(f"[ORGANIZE_GROUP] Album Artist: {album_artist}, Album: {album_name}, Year: {year}")
        logging.info(f"[ORGANIZE_GROUP] Total items to process: {len(items)}")
        logging.info(f"[ORGANIZE_GROUP] =========================================")
        
        for item in items:
            try:
                file_path = item['file_path']
                
                if not os.path.exists(file_path):
                    error_msg = f"File not found at {file_path}"
                    errors.append(f"{item['title']}: {error_msg}")
                    logging.error(f"[ORGANIZE_GROUP] Item {item['id']}: {error_msg}")
                    log_debug(f"[ORGANIZE_GROUP] Item {item['id']}: {error_msg}")
                    continue
                
                logging.debug(f"[ORGANIZE_GROUP] Processing item {item['id']}: {file_path} (title: {item['title']})")
                
                # Update track metadata in database
                try:
                    update_queue_item(
                        item['id'],
                        artist=item['artist'],  # Keep original artist
                        album=album_name,
                        album_artist=album_artist if album_artist else item['artist'],
                        year=year
                    )
                    logging.debug(f"[ORGANIZE_GROUP] Item {item['id']}: Updated metadata - artist={item['artist']}, album={album_name}, album_artist={album_artist}, year={year}")
                except Exception as meta_error:
                    logging.warning(f"[ORGANIZE_GROUP] Item {item['id']}: Failed to update metadata: {meta_error}")
                    log_debug(f"[ORGANIZE_GROUP] Item {item['id']}: Failed to update metadata: {meta_error}")
                
                # Move file to music directory
                try:
                    music_root = os.environ.get("MUSIC_ROOT", "/music")
                    target_dir = os.path.join(music_root, album_artist or item['artist'], album_name)
                    os.makedirs(target_dir, exist_ok=True)
                    
                    filename = os.path.basename(file_path)
                    target_path = os.path.join(target_dir, filename)
                    
                    logging.debug(f"[ORGANIZE_GROUP] Item {item['id']}: Move - source={file_path}")
                    logging.debug(f"[ORGANIZE_GROUP] Item {item['id']}: Move - target={target_path}")
                    
                    # Handle duplicates
                    if os.path.exists(target_path):
                        base, ext = os.path.splitext(filename)
                        counter = 1
                        while os.path.exists(os.path.join(target_dir, f"{base}_{counter}{ext}")):
                            counter += 1
                        target_path = os.path.join(target_dir, f"{base}_{counter}{ext}")
                        logging.debug(f"[ORGANIZE_GROUP] Item {item['id']}: Target exists, using counter={counter}: {target_path}")
                    
                    logging.info(f"[ORGANIZE_GROUP] Item {item['id']}: Moving file")
                    shutil.move(file_path, target_path)
                    
                    # Verify the move
                    if os.path.exists(target_path) and not os.path.exists(file_path):
                        logging.info(f"[ORGANIZE_GROUP] Item {item['id']}: ✅ Move successful")
                        update_queue_item(item['id'], status='imported', file_path=target_path)
                        updated_count += 1
                    else:
                        error_msg = f"Move verification failed"
                        errors.append(f"{item['title']}: {error_msg}")
                        update_queue_item(item['id'], status='failed', failure_reason=error_msg)
                        logging.error(f"[ORGANIZE_GROUP] Item {item['id']}: {error_msg}")
                        log_debug(f"[ORGANIZE_GROUP] Item {item['id']}: {error_msg}")
                        
                except Exception as move_error:
                    error_msg = str(move_error)
                    errors.append(f"{item['title']}: {error_msg}")
                    update_queue_item(item['id'], status='failed', failure_reason=error_msg)
                    logging.error(f"[ORGANIZE_GROUP] Item {item['id']}: Move failed - {type(move_error).__name__}: {move_error}")
                    log_debug(f"[ORGANIZE_GROUP] Item {item['id']}: Move failed - {type(move_error).__name__}: {move_error}")
                    
            except Exception as e:
                errors.append(f"{item['title'] or 'Unknown'}: {str(e)}")
                logging.error(f"[ORGANIZE_GROUP] Error processing item {item['id']}: {e}")
                log_debug(f"[ORGANIZE_GROUP] Error processing item {item['id']}: {e}")
                continue
        
        logging.info(f"[ORGANIZE_GROUP] ========================================")
        logging.info(f"[ORGANIZE_GROUP] Organization complete!")
        logging.info(f"[ORGANIZE_GROUP] Results: {updated_count}/{len(items)} successful")
        if errors:
            logging.info(f"[ORGANIZE_GROUP] Failed items ({len(errors)}):")
            for error in errors:
                logging.info(f"[ORGANIZE_GROUP]   - {error}")
        logging.info(f"[ORGANIZE_GROUP] =========================================")
        
        return jsonify({
            "success": True,
            "organized": updated_count,
            "total": len(items),
            "errors": errors,
            "message": f"Organized {updated_count}/{len(items)} files"
        })

        
    except Exception as e:
        logging.error(f"[ORGANIZE_GROUP] Unhandled error during organization: {type(e).__name__}: {e}")
        log_debug(f"[ORGANIZE_GROUP] Unhandled error during organization: {type(e).__name__}: {e}")
        import traceback
        logging.error(f"[ORGANIZE_GROUP] Traceback: {traceback.format_exc()}")
        log_debug(f"[ORGANIZE_GROUP] Traceback: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 400


@app.route("/api/queue-processor/status", methods=["GET"])
def api_queue_processor_status():
    """Get queue processor status"""
    try:
        import subprocess
        import psutil
        
        # Try to find the queue processor process
        processor_running = False
        processor_pid = None
        processor_memory = 0
        processor_uptime = None
        
        try:
            # Check if queue_processor.py is running
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    if proc.info['cmdline'] and len(proc.info['cmdline']) > 1:
                        if 'queue_processor.py' in ' '.join(proc.info['cmdline']):
                            processor_running = True
                            processor_pid = proc.info['pid']
                            processor_memory = proc.memory_info().rss / 1024 / 1024  # Convert to MB
                            processor_uptime = datetime.now() - datetime.fromtimestamp(proc.create_time())
                            break
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    pass
        except ImportError:
            # psutil not installed, try ps command
            try:
                result = subprocess.run(['ps', 'aux'], capture_output=True, text=True, timeout=5)
                processor_running = 'queue_processor.py' in result.stdout
            except:
                pass
        
        # Check queue status
        from download_queue_manager import get_queue
        
        queued_items = get_queue(status='queued', limit=100)
        downloading_items = get_queue(status='downloading', limit=100)
        failed_items = get_queue(status='failed', limit=100)
        completed_items = get_queue(status='completed', limit=100)
        
        return jsonify({
            "success": True,
            "processor_running": processor_running,
            "processor_pid": processor_pid,
            "processor_memory_mb": round(processor_memory, 2) if processor_memory else 0,
            "processor_uptime": str(processor_uptime) if processor_uptime else None,
            "queue_stats": {
                "queued": len(queued_items),
                "downloading": len(downloading_items),
                "failed": len(failed_items),
                "completed": len(completed_items),
                "total": len(queued_items) + len(downloading_items) + len(failed_items) + len(completed_items)
            }
        })
        
    except Exception as e:
        logging.error(f"Error getting queue processor status: {e}")
        return jsonify({
            "success": False,
            "processor_running": False,
            "error": str(e)
        }), 500


@app.route("/api/queue-processor/restart", methods=["POST"])
def api_queue_processor_restart():
    """Restart queue processor"""
    try:
        import subprocess
        import psutil
        import signal
        
        # Try to kill existing process
        killed = False
        try:
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    if proc.info['cmdline'] and len(proc.info['cmdline']) > 1:
                        if 'queue_processor.py' in ' '.join(proc.info['cmdline']):
                            logging.info(f"Killing queue processor (PID: {proc.info['pid']})")
                            proc.send_signal(signal.SIGTERM)
                            killed = True
                            # Wait up to 5 seconds for process to terminate
                            try:
                                proc.wait(timeout=5)
                            except psutil.TimeoutExpired:
                                proc.kill()
                            break
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    pass
        except ImportError:
            # psutil not installed, try killall
            subprocess.run(['killall', 'queue_processor.py'], capture_output=True)
            killed = True
        
        # Wait a bit for process to fully terminate
        import time
        time.sleep(1)
        
        # Try to start new process
        try:
            # Change to app directory
            app_dir = os.path.dirname(os.path.abspath(__file__))
            
            # Start queue processor
            subprocess.Popen(
                ['python3', 'queue_processor.py', '30'],
                cwd=app_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )
            
            logging.info("Queue processor restarted successfully")
            
            time.sleep(2)  # Wait for process to start
            
            return jsonify({
                "success": True,
                "message": "Queue processor restarted successfully",
                "killed_previous": killed
            })
        except Exception as e:
            logging.error(f"Error starting queue processor: {e}")
            return jsonify({
                "success": False,
                "message": "Failed to start queue processor",
                "error": str(e),
                "killed_previous": killed
            }), 500
            
    except Exception as e:
        logging.error(f"Error restarting queue processor: {e}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


def api_lastfm_sync_status():
    """Get Last.fm sync status and next scheduled sync"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        current_user = session.get("username", "default_user")
        
        # Get scheduler config
        cursor.execute("""
            SELECT enabled, sync_time, last_sync, next_sync 
            FROM lastfm_scheduler_config 
            WHERE username = ?
        """, (current_user,))
        
        config = cursor.fetchone()
        conn.close()
        
        if config:
            return jsonify({
                "enabled": config[0],
                "sync_time": config[1],
                "last_sync": config[2],
                "next_sync": config[3]
            })
        else:
            # Return default config if not found
            return jsonify({
                "enabled": False,
                "sync_time": "01:00",
                "last_sync": None,
                "next_sync": None
            })
    except Exception as e:
        logging.error(f"Error getting Last.fm sync status: {e}")
        return jsonify({
            "error": str(e)
        }), 500


@app.route("/api/lastfm/sync/now", methods=["POST"])
def api_lastfm_sync_now():
    """Manually trigger Last.fm recommendations sync"""
    from api_clients.lastfm import get_lastfm_recommendations
    from datetime import datetime
    import unicodedata
    
    cfg = get_config()
    lastfm_config = cfg.get("api_integrations", {}).get("lastfm", {})
    
    if not lastfm_config.get("enabled"):
        return jsonify({"error": "Last.fm not enabled"}), 400
    
    api_key = lastfm_config.get("api_key", "")
    if not api_key:
        return jsonify({"error": "Last.fm API key not configured"}), 400
    
    current_user = session.get("username")
    username = None
    if current_user:
        navidrome_users = cfg.get("navidrome_users", [])
        user_cfg = next((u for u in navidrome_users if u.get("user") == current_user), None)
        if user_cfg:
            username = user_cfg.get("lastfm_username", "")
    
    # Fetch fresh recommendations from Last.fm
    recommendations = get_lastfm_recommendations(api_key, username=username)
    
    # Better check for empty recommendations (include checking if all lists are empty)
    if not recommendations or not any([
        recommendations.get("artists", []),
        recommendations.get("albums", []),
        recommendations.get("tracks", [])
    ]):
        return jsonify({
            "error": "No recommendations from Last.fm (empty result)",
            "success": False
        }), 500
    
    # Helper function to normalize names
    def normalize_name(name):
        if not name:
            return ""
        normalized = name.lower().strip()
        normalized = unicodedata.normalize('NFD', normalized)
        normalized = ''.join(c for c in normalized if unicodedata.category(c) != 'Mn')
        normalized = ' '.join(normalized.split())
        return normalized
    
    # Get existing collection items
    existing_albums = set()
    existing_artists = set()
    existing_tracks = set()
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("SELECT DISTINCT artist, album FROM tracks WHERE artist IS NOT NULL AND album IS NOT NULL")
        for row in cursor.fetchall():
            artist = row['artist'] if row else ''
            album = row['album'] if row else ''
            if artist and album:
                artist_norm = normalize_name(artist)
                album_norm = normalize_name(album)
                existing_albums.add((artist_norm, album_norm))
        
        cursor.execute("SELECT DISTINCT artist FROM tracks WHERE artist IS NOT NULL")
        for row in cursor.fetchall():
            artist = row['artist'] if row else ''
            if artist:
                artist_norm = normalize_name(artist)
                existing_artists.add(artist_norm)
        
        cursor.execute("SELECT DISTINCT artist, title FROM tracks WHERE artist IS NOT NULL AND title IS NOT NULL")
        for row in cursor.fetchall():
            artist = row['artist'] if row else ''
            title = row['title'] if row else ''
            if artist and title:
                artist_norm = normalize_name(artist)
                title_norm = normalize_name(title)
                existing_tracks.add((artist_norm, title_norm))
        conn.close()
    except Exception as e:
        logging.warning(f"Failed to check collection status: {e}")
    
    # Store recommendations in database with batch insert to reduce lock contention
    conn = get_db()
    cursor = conn.cursor()
    
    artists_count = 0
    albums_count = 0
    tracks_count = 0
    filtered_count = 0
    
    sync_start = datetime.now()
    sync_now = datetime.now()
    
    try:
        # Prepare all insert data first to minimize transaction time
        insert_data = []
        username_val = current_user or "default_user"
        
        # Process artists
        for artist in recommendations.get("artists", []):
            artist_name = artist.get("name", "")
            artist_norm = normalize_name(artist_name)
            
            if artist_norm and artist_norm not in existing_artists:
                insert_data.append((
                    username_val,
                    "artist",
                    artist_name,
                    None,
                    artist.get("image", ""),
                    artist.get("playcount", 0),
                    artist.get("url", ""),
                    None,
                    sync_now
                ))
                artists_count += 1
            else:
                filtered_count += 1
        
        # Process albums
        for album in recommendations.get("albums", []):
            artist_name = album.get("artist", "")
            album_name = album.get("name", "")
            artist_norm = normalize_name(artist_name)
            album_norm = normalize_name(album_name)
            
            if (artist_norm, album_norm) and (artist_norm, album_norm) not in existing_albums:
                insert_data.append((
                    username_val,
                    "album",
                    album_name,
                    artist_name,
                    album.get("image", ""),
                    album.get("playcount", 0),
                    album.get("url", ""),
                    None,
                    sync_now
                ))
                albums_count += 1
            else:
                filtered_count += 1
        
        # Process tracks
        for track in recommendations.get("tracks", []):
            artist_name = track.get("artist", "")
            track_name = track.get("name", "")
            artist_norm = normalize_name(artist_name)
            track_norm = normalize_name(track_name)
            
            if (artist_norm, track_norm) and (artist_norm, track_norm) not in existing_tracks:
                insert_data.append((
                    username_val,
                    "track",
                    track_name,
                    artist_name,
                    track.get("image", ""),
                    track.get("playcount", 0),
                    track.get("url", ""),
                    None,
                    sync_now
                ))
                tracks_count += 1
            else:
                filtered_count += 1
        
        # Now execute all operations in a single transaction to avoid lock contention
        try:
            # Clear old recommendations for this user
            cursor.execute("DELETE FROM lastfm_recommendations WHERE username = ?", (username_val,))
            
            # Batch insert all recommendations using executemany
            if insert_data:
                cursor.executemany("""
                    INSERT INTO lastfm_recommendations 
                    (username, recommendation_type, item_name, artist_name, image_url, playcount, lastfm_url, metadata, synced_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, insert_data)
            
            # Update sync history
            sync_end = datetime.now()
            cursor.execute("""
                INSERT INTO lastfm_sync_history 
                (username, sync_type, artists_count, albums_count, tracks_count, filtered_count, sync_start, sync_end)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                username_val,
                "manual",
                artists_count,
                albums_count,
                tracks_count,
                filtered_count,
                sync_start,
                sync_end
            ))
            
            # Update scheduler config with last sync time
            cursor.execute("""
                INSERT OR REPLACE INTO lastfm_scheduler_config 
                (username, last_sync)
                VALUES (?, ?)
            """, (username_val, datetime.now()))
            
            conn.commit()
            conn.close()
            
            logging.info(f"Last.fm sync complete for {username_val}: {artists_count} artists, {albums_count} albums, {tracks_count} tracks")
            
            return jsonify({
                "success": True,
                "artists_synced": artists_count,
                "albums_synced": albums_count,
                "tracks_synced": tracks_count,
                "filtered_count": filtered_count,
                "total": artists_count + albums_count + tracks_count
            })
            
        except Exception as insert_error:
            try:
                conn.rollback()
            except:
                pass
            conn.close()
            logging.error(f"Database error during Last.fm sync: {insert_error}")
            raise
    
    except Exception as e:
        try:
            conn.close()
        except:
            pass
        logging.error(f"Error syncing Last.fm recommendations: {e}", exc_info=True)
        return jsonify({
            "error": str(e),
            "success": False
        }), 500


@app.route("/api/lastfm/recommendations", methods=["GET"])
def api_lastfm_recommendations():
    """Get Last.fm recommendations from cache"""
    try:
        current_user = session.get("username", "default_user")
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Get cached recommendations
        cursor.execute("""
            SELECT recommendation_type, item_name, artist_name, image_url, playcount, lastfm_url
            FROM lastfm_recommendations
            WHERE username = ?
            ORDER BY synced_at DESC, recommendation_type, item_name
        """, (current_user,))
        
        rows = cursor.fetchall()
        
        # Get last sync time
        cursor.execute("""
            SELECT MAX(synced_at) FROM lastfm_recommendations WHERE username = ?
        """, (current_user,))
        
        last_sync_row = cursor.fetchone()
        last_sync = last_sync_row[0] if last_sync_row and last_sync_row[0] else None
        conn.close()
        
        # Organize into artists, albums, tracks
        recommendations = {
            "artists": [],
            "albums": [],
            "tracks": []
        }
        
        for row in rows:
            if row is None:
                continue
            
            try:
                rec_type = row['recommendation_type']
                item_name = row['item_name']
                artist_name = row['artist_name']
                image_url = row['image_url']
                playcount = row['playcount']
                url = row['lastfm_url']
                
                rec_item = {
                    "name": item_name,
                    "image": image_url,
                    "playcount": playcount,
                    "url": url
                }
                
                if rec_type == "artist":
                    recommendations["artists"].append(rec_item)
                elif rec_type == "album":
                    rec_item["artist"] = artist_name
                    recommendations["albums"].append(rec_item)
                elif rec_type == "track":
                    rec_item["artist"] = artist_name
                    recommendations["tracks"].append(rec_item)
            except (IndexError, TypeError) as e:
                logging.debug(f"Error processing recommendation row: {e}, row: {row}")
                continue
        
        return jsonify({
            "recommendations": recommendations,
            "last_sync": last_sync,
            "cache_info": f"Cached on {last_sync}" if last_sync else "No cached data available"
        })
    except Exception as e:
        logging.error(f"Last.fm recommendations error: {str(e)}")
        return jsonify({"recommendations": {"artists": [], "albums": [], "tracks": []}, "cache_info": "Error loading cache"})


@app.route("/api/lastfm/create-playlist", methods=["POST"])
def api_lastfm_create_playlist():
    """
    Create a Navidrome playlist from Last.fm recommendations.
    Searches for tracks in the local library and matches them.
    """
    try:
        data = request.get_json()
        rec_type = data.get("type", "tracks")  # tracks, artists, or albums
        
        cfg = get_config()
        lastfm_config = cfg.get("api_integrations", {}).get("lastfm", {})
        
        if not lastfm_config.get("enabled"):
            return jsonify({"error": "Last.fm not enabled"}), 400
        
        api_key = lastfm_config.get("api_key", "")
        if not api_key:
            return jsonify({"error": "Last.fm API key not configured"}), 400
        
        # Get username from current user's navidrome settings (per-user configuration)
        current_user = session.get("username")
        username = None
        if current_user:
            navidrome_users = cfg.get("navidrome_users", [])
            user_cfg = next((u for u in navidrome_users if u.get("user") == current_user), None)
            if user_cfg:
                username = user_cfg.get("lastfm_username", "")
        
        logging.info(f"[LASTFM_PLAYLIST] Loading {rec_type} recommendations for user={current_user}, lastfm_user={username or 'undefined'}")
        
        # Get recommendations with username to personalize results
        from api_clients.lastfm import get_lastfm_recommendations
        
        # Pass database connection to filter out existing albums
        def get_db_for_lastfm():
            """Helper to get DB connection for Last.fm filtering"""
            return get_db()
        
        recommendations = get_lastfm_recommendations(api_key, username=username, db_connection=get_db_for_lastfm)
        
        # Check if we got any recommendations at all
        if not recommendations or not any([recommendations.get("artists"), recommendations.get("albums"), recommendations.get("tracks")]):
            # Log warning with more context
            has_username = bool(username)
            has_api_key = bool(api_key)
            logging.warning(f"[LASTFM_PLAYLIST] No recommendations returned - has_username={has_username}, has_api_key={has_api_key}. This may indicate: auth failure, user hasn't scrobbled enough, or API is unavailable")
            return jsonify({
                "error": f"No {rec_type} recommendations found. Ensure Last.fm account has scrobbling history and API key is valid."
            }), 404
        
        # Get the appropriate list based on type
        rec_list = []
        if rec_type == "tracks":
            rec_list = recommendations.get("tracks", [])
        elif rec_type == "artists":
            rec_list = recommendations.get("artists", [])
        elif rec_type == "albums":
            rec_list = recommendations.get("albums", [])
        
        if not rec_list:
            logging.info(f"[LASTFM_PLAYLIST] Type '{rec_type}' had no recommendations (other types may have returned results)")
            return jsonify({"error": f"No {rec_type} recommendations found"}), 404
        
        logging.info(f"[LASTFM_PLAYLIST] Got {len(rec_list)} {rec_type} recommendations to search")
        
        # Search for matching tracks in database
        matched_tracks = []
        missing_tracks = []
        
        # Get database connection
        conn = get_db()
        cursor = conn.cursor()
        
        for rec in rec_list:
            if rec_type == "tracks":
                # For tracks: rec has "artist" and "name" (track name)
                artist_name = rec.get("artist", "")
                track_name = rec.get("name", "")
                
                if not artist_name or not track_name:
                    continue
                
                # Search by artist and title
                cursor.execute("""
                    SELECT id, artist, title FROM tracks 
                    WHERE LOWER(artist) = LOWER(?) AND LOWER(title) = LOWER(?)
                    LIMIT 1
                """, (artist_name, track_name))
                result = cursor.fetchone()
                
                if result:
                    matched_tracks.append({
                        "id": result[0],
                        "artist": result[1],
                        "title": result[2]
                    })
                else:
                    missing_tracks.append({
                        "artist": artist_name,
                        "title": track_name,
                        "playcount": rec.get("playcount", 0)
                    })
                    
            elif rec_type == "albums":
                # For albums: rec has "artist" and "name" (album name)
                artist_name = rec.get("artist", "")
                album_name = rec.get("name", "")
                
                if not artist_name or not album_name:
                    continue
                
                # Search for albums by artist and album name
                cursor.execute("""
                    SELECT id, artist, album FROM tracks 
                    WHERE LOWER(artist) = LOWER(?) AND LOWER(album) = LOWER(?)
                    LIMIT 1
                """, (artist_name, album_name))
                result = cursor.fetchone()
                
                if result:
                    matched_tracks.append({
                        "id": result[0],
                        "artist": result[1],
                        "title": result[2]  # Using 'album' as title for display
                    })
                else:
                    missing_tracks.append({
                        "artist": artist_name,
                        "title": album_name,
                        "playcount": rec.get("playcount", 0)
                    })
                    
            elif rec_type == "artists":
                # For artists: rec only has "name" (artist name)
                artist_name = rec.get("name", "")
                
                if not artist_name:
                    continue
                
                # Search for any tracks by this artist
                cursor.execute("""
                    SELECT id, artist, album FROM tracks 
                    WHERE LOWER(artist) = LOWER(?)
                    LIMIT 5
                """, (artist_name,))
                results = cursor.fetchall()
                
                if results:
                    for result in results:
                        matched_tracks.append({
                            "id": result[0],
                            "artist": result[1],
                            "title": result[2]
                        })
                else:
                    missing_tracks.append({
                        "artist": artist_name,
                        "title": f"(multiple tracks)",
                        "playcount": rec.get("playcount", 0)
                    })
        
        conn.close()
        
        logging.info(f"[LASTFM_PLAYLIST] Matched {len(matched_tracks)} / {len(rec_list)} {rec_type} recommendations")
        
        return jsonify({
            "total_recommendations": len(rec_list),
            "matched": len(matched_tracks),
            "missing": len(missing_tracks),
            "matched_tracks": matched_tracks,
            "missing_tracks": missing_tracks,
            "recommendation_type": rec_type
        })
        
    except Exception as e:
        logging.error(f"[LASTFM_PLAYLIST] Error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/recommended-playlists", methods=["GET"])
def api_recommended_playlists():
    """
    Generate recommended playlists from Last.fm and ListenBrainz data.
    Returns four categories of playlists:
    - similar_artists: Playlists based on similar artists
    - top_genres: Playlists based on library genres
    - mood_playlists: Playlists based on track ratings
    - discovery: Playlists for unrated and recently added tracks
    """
    try:
        from playlist_recommendations import PlaylistRecommender
        
        cfg = get_config()
        
        # Get database connection
        conn = get_db()
        
        # Initialize clients if available
        lastfm_client = None
        listenbrainz_client = None
        
        lastfm_config = cfg.get("api_integrations", {}).get("lastfm", {})
        if lastfm_config.get("enabled") and lastfm_config.get("api_key"):
            from api_clients.lastfm import LastFmClient
            lastfm_client = LastFmClient(lastfm_config.get("api_key"))
        
        listenbrainz_config = cfg.get("api_integrations", {}).get("listenbrainz", {})
        if listenbrainz_config.get("enabled"):
            from api_clients.audiodb_and_listenbrainz import ListenBrainzClient
            listenbrainz_client = ListenBrainzClient()
        
        # Create recommender instance
        recommender = PlaylistRecommender(
            lastfm_client=lastfm_client,
            listenbrainz_client=listenbrainz_client,
            db_connection=conn
        )
        
        # Get recommendations
        recommendations = recommender.get_recommendations()
        
        conn.close()
        
        return jsonify({
            "success": True,
            "recommendations": recommendations
        })
        
    except Exception as e:
        logging.error(f"[RECOMMENDED_PLAYLISTS] Error: {e}", exc_info=True)
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route("/api/listenbrainz/sync/now", methods=["POST"])
def api_listenbrainz_sync_now():
    """Manually trigger ListenBrainz recommendations sync"""
    from api_clients.audiodb_and_listenbrainz import ListenBrainzUserClient
    from datetime import datetime
    import unicodedata
    
    cfg = get_config()
    lb_config = cfg.get("api_integrations", {}).get("listenbrainz", {})
    
    if not lb_config.get("enabled"):
        return jsonify({"error": "ListenBrainz not enabled"}), 400
    
    current_user = session.get("username")
    user_token = None
    
    if current_user:
        navidrome_users = cfg.get("navidrome_users", [])
        user_cfg = next((u for u in navidrome_users if u.get("user") == current_user), None)
        if user_cfg:
            user_token = user_cfg.get("listenbrainz_user_token", "")
    
    if not user_token:
        return jsonify({"error": "ListenBrainz user token not configured"}), 400
    
    # Create ListenBrainz client and validate token
    try:
        lb_client = ListenBrainzUserClient(user_token)
        username = lb_client.get_username_from_token()
        if not username:
            return jsonify({"error": "Invalid ListenBrainz token"}), 400
    except Exception as e:
        logging.error(f"Failed to validate ListenBrainz token: {e}")
        return jsonify({"error": str(e)}), 500
    
    # Fetch fresh recommendations from ListenBrainz (weekly explorations)
    try:
        recommendations = lb_client.get_weekly_exploration(username)
        # Convert from ListenBrainz format to our format
        tracks_list = []
        for track in recommendations:
            # ListenBrainz returns recordings with artist/title info
            if isinstance(track, dict):
                track_name = track.get("title", track.get("name", ""))
                artist_obj = track.get("artist", {})
                artist_name = ""
                if isinstance(artist_obj, dict):
                    artist_name = artist_obj.get("name", "")
                elif isinstance(artist_obj, str):
                    artist_name = artist_obj
                
                if track_name and artist_name:
                    tracks_list.append({
                        "track_name": track_name,
                        "artist_name": artist_name,
                        "release_name": track.get("release", {}).get("name", "") if isinstance(track.get("release"), dict) else "",
                        "confidence": 0.8,
                        "source": "listenbrainz-weekly-exploration"
                    })
    except Exception as e:
        logging.error(f"Failed to fetch ListenBrainz recommendations: {e}")
        return jsonify({"error": str(e)}), 500
    
    if not tracks_list:
        return jsonify({
            "error": "No recommendations from ListenBrainz (empty result)",
            "success": False
        }), 500
    
    # Helper function to normalize names
    def normalize_name(name):
        if not name:
            return ""
        normalized = name.lower().strip()
        normalized = unicodedata.normalize('NFD', normalized)
        normalized = ''.join(c for c in normalized if unicodedata.category(c) != 'Mn')
        normalized = ' '.join(normalized.split())
        return normalized
    
    # Get existing collection items
    existing_tracks = set()
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("SELECT DISTINCT artist, title FROM tracks WHERE artist IS NOT NULL AND title IS NOT NULL")
        for row in cursor.fetchall():
            artist = row['artist'] if row else ''
            title = row['title'] if row else ''
            if artist and title:
                artist_norm = normalize_name(artist)
                title_norm = normalize_name(title)
                existing_tracks.add((artist_norm, title_norm))
        conn.close()
    except Exception as e:
        logging.warning(f"Failed to check collection status: {e}")
    
    # Store recommendations in database
    conn = get_db()
    cursor = conn.cursor()
    
    tracks_count = 0
    filtered_count = 0
    sync_start = datetime.now()
    sync_now = datetime.now()
    
    try:
        # Prepare all insert data
        insert_data = []
        username_val = current_user or "default_user"
        
        for track in tracks_list:
            track_name = track.get("track_name", "")
            artist_name = track.get("artist_name", "")
            artist_norm = normalize_name(artist_name)
            track_norm = normalize_name(track_name)
            
            if (artist_norm, track_norm) and (artist_norm, track_norm) not in existing_tracks:
                insert_data.append((
                    username_val,
                    "track",
                    track_name,
                    artist_name,
                    track.get("release_name", ""),
                    track.get("confidence", 0.5),
                    track.get("source", "unknown"),
                    None,
                    sync_now
                ))
                tracks_count += 1
            else:
                filtered_count += 1
        
        # Execute transaction
        try:
            # Clear old recommendations for this user
            cursor.execute("DELETE FROM listenbrainz_recommendations WHERE username = ?", (username_val,))
            
            # Batch insert all recommendations
            if insert_data:
                cursor.executemany("""
                    INSERT INTO listenbrainz_recommendations 
                    (username, recommendation_type, track_name, artist_name, release_name, confidence, source, metadata, synced_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, insert_data)
            
            # Update sync history
            sync_end = datetime.now()
            cursor.execute("""
                INSERT INTO listenbrainz_sync_history 
                (username, sync_type, source, tracks_count, filtered_count, sync_start, sync_end)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                username_val,
                "manual",
                "listenbrainz-api",
                tracks_count,
                filtered_count,
                sync_start,
                sync_end
            ))
            
            conn.commit()
            conn.close()
            
            logging.info(f"ListenBrainz sync complete for {username_val}: {tracks_count} tracks")
            
            return jsonify({
                "success": True,
                "tracks_synced": tracks_count,
                "filtered_count": filtered_count,
                "total": tracks_count
            })
            
        except Exception as insert_error:
            try:
                conn.rollback()
            except:
                pass
            conn.close()
            logging.error(f"Database error during ListenBrainz sync: {insert_error}")
            raise
    
    except Exception as e:
        try:
            conn.close()
        except:
            pass
        logging.error(f"Error syncing ListenBrainz recommendations: {e}", exc_info=True)
        return jsonify({"error": str(e), "success": False}), 500
    
    cfg = get_config()
    lb_config = cfg.get("api_integrations", {}).get("listenbrainz", {})
    
    if not lb_config.get("enabled"):
        return jsonify({"error": "ListenBrainz not enabled"}), 400
    
    current_user = session.get("username")
    username = None
    user_token = None
    
    if current_user:
        navidrome_users = cfg.get("navidrome_users", [])
        user_cfg = next((u for u in navidrome_users if u.get("user") == current_user), None)
        if user_cfg:
            username = user_cfg.get("listenbrainz_username", "")
            user_token = user_cfg.get("listenbrainz_token", "")
    
    if not username:
        return jsonify({"error": "ListenBrainz username not configured"}), 400
    
    # Fetch fresh recommendations from ListenBrainz
    try:
        recommendations = get_listenbrainz_recommendations(username, user_token=user_token)
    except Exception as e:
        logging.error(f"Failed to fetch ListenBrainz recommendations: {e}")
        return jsonify({"error": str(e)}), 500
    
    if not recommendations:
        return jsonify({
            "error": "No recommendations from ListenBrainz (empty result)",
            "success": False
        }), 500
    
    # Helper function to normalize names
    def normalize_name(name):
        if not name:
            return ""
        normalized = name.lower().strip()
        normalized = unicodedata.normalize('NFD', normalized)
        normalized = ''.join(c for c in normalized if unicodedata.category(c) != 'Mn')
        normalized = ' '.join(normalized.split())
        return normalized
    
    # Get existing collection items
    existing_tracks = set()
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("SELECT DISTINCT artist, title FROM tracks WHERE artist IS NOT NULL AND title IS NOT NULL")
        for row in cursor.fetchall():
            artist = row['artist'] if row else ''
            title = row['title'] if row else ''
            if artist and title:
                artist_norm = normalize_name(artist)
                title_norm = normalize_name(title)
                existing_tracks.add((artist_norm, title_norm))
        conn.close()
    except Exception as e:
        logging.warning(f"Failed to check collection status: {e}")
    
    # Store recommendations in database
    conn = get_db()
    cursor = conn.cursor()
    
    tracks_count = 0
    filtered_count = 0
    sync_start = datetime.now()
    sync_now = datetime.now()
    
    try:
        # Prepare all insert data
        insert_data = []
        username_val = current_user or "default_user"
        
        for track in recommendations:
            track_name = track.get("track_name", "")
            artist_name = track.get("artist_name", "")
            artist_norm = normalize_name(artist_name)
            track_norm = normalize_name(track_name)
            
            if (artist_norm, track_norm) and (artist_norm, track_norm) not in existing_tracks:
                insert_data.append((
                    username_val,
                    "track",
                    track_name,
                    artist_name,
                    track.get("release_name", ""),
                    track.get("confidence", 0.5),
                    track.get("source", "unknown"),
                    None,
                    sync_now
                ))
                tracks_count += 1
            else:
                filtered_count += 1
        
        # Execute transaction
        try:
            # Clear old recommendations for this user
            cursor.execute("DELETE FROM listenbrainz_recommendations WHERE username = ?", (username_val,))
            
            # Batch insert all recommendations
            if insert_data:
                cursor.executemany("""
                    INSERT INTO listenbrainz_recommendations 
                    (username, recommendation_type, track_name, artist_name, release_name, confidence, source, metadata, synced_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, insert_data)
            
            # Update sync history
            sync_end = datetime.now()
            cursor.execute("""
                INSERT INTO listenbrainz_sync_history 
                (username, sync_type, source, tracks_count, filtered_count, sync_start, sync_end)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                username_val,
                "manual",
                "listenbrainz-api",
                tracks_count,
                filtered_count,
                sync_start,
                sync_end
            ))
            
            conn.commit()
            conn.close()
            
            logging.info(f"ListenBrainz sync complete for {username_val}: {tracks_count} tracks")
            
            return jsonify({
                "success": True,
                "tracks_synced": tracks_count,
                "filtered_count": filtered_count,
                "total": tracks_count
            })
            
        except Exception as insert_error:
            try:
                conn.rollback()
            except:
                pass
            conn.close()
            logging.error(f"Database error during ListenBrainz sync: {insert_error}")
            raise
    
    except Exception as e:
        try:
            conn.close()
        except:
            pass
        logging.error(f"Error syncing ListenBrainz recommendations: {e}", exc_info=True)
        return jsonify({"error": str(e), "success": False}), 500


@app.route("/api/listenbrainz/recommendations", methods=["GET"])
def api_listenbrainz_recommendations_cached():
    """Get ListenBrainz recommendations from cache"""
    try:
        current_user = session.get("username", "default_user")
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Get recommendations ordered by synced_at (most recent first)
        cursor.execute("""
            SELECT 
                track_name, 
                artist_name, 
                release_name, 
                confidence, 
                source,
                synced_at
            FROM listenbrainz_recommendations 
            WHERE username = ?
            ORDER BY synced_at DESC
            LIMIT 50
        """, (current_user,))
        
        rows = cursor.fetchall()
        conn.close()
        
        tracks = []
        if rows:
            last_sync = None
            for row in rows:
                tracks.append({
                    "name": row['track_name'],
                    "artist": row['artist_name'],
                    "album": row['release_name'],
                    "confidence": row['confidence'],
                    "source": row['source'],
                    "url": f"https://listenbrainz.org/user/{current_user}/"
                })
                if not last_sync:
                    last_sync = row['synced_at']
            
            return jsonify({
                "recommendations": {
                    "tracks": tracks
                },
                "cache_info": f"Cached on {last_sync}" if last_sync else "No cached data available"
            })
        else:
            return jsonify({
                "recommendations": {"tracks": []},
                "cache_info": "No cached data available"
            })
    
    except Exception as e:
        logging.error(f"ListenBrainz recommendations error: {str(e)}")
        return jsonify({"recommendations": {"tracks": []}, "cache_info": "Error loading cache"})


@app.route("/downloads-manager")
def downloads_manager():
    """Downloads manager UI page"""
    cfg = get_config()
    
    # Get downloads folder from config, fall back to env var, then default
    downloads_dir = cfg.get("downloads", {}).get("folder", os.environ.get("DOWNLOADS_DIR", "/downloads"))
    
    return render_template("downloads_manager.html", 
                         downloads_dir=downloads_dir,
                         api_services=cfg.get('api_integrations', {}))


@app.route("/smart-playlists")
def smart_playlists():
    """Redirect to unified playlist manager"""
    return redirect(url_for("playlist_manager"))


@app.route("/downloads-monitor")
def downloads_monitor_legacy():
    """Downloads monitoring UI page (legacy route)"""
    # Legacy route: redirect to unified downloads page (search + monitor)
    return redirect(url_for("downloads"))


@app.route("/api/qbittorrent/status", methods=["GET"])
def qbit_status():
    """Get qBittorrent download status"""
    cfg = get_config()
    qbit_config = cfg.get("qbittorrent", {})
    
    if not qbit_config.get("enabled"):
        return jsonify({"error": "qBittorrent integration not enabled"}), 400
    
    web_url = qbit_config.get("web_url", "http://localhost:8080")
    username = qbit_config.get("username", "")
    password = qbit_config.get("password", "")
    
    try:
        import requests as req
        
        # Login
        session = req.Session()
        login_url = f"{web_url}/api/v2/auth/login"
        login_resp = session.post(login_url, data={"username": username, "password": password}, timeout=10)
        
        if login_resp.text != "Ok.":
            return jsonify({"error": "Failed to login to qBittorrent"}), 500
        
        # Get torrents info
        torrents_url = f"{web_url}/api/v2/torrents/info"
        resp = session.get(torrents_url, timeout=10)
        
        if resp.status_code != 200:
            return jsonify({"error": f"Failed to get torrents: {resp.status_code}"}), 500
        
        torrents = resp.json()
        
        # Filter and format torrents - only show Music category
        active_torrents = []
        for torrent in torrents:
            # Only include torrents in Music category
            if torrent.get("category", "") == "Music":
                active_torrents.append({
                    "hash": torrent.get("hash", ""),
                    "name": torrent.get("name", ""),
                    "state": torrent.get("state", ""),
                    "progress": round(torrent.get("progress", 0) * 100, 2),
                    "dlspeed": torrent.get("dlspeed", 0),
                    "upspeed": torrent.get("upspeed", 0),
                    "downloaded": torrent.get("downloaded", 0),
                    "uploaded": torrent.get("uploaded", 0),
                    "size": torrent.get("size", 0),
                    "eta": torrent.get("eta", 0),
                    "num_seeds": torrent.get("num_seeds", 0),
                    "num_leechs": torrent.get("num_leechs", 0),
                    "category": torrent.get("category", ""),
                    "save_path": torrent.get("save_path", ""),
                    "added_on": torrent.get("added_on", 0)
                })
        
        # Sort by most recently added (added_on descending)
        active_torrents.sort(key=lambda x: x.get("added_on", 0), reverse=True)
        
        return jsonify({"torrents": active_torrents})
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/slskd/status", methods=["GET"])
def slskd_status():
    """Get slskd download status"""
    cfg = get_config()
    slskd_config = cfg.get("slskd", {})
    
    if not slskd_config.get("enabled"):
        return jsonify({"error": "slskd integration not enabled"}), 400
    
    web_url = slskd_config.get("web_url", "http://localhost:5030")
    api_key = slskd_config.get("api_key", "")
    
    try:
        import requests as req
        
        headers = {"X-API-Key": api_key} if api_key else {}
        
        # Get transfers
        transfers_url = f"{web_url}/api/v0/transfers/downloads"
        logging.debug(f"slskd_status: Fetching from {transfers_url}")
        resp = req.get(transfers_url, headers=headers, timeout=10)
        
        logging.debug(f"slskd_status: Response status {resp.status_code}")
        
        if resp.status_code != 200:
            logging.error(f"slskd_status: Failed to get transfers: {resp.status_code} - {resp.text[:500]}")
            return jsonify({"error": f"Failed to get transfers: {resp.status_code}"}), 500
        
        downloads_data = resp.json()
        logging.debug(f"slskd_status: Response is list: {isinstance(downloads_data, list)}, count: {len(downloads_data) if isinstance(downloads_data, list) else 'N/A'}")
        
        # Format downloads - slskd API returns array of UserResponse objects
        # Structure: [{ "username": "...", "directories": [{ "directory": "...", "files": [...] }] }]
        active_downloads = []
        
        if isinstance(downloads_data, list):
            # Correct format: array of user objects with nested directories and files
            for user_obj in downloads_data:
                if not isinstance(user_obj, dict):
                    continue
                
                username = user_obj.get("username", "Unknown")
                directories = user_obj.get("directories", [])
                
                if not isinstance(directories, list):
                    continue
                
                # Iterate through directories for this user
                for dir_obj in directories:
                    if not isinstance(dir_obj, dict):
                        continue
                    
                    files = dir_obj.get("files", [])
                    if not isinstance(files, list):
                        continue
                    
                    # Process each file
                    for file_obj in files:
                        if not isinstance(file_obj, dict):
                            continue
                        
                        filename = file_obj.get("filename", "Unknown")
                        size = int(file_obj.get("size", 0))
                        bytes_transferred = int(file_obj.get("bytesTransferred", 0))
                        percent_complete = int(file_obj.get("percentComplete", 0))
                        
                        # Normalize state
                        state_raw = file_obj.get("state", "")
                        state_lower = str(state_raw).lower()
                        
                        if "completed" in state_lower and "succeeded" in state_lower:
                            state = "Completed"
                        elif "completed" in state_lower and ("errored" in state_lower or "failed" in state_lower):
                            state = "Failed"
                        elif "completed" in state_lower and "cancelled" in state_lower:
                            state = "Cancelled"
                        elif "inprogress" in state_lower:
                            state = "Downloading"
                        elif "queued" in state_lower:
                            state = "Queued"
                        elif "initializing" in state_lower:
                            state = "Initializing"
                        else:
                            state = state_raw or "Unknown"
                        
                        average_speed = int(file_obj.get("averageSpeed", 0))
                        
                        logging.debug(f"slskd download: {username} -> {filename[:60]}, state={state}, progress={percent_complete}%, size={size}")
                        
                        active_downloads.append({
                            "username": username,
                            "filename": filename,
                            "state": state,
                            "progress": percent_complete,
                            "bytesTransferred": bytes_transferred,
                            "size": size,
                            "averageSpeed": average_speed,
                            "remoteToken": file_obj.get("id", ""),
                        })
        
        logging.info(f"slskd_status: Returning {len(active_downloads)} active downloads")
        return jsonify({"downloads": active_downloads})
        
    except Exception as e:
        logging.error(f"Error fetching slskd status: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/smartplaylist/create", methods=["POST"])
def api_create_smart_playlist():
    """Create a new Smart Playlist (.nsp file)"""
    try:
        data = request.get_json()
        file_name = data.get('fileName', '').strip()
        playlist_data = data.get('playlist', {})
        
        if not file_name:
            return jsonify({"error": "File name is required"}), 400
        
        if not playlist_data.get('name'):
            return jsonify({"error": "Playlist name is required"}), 400
        
        # Sanitize file name
        file_name = ''.join(c for c in file_name if c.isalnum() or c in ('-', '_', ' '))
        if not file_name:
            return jsonify({"error": "Invalid file name"}), 400
        
        # Create playlists directory if it doesn't exist
        music_folder = os.environ.get("MUSIC_FOLDER", "/music")
        playlists_dir = os.path.join(music_folder, "Playlists")
        os.makedirs(playlists_dir, exist_ok=True)
        
        # Create file path
        file_path = os.path.join(playlists_dir, f"{file_name}.nsp")
        
        # Check if file already exists
        if os.path.exists(file_path):
            return jsonify({"error": f"Playlist file '{file_name}.nsp' already exists"}), 400
        
        # Write the playlist file
        try:
            with open(file_path, 'w') as f:
                json.dump(playlist_data, f, indent=2)
            
            return jsonify({
                "success": True,
                "message": f"Smart Playlist '{playlist_data.get('name')}' created successfully",
                "file_path": file_path,
                "file_name": f"{file_name}.nsp"
            }), 201
        
        except IOError as e:
            return jsonify({"error": f"Failed to write playlist file: {str(e)}"}), 500
    
    except Exception as e:
        logging.error(f"Error creating smart playlist: {str(e)}")
        return jsonify({"error": str(e)}), 500


# ============================================================================
# BEETS MUSIC TAGGING ROUTES
# ============================================================================

@app.route("/metadata-compare", methods=["GET"])
def metadata_compare():
    """Metadata comparison page - compare Navidrome vs Beets album data"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Get album mismatches (where Navidrome and Beets data differ)
        cursor.execute("""
            SELECT DISTINCT 
                album,
                artist,
                year,
                beets_year,
                navidrome_genres,
                musicbrainz_genres,
                COUNT(*) as track_count
            FROM tracks
            WHERE musicbrainz_album_mbid IS NOT NULL
            GROUP BY album, artist
            ORDER BY artist, album
        """)
        
        albums = cursor.fetchall()
        album_comparisons = []
        
        for album_row in albums:
            album = album_row[0]
            artist = album_row[1]
            nav_year = album_row[2]
            beets_year = album_row[3]
            nav_genres = album_row[4]
            beets_genres = album_row[5]
            track_count = album_row[6]
            
            # Check for mismatches
            has_mismatch = (
                (nav_year != beets_year) or
                (nav_genres != beets_genres)
            )
            
            if has_mismatch:
                album_comparisons.append({
                    "album": album,
                    "artist": artist,
                    "navidrome": {
                        "year": nav_year,
                        "genres": nav_genres.split(",") if nav_genres else []
                    },
                    "beets": {
                        "year": beets_year,
                        "genres": beets_genres.split(",") if beets_genres else []
                    },
                    "track_count": track_count
                })
        
        conn.close()
        
        return render_template(
            "metadata_compare.html",
            album_comparisons=album_comparisons
        )
    except Exception as e:
        logging.error(f"Error loading metadata comparison: {str(e)}")
        flash(f"Error loading metadata comparison: {str(e)}", "danger")
        return redirect(url_for("dashboard"))


@app.route("/api/metadata-compare/search-musicbrainz", methods=["POST"])
def search_musicbrainz_for_album():
    """Search MusicBrainz for album matches"""
    try:
        data = request.json or {}
        artist = data.get("artist", "")
        album = data.get("album", "")
        
        if not artist or not album:
            return jsonify({"error": "Artist and album name required"}), 400
        
        # Import MusicBrainz client and use get_suggested_mbid
        from api_clients.musicbrainz import MusicBrainzClient
        mb_client = MusicBrainzClient()
        mbid, confidence = mb_client.get_suggested_mbid(album, artist)
        result = {
            "mbid": mbid,
            "confidence": confidence,
            "album": album,
            "artist": artist
        }
        return jsonify({
            "success": True,
            "result": result
        })
    except Exception as e:
        logging.error(f"MusicBrainz search error: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/metadata-compare/accept-navidrome", methods=["POST"])
def accept_navidrome_data():
    """Accept Navidrome data and lock it from being overwritten by Beets"""
    try:
        data = request.json or {}
        album = data.get("album", "")
        artist = data.get("artist", "")
        
        if not artist or not album:
            return jsonify({"error": "Artist and album name required"}), 400
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Mark all tracks in this album as locked from Beets overwrites
        cursor.execute("""
            UPDATE tracks 
            SET metadata_locked = 1
            WHERE artist = ? AND album = ?
        """, (artist, album))
        
        conn.commit()
        conn.close()
        
        return jsonify({
            "success": True,
            "message": f"Navidrome data locked for {artist} - {album}"
        })
    except Exception as e:
        logging.error(f"Error accepting Navidrome data: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/metadata-compare/apply-musicbrainz", methods=["POST"])
def apply_musicbrainz_data():
    """Apply MusicBrainz data to an album"""
    try:
        data = request.json or {}
        album = data.get("album", "")
        artist = data.get("artist", "")
        mb_data = data.get("mb_data", {})
        
        if not artist or not album or not mb_data:
            return jsonify({"error": "Artist, album, and MusicBrainz data required"}), 400
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Update tracks with MusicBrainz data
        cursor.execute("""
            UPDATE tracks 
            SET 
                year = ?,
                musicbrainz_genres = ?
            WHERE artist = ? AND album = ?
        """, (
            mb_data.get("year"),
            ",".join(mb_data.get("genres", [])),
            artist,
            album
        ))
        
        conn.commit()
        conn.close()
        
        return jsonify({
            "success": True,
            "message": f"Applied MusicBrainz data to {artist} - {album}"
        })
    except Exception as e:
        logging.error(f"Error applying MusicBrainz data: {str(e)}")
        return jsonify({"error": str(e)}), 500


# ============================================================================
# SPOTIFY PLAYLIST IMPORT ROUTES
# ============================================================================

@app.route("/playlist/import")
def playlist_import():
    """Redirect to unified playlist manager"""
    return redirect(url_for("playlist_manager"))


@app.route("/api/playlist/import", methods=["POST"])
def api_playlist_import():
    """API endpoint to import a Spotify playlist and match to Navidrome database using enhanced ISRC + fuzzy + strict matching"""
    try:
        data = request.get_json()
        spotify_url = data.get("spotify_url", "").strip()
        playlist_name = data.get("playlist_name", "").strip()
        playlist_description = data.get("playlist_description", "").strip()
        
        if not spotify_url or not playlist_name:
            return jsonify({"error": "Missing required fields"}), 400
        
        # Extract playlist ID from various URL formats
        playlist_id = extract_spotify_playlist_id(spotify_url)
        if not playlist_id:
            return jsonify({"error": "Invalid Spotify playlist URL or ID"}), 400
        
        # Get Spotify client and fetch playlist tracks
        try:
            spotify_tracks = get_spotify_playlist_tracks(playlist_id)
        except Exception as e:
            return jsonify({"error": f"Failed to fetch Spotify playlist: {str(e)}"}), 500
        
        if not spotify_tracks:
            return jsonify({"error": "Playlist is empty or could not be fetched"}), 400
        
        # Match tracks using enhanced 3-tier strategy: ISRC → Fuzzy → Strict
        matched_tracks = []
        missing_tracks = []
        
        # Statistics for matching strategies
        match_stats = {
            "isrc": 0,
            "fuzzy": 0,
            "strict": 0,
            "unmatched": 0
        }
        
        conn = get_db()
        cursor = conn.cursor()

        for spotify_track in spotify_tracks:
            # Use enhanced matching from playlist_matcher
            matched_track, confidence, strategy = enhanced_match_track(
                spotify_track,
                cursor,
                enable_isrc=True,
                enable_fuzzy=True,
                enable_strict=True,
                fuzzy_threshold=0.80,  # Based on navispot's threshold
                logger=logging
            )
            
            if matched_track:
                matched_tracks.append({
                    "id": matched_track["id"],
                    "title": matched_track["title"],
                    "artist": matched_track["artist"],
                    "album": matched_track["album"],
                    "stars": matched_track["stars"],
                    "confidence": confidence,
                    "strategy": strategy
                })
                match_stats[strategy] += 1
            else:
                missing_tracks.append({
                    "title": spotify_track.get("title", ""),
                    "artist": spotify_track.get("artist", ""),
                    "album": spotify_track.get("album", ""),
                    "spotify_id": spotify_track.get("spotify_id", ""),
                    "isrc": spotify_track.get("isrc", ""),
                    "best_score": confidence
                })
                match_stats["unmatched"] += 1
        
        conn.close()
        
        # Check if slskd is enabled
        config_data, _ = _read_yaml(CONFIG_PATH)
        slskd_enabled = config_data.get("slskd", {}).get("enabled", False)
        
        # Log matching statistics
        logging.info(f"Playlist import '{playlist_name}': Matched {len(matched_tracks)}/{len(spotify_tracks)} tracks")
        logging.info(f"  Strategy breakdown: ISRC={match_stats['isrc']}, Fuzzy={match_stats['fuzzy']}, Strict={match_stats['strict']}, Unmatched={match_stats['unmatched']}")
        
        return jsonify({
            "success": True,
            "playlist_name": playlist_name,
            "playlist_description": playlist_description,
            "matched_tracks": matched_tracks,
            "missing_tracks": missing_tracks,
            "slskd_enabled": slskd_enabled,
            "spotify_playlist_id": playlist_id,
            "message": f"Matched {len(matched_tracks)}/{len(spotify_tracks)} tracks",
            "match_stats": match_stats
        })
    
    except Exception as e:
        logging.error(f"Playlist import error: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/playlist/create", methods=["POST"])
def api_playlist_create():
    """API endpoint to create a Navidrome playlist from matched tracks"""
    try:
        data = request.get_json()
        playlist_name = data.get("playlist_name", "").strip()
        playlist_description = data.get("playlist_description", "").strip()
        matched_tracks = data.get("matched_tracks", [])
        
        if not playlist_name or not matched_tracks:
            return jsonify({"error": "Missing required fields"}), 400
        
        # Get track IDs from matched tracks
        track_ids = [track.get("id") for track in matched_tracks if track.get("id")]
        
        if not track_ids:
            return jsonify({"error": "No valid tracks to add to playlist"}), 400
        
        # Create NSP playlist file
        playlist_data = {
            "name": playlist_name,
            "comment": playlist_description or "Imported from Spotify",
            "all": []
        }
        
        # Add track IDs as a list
        playlist_data["trackIds"] = track_ids
        
        # Create playlists directory if it doesn't exist
        music_folder = os.environ.get("MUSIC_FOLDER", "/music")
        playlists_dir = os.path.join(music_folder, "Playlists")
        os.makedirs(playlists_dir, exist_ok=True)
        
        # Sanitize playlist name for filename
        file_name = "".join(c for c in playlist_name if c.isalnum() or c in ('-', '_', ' '))
        if not file_name:
            return jsonify({"error": "Invalid playlist name"}), 400
        
        file_path = os.path.join(playlists_dir, f"{file_name}.nsp")
        
        # Check if file already exists
        if os.path.exists(file_path):
            return jsonify({"error": f"Playlist file '{file_name}.nsp' already exists"}), 400
        
        # Write the playlist file
        try:
            with open(file_path, 'w') as f:
                json.dump(playlist_data, f, indent=2)
            
            logging.info(f"Created playlist: {playlist_name} with {len(track_ids)} tracks")
            
            return jsonify({
                "success": True,
                "message": f"Playlist '{playlist_name}' created successfully",
                "file_path": file_path,
                "file_name": f"{file_name}.nsp",
                "track_count": len(track_ids)
            }), 201
        
        except IOError as e:
            return jsonify({"error": f"Failed to write playlist file: {str(e)}"}), 500
    
    except Exception as e:
        logging.error(f"Playlist creation error: {str(e)}")
        return jsonify({"error": str(e)}), 500


def extract_spotify_playlist_id(url_or_id):
    """Extract Spotify playlist ID from URL or return the ID if already in correct format"""
    import re
    
    # If it's just an ID (32 characters of alphanumeric)
    if re.match(r"^[a-zA-Z0-9]{22}$", url_or_id):
        return url_or_id
    
    # Extract from various URL formats
    patterns = [
        r"spotify\.com/playlist/([a-zA-Z0-9]+)",  # https://open.spotify.com/playlist/...
        r"spotify:playlist:([a-zA-Z0-9]+)",       # spotify:playlist:...
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url_or_id)
        if match:
            return match.group(1)
    
    return None


def get_spotify_playlist_tracks(playlist_id):
    """Fetch tracks from a Spotify playlist"""
    try:
        from popularity_helpers import get_spotify_client
        spotify_client = get_spotify_client()
        if not spotify_client:
            raise Exception("Spotify client not configured")
        
        # Use SpotifyClient's get_playlist_tracks method
        tracks = spotify_client.get_playlist_tracks(playlist_id)
        return tracks
    
    except Exception as e:
        logging.error(f"Error fetching Spotify playlist: {str(e)}")
        raise




@app.route("/api/track/discogs", methods=["POST"])
def api_track_discogs_lookup():
    """Lookup track on Discogs for better metadata and genres"""
    try:
        from api_clients.discogs import DiscogsClient
        
        data = request.get_json()
        title = data.get("title", "")
        artist = data.get("artist", "")
        album = data.get("album", "")
        
        if not title or not artist:
            return jsonify({"error": "Missing title or artist"}), 400
        
        # Get Discogs token from config
        cfg = get_config()
        # Check both api_integrations.discogs and root discogs for backwards compatibility
        discogs_config = cfg.get("api_integrations", {}).get("discogs", {}) or cfg.get("discogs", {})
        token = discogs_config.get("token", "")
        
        if not token:
            return jsonify({"error": "Discogs token not configured. Please add your Discogs token in config.yaml under api_integrations.discogs.token"}), 400
        
        # Use DiscogsClient with proper session and retry logic
        client = DiscogsClient(token=token)
        
        # Prepare search query
        from api_clients import session
        query = f"{artist} {album or title}"
        
        # Search Discogs API using the shared session with retry logic
        headers = {
            "Authorization": f"Discogs token={token}",
            "User-Agent": "sptnr-cli/1.0 +https://github.com/M0VENTURA/sptnr"
        }
        
        import time
        response = session.get(
            "https://api.discogs.com/database/search",
            params={"q": query, "type": "release", "per_page": 5},
            headers=headers,
            timeout=(5, 10)
        )
        
        # Handle rate limiting
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 60))
            time.sleep(min(retry_after, 5))  # Cap at 5 seconds
            response = session.get(
                "https://api.discogs.com/database/search",
                params={"q": query, "type": "release", "per_page": 5},
                headers=headers,
                timeout=(5, 10)
            )
        
        # Check for auth errors
        if response.status_code == 401:
            return jsonify({"error": "Discogs API authentication failed: invalid or expired token"}), 401
        elif response.status_code == 403:
            return jsonify({"error": "Discogs API access forbidden: check token permissions"}), 403
        
        if not response.ok:
            return jsonify({"error": f"Discogs API error: {response.status_code}"}), 500
        
        results_data = response.json().get("results", [])
        
        if not results_data:
            return jsonify({"results": [], "message": "No Discogs matches found"}), 200
        
        # Format results
        formatted_results = []
        for result in results_data[:5]:
            # Check if format includes "Single" to detect singles
            formats = result.get("format", [])
            is_single_release = "Single" in formats if formats else False
            
            formatted_results.append({
                "title": result.get("title", "Unknown"),
                "year": result.get("year", ""),
                "genre": result.get("genre", []),
                "style": result.get("style", []),
                "format": formats,
                "is_single": is_single_release,
                "url": result.get("resource_url", ""),
                "source": "discogs",
                "discogs_id": result.get("id", "")
            })
        
        return jsonify({"results": formatted_results}), 200
    except Exception as e:
        logger = logging.getLogger('sptnr')
        logger.error(f"Discogs lookup error: {e}")
        import traceback
        logger.debug(f"Traceback: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/track/<track_id>", methods=["GET"])
def api_get_track(track_id):
    """Get track metadata by ID"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, title, artist, album, genres, stars, is_single, 
                   single_confidence, duration, track_number, disc_number,
                   year, album_artist, composer, comment, mbid, file_path
            FROM tracks 
            WHERE id = ?
        """, (track_id,))
        
        row = cursor.fetchone()
        conn.close()
        
        if not row:
            return jsonify({"error": "Track not found"}), 404
        
        # Safely convert row to dict
        try:
            if hasattr(row, 'keys'):
                # Row is already a dict-like object (from Row factory)
                track = dict(row)
            else:
                # Row is a tuple, build dict manually
                track = {
                    'id': row[0],
                    'title': row[1],
                    'artist': row[2],
                    'album': row[3],
                    'genres': row[4],
                    'stars': row[5],
                    'is_single': row[6],
                    'single_confidence': row[7],
                    'duration': row[8],
                    'track_number': row[9],
                    'disc_number': row[10],
                    'year': row[11],
                    'album_artist': row[12],
                    'composer': row[13],
                    'comment': row[14],
                    'mbid': row[15],
                    'file_path': row[16]
                }
        except (IndexError, TypeError) as e:
            logging.error(f"[API] Error converting track row to dict: {e}")
            return jsonify({"error": "Failed to process track data"}), 500
            
        return jsonify(track)
    except Exception as e:
        logging.error(f"[API] Error fetching track {track_id}: {e}")
        import traceback
        logging.error(traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route("/api/track/genre-recommendations", methods=["GET"])
def track_genre_recommendations():
    """Get genre recommendations for a track from various sources"""
    track_id = request.args.get("track_id", "").strip()
    
    if not track_id:
        return jsonify({"error": "Track ID is required"}), 400
    
    try:
        # Get track info
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT artist, album, title, 
                   spotify_genres, lastfm_tags, discogs_genres, 
                   musicbrainz_genres, navidrome_genres
            FROM tracks 
            WHERE id = ?
        """, (track_id,))
        
        track = cursor.fetchone()
        conn.close()
        
        if not track:
            return jsonify({"error": "Track not found"}), 404
        
        # Collect all genres from various sources
        genres_set = set()
        
        # Parse genres from all available sources
        for genre_field in [track["spotify_genres"], track["lastfm_tags"], 
                           track["discogs_genres"], track["musicbrainz_genres"], 
                           track["navidrome_genres"]]:
            if genre_field:
                # Handle both comma-separated strings and JSON arrays
                try:
                    genre_list = json.loads(genre_field)
                    if isinstance(genre_list, list):
                        genres_set.update(g.strip() for g in genre_list if g and g.strip())
                except:
                    # Handle backslash-separated (from Navidrome) and comma-separated
                    delimiter = '\\' if '\\' in str(genre_field) else ','
                    genres_set.update(g.strip() for g in str(genre_field).split(delimiter) if g and g.strip())
        
        # Get artist-level genres
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT spotify_genres, lastfm_tags, discogs_genres, 
                   musicbrainz_genres, navidrome_genres
            FROM tracks 
            WHERE artist = ?
            LIMIT 50
        """, (track["artist"],))
        
        artist_tracks = cursor.fetchall()
        conn.close()
        
        # Aggregate artist genres
        artist_genres_count = {}
        for t in artist_tracks:
            for genre_field in [t["spotify_genres"], t["lastfm_tags"], 
                               t["discogs_genres"], t["musicbrainz_genres"], 
                               t["navidrome_genres"]]:
                if genre_field:
                    try:
                        genre_list = json.loads(genre_field)
                        if isinstance(genre_list, list):
                            for g in genre_list:
                                if g and g.strip():
                                    artist_genres_count[g.strip()] = artist_genres_count.get(g.strip(), 0) + 1
                    except:
                        # Handle backslash-separated (from Navidrome) and comma-separated
                        delimiter = '\\' if '\\' in str(genre_field) else ','
                        for g in str(genre_field).split(delimiter):
                            if g and g.strip():
                                artist_genres_count[g.strip()] = artist_genres_count.get(g.strip(), 0) + 1
        
        # Sort by frequency and add top artist genres
        sorted_artist_genres = sorted(artist_genres_count.items(), key=lambda x: x[1], reverse=True)
        genres_set.update(g[0] for g in sorted_artist_genres[:10])
        
        # Clean up and format genres
        recommendations = []
        for genre in sorted(genres_set):
            # Skip empty, very short, or malformed genres
            if genre and len(genre) > 2 and not genre.isdigit():
                # Capitalize properly
                formatted = ' '.join(word.capitalize() for word in genre.split())
                if formatted not in recommendations:
                    recommendations.append(formatted)
        
        return jsonify({
            "track_id": track_id,
            "artist": track["artist"],
            "title": track["title"],
            "recommendations": recommendations[:20]  # Limit to top 20
        })
        
    except Exception as e:
        logging.error(f"Error fetching genre recommendations: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/album/<path:artist>/<path:album>/track-recommendations", methods=["GET"])
def api_album_track_recommendations(artist, album):
    """Get genre recommendations for all tracks in an album
    
    Returns recommendations from Spotify, MusicBrainz, Discogs, LastFM, and Navidrome sources.
    """
    logger = logging.getLogger('sptnr')
    try:
        from urllib.parse import unquote
        artist = unquote(artist)
        album = unquote(album)
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Get all tracks in the album with their metadata and genres
        cursor.execute("""
            SELECT 
                id, title, artist, track_number,
                spotify_genres, spotify_artist_genres,
                lastfm_tags,
                discogs_genres, discogs_artist_genres,
                musicbrainz_genres, musicbrainz_artist_genres,
                navidrome_genres
            FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = ? AND album = ?
            ORDER BY COALESCE(disc_number, 1), COALESCE(track_number, 999), title COLLATE NOCASE
        """, (artist, album))
        
        tracks_data = cursor.fetchall()
        conn.close()
        
        if not tracks_data:
            return jsonify({"error": "Album not found"}), 404
        
        # Build recommendations for each track
        recommendations_by_track = {}
        
        for track in tracks_data:
            track_id = track['id'] if isinstance(track, dict) else track[0]
            track_title = track['title'] if isinstance(track, dict) else track[1]
            
            # Collect genres from all sources
            genres_set = set()
            
            # Parse each genre field
            genre_fields = {
                'spotify_genres': track['spotify_genres'] if isinstance(track, dict) else track[4],
                'spotify_artist_genres': track['spotify_artist_genres'] if isinstance(track, dict) else track[5],
                'lastfm_tags': track['lastfm_tags'] if isinstance(track, dict) else track[6],
                'discogs_genres': track['discogs_genres'] if isinstance(track, dict) else track[7],
                'discogs_artist_genres': track['discogs_artist_genres'] if isinstance(track, dict) else track[8],
                'musicbrainz_genres': track['musicbrainz_genres'] if isinstance(track, dict) else track[9],
                'musicbrainz_artist_genres': track['musicbrainz_artist_genres'] if isinstance(track, dict) else track[10],
                'navidrome_genres': track['navidrome_genres'] if isinstance(track, dict) else track[11],
            }
            
            # Extract genres from all fields
            for field_name, field_value in genre_fields.items():
                if field_value:
                    try:
                        # Ensure field_value is a string
                        if isinstance(field_value, dict):
                            # If it's already a dict (unlikely), skip it
                            continue
                        if not isinstance(field_value, str):
                            field_value = str(field_value)
                        
                        # Try to parse as JSON array
                        try:
                            genre_list = json.loads(field_value)
                            if isinstance(genre_list, list):
                                for g in genre_list:
                                    genre_str = None
                                    if isinstance(g, str):
                                        genre_str = g
                                    elif isinstance(g, dict):
                                        # Try to extract from dict (could be {"name": "Rock"} or {"genre": "Rock"})
                                        genre_str = g.get('name') or g.get('genre') or g.get('title') or str(g)
                                    else:
                                        genre_str = str(g)
                                    
                                    if genre_str and isinstance(genre_str, str) and len(genre_str.strip()) > 2:
                                        genres_set.add(genre_str.strip())
                            elif isinstance(genre_list, dict):
                                # If parsed JSON is a dict, try to extract 'genres' key
                                if 'genres' in genre_list and isinstance(genre_list['genres'], list):
                                    for g in genre_list['genres']:
                                        genre_str = g if isinstance(g, str) else str(g)
                                        if genre_str and len(genre_str.strip()) > 2:
                                            genres_set.add(genre_str.strip())
                        except (json.JSONDecodeError, TypeError):
                            # Handle comma or backslash-separated genres
                            delimiter = '\\' if '\\' in field_value else ','
                            for g in field_value.split(delimiter):
                                if g and isinstance(g, str) and len(g.strip()) > 2:
                                    genres_set.add(g.strip())
                    except Exception as e:
                        logger.debug(f"Error processing genre field {field_name}: {e}")
                        continue
            
            # Format and deduplicate genres
            recommendations = []
            for genre in sorted(genres_set):
                # Capitalize properly
                formatted = ' '.join(word.capitalize() for word in genre.split())
                if formatted not in recommendations:
                    recommendations.append(formatted)
            
            recommendations_by_track[str(track_id)] = {
                'title': track_title,
                'recommendations': recommendations[:15]  # Limit to top 15
            }
        
        return jsonify({
            "success": True,
            "album": album,
            "artist": artist,
            "recommendations": recommendations_by_track
        }), 200
        
    except Exception as e:
        logger.error(f"Error fetching track recommendations: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/album/musicbrainz", methods=["POST"])
def api_album_musicbrainz_lookup():
    """Lookup album on MusicBrainz for multiple matches (Picard-style) with retry logic"""
    logger = logging.getLogger('sptnr')
    try:
        data = request.get_json(force=True, silent=True)
        if not data:
            return jsonify({"error": "Invalid JSON in request body"}), 400
        album = data.get("album", "")
        artist = data.get("artist", "")
        
        if not album or not artist:
            return jsonify({"error": "Missing album or artist"}), 400
        
        # Search MusicBrainz for release groups using shared retry session
        query = f'release:"{album}" AND artist:"{artist}"'
        headers = {"User-Agent": MUSICBRAINZ_USER_AGENT}
        
        # Use shared retry session with built-in retry logic and SSL error handling
        # The session automatically retries on SSL, connection, and timeout errors
        # Using context manager to ensure session is properly closed
        with create_retry_session(
            retries=3,
            backoff=1.0,
            status_forcelist=(429, 500, 502, 503, 504)
        ) as session:
            try:
                resp = session.get(
                    "https://musicbrainz.org/ws/2/release-group",
                    params={"query": query, "fmt": "json", "limit": 10},
                    headers=headers,
                    timeout=(5, 10)  # (connect_timeout, read_timeout)
                )
                resp.raise_for_status()
                data = resp.json()
                release_groups = data.get("release-groups", []) or []
            except requests.exceptions.RequestException as e:
                # This catches errors after all retry attempts are exhausted
                # Use warning level for transient network issues (not critical errors)
                logger.warning(f"MusicBrainz album lookup unavailable after retries: {e}")
                return jsonify({
                    "error": f"MusicBrainz connection failed. Try Discogs instead.",
                    "results": []
                }), 503
        
        if not release_groups:
            return jsonify({"results": [], "message": "No MusicBrainz album matches found"}), 200
        
        # Format results with similarity scores
        import difflib
        results = []
        for rg in release_groups:
            rg_id = rg.get("id", "")
            rg_title = rg.get("title", "")
            primary_type = rg.get("primary-type", "Album")
            first_release = rg.get("first-release-date", "")
            
            # Get artist credit
            artist_credit = rg.get("artist-credit", [])
            rg_artist = artist_credit[0].get("name", "") if artist_credit else ""
            
            # Calculate similarity scores
            title_similarity = difflib.SequenceMatcher(None, album.lower(), rg_title.lower()).ratio()
            artist_similarity = difflib.SequenceMatcher(None, artist.lower(), rg_artist.lower()).ratio()
            overall_confidence = (title_similarity * 0.7 + artist_similarity * 0.3)
            
            # Get cover art URL
            cover_art_url = f"https://coverartarchive.org/release-group/{rg_id}/front-250" if rg_id else ""
            
            results.append({
                "mbid": rg_id,
                "title": rg_title,
                "artist": rg_artist,
                "primary_type": primary_type,
                "first_release_date": first_release,
                "cover_art_url": cover_art_url,
                "confidence": round(overall_confidence, 3),
                "title_similarity": round(title_similarity, 3),
                "artist_similarity": round(artist_similarity, 3),
                "source": "musicbrainz"
            })
        
        # Sort by confidence
        results.sort(key=lambda x: x["confidence"], reverse=True)
        
        return jsonify({"results": results[:10]}), 200
            
    except Exception as e:
        logger = logging.getLogger('sptnr')
        logger.error(f"MusicBrainz album lookup error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/album/discogs", methods=["POST"])
def api_album_discogs_lookup():
    """Lookup album on Discogs for better metadata and genres"""
    try:
        from popularity import _discogs_search, _get_discogs_session
        import difflib
        
        data = request.get_json(force=True, silent=True)
        if not data:
            return jsonify({"error": "Invalid JSON in request body"}), 400
        album = data.get("album", "")
        artist = data.get("artist", "")
        
        if not album or not artist:
            return jsonify({"error": "Missing album or artist"}), 400
        
        # Get Discogs config for token
        config_data, _ = _read_yaml(CONFIG_PATH)
        discogs_config = config_data.get("api_integrations", {}).get("discogs", {})
        discogs_token = discogs_config.get("token", "")
        
        # Search Discogs with multiple query strategies
        session = _get_discogs_session()
        headers = {"User-Agent": "Sptnr/1.0"}
        if discogs_token:
            headers["Authorization"] = f"Discogs token={discogs_token}"
        
        # Try different query formats to improve match rate
        queries = [
            f"{artist} {album}",  # Full query
            f'artist:"{artist}" release:"{album}"',  # Structured query
            f'{artist} "{album}"',  # Quoted album
        ]
        
        results = []
        for query in queries:
            logger = logging.getLogger('sptnr')
            logger.debug(f"Discogs search attempt: {query}")
            results = _discogs_search(session, headers, query, kind="release", per_page=10)
            if results:
                logger.debug(f"Discogs search found {len(results)} results")
                break
            logger.debug(f"Discogs search with query '{query}' returned no results")
        
        if not results:
            return jsonify({"results": [], "message": "No Discogs album matches found"}), 200
        
        # Format results with similarity scoring
        formatted_results = []
        for result in results[:10]:
            result_title = result.get("title", "Unknown")
            # Calculate similarity to improve ordering
            title_sim = difflib.SequenceMatcher(None, album.lower(), result_title.lower()).ratio()
            artist_part = result_title.split("-")[0] if "-" in result_title else result_title
            artist_sim = difflib.SequenceMatcher(None, artist.lower(), artist_part.lower()).ratio()
            overall_conf = (title_sim * 0.7 + artist_sim * 0.3)
            
            formatted_results.append({
                "title": result_title,
                "year": result.get("year", ""),
                "genre": result.get("genre", []),
                "style": result.get("style", []),
                "format": result.get("format", []),
                "url": result.get("resource_url", ""),
                "discogs_id": result.get("id", ""),
                "confidence": round(overall_conf, 3),
                "source": "discogs"
            })
        
        # Sort by confidence
        formatted_results.sort(key=lambda x: x["confidence"], reverse=True)
        
        return jsonify({"results": formatted_results}), 200
    except Exception as e:
        logger = logging.getLogger('sptnr')
        logger.error(f"Discogs lookup error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/album/spotify-genres", methods=["POST"])
def api_album_spotify_genres():
    """Get Spotify genres for an album from database"""
    logger = logging.getLogger('sptnr')
    try:
        data = request.get_json(force=True, silent=True)
        if not data:
            return jsonify({"error": "Invalid JSON in request body"}), 400
        album = data.get("album", "")
        artist = data.get("artist", "")
        
        if not album or not artist:
            return jsonify({"error": "Missing album or artist"}), 400
        
        # Get Spotify artist genres from tracks in this album
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT spotify_artist_genres
            FROM tracks
            WHERE artist = ? AND album = ? 
            AND spotify_artist_genres IS NOT NULL 
            AND spotify_artist_genres != ''
        """, (artist, album))
        
        genre_rows = cursor.fetchall()
        conn.close()
        
        # Aggregate unique genres
        genres = set()
        for row in genre_rows:
            try:
                genre_value = row[0] if row else None
                if genre_value:
                    # Parse JSON array
                    genre_list = json.loads(genre_value)
                    if isinstance(genre_list, list):
                        genres.update(genre_list)
            except (json.JSONDecodeError, IndexError, TypeError) as e:
                logger.debug(f"Error parsing Spotify genres: {e}")
                continue
        
        return jsonify({"genres": sorted(list(genres))}), 200
    except Exception as e:
        logger.error(f"Spotify genres lookup error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/album/apply-mbid", methods=["POST"])
def api_album_apply_mbid():
    """Apply MusicBrainz ID and cover art to all tracks in an album"""
    try:
        data = request.get_json()
        artist = data.get("artist", "")
        album = data.get("album", "")
        mbid = data.get("mbid", "")
        cover_art_url = data.get("cover_art_url", "")
        
        if not artist or not album:
            return jsonify({"error": "Missing artist or album"}), 400
        
        conn = get_db()
        cursor = conn.cursor()

        # Detect compatible MBID column for mixed schema deployments.
        cursor.execute("PRAGMA table_info(tracks)")
        track_columns = {row[1] for row in cursor.fetchall()}
        mb_album_column = None
        if "musicbrainz_album_mbid" in track_columns:
            mb_album_column = "musicbrainz_album_mbid"
        elif "beets_album_mbid" in track_columns:
            mb_album_column = "beets_album_mbid"
        
        # Update all tracks in this album with MBID and cover art
        updates = []
        if mbid:
            updates.append("mbid = ?")
            if mb_album_column:
                updates.append(f"{mb_album_column} = ?")
        if cover_art_url:
            updates.append("cover_art_url = ?")
        
        if not updates:
            return jsonify({"error": "No data to update"}), 400
        
        query = (
            f"UPDATE tracks SET {', '.join(updates)} "
            "WHERE COALESCE(NULLIF(album_artist, ''), artist) = ? AND album = ?"
        )
        params = []
        if mbid:
            params.append(mbid)
            if mb_album_column:
                params.append(mbid)  # Same ID for both fields
        if cover_art_url:
            params.append(cover_art_url)
        params.extend([artist, album])
        
        cursor.execute(query, params)
        rows_updated = cursor.rowcount
        conn.commit()
        conn.close()
        
        logging.info(f"Applied MBID {mbid} to {rows_updated} tracks in {artist} - {album}")
        
        return jsonify({
            "success": True,
            "message": f"Updated {rows_updated} tracks with MBID and cover art",
            "rows_updated": rows_updated
        }), 200
    except Exception as e:
        logger = logging.getLogger('sptnr')
        logger.error(f"Apply MBID error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/album/apply-discogs-id", methods=["POST"])
def api_album_apply_discogs_id():
    """Apply Discogs ID to all tracks in an album"""
    import time
    logger = logging.getLogger('sptnr')
    try:
        data = request.get_json()
        artist = data.get("artist", "")
        album = data.get("album", "")
        discogs_id = data.get("discogs_id", "")
        is_single = data.get("is_single", False)  # Check if Discogs marked this as Single
        
        if not artist or not album or not discogs_id:
            return jsonify({"error": "Missing required fields"}), 400
        
        # Retry logic for database lock
        max_retries = 3
        retry_delay = 0.5
        rows_updated = 0
        last_error = None
        
        for attempt in range(max_retries):
            try:
                conn = get_db()
                conn.isolation_level = None  # Autocommit mode
                cursor = conn.cursor()
                
                # Update all tracks in this album with Discogs ID and is_single flag if detected
                if is_single:
                    # If Discogs detected this as a Single, mark tracks as singles with high confidence and set 5★ rating
                    cursor.execute(
                        "UPDATE tracks SET discogs_album_id = ?, is_single = 1, single_confidence = 'high', single_sources = CASE WHEN single_sources IS NULL THEN 'discogs' ELSE single_sources || ',discogs' END, stars = 5 WHERE artist = ? AND album = ?",
                        (discogs_id, artist, album)
                    )
                else:
                    # Just update the Discogs ID
                    cursor.execute(
                        "UPDATE tracks SET discogs_album_id = ? WHERE artist = ? AND album = ?",
                        (discogs_id, artist, album)
                    )
                
                rows_updated = cursor.rowcount
                conn.commit()
                conn.close()
                
                if is_single:
                    logger.info(f"Updated {rows_updated} tracks with Discogs ID {discogs_id} and marked as single with 5★ rating for {artist} - {album}")
                else:
                    logger.info(f"Updated {rows_updated} tracks with Discogs ID {discogs_id} for {artist} - {album}")
                
                return jsonify({
                    "success": True,
                    "message": f"Updated {rows_updated} tracks with Discogs ID" + (" and marked as single with 5★ rating" if is_single else ""),
                    "rows_updated": rows_updated
                }), 200
            except sqlite3.OperationalError as e:
                last_error = e
                if "database is locked" in str(e):
                    if attempt < max_retries - 1:
                        logger.warning(f"Database locked on apply-discogs-id, retry {attempt + 1}/{max_retries}")
                        time.sleep(retry_delay)
                        retry_delay *= 2  # Exponential backoff
                    else:
                        logger.error(f"Database locked after {max_retries} retries: {e}")
                else:
                    raise
            except Exception as inner_e:
                last_error = inner_e
                raise
        
        # If we get here, all retries failed
        logger.error(f"Apply Discogs ID failed after {max_retries} retries: {last_error}")
        return jsonify({"error": f"Database locked. Please try again."}), 503
    except Exception as e:
        logger = logging.getLogger('sptnr')
        logger.error(f"Apply Discogs ID error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/album/majority-artist", methods=["POST"])
def api_album_majority_artist():
    """Get the most common artist from all tracks in an album"""
    from collections import Counter
    try:
        data = request.get_json()
        artist = data.get("artist", "").strip()
        album = data.get("album", "").strip()
        
        if not artist or not album:
            return jsonify({"error": "Missing required fields"}), 400
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Get all track artists from this album
        cursor.execute("""
            SELECT artist FROM tracks 
            WHERE album = ? 
            ORDER BY track_number ASC
        """, (album,))
        
        tracks = cursor.fetchall()
        conn.close()
        
        if not tracks:
            # No tracks found, return the album artist as default
            return jsonify({"majority_artist": artist}), 200
        
        # Count occurrences of each artist
        artists = [t['artist'] for t in tracks]
        artist_counts = Counter(artists)
        
        # Get the most common artist
        most_common_artist = artist_counts.most_common(1)[0][0] if artist_counts else artist
        
        # Also return the count and total tracks for reference
        return jsonify({
            "majority_artist": most_common_artist,
            "count": artist_counts[most_common_artist],
            "total_tracks": len(artists)
        }), 200
    except Exception as e:
        logger = logging.getLogger('sptnr')
        logger.error(f"Get majority artist error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/album/apply-genres", methods=["POST"])
def api_album_apply_genres():
    """Apply selected genres to all audio files in an album"""
    logger = logging.getLogger('sptnr')
    conn = None
    try:
        data = request.get_json()
        artist = data.get("artist", "").strip()
        album = data.get("album", "").strip()
        genres = data.get("genres", [])
        
        if not artist or not album or not genres:
            return jsonify({"error": "Missing required fields"}), 400
        
        # Get all tracks in the album from database
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, title, file_path
            FROM tracks
            WHERE artist = ? AND album = ?
        """, (artist, album))
        tracks = cursor.fetchall()
        
        if not tracks:
            return jsonify({"error": "No tracks found in album"}), 404
        
        # Write genres to audio files using mutagen and update database per-track
        updated_count = 0
        failed_files = []
        genres_str = ','.join(genres)
        
        for track in tracks:
            # Prefer beets_path, fallback to file_path
            file_path = row_get(track, 'file_path')
            
            if not file_path or not os.path.exists(file_path):
                track_id = row_get(track, 'id', 'unknown')
                track_title = row_get(track, 'title', '')
                failed_files.append(track_title if track_title else f"Track ID: {track_id}")
                continue
            
            # Update audio file using helper function
            success, error = update_audio_file_genres(file_path, genres)
            
            if success:
                # Update database only after successful file update
                try:
                    cursor.execute("""
                        UPDATE tracks
                        SET genres = ?
                        WHERE id = ?
                    """, (genres_str, row_get(track, 'id')))
                    updated_count += 1
                except Exception as db_error:
                    logger.error(f"Failed to update database for {file_path}: {db_error}")
                    track_title = row_get(track, 'title', '')
                    failed_files.append(track_title if track_title else f"Track ID: {row_get(track, 'id')}")
            else:
                # Check if error is due to unsupported format or actual failure
                if error and "Unsupported format" in error:
                    logger.debug(f"Skipped {file_path}: {error}")
                    # Still update database for unsupported formats
                    try:
                        cursor.execute("""
                            UPDATE tracks
                            SET genres = ?
                            WHERE id = ?
                        """, (genres_str, row_get(track, 'id')))
                        updated_count += 1
                    except Exception as db_error:
                        logger.error(f"Failed to update database for {file_path}: {db_error}")
                        track_title = row_get(track, 'title', '')
                        failed_files.append(track_title if track_title else f"Track ID: {row_get(track, 'id')}")
                else:
                    logger.error(f"Failed to update {file_path}: {error}")
                    track_id = row_get(track, 'id', 'unknown')
                    track_title = row_get(track, 'title', '')
                    failed_files.append(track_title if track_title else f"Track ID: {track_id}")
        
        conn.commit()
        
        message = f"Updated {updated_count} audio file(s) with genres: {', '.join(genres)}"
        if failed_files:
            message += f". Failed to update {len(failed_files)} file(s): {', '.join(failed_files[:5])}"
        
        logger.info(f"Applied genres {genres} to album '{album}' by {artist}: {updated_count} files updated")
        
        return jsonify({
            "success": True,
            "message": message,
            "updated_count": updated_count,
            "failed_count": len(failed_files)
        }), 200
        
    except Exception as e:
        logger.error(f"Apply genres error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/album/add-to-missing-releases", methods=["POST"])
def api_album_add_to_missing_releases():
    """Add an album to the missing releases tracking list"""
    try:
        data = request.get_json()
        artist = data.get("artist", "").strip()
        album = data.get("album", "").strip()
        year = data.get("year", "").strip()
        
        if not artist or not album:
            return jsonify({"error": "Artist and album are required"}), 400
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Check if missing_releases table exists
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='missing_releases'
        """)
        if not cursor.fetchone():
            conn.close()
            return jsonify({"error": "Missing releases table not found"}), 500
        
        # Insert or update the album in missing_releases
        cursor.execute("""
            INSERT OR REPLACE INTO missing_releases 
            (artist, release_id, title, first_release_date, category, last_checked)
            VALUES (?, ?, ?, ?, 'Album', CURRENT_TIMESTAMP)
        """, (
            artist,
            f"{artist}-{album}".lower().replace(" ", "-"),  # Use artist-album as release_id if MusicBrainz ID not available
            album,
            year if year else None
        ))
        
        conn.commit()
        conn.close()
        
        logging.info(f"Added {album} by {artist} to missing releases")
        
        return jsonify({
            "success": True,
            "message": f"Added '{album}' by {artist} to missing releases tracking"
        }), 200
        
    except Exception as e:
        logging.error(f"Error adding album to missing releases: {e}")
        import traceback
        logging.error(traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route("/api/artist/apply-genres", methods=["POST"])
def api_artist_apply_genres():
    """Apply selected genres to all audio files for all tracks by an artist"""
    logger = logging.getLogger('sptnr')
    conn = None
    try:
        data = request.get_json()
        artist = data.get("artist", "").strip()
        genres = data.get("genres", [])
        
        if not artist or not genres:
            return jsonify({"error": "Missing required fields"}), 400
        
        # Get all tracks by the artist from database
        # Use COALESCE with album_artist to match artist page logic
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, title, album, file_path
            FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = ? OR artist = ?
        """, (artist, artist))
        tracks = cursor.fetchall()
        
        if not tracks:
            return jsonify({"error": "No tracks found for artist"}), 404
        
        # Write genres to audio files using mutagen and update database per-track
        updated_count = 0
        failed_files = []
        genres_str = ','.join(genres)
        
        for track in tracks:
            # Prefer beets_path, fallback to file_path
            file_path = row_get(track, 'file_path')
            
            if not file_path or not os.path.exists(file_path):
                track_id = row_get(track, 'id', 'unknown')
                album = row_get(track, 'album', '')
                title = row_get(track, 'title', '')
                if album and title:
                    failed_files.append(f"{album} - {title}")
                else:
                    failed_files.append(f"Track ID: {track_id}")
                continue
            
            # Update audio file using helper function
            success, error = update_audio_file_genres(file_path, genres)
            
            if success:
                # Update database only after successful file update
                try:
                    cursor.execute("""
                        UPDATE tracks
                        SET genres = ?
                        WHERE id = ?
                    """, (genres_str, row_get(track, 'id')))
                    updated_count += 1
                except Exception as db_error:
                    logger.error(f"Failed to update database for {file_path}: {db_error}")
                    album = row_get(track, 'album', '')
                    title = row_get(track, 'title', '')
                    if album and title:
                        failed_files.append(f"{album} - {title}")
                    else:
                        failed_files.append(f"Track ID: {row_get(track, 'id')}")
            else:
                # Check if error is due to unsupported format or actual failure
                if error and "Unsupported format" in error:
                    logger.debug(f"Skipped {file_path}: {error}")
                    # Still update database for unsupported formats
                    try:
                        cursor.execute("""
                            UPDATE tracks
                            SET genres = ?
                            WHERE id = ?
                        """, (genres_str, row_get(track, 'id')))
                        updated_count += 1
                    except Exception as db_error:
                        logger.error(f"Failed to update database for {file_path}: {db_error}")
                        album = row_get(track, 'album', '')
                        title = row_get(track, 'title', '')
                        if album and title:
                            failed_files.append(f"{album} - {title}")
                        else:
                            failed_files.append(f"Track ID: {row_get(track, 'id')}")
                else:
                    logger.error(f"Failed to update {file_path}: {error}")
                    track_id = row_get(track, 'id', 'unknown')
                    album = row_get(track, 'album', '')
                    title = row_get(track, 'title', '')
                    if album and title:
                        failed_files.append(f"{album} - {title}")
                    else:
                        failed_files.append(f"Track ID: {track_id}")
        
        conn.commit()
        
        message = f"Updated {updated_count} audio file(s) across all albums by {artist} with genres: {', '.join(genres)}"
        if failed_files:
            message += f". Failed to update {len(failed_files)} file(s)"
        
        logger.info(f"Applied genres {genres} to all tracks by artist '{artist}': {updated_count} files updated")
        
        return jsonify({
            "success": True,
            "message": message,
            "updated_count": updated_count,
            "failed_count": len(failed_files),
            "total_tracks": len(tracks)
        }), 200
        
    except Exception as e:
        logger.error(f"Apply genres error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route("/api/track/musicbrainz", methods=["POST"])
def api_track_musicbrainz_lookup():
    """Lookup track on MusicBrainz for multiple matches (Picard-style) with retry logic"""
    logger = logging.getLogger('sptnr')
    try:
        data = request.get_json()
        title = data.get("title", "")
        artist = data.get("artist", "")
        
        if not title or not artist:
            return jsonify({"error": "Missing title or artist"}), 400
        
        # Search MusicBrainz for recordings with retry
        query = f'recording:"{title}" AND artist:"{artist}"'
        headers = {"User-Agent": MUSICBRAINZ_USER_AGENT}
        
        max_retries = 2
        for attempt in range(max_retries):
            try:
                resp = requests.get(
                    "https://musicbrainz.org/ws/2/recording",
                    params={"query": query, "fmt": "json", "limit": 10, "inc": "releases+artist-credits"},
                    headers=headers,
                    timeout=5
                )
                resp.raise_for_status()
                data = resp.json()
                recordings = data.get("recordings", []) or []
                
                if not recordings:
                    return jsonify({"results": [], "message": "No MusicBrainz track matches found"}), 200
                
                # Format results with similarity scores
                import difflib
                results = []
                for rec in recordings:
                    rec_id = rec.get("id", "")
                    rec_title = rec.get("title", "")
                    rec_length = rec.get("length", 0)  # in milliseconds
                    
                    # Get artist credit
                    artist_credit = rec.get("artist-credit", [])
                    rec_artist = artist_credit[0].get("name", "") if artist_credit else ""
                    
                    # Get releases (albums this appears on)
                    releases = rec.get("releases", []) or []
                    release_list = []
                    for rel in releases[:5]:
                        release_list.append({
                            "id": rel.get("id", ""),
                            "title": rel.get("title", "")
                        })
                    
                    # Calculate similarity scores
                    title_similarity = difflib.SequenceMatcher(None, title.lower(), rec_title.lower()).ratio()
                    artist_similarity = difflib.SequenceMatcher(None, artist.lower(), rec_artist.lower()).ratio()
                    overall_confidence = (title_similarity * 0.7 + artist_similarity * 0.3)
                    
                    results.append({
                        "mbid": rec_id,
                        "title": rec_title,
                        "artist": rec_artist,
                        "length": rec_length,
                        "releases": release_list,
                        "confidence": round(overall_confidence, 3),
                        "title_similarity": round(title_similarity, 3),
                        "artist_similarity": round(artist_similarity, 3),
                        "source": "musicbrainz"
                    })
                
                # Sort by confidence
                results.sort(key=lambda x: x["confidence"], reverse=True)
                
                return jsonify({"results": results[:10]}), 200
                
            except requests.exceptions.Timeout:
                logging.debug(f"MusicBrainz timeout (attempt {attempt+1}/{max_retries}) for {title} by {artist}")
                if attempt < max_retries - 1:
                    time.sleep(1 * (2 ** attempt))  # Exponential backoff
            except requests.exceptions.ConnectionError as e:
                logging.debug(f"MusicBrainz connection error (attempt {attempt+1}/{max_retries}): {type(e).__name__}")
                if attempt < max_retries - 1:
                    time.sleep(1 * (2 ** attempt))
            except Exception as e:
                logging.debug(f"MusicBrainz lookup error: {e}")
                if attempt == max_retries - 1:
                    return jsonify({"error": f"MusicBrainz lookup failed: {str(e)}"}), 500
                time.sleep(1)
        
        # Log at warning level for transient network issues (not critical errors)
        logger.warning(f"MusicBrainz track lookup unavailable after retries for '{title}' by '{artist}'")
        return jsonify({"error": "MusicBrainz connection failed. Try again later."}), 503
            
    except Exception as e:
        logger.error(f"MusicBrainz track lookup error: {e}")
        return jsonify({"error": str(e)}), 500

# ==========================================================================
# GENRE MANAGEMENT API ROUTES
# ==========================================================================

@app.route("/api/genres/remove", methods=["POST"])
def api_remove_genres():
    """
    Remove specific genres from artist or album's tracks
    Updates database only
    """
    try:
        data = request.get_json() or {}
        artist_name = data.get("artist_name", "").strip()
        album_name = data.get("album_name", "").strip()
        genres_to_remove = data.get("genres", [])
        
        if not artist_name and not album_name:
            return jsonify({"error": "artist_name or album_name required"}), 400
        
        if not genres_to_remove or not isinstance(genres_to_remove, list):
            return jsonify({"error": "genres must be a non-empty list"}), 400
        
        conn = sqlite3.connect(DB_PATH, timeout=120.0)
        cursor = conn.cursor()
        
        # Build WHERE clause
        if album_name:
            cursor.execute("SELECT id, title, genres FROM tracks WHERE artist = ? AND album = ?", 
                          (artist_name, album_name))
        else:
            cursor.execute("SELECT id, title, genres FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = ?", 
                          (artist_name,))
        
        rows = cursor.fetchall()
        affected_count = 0
        
        # Build list of updates in memory first
        for row in rows:
            track_id, title, genres_str = row
            if not genres_str:
                continue
            
            # Parse genres
            genre_list = [g.strip() for g in re.split(r'[\\,]+', genres_str)]
            
            # Remove specified genres (case-insensitive)
            genres_to_remove_lower = [g.lower() for g in genres_to_remove]
            filtered_genres = [
                g for g in genre_list
                if g.lower() not in genres_to_remove_lower
            ]
            
            # Only update if changes were made
            if len(filtered_genres) != len(genre_list):
                new_genres_str = '\\'.join(filtered_genres) if filtered_genres else ''
                
                # Update database
                cursor.execute("""
                    UPDATE tracks SET genres = ?
                    WHERE id = ?
                """, (new_genres_str, track_id))
                
                affected_count += 1
        
        conn.commit()
        conn.close()
        
        # Log bulk change
        if affected_count > 0:
            log_genre_update(
                artist_name=artist_name,
                album_name=album_name,
                track_id=None,
                genres_before='',
                genres_after='',
                action_type='remove_from_album' if album_name else 'remove_from_artist',
                affected_count=affected_count,
                change_summary=f"Removed genres from {affected_count} tracks: {', '.join(genres_to_remove)}"
            )
        
        # Trigger Navidrome scan in background
        def trigger_scan():
            try:
                from api_clients.navidrome import NavidromeClient
                cfg = get_config()
                navidrome_config = cfg.get("navidrome", {})
                
                if navidrome_config.get("base_url"):
                    client = NavidromeClient(
                        navidrome_config.get("base_url"),
                        navidrome_config.get("user"),
                        navidrome_config.get("password")
                    )
                    client.start_scan()
            except Exception as e:
                logging.error(f"Failed to trigger Navidrome scan: {e}")
        
        # Run scan in background thread
        scan_thread = threading.Thread(target=trigger_scan, daemon=True)
        scan_thread.start()
        
        return jsonify({
            "success": True,
            "affected_tracks": affected_count,
            "message": f"Removed genres from {affected_count} track(s)",
            "scan_triggered": True
        }), 200
        
    except Exception as e:
        logging.error(f"Error removing genres: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/track/update-metadata", methods=["POST"])
def api_track_update_metadata():
    """
    Update track metadata (comprehensive - all fields)
    
    Request body:
    {
        "track_id": "track_id_from_db",
        "title": "new title (optional)",
        "artist": "new artist (optional)",
        "album": "new album (optional)",
        "genres": "new genres separated by backslash (optional)",
        "stars": 0-5 (optional),
        "is_single": 0 or 1 (optional),
        "single_confidence": "low|medium|high" (optional),
        "year": "YYYY (optional)",
        "album_artist": "(optional)",
        "composer": "(optional)",
        "track_number": "(optional)",
        "disc_number": "(optional)",
        "comment": "(optional)",
        "mbid": "(optional)"
    }
    """
    conn = None
    try:
        data = request.get_json() or {}
        track_id = data.get("track_id", "").strip()
        sync_to_file = data.get("sync_to_file", True)
        
        if not track_id:
            return jsonify({"error": "track_id required"}), 400
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Build database update with provided fields
        db_updates = {}
        if 'title' in data and data['title'] is not None:
            db_updates['title'] = data['title'].strip()
        if 'artist' in data and data['artist'] is not None:
            db_updates['artist'] = data['artist'].strip()
        if 'album' in data and data['album'] is not None:
            db_updates['album'] = data['album'].strip()
        if 'genres' in data and data['genres'] is not None:
            db_updates['genres'] = data['genres'].strip()
        if 'stars' in data:
            db_updates['stars'] = int(data['stars']) if data['stars'] else 0
        if 'is_single' in data:
            db_updates['is_single'] = 1 if data['is_single'] else 0
        if 'single_confidence' in data and data['single_confidence'] is not None:
            db_updates['single_confidence'] = data['single_confidence'].strip()
        if 'year' in data and data['year'] is not None:
            db_updates['year'] = data['year'].strip() or None
        if 'album_artist' in data and data['album_artist'] is not None:
            db_updates['album_artist'] = data['album_artist'].strip() or None
        if 'composer' in data and data['composer'] is not None:
            db_updates['composer'] = data['composer'].strip() or None
        if 'track_number' in data and data['track_number'] is not None:
            db_updates['track_number'] = data['track_number'].strip() or None
        if 'disc_number' in data:
            db_updates['disc_number'] = int(data['disc_number']) if data['disc_number'] else None
        if 'comment' in data and data['comment'] is not None:
            db_updates['comment'] = data['comment'].strip() or None
        if 'mbid' in data and data['mbid'] is not None:
            db_updates['mbid'] = data['mbid'].strip() or None
        
        if not db_updates:
            conn.close()
            return jsonify({"error": "At least one field required"}), 400
        
        # Update database
        set_clause = ", ".join([f"{k} = ?" for k in db_updates.keys()])
        values = list(db_updates.values()) + [track_id]
        cursor.execute(f"UPDATE tracks SET {set_clause} WHERE id = ?", values)
        conn.commit()
        
        conn.close()
        conn = None

        # Sync tags back to file by default for album/artist/track editing flows.
        file_synced = False
        if sync_to_file:
            try:
                from helpers.tag_manager import sync_track_tags_to_file
                file_synced = sync_track_tags_to_file(track_id)
            except Exception as sync_error:
                logging.warning(f"Track metadata DB update succeeded but file sync failed for {track_id}: {sync_error}")
        
        return jsonify({
            "success": True,
            "track_id": track_id,
            "file_synced": file_synced,
            "changes": db_updates,
            "message": "Track metadata updated successfully"
        }), 200
            
    except Exception as e:
        logging.error(f"Error updating track metadata: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

@app.route("/api/navidrome/scan/start", methods=["POST"])
def api_start_navidrome_scan():
    """
    Trigger a library scan in Navidrome
    """
    try:
        from api_clients.navidrome import NavidromeClient
        cfg = get_config()
        navidrome_config = cfg.get("navidrome", {})
        
        if not navidrome_config.get("base_url"):
            return jsonify({"error": "Navidrome not configured"}), 400
        
        client = NavidromeClient(
            navidrome_config.get("base_url"),
            navidrome_config.get("user"),
            navidrome_config.get("password")
        )
        
        success = client.start_scan()
        
        if success:
            return jsonify({
                "success": True,
                "message": "Navidrome scan started successfully"
            }), 200
        else:
            return jsonify({
                "success": False,
                "error": "Failed to start Navidrome scan"
            }), 500
            
    except Exception as e:
        logging.error(f"Error starting Navidrome scan: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/navidrome/scan/status", methods=["GET"])
def api_get_navidrome_scan_status():
    """
    Get the current Navidrome library scan status
    """
    try:
        from api_clients.navidrome import NavidromeClient
        cfg = get_config()
        navidrome_config = cfg.get("navidrome", {})
        
        if not navidrome_config.get("base_url"):
            return jsonify({"error": "Navidrome not configured"}), 400
        
        client = NavidromeClient(
            navidrome_config.get("base_url"),
            navidrome_config.get("user"),
            navidrome_config.get("password")
        )
        
        status = client.get_scan_status()
        
        return jsonify(status), 200
            
    except Exception as e:
        logging.error(f"Error getting Navidrome scan status: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/navidrome/import/pre-sync-artists", methods=["POST"])
def api_pre_sync_navidrome_artists():
    """
    Pre-import sync: Batch fetch and sync new album artists from Navidrome
    
    This is a quick operation that identifies all unique album artists in Navidrome
    and adds any missing ones to the database in a single batch operation.
    Can be run before a full import to ensure all artists exist.
    
    Query params:
      - artist_id: Optional single artist ID to sync (instead of all artists)
    """
    try:
        from helpers.scan_helpers import pre_import_sync_album_artists
        
        artist_id = request.args.get('artist_id')
        
        result = pre_import_sync_album_artists(artist_id=artist_id)
        
        if 'error' in result:
            return jsonify(result), 400
        
        return jsonify(result), 200
            
    except Exception as e:
        logging.error(f"Error syncing Navidrome album artists: {e}")
        return jsonify({"error": str(e), "success": False}), 500

@app.route("/api/genres/recent-updates", methods=["GET"])
def api_recent_genre_updates():
    """
    Get recent genre updates for the logs page
    Returns last 50 updates with pagination support
    """
    try:
        limit = request.args.get("limit", 50, type=int)
        offset = request.args.get("offset", 0, type=int)
        
        conn = sqlite3.connect(DB_PATH, timeout=120.0)
        cursor = conn.cursor()
        
        # Get total count
        cursor.execute("SELECT COUNT(*) FROM genre_updates")
        total_count = cursor.fetchone()[0]
        
        # Get recent updates
        cursor.execute("""
            SELECT 
                id, artist_name, album_name, track_id, genres_before, genres_after,
                action_type, affected_track_count, change_summary, created_at
            FROM genre_updates
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        """, (limit, offset))
        
        rows = cursor.fetchall()
        conn.close()
        
        updates = []
        for row in rows:
            updates.append({
                "id": row[0],
                "artist": row[1],
                "album": row[2],
                "track_id": row[3],
                "genres_before": row[4],
                "genres_after": row[5],
                "action_type": row[6],
                "affected_count": row[7],
                "summary": row[8],
                "timestamp": row[9]
            })
        
        return jsonify({
            "updates": updates,
            "total": total_count,
            "limit": limit,
            "offset": offset
        }), 200
        
    except Exception as e:
        logging.error(f"Error fetching genre updates: {e}")
        return jsonify({"error": str(e)}), 500

# ==========================================================================
# PLAYLIST MANAGER ROUTES
# ==========================================================================

@app.route("/playlist-manager")
def playlist_manager():
    """Redirect to playlists browse (default page)"""
    return redirect("/playlists/browse")


@app.route("/playlists/browse")
def playlists_browse():
    """Browse playlists page"""
    cfg = get_config()
    navidrome_config = cfg.get("navidrome", {})
    navidrome_users = cfg.get("navidrome_users", [])
    
    # If navidrome_users not configured, use single user
    if not navidrome_users and navidrome_config.get("user"):
        navidrome_users = [{
            "base_url": navidrome_config.get("base_url"),
            "user": navidrome_config.get("user")
        }]
    
    return render_template('playlists_browse.html', 
                         navidrome_users=navidrome_users)


@app.route("/playlists/create/<playlist_type>")
def playlists_create(playlist_type):
    """Create playlist pages"""
    cfg = get_config()
    navidrome_config = cfg.get("navidrome", {})
    navidrome_users = cfg.get("navidrome_users", [])
    
    # If navidrome_users not configured, use single user
    if not navidrome_users and navidrome_config.get("user"):
        navidrome_users = [{
            "base_url": navidrome_config.get("base_url"),
            "user": navidrome_config.get("user")
        }]
    
    # Get top 20 most used genres for Smart Playlists section
    top_genres = []
    if playlist_type == 'smart':
        try:
            conn = get_db()
            cursor = conn.cursor()
            
            # Query to get all genres and count songs
            # Genres are stored as comma-separated values in the 'genres' field
            cursor.execute("""
                SELECT genres FROM tracks 
                WHERE genres IS NOT NULL AND genres != ''
            """)
            
            # Count genre occurrences
            genre_counts = {}
            for row in cursor.fetchall():
                genres_str = row[0]
                if genres_str:
                    # Split by comma and trim whitespace
                    genres_list = [g.strip() for g in genres_str.split(',') if g.strip()]
                    for genre in genres_list:
                        genre_counts[genre] = genre_counts.get(genre, 0) + 1
            
            # Sort by count (descending) and get top 20
            sorted_genres = sorted(genre_counts.items(), key=lambda x: x[1], reverse=True)[:20]
            top_genres = [{'name': genre, 'count': count} for genre, count in sorted_genres]
            
            conn.close()
        except Exception as e:
            logging.error(f"Error fetching top genres: {e}")
    
    # Check service configurations
    spotify_enabled = cfg.get("api_integrations", {}).get("spotify", {}).get("enabled", False)
    lastfm_enabled = cfg.get("api_integrations", {}).get("lastfm", {}).get("enabled", False)
    
    return render_template('playlists_create.html',
                         playlist_type=playlist_type,
                         navidrome_users=navidrome_users,
                         top_genres=top_genres,
                         spotify_enabled=spotify_enabled,
                         lastfm_enabled=lastfm_enabled)


@app.route("/playlists/import")
def playlists_import():
    """Import playlists page"""
    cfg = get_config()
    navidrome_config = cfg.get("navidrome", {})
    navidrome_users = cfg.get("navidrome_users", [])
    
    # If navidrome_users not configured, use single user
    if not navidrome_users and navidrome_config.get("user"):
        navidrome_users = [{
            "base_url": navidrome_config.get("base_url"),
            "user": navidrome_config.get("user")
        }]
    
    # Check service configurations
    spotify_enabled = cfg.get("api_integrations", {}).get("spotify", {}).get("enabled", False)
    lastfm_enabled = cfg.get("api_integrations", {}).get("lastfm", {}).get("enabled", False)
    
    return render_template('playlists_import.html',
                         navidrome_users=navidrome_users,
                         spotify_enabled=spotify_enabled,
                         lastfm_enabled=lastfm_enabled)


@app.route("/api/playlist/list")
def api_playlist_list():
    """List all playlists in Navidrome, including type and metadata"""
    try:
        cfg = get_config()
        current_user = session.get("username")
        navidrome_users = cfg.get("navidrome_users", [])
        nav_cfg = None

        # Multi-user support: find config for current user
        if navidrome_users and current_user:
            nav_cfg = next((u for u in navidrome_users if u.get("user") == current_user), None)
        
        # Fallback to legacy single-user config
        if not nav_cfg:
            nav_cfg = cfg.get("navidrome", {})
        
        base_url = nav_cfg.get("base_url", "http://localhost:4533")
        user = nav_cfg.get("user", "admin")
        password = nav_cfg.get("pass", "")
        
        if not (base_url and user and password):
            logging.error(f"Navidrome not configured: base_url={base_url}, user={user}, password={'set' if password else 'unset'}")
            return jsonify({"error": "Navidrome not configured. Please check your config file."}), 400
        
        import requests as req

        # Get playlists (regular and smart)
        try:
            playlists_response = req.get(
                f"{base_url}/rest/getPlaylists.view",
                params={"u": user, "p": password, "c": "sptnr", "f": "json"},
                timeout=10
            )
            playlists_data = playlists_response.json()
            playlists = []
            for playlist in playlists_data.get("subsonic-response", {}).get("playlists", {}).get("playlist", []):
                playlist_type = "smart" if playlist.get("isSmart") or playlist.get("criteria") else "regular"
                playlists.append({
                    "id": playlist.get("id"),
                    "name": playlist.get("name"),
                    "type": playlist_type,
                    "songCount": playlist.get("songCount"),
                    "owner": playlist.get("owner"),
                    "public": playlist.get("public"),
                    "created": playlist.get("created"),
                    "changed": playlist.get("changed"),
                    "comment": playlist.get("comment"),
                    "path": playlist.get("id")
                })
        except Exception as e:
            logging.error(f"Navidrome playlist fetch error: {e}")
            playlists = []

        # Optionally: scan for .nsp files in Playlists dir for local smart playlists
        import os, glob
        music_folder = os.environ.get("MUSIC_FOLDER", "/music")
        playlists_dir = os.path.join(music_folder, "Playlists")
        nsp_files = glob.glob(os.path.join(playlists_dir, "*.nsp"))
        for nsp_path in nsp_files:
            try:
                with open(nsp_path, "r", encoding="utf-8") as f:
                    nsp_data = json.load(f)
                playlists.append({
                    "id": os.path.basename(nsp_path),
                    "name": nsp_data.get("name", os.path.basename(nsp_path)),
                    "type": "smart-local",
                    "songCount": len(nsp_data.get("songs", [])),
                    "owner": nsp_data.get("owner", user),
                    "public": nsp_data.get("public", False),
                    "created": nsp_data.get("created"),
                    "changed": nsp_data.get("changed"),
                    "comment": nsp_data.get("description", ""),
                    "path": nsp_path
                })
            except Exception as e:
                logging.warning(f"Failed to load smart playlist file {nsp_path}: {e}")

        return jsonify({"playlists": playlists}), 200
    except Exception as e:
        logging.error(f"Error listing playlists: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 200

@app.route("/api/playlist/load", methods=["POST"])
def api_playlist_load():
    """Load playlist tracks"""
    try:
        data = request.get_json(force=True, silent=True) or {}
        playlist_id = data.get("playlist_path")
        if not playlist_id:
            return jsonify({"error": "Missing playlist_path"}), 200

        cfg = get_config()
        navidrome_config = cfg.get("navidrome", {})
        base_url = navidrome_config.get("base_url", "http://localhost:4533")
        user = navidrome_config.get("user", "admin")
        password = navidrome_config.get("pass", "")
        import requests as req

        # Get playlist tracks
        try:
            playlist_response = req.get(
                f"{base_url}/rest/getPlaylist.view",
                params={"u": user, "p": password, "c": "sptnr", "f": "json", "id": playlist_id},
                timeout=10
            )
            playlist_data = playlist_response.json().get("subsonic-response", {}).get("playlist", {})
            tracks = playlist_data.get("entry", [])
            if not isinstance(tracks, list):
                tracks = [tracks] if tracks else []
        except Exception as e:
            logging.error(f"Navidrome playlist fetch error: {e}")
            tracks = []

        songs = []
        matched_files = []
        for track in tracks:
            song = {
                "id": track.get("id"),
                "title": track.get("title", "Unknown"),
                "artist": track.get("artist", "Unknown"),
                "album": track.get("album", "Unknown"),
                "detected": True
            }
            songs.append(song)
            matched_files.append({
                "id": track.get("id"),
                "title": song["title"],
                "artist": song["artist"],
                "filename": track.get("path", "")
            })

        return jsonify({
            "playlist_path": playlist_id,
            "songs": songs,
            "matched_files": matched_files,
            "total": len(songs),
            "matched": len(matched_files)
        }), 200
    except Exception as e:
        logging.error(f"Error loading playlist: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 200

@app.route("/api/playlist/search-songs", methods=["POST"])
def api_playlist_search_songs():
    """Search for songs in library"""
    try:
        data = request.get_json() or {}
        raw_query = (data.get("query") or "").strip()
        artist = (data.get("artist") or "").strip()
        title = (data.get("title") or "").strip()
        album = (data.get("album") or "").strip()

        # Build a combined query string so Navidrome search remains happy
        search_terms = [t for t in [title, artist, album, raw_query] if t]
        query = " ".join(search_terms).strip()
        
        if not query or len(query) < 2:
            return jsonify({"error": "Query too short"}), 400
        
        cfg = get_config()
        nav_users = cfg.get("navidrome_users") or []
        if not nav_users:
            nav = cfg.get("navidrome", {}) or {}
            if nav.get("base_url"):
                nav_users = [nav]

        base_url = None
        user = None
        password = None
        if nav_users:
            nd = nav_users[0]
            base_url = nd.get("base_url") or nd.get("url") or "http://localhost:4533"
            user = nd.get("user") or nd.get("username") or "admin"
            password = nd.get("pass") or nd.get("password") or ""
        else:
            base_url = "http://localhost:4533"
            user = "admin"
            password = ""

        import requests as req

        # Build token auth if password available
        params = {
            "u": user,
            "c": "sptnr",
            "f": "json",
            "v": "1.16.0",
            "query": query,
            "songCount": 50,
        }

        if password:
            salt = secrets.token_hex(8)
            token = hashlib.md5((password + salt).encode()).hexdigest()
            params.update({"t": token, "s": salt})
        else:
            params["p"] = ""  # empty password to satisfy API

        results = []

        try:
            search_response = req.get(
                f"{base_url.rstrip('/')}/rest/search3.view",
                params=params,
                timeout=10,
            )

            response_data = search_response.json()
            if response_data.get("subsonic-response", {}).get("status") == "ok":
                search_data = response_data.get("subsonic-response", {}).get("searchResult3", {})
                songs = search_data.get("song", [])
                if not isinstance(songs, list):
                    songs = [songs] if songs else []
                for song in songs[:50]:
                    results.append({
                        "id": song.get("id"),
                        "title": song.get("title", "Unknown"),
                        "artist": song.get("artist", "Unknown"),
                        "album": song.get("album", "Unknown"),
                        "duration": song.get("duration", 0)
                    })
        except Exception as nav_err:
            logging.debug(f"Navidrome search failed, will fallback to local DB: {nav_err}")

        # Fallback: search local sptnr DB if Navidrome returned nothing
        if not results:
            try:
                conn = get_db()
                cursor = conn.cursor()

                where_clauses = []
                params = []

                if title:
                    where_clauses.append("LOWER(title) LIKE ?")
                    params.append(f"%{title.lower()}%")
                if artist:
                    where_clauses.append("LOWER(artist) LIKE ?")
                    params.append(f"%{artist.lower()}%")
                if album:
                    where_clauses.append("LOWER(album) LIKE ?")
                    params.append(f"%{album.lower()}%")

                if not where_clauses:
                    pattern = f"%{query.lower()}%"
                    where_clauses.append("(LOWER(title) LIKE ? OR LOWER(artist) LIKE ? OR LOWER(album) LIKE ?)")
                    params.extend([pattern, pattern, pattern])

                where_sql = " AND ".join(where_clauses)

                cursor.execute(
                    f"""
                    SELECT id, title, artist, album, duration
                    FROM tracks
                    WHERE {where_sql}
                    ORDER BY stars DESC NULLS LAST, title COLLATE NOCASE
                    LIMIT 50
                    """,
                    tuple(params),
                )
                for row in cursor.fetchall() or []:
                    results.append({
                        "id": row[0],
                        "title": row[1],
                        "artist": row[2],
                        "album": row[3],
                        "duration": row[4] or 0,
                    })
                conn.close()
            except Exception as db_err:
                logging.error(f"Local DB search failed: {db_err}")

        return jsonify({"songs": results}), 200
    except Exception as e:
        logging.error(f"Error searching songs: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route("/api/playlist/create-custom", methods=["POST"])
def api_playlist_create_custom():
    """Create a custom playlist in Navidrome"""
    try:
        data = request.get_json()
        name = data.get("name", "").strip()
        description = data.get("description", "").strip()
        user_name = data.get("user", "admin")
        is_public = data.get("is_public", False)
        songs = data.get("songs", [])
        
        if not name:
            return jsonify({"error": "Playlist name is required"}), 400
        
        if not songs:
            return jsonify({"error": "Add at least one song"}), 400
        
        cfg = get_config()
        navidrome_config = cfg.get("navidrome", {})
        base_url = navidrome_config.get("base_url", "http://localhost:4533")
        user = navidrome_config.get("user", "admin")
        password = navidrome_config.get("pass", "")
        
        import requests as req
        
        # Create playlist
        create_response = req.post(
            f"{base_url}/rest/createPlaylist.view",
            params={
                "u": user,
                "p": password,
                "c": "sptnr",
                "f": "json",
                "name": name,
                "comment": description,
                "public": "true" if is_public else "false"
            },
            timeout=10
        )
        
        create_data = create_response.json()
        playlist_id = create_data.get("subsonic-response", {}).get("playlist", {}).get("id")
        
        if not playlist_id:
            return jsonify({"error": "Failed to create playlist"}), 500
        
        # Add songs to playlist
        for song in songs:
            req.post(
                f"{base_url}/rest/updatePlaylist.view",
                params={
                    "u": user,
                    "p": password,
                    "c": "sptnr",
                    "f": "json",
                    "playlistId": playlist_id,
                    "songIdToAdd": song.get("id")
                },
                timeout=10
            )
        
        logging.info(f"Created playlist '{name}' with {len(songs)} songs")
        response = jsonify({
            "success": True,
            "playlist_id": playlist_id,
            "message": f"Playlist created with {len(songs)} songs"
        })
        response.status_code = 201
        return response
    except Exception as e:
        logging.error(f"Error creating custom playlist: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# =============================================================================
# LISTENBRAINZ RECOMMENDATIONS API
# =============================================================================

@app.route("/api/listenbrainz/recommendations/<rec_type>", methods=["GET"])
def api_listenbrainz_recommendations(rec_type):
    """
    Get ListenBrainz recommendations for the current user.
    
    rec_type can be:
    - weekly_jams: Current week's personalized jams
    - weekly_exploration: Discovery mode recommendations
    - last_week_jams: Previous week's jams
    - last_week_exploration: Previous week's exploration
    """
    try:
        cfg = get_config()
        current_user = session.get("username")
        navidrome_users = cfg.get("navidrome_users", [])
        
        # Find user config
        user_cfg = None
        if navidrome_users and current_user:
            user_cfg = next((u for u in navidrome_users if u.get("user") == current_user), None)
        
        if not user_cfg:
            return jsonify({"error": "User not found in configuration"}), 404
        
        # Get ListenBrainz token
        lb_token = user_cfg.get("listenbrainz_user_token", "")
        if not lb_token:
            return jsonify({"error": "ListenBrainz token not configured for this user"}), 400
        
        # Import ListenBrainz client
        from api_clients.audiodb_and_listenbrainz import ListenBrainzUserClient
        client = ListenBrainzUserClient(lb_token)
        
        # Get username from token
        username = client.get_username_from_token()
        if not username:
            return jsonify({"error": "Failed to validate ListenBrainz token"}), 401
        
        # Get recommendations based on type
        recommendations = []
        if rec_type == "weekly_jams":
            recommendations = client.get_weekly_jams(username)
        elif rec_type == "weekly_exploration":
            recommendations = client.get_weekly_exploration(username)
        elif rec_type == "last_week_jams":
            recommendations = client.get_last_week_jams(username)
        elif rec_type == "last_week_exploration":
            recommendations = client.get_last_week_exploration(username)
        else:
            return jsonify({"error": f"Unknown recommendation type: {rec_type}"}), 400
        
        return jsonify({
            "success": True,
            "type": rec_type,
            "username": username,
            "count": len(recommendations),
            "recommendations": recommendations
        })
        
    except Exception as e:
        logging.error(f"Error fetching ListenBrainz recommendations: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/listenbrainz/create-playlist", methods=["POST"])
def api_listenbrainz_create_playlist():
    """
    Create a Navidrome playlist from ListenBrainz recommendations.
    Searches for tracks in the local library and adds them to a new playlist.
    """
    try:
        data = request.get_json()
        rec_type = data.get("type", "weekly_jams")
        playlist_name = data.get("name", f"ListenBrainz {rec_type.replace('_', ' ').title()}")
        
        cfg = get_config()
        current_user = session.get("username")
        navidrome_users = cfg.get("navidrome_users", [])
        
        # Find user config
        user_cfg = None
        if navidrome_users and current_user:
            user_cfg = next((u for u in navidrome_users if u.get("user") == current_user), None)
        
        if not user_cfg:
            return jsonify({"error": "User not found in configuration"}), 404
        
        # Get ListenBrainz token
        lb_token = user_cfg.get("listenbrainz_user_token", "")
        if not lb_token:
            return jsonify({"error": "ListenBrainz token not configured"}), 400
        
        # Get recommendations
        from api_clients.audiodb_and_listenbrainz import ListenBrainzUserClient
        client = ListenBrainzUserClient(lb_token)
        username = client.get_username_from_token()
        
        recommendations = []
        if rec_type == "weekly_jams":
            recommendations = client.get_weekly_jams(username)
        elif rec_type == "weekly_exploration":
            recommendations = client.get_weekly_exploration(username)
        elif rec_type == "last_week_jams":
            recommendations = client.get_last_week_jams(username)
        elif rec_type == "last_week_exploration":
            recommendations = client.get_last_week_exploration(username)
        
        if not recommendations:
            return jsonify({"error": "No recommendations found"}), 404
        
        # Search for matching tracks in database
        matched_tracks = []
        missing_tracks = []
        
        # Get database connection
        conn = get_db()
        c = conn.cursor()
        
        for rec in recommendations:
            # Try to match by MBID first, then by artist/title
            mbid = rec.get("recording_mbid") or rec.get("mbid")
            artist_name = rec.get("artist_name", "")
            track_name = rec.get("recording_name") or rec.get("track_name", "")
            
            track_id = None
            if mbid:
                # Search by MBID in database
                c.execute("SELECT id FROM tracks WHERE musicbrainz_id = ?", (mbid,))
                result = c.fetchone()
                if result:
                    track_id = result[0]
            
            if not track_id and artist_name and track_name:
                # Search by artist and title
                c.execute("""
                    SELECT id FROM tracks 
                    WHERE LOWER(artist) = LOWER(?) AND LOWER(title) = LOWER(?)
                    LIMIT 1
                """, (artist_name, track_name))
                result = c.fetchone()
                if result:
                    track_id = result[0]
            
            if track_id:
                matched_tracks.append({"id": track_id, "artist": artist_name, "title": track_name})
            else:
                missing_tracks.append({"artist": artist_name, "title": track_name, "mbid": mbid})
        
        conn.close()
        
        # Note: Playlist creation is delegated to the frontend using matched_tracks
        # The frontend will call /api/playlist/create-custom with the matched tracks
        # This endpoint's purpose is to provide the matched/missing track analysis
        
        return jsonify({
            "success": True,
            "total_recommendations": len(recommendations),
            "matched": len(matched_tracks),
            "missing": len(missing_tracks),
            "matched_tracks": matched_tracks[:100],  # Limit response size but allow more tracks
            "missing_tracks": missing_tracks[:100],
            "note": "Use /api/playlist/create-custom to create a playlist with matched_tracks"
        })
        
    except Exception as e:
        logging.error(f"Error creating ListenBrainz playlist: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/database/cleanup-duplicates", methods=["POST"])
def api_cleanup_duplicates():
    """API endpoint to clean up duplicate albums in the database"""
    try:
        data = request.get_json() or {}
        dry_run = data.get("dry_run", True)
        
        # Import the cleanup function
        from deprecated.fix_duplicate_albums import fix_duplicates
        
        # Run the cleanup
        stats = fix_duplicates(dry_run=dry_run)
        
        return jsonify({
            "success": True,
            "dry_run": dry_run,
            "stats": stats,
            "message": f"{'Would delete' if dry_run else 'Deleted'} {stats['tracks_deleted']} duplicate tracks from {stats['albums_affected']} albums"
        })
    
    except Exception as e:
        logging.error(f"Duplicate cleanup error: {str(e)}")
        return jsonify({"error": str(e)}), 500


# ============================================================================
# UPCOMING RELEASES
# ============================================================================

@app.route("/api/upcoming-releases", methods=["GET"])
def api_upcoming_releases():
    """Get upcoming releases, optionally filtered by collection artists"""
    try:
        from wikipedia_releases_scraper import WikipediaReleaseScraper
        
        filter_collection = request.args.get("collection", "false").lower() == "true"
        
        scraper = WikipediaReleaseScraper(db_path=DB_PATH)
        releases = scraper.get_upcoming_releases(artist_in_collection=filter_collection)
        
        # Group by month for UI display
        grouped = {}
        for release in releases:
            # Handle cases where release_date might be None
            release_date = release.get('release_date') or 'Unknown'
            month = release_date[:7] if release_date and release_date != 'Unknown' else 'Unknown'
            if month not in grouped:
                grouped[month] = []
            grouped[month].append(release)
        
        return jsonify({
            "success": True,
            "total": len(releases),
            "grouped": grouped,
            "releases": releases
        })
    except Exception as e:
        logging.error(f"Error fetching upcoming releases: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/upcoming-releases/scrape", methods=["POST"])
def api_scrape_upcoming_releases():
    """Trigger a Wikipedia scrape for upcoming releases"""
    try:
        from wikipedia_releases_scraper import WikipediaReleaseScraper
        
        scraper = WikipediaReleaseScraper(db_path=DB_PATH)
        results = scraper.scrape_all_sources()
        
        return jsonify({
            "success": True,
            "message": f"Scraped {results['total_items']} releases ({results['total_added']} new, {results['total_updated']} updated)",
            "results": results
        })
    except Exception as e:
        logging.error(f"Error scraping Wikipedia: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/upcoming-releases/clear", methods=["POST"])
def api_clear_upcoming_releases():
    """Clear all upcoming releases from the database"""
    try:
        from wikipedia_releases_scraper import WikipediaReleaseScraper
        
        scraper = WikipediaReleaseScraper(db_path=DB_PATH)
        result = scraper.clear_upcoming_releases()
        
        if result.get("success"):
            return jsonify(result)
        else:
            return jsonify(result), 500
    except Exception as e:
        logging.error(f"Error clearing upcoming releases: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/upcoming-releases/search-musicbrainz", methods=["POST"])
def api_search_musicbrainz_release():
    """Search MusicBrainz for a release and return track listings"""
    try:
        data = request.get_json() or {}
        artist = data.get("artist", "")
        album = data.get("album", "")
        
        if not artist or not album:
            return jsonify({"error": "Artist and album name required"}), 400
        
        # Search MusicBrainz for release groups
        headers = {"User-Agent": MUSICBRAINZ_USER_AGENT}
        search_url = "https://musicbrainz.org/ws/2/release-group"
        
        # Build search query - use fuzzy matching instead of exact quotes for better results
        # This allows partial matches and typos to still return results
        query = f'artist:{artist} AND releasegroup:{album}'
        params = {
            "fmt": "json",
            "query": query,
            "limit": 20  # Increased from 10 to show more potential matches
        }
        
        logging.info(f"Searching MusicBrainz for: {artist} - {album}")
        
        response = requests.get(search_url, headers=headers, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        release_groups = data.get("release-groups", [])
        
        if not release_groups:
            return jsonify({
                "success": True,
                "results": [],
                "message": f"No releases found for {artist} - {album}"
            })
        
        # For each release group, fetch one representative release with tracks
        results = []
        for rg in release_groups[:10]:  # Increased from 5 to show more potential matches
            rg_id = rg.get("id", "")
            rg_title = rg.get("title", "")
            rg_type = rg.get("primary-type", "")
            first_release_date = rg.get("first-release-date", "")
            
            # Fetch releases in this release group
            releases_url = f"https://musicbrainz.org/ws/2/release-group/{rg_id}"
            releases_params = {
                "fmt": "json",
                "inc": "releases"
            }
            
            time.sleep(1)  # Rate limiting
            
            try:
                releases_response = requests.get(releases_url, headers=headers, params=releases_params, timeout=15)
                releases_response.raise_for_status()
                rg_data = releases_response.json()
                
                releases = rg_data.get("releases", [])
                if not releases:
                    continue
                
                # Pick the first release to get tracks
                release = releases[0]
                release_id = release.get("id", "")
                
                # Fetch full release with tracks
                release_url = f"https://musicbrainz.org/ws/2/release/{release_id}"
                release_params = {
                    "fmt": "json",
                    "inc": "recordings"
                }
                
                time.sleep(1)  # Rate limiting
                
                release_response = requests.get(release_url, headers=headers, params=release_params, timeout=15)
                release_response.raise_for_status()
                release_data = release_response.json()
                
                # Extract tracks
                media = release_data.get("media", [])
                tracks = []
                for disc in media:
                    for track in disc.get("tracks", []):
                        recording = track.get("recording", {})
                        tracks.append({
                            "title": recording.get("title", "Unknown"),
                            "position": track.get("position", ""),
                            "length": recording.get("length", 0)
                        })
                
                results.append({
                    "release_group_id": rg_id,
                    "release_id": release_id,
                    "title": rg_title,
                    "artist": artist,
                    "type": rg_type,
                    "date": first_release_date,
                    "track_count": len(tracks),
                    "tracks": tracks
                })
                
            except Exception as e:
                logging.warning(f"Error fetching tracks for release group {rg_id}: {e}")
                continue
        
        return jsonify({
            "success": True,
            "results": results,
            "total": len(results)
        })
        
    except requests.exceptions.HTTPError as e:
        logging.error(f"MusicBrainz API error: {e}")
        return jsonify({"error": f"MusicBrainz API error: {e.response.status_code if hasattr(e, 'response') else str(e)}"}), 500
    except Exception as e:
        logging.error(f"Error searching MusicBrainz: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/upcoming-releases/search-discogs", methods=["POST"])
def api_search_discogs_release():
    """Search Discogs for a release and return track listings as fallback"""
    try:
        data = request.get_json() or {}
        artist = data.get("artist", "")
        album = data.get("album", "")
        
        if not artist or not album:
            return jsonify({"error": "Artist and album name required"}), 400
        
        # Get Discogs configuration
        cfg = get_config()
        discogs_config = cfg.get("discogs", {})
        discogs_token = discogs_config.get("token", "")
        discogs_enabled = discogs_config.get("enabled", False)
        
        if not discogs_enabled or not discogs_token:
            return jsonify({
                "success": False,
                "error": "Discogs is not configured. Please add your Discogs token in config."
            }), 400
        
        from api_clients.discogs import DiscogsClient
        
        discogs_client = DiscogsClient(token=discogs_token, enabled=discogs_enabled)
        
        # Search Discogs for releases
        search_url = f"{discogs_client.base_url}/database/search"
        search_params = {
            "q": f"{artist} {album}",
            "type": "release",
            "per_page": 20
        }
        
        logging.info(f"Searching Discogs for: {artist} - {album}")
        
        time.sleep(DISCOGS_RATE_LIMIT_DELAY)  # Discogs rate limiting
        
        response = requests.get(search_url, headers=discogs_client.headers, params=search_params, timeout=15)
        response.raise_for_status()
        search_data = response.json()
        
        search_results = search_data.get("results", [])
        
        if not search_results:
            return jsonify({
                "success": True,
                "results": [],
                "message": f"No releases found on Discogs for {artist} - {album}"
            })
        
        # Fetch details for top matches
        results = []
        for result in search_results[:10]:  # Limit to 10 results
            release_id = result.get('id')
            if not release_id:
                continue
            
            try:
                time.sleep(DISCOGS_RATE_LIMIT_DELAY)  # Rate limiting
                
                # Fetch full release data
                release_url = f"{discogs_client.base_url}/releases/{release_id}"
                release_response = requests.get(release_url, headers=discogs_client.headers, timeout=15)
                release_response.raise_for_status()
                release_data = release_response.json()
                
                # Extract track listing
                tracklist = release_data.get("tracklist", [])
                tracks = []
                for track in tracklist:
                    if track.get("type_") == "track":
                        tracks.append({
                            "title": track.get("title", "Unknown"),
                            "position": track.get("position", ""),
                            "duration": track.get("duration", "")
                        })
                
                # Get release info
                release_title = release_data.get("title", "")
                release_year = release_data.get("year")
                release_formats = release_data.get("formats", [])
                format_names = [fmt.get("name", "") for fmt in release_formats]
                
                results.append({
                    "release_id": release_id,
                    "title": release_title,
                    "artist": artist,
                    "year": release_year,
                    "formats": format_names,
                    "track_count": len(tracks),
                    "tracks": tracks,
                    "source": "discogs"
                })
                
            except Exception as e:
                logging.warning(f"Error fetching Discogs release {release_id}: {e}")
                continue
        
        return jsonify({
            "success": True,
            "results": results,
            "total": len(results),
            "source": "discogs"
        })
        
    except requests.exceptions.HTTPError as e:
        logging.error(f"Discogs API error: {e}")
        return jsonify({"error": f"Discogs API error: {e.response.status_code if hasattr(e, 'response') else str(e)}"}), 500
    except Exception as e:
        logging.error(f"Error searching Discogs: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/upcoming-releases/search", methods=["POST"])
def api_search_upcoming_release():
    """Search for downloads of an upcoming release"""
    try:
        data = request.get_json() or {}
        artist = data.get("artist", "")
        album = data.get("album", "")
        
        if not artist or not album:
            return jsonify({"error": "Artist and album name required"}), 400
        
        # Redirect to downloads with search query
        search_query = f"{artist} {album}"
        
        return jsonify({
            "success": True,
            "search_query": search_query,
            "message": f"Ready to search for: {search_query}"
        })
    except Exception as e:
        logging.error(f"Error processing release search: {str(e)}")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    # Check if background scanner should auto-start on app launch
    try:
        cfg = get_config()
        features = cfg.get('features', {})
        
        # Only show auto-start status if perpetual mode is enabled
        if features.get('perpetual'):
            print("Background scanner auto-start: ENABLED")
            print("Starting Navidrome sync and popularity scan in background...")
            
            # Start the beets auto-import and scanner in background thread
            def start_scanner():
                import time as time_module
                time_module.sleep(2)  # Give Flask time to start
                try:
                    logger = logging.getLogger('sptnr')
                    logger.info("Auto-starting scanner with perpetual mode...")
                    
                    # Run the standard scanner
                    print("Step 1: Running Navidrome sync and popularity scan...")
                    logger.info("Step 1: Running Navidrome sync and popularity scan...")
                    from start import run_scan
                    run_scan(scan_type='full')
                except Exception as e:
                    import traceback
                    print(f"Error in background scanner: {e}")
                    print(traceback.format_exc())
                    logger = logging.getLogger('sptnr')
                    logger.error(f"Error in background scanner: {e}")
                    logger.error(traceback.format_exc())
            
            scanner_thread = threading.Thread(target=start_scanner, daemon=True)
            scanner_thread.start()
        else:
            # Perpetual mode disabled - scans will be triggered manually
            print("Background scanner auto-start: DISABLED (trigger scans manually via web UI)")
    except Exception as e:
        import traceback
        print(f"Error checking auto-start configuration: {e}")
        print(traceback.format_exc())
    
    # Start Download Retry Manager (for persistent downloads)
    try:
        def start_retry_manager():
            """Background thread to manage download retries"""
            import time as time_module
            from download_retry_manager import run_retry_manager
            
            # Wait for Flask to start
            time_module.sleep(5)
            
            try:
                cfg = get_config()
                # Get scheduler config
                scheduler_config = cfg.get("features", {}).get("retry_scheduler", {})
                interval = scheduler_config.get("interval_seconds", 60)
                
                navidrome_config = cfg.get("navidrome", {})
                navidrome_url = navidrome_config.get("url", "http://localhost:4533")
                navidrome_token = navidrome_config.get("token", "")
                
                logging.info(f"[RETRY_SCHEDULER] Started with interval: {interval}s")
                
                while not retry_scheduler.get("stop_event", threading.Event()).is_set():
                    try:
                        stats = run_retry_manager(DB_PATH, navidrome_url, navidrome_token)
                        if stats["retried"] > 0 or stats["completed"] > 0:
                            logging.info(f"[RETRY_SCHEDULER] Retried: {stats['retried']}, Completed: {stats['completed']}, Failed: {stats['failed']}")
                    except Exception as e:
                        logging.error(f"[RETRY_SCHEDULER] Error: {e}")
                    
                    # Wait with stop event check
                    if retry_scheduler.get("stop_event", threading.Event()).wait(timeout=interval):
                        # Stop event was set
                        break
                
                logging.info("[RETRY_SCHEDULER] Stopped")
            except Exception as e:
                logging.error(f"[RETRY_SCHEDULER] Worker error: {e}")
            finally:
                with retry_scheduler_lock:
                    retry_scheduler["running"] = False
        
        print("Starting Download Retry Manager...")
        retry_scheduler["stop_event"] = threading.Event()
        retry_thread = threading.Thread(target=start_retry_manager, daemon=True)
        retry_thread.start()
        
        with retry_scheduler_lock:
            retry_scheduler["thread"] = retry_thread
            retry_scheduler["running"] = True
    except Exception as e:
        logging.warning(f"Could not start Download Retry Manager: {e}")
    
    # Start Flask application
    app.run(debug=False, host="0.0.0.0", port=5000)



