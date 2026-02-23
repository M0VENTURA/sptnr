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
    from db_utils import DB_PATH
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
        
        # If filter_missing is enabled and this artist has no missing fields, skip it
        if filter_missing and len(albums_needing_reimport) == 0 and len(existing_track_ids) > 0:
            logging.debug(f"Skipping artist '{artist_name}' - no albums with missing fields (filter_missing=True)")
            return
        
        if verbose:
            print(f"🎤 Scanning artist: {artist_name} ({len(albums)} albums)")
        logging.info(f"🎤 [Navidrome] Scanning artist: {artist_name} ({len(albums)} albums, force={force}, filter_missing={filter_missing}, album_filter={album_filter or 'None'})")
        # Save artist-level progress
        if total_artists > 0:
            save_navidrome_scan_progress(artist_name, processed_artists, total_artists)

        total_albums = len(albums)
        for alb_idx, alb in enumerate(albums, 1):
            album_name = alb.get("name") or ""
            
            # Skip albums that don't match filter_missing (if enabled)
            if filter_missing and album_name not in albums_needing_reimport:
                logging.debug(f"Skipping album '{album_name}' - no missing fields (filter_missing=True)")
                continue
            
            # Skip albums that don't match the filter (if provided)
            if album_filter and album_name.strip() != album_filter.strip():
                logging.debug(f"Skipping album '{album_name}' - does not match filter '{album_filter}'")
                continue
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
                album_data = fetch_album_tracks(album_id)
                tracks = album_data.get("tracks", [])
                api_album_artist = album_data.get("artist", "")
            except Exception as e:
                logging.debug(f"Failed to fetch tracks for album '{album_name}': {e}")
                tracks = []
                api_album_artist = ""

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
            
            # Get the album artist with priority order:
            # 1. api_album_artist - from getAlbum.view response (most reliable)
            # 2. alb.get("artist") - from getArtist.view response 
            # 3. artist_name - the function parameter (artist we're importing)
            # Note: track.albumArtist field can be incorrect (e.g., containing track artist with feat.)
            album_artist_value = api_album_artist or alb.get("artist") or artist_name

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
                    "album_artist": album_artist_value,
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


def pre_import_sync_album_artists(artist_id: str = None) -> dict:
    """
    Pre-import sync: Batch fetch unique album artists from Navidrome and ensure they exist in database.
    
    This is called before the main Navidrome import to quickly identify and insert any new
    album artists in a single pass, avoiding the need to check one-by-one during track import.
    
    Args:
        artist_id: Single artist ID to sync (optional). If None, syncs all artists.
        
    Returns:
        Dict with results: {
            'unique_album_artists': int,
            'new_artists_created': int,
            'existing_artists': int,
            'sync_time_ms': float,
            'new_artists': [list of artist names that were created],
            'success': bool
        }
    """
    import time
    from popularity_helpers import _get_nav_client
    
    start_time = time.time()
    
    try:
        nav_client = _get_nav_client()
        if not nav_client:
            return {'error': 'Navidrome client not available', 'success': False}
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get current artists in database
        cursor.execute("SELECT DISTINCT LOWER(name) FROM artists WHERE name IS NOT NULL AND name != ''")
        existing_artists = set(row[0] for row in cursor.fetchall())
        logging.debug(f"Found {len(existing_artists)} existing artists in database")
        
        # Fetch all artists from Navidrome
        if artist_id:
            # Single artist sync
            artist_info = nav_client.get_artists([artist_id])
            artists_to_sync = artist_info if isinstance(artist_info, list) else [artist_info]
        else:
            # All artists
            artists_to_sync = nav_client.get_artists()
        
        logging.info(f"Pre-import sync: Scanning {len(artists_to_sync)} artist(s) from Navidrome")
        
        # Extract unique album artists from all albums of all artists
        unique_album_artists = {}  # name -> count
        
        for artist_data in artists_to_sync:
            artist_name = artist_data.get('name', '')
            if not artist_name:
                continue
            
            artist_id_val = artist_data.get('id', '')
            
            # Fetch albums for this artist
            try:
                albums = nav_client.get_albums(artist_id=artist_id_val)
                for album in albums:
                    album_artist = album.get('artist', '').strip()
                    if album_artist:
                        key = album_artist.lower()
                        if key not in unique_album_artists:
                            unique_album_artists[key] = {'original': album_artist, 'count': 0}
                        unique_album_artists[key]['count'] += 1
            except Exception as e:
                logging.debug(f"Error fetching albums for artist {artist_name}: {e}")
                continue
        
        logging.info(f"Pre-import sync: Found {len(unique_album_artists)} unique album artists across all albums")
        
        # Identify new artists
        new_artists_to_add = []
        for artist_key, artist_info in unique_album_artists.items():
            if artist_key not in existing_artists:
                new_artists_to_add.append(artist_info['original'])
        
        logging.info(f"Pre-import sync: {len(new_artists_to_add)} new album artists need to be added to database")
        
        # Batch insert new artists in a single transaction
        if new_artists_to_add:
            try:
                for artist_name in new_artists_to_add:
                    cursor.execute("""
                        INSERT OR IGNORE INTO artists (id, name)
                        VALUES (?, ?)
                    """, (artist_name.lower().replace(' ', '_'), artist_name))
                    logging.debug(f"Created artist record: {artist_name}")
                
                conn.commit()
                logging.info(f"Pre-import sync: Created {len(new_artists_to_add)} new artist record(s)")
            except Exception as e:
                logging.debug(f"Error batch inserting artists: {e}")
                conn.rollback()
                conn.close()
                return {
                    'error': f'Failed to batch insert artists: {e}',
                    'unique_album_artists': len(unique_album_artists),
                    'new_artists_created': 0,
                    'success': False
                }
        
        sync_time_ms = (time.time() - start_time) * 1000
        
        result = {
            'unique_album_artists': len(unique_album_artists),
            'new_artists_created': len(new_artists_to_add),
            'existing_artists': len(unique_album_artists) - len(new_artists_to_add),
            'sync_time_ms': round(sync_time_ms, 2),
            'new_artists': new_artists_to_add,
            'success': True
        }
        
        logging.info(f"Pre-import sync complete: {result['unique_album_artists']} unique album artists, {result['new_artists_created']} new, {result['existing_artists']} existing")
        
        conn.close()
        return result
        
    except Exception as e:
        logging.debug(f"Pre-import artist sync failed: {e}", exc_info=True)
        return {
            'error': str(e),
            'success': False
        }


