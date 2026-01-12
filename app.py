

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

# Diagnostic: Print which start.py is being imported
import importlib.util
spec = importlib.util.find_spec("start")
if spec and spec.origin:
    print(f"[DIAGNOSTIC] start.py will be imported from: {spec.origin}")
else:
    print("[DIAGNOSTIC] start.py module not found in import path!")

# ...existing code...
