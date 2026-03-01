#!/usr/bin/env python3
"""
Popularity Scanner - Detects track popularity from external sources (Spotify, Last.fm, ListenBrainz).
Calculates popularity scores and updates database.
Note: Singles detection is handled separately by sptnr.py rate_artist() function.
"""

import os
import sqlite3
import logging
import json
import math
import yaml
import atexit
import time
import heapq
import re
import difflib
import unicodedata
import requests
from contextlib import contextmanager
from datetime import datetime, timedelta
from statistics import median, mean, stdev
from concurrent.futures import ThreadPoolExecutor
import concurrent.futures
from api_clients import session, timeout_safe_session
from helpers.helpers import find_matching_spotify_single
from helpers.matching_utils import normalize_album

# Import centralized logging
from helpers.logging_config import setup_logging, log_unified, log_info, log_debug

# Set up logging for popularity service
setup_logging("popularity")

# Import API clients for single detection at module level
try:
    from api_clients.musicbrainz import MusicBrainzClient, get_artist_country  # type: ignore
    HAVE_MUSICBRAINZ = True
except ImportError as e:
    log_debug(f"MusicBrainz client unavailable: {e}")
    HAVE_MUSICBRAINZ = False
    MusicBrainzClient = None  # type: ignore
    
try:
    from api_clients.discogs import DiscogsClient  # type: ignore
    HAVE_DISCOGS = True
    HAVE_DISCOGS_VIDEO = True
except ImportError as e:
    log_debug(f"Discogs client unavailable: {e}")
    HAVE_DISCOGS = False
    HAVE_DISCOGS_VIDEO = False
    DiscogsClient = None  # type: ignore

try:
    from api_clients.audiodb import get_artist_biography, get_artist_fanart
    HAVE_AUDIODB = True
except ImportError as e:
    log_debug(f"AudioDB client unavailable: {e}")
    HAVE_AUDIODB = False

# Timeout-safe clients for use within _run_with_timeout() context
# These use timeout_safe_session with reduced retry count to prevent exceeding timeout
_timeout_safe_mb_client = None
_timeout_safe_discogs_clients = {}  # token -> client mapping

def _get_timeout_safe_musicbrainz_client():
    """Get or create timeout-safe MusicBrainz client for use in popularity scanner."""
    global _timeout_safe_mb_client
    if _timeout_safe_mb_client is None and HAVE_MUSICBRAINZ:
        _timeout_safe_mb_client = MusicBrainzClient(http_session=timeout_safe_session, enabled=True)
    return _timeout_safe_mb_client

def _get_timeout_safe_discogs_client(token: str):
    """Get or create timeout-safe Discogs client for use in popularity scanner."""
    global _timeout_safe_discogs_clients
    if not HAVE_DISCOGS:
        return None
    if token not in _timeout_safe_discogs_clients:
        _timeout_safe_discogs_clients[token] = DiscogsClient(token, http_session=timeout_safe_session, enabled=True)
    return _timeout_safe_discogs_clients.get(token)

# Module-level logger
logger = logging.getLogger(__name__)

# Keyword filter for non-singles (defined at module level for performance)
# Filters out alternate versions: live, acoustic, orchestral, remixes, demos, etc.
# Note: List is used (not set) since we perform substring matching with 'any(k in title...)'
IGNORE_SINGLE_KEYWORDS = [
    "intro", "outro", "jam",  # intros/outros/jams
    "live", "unplugged",  # live performances
    "remix", "edit", "mix",  # remixes and edits
    "acoustic", "orchestral",  # alternate arrangements
    "demo", "instrumental", "karaoke",  # alternate versions
    "remaster", "remastered"  # remasters
]

# Minimum tracks required for artist-level standout/star rating comparison
MIN_TRACKS_FOR_ARTIST_COMPARISON = 10
# Subset of keywords to check in Spotify album names (for album-level filtering)