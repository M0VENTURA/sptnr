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

try:
    from scan_history import log_album_scan
    _scan_history_available = True
except ImportError as e:
    logging.warning(f"scan_history module not available: {e}")
    _scan_history_available = False
    def log_album_scan(*args, **kwargs):
        logging.debug(f"log_album_scan called but scan_history not available: {args}")

try:
    from helpers import detect_live_album
except ImportError:
    def detect_live_album(album_name):
        """Fallback if helpers module not available"""
        return {"is_live": False, "is_unplugged": False}

try:
    from single_detector import get_current_single_detection
except ImportError:
    def get_current_single_detection(track_id: str) -> dict:
        """Fallback if single_detector not available"""
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
        try:
            log_debug(f"get_existing_file_path failed for track_id {track_id}: {e}", exc_info=True)
        except Exception:
            pass
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
            "percent_complete": int((processed_artists / total_artists * 100)) if total_artists > 0 else 0
        }
        with open(progress_file, 'w') as f:
            json.dump(progress, f, indent=2)
        log_debug(f"Progress saved to {progress_file}: {progress['percent_complete']}% ({processed_artists}/{total_artists})")
    except Exception as e:
        log_debug(f"Failed to save Navidrome scan progress: {e}", exc_info=True)


def scan_artist_to_db(artist_name: str, artist_id: str, verbose: bool = False, force: bool = False, processed_artists: int = 0, total_artists: int = 0, album_filter: str = None):
    """
    Scan a single artist from Navidrome and persist tracks to DB.

    Args:
        artist_name: Name of the artist to scan
        artist_id: Navidrome ID of the artist
        verbose: Enable verbose logging
        force: Force re-import even if cached
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
        
        # Unified log: Simple artist-level progress only
        log_unified(f"Navidrome Import Scan - Scanning Artist {artist_name} ({len(albums)} albums to be scanned)")
        
        # Detailed logging to info
        log_info(f"Starting Navidrome import for artist: {artist_name}")
        log_info(f"Artist: {artist_name}, Total albums: {len(albums)}, Force: {force}, Album filter: {album_filter or 'None'}")
        
        # Debug logging for technical details
        log_debug(f"Navidrome import - Artist: {artist_name}, Artist ID: {artist_id}, Albums: {len(albums)}, Force: {force}, Processed: {processed_artists}/{total_artists}")
        
        # Save artist-level progress
        if total_artists > 0:
            save_navidrome_scan_progress(artist_name, processed_artists, total_artists)
            log_debug(f"Progress saved: {processed_artists}/{total_artists} artists processed")


        total_albums = len(albums)
        tracks_imported = 0
        albums_scanned = 0
        
        for alb_idx, alb in enumerate(albums, 1):
            album_name = alb.get("name") or ""
            
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
            
            # Detect if this is a live/unplugged album
            album_context = detect_live_album(album_name)
            if album_context.get("is_live") or album_context.get("is_unplugged"):
                log_info(f"Detected live/unplugged album: {album_name}")
                log_debug(f"Album context: {album_context}")
            
            try:
                tracks = fetch_album_tracks(album_id)
                log_debug(f"API Response: fetch_album_tracks returned {len(tracks)} tracks for album_id={album_id}")
            except Exception as e:
                log_debug(f"Failed to fetch tracks for album '{album_name}': {e}", exc_info=True)
                tracks = []

            cached_ids_for_album = existing_album_tracks.get(album_name, set())

            # Skip album only if it's already cached AND doesn't need re-import due to missing fields
            album_needs_reimport = album_name in albums_needing_reimport
            if not force and not album_needs_reimport and tracks and len(cached_ids_for_album) >= len(tracks):
                # Unified log: Note when album is skipped
                log_unified(f"Navidrome Import Scan - Skipped {album_name} (already cached)")
                
                # Info log: More details
                log_info(f"Skipping cached album: {album_name} ({len(cached_ids_for_album)} tracks already in database)")
                
                # Debug log: Technical details
                log_debug(f"Album skip decision - Album: {album_name}, Cached tracks: {len(cached_ids_for_album)}, Navidrome tracks: {len(tracks)}, Force: {force}, Needs reimport: {album_needs_reimport}")
                
                # Still log skipped albums to scan history
                log_album_scan(artist_name, album_name, 'navidrome', len(cached_ids_for_album), 'skipped')
                continue

            if album_needs_reimport:
                log_info(f"Re-importing album with missing fields: {album_name}")
                log_debug(f"Album flagged for reimport: {album_name}")
            
            # Track the number of tracks actually processed for this album
            album_tracks_processed = 0

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
                
                # Extract genre from Navidrome and use it as the initial genres value
                navidrome_genre = t.get("genre", "")
                navidrome_genre_list = [navidrome_genre] if navidrome_genre else []
                
                # Get current single detection state to preserve user edits during Navidrome sync
                current_single = get_current_single_detection(track_id)
                log_debug(f"Track {track_id} - Current single detection: is_single={current_single['is_single']}, confidence={current_single['single_confidence']}")
                
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
                    "file_path": None,  # Leave file_path unset for Navidrome; beets import owns the canonical path
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
                    "album_artist": t.get("albumArtist", ""),
                    "bitrate": t.get("bitRate"),
                    "sample_rate": t.get("samplingRate"),
                    # Store album context for single detection
                    "album_context_live": 1 if album_context.get("is_live") else 0,
                    "album_context_unplugged": 1 if album_context.get("is_unplugged") else 0,
                }
                
                # Debug log: Track data being saved
                log_debug(f"Saving track to DB - ID: {track_id}, Title: {td['title']}, Track#: {td['track_number']}, Duration: {td['duration']}s")
                
                save_to_db(td)
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
        
        # Fetch artist biography and images after successful import
        try:
            _fetch_artist_metadata(artist_name, verbose=verbose)
        except Exception as e:
            log_debug(f"Failed to fetch artist metadata for {artist_name}: {e}", exc_info=True)
        
        # Scan for missing releases from MusicBrainz
        try:
            _scan_missing_musicbrainz_releases(artist_name, verbose=verbose)
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


def _fetch_artist_metadata(artist_name: str, verbose: bool = False):
    """
    Fetch and store artist biography and images from external APIs.
    
    This is called after a successful artist scan to enhance artist metadata.
    Only fetches if data doesn't exist or if force=true in config.
    
    Args:
        artist_name: Name of the artist
        verbose: Enable verbose logging
    """
    from api_clients.discogs import get_discogs_artist_biography
    from api_clients.applemusic import get_artist_artwork
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
        
        # Get Discogs configuration
        discogs_config = config.get("api_integrations", {}).get("discogs", {})
        discogs_enabled = discogs_config.get("enabled", False)
        discogs_token = discogs_config.get("token", "")
        log_debug(f"Discogs config - Enabled: {discogs_enabled}, Token present: {bool(discogs_token)}")
        
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
        
        # Try to fetch artist image from Apple Music (only if needed)
        artist_image_url = ""
        if fetch_image:
            log_info(f"Fetching artist image for {artist_name} from Apple Music...")
            log_debug(f"API Call: get_artist_artwork(artist_name={artist_name}, size=500)")
            artist_image_url = get_artist_artwork(artist_name, size=500, enabled=True)
            log_debug(f"API Response: {artist_image_url}")
            if artist_image_url:
                log_info(f"Retrieved artist image from Apple Music")
                log_debug(f"Image URL: {artist_image_url}")
        
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
    import requests
    import difflib
    import unicodedata
    import re
    
    conn = None
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
        headers = {"User-Agent": "sptnr-cli/1.0 (https://github.com/M0VENTURA/sptnr)"}
        query = f'artist:"{artist_name}" AND (primarytype:album OR primarytype:ep OR primarytype:single)'
        log_debug(f"MusicBrainz query: {query}")
        
        all_mb_releases = []
        offset = 0
        page_size = 100
        max_pages = 5  # Limit to 500 releases max
        
        log_info(f"Querying MusicBrainz for releases by {artist_name}...")
        
        # Paginate through results
        for page in range(max_pages):
            try:
                log_debug(f"MusicBrainz API call - Page {page+1}, Offset: {offset}, Limit: {page_size}")
                resp = requests.get(
                    "https://musicbrainz.org/ws/2/release-group",
                    params={"query": query, "fmt": "json", "limit": page_size, "offset": offset},
                    headers=headers,
                    timeout=10
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
                time.sleep(1.0)  # Rate limiting
                
            except Exception as e:
                log_debug(f"MusicBrainz query failed for {artist_name} at offset {offset}: {e}", exc_info=True)
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
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def scan_library_to_db(verbose: bool = False, force: bool = False):
    """
    Scan the entire Navidrome library (artists -> albums -> tracks) and persist
    a lightweight representation of each track into the local DB.

    Behavior:
      - Uses NavidromeClient API helpers: build_artist_index(), fetch_artist_albums(), fetch_album_tracks()
      - For each track, writes a minimal `track_data` record via `save_to_db()`
      - Uses INSERT OR REPLACE semantics (so re-running is safe and refreshes `last_scanned`)
    """
    from popularity_helpers import build_artist_index
    
    # Unified log: Simple start notification
    log_unified("Navidrome Import Scan - Starting Navidrome Import")
    
    # Info log: Detailed start information
    log_info(f"Starting Navidrome library scan")
    log_info(f"Scan parameters - Verbose: {verbose}, Force: {force}")
    log_info(f"Start time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Debug log: Technical details
    log_debug(f"scan_library_to_db called with verbose={verbose}, force={force}")
    
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

    total_written = 0
    total_skipped = 0
    total_albums_skipped = 0
    total_artists = len(artist_map_local)
    artist_count = 0
    
    log_info(f"Starting scan of {total_artists} artists from Navidrome")
    log_debug(f"Total artists to scan: {total_artists}")
    
    for name, info in artist_map_local.items():
        artist_count += 1
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
    log_info(f"Navidrome library scan complete")
    log_info(f"End time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log_info(f"Total artists scanned: {total_artists}")
    
    # Debug log: Technical summary
    log_debug(f"Library scan complete - Artists: {total_artists}, Verbose: {verbose}, Force: {force}")


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
