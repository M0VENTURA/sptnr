#!/usr/bin/env python3
"""
Navidrome Scanner - Scans Navidrome library and populates the database with tracks.
Extracts artist/album/track information from Navidrome API.
"""

import os
import sqlite3
import logging
from datetime import datetime
import sys
import json

# Setup logging
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/config/navidromescan.log"),
        logging.StreamHandler()
    ]
)

MUSIC_ROOT = os.environ.get("MUSIC_ROOT", "/music")
DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")

# Import from start.py (need to add it to path)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from start import (
    nav_client,
    config,
    get_db_connection,
    save_to_db,
    fetch_artist_albums,  # Import the wrapper - used by update_artist_stats
    fetch_album_tracks,   # Import the wrapper - used by update_artist_stats
    build_artist_index,   # Import for convenience, but navidromescan defines its own
)

def get_db_connection_local():
    """Get database connection with WAL mode"""
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


# ============ NAVIDROME API WRAPPERS ============

def fetch_artist_albums(artist_id):
    """Fetch albums for an artist (wrapper using NavidromeClient)."""
    return nav_client.fetch_artist_albums(artist_id)


def fetch_album_tracks(album_id):
    """
    Fetch all tracks for an album using Subsonic API (wrapper using NavidromeClient).
    :param album_id: Album ID in Navidrome
    :return: List of track objects
    """
    return nav_client.fetch_album_tracks(album_id)


# ============ NAVIDROME SCANNING FUNCTIONS ============

def build_artist_index(verbose: bool = False):
    """Build artist index from Navidrome (wrapper using NavidromeClient)."""
    artist_map_from_api = nav_client.build_artist_index()
    
    # Persist to database
    conn = get_db_connection()
    cursor = conn.cursor()
    for artist_name, info in artist_map_from_api.items():
        artist_id = info.get("id")
        cursor.execute("""
            INSERT OR REPLACE INTO artist_stats (artist_id, artist_name, album_count, track_count, last_updated)
            VALUES (?, ?, ?, ?, ?)
        """, (artist_id, artist_name, 0, 0, None))
        if verbose:
            print(f"   📝 Added artist to index: {artist_name} (ID: {artist_id})")
            logging.info(f"Added artist to index: {artist_name} (ID: {artist_id})")
    conn.commit()
    conn.close()
    
    logging.info(f"✅ Cached {len(artist_map_from_api)} artists in DB")
    print(f"✅ Cached {len(artist_map_from_api)} artists in DB")
    return artist_map_from_api


