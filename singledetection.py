#!/usr/bin/env python3
"""
Single Detection Scanner - Detects which tracks are singles vs album tracks.
Uses Discogs, Last.fm, MusicBrainz, DuckDuckGo and other sources.

Integrates:
- single_detector.py: Advanced multi-source weighted scoring
- ddg_searchapi_checker.py: DuckDuckGo official video detection
"""

import os
import sqlite3
import logging
import json
from datetime import datetime
from typing import Optional, Dict, List
import sys
import re
import time
import threading
import difflib
from concurrent.futures import ThreadPoolExecutor



# Cleaned up: Only DB helpers and orchestration wrappers remain. All single detection logic is now in single_detector.py.

import sqlite3
import logging
from typing import Optional, Dict

def get_db_connection():
    from start import DB_PATH
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn

def get_cached_source_detections(track_id: str) -> dict:
    """
    Retrieve cached per-source single detection results from database.
    Returns dict with source names as keys and boolean values (or None if not cached).
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """SELECT source_discogs_single, source_discogs_video, source_spotify_single,
                      source_musicbrainz_single, source_lastfm_single, source_short_release,
                      source_detection_date FROM tracks WHERE id = ?""",
            (track_id,)
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            return {
                "discogs_single": row["source_discogs_single"],
                "discogs_video": row["source_discogs_video"],
                "spotify_single": row["source_spotify_single"],
                "musicbrainz_single": row["source_musicbrainz_single"],
                "lastfm_single": row["source_lastfm_single"],
                "short_release": row["source_short_release"],
                "detection_date": row["source_detection_date"]
            }
        return {}
    except Exception as e:
        logging.debug(f"Could not get cached detection results for {track_id}: {e}")
        return {}

def save_source_detections(track_id: str, source_results: dict) -> None:
    """
    Save per-source single detection results to database for caching.
    source_results should have keys: discogs_single, discogs_video, spotify_single, 
    musicbrainz_single, lastfm_single, short_release (all boolean or None)
    """
    from datetime import datetime
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE tracks SET 
                source_discogs_single = ?,
                source_discogs_video = ?,
                source_spotify_single = ?,
                source_musicbrainz_single = ?,
                source_lastfm_single = ?,
                source_short_release = ?,
                source_detection_date = ?
            WHERE id = ?""",
            (
                source_results.get("discogs_single"),
                source_results.get("discogs_video"),
                source_results.get("spotify_single"),
                source_results.get("musicbrainz_single"),
                source_results.get("lastfm_single"),
                source_results.get("short_release"),
                datetime.now().isoformat(),
                track_id
            )
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logging.debug(f"Could not save detection results for {track_id}: {e}")

    # All orchestration and detection logic should now use single_detector.py



def get_cached_source_detections(track_id: str) -> dict:
    """
    Retrieve cached per-source single detection results from database.
    Returns dict with source names as keys and boolean values (or None if not cached).
    """
    try:
        from start import DB_PATH
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """SELECT source_discogs_single, source_discogs_video, source_spotify_single,
                      source_musicbrainz_single, source_lastfm_single, source_short_release,
                      source_detection_date FROM tracks WHERE id = ?""",
            (track_id,)
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            return {
                "discogs_single": row["source_discogs_single"],
                "discogs_video": row["source_discogs_video"],
                "spotify_single": row["source_spotify_single"],
                "musicbrainz_single": row["source_musicbrainz_single"],
                "lastfm_single": row["source_lastfm_single"],
                "short_release": row["source_short_release"],
                "detection_date": row["source_detection_date"]
            }
        return {}
    except Exception as e:
        logging.debug(f"Could not get cached detection results for {track_id}: {e}")
        return {}


def save_source_detections(track_id: str, source_results: dict) -> None:
    """
    Save per-source single detection results to database for caching.
    source_results should have keys: discogs_single, discogs_video, spotify_single, 
    musicbrainz_single, lastfm_single, short_release (all boolean or None)
    """
    try:
        from start import DB_PATH
        from datetime import datetime
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE tracks SET 
                source_discogs_single = ?,
                source_discogs_video = ?,
                source_spotify_single = ?,
                source_musicbrainz_single = ?,
                source_lastfm_single = ?,
                source_short_release = ?,
                source_detection_date = ?
            WHERE id = ?""",
            (
                source_results.get("discogs_single"),
                source_results.get("discogs_video"),
                source_results.get("spotify_single"),
                source_results.get("musicbrainz_single"),
                source_results.get("lastfm_single"),
                source_results.get("short_release"),
                datetime.now().isoformat(),
                track_id
            )
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logging.debug(f"Could not save detection results for {track_id}: {e}")
