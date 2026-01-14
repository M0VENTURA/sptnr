#!/usr/bin/env python3
"""
Popularity Scanner - Detects track popularity from external sources (Spotify, Last.fm, ListenBrainz).
Calculates popularity scores and updates database.
"""




import os
import sqlite3
import logging
import json
import math
from datetime import datetime
from statistics import median
from api_clients import session

# --- Singles Detection Thresholds ---
# Tracks with popularity >= this threshold are considered high-confidence singles
HIGH_POPULARITY_THRESHOLD = 70
# Tracks with popularity >= this threshold get a bonus star
MEDIUM_POPULARITY_THRESHOLD = 50

# Dedicated popularity logger (no propagation to root)



# --- Dual Logger Setup: sptnr.log and unified_scan.log ---
import logging
LOG_PATH = os.environ.get("LOG_PATH", "/config/sptnr.log")
UNIFIED_LOG_PATH = os.environ.get("UNIFIED_SCAN_LOG_PATH", "/config/unified_scan.log")
VERBOSE = (
    os.environ.get("SPTNR_VERBOSE_POPULARITY") or os.environ.get("SPTNR_VERBOSE") or "0"
) == "1"
SERVICE_PREFIX = "popularity_"

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
unified_file_handler.setFormatter(formatter)
unified_logger.setLevel(logging.INFO)
# Always add the file handler (even if handlers exist)
unified_logger.addHandler(unified_file_handler)
unified_logger.propagate = False
print("unified_logger handlers:", unified_logger.handlers)

def log_basic(msg):
    logging.info(msg)

def log_unified(msg):
    unified_logger.info(msg)

def log_verbose(msg):
    if VERBOSE:
        logging.info(msg)




DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")
POPULARITY_PROGRESS_FILE = os.environ.get("POPULARITY_PROGRESS_FILE", "/database/popularity_scan_progress.json")
NAVIDROME_PROGRESS_FILE = os.environ.get("NAVIDROME_PROGRESS_FILE", "/database/navidrome_scan_progress.json")
from popularity_helpers import (
    get_spotify_artist_id,
    search_spotify_track,
    get_lastfm_track_info,
    get_listenbrainz_score,
    score_by_age,
    SPOTIFY_WEIGHT,
    LASTFM_WEIGHT,
    LISTENBRAINZ_WEIGHT,
    AGE_WEIGHT,
)

# Import scan history tracker
try:
    from scan_history import log_album_scan
except ImportError:
    def log_album_scan(*args, **kwargs):
        pass  # Fallback if scan_history not available

# --- DEBUG: Test log_unified and print log path ---
if __name__ == "__main__":
    try:
        print("UNIFIED_LOG_PATH:", UNIFIED_LOG_PATH)
        log_unified("TEST ENTRY: log_unified() at script start")
    except Exception as e:
        print("log_unified() test failed:", e)