def scan_library_to_db(verbose: bool = False, force: bool = False):
    """
    Scan the entire Navidrome library (artists -> albums -> tracks) and persist
    a lightweight representation of each track into the local DB.

    Behavior:
      - Uses NavidromeClient API helpers: build_artist_index(), fetch_artist_albums(), fetch_album_tracks()
      - For each track, writes a minimal `track_data` record via `save_to_db()`
      - Uses INSERT OR REPLACE semantics (so re-running is safe and refreshes `last_scanned`)
    """
    print("🔎 Scanning Navidrome library into local DB...")
    artist_map_local = build_artist_index(verbose=verbose) or {}
    if not artist_map_local:
        print("⚠️ No artists available from Navidrome; aborting library scan.")
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
    
    for name, info in artist_map_local.items():
        artist_count += 1
        artist_id = info.get("id")
        if not artist_id:
            print(f"⚠️ [{artist_count}/{total_artists}] Skipping '{name}' (no artist ID)")
            continue
        
        print(f"🎨 [{artist_count}/{total_artists}] Processing artist: {name}")
        logging.info(f"Processing artist {artist_count}/{total_artists}: {name} (ID: {artist_id})")

        # Prefetch cached tracks for this artist to enable per-artist skip decisions
        existing_album_tracks: dict[str, set[str]] = {}
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT album, id FROM tracks WHERE artist = ?", (name,))
            for alb_name, tid in cursor.fetchall():
                if alb_name not in existing_album_tracks:
                    existing_album_tracks[alb_name] = set()
                existing_album_tracks[alb_name].add(tid)
            conn.close()
        except Exception as e:
            logging.debug(f"Prefetch existing tracks for artist '{name}' failed: {e}")
        
        try:
            albums = fetch_artist_albums(artist_id)
            if albums:
                print(f"   📀 Found {len(albums)} albums")
                logging.info(f"Found {len(albums)} albums for artist '{name}'")
        except Exception as e:
            print(f"   ❌ Failed to fetch albums: {e}")
            logging.error(f"Failed to fetch albums for '{name}': {e}")
            albums = []
        
        album_count = 0
        for alb in albums:
            album_count += 1
            album_name = alb.get("name") or ""
            album_id = alb.get("id")
            if not album_id:
                continue
            
            print(f"   📀 [{album_count}/{len(albums)}] Album: {album_name[:50]}...")
            logging.info(f"Scanning album {album_count}/{len(albums)}: {album_name}")
            
            try:
                tracks = fetch_album_tracks(album_id)
                if tracks:
                    print(f"      🎵 Found {len(tracks)} tracks")
                    logging.info(f"Found {len(tracks)} tracks in album '{album_name}'")
            except Exception as e:
                print(f"      ❌ Failed to fetch tracks: {e}")
                logging.error(f"Failed to fetch tracks for album '{album_name}': {e}")
                tracks = []

            # Album-level skip if counts already match cached tracks (unless force=True)
            cached_ids_for_album = existing_album_tracks.get(album_name, set())
            if not force and tracks and len(cached_ids_for_album) >= len(tracks):
                total_albums_skipped += 1
                print(f"      ⏩ Skipping album (already cached): {album_name}")
                logging.info(f"Skipping album '{album_name}' — cached {len(cached_ids_for_album)} tracks matches API {len(tracks)}")
                continue
            
            tracks_written = 0
            tracks_skipped = 0
            tracks_updated = 0
            for t in tracks:
                track_id = t.get("id")
                if not track_id:
                    continue
                
                # Check if track exists and needs metadata update
                needs_update = False
                if not force and (track_id in existing_track_ids or track_id in cached_ids_for_album):
                    # Check if existing track is missing new metadata fields
                    try:
                        conn_check = get_db_connection()
                        cursor_check = conn_check.cursor()
                        cursor_check.execute("""
                            SELECT duration, track_number, year, bitrate 
                            FROM tracks 
                            WHERE id = ?
                        """, (track_id,))
                        row = cursor_check.fetchone()
                        conn_check.close()
                        
                        # If any of these critical fields are NULL, we MUST update (especially duration)
                        if row and (row[0] is None or row[1] is None or row[2] is None or row[3] is None):
                            needs_update = True
                            logging.info(f"Track {track_id} needs metadata update (missing: duration={row[0] is None}, track_number={row[1] is None}, year={row[2] is None}, bitrate={row[3] is None})")
                        else:
                            tracks_skipped += 1
                            continue
                    except Exception as e:
                        logging.debug(f"Error checking track metadata: {e}")
                        tracks_skipped += 1
                        continue
                
                td = {
                    "id": track_id,
                    "title": t.get("title", ""),
                    "album": album_name,
                    "artist": name,
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
                    "spotify_release_date": "",
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
                    # Enhanced metadata from Navidrome for better matching
                    "duration": t.get("duration"),  # Track duration in seconds
                    "track_number": t.get("track"),  # Track number
                    "disc_number": t.get("discNumber"),  # Disc number
                    "year": t.get("year"),  # Release year
                    "album_artist": t.get("albumArtist", ""),  # Album artist
                    "bitrate": t.get("bitRate"),  # Bitrate in kbps
                    "sample_rate": t.get("samplingRate"),  # Sample rate in Hz
                }
                try:
                    save_to_db(td)
                    total_written += 1
                    if needs_update:
                        tracks_updated += 1
                    else:
                        tracks_written += 1
                    existing_track_ids.add(track_id)
                    cached_ids_for_album.add(track_id)
                except Exception as e:
                    logging.debug(f"Failed to save track {track_id} -> {e}")
            
            if tracks_written > 0:
                print(f"      ✅ Saved {tracks_written} new tracks to DB")
                logging.info(f"Saved {tracks_written} new tracks from album '{album_name}'")
            if tracks_updated > 0:
                print(f"      🔄 Updated {tracks_updated} tracks with new metadata")
                logging.info(f"Updated {tracks_updated} tracks with metadata from album '{album_name}'")
            if tracks_skipped > 0:
                total_skipped += tracks_skipped
                print(f"      ⏩ Skipped {tracks_skipped} cached tracks")
                logging.info(f"Skipped {tracks_skipped} cached tracks for album '{album_name}'")
        
        if album_count > 0:
            print(f"   ✅ Completed {album_count} albums for '{name}'")
            
    print(f"✅ Library scan complete. Tracks written/updated: {total_written}; skipped cached: {total_skipped}")
    logging.info(f"Library scan complete. Written/updated: {total_written}; skipped cached: {total_skipped}; albums skipped: {total_albums_skipped}")


