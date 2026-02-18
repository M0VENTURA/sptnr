#!/usr/bin/env python3
"""
Navidrome Import Module - Handles importing metadata from Navidrome to local database.

This module is responsible for:
- Scanning artists from Navidrome
- Importing album and track metadata
- Logging to unified_scan.log (basic details only)
- Logging to info.log (detailed operations)
- Logging to debug.log (debug information)
- Preserving user-edited single detection and ratings
"""

import os
import logging
import sqlite3
import time
import json
import difflib
import unicodedata
import re
import requests
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

# --- Configuration ---
LOCAL_TZ = os.environ.get("TIMEZONE") or os.environ.get("TZ") or "UTC"

# --- Logging Setup with centralized config ---
from logging_config import setup_logging, log_unified, log_info, log_debug

# Set up logging for navidrome_import service
setup_logging("navidrome_import")

# Keep standard logging for backward compatibility
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# --- Import dependencies ---
from db_utils import get_db_connection
from popularity_helpers import fetch_artist_albums, fetch_album_tracks, save_to_db
from api_clients.musicbrainz import _USER_AGENT as MUSICBRAINZ_USER_AGENT
from helpers import create_retry_session

try:
    from metadata_reader import write_genre_to_audio_file
except ImportError:
    def write_genre_to_audio_file(file_path, genres):
        """Fallback if metadata_reader not available"""
        return False

try:
    from scan_history import log_album_scan
    _scan_history_available = True
except ImportError as e:
    logging.warning(f"scan_history module not available: {e}")
    _scan_history_available = False
    def log_album_scan(*args, **kwargs):
        logging.debug(f"log_album_scan called but scan_history not available: {args}")

try:
    from helpers import detect_live_album, detect_christmas_song
except ImportError:
    def detect_live_album(album_name):
        """Fallback if helpers module not available"""
        return {"is_live": False, "is_unplugged": False}
    
    def detect_christmas_song(track_title, album_title):
        """Fallback if helpers module not available"""
        return False

try:
    from single_detector import get_current_single_detection
except ImportError:
    def get_current_single_detection(track_id: str) -> dict:
        """Fallback if single_detector not available - query database directly"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT is_single, single_confidence, single_sources, stars FROM tracks WHERE id = ?",
                (track_id,)
            )
            row = cursor.fetchone()
            conn.close()
            if row:
                is_single, confidence, sources_json, stars = row
                sources = json.loads(sources_json) if sources_json else []
                return {
                    "is_single": bool(is_single),
                    "single_confidence": confidence or "low",
                    "single_sources": sources,
                    "stars": stars or 0
                }
        except Exception as e:
            log_debug(f"Failed to get current single detection for track {track_id}: {e}")
        return {"is_single": False, "single_confidence": "low", "single_sources": [], "stars": 0}


def _now_local_iso() -> str:
    """Return ISO timestamp in configured local timezone."""
    try:
        return datetime.now(ZoneInfo(LOCAL_TZ)).isoformat()
    except Exception:
        return datetime.now().isoformat()


def get_existing_file_path(track_id: str) -> Optional[str]:
    """
    Get existing file_path from database to preserve Beets paths.
    
    Args:
        track_id: Track ID to look up
        
    Returns:
        Existing file_path or None
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT file_path FROM tracks WHERE id = ?", (track_id,))
        log_debug(f"DB Query: SELECT file_path FROM tracks WHERE id = '{track_id}'")
        row = cursor.fetchone()
        conn.close()
        result = row[0] if row and row[0] else None
        log_debug(f"Existing file_path for track {track_id}: {result}")
        return result
    except Exception as e:
        log_debug(f"get_existing_file_path failed for track_id {track_id}: {e}", exc_info=True)
        return None


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
            "percent_complete": int((processed_artists / total_artists * 100)) if total_artists > 0 else 0,
            "last_updated": datetime.now().isoformat()
        }
        with open(progress_file, 'w') as f:
            json.dump(progress, f, indent=2)
        log_debug(f"Progress saved to {progress_file}: {progress['percent_complete']}% ({processed_artists}/{total_artists})")
    except Exception as e:
        log_debug(f"Failed to save Navidrome scan progress: {e}", exc_info=True)


