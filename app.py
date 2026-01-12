

# --- Unified Log API ---
# Place all Flask route definitions after app = Flask(__name__)

# --- ENVIRONMENT VARIABLE EDITING SUPPORT ---
# List of all environment variables used in the project (compiled from codebase)
ALL_ENV_VARS = [
    "SECRET_KEY", "CONFIG_PATH", "DB_PATH", "LOG_PATH", "APP_DIR", "SPTNR_DISABLE_BOOT_ND_IMPORT", "SPTNR_SKIP_SINGLES",
    "MUSIC_ROOT", "MUSIC_FOLDER", "DOWNLOADS_DIR", "POPULARITY_LOG_PATH", "POPULARITY_LOG_STDOUT", "POPULARITY_PROGRESS_FILE",
    "NAVIDROME_PROGRESS_FILE", "SINGLES_PROGRESS_FILE", "PROGRESS_FILE", "TIMEZONE", "TZ", "SPOTIFY_USER_TOKEN",
    "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_WEIGHT", "LASTFM_WEIGHT", "LISTENBRAINZ_WEIGHT", "AGE_WEIGHT",
    "LASTFMAPIKEY", "NAV_BASE_URL", "NAV_USER", "NAV_PASS", "YOUTUBE_API_KEY", "GOOGLE_CSE_ID", "GOOGLE_API_KEY",
    "TRUSTED_CHANNEL_IDS", "DISCOGS_TOKEN", "AI_API_KEY", "DEV_BOOST_WEIGHT", "AUDIODB_API_KEY", "WEB_API_KEY",
    "ENABLE_WEB_API_KEY", "MP3_PROGRESS_FILE", "BEETS_LOG_PATH", "SEARCHAPI_IO_KEY",
    "PG_HOST", "PG_PORT", "PG_USER", "PG_PASSWORD", "PG_DATABASE"
]

def get_all_env_vars():
    # Return a dict of all relevant env vars and their current values
    return {var: os.environ.get(var, "") for var in ALL_ENV_VARS}


# Place all Flask route definitions after app = Flask(__name__)

#!/usr/bin/env python3
import sqlite3
import psycopg2
import psycopg2.extras
from contextlib import closing
import json
import yaml
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response, send_file, session
from datetime import datetime
import copy
from functools import wraps
from scan_helpers import scan_artist_to_db
from start import rate_artist
from popularity import popularity_scan
from start import build_artist_index
from metadata_reader import aggregate_genres_from_tracks
from check_db import update_schema
from start import save_to_db
from scan_helpers import scan_artist_to_db

import os
import sys
from beets_integration import _get_beets_client
import secrets
import subprocess
import threading
import time
import logging
import re
from api_clients.slskd import SlskdClient
from metadata_reader import get_track_metadata_from_db, find_track_file, read_mp3_metadata
import io
from helpers import create_retry_session
import difflib
import unicodedata
import requests
import hashlib


# Unified logging setup

import logging
LOG_PATH = os.environ.get("LOG_PATH", "/config/sptnr.log")
VERBOSE = (
    os.environ.get("SPTNR_VERBOSE_APP") or os.environ.get("SPTNR_VERBOSE") or "0"
) == "1"
SERVICE_PREFIX = "WebUI_"

class ServicePrefixFormatter(logging.Formatter):
    def __init__(self, prefix, fmt=None):
        super().__init__(fmt or '%(asctime)s [%(levelname)s] %(message)s')
        self.prefix = prefix
    def format(self, record):
        record.msg = f"{self.prefix}{record.msg}"
        return super().format(record)

formatter = ServicePrefixFormatter(SERVICE_PREFIX)
file_handler = logging.FileHandler(LOG_PATH)
file_handler.setFormatter(formatter)
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
logging.basicConfig(level=logging.INFO, handlers=[file_handler, stream_handler])

def log_basic(msg):
    if VERBOSE:
        logging.info(msg)

def log_verbose(msg):
    if VERBOSE:
        logging.info(f"[VERBOSE] {msg}")

app = Flask(__name__)

# --- Unified Log API ---
@app.route("/api/unified-log")
def api_unified_log():
    lines = int(request.args.get("lines", 40))
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
        # Filter out HTTP request/response logs unless verbose is enabled
        if not verbose:
            import re
            http_log_pattern = re.compile(r'"(GET|POST|PUT|DELETE|PATCH) /api/.* HTTP/1\\.[01]" (200|201|204|400|401|403|404|500|502|503)')
            log_lines = [line for line in log_lines if not http_log_pattern.search(line)]
        # Only return the last N lines
        log_lines = log_lines[-lines:]
        log_verbose(f"[api_unified_log] Returning {len(log_lines)} log lines")
        return jsonify({"lines": [line.rstrip('\n') for line in log_lines]})
    except Exception as e:
        log_verbose(f"[api_unified_log] Exception processing log lines: {e}")
        return jsonify({"error": str(e), "lines": []}), 500
if VERBOSE:
    logging.basicConfig(level=logging.WARNING, handlers=[file_handler, stream_handler])
else:
    logging.basicConfig(level=logging.ERROR, handlers=[file_handler, stream_handler])

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
# ...existing code...
