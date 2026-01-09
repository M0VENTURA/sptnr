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

# Import single detection
from singledetection import rate_track_single_detection, config as singles_config

# Dedicated popularity logger (no propagation to root)

import logging
LOG_PATH = os.environ.get("LOG_PATH", "/config/sptnr.log")
VERBOSE = os.environ.get("SPTNR_VERBOSE", "0") == "1"
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

def log_basic(msg):
    logging.info(msg)

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

def scan_popularity(verbose: bool = False, artist: str | None = None):
    """
    Scan and update popularity scores from all available sources.
    Updates spotify_score, lastfm_ratio, listenbrainz_score, composite score, and initial stars.
    Optionally filter by artist.
    """
    if _navidrome_scan_running():
        msg = "Navidrome scan is running; skipping popularity scan until it completes"
        log_basic(f"WARNING: {msg}")
        print(f"⚠️ {msg}")
        return
    log_basic("=" * 60)
    log_basic("Popularity Scanner Started")
    log_basic("=" * 60)

    try:
        # Build filter
        params = []
        artist_filter = ""
        if artist:
            artist_filter = " AND artist = ?"
            params.append(artist)

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT DISTINCT artist, album, title, id,
                   spotify_score, lastfm_ratio, listenbrainz_score,
                   stars,
                   last_scanned, mbid, spotify_release_date
            FROM tracks
            WHERE ((spotify_score = 0 AND lastfm_ratio = 0 AND listenbrainz_score = 0) OR
                  (last_scanned IS NOT NULL AND datetime(last_scanned) < datetime('now', '-7 days')))
                  {artist_filter}
            ORDER BY artist, album, title
            LIMIT 2000
        """, params)

        tracks = cursor.fetchall()
        conn.close()

        if not tracks:
            print("✅ All tracks have recent popularity data")
            log_basic("All tracks have recent popularity data")
            return
        print(f"📊 Scanning popularity for {len(tracks)} tracks..." + (f" (artist={artist})" if artist else ""))

        updated_count = 0
        current_album = None
        album_tracks = 0
        
        for idx, track in enumerate(tracks, 1):
            if idx % 50 == 0:
                print(f"Progress: {idx}/{len(tracks)}")
                log_basic(f"Popularity scan progress: {idx}/{len(tracks)}")
                save_popularity_progress(idx, len(tracks))

            artist_name = track['artist']
            album_name = track['album']
            title = track['title']
            track_id = track['id']
            
            # Track album changes to log when we finish an album
            if current_album != (artist_name, album_name):
                # Log the previous album if we were processing one
                if current_album is not None and album_tracks > 0:
                    log_album_scan(current_album[0], current_album[1], 'popularity', album_tracks, 'completed')
                    log_basic(f"Completed popularity scan for {current_album[0]} - {current_album[1]} ({album_tracks} tracks)")
                
                current_album = (artist_name, album_name)
                album_tracks = 0
            
            album_tracks += 1

            # Get Spotify score
            spotify_score = track['spotify_score'] or 0
            try:
                results = search_spotify_track(title, artist_name)
                if results and isinstance(results, list) and len(results) > 0:
                    best_match = max(results, key=lambda r: r.get('popularity', 0))
                    spotify_score = best_match.get('popularity', 0)
                    if verbose:
                        log_verbose(f"Spotify popularity for {title}: {spotify_score}")
            except Exception as e:
                log_verbose(f"Spotify popularity lookup failed for {title}: {e}")

            # Get Last.fm ratio + raw playcount
            lastfm_ratio = track['lastfm_ratio'] or 0
            lastfm_playcount = 0
            try:
                info = get_lastfm_track_info(artist_name, title)
                if info and info.get('track_play'):
                    lastfm_playcount = int(info['track_play'])
                    lastfm_ratio = min(100, lastfm_playcount / 10)
                    if verbose:
                        log_verbose(f"Last.fm ratio for {title}: {lastfm_ratio} (playcount: {info['track_play']})")
            except Exception as e:
                log_verbose(f"Last.fm lookup failed for {title}: {e}")

            # Get ListenBrainz score
            listenbrainz_count = track['listenbrainz_score'] or 0
            try:
                mbid_value = track['mbid'] if 'mbid' in track.keys() else ''
                score = get_listenbrainz_score(mbid_value, artist_name, title)
                listenbrainz_count = score
                if verbose or score > 0:
                    if score > 0:
                        log_verbose(f"ListenBrainz count for {title}: {listenbrainz_count}")
                    else:
                        log_verbose(f"ListenBrainz: No data available for {title} (MBID: {mbid_value or 'N/A'})")
            except Exception as e:
                log_verbose(f"ListenBrainz lookup failed for {title}: {e}")
                # --- Singles detection: run alongside popularity scan ---
                # Prepare album context for single detection (minimal, can be expanded)
                album_ctx = {"album": album_name}
                # Call single detection and update track dict
                try:
                    single_result = rate_track_single_detection(
                        dict(track),  # pass a copy to avoid side effects
                        artist_name,
                        album_ctx,
                        singles_config,
                        verbose=verbose
                    )
                except Exception as e:
                    log_basic(f"Single detection failed for {title}: {e}")
                    single_result = {}

                # Extract singles fields for DB update
                is_single = single_result.get("is_single", False)
                single_sources = ",".join(single_result.get("single_sources", [])) if single_result.get("single_sources") else None
                single_confidence = single_result.get("single_confidence", None)
                single_stars = single_result.get("stars", None)

            # Compute composite score (align with start.py scoring)
            release_date = track['spotify_release_date'] or "1992-01-01"
            momentum_raw, _ = score_by_age(lastfm_playcount, release_date)
            sp_norm = spotify_score / 100.0
                    cursor.execute("""
                        UPDATE tracks SET
                            spotify_score = ?,
                            lastfm_ratio = ?,
                            listenbrainz_score = ?,
                            score = ?,
                            popularity_score = ?,
                            stars = CASE WHEN (stars IS NULL OR stars = 0) THEN ? ELSE stars END,
                            is_single = ?,
                            single_source = ?,
                            single_confidence = ?,
                            single_stars = ?
                        WHERE id = ?
                    """,
                    (
                        spotify_score,
                        lastfm_ratio,
                        listenbrainz_count,
                        composite_score,
                        composite_score,
                        base_stars,
                        int(is_single),
                        single_sources,
                        single_confidence,
                        single_stars,
                        track_id
                    ))
            # Initial star assignment (will be refined later by single detection)
            if composite_score >= 4.0:
                base_stars = 4
            elif composite_score >= 2.5:
                base_stars = 3
            elif composite_score >= 1.0:
                base_stars = 2
            else:
                base_stars = 1

            # Update database
            try:
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE tracks SET
                        spotify_score = ?,
                        lastfm_ratio = ?,
                        listenbrainz_score = ?,
                        score = ?,
                        popularity_score = ?,
                        stars = CASE WHEN (stars IS NULL OR stars = 0) THEN ? ELSE stars END
                    WHERE id = ?
                """, (spotify_score, lastfm_ratio, listenbrainz_count, composite_score, composite_score, base_stars, track_id))
                conn.commit()
                conn.close()
                updated_count += 1

                if verbose:
                    print(f"  ✓ {title}: Spotify={spotify_score}, LastFM={lastfm_ratio:.1f}, LB={listenbrainz_count}, Score={composite_score:.3f}")
            except Exception as e:
                log_basic(f"Failed to update track {track_id}: {e}")

        # Log the final album after the loop completes
        if current_album is not None and album_tracks > 0:
            log_album_scan(current_album[0], current_album[1], 'popularity', album_tracks, 'completed')
            log_basic(f"Completed popularity scan for {current_album[0]} - {current_album[1]} ({album_tracks} tracks)")

        print(f"✅ Popularity scan complete: Updated {updated_count}/{len(tracks)} tracks")
        log_basic(f"Popularity scan complete: Updated {updated_count}/{len(tracks)} tracks")

    except Exception as e:
        log_basic(f"Popularity scan failed: {e}")
        print(f"❌ Popularity scan failed: {e}")
        raise

    finally:
        log_basic("=" * 60)