def scan_artist_to_db(artist_name: str, artist_id: str, verbose: bool = False, force: bool = False, filter_missing: bool = False, processed_artists: int = 0, total_artists: int = 0, album_filter: str = None):
    """
    Scan a single artist from Navidrome and persist tracks to DB.

    Args:
        artist_name: Name of the artist to scan
        artist_id: Navidrome ID of the artist
        verbose: Enable verbose logging
        force: Force re-import even if cached
        filter_missing: Only scan artists/albums with missing fields
        processed_artists: Current artist index (1-based) for progress tracking
        total_artists: Total number of artists for progress tracking
        album_filter: Only scan this specific album (if provided)
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
            log_debug(f"DB Query: SELECT album, id, {', '.join(critical_fields)} FROM tracks WHERE artist = '{artist_name}'")
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
                    if alb_name not in albums_logged:
                        log_info(f"Album '{alb_name}' flagged for re-import due to missing fields")
                        albums_logged.add(alb_name)
            conn.close()
        except Exception as e:
            log_debug(f"Prefetch existing tracks for artist '{artist_name}' failed: {e}", exc_info=True)

        albums = fetch_artist_albums(artist_id)
        log_debug(f"API Response: fetch_artist_albums returned {len(albums)} albums for artist_id={artist_id}")
        
        # If filter_missing is enabled and this artist has no missing fields, skip it
        if filter_missing and len(albums_needing_reimport) == 0 and len(existing_track_ids) > 0:
            log_debug(f"Skipping artist '{artist_name}' - no albums with missing fields (filter_missing=True)")
            return
        
        # Unified log: Simple artist-level progress only
        log_unified(f"Navidrome Import Scan - Scanning Artist {artist_name} ({len(albums)} albums to be scanned)")
        
        # Detailed logging to info
        log_info(f"Starting Navidrome import for artist: {artist_name}")
        log_info(f"Artist: {artist_name}, Total albums: {len(albums)}, Force: {force}, Filter missing: {filter_missing}, Album filter: {album_filter or 'None'}")
        
        # Debug logging for technical details
        log_debug(f"Navidrome import - Artist: {artist_name}, Artist ID: {artist_id}, Albums: {len(albums)}, Force: {force}, Filter missing: {filter_missing}, Processed: {processed_artists}/{total_artists}")
        
        # Save artist-level progress
        if total_artists > 0:
            save_navidrome_scan_progress(artist_name, processed_artists, total_artists)
            log_debug(f"Progress saved: {processed_artists}/{total_artists} artists processed")


        total_albums = len(albums)
        tracks_imported = 0
        albums_scanned = 0
        imported_track_ids = set()  # Track which tracks were imported from Navidrome
        
        for alb_idx, alb in enumerate(albums, 1):
            album_name = alb.get("name") or ""
            
            # Skip albums that don't match filter_missing (if enabled)
            if filter_missing and album_name not in albums_needing_reimport:
                log_debug(f"Skipping album '{album_name}' - no missing fields (filter_missing=True)")
                continue
            
            # Skip albums that don't match the filter (if provided)
            if album_filter and album_name.strip() != album_filter.strip():
                log_debug(f"Skipping album '{album_name}' - does not match filter '{album_filter}'")
                continue
            
            album_id = alb.get("id")
            if not album_id:
                log_debug(f"Skipping album '{album_name}' - no album ID")
                continue
            
            # Unified log: Simple album-level progress only
            log_unified(f"Navidrome Import Scan - Importing {artist_name} - {album_name}")
            
            # Detailed info logging
            log_info(f"Processing album {alb_idx}/{total_albums}: {album_name}")
            log_info(f"Album: {album_name}, Artist: {artist_name}")
            
            # Debug logging for technical details
            log_debug(f"Album details - ID: {album_id}, Name: {album_name}, Artist: {artist_name}, Index: {alb_idx}/{total_albums}")
            log_debug(f"[ALBUM_ART] Album '{album_name}' starting import. Will attempt to extract cover art from Navidrome.")
            
            # Detect if this is a live/unplugged album
            album_context = detect_live_album(album_name)
            if album_context.get("is_live") or album_context.get("is_unplugged"):
                log_info(f"Detected live/unplugged album: {album_name}")
                log_debug(f"Album context: {album_context}")
            
            try:
                album_data = fetch_album_tracks(album_id)
                tracks = album_data.get("tracks", [])
                # Get album artist from the album-level metadata (most reliable source)
                api_album_artist = album_data.get("artist", "")
                
                # Extract album art URL from Navidrome's coverArt ID
                # Navidrome returns a coverArt ID that we can use to construct the direct API URL
                navidrome_cover_art_id = album_data.get("coverArt", "")
                album_cover_art_url = ""
                if navidrome_cover_art_id:
                    # Construct the Navidrome API URL for cover art
                    # Format: {base_url}/rest/getCoverArt.view?u={username}&p={password}&v=1.12.0&c=sptnr&id={coverArtId}
                    try:
                        from config_loader import load_config
                        navidrome_config = load_config().get("musicserver", {})
                        base_url = navidrome_config.get("url", "").rstrip('/')
                        if base_url:
                            # The coverArt URL is already formatted correctly by Navidrome
                            album_cover_art_url = f"{base_url}/rest/getCoverArt.view?u={navidrome_config.get('username', '')}&p={navidrome_config.get('password', '')}&v=1.12.0&c=sptnr&id={navidrome_cover_art_id}"
                            log_debug(f"[ALBUM_ART] Constructed Navidrome cover art URL for album '{album_name}': {album_cover_art_url[:80]}...")
                    except Exception as e:
                        log_debug(f"[ALBUM_ART] Failed to construct Navidrome cover art URL: {e}")
                
                log_debug(f"API Response: fetch_album_tracks returned {len(tracks)} tracks for album_id={album_id}, album_artist='{api_album_artist}'")
                
                # Debug: Log the first track's full data structure to see what Navidrome provides
                if tracks:
                    import json
                    first_track = tracks[0]
                    log_debug(f"[NAVIDROME_API] First track raw data: {json.dumps(first_track, indent=2, default=str)}")
                    # Also check specifically for genre-related fields
                    genre_fields = {k: v for k, v in first_track.items() if 'genre' in k.lower()}
                    if genre_fields:
                        log_debug(f"[NAVIDROME_API] Genre-related fields in first track: {genre_fields}")
                    
            except Exception as e:
                log_debug(f"Failed to fetch tracks for album '{album_name}': {e}", exc_info=True)
                tracks = []
                api_album_artist = ""

            cached_ids_for_album = existing_album_tracks.get(album_name, set())

            # Note: Always import tracks from Navidrome (the source of truth for your library).
            # Don't skip based on cached count - Navidrome is authoritative.
            # The save_to_db() function handles deduplication via content matching,
            # so we won't get duplicates even if tracks were partially imported before.
            # This ensures popularity scan always has complete track data to work with.
            album_needs_reimport = album_name in albums_needing_reimport
            if album_needs_reimport:
                log_info(f"Re-importing album with missing fields: {album_name}")
                log_debug(f"Album flagged for reimport: {album_name}")
            
            # Track the number of tracks actually processed for this album
            album_tracks_processed = 0
            
            # Get the album artist with priority order:
            # 1. api_album_artist - from getAlbum.view response (most reliable)
            # 2. alb.get("artist") - from getArtist.view response 
            # 3. artist_name - the function parameter (artist we're importing)
            # Note: track.albumArtist field can be incorrect (e.g., containing track artist with feat.)
            album_artist_value = api_album_artist or alb.get("artist") or artist_name

            for t in tracks:
                track_id = t.get("id")
                if not track_id:
                    log_debug(f"Skipping track with no ID in album: {album_name}")
                    continue

                # Normalize numeric fields from Navidrome payload
                def _safe_int(val):
                    try:
                        return int(val)
                    except (TypeError, ValueError):
                        return None

                raw_track = t.get("trackNumber") if "trackNumber" in t else t.get("track")
                raw_disc = t.get("discNumber") if "discNumber" in t else t.get("disc")
                
                # Extract genre from Navidrome via Subsonic API
                # First, try to get genres from the genres array if available
                # Then fall back to the genre field with various separator formats
                
                navidrome_genre_raw = ""
                
                # Priority 1: Extract from genres array (most reliable, returns multiple genre objects)
                if t.get("genres"):
                    # Extract genre names from genres array
                    genre_names = [g.get("name", "").strip() for g in t.get("genres", []) if g.get("name", "").strip()]
                    if genre_names:
                        navidrome_genre_raw = "\\".join(genre_names)
                        log_debug(f"[GENRE] Track {track_id} - Extracted {len(genre_names)} genres from genres array: {genre_names}")
                
                # Priority 2: Fall back to genre field with various separator formats
                # Subsonic API returns genres as a single string with various separator formats:
                # - bullet: "Genre1 • Genre2 • Genre3"
                # - backslash: "Genre1\Genre2\Genre3" or "Genre1\\Genre2\\Genre3"
                # - semicolon: "Genre1; Genre2; Genre3"
                # - comma: "Genre1, Genre2, Genre3"
                if not navidrome_genre_raw and t.get("genre"):
                    navidrome_genre_raw = t.get("genre", "") or ""
                    log_debug(f"[GENRE] Track {track_id} - Falling back to genre field: '{navidrome_genre_raw}'")
                
                log_debug(f"[GENRE] Track {track_id} - Raw genre from Navidrome: '{navidrome_genre_raw}'")
                
                # Parse genres from Navidrome
                navidrome_genre_list = []
                
                if navidrome_genre_raw:
                    # First, handle case where genres are already split with • (bullet, common in Navidrome)
                    if "•" in navidrome_genre_raw:
                        # Split on bullet first (highest priority)
                        navidrome_genre_list = [g.strip() for g in navidrome_genre_raw.split("•") if g.strip()]
                        log_debug(f"[GENRE] Track {track_id} - Split on bullet separator: {navidrome_genre_list}")
                    else:
                        # Fall back to normalizing other separators to backslash
                        normalized = navidrome_genre_raw.replace(";", "\\").replace(",", "\\")
                        # Also handle cases where backslashes might be escaped
                        normalized = normalized.replace("\\\\", "\\")
                        log_debug(f"[GENRE] Track {track_id} - After separator normalization: '{normalized}'")
                        
                        # Split and clean
                        navidrome_genre_list = [g.strip() for g in normalized.split("\\") if g.strip()]
                        log_debug(f"[GENRE] Track {track_id} - Split on normalized separators: {navidrome_genre_list}")
                    
                    if navidrome_genre_list:
                        log_debug(f"[GENRE] Track {track_id} - Parsed genre list ({len(navidrome_genre_list)} genres): {navidrome_genre_list}")
                    else:
                        log_debug(f"[GENRE] Track {track_id} - Could not parse genres from: '{navidrome_genre_raw}'")
                else:
                    log_debug(f"[GENRE] Track {track_id} - No genres found in Navidrome data")
                
                # Reconstruct with double backslash for ID3 compatibility
                navidrome_genre = "\\".join(navidrome_genre_list) if navidrome_genre_list else ""
                if navidrome_genre:
                    log_debug(f"[GENRE] Track {track_id} - Reconstructed double-backslash format: '{navidrome_genre}'")
                else:
                    log_debug(f"[GENRE] Track {track_id} - No genres from Navidrome")
                
                # Detect Christmas songs and add Christmas genre
                track_title = t.get("title", "")
                is_christmas = detect_christmas_song(track_title, album_name)
                if is_christmas:
                    # Add Christmas to genre if not already present
                    if not any("christmas" in g.lower() for g in navidrome_genre_list):
                        navidrome_genre_list.append("Christmas")
                        # Update the string representation with double backslash format
                        navidrome_genre = "\\".join(navidrome_genre_list)
                        log_debug(f"[GENRE] Track {track_id} - Detected Christmas song, added 'Christmas' genre. Updated genres ({len(navidrome_genre_list)}): {navidrome_genre_list}")
                        log_debug(f"[GENRE] Track {track_id} - Final genre string for storage: '{navidrome_genre}'")
                    else:
                        log_debug(f"[GENRE] Track {track_id} - Christmas song but 'Christmas' genre already present")
                else:
                    log_debug(f"[GENRE] Track {track_id} - Not a Christmas song")
                
                # Get file path from Navidrome track data
                # Navidrome provides 'path' field which is the file path relative to music folder
                navidrome_path = t.get("path", "")
                
                # Get current single detection state to preserve user edits during Navidrome sync
                current_single = get_current_single_detection(track_id)
                log_debug(f"Track {track_id} - Current single detection: is_single={current_single['is_single']}, confidence={current_single['single_confidence']}")
                
                # During Navidrome imports, use ONLY what Navidrome provides for genres and title
                # Do NOT process through genre_title_processor which adds genres from album names
                # This ensures imports from Navidrome are clean and not affected by album-based rules
                log_debug(f"[GENRE] Track {track_id} - Using Navidrome genres directly (no processing): {navidrome_genre_list}")
                
                # Debug log: Show all genre fields being saved
                log_debug(f"[GENRE] Track {track_id} ({track_title}) - Saving to DB with genres: '{navidrome_genre}'")
                if navidrome_genre_list:
                    log_debug(f"[GENRE] Track {track_id} - Genre list has {len(navidrome_genre_list)} entries: {navidrome_genre_list}")
                
                td = {
                    "id": track_id,
                    "title": track_title,
                    "album": album_name,
                    "artist": t.get("artist", artist_name),
                    "score": 0.0,
                    "spotify_score": 0,
                    "lastfm_score": 0,
                    "listenbrainz_score": 0,
                    "age_score": 0,
                    "genres": navidrome_genre if navidrome_genre else "",  # Store with double backslash format for ID3 compatibility
                    "navidrome_genres": navidrome_genre if navidrome_genre else "",  # Store as double backslash separated string
                    "navidrome_genre": navidrome_genre,  # Also store in single genre field as double backslash separated string
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
                    "cover_art_url": album_cover_art_url,  # Album art from Navidrome
                    "lastfm_track_playcount": 0,
                    "file_path": navidrome_path if navidrome_path else None,  # Store Navidrome path for better matching
                    "last_scanned": _now_local_iso(),
                    "spotify_album_type": "",
                    "spotify_total_tracks": 0,
                    "spotify_id": None,
                    "is_spotify_single": False,
                    "is_single": current_single["is_single"],  # Preserve user edits
                    "single_confidence": current_single["single_confidence"],  # Preserve user edits
                    "single_sources": current_single["single_sources"],  # Preserve user edits
                    "mbid": t.get("mbid", "") or "",
                    "suggested_mbid": "",
                    "suggested_mbid_confidence": 0.0,
                    "stars": int(t.get("userRating", 0) or 0),
                    "duration": t.get("duration"),
                    "track_number": _safe_int(raw_track),
                    "disc_number": _safe_int(raw_disc),
                    "year": t.get("year"),
                    "album_artist": album_artist_value,
                    "bitrate": t.get("bitRate"),
                    "sample_rate": t.get("samplingRate"),
                    # ✅ Additional Navidrome metadata fields
                    "albumartist": t.get("albumArtist", "") or "",
                    "albumartistsort": t.get("albumArtistSort", "") or "",
                    "arranger": t.get("arranger", "") or "",
                    "artists": json.dumps(t.get("artists", [])) if t.get("artists") else json.dumps([]),
                    "artistsort": t.get("artistSort", "") or "",
                    "asin": t.get("asin", "") or "",
                    "barcode": t.get("barcode", "") or "",
                    "catalognumber": t.get("catalognumber", "") or "", 
                    "label": t.get("label", "") or "",
                    "media": t.get("media", "") or "",
                    "mixer": t.get("mixer", "") or "",
                    "performer": json.dumps(t.get("performer", [])) if t.get("performer") else json.dumps([]),
                    "producer": json.dumps(t.get("producer", [])) if t.get("producer") else json.dumps([]),
                    "releasecountry": t.get("releasecountry", "") or "",
                    "releasestatus": t.get("releasestatus", "") or "",
                    "releasetype": t.get("releasetype", "") or "",
                    "script": t.get("script", "") or "",
                    "work": t.get("work", "") or "",
                    "writer": json.dumps(t.get("writer", [])) if t.get("writer") else json.dumps([]),
                    # ✅ MusicBrainz IDs
                    "musicbrainz_albumartistid": t.get("musicbrainz_albumartistid", "") or "",
                    "musicbrainz_albumid": t.get("musicbrainz_albumid", "") or "",
                    "musicbrainz_albumstatus": t.get("musicbrainz_albumstatus", "") or "",
                    "musicbrainz_albumtype": t.get("musicbrainz_albumtype", "") or "",
                    "musicbrainz_releasegroupid": t.get("musicbrainz_releasegroupid", "") or "",
                    "musicbrainz_releasetrackid": t.get("musicbrainz_releasetrackid", "") or "",
                    "musicbrainz_workid": t.get("musicbrainz_workid", "") or "",
                    # ✅ Date fields
                    "originaldate": t.get("originaldate", "") or "",
                    "originalyear": _safe_int(t.get("originalyear")),
                    "totaldiscs": _safe_int(t.get("totaldiscs")) or _safe_int(album_data.get("disctotal")),
                    "tracktotal": _safe_int(t.get("tracktotal")) or _safe_int(album_data.get("totaltracks")),
                    # Store album context for single detection
                    "album_context_live": 1 if album_context.get("is_live") else 0,
                    "album_context_unplugged": 1 if album_context.get("is_unplugged") else 0,
                }
                
                # Debug log: Track data being saved
                log_debug(f"Saving track to DB - ID: {track_id}, Title: {td['title']}, Track#: {td['track_number']}, Duration: {td['duration']}s")
                # Debug log: Album art is being stored from Navidrome
                if album_cover_art_url:
                    log_debug(f"[ALBUM_ART] Album art for '{track_title}' from Navidrome: {album_cover_art_url[:80]}...")
                else:
                    log_debug(f"[ALBUM_ART] No album art available from Navidrome for '{track_title}'")
                
                # ✅ FORCE NAVIDROME UPDATES: Before calling save_to_db, check if track exists and do explicit update
                # This ensures titles and genres from Navidrome ALWAYS overwrite existing data
                conn = get_db_connection()
                cursor = conn.cursor()
                
                # Find existing track by multiple methods (file_path takes priority)
                existing_track = None
                
                # Method 1: Match by file_path (most reliable)
                if navidrome_path:
                    cursor.execute("SELECT id FROM tracks WHERE file_path = ? LIMIT 1", (navidrome_path,))
                    row = cursor.fetchone()
                    if row:
                        existing_track = row['id']
                        log_debug(f"[NAVIDROME_UPDATE] Found existing track by file_path: {existing_track}")
                
                # Method 2: Match by artist+album+track_number (track number won't change)
                if not existing_track and td['track_number']:
                    cursor.execute("""
                        SELECT id FROM tracks 
                        WHERE artist = ? AND album = ? AND track_number = ? LIMIT 1
                    """, (artist_name, album_name, td['track_number']))
                    row = cursor.fetchone()
                    if row:
                        existing_track = row['id']
                        log_debug(f"[NAVIDROME_UPDATE] Found existing track by position: {existing_track}")
                
                # Method 3: Match by duration (if duration is unique enough)
                if not existing_track and td['duration'] and td['duration'] > 0:
                    cursor.execute("""
                        SELECT id FROM tracks 
                        WHERE artist = ? AND album = ? AND ABS(duration - ?) <= 2 
                        AND title LIKE ? LIMIT 1
                    """, (artist_name, album_name, td['duration'], f"%{track_title.split()[0]}%"))  # Match first word of title
                    row = cursor.fetchone()
                    if row:
                        existing_track = row['id']
                        log_debug(f"[NAVIDROME_UPDATE] Found existing track by duration match: {existing_track}")
                
                # If track exists, explicitly UPDATE title and genres (overwrite user edits from Navidrome)
                if existing_track:
                    try:
                        # Update only the fields that Navidrome provides updates for
                        cursor.execute("""
                            UPDATE tracks 
                            SET title = ?, genres = ?, navidrome_genre = ?, navidrome_genres = ?,
                                last_scanned = ?, file_path = ?
                            WHERE id = ?
                        """, (track_title, navidrome_genre, navidrome_genre, navidrome_genre, 
                              _now_local_iso(), navidrome_path if navidrome_path else None, 
                              existing_track))
                        conn.commit()
                        log_info(f"Updated existing track {existing_track}: Title='{track_title}', Genres='{navidrome_genre}'")
                        log_debug(f"[NAVIDROME_UPDATE] Overwrote title and genres for track {existing_track}")
                        imported_track_ids.add(existing_track)
                    except Exception as e:
                        log_debug(f"[NAVIDROME_UPDATE] Failed to update track {existing_track}: {e}")
                        # Fall back to save_to_db if update fails
                        save_to_db(td)
                        imported_track_ids.add(track_id)
                else:
                    # No existing track found, use normal insert/upsert logic
                    save_to_db(td)
                    imported_track_ids.add(track_id)
                
                conn.close()
                
                # If track is Christmas and has a file path, update the genre tag in the audio file
                if is_christmas and navidrome_path:
                    try:
                        if write_genre_to_audio_file(navidrome_path, navidrome_genre):
                            log_debug(f"Updated genre tags in audio file for Christmas song: {track_title}")
                        else:
                            log_debug(f"Failed to update genre tags in audio file for: {track_title}")
                    except Exception as e:
                        log_debug(f"Error writing genre tags to audio file {navidrome_path}: {e}")
                
                album_tracks_processed += 1
                tracks_imported += 1

            # Log this album completion to scan_history
            if album_tracks_processed > 0:
                albums_scanned += 1
                
                # Info log: Detailed completion info
                log_info(f"Completed import: {artist_name} - {album_name} ({album_tracks_processed} tracks)")
                log_info(f"Scan history recorded: {artist_name} - {album_name}")
                
                # Debug log: Technical details
                log_debug(f"Album scan complete - Artist: {artist_name}, Album: {album_name}, Tracks: {album_tracks_processed}, Total tracks imported so far: {tracks_imported}")
                if album_cover_art_url:
                    log_debug(f"[ALBUM_ART] Album art stored for '{album_name}' from Navidrome")
                else:
                    log_debug(f"[ALBUM_ART] No album art available from Navidrome for '{album_name}'")
                
                log_album_scan(artist_name, album_name, 'navidrome', album_tracks_processed, 'completed')
            
            # Update progress after each album to keep progress bars responsive
            if total_artists > 0:
                save_navidrome_scan_progress(artist_name, processed_artists, total_artists)
                log_debug(f"Progress updated after album: {artist_name} - {album_name}")
        
        # Info log: Summary for artist
        log_info(f"Completed Navidrome import for artist: {artist_name}")
        log_info(f"Summary: {artist_name} - {albums_scanned} albums, {tracks_imported} tracks imported")
        
        # Debug log: Technical summary
        log_debug(f"Artist scan complete - Name: {artist_name}, Albums scanned: {albums_scanned}, Tracks imported: {tracks_imported}, Force: {force}")
        
        # Cleanup: Remove orphaned tracks (missing from Navidrome AND missing physical files)
        # IMPORTANT: Only run cleanup during full artist scans (no album_filter)
        # When filtering to a single album, we only import that album's tracks, so we can't
        # reliably detect which tracks from other albums are truly orphaned
        if not album_filter:
            try:
                tracks_removed = _cleanup_orphaned_tracks(artist_name, imported_track_ids)
                if tracks_removed > 0:
                    log_unified(f"Navidrome Import Scan - Cleanup: Removed {tracks_removed} orphaned tracks for {artist_name}")
                    log_info(f"Cleanup: Removed {tracks_removed} orphaned tracks (missing from Navidrome + physical files deleted) for artist: {artist_name}")
                    log_debug(f"Orphaned tracks cleanup complete - Artist: {artist_name}, Tracks removed: {tracks_removed}")
            except Exception as e:
                log_debug(f"Orphaned tracks cleanup failed for {artist_name}: {e}", exc_info=True)
        else:
            log_debug(f"Skipping orphaned track cleanup - album_filter '{album_filter}' specified (single album rescan)")
        
        # Fetch artist biography and images after successful import
        try:
            _fetch_artist_metadata(artist_name, verbose=verbose)
        except Exception as e:
            log_debug(f"Failed to fetch artist metadata for {artist_name}: {e}", exc_info=True)
        
        # Scan for missing releases from MusicBrainz
        try:
            _scan_missing_musicbrainz_releases(artist_name, verbose=verbose)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            # Silently skip MusicBrainz scan on connection errors - these are transient
            log_debug(f"Skipping MusicBrainz scan for {artist_name} due to connection issue: {type(e).__name__}")
        except Exception as e:
            log_debug(f"Failed to scan missing MusicBrainz releases for {artist_name}: {e}", exc_info=True)
    except Exception as e:
        # Unified log: Simple error notification
        log_unified(f"Navidrome Import Scan - ERROR importing {artist_name}")
        
        # Info log: Error details
        log_info(f"Navidrome import error for {artist_name}: {e}")
        
        # Debug log: Full error with stack trace
        log_debug(f"scan_artist_to_db failed for {artist_name}: {e}", exc_info=True)
        raise


def _cleanup_orphaned_tracks(artist_name: str, imported_track_ids: set) -> int:
    """
    Remove tracks from database that are:
    1. Not in the current Navidrome import (missing from Navidrome)
    2. AND have physical file paths that no longer exist
    
    This prevents stale database entries for deleted files while preserving
    any manually added or locally-only tracks.
    
    Args:
        artist_name: Name of the artist
        imported_track_ids: Set of track IDs that were successfully imported from Navidrome
        
    Returns:
        Number of tracks deleted
    """
    import os
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get all tracks for this artist that were NOT imported just now
        # This finds tracks that are missing from the current Navidrome scan
        cursor.execute("""
            SELECT id, title, file_path, album FROM tracks 
            WHERE artist = ? AND id NOT IN ({})
        """.format(','.join('?' * len(imported_track_ids)) if imported_track_ids else 'SELECT 1 WHERE 0'),
        (artist_name, *imported_track_ids) if imported_track_ids else (artist_name,))
        
        missing_tracks = cursor.fetchall()
        tracks_removed = 0
        
        for row in missing_tracks:
            track_id = row[0]
            title = row[1]
            file_path = row[2]
            album = row[3]
            
            # Check if the physical file exists
            file_exists = False
            if file_path:
                try:
                    file_exists = os.path.exists(file_path)
                except Exception as e:
                    log_debug(f"Error checking file path for track {track_id}: {e}")
                    file_exists = False  # If we can't check, assume it doesn't exist
            
            # Only delete if file doesn't exist AND we have a file path to check
            if file_path and not file_exists:
                try:
                    cursor.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
                    tracks_removed += 1
                    log_info(f"Cleanup: Deleted orphaned track '{title}' (Album: {album}) - file not found: {file_path}")
                    log_debug(f"Deleted orphaned track ID: {track_id}, file path was: {file_path}")
                except Exception as e:
                    log_debug(f"Failed to delete orphaned track {track_id}: {e}")
        
        conn.commit()
        conn.close()
        
        log_debug(f"Orphaned track cleanup: {tracks_removed} tracks deleted for artist {artist_name}")
        return tracks_removed
        
    except Exception as e:
        log_debug(f"Error during orphaned track cleanup for {artist_name}: {e}", exc_info=True)
        return 0


def _fetch_artist_metadata(artist_name: str, verbose: bool = False):
    """
    Fetch and store artist biography and images from external APIs.
    
    This is called after a successful artist scan to enhance artist metadata.
    Only fetches if data doesn't exist or if force=true in config.
    
    Image sources priority (in order):
    1. The AudioDB (fanart) - 30 requests/min, good quality
    2. Apple Music - unlimited, reliable
    3. MusicBrainz CAA - unlimited, good coverage
    
    Args:
        artist_name: Name of the artist
        verbose: Enable verbose logging
    """
    from api_clients.discogs import get_discogs_artist_biography
    from api_clients.applemusic import get_artist_artwork
    from api_clients.audiodb import get_artist_fanart
    from config_loader import load_config
    
    try:
        config = load_config()
        log_debug(f"Fetching artist metadata for: {artist_name}")
        
        # Check if force flag is enabled
        force = config.get("features", {}).get("force", False)
        log_debug(f"Force flag: {force}")
        
        # Check if artist metadata already exists
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Create artist_metadata table if it doesn't exist
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS artist_metadata (
                artist_name TEXT PRIMARY KEY,
                biography TEXT,
                image_url TEXT,
                updated_at TEXT
            )
        """)
        log_debug(f"DB: Ensured artist_metadata table exists")
        
        # Check for existing metadata
        cursor.execute("""
            SELECT biography, image_url 
            FROM artist_metadata 
            WHERE artist_name = ?
        """, (artist_name,))
        log_debug(f"DB Query: SELECT biography, image_url FROM artist_metadata WHERE artist_name = '{artist_name}'")
        existing_row = cursor.fetchone()
        
        # Determine what needs to be fetched
        fetch_bio = force
        fetch_image = force
        
        if existing_row and not force:
            existing_bio = existing_row[0] or ""
            existing_image = existing_row[1] or ""
            
            # Only fetch if missing
            fetch_bio = not existing_bio
            fetch_image = not existing_image
            
            if not fetch_bio and not fetch_image:
                log_info(f"Artist metadata already exists for {artist_name}, skipping fetch")
                log_debug(f"Metadata exists - Bio length: {len(existing_bio)}, Image URL: {bool(existing_image)}")
                conn.close()
                return
        
        conn.close()
        
        # Get API configurations
        discogs_config = config.get("api_integrations", {}).get("discogs", {})
        discogs_enabled = discogs_config.get("enabled", False)
        discogs_token = discogs_config.get("token", "")
        log_debug(f"Discogs config - Enabled: {discogs_enabled}, Token present: {bool(discogs_token)}")
        
        audiodb_config = config.get("api_integrations", {}).get("audiodb", {})
        audiodb_enabled = audiodb_config.get("enabled", False)
        audiodb_api_key = audiodb_config.get("api_key", "195003")
        log_debug(f"AudioDB config - Enabled: {audiodb_enabled}, API key present: {bool(audiodb_api_key)}")
        
        # Try to fetch biography from Discogs (only if needed)
        biography = ""
        if fetch_bio and discogs_enabled and discogs_token:
            log_info(f"Fetching biography for {artist_name} from Discogs...")
            log_debug(f"API Call: get_discogs_artist_biography(artist_name={artist_name})")
            bio_data = get_discogs_artist_biography(artist_name, token=discogs_token, enabled=True)
            log_debug(f"API Response: {bio_data}")
            biography = bio_data.get("profile", "")
            if biography:
                log_info(f"Retrieved artist biography from Discogs ({len(biography)} characters)")
                log_debug(f"Biography preview: {biography[:100]}...")
        
        # Try to fetch artist image with fallback chain (only if needed)
        artist_image_url = ""
        if fetch_image:
            # Priority 1: Try The AudioDB
            if audiodb_enabled and audiodb_api_key:
                log_info(f"Fetching artist image for {artist_name} from The AudioDB...")
                log_debug(f"API Call: get_artist_fanart(artist_name={artist_name})")
                artist_image_url = get_artist_fanart(artist_name, api_key=audiodb_api_key, enabled=True)
                if artist_image_url:
                    log_info(f"Retrieved artist image from The AudioDB")
                    log_debug(f"Image URL: {artist_image_url}")
            
            # Priority 2: Fall back to Apple Music if AudioDB didn't return anything
            if not artist_image_url:
                log_info(f"Fetching artist image for {artist_name} from Apple Music (AudioDB fallback)...")
                log_debug(f"API Call: get_artist_artwork(artist_name={artist_name}, size=500)")
                artist_image_url = get_artist_artwork(artist_name, size=500, enabled=True)
                if artist_image_url:
                    log_info(f"Retrieved artist image from Apple Music")
                    log_debug(f"Image URL: {artist_image_url}")
            
            # Priority 3: Fall back to MusicBrainz if still nothing found
            if not artist_image_url:
                try:
                    log_debug(f"Attempting to fetch artist image from MusicBrainz CAA...")
                    # Simple MusicBrainz artist lookup to get MBID
                    mb_search_url = "https://musicbrainz.org/ws/2/artist"
                    mb_params = {"query": f'"{artist_name}"', "fmt": "json", "limit": 1}
                    mb_headers = {"User-Agent": MUSICBRAINZ_USER_AGENT}
                    
                    session = create_retry_session(user_agent=MUSICBRAINZ_USER_AGENT, retries=3, backoff=1.0)
                    mb_resp = session.get(mb_search_url, params=mb_params, headers=mb_headers, timeout=5)
                    mb_resp.raise_for_status()
                    
                    mb_data = mb_resp.json()
                    artists = mb_data.get("artists", [])
                    
                    if artists:
                        mbid = artists[0].get("id")
                        if mbid:
                            # Construct CAA URL for artist
                            artist_image_url = f"https://coverartarchive.org/artist/{mbid}/front-500"
                            log_info(f"Retrieved artist image from MusicBrainz CAA")
                            log_debug(f"Image URL: {artist_image_url}")
                except Exception as e:
                    log_debug(f"MusicBrainz fallback failed: {e}")
        
        # Store in database
        if biography or artist_image_url:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            # Insert or update artist metadata
            cursor.execute("""
                INSERT OR REPLACE INTO artist_metadata (artist_name, biography, image_url, updated_at)
                VALUES (?, ?, ?, ?)
            """, (artist_name, biography, artist_image_url, datetime.now().isoformat()))
            log_debug(f"DB: INSERT OR REPLACE artist_metadata for {artist_name}")
            
            conn.commit()
            conn.close()
            
            log_info(f"Stored artist metadata for {artist_name}")
            log_debug(f"Metadata saved - Bio: {bool(biography)}, Image: {bool(artist_image_url)}")
    
    except Exception as e:
        log_info(f"Error fetching artist metadata for {artist_name}: {e}")
        log_debug(f"_fetch_artist_metadata error for {artist_name}: {e}", exc_info=True)


