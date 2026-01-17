#!/usr/bin/env python3
"""
Navidrome Import Module - Handles importing metadata from Navidrome to local database.

This module is responsible for:
- Scanning artists from Navidrome
- Importing album and track metadata
- Logging to unified_scan.log
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
LOG_PATH = os.environ.get("LOG_PATH", "/config/sptnr.log")
UNIFIED_LOG_PATH = os.environ.get("UNIFIED_SCAN_LOG_PATH", "/config/unified_scan.log")
SERVICE_PREFIX = "navidrome_import_"
LOCAL_TZ = os.environ.get("TIMEZONE") or os.environ.get("TZ") or "UTC"

# --- Logging Setup ---
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

# Dedicated logger for unified_scan.log
unified_logger = logging.getLogger("unified_scan")
unified_file_handler = logging.FileHandler(UNIFIED_LOG_PATH)
# Use a clean formatter without service prefix for unified log
unified_formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
unified_file_handler.setFormatter(unified_formatter)
unified_logger.setLevel(logging.INFO)
if not unified_logger.hasHandlers():
    unified_logger.addHandler(unified_file_handler)
unified_logger.propagate = False

def log_unified(msg):
    """Log to unified_scan.log"""
    unified_logger.info(msg)
    for handler in unified_logger.handlers:
        try:
            handler.flush()
        except Exception:
            pass

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
        row = cursor.fetchone()
        conn.close()
        return row[0] if row and row[0] else None
    except Exception:
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
    except Exception as e:
        logging.error(f"Failed to save Navidrome scan progress: {e}")


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
        if album_filter:
            log_unified(f"🎤 [Navidrome] Starting import for album: {artist_name} - {album_filter}")
        else:
            log_unified(f"🎤 [Navidrome] Starting import for artist: {artist_name}")
        
        # Prefetch cached track IDs for this artist and check for missing critical fields
        existing_track_ids: set[str] = set()
        existing_album_tracks: dict[str, set[str]] = {}
        albums_needing_reimport: set[str] = set()  # Track albums with missing fields
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
                    if verbose:
                        logging.info(f"Album '{alb_name}' flagged for re-import due to missing fields")
            conn.close()
        except Exception as e:
            logging.debug(f"Prefetch existing tracks for artist '{artist_name}' failed: {e}")

        albums = fetch_artist_albums(artist_id)
        if verbose:
            print(f"🎤 Scanning artist: {artist_name} ({len(albums)} albums)")
        log_unified(f"   💿 Found {len(albums)} albums for {artist_name}")
        logging.info(f"🎤 [Navidrome] Scanning artist: {artist_name} ({len(albums)} albums)")
        
        # Save artist-level progress
        if total_artists > 0:
            save_navidrome_scan_progress(artist_name, processed_artists, total_artists)

        total_albums = len(albums)
        tracks_imported = 0
        albums_scanned = 0
        
        for alb_idx, alb in enumerate(albums, 1):
            album_name = alb.get("name") or ""
            
            # Skip albums that don't match the filter (if provided)
            if album_filter and album_name.strip() != album_filter.strip():
                continue
            
            album_id = alb.get("id")
            if not album_id:
                continue
            
            log_unified(f"      💿 [Album {alb_idx}/{total_albums}] {album_name}")
            logging.info(f"   💿 [Album {alb_idx}/{total_albums}] {album_name}")
            
            # Detect if this is a live/unplugged album
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
                log_unified(f"         ⏩ Skipped (already cached): {album_name}")
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
                
                # Get current single detection state to preserve user edits during Navidrome sync
                current_single = get_current_single_detection(track_id)
                
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
                save_to_db(td)
                album_tracks_processed += 1
                tracks_imported += 1

            # Log this album completion to scan_history
            if album_tracks_processed > 0:
                albums_scanned += 1
                logging.info(f"Logging to scan_history: {artist_name} - {album_name} ({album_tracks_processed} tracks)")
                log_album_scan(artist_name, album_name, 'navidrome', album_tracks_processed, 'completed')
                log_unified(f"         ✓ Imported {album_tracks_processed} tracks from {album_name}")
                logging.info(f"Completed navidrome scan for {artist_name} - {album_name} ({album_tracks_processed} tracks)")
            
            # Update progress after each album to keep progress bars responsive
            if total_artists > 0:
                save_navidrome_scan_progress(artist_name, processed_artists, total_artists)
        
        log_unified(f"✅ [Navidrome] Completed import for {artist_name}: {albums_scanned} albums, {tracks_imported} tracks")
        if verbose:
            print(f"Artist scan complete: {artist_name}")
            logging.info(f"Artist scan complete: {artist_name}")
        
        # Fetch artist biography and images after successful import
        try:
            _fetch_artist_metadata(artist_name, verbose=verbose)
        except Exception as e:
            logging.debug(f"Failed to fetch artist metadata for {artist_name}: {e}")
        
        # Scan for missing releases from MusicBrainz
        try:
            _scan_missing_musicbrainz_releases(artist_name, verbose=verbose)
        except Exception as e:
            logging.debug(f"Failed to scan missing MusicBrainz releases for {artist_name}: {e}")
    except Exception as e:
        log_unified(f"❌ [Navidrome] Import failed for {artist_name}: {e}")
        logging.error(f"scan_artist_to_db failed for {artist_name}: {e}")
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
        
        # Check if force flag is enabled
        force = config.get("features", {}).get("force", False)
        
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
        
        # Check for existing metadata
        cursor.execute("""
            SELECT biography, image_url 
            FROM artist_metadata 
            WHERE artist_name = ?
        """, (artist_name,))
        existing_row = cursor.fetchone()
        
        # If metadata exists and force is not enabled, skip fetching
        if existing_row and not force:
            existing_bio = existing_row[0] if existing_row[0] else ""
            existing_image = existing_row[1] if len(existing_row) > 1 and existing_row[1] else ""
            
            if existing_bio or existing_image:
                if verbose:
                    logging.info(f"Artist metadata already exists for {artist_name}, skipping fetch (use force=true to re-fetch)")
                conn.close()
                return
        
        conn.close()
        
        # Get Discogs configuration
        discogs_config = config.get("api_integrations", {}).get("discogs", {})
        discogs_enabled = discogs_config.get("enabled", False)
        discogs_token = discogs_config.get("token", "")
        
        # Try to fetch biography from Discogs
        biography = ""
        if discogs_enabled and discogs_token:
            if verbose:
                logging.info(f"Fetching biography for {artist_name} from Discogs...")
            bio_data = get_discogs_artist_biography(artist_name, token=discogs_token, enabled=True)
            biography = bio_data.get("profile", "")
            if biography:
                log_unified(f"   📖 Retrieved artist biography from Discogs ({len(biography)} chars)")
        
        # Try to fetch artist image from Apple Music (iTunes Search API - no auth needed)
        artist_image_url = ""
        if verbose:
            logging.info(f"Fetching artist image for {artist_name} from Apple Music...")
        artist_image_url = get_artist_artwork(artist_name, size=500, enabled=True)
        if artist_image_url:
            log_unified(f"   🖼️  Retrieved artist image from Apple Music")
        
        # Store in database
        if biography or artist_image_url:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            # Insert or update artist metadata
            cursor.execute("""
                INSERT OR REPLACE INTO artist_metadata (artist_name, biography, image_url, updated_at)
                VALUES (?, ?, ?, ?)
            """, (artist_name, biography, artist_image_url, datetime.now().isoformat()))
            
            conn.commit()
            conn.close()
            
            if verbose:
                logging.info(f"Stored artist metadata for {artist_name}")
    
    except Exception as e:
        logging.debug(f"Error fetching artist metadata for {artist_name}: {e}")


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
        # Get existing albums from database
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT album FROM tracks WHERE artist = ?", (artist_name,))
        existing_albums = {row[0].lower().strip() for row in cursor.fetchall() if row[0]}
        
        # Query MusicBrainz for all release groups
        headers = {"User-Agent": "sptnr-cli/1.0 (https://github.com/M0VENTURA/sptnr)"}
        query = f'artist:"{artist_name}" AND (primarytype:album OR primarytype:ep OR primarytype:single)'
        
        all_mb_releases = []
        offset = 0
        page_size = 100
        max_pages = 5  # Limit to 500 releases max
        
        if verbose:
            logging.info(f"Querying MusicBrainz for releases by {artist_name}...")
        
        # Paginate through results
        for page in range(max_pages):
            try:
                resp = requests.get(
                    "https://musicbrainz.org/ws/2/release-group",
                    params={"query": query, "fmt": "json", "limit": page_size, "offset": offset},
                    headers=headers,
                    timeout=10
                )
                resp.raise_for_status()
                data = resp.json()
                
                release_groups = data.get("release-groups", []) or []
                if not release_groups:
                    break
                
                all_mb_releases.extend(release_groups)
                
                # Check if we've fetched all available
                total_count = data.get("count", 0)
                if offset + len(release_groups) >= total_count:
                    break
                
                offset += page_size
                time.sleep(1.0)  # Rate limiting
                
            except Exception as e:
                logging.debug(f"MusicBrainz query failed for {artist_name} at offset {offset}: {e}")
                break
        
        if not all_mb_releases:
            if verbose:
                logging.info(f"No MusicBrainz releases found for {artist_name}")
            return
        
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
                    break
            
            if is_missing:
                # Skip compilations
                secondary_types = rg.get("secondary-types", []) or []
                if "Compilation" in secondary_types:
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
        
        if missing_releases:
            log_unified(f"   🔍 Found {len(missing_releases)} missing releases on MusicBrainz")
            
            # Create missing_releases table if it doesn't exist
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS missing_releases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    artist TEXT NOT NULL,
                    title TEXT NOT NULL,
                    release_type TEXT NOT NULL,
                    release_date TEXT,
                    mbid TEXT,
                    source TEXT DEFAULT 'musicbrainz',
                    discovered_at TEXT NOT NULL,
                    UNIQUE(artist, title, source)
                )
            """)
            
            # Insert missing releases
            for release in missing_releases:
                cursor.execute("""
                    INSERT OR IGNORE INTO missing_releases 
                    (artist, title, release_type, release_date, mbid, source, discovered_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    release["artist"],
                    release["title"],
                    release["release_type"],
                    release["release_date"],
                    release["mbid"],
                    release["source"],
                    datetime.now().isoformat()
                ))
            
            conn.commit()
            
            if verbose:
                logging.info(f"Stored {len(missing_releases)} missing releases for {artist_name}")
        else:
            if verbose:
                logging.info(f"No missing releases found for {artist_name}")
        
    except Exception as e:
        logging.debug(f"Error scanning missing MusicBrainz releases for {artist_name}: {e}")
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
    
    log_unified("\n🟢 ==================== NAVIDROME LIBRARY SCAN STARTED ==================== 🟢")
    log_unified(f"🕒 Start Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log_unified("=" * 70)
    
    def _safe_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    
    print("🔎 Scanning Navidrome library into local DB...")
    log_unified("🔎 Scanning Navidrome library into local DB...")
    
    artist_map_local = build_artist_index(verbose=verbose) or {}
    if not artist_map_local:
        print("⚠️ No artists available from Navidrome; aborting library scan.")
        log_unified("⚠️ No artists available from Navidrome; aborting library scan.")
        return

    # Cache existing track IDs to avoid re-writing cached rows unless force=True
    existing_track_ids: set[str] = set()
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM tracks")
        existing_track_ids = {row[0] for row in cursor.fetchall()}
        conn.close()
    except Exception as e:
        logging.debug(f"Prefetch existing track IDs failed: {e}")

    total_written = 0
    total_skipped = 0
    total_albums_skipped = 0
    total_artists = len(artist_map_local)
    artist_count = 0
    
    print(f"📊 Starting scan of {total_artists} artists...")
    log_unified(f"📊 Starting scan of {total_artists} artists...")
    
    for name, info in artist_map_local.items():
        artist_count += 1
        artist_id = info.get("id")
        if not artist_id:
            print(f"⚠️ [{artist_count}/{total_artists}] Skipping '{name}' (no artist ID)")
            log_unified(f"⚠️ [{artist_count}/{total_artists}] Skipping '{name}' (no artist ID)")
            continue
        
        print(f"🎨 [{artist_count}/{total_artists}] Processing artist: {name}")
        logging.debug(f"Processing artist {artist_count}/{total_artists}: {name} (ID: {artist_id})")

        try:
            # Use the consolidated scan_artist_to_db function
            scan_artist_to_db(name, artist_id, verbose=verbose, force=force, processed_artists=artist_count, total_artists=total_artists)
        except Exception as e:
            print(f"   ❌ Failed to scan artist: {e}")
            logging.error(f"Failed to scan artist '{name}': {e}")
    
    log_unified("")
    log_unified("🟢 ==================== NAVIDROME LIBRARY SCAN COMPLETE ==================== 🟢")
    log_unified(f"🏁 End Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log_unified(f"✅ Scanned {total_artists} artists")
    log_unified("=" * 70)
    print(f"✅ Library scan complete. {total_artists} artists scanned.")
    logging.info(f"Library scan complete. {total_artists} artists scanned.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Navidrome import module - import metadata from Navidrome to local DB")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output")
    parser.add_argument("--force", action="store_true", help="Force re-import of all tracks")
    parser.add_argument("--artist", type=str, help="Import specific artist by name")
    
    args = parser.parse_args()
    
    if args.artist:
        # Import single artist
        from popularity_helpers import build_artist_index
        artist_map = build_artist_index()
        artist_info = artist_map.get(args.artist)
        if artist_info:
            scan_artist_to_db(args.artist, artist_info['id'], verbose=args.verbose, force=args.force)
        else:
            print(f"❌ Artist '{args.artist}' not found in Navidrome")
    else:
        # Import entire library
        scan_library_to_db(verbose=args.verbose, force=args.force)
