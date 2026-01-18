#!/usr/bin/env python3
"""Helper functions for scanning and rating operations."""

import logging
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from start import get_db_connection, fetch_artist_albums, fetch_album_tracks, save_to_db
from single_detector import get_current_single_detection
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

# --- Single Detection DB Helpers ---
def get_db_connection():
    from start import DB_PATH
    import sqlite3
    conn = sqlite3.connect(DB_PATH, timeout=120.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


# Color constants
LIGHT_RED = Fore.RED + Style.BRIGHT
LIGHT_GREEN = Fore.GREEN + Style.BRIGHT
LIGHT_BLUE = Fore.BLUE + Style.BRIGHT
LIGHT_YELLOW = Fore.YELLOW + Style.BRIGHT
LIGHT_CYAN = Fore.CYAN + Style.BRIGHT
RESET = Style.RESET_ALL

# Configuration constants
PROGRESS_UPDATE_INTERVAL = 10  # Update progress every N items
API_RATE_LIMIT_DELAY = 0.1  # Delay between API calls to avoid rate limiting
LOCAL_TZ = os.environ.get("TIMEZONE") or os.environ.get("TZ") or "UTC"

def _now_local_iso() -> str:
    """Return ISO timestamp in configured local timezone."""
    try:
        return datetime.now(ZoneInfo(LOCAL_TZ)).isoformat()
    except Exception:
        return datetime.now().isoformat()

def save_navidrome_scan_progress(current_artist, processed_artists, total_artists):
    """Save Navidrome scan progress to JSON file (using artist list for progress tracking)"""
    try:
        progress_file = os.environ.get("NAVIDROME_PROGRESS_FILE", "/database/navidrome_scan_progress.json")
        progress = {
            "current_artist": current_artist,
            "processed_artists": processed_artists,
            "total_artists": total_artists,
            "is_running": True,
            "scan_type": "navidrome_scan",
            "percent_complete": int((processed_artists / total_artists * 100)) if total_artists > 0 else 0
        }
        with open(progress_file, 'w') as f:
            json.dump(progress, f, indent=2)
    except Exception as e:
        logging.error(f"Failed to save Navidrome scan progress: {e}")

def scan_artist_to_db(artist_name: str, artist_id: str, verbose: bool = False, force: bool = False, processed_artists: int = 0, total_artists: int = 0):
    """
    Scan a single artist from Navidrome and persist tracks to DB.

    Args:
        artist_name: Name of the artist to scan
        artist_id: Navidrome ID of the artist
        verbose: Enable verbose logging
        force: Force re-import even if cached
        processed_artists: Current artist index (1-based) for progress tracking
        total_artists: Total number of artists for progress tracking
    """
    try:
        # Prefetch cached track IDs for this artist and check for missing critical fields
        existing_track_ids: set[str] = set()
        existing_album_tracks: dict[str, set[str]] = {}
        albums_needing_reimport: set[str] = set()  # Track albums with missing fields
        albums_logged: set[str] = set()  # Track which albums we've already logged
        try:
            conn = get_db_connection()
            cursor = conn.cursor()

            # Critical fields that should be imported from Navidrome
            critical_fields = ['duration', 'track_number', 'year', 'file_path']

            # Get existing tracks and check for missing fields
            cursor.execute(f"SELECT album, id, {', '.join(critical_fields)} FROM tracks WHERE artist = ?", (artist_name,))
            for row in cursor.fetchall():
                alb_name = row[0]
                tid = row[1]
                existing_track_ids.add(tid)
                existing_album_tracks.setdefault(alb_name, set()).add(tid)

                # Check if any critical field is missing (NULL or empty)
                field_values = row[2:]
                if any(val is None or val == '' or val == 0 for val in field_values):
                    albums_needing_reimport.add(alb_name)
                    # Only log once per album to avoid duplicate messages
                    if verbose and alb_name not in albums_logged:
                        logging.info(f"Album '{alb_name}' flagged for re-import due to missing fields")
                        albums_logged.add(alb_name)
            conn.close()
        except Exception as e:
            logging.debug(f"Prefetch existing tracks for artist '{artist_name}' failed: {e}")

        albums = fetch_artist_albums(artist_id)
        if verbose:
            print(f"🎤 Scanning artist: {artist_name} ({len(albums)} albums)")
        logging.info(f"🎤 [Navidrome] Scanning artist: {artist_name} ({len(albums)} albums)")
        # Save artist-level progress
        if total_artists > 0:
            save_navidrome_scan_progress(artist_name, processed_artists, total_artists)

        total_albums = len(albums)
        for alb_idx, alb in enumerate(albums, 1):
            album_name = alb.get("name") or ""
            album_id = alb.get("id")
            if not album_id:
                continue
            logging.info(f"   💿 [Album {alb_idx}/{total_albums}] {album_name}")
            
            # Detect if this is a live/unplugged album
            from helpers import detect_live_album
            album_context = detect_live_album(album_name)
            if album_context.get("is_live") or album_context.get("is_unplugged"):
                logging.info(f"      🎤 Detected live/unplugged album: {album_name}")
            try:
                tracks = fetch_album_tracks(album_id)
            except Exception as e:
                logging.debug(f"Failed to fetch tracks for album '{album_name}': {e}")
                tracks = []

            cached_ids_for_album = existing_album_tracks.get(album_name, set())

            # Skip album only if it's already cached AND doesn't need re-import due to missing fields
            album_needs_reimport = album_name in albums_needing_reimport
            if not force and not album_needs_reimport and tracks and len(cached_ids_for_album) >= len(tracks):
                if verbose:
                    print(f"   Skipping cached album: {album_name}")
                # Still log skipped albums to scan history
                log_album_scan(artist_name, album_name, 'navidrome', len(cached_ids_for_album), 'skipped')
                continue

            if album_needs_reimport and verbose:
                print(f"   Re-importing album with missing fields: {album_name}")
            # Track the number of tracks actually processed for this album
            album_tracks_processed = 0

            for t in tracks:
                track_id = t.get("id")
                if not track_id:
                    continue

                # Normalize numeric fields from Navidrome payload
                def _safe_int(val):
                    try:
                        return int(val)
                    except (TypeError, ValueError):
                        return None

                raw_track = t.get("trackNumber") if "trackNumber" in t else t.get("track")
                raw_disc = t.get("discNumber") if "discNumber" in t else t.get("disc")
                
                # Extract genre from Navidrome and use it as the initial genres value
                navidrome_genre = t.get("genre", "")
                navidrome_genre_list = [navidrome_genre] if navidrome_genre else []
                
                td = {
                    "id": track_id,
                    "title": t.get("title", ""),
                    "album": album_name,
                    "artist": artist_name,
                    "score": 0.0,
                    "spotify_score": 0,
                    "lastfm_score": 0,
                    "listenbrainz_score": 0,
                    "age_score": 0,
                    "genres": navidrome_genre if navidrome_genre else "",  # Initialize with Navidrome genre
                    "navidrome_genres": navidrome_genre if navidrome_genre else "",  # Store as comma-separated string
                    "navidrome_genre": navidrome_genre,  # Also store in single genre field
                    "spotify_genres": json.dumps([]),  # Serialize as JSON string
                    "lastfm_tags": json.dumps([]),  # Serialize as JSON string
                    "discogs_genres": json.dumps([]),  # Serialize as JSON string
                    "audiodb_genres": json.dumps([]),  # Serialize as JSON string
                    "musicbrainz_genres": json.dumps([]),  # Serialize as JSON string
                    "spotify_album": "",
                    "spotify_artist": "",
                    "spotify_popularity": 0,
                    "spotify_release_date": t.get("year", "") or "",
                    "spotify_album_art_url": "",
                    "lastfm_track_playcount": 0,
                    # Leave file_path unset for Navidrome; beets import owns the canonical path
                    "file_path": None,
                    "last_scanned": _now_local_iso(),
                    "spotify_album_type": "",
                    "spotify_total_tracks": 0,
                    "spotify_id": None,
                    "is_spotify_single": False,
                    "is_single": False,
                    "single_confidence": "low",
                    "single_sources": json.dumps([]),  # Serialize as JSON string
                    "mbid": t.get("mbid", "") or "",
                    "suggested_mbid": "",
                    "suggested_mbid_confidence": 0.0,
                    "stars": int(t.get("userRating", 0) or 0),
                    "duration": t.get("duration"),
                    "track_number": _safe_int(raw_track),
                    "disc_number": _safe_int(raw_disc),
                    "year": t.get("year"),
                    "album_artist": t.get("albumArtist", ""),
                    "bitrate": t.get("bitRate"),
                    "sample_rate": t.get("samplingRate"),
                    # Store album context for single detection
                    "album_context_live": 1 if album_context.get("is_live") else 0,
                    "album_context_unplugged": 1 if album_context.get("is_unplugged") else 0,
                }
                save_to_db(td)
                album_tracks_processed += 1

            # Log this album completion to scan_history
            if album_tracks_processed > 0:
                logging.info(f"Logging to scan_history: {artist_name} - {album_name} ({album_tracks_processed} tracks)")
                log_album_scan(artist_name, album_name, 'navidrome', album_tracks_processed, 'completed')
                logging.info(f"Completed navidrome scan for {artist_name} - {album_name} ({album_tracks_processed} tracks)")
        if verbose:
            print(f"Artist scan complete: {artist_name}")
            logging.info(f"Artist scan complete: {artist_name}")
    except Exception as e:
        logging.error(f"scan_artist_to_db failed for {artist_name}: {e}")
        raise