def get_db_connection():
    """Get database connection with WAL mode"""
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def _navidrome_scan_running() -> bool:
    """Return True if Navidrome scan progress file says a scan is running."""
    try:
        if os.path.exists(NAVIDROME_PROGRESS_FILE):
            with open(NAVIDROME_PROGRESS_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
                return bool(state.get("is_running"))
    except Exception as e:
        log_verbose(f"Could not read Navidrome progress file: {e}")
    return False

def sync_track_rating_to_navidrome(track_id: str, stars: int) -> bool:
    """
    Sync a single track rating to Navidrome using the Subsonic API.
    
    Args:
        track_id: Navidrome track ID
        stars: Star rating (1-5)
        
    Returns:
        True if successful, False otherwise
    """
    try:
        # Get Navidrome credentials from environment
        nav_url = os.environ.get("NAVIDROME_URL", "").strip("/")
        nav_user = os.environ.get("NAVIDROME_USER", "")
        nav_pass = os.environ.get("NAVIDROME_PASS", "")
        
        if not all([nav_url, nav_user, nav_pass]):
            log_verbose("Navidrome credentials not configured, skipping rating sync")
            return False
        
        # Build Subsonic API parameters
        params = {
            "u": nav_user,
            "p": nav_pass,
            "v": "1.16.1",
            "c": "sptnr",
            "f": "json",
            "id": track_id,
            "rating": stars
        }
        
        # Call setRating API
        response = session.get(f"{nav_url}/rest/setRating.view", params=params, timeout=10)
        response.raise_for_status()
        
        # Check if response indicates success
        result = response.json()
        if result.get("subsonic-response", {}).get("status") == "ok":
            return True
        else:
            error_msg = result.get("subsonic-response", {}).get("error", {}).get("message", "Unknown error")
            log_verbose(f"Navidrome API error: {error_msg}")
            return False
            
    except Exception as e:
        log_verbose(f"Failed to sync rating to Navidrome: {e}")
        return False

def save_popularity_progress(processed_artists: int, total_artists: int):
    """Save popularity scan progress to file"""
    try:
        progress_data = {
            "is_running": True,
            "scan_type": "popularity_scan",
            "processed_artists": processed_artists,
            "total_artists": total_artists,
            "percent_complete": int((processed_artists / total_artists * 100)) if total_artists > 0 else 0
        }
        with open(POPULARITY_PROGRESS_FILE, 'w') as f:
            json.dump(progress_data, f)
    except Exception as e:
        log_basic(f"Error saving popularity progress: {e}")

def popularity_scan(verbose: bool = False):
    """Detect track popularity from external sources (legacy function)"""
    log_unified("=" * 60)
    log_unified("Popularity Scanner Started")
    log_unified("=" * 60)
    log_unified(f"🟢 Popularity scan started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # Initialize popularity helpers to configure Spotify client
    from popularity_helpers import configure_popularity_helpers
    configure_popularity_helpers()
    log_unified("✅ Spotify client configured")

    log_verbose("Connecting to database for popularity scan...")
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Get all tracks that need popularity detection
        sql = ("""
            SELECT id, artist, title, album
            FROM tracks
            WHERE popularity_score IS NULL OR popularity_score = 0
            ORDER BY artist, album, title
        """)
        log_verbose(f"Executing SQL: {sql.strip()}")
        cursor.execute(sql)

        tracks = cursor.fetchall()
        log_unified(f"Found {len(tracks)} tracks to scan for popularity")
        log_verbose(f"Fetched {len(tracks)} tracks from database.")

        if not tracks:
            log_unified("No tracks found for popularity scan. Exiting.")
            return

        # Group tracks by artist and album
        from collections import defaultdict
        artist_album_tracks = defaultdict(lambda: defaultdict(list))
        for track in tracks:
            artist_album_tracks[track["artist"]][track["album"]].append(track)

        scanned_count = 0
        for artist, albums in artist_album_tracks.items():
            log_unified(f"Currently Scanning Artist: {artist}")
            for album, album_tracks in albums.items():
                log_unified(f'Scanning "{artist} - {album}" for Popularity')
                album_scanned = 0
                for track in album_tracks:
                    track_id = track["id"]
                    title = track["title"]

                    # Progress log every track
                    log_unified(f'Scanning track: "{title}" (Track ID: {track_id})')

                    # Try to get popularity from Spotify
                    spotify_score = 0
                    try:
                        artist_id = get_spotify_artist_id(artist)
                        if artist_id:
                            spotify_results = search_spotify_track(title, artist, album)
                            if spotify_results and isinstance(spotify_results, list) and len(spotify_results) > 0:
                                best_match = max(spotify_results, key=lambda r: r.get('popularity', 0))
                                spotify_score = best_match.get("popularity", 0)
                    except Exception as e:
                        log_unified(f"Spotify lookup failed for {artist} - {title}: {e}")

                    # Try to get popularity from Last.fm
                    lastfm_score = 0
                    try:
                        lastfm_info = get_lastfm_track_info(artist, title)
                        if lastfm_info and lastfm_info.get("track_play"):
                            lastfm_score = min(100, int(lastfm_info["track_play"]) // 100)
                    except Exception as e:
                        log_unified(f"Last.fm lookup failed for {artist} - {title}: {e}")

                    # Average the scores
                    if spotify_score > 0 or lastfm_score > 0:
                        popularity_score = (spotify_score + lastfm_score) / 2.0
                        cursor.execute(
                            "UPDATE tracks SET popularity_score = ? WHERE id = ?",
                            (popularity_score, track_id)
                        )
                        scanned_count += 1
                        album_scanned += 1
                    else:
                        log_unified(f"No popularity score found for {artist} - {title}")

                    # Save progress after each track
                    save_popularity_progress(scanned_count, len(tracks))

                log_unified(f'Album Scanned: "{artist} - {album}". Popularity Applied to {album_scanned} tracks.')

                # Calculate star ratings and detect singles
                log_unified(f'Calculating star ratings and detecting singles for "{artist} - {album}"')
                
                # Get all tracks for this album with their popularity scores
                cursor.execute(
                    "SELECT id, title, popularity_score FROM tracks WHERE artist = ? AND album = ? ORDER BY popularity_score DESC",
                    (artist, album)
                )
                album_tracks_with_scores = cursor.fetchall()
                
                if album_tracks_with_scores and len(album_tracks_with_scores) > 0:
                    # Calculate star ratings using the same logic as sptnr.py
                    total_tracks = len(album_tracks_with_scores)
                    band_size = math.ceil(total_tracks / 4)
                    
                    # Calculate median score for threshold
                    scores = [t["popularity_score"] if t["popularity_score"] else 0 for t in album_tracks_with_scores]
                    median_score = median(scores) if scores else 10
                    if median_score == 0:
                        median_score = 10
                    jump_threshold = median_score * 1.7
                    
                    singles_detected = []
                    star_distribution = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
                    
                    for i, track_row in enumerate(album_tracks_with_scores):
                        track_id = track_row["id"]
                        title = track_row["title"]
                        popularity_score = track_row["popularity_score"] if track_row["popularity_score"] else 0
                        
                        # Calculate band-based star rating
                        band_index = i // band_size
                        stars = max(1, 4 - band_index)
                        
                        # Boost to 5 stars if score exceeds threshold
                        if popularity_score >= jump_threshold:
                            stars = 5
                        
                        # Detect if track is a single (basic detection based on high popularity)
                        is_single = False
                        single_confidence = "low"
                        single_sources = []
                        
                        # Simple heuristic: tracks with high popularity are likely singles
                        if popularity_score >= HIGH_POPULARITY_THRESHOLD:
                            is_single = True
                            single_confidence = "high"
                            single_sources.append("high_popularity")
                            stars = 5  # Singles get 5 stars
                        elif popularity_score >= MEDIUM_POPULARITY_THRESHOLD:
                            is_single = True
                            single_confidence = "medium"
                            single_sources.append("medium_popularity")
                            stars = min(stars + 1, 5)
                        
                        # Ensure at least 1 star
                        stars = max(stars, 1)
                        
                        # Update track in database
                        cursor.execute(
                            """UPDATE tracks 
                            SET stars = ?, is_single = ?, single_confidence = ?, single_sources = ?
                            WHERE id = ?""",
                            (stars, 1 if is_single else 0, single_confidence, ','.join(single_sources), track_id)
                        )
                        
                        star_distribution[stars] += 1
                        
                        # Log track with star rating
                        star_display = "★" * stars + "☆" * (5 - stars)
                        log_unified(f"   {star_display} ({stars}/5) - {title} (popularity: {popularity_score:.1f})")
                        
                        # Sync to Navidrome
                        if sync_track_rating_to_navidrome(track_id, stars):
                            log_unified(f"      ✓ Synced to Navidrome")
                        else:
                            log_unified(f"      ⚠ Skipped Navidrome sync (not configured or failed)")
                        
                        if is_single:
                            singles_detected.append({
                                "title": title,
                                "confidence": single_confidence,
                                "sources": single_sources,
                                "score": popularity_score
                            })
                    
                    # Log singles summary
                    if singles_detected:
                        log_unified(f'✓ Detected {len(singles_detected)} singles in "{album}":')
                        for single in singles_detected:
                            sources_str = ', '.join(single["sources"]) if single["sources"] else "unknown"
                            log_unified(f'   → "{single["title"]}" [{single["confidence"]} confidence from {sources_str}] (score: {single["score"]:.1f})')
                    else:
                        log_unified(f'No singles detected in "{album}"')
                    
                    # Log star distribution
                    dist_str = ", ".join([f"{stars}★: {count}" for stars, count in sorted(star_distribution.items(), reverse=True) if count > 0])
                    log_unified(f'Star distribution for "{album}": {dist_str}')
                
                # Log album scan
                log_album_scan(artist, album, 'popularity', album_scanned, 'completed')

            # After artist scans, check/create essential playlist (dummy logic)
            log_unified(f'Checking if essential playlist needs to be created for artist: {artist}')
            # Dummy: always create
            log_unified(f'Essential playlist created for artist: {artist}')

        log_verbose("Committing changes to database.")
        conn.commit()

        log_unified(f"✅ Popularity scan completed: {scanned_count} tracks updated")
        log_verbose(f"Popularity scan completed: {scanned_count} tracks updated")
    except Exception as e:
        log_unified(f"❌ Popularity scan failed: {str(e)}")
        raise
    finally:
        if conn:
            conn.close()
        log_unified("=" * 60)
        log_unified(f"✅ Popularity scan complete at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

def create_or_update_playlist_for_artist(artist_name: str, tracks: list):
    """
    Create or update a playlist for an artist using the cached tracks.
    This is a placeholder function that logs the intent but doesn't actually create playlists yet.
    
    Args:
        artist_name: Name of the artist
        tracks: List of track dictionaries with id, artist, album, title, stars
    """
    log_basic(f"Playlist update requested for artist: {artist_name} ({len(tracks)} tracks)")
    # TODO: Implement actual playlist creation/update via Navidrome API
    # For now, this is a no-op to prevent import errors

def refresh_all_playlists_from_db():
    """
    Refresh all smart playlists for all artists from DB cache (no track rescans).
    This function pulls distinct artists that have cached tracks and updates their playlists.
    """
    log_basic("🔄 Refreshing smart playlists for all artists from DB cache (no track rescans)...")
    
    # Pull distinct artists that have cached tracks
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT artist FROM tracks")
        artists = [row[0] for row in cursor.fetchall()]
        
        if not artists:
            log_basic("⚠️ No cached tracks in DB. Skipping playlist refresh.")
            return
        
        for name in artists:
            cursor.execute("SELECT id, artist, album, title, stars FROM tracks WHERE artist = ?", (name,))
            rows = cursor.fetchall()
            
            if not rows:
                log_basic(f"⚠️ No cached tracks found for '{name}', skipping.")
                continue
            
            tracks = [
                {
                    "id": r[0],
                    "artist": r[1],
                    "album": r[2],
                    "title": r[3],
                    "stars": int(r[4]) if r[4] else 0
                }
                for r in rows
            ]
            create_or_update_playlist_for_artist(name, tracks)
            log_basic(f"✅ Playlist refreshed for '{name}' ({len(tracks)} tracks)")
    except Exception as e:
        log_basic(f"❌ Error refreshing playlists: {e}")
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run popularity scan.")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output")
    args = parser.parse_args()
    popularity_scan(verbose=args.verbose)