def _scan_missing_musicbrainz_releases(artist_name: str, verbose: bool = False):
    """
    Query MusicBrainz for missing singles, EPs, and albums for an artist.
    
    Compares MusicBrainz releases to what's already in the database and stores
    information about missing releases.
    
    Args:
        artist_name: Name of the artist
        verbose: Enable verbose logging
    """
    import difflib
    
    conn = None
    session = None
    try:
        log_debug(f"Starting MusicBrainz release scan for: {artist_name}")
        
        # Get existing albums from database
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT album FROM tracks WHERE artist = ?", (artist_name,))
        log_debug(f"DB Query: SELECT DISTINCT album FROM tracks WHERE artist = '{artist_name}'")
        existing_albums = {row[0].lower().strip() for row in cursor.fetchall() if row[0]}
        log_debug(f"Found {len(existing_albums)} existing albums in database")
        
        # Query MusicBrainz for all release groups
        headers = {"User-Agent": MUSICBRAINZ_USER_AGENT}
        query = f'artist:"{artist_name}" AND (primarytype:album OR primarytype:ep OR primarytype:single)'
        log_debug(f"MusicBrainz query: {query}")
        
        all_mb_releases = []
        offset = 0
        page_size = 100
        max_pages = 5  # Limit to 500 releases max
        
        log_info(f"Querying MusicBrainz for releases by {artist_name}...")
        
        # Paginate through results with improved error handling
        session = create_retry_session(user_agent=headers.get("User-Agent"), retries=5, backoff=2.0)
        for page in range(max_pages):
            retry_count = 0
            max_retries = 2
            last_error = None
            
            while retry_count < max_retries:
                try:
                    log_debug(f"MusicBrainz API call - Page {page+1}, Offset: {offset}, Limit: {page_size}")
                    resp = session.get(
                        "https://musicbrainz.org/ws/2/release-group",
                        params={"query": query, "fmt": "json", "limit": page_size, "offset": offset},
                        timeout=15
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    log_debug(f"MusicBrainz API response - Status: {resp.status_code}, Release groups: {len(data.get('release-groups', []))}")
                    
                    release_groups = data.get("release-groups", []) or []
                    if not release_groups:
                        log_debug(f"No more release groups found at offset {offset}")
                        break
                    
                    all_mb_releases.extend(release_groups)
                    
                    # Check if we've fetched all available
                    total_count = data.get("count", 0)
                    log_debug(f"Total MusicBrainz releases available: {total_count}, Fetched so far: {len(all_mb_releases)}")
                    if offset + len(release_groups) >= total_count:
                        break
                    
                    offset += page_size
                    time.sleep(2.0)  # Rate limiting - increased from 1.0 to 2.0
                    
                    # Success - exit retry loop
                    break
                    
                except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                    last_error = e
                    retry_count += 1
                    if retry_count < max_retries:
                        wait_time = 5 * retry_count  # 5s, 10s backoff
                        log_debug(f"MusicBrainz connection error for {artist_name} at offset {offset}, retry {retry_count}/{max_retries} in {wait_time}s: {e}")
                        time.sleep(wait_time)
                    else:
                        log_debug(f"MusicBrainz query failed after {max_retries} retries for {artist_name} at offset {offset}: {e}", exc_info=True)
                        break
                        
                except Exception as e:
                    log_debug(f"MusicBrainz query failed for {artist_name} at offset {offset}: {e}", exc_info=True)
                    break
            
            # If we exhausted retries, break out of page loop
            if retry_count >= max_retries and last_error:
                log_info(f"Stopping MusicBrainz scan for {artist_name} due to repeated connection errors")
                break
        
        if not all_mb_releases:
            log_info(f"No MusicBrainz releases found for {artist_name}")
            log_debug(f"MusicBrainz returned 0 releases for {artist_name}")
            return
        
        log_info(f"Retrieved {len(all_mb_releases)} releases from MusicBrainz for {artist_name}")
        log_debug(f"MusicBrainz releases fetched: {len(all_mb_releases)}")
        
        # Normalize function for title comparison
        def normalize_title(title: str) -> str:
            if not title:
                return ""
            # Remove accents
            title = unicodedata.normalize("NFKD", title)
            title = "".join(c for c in title if not unicodedata.combining(c))
            title = title.lower()
            # Remove parenthetical content and brackets
            title = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", title)
            # Remove remaster/deluxe/etc
            title = re.sub(r"(?i)\b(remaster(?:ed)?\s*\d{0,4}|deluxe|live|mono|stereo|edit|mix|version|bonus track)\b", " ", title)
            # Keep alphanumeric only
            title = re.sub(r"[^a-z0-9]+", " ", title)
            return " ".join(title.split())
        
        # Find missing releases
        missing_releases = []
        for rg in all_mb_releases:
            mb_title = rg.get("title", "")
            norm_mb_title = normalize_title(mb_title)
            
            # Skip if title is empty or matches an existing album
            if not norm_mb_title:
                continue
            
            # Check if this release already exists
            is_missing = True
            for existing in existing_albums:
                norm_existing = normalize_title(existing)
                similarity = difflib.SequenceMatcher(None, norm_mb_title, norm_existing).ratio()
                if similarity > 0.85:  # High similarity threshold
                    is_missing = False
                    log_debug(f"Release '{mb_title}' matches existing album (similarity: {similarity:.2f})")
                    break
            
            if is_missing:
                # Skip compilations
                secondary_types = rg.get("secondary-types", []) or []
                if "Compilation" in secondary_types:
                    log_debug(f"Skipping compilation: {mb_title}")
                    continue
                
                primary_type = (rg.get("primary-type") or "Album").lower()
                release_date = rg.get("first-release-date", "")
                mbid = rg.get("id", "")
                
                missing_releases.append({
                    "artist": artist_name,
                    "title": mb_title,
                    "release_type": primary_type,
                    "release_date": release_date,
                    "mbid": mbid,
                    "source": "musicbrainz"
                })
                log_debug(f"Missing release found - Title: {mb_title}, Type: {primary_type}, Date: {release_date}, MBID: {mbid}")
        
        if missing_releases:
            log_info(f"Found {len(missing_releases)} missing releases on MusicBrainz for {artist_name}")
            log_debug(f"Missing releases count: {len(missing_releases)}")
            
            # Note: missing_releases table is created by check_db.py with schema:
            # (id, artist, release_id, title, primary_type, first_release_date, 
            #  cover_art_url, category, last_checked)
            
            # Insert missing releases
            for release in missing_releases:
                cursor.execute("""
                    INSERT OR IGNORE INTO missing_releases 
                    (artist, release_id, title, primary_type, first_release_date, cover_art_url, category, last_checked)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    release["artist"],
                    release["mbid"],
                    release["title"],
                    release["release_type"],
                    release["release_date"],
                    f"https://coverartarchive.org/release-group/{release['mbid']}/front-250" if release["mbid"] else "",
                    release["release_type"].capitalize(),
                    datetime.now().isoformat()
                ))
                log_debug(f"DB: Inserted missing release - {release['title']}")
            
            conn.commit()
            
            log_info(f"Stored {len(missing_releases)} missing releases for {artist_name}")
            log_debug(f"Missing releases saved to database")
        else:
            log_info(f"No missing releases found for {artist_name}")
            log_debug(f"All MusicBrainz releases already in database")
        
    except Exception as e:
        log_info(f"Error scanning missing MusicBrainz releases for {artist_name}: {e}")
        log_debug(f"_scan_missing_musicbrainz_releases error for {artist_name}: {e}", exc_info=True)
    finally:
        if session:
            try:
                session.close()
            except Exception:
                pass
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_navidrome_library_stats(artist_map: dict) -> dict:
    """
    Calculate total albums and tracks available from Navidrome.
    
    Args:
        artist_map: Artist map from build_artist_index()
        
    Returns:
        Dict with 'total_albums' and 'total_tracks' counts from Navidrome
    """
    try:
        total_albums = sum(info.get("album_count", 0) for info in artist_map.values())
        total_tracks = 0
        
        # Count total tracks by fetching each album
        for artist_name, artist_info in artist_map.items():
            artist_id = artist_info.get("id")
            if not artist_id:
                continue
            
            try:
                albums = fetch_artist_albums(artist_id)
                for album in albums:
                    album_id = album.get("id")
                    if not album_id:
                        continue
                    
                    try:
                        album_data = fetch_album_tracks(album_id)
                        tracks = album_data.get("tracks", [])
                        total_tracks += len(tracks)
                    except Exception as e:
                        log_debug(f"Failed to fetch tracks for album {album.get('name')}: {e}")
                        continue
            except Exception as e:
                log_debug(f"Failed to fetch albums for artist {artist_name}: {e}")
                continue
        
        log_debug(f"Navidrome stats: {total_albums} albums, {total_tracks} songs")
        return {
            "total_albums": total_albums,
            "total_tracks": total_tracks
        }
    except Exception as e:
        log_debug(f"Failed to get Navidrome library stats: {e}", exc_info=True)
        return {"total_albums": 0, "total_tracks": 0}


def get_database_library_stats() -> dict:
    """
    Get library statistics from the local database.
    
    Note: Uses COUNT(DISTINCT album) which should be fast enough for typical
    library sizes. If performance becomes an issue, consider adding an index
    on the album column.
    
    Returns:
        Dict with 'total_albums' and 'total_tracks' counts from the database
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Count distinct albums
        cursor.execute("SELECT COUNT(DISTINCT album) FROM tracks WHERE album IS NOT NULL AND album != ''")
        total_albums = cursor.fetchone()[0] or 0
        
        # Count total songs/tracks
        cursor.execute("SELECT COUNT(*) FROM tracks")
        total_tracks = cursor.fetchone()[0] or 0
        
        conn.close()
        
        log_debug(f"Database stats: {total_albums} albums, {total_tracks} songs")
        return {
            "total_albums": total_albums,
            "total_tracks": total_tracks
        }
    except Exception as e:
        log_debug(f"Failed to get database library stats: {e}", exc_info=True)
        return {"total_albums": 0, "total_tracks": 0}


def scan_library_to_db(verbose: bool = False, force: bool = False):
    """
    Scan the entire Navidrome library (artists -> albums -> tracks) and persist
    a lightweight representation of each track into the local DB.

    Behavior:
      - Uses NavidromeClient API helpers: build_artist_index(), fetch_artist_albums(), fetch_album_tracks()
      - For each track, writes a minimal `track_data` record via `save_to_db()`
      - Uses INSERT OR REPLACE semantics (so re-running is safe and refreshes `last_scanned`)
      - Supports auto-resume: If an interrupted scan is detected, resumes from last scanned artist
    """
    from popularity_helpers import build_artist_index
    from scan_resume import should_resume_scan, get_artists_to_scan, mark_scan_completed
    
    # Check for interrupted scan
    should_resume, resume_from_artist = should_resume_scan("navidrome")
    
    # Unified log: Simple start notification
    if should_resume:
        log_unified(f"Navidrome Import Scan - Resuming from {resume_from_artist}")
    else:
        log_unified("Navidrome Import Scan - Starting Navidrome Import")
    
    # Info log: Detailed start information
    log_info(f"Starting Navidrome library scan")
    log_info(f"Scan parameters - Verbose: {verbose}, Force: {force}, Resume: {should_resume}")
    if should_resume:
        log_info(f"Resuming scan from artist: {resume_from_artist}")
    log_info(f"Start time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Debug log: Technical details
    log_debug(f"scan_library_to_db called with verbose={verbose}, force={force}, resume={should_resume}")
    
    def _safe_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    
    log_info("Building artist index from Navidrome...")
    log_debug("API Call: build_artist_index()")
    artist_map_local = build_artist_index(verbose=verbose) or {}
    log_debug(f"API Response: build_artist_index returned {len(artist_map_local)} artists")
    
    if not artist_map_local:
        log_unified("Navidrome Import Scan - ERROR: No artists available from Navidrome")
        log_info("No artists available from Navidrome; aborting library scan")
        log_debug("build_artist_index returned empty artist map")
        return
    
    # Optimization: Check if library totals match before scanning each album
    # Skip individual album checks if force=False and totals match
    # Note: This optimization checks both album and track counts.
    # If either count differs, the scan will proceed to update.
    # Use --force to bypass this check and always scan.
    if not force:
        log_info("Checking if library is already up-to-date (comparing album and track counts)...")
        log_debug("Getting library stats from Navidrome and database")
        
        # Get Navidrome stats
        nav_stats = get_navidrome_library_stats(artist_map_local)
        navidrome_album_count = nav_stats.get("total_albums", 0)
        navidrome_track_count = nav_stats.get("total_tracks", 0)
        
        # Get database stats
        db_stats = get_database_library_stats()
        db_album_count = db_stats.get("total_albums", 0)
        db_track_count = db_stats.get("total_tracks", 0)
        
        log_info(f"Navidrome: {navidrome_album_count} albums, {navidrome_track_count} songs")
        log_info(f"Database: {db_album_count} albums, {db_track_count} songs")
        log_debug(f"Library comparison - Albums: Nav={navidrome_album_count} vs DB={db_album_count}, Tracks: Nav={navidrome_track_count} vs DB={db_track_count}")
        
        # Skip scan only if BOTH album and track counts match
        if (navidrome_album_count > 0 and navidrome_track_count > 0 and
            navidrome_album_count == db_album_count and 
            navidrome_track_count == db_track_count):
            log_unified("Navidrome Import Scan - Library already up-to-date, skipping scan")
            log_info(f"Library is already up-to-date ({db_album_count} albums, {db_track_count} songs)")
            log_info("Use --force to re-import all tracks")
            log_debug("Early exit: both album and track counts match, skipping detailed scan")
            return
        
        # If counts don't match, log which count(s) differ
        if navidrome_album_count != db_album_count:
            log_info(f"Album count mismatch: Navidrome has {navidrome_album_count}, database has {db_album_count}")
        if navidrome_track_count != db_track_count:
            log_info(f"Track count mismatch: Navidrome has {navidrome_track_count}, database has {db_track_count}")
        log_info("Proceeding with full library scan to sync differences")

    # Cache existing track IDs to avoid re-writing cached rows unless force=True
    existing_track_ids: set[str] = set()
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM tracks")
        log_debug("DB Query: SELECT id FROM tracks")
        existing_track_ids = {row[0] for row in cursor.fetchall()}
        log_debug(f"Found {len(existing_track_ids)} existing tracks in database")
        conn.close()
    except Exception as e:
        log_debug(f"Prefetch existing track IDs failed: {e}", exc_info=True)

    # Get list of artists already in database and their track counts
    db_artists: dict[str, int] = {}  # artist_name -> track_count
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT artist, COUNT(*) as track_count FROM tracks GROUP BY artist")
        log_debug("DB Query: SELECT artist, COUNT(*) as track_count FROM tracks GROUP BY artist")
        db_artists = {row[0]: row[1] for row in cursor.fetchall() if row[0]}
        log_debug(f"Found {len(db_artists)} artists in database with track counts")
        conn.close()
    except Exception as e:
        log_debug(f"Failed to fetch existing artists from database: {e}", exc_info=True)

    # Detect missing artists (in Navidrome but not in database)
    missing_artists = []
    artists_with_mismatched_counts = []
    
    for artist_name in artist_map_local.keys():
        if artist_name not in db_artists:
            missing_artists.append(artist_name)
            log_info(f"🆕 Missing artist detected: {artist_name}")
            log_debug(f"Artist '{artist_name}' is in Navidrome but not in database")
        else:
            # Get track count from Navidrome for this artist
            try:
                artist_id = artist_map_local[artist_name].get("id")
                if artist_id:
                    albums = fetch_artist_albums(artist_id)
                    nav_track_count = 0
                    for album in albums:
                        album_id = album.get("id")
                        if album_id:
                            try:
                                album_data = fetch_album_tracks(album_id)
                                tracks = album_data.get("tracks", [])
                                nav_track_count += len(tracks)
                            except Exception as e:
                                log_debug(f"Failed to fetch tracks for album {album.get('name')}: {e}")
                                continue
                    
                    db_track_count = db_artists[artist_name]
                    if nav_track_count != db_track_count:
                        artists_with_mismatched_counts.append({
                            "name": artist_name,
                            "navidrome_count": nav_track_count,
                            "database_count": db_track_count
                        })
                        log_info(f"⚠️ Track count mismatch for {artist_name}: Navidrome={nav_track_count}, Database={db_track_count}")
                        log_debug(f"Artist '{artist_name}' has different track counts: Nav={nav_track_count} vs DB={db_track_count}")
            except Exception as e:
                log_debug(f"Failed to get track count for existing artist '{artist_name}': {e}")
    
    if missing_artists:
        log_unified(f"Navidrome Import Scan - Found {len(missing_artists)} missing artists to import")
        log_info(f"Found {len(missing_artists)} missing artists from Navidrome")
        log_debug(f"Missing artists: {missing_artists}")
    
    if artists_with_mismatched_counts:
        log_unified(f"Navidrome Import Scan - Found {len(artists_with_mismatched_counts)} artists with mismatched track counts")
        log_info(f"Found {len(artists_with_mismatched_counts)} artists with different track counts in Navidrome vs database")
        for artist_info in artists_with_mismatched_counts:
            log_debug(f"Mismatch: {artist_info['name']} (Nav={artist_info['navidrome_count']} vs DB={artist_info['database_count']})")

    total_written = 0
    total_skipped = 0
    total_albums_skipped = 0
    
    # Get artist list and apply resume logic
    all_artists = list(artist_map_local.keys())
    total_artists = len(all_artists)
    
    # Get artists to scan (may skip already scanned if resuming)
    artists_to_scan = get_artists_to_scan(all_artists, resume_from_artist if should_resume else None)
    artists_to_scan_count = len(artists_to_scan)
    
    # Calculate starting index for progress tracking
    artist_start_index = total_artists - artists_to_scan_count
    artist_count = artist_start_index
    
    if should_resume:
        log_info(f"Resuming scan: {artists_to_scan_count} artists remaining ({artist_start_index} already scanned)")
        log_debug(f"Resume: Starting from index {artist_start_index + 1}/{total_artists}")
    else:
        log_info(f"Starting scan of {total_artists} artists from Navidrome")
        log_debug(f"Total artists to scan: {total_artists}")
    
    log_info(f"Missing artists found: {len(missing_artists)}, Artists with mismatched counts: {len(artists_with_mismatched_counts)}")
    
    for name in artists_to_scan:
        artist_count += 1
        info = artist_map_local.get(name)
        if not info:
            log_debug(f"Artist '{name}' not found in artist map")
            continue
            
        artist_id = info.get("id")
        if not artist_id:
            log_info(f"Skipping artist '{name}' - no artist ID available")
            log_debug(f"Artist '{name}' has no ID in artist map: {info}")
            continue
        
        log_debug(f"Processing artist {artist_count}/{total_artists}: {name} (ID: {artist_id})")

        try:
            # Use the consolidated scan_artist_to_db function
            scan_artist_to_db(name, artist_id, verbose=verbose, force=force, processed_artists=artist_count, total_artists=total_artists)
        except Exception as e:
            log_info(f"Failed to scan artist '{name}': {e}")
            log_debug(f"scan_artist_to_db failed for '{name}': {e}", exc_info=True)
    
    # Info log: Detailed completion summary
    log_unified(f"Navidrome Import Scan - Complete: {len(missing_artists)} new artists added")
    log_info(f"Navidrome library scan complete")
    log_info(f"End time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log_info(f"Summary:")
    log_info(f"  - Total artists scanned: {total_artists}")
    log_info(f"  - Missing artists imported: {len(missing_artists)}")
    log_info(f"  - Artists with mismatched track counts: {len(artists_with_mismatched_counts)}")
    if missing_artists:
        log_info(f"  - Newly imported: {', '.join(missing_artists[:5])}" + (" and more..." if len(missing_artists) > 5 else ""))
    if artists_with_mismatched_counts:
        log_info(f"  - Track mismatches detected in: {', '.join([a['name'] for a in artists_with_mismatched_counts[:5]])}" + (" and more..." if len(artists_with_mismatched_counts) > 5 else ""))
    
    # Debug log: Technical summary
    log_debug(f"Library scan complete - Artists: {total_artists}, Missing: {len(missing_artists)}, Mismatched: {len(artists_with_mismatched_counts)}, Verbose: {verbose}, Force: {force}")
    
    # Mark scan as completed and clear resume state
    mark_scan_completed("navidrome")
    log_info("Scan completed successfully, progress file cleared")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Navidrome import module - import metadata from Navidrome to local DB")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output")
    parser.add_argument("--force", action="store_true", help="Force re-import of all tracks")
    parser.add_argument("--artist", type=str, help="Import specific artist by name")
    
    args = parser.parse_args()
    
    log_info(f"Navidrome import started with args: verbose={args.verbose}, force={args.force}, artist={args.artist}")
    log_debug(f"Command line arguments: {args}")
    
    if args.artist:
        # Import single artist
        log_info(f"Single artist import requested: {args.artist}")
        log_debug(f"Building artist index to find artist: {args.artist}")
        from popularity_helpers import build_artist_index
        artist_map = build_artist_index()
        log_debug(f"Artist map built with {len(artist_map)} artists")
        artist_info = artist_map.get(args.artist)
        if artist_info:
            log_info(f"Found artist in Navidrome: {args.artist}")
            log_debug(f"Artist info: {artist_info}")
            scan_artist_to_db(args.artist, artist_info['id'], verbose=args.verbose, force=args.force)
        else:
            log_info(f"Artist '{args.artist}' not found in Navidrome")
            log_debug(f"Artist '{args.artist}' not in artist_map keys: {list(artist_map.keys())}")
            print(f"❌ Artist '{args.artist}' not found in Navidrome")
    else:
        # Import entire library
        log_info("Full library import requested")
        log_debug("Starting full Navidrome library scan")
        scan_library_to_db(verbose=args.verbose, force=args.force)
