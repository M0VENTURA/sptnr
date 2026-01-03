#!/usr/bin/env python3
"""Helper functions for scanning and rating operations."""

import logging
import json
import os
import time
from datetime import datetime
from start import get_db_connection, fetch_artist_albums, fetch_album_tracks, save_to_db
from colorama import Fore, Style

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


def save_navidrome_scan_progress(current_artist, current_album, scanned_albums, total_albums):
    """Save Navidrome scan progress to JSON file"""
    try:
        progress_file = os.environ.get("NAVIDROME_PROGRESS_FILE", "/database/navidrome_scan_progress.json")
        progress = {
            "current_artist": current_artist,
            "current_album": current_album,
            "scanned_albums": scanned_albums,
            "total_albums": total_albums,
            "is_running": True,
            "scan_type": "navidrome_scan"
        }
        with open(progress_file, 'w') as f:
            json.dump(progress, f, indent=2)
    except Exception as e:
        logging.error(f"Failed to save Navidrome scan progress: {e}")


def scan_artist_to_db(artist_name: str, artist_id: str, verbose: bool = False, force: bool = False):
    """Scan a single artist from Navidrome and persist tracks to DB."""
    try:
        # Prefetch cached track IDs for this artist
        existing_track_ids: set[str] = set()
        existing_album_tracks: dict[str, set[str]] = {}
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT album, id FROM tracks WHERE artist = ?", (artist_name,))
            for alb_name, tid in cursor.fetchall():
                existing_track_ids.add(tid)
                existing_album_tracks.setdefault(alb_name, set()).add(tid)
            conn.close()
        except Exception as e:
            logging.debug(f"Prefetch existing tracks for artist '{artist_name}' failed: {e}")

        albums = fetch_artist_albums(artist_id)
        if verbose:
            print(f"Scanning artist: {artist_name} ({len(albums)} albums)")
            logging.info(f"Scanning artist {artist_name} ({len(albums)} albums)")

        total_albums = len(albums)
        for alb_idx, alb in enumerate(albums):
            album_name = alb.get("name") or ""
            album_id = alb.get("id")
            if not album_id:
                continue

            # Save progress
            save_navidrome_scan_progress(artist_name, album_name, alb_idx, total_albums)

            try:
                tracks = fetch_album_tracks(album_id)
            except Exception as e:
                logging.debug(f"Failed to fetch tracks for album '{album_name}': {e}")
                tracks = []

            cached_ids_for_album = existing_album_tracks.get(album_name, set())
            if not force and tracks and len(cached_ids_for_album) >= len(tracks):
                if verbose:
                    print(f"   Skipping cached album: {album_name}")
                continue

            for t in tracks:
                track_id = t.get("id")
                if not track_id:
                    continue

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
                    "file_path": t.get("path", ""),
                    "last_scanned": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                    "spotify_album_type": "",
                    "spotify_total_tracks": 0,
                    "spotify_id": None,
                    "is_spotify_single": False,
                    "is_single": False,
                    "single_confidence": "low",
                    "single_sources": [],
                    "stars": 0,
                    "mbid": t.get("mbid", "") or "",
                    "suggested_mbid": "",
                    "suggested_mbid_confidence": 0.0,
                    "navidrome_rating": int(t.get("userRating", 0) or 0),
                    "duration": t.get("duration"),
                    "track_number": t.get("track"),
                    "disc_number": t.get("discNumber"),
                    "year": t.get("year"),
                    "album_artist": t.get("albumArtist", ""),
                    "bitrate": t.get("bitRate"),
                    "sample_rate": t.get("samplingRate"),
                }
                save_to_db(td)

        if verbose:
            print(f"Artist scan complete: {artist_name}")
            logging.info(f"Artist scan complete: {artist_name}")
    except Exception as e:
        logging.error(f"scan_artist_to_db failed for {artist_name}: {e}")
        raise
    finally:
        # Mark scan as complete for this artist
        try:
            progress_file = os.environ.get("NAVIDROME_PROGRESS_FILE", "/database/navidrome_scan_progress.json")
            with open(progress_file, 'w') as f:
                json.dump({"is_running": False, "scan_type": "navidrome_scan"}, f)
        except Exception as e:
            logging.error(f"Failed to mark Navidrome scan as complete: {e}")


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
            try:
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM tracks WHERE artist = ?", (artist_name,))
                count = cursor.fetchone()[0]
                total_tracks += count
                
                # Count ratings
                cursor.execute("SELECT COUNT(*) FROM tracks WHERE artist = ? AND navidrome_rating > 0", (artist_name,))
                rated_count = cursor.fetchone()[0]
                ratings_saved += rated_count
                
                conn.close()
            except Exception as e:
                logging.debug(f"Failed to count tracks for {artist_name}: {e}")
            
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