def update_artist_stats(artist_id, artist_name):
    """Update album and track counts for an artist."""
    album_count = len(fetch_artist_albums(artist_id))
    track_count = sum(len(fetch_album_tracks(a['id'])) for a in fetch_artist_albums(artist_id))
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO artist_stats (artist_id, artist_name, album_count, track_count, last_updated)
        VALUES (?, ?, ?, ?, ?)
    """, (artist_id, artist_name, album_count, track_count, datetime.now().strftime("%Y-%m-%dT%H:%M:%S")))
    conn.commit()
    conn.close()


def load_artist_map():
    """Load artist map from database cache."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT artist_id, artist_name, album_count, track_count, last_updated FROM artist_stats")
    rows = cursor.fetchall()
    conn.close()
    return {row[1]: {"id": row[0], "album_count": row[2], "track_count": row[3], "last_updated": row[4]} for row in rows}


def get_album_last_scanned_from_db(artist_name: str, album_name: str) -> str | None:
    """
    Return the most recent 'last_scanned' timestamp among tracks already saved
    for (artist, album). Timestamp is in '%Y-%m-%dT%H:%M:%S' or None if missing.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT MAX(last_scanned) FROM tracks WHERE artist = ? AND album = ?",
            (artist_name, album_name),
        )
        row = cursor.fetchone()
        conn.close()
        return (row[0] if row and row[0] else None)
    except Exception as e:
        logging.debug(f"get_album_last_scanned_from_db failed for '{artist_name} / {album_name}': {e}")
        return None


def get_album_track_count_in_db(artist_name: str, album_name: str) -> int:
    """
    Return how many tracks for (artist, album) currently exist in DB.
    Useful to avoid skipping albums that have no cached tracks yet.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM tracks WHERE artist = ? AND album = ?",
            (artist_name, album_name),
        )
        count = cursor.fetchone()[0] or 0
        conn.close()
        return count
    except Exception as e:
        logging.debug(f"get_album_track_count_in_db failed for '{artist_name} / {album_name}': {e}")
        return 0


def navidrome_scan(verbose: bool = False, force: bool = False):
    """Scan Navidrome library and populate database"""
    logging.info("=" * 60)
    logging.info("Navidrome Scanner Started")
    logging.info("=" * 60)
    
    try:
        logging.info("Building artist index from Navidrome...")
        build_artist_index(verbose=verbose)
        
        logging.info("Scanning library to populate track database...")
        scan_library_to_db(verbose=verbose, force=force)
        
        logging.info("✅ Navidrome scan completed successfully")
        
    except Exception as e:
        logging.error(f"❌ Navidrome scan failed: {str(e)}")
        raise
    
    finally:
        logging.info("=" * 60)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Scan Navidrome library and populate database")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output")
    parser.add_argument("--force", action="store_true", help="Force re-scan")
    
    args = parser.parse_args()
    navidrome_scan(verbose=args.verbose, force=args.force)
