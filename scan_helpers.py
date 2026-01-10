#!/usr/bin/env python3
"""Helper functions for scanning and rating operations."""

import logging
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from start import get_db_connection, fetch_artist_albums, fetch_album_tracks, save_to_db
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
    """Scan a single artist from Navidrome and persist tracks to DB.
    
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
                    "genres": [],
                    "navidrome_genres": [t.get("genre")] if t.get("genre") else [],
                    "spotify_genres": [],
                    "lastfm_tags": [],
                    "discogs_genres": [],
                    "audiodb_genres": [],
                    "musicbrainz_genres": [],
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
                    "single_sources": [],
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


def count_mp3_files(music_folder):
    """Count total MP3 files in music folder recursively"""
    print(f"{LIGHT_BLUE}📂 Scanning {music_folder} for MP3 files...{RESET}")
    total = 0
    
    if not os.path.exists(music_folder):
        print(f"{LIGHT_YELLOW}⚠️ Music folder not found: {music_folder}{RESET}")
        return 0
    
    try:
        for root, dirs, files in os.walk(music_folder):
            for file in files:
                if file.lower().endswith('.mp3'):
                    total += 1
        
        print(f"{LIGHT_GREEN}✅ Found {total} MP3 files{RESET}")
        return total
    except Exception as e:
        print(f"{LIGHT_RED}❌ Error counting MP3 files: {type(e).__name__} - {e}{RESET}")
        return 0


def scan_mp3_metadata(music_folder, show_progress=True):
    """
    Scan MP3 files in music folder and extract metadata.
    Returns count of files scanned and any errors encountered.
    """
    print(f"\n{LIGHT_CYAN}{'='*60}{RESET}")
    print(f"{LIGHT_CYAN}🎵 Starting MP3 Metadata Scan{RESET}")
    print(f"{LIGHT_CYAN}{'='*60}{RESET}\n")
    
    # First, count total files
    total_files = count_mp3_files(music_folder)
    
    if total_files == 0:
        print(f"{LIGHT_YELLOW}⚠️ No MP3 files found to scan{RESET}")
        return 0, []
    
    scanned = 0
    errors = []
    
    print(f"{LIGHT_BLUE}📊 Beginning scan of {total_files} files...{RESET}\n")
    
    try:
        for root, dirs, files in os.walk(music_folder):
            for file in files:
                if file.lower().endswith('.mp3'):
                    scanned += 1
                    
                    if show_progress and scanned % PROGRESS_UPDATE_INTERVAL == 0:
                        percentage = (scanned / total_files) * 100
                        print(f"\r{LIGHT_BLUE}📈 Progress: {scanned}/{total_files} files ({percentage:.1f}%){'  '}{RESET}", end='', flush=True)
        
        # Final progress update
        if show_progress:
            print(f"\r{LIGHT_GREEN}✅ Progress: {scanned}/{total_files} files (100.0%){'  '}{RESET}")
        
        print(f"\n{LIGHT_GREEN}✅ MP3 metadata scan complete!{RESET}")
        print(f"{LIGHT_GREEN}   Scanned: {scanned} files{RESET}\n")
        
    except Exception as e:
        errors.append(f"Scan error: {type(e).__name__} - {e}")
        print(f"\n{LIGHT_RED}❌ Error during scan: {type(e).__name__} - {e}{RESET}")
    
    return scanned, errors


def scan_navidrome_with_progress(verbose=False):
    """
    Scan all artists from Navidrome library with progress indicators.
    This is a wrapper that calls scan_artist_to_db for each artist.
    Optimized to avoid duplicate API calls - counts and scans in single pass.
    """
    print(f"\n{LIGHT_CYAN}{'='*60}{RESET}")
    print(f"{LIGHT_CYAN}🎵 Starting Navidrome Library Scan{RESET}")
    print(f"{LIGHT_CYAN}{'='*60}{RESET}\n")
    
    print(f"{LIGHT_BLUE}📊 Scanning Navidrome library...{RESET}")
    
    # Get artist index - use build_artist_index from start.py
    from start import build_artist_index as get_artist_map
    artist_index = get_artist_map(verbose=verbose)
    
    if not artist_index:
        print(f"{LIGHT_RED}❌ No artists found in index{RESET}")
        return 0
    
    # Single-pass scan: count and process simultaneously
    print(f"{LIGHT_BLUE}🔍 Processing {len(artist_index)} artists...{RESET}\n")
    
    scanned = 0
    ratings_saved = 0
    artist_count = 0
    total_tracks = 0
    
    for artist_name, artist_info in artist_index.items():
        artist_count += 1
        artist_id = artist_info.get("id") if isinstance(artist_info, dict) else artist_info
        
        try:
            # Scan artist to database
            scan_artist_to_db(artist_name, artist_id, verbose=verbose, force=False)
            
            # Count tracks for this artist
            conn = None
            try:
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM tracks WHERE artist = ?", (artist_name,))
                count = cursor.fetchone()[0]
                total_tracks += count
                
                # Count ratings
                cursor.execute("SELECT COUNT(*) FROM tracks WHERE artist = ? AND stars > 0", (artist_name,))
                rated_count = cursor.fetchone()[0]
                ratings_saved += rated_count
            except Exception as e:
                logging.debug(f"Failed to count tracks for {artist_name}: {e}")
            finally:
                if conn:
                    conn.close()
            
            scanned += 1
            
            # Show progress
            if scanned % PROGRESS_UPDATE_INTERVAL == 0:
                print(f"\r{LIGHT_BLUE}📈 Progress: {total_tracks} tracks scanned | {artist_count}/{len(artist_index)} artists | Ratings: {ratings_saved}{RESET}", end='', flush=True)
                
            time.sleep(API_RATE_LIMIT_DELAY)  # Small delay to avoid overwhelming the API
            
        except Exception as e:
            logging.error(f"Failed to scan artist {artist_name}: {e}")
            continue
    
    # Final progress
    print(f"\r{LIGHT_GREEN}✅ Progress: {total_tracks} tracks scanned | {artist_count}/{len(artist_index)} artists | Ratings: {ratings_saved}{'  '}{RESET}")
    print(f"\n{LIGHT_GREEN}✅ Navidrome scan complete!{RESET}")
    print(f"{LIGHT_GREEN}   Total tracks scanned: {total_tracks}{RESET}")
    print(f"{LIGHT_GREEN}   Tracks with ratings: {ratings_saved}{RESET}\n")
    
    return total_tracks

