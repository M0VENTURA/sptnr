#!/usr/bin/env python3
# ðŸŽ§ SPTNR â€“ Navidrome Rating CLI with Spotify + Last.fm + Navidrome API Integration

import argparse
import os
import sys
import time
import logging
import re
import sqlite3
import math
import json
import threading
import difflib
import unicodedata
import requests
import yaml
from colorama import init, Fore, Style
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from helpers import strip_parentheses, create_retry_session
from datetime import datetime, timedelta
from statistics import median
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from scan_history import log_album_scan

# âœ… Import modular API clients
from api_clients.navidrome import NavidromeClient
from api_clients.spotify import SpotifyClient
from api_clients.lastfm import LastFmClient
from api_clients.musicbrainz import MusicBrainzClient
from api_clients.discogs import DiscogsClient
from api_clients.audiodb_and_listenbrainz import ListenBrainzClient, AudioDbClient
from popularity_helpers import (
    configure_popularity_helpers,
    get_spotify_artist_id,
    get_spotify_artist_single_track_ids,
    search_spotify_track,
    get_lastfm_track_info,
    get_listenbrainz_score,
    score_by_age,
    SPOTIFY_WEIGHT,
    LASTFM_WEIGHT,
    LISTENBRAINZ_WEIGHT,
    AGE_WEIGHT,
)

# ðŸŽ¨ Colorama setup
init(autoreset=True)
LIGHT_RED = Fore.RED + Style.BRIGHT
LIGHT_GREEN = Fore.GREEN + Style.BRIGHT
LIGHT_BLUE = Fore.BLUE + Style.BRIGHT
LIGHT_YELLOW = Fore.YELLOW + Style.BRIGHT
LIGHT_CYAN = Fore.CYAN + Style.BRIGHT
RESET = Style.RESET_ALL

# Helper function to parse datetime flexibly
def parse_datetime_flexible(date_string):
    """Parse datetime with flexible format handling for both 'T' and space separators."""
    formats = ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"]
    for fmt in formats:
        try:
            return datetime.strptime(date_string, fmt)
        except ValueError:
            continue
    raise ValueError(f"Could not parse datetime: {date_string}")

# ...existing code...