def fetch_artist_metadata(artist_name: str, verbose: bool = False):
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
    from api_clients.musicbrainz import _USER_AGENT as MUSICBRAINZ_USER_AGENT
    from helpers import create_retry_session
    from config_loader import load_config
    
    try:
        config = load_config()
        logging.debug(f"Fetching artist metadata for: {artist_name}")
        
        # Check if force flag is enabled
        force = config.get("features", {}).get("force", False)
        logging.debug(f"Force flag: {force}")
        
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
        logging.debug(f"DB: Ensured artist_metadata table exists")
        
        # Check for existing metadata
        cursor.execute("""
            SELECT biography, image_url 
            FROM artist_metadata 
            WHERE artist_name = ?
        """, (artist_name,))
        logging.debug(f"DB Query: SELECT biography, image_url FROM artist_metadata WHERE artist_name = '{artist_name}'")
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
                logging.info(f"Artist metadata already exists for {artist_name}, skipping fetch")
                logging.debug(f"Metadata exists - Bio length: {len(existing_bio)}, Image URL: {bool(existing_image)}")
                conn.close()
                return
        
        conn.close()
        
        # Get API configurations
        discogs_config = config.get("api_integrations", {}).get("discogs", {})
        discogs_enabled = discogs_config.get("enabled", False)
        discogs_token = discogs_config.get("token", "")
        logging.debug(f"Discogs config - Enabled: {discogs_enabled}, Token present: {bool(discogs_token)}")
        
        audiodb_config = config.get("api_integrations", {}).get("audiodb", {})
        audiodb_enabled = audiodb_config.get("enabled", False)
        audiodb_api_key = audiodb_config.get("api_key", "195003")
        logging.debug(f"AudioDB config - Enabled: {audiodb_enabled}, API key present: {bool(audiodb_api_key)}")
        
        # Try to fetch biography from Discogs (only if needed)
        biography = ""
        if fetch_bio and discogs_enabled and discogs_token:
            logging.info(f"Fetching biography for {artist_name} from Discogs...")
            logging.debug(f"API Call: get_discogs_artist_biography(artist_name={artist_name})")
            bio_data = get_discogs_artist_biography(artist_name, token=discogs_token, enabled=True)
            logging.debug(f"API Response: {bio_data}")
            biography = bio_data.get("profile", "")
            if biography:
                logging.info(f"Retrieved artist biography from Discogs ({len(biography)} characters)")
                logging.debug(f"Biography preview: {biography[:100]}...")
        
        # Try to fetch artist image with fallback chain (only if needed)
        artist_image_url = ""
        if fetch_image:
            # Priority 1: Try The AudioDB
            if audiodb_enabled and audiodb_api_key:
                logging.info(f"Fetching artist image for {artist_name} from The AudioDB...")
                logging.debug(f"API Call: get_artist_fanart(artist_name={artist_name})")
                artist_image_url = get_artist_fanart(artist_name, api_key=audiodb_api_key, enabled=True)
                if artist_image_url:
                    logging.info(f"Retrieved artist image from The AudioDB")
                    logging.debug(f"Image URL: {artist_image_url}")
            
            # Priority 2: Fall back to Apple Music if AudioDB didn't return anything
            if not artist_image_url:
                logging.info(f"Fetching artist image for {artist_name} from Apple Music (AudioDB fallback)...")
                logging.debug(f"API Call: get_artist_artwork(artist_name={artist_name}, size=500)")
                artist_image_url = get_artist_artwork(artist_name, size=500, enabled=True)
                if artist_image_url:
                    logging.info(f"Retrieved artist image from Apple Music")
                    logging.debug(f"Image URL: {artist_image_url}")
            
            # Priority 3: Fall back to MusicBrainz if still nothing found
            if not artist_image_url:
                try:
                    logging.debug(f"Attempting to fetch artist image from MusicBrainz CAA...")
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
                            logging.info(f"Retrieved artist image from MusicBrainz CAA")
                            logging.debug(f"Image URL: {artist_image_url}")
                except Exception as e:
                    logging.debug(f"MusicBrainz fallback failed: {e}")
        
        # Store in database
        if biography or artist_image_url:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            # Insert or update artist metadata
            cursor.execute("""
                INSERT OR REPLACE INTO artist_metadata (artist_name, biography, image_url, updated_at)
                VALUES (?, ?, ?, ?)
            """, (artist_name, biography, artist_image_url, datetime.now().isoformat()))
            logging.debug(f"DB: INSERT OR REPLACE artist_metadata for {artist_name}")
            
            conn.commit()
            conn.close()
            
            logging.info(f"Stored artist metadata for {artist_name}")
            logging.debug(f"Metadata saved - Bio: {bool(biography)}, Image: {bool(artist_image_url)}")
    
    except Exception as e:
        logging.info(f"Error fetching artist metadata for {artist_name}: {e}")
        logging.debug(f"fetch_artist_metadata error for {artist_name}: {e}", exc_info=True)