def popularity_scan(verbose: bool = False):
    """Detect track popularity from external sources (legacy function)"""
    log_basic("=" * 60)
    log_basic("Popularity Scanner Started")
    log_basic("=" * 60)

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Get all tracks that need popularity detection
        cursor.execute("""
            SELECT id, artist, title, album
            FROM tracks
            WHERE popularity_score IS NULL OR popularity_score = 0
            ORDER BY artist, title
        """)

        tracks = cursor.fetchall()
        log_basic(f"Found {len(tracks)} tracks to scan for popularity")

        scanned_count = 0

        for track in tracks:
            track_id = track["id"]
            artist = track["artist"]
            title = track["title"]

            if verbose:
                log_verbose(f"Scanning: {artist} - {title}")

            # Try to get popularity from Spotify
            spotify_score = 0
            try:
                artist_id = get_spotify_artist_id(artist)
                if artist_id:
                    spotify_results = search_spotify_track(title, artist, track.get("album"))
                    if spotify_results and isinstance(spotify_results, list) and len(spotify_results) > 0:
                        # Select best match (highest popularity)
                        best_match = max(spotify_results, key=lambda r: r.get('popularity', 0))
                        spotify_score = best_match.get("popularity", 0)
            except Exception as e:
                if verbose:
                    log_verbose(f"Spotify lookup failed for {artist} - {title}: {e}")

            # Try to get popularity from Last.fm
            lastfm_score = 0
            try:
                lastfm_info = get_lastfm_track_info(artist, title)
                if lastfm_info and lastfm_info.get("track_play"):
                    lastfm_score = min(100, int(lastfm_info["track_play"]) // 100)
            except Exception as e:
                if verbose:
                    log_verbose(f"Last.fm lookup failed for {artist} - {title}: {e}")

            # Average the scores
            if spotify_score > 0 or lastfm_score > 0:
                popularity_score = (spotify_score + lastfm_score) / 2.0
                cursor.execute(
                    "UPDATE tracks SET popularity_score = ? WHERE id = ?",
                    (popularity_score, track_id)
                )
                scanned_count += 1

        conn.commit()
        conn.close()

        log_basic(f"✅ Popularity scan completed: {scanned_count} tracks updated")

    except Exception as e:
        log_basic(f"❌ Popularity scan failed: {str(e)}")
        raise

    finally:
        log_basic("=" * 60)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Detect track popularity from external sources")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output")
    
    args = parser.parse_args()
    popularity_scan(verbose=args.verbose)
