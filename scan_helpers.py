#!/usr/bin/env python3
"""Helper functions for scanning and rating operations."""

import logging
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from .start import get_db_connection, fetch_artist_albums, fetch_album_tracks, save_to_db
from colorama import Fore, Style

try:
    from scan_history import log_album_scan
    _scan_history_available = True
except ImportError as e:
    # Fallback if scan_history module not available
    logging.warning(f"scan_history module not available: {e}")
    _scan_history_available = False
    def log_album_scan(*args, **kwargs):
        logging.debug(f"log_album_scan called but scan_history not available: {args}")

# ...existing code...
