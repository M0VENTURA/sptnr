

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

# ...existing code...

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
            "listenbrainz": {"enabled": True},
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
        "weights": {"spotify": 0.4, "lastfm": 0.3, "listenbrainz": 0.2, "age": 0.1},
        "database": {"path": DB_PATH, "vacuum_on_start": False},
        "logging": {"level": "INFO", "file": LOG_PATH, "console": True},
        "features": {
            "dry_run": False,
            "sync": True,
            "force": False,
            "verbose": False,
            "perpetual": True,
            "batchrate": False,
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
# ...existing code...
