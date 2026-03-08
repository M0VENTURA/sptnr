# ...existing code...

import os
import math
from datetime import datetime, timedelta
import sys
import yaml
from concurrent.futures import ThreadPoolExecutor
from typing import cast
# Placeholders for undefined variables/objects (replace with actual implementations as needed)
session = None
nav_client = None
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/config.yaml")
get_spotify_artist_id = None
get_spotify_artist_single_track_ids = None
parse_datetime_flexible = None
log_album_scan = None
search_spotify_track = None
SPOTIFY_WEIGHT = 1.0
LASTFM_WEIGHT = 1.0
LISTENBRAINZ_WEIGHT = 1.0
AGE_WEIGHT = 1.0
GOOGLE_ENABLED = False
YOUTUBE_ENABLED = False
AUDIODB_ENABLED = False
load_config = None
import logging
import sqlite3
import time
import json
import re
import difflib
from statistics import median
from collections import defaultdict
from helpers.helpers import strip_parentheses
from popularity import create_or_update_playlist_for_artist, refresh_all_playlists_from_db
# Dummy/placeholder clients for external APIs (replace with actual imports as needed)
musicbrainz_client = None
discogs_client = None
audiodb_client = None
lastfm_client = None
listenbrainz_client = None
# Placeholder config values (replace with actual config loading as needed)
LOG_PATH = 'app.log'

# Import shared helpers from popularity_helpers
from popularity_helpers import (
    fetch_artist_albums,
    fetch_album_tracks,
    save_to_db,
    build_artist_index,
    load_artist_map,
    get_album_last_scanned_from_db,
    get_album_track_count_in_db,
)
# Import DB connection helper
from helpers.db_utils import get_db_connection, get_current_track_rating, _is_postgres_connection

# Import scan helpers
from helpers.scan_helpers import scan_library_to_db

# Import single detection helper
from single_detector import get_current_single_detection

# --- Navidrome user config ---
import yaml
with open(CONFIG_PATH, 'r') as f:
    _cfg = yaml.safe_load(f)
_nav = _cfg.get('navidrome', {})
_nav_users = _cfg.get('navidrome_users', None)
if _nav_users:
    NAV_USERS = [
        {"base_url": u["base_url"], "user": u["user"], "pass": u["pass"]}
        for u in _nav_users
    ]
else:
    NAV_USERS = None
NAV_BASE_URL = _nav.get("base_url")
USERNAME = _nav.get("user")
PASSWORD = _nav.get("pass")

# --- log_unified fallback ---
def log_unified(msg: str) -> None:
    """Unified logging function that wraps either imported or fallback implementation."""
    try:
        from popularity_helpers import log_unified as _imported_log_unified
        _imported_log_unified(msg)
    except ImportError:
        print(f"[LOG] {msg}")

# --- argparse import ---
import argparse

# Note: get_current_track_rating() imported from helpers.db_utils (line 60)

# --- Spotify API Helpers ---
def _clean_values(values):
    """Return list of numeric values excluding None; keep zeros as informative."""
    return [v for v in values if v is not None]

def _mad(values):
    """Median Absolute Deviation (robust dispersion)."""
    vals = _clean_values(values)
    if not vals:
        return 0.0
    m = median(vals)
    return median([abs(v - m) for v in vals])

def _cv(values):
    """Coefficient of Variation (std/mean) â€“ simple, less robust; use MAD if you prefer."""
    vals = _clean_values(values)
    if not vals:
        return 0.0
    mean = sum(vals) / len(vals)
    if mean == 0:
        return 0.0
    # A lightweight std approximation (no statistics.stdev to avoid tiny samples).
    var = sum((v - mean) ** 2 for v in vals) / max(1, (len(vals) - 1))
    std = var ** 0.5
    return std / mean

def _coverage(values):
    """Fraction of tracks with non-None values."""
    total = len(values)
    non_null = len([v for v in values if v is not None])
    return (non_null / total) if total else 0.0

def _reliability(dispersion, coverage, n_effective, disp_floor=1e-6):
    """
    Combine dispersion & coverage into a reliability score.
    - dispersion: MAD or CV (prefer MAD for robustness)
    - coverage: fraction in [0,1]
    - n_effective: non-null count, shrinks score for tiny samples
    """
    disp = max(dispersion, disp_floor)
    size_factor = min(1.0, n_effective / 8.0)  # shrink when few points
    return disp * coverage * size_factor

def compute_adaptive_weights(album_tracks, base_weights, clamp=(0.25, 1.75), use='mad'):
    """
    Compute per-album adaptive weights for spotify/lastfm/listenbrainz.
    base_weights: dict like {'spotify': 0.4, 'lastfm': 0.3, 'listenbrainz': 0.2}
    clamp: (min_factor, max_factor) relative to base weight
    use: 'mad' (robust) or 'cv' (simple)
    Returns normalized weights that sum to 1 across the three sources.
    """
    # Collect per-track raw values
    sp = [t.get('spotify_score') for t in album_tracks]
    lf = [t.get('lastfm_ratio')   for t in album_tracks]  # youâ€™ll add this field below
    lb = [t.get('listenbrainz_score') for t in album_tracks]

    # Choose dispersion metric
    disp_fn = _mad if use == 'mad' else _cv

    # Compute metrics per source
    def metrics(vals):
        disp = disp_fn(vals)
        cov  = _coverage(vals)
        n_eff = len([v for v in vals if v is not None])
        rel = _reliability(disp, cov, n_eff)
        return disp, cov, n_eff, rel

    sp_d, sp_c, sp_n, sp_rel = metrics(sp)
    lf_d, lf_c, lf_n, lf_rel = metrics(lf)
    lb_d, lb_c, lb_n, lb_rel = metrics(lb)

    # Relative reliability as multipliers vs. mean reliability
    rels = {'spotify': sp_rel, 'lastfm': lf_rel, 'listenbrainz': lb_rel}
    mean_rel = sum(rels.values()) / max(1, len(rels))
    # If all reliabilities are ~0 (no info anywhere), fall back to base
    if mean_rel == 0:
        return base_weights.copy()

    factors = {k: (rels[k] / mean_rel) for k in rels}
    # Clamp relative factors to avoid extreme swings
    min_f, max_f = clamp
    factors = {k: min(max(factors[k], min_f), max_f) for k in factors}

    # Apply to base weights and renormalize to sum=1
    adapted = {k: base_weights.get(k, 0.0) * factors[k] for k in factors}
    total = sum(adapted.values())
    if total == 0:
        return base_weights.copy()
    adapted = {k: adapted[k] / total for k in adapted}
    return adapted



def select_best_spotify_match(results, track_title, album_context: dict | None = None):
    """
    Select the best Spotify match based on popularity and album type,
    allowing 'live' only when album context permits (live/unplugged).
    """
    allow_live_remix = bool(album_context and (album_context.get("is_live") or album_context.get("is_unplugged")))
    filtered = [r for r in results if is_valid_version(r["name"], allow_live_remix=allow_live_remix)]
    if not filtered:
        return {"popularity": 0}

    singles = [r for r in filtered if (r.get("album", {}).get("album_type", "").lower() == "single")]
    if singles:
        return max(singles, key=lambda r: r.get("popularity", 0))
    return max(filtered, key=lambda r: r.get("popularity", 0))

try:
    from bs4 import BeautifulSoup
    HAVE_BS4 = True
except Exception:
    HAVE_BS4 = False

# --- Helpers (reuse your existing normalizers) ---
_DEF_USER_AGENT = "sptnr-cli/2.0"





def get_suggested_mbid(title: str, artist: str, limit: int = 5) -> tuple[str, float]:
    """Get suggested MusicBrainz ID (wrapper using MusicBrainzClient)."""
    if musicbrainz_client:
        return musicbrainz_client.get_suggested_mbid(title, artist, limit)
    return ("", 0.0)

# --- Genre Helpers ---

def get_discogs_genres(title: str, artist: str) -> list[str]:
    """Fetch genres from Discogs (wrapper using DiscogsClient)."""
    if discogs_client:
        return discogs_client.get_genres(title, artist)
    return []

def get_audiodb_genres(artist: str) -> list[str]:
    """Fetch genres from AudioDB (wrapper using AudioDbClient)."""
    if audiodb_client:
        return audiodb_client.get_artist_genres(artist)
    return []

def get_musicbrainz_genres(title: str, artist: str) -> list[str]:
    """Fetch genres from MusicBrainz (wrapper using MusicBrainzClient)."""
    if musicbrainz_client:
        return musicbrainz_client.get_genres(title, artist)
    return []

def is_valid_version(track_title, allow_live_remix=False):
    """Validate track version against blacklist and whitelist."""
    title = track_title.lower()
    blacklist = ["live", "remix", "mix", "edit", "rework", "bootleg"]
    whitelist = ["remaster"]
    if allow_live_remix:
        blacklist = [b for b in blacklist if b not in ["live", "remix"]]
    if any(b in title for b in blacklist) and not any(w in title for w in whitelist):
        return False
    return True

# `strip_parentheses` moved to `helpers.py` for reuse across modules
# --- Last.fm Helpers ---


def get_lastfm_track_info(artist: str, title: str) -> dict:
    """Implementation moved to popularity.py"""
    return {}


def get_listenbrainz_score(mbid: str, artist: str = "", title: str = "") -> int:
    """Implementation moved to popularity.py"""
    return 0


def score_by_age(playcount: int, release_str: str) -> int:
    """Implementation moved to popularity.py"""
    return 0

# --- Genre Handling ---

GENRE_WEIGHTS = {
    "musicbrainz": 0.40,
    "discogs": 0.25,
    "audiodb": 0.20,
    "lastfm": 0.10,
    "spotify": 0.05
}

def normalize_genre(genre):
    """Normalize genre names to avoid duplicates and inconsistencies."""
    genre = genre.lower().strip()
    synonyms = {"hip hop": "hip-hop", "r&b": "rnb"}
    return synonyms.get(genre, genre)

def clean_conflicting_genres(genres):
    """Remove conflicting or irrelevant genres based on dominant tags."""
    genres_lower = [g.lower() for g in genres]
    if any("punk" in g for g in genres_lower):
        genres_lower = [g for g in genres_lower if g not in ["electronic", "electro"]]
    if any("metal" in g for g in genres_lower):
        genres_lower = [g for g in genres_lower if g not in ["electronic", "electro"]]
    if any("progressive metal" in g for g in genres_lower):
        genres_lower = [g for g in genres_lower if g not in ["metal", "heavy metal"]]
    return genres_lower

def get_top_genres_with_navidrome(sources, nav_genres, title="", album=""):
    """Combine online-sourced genres with Navidrome genres for comparison."""
    genre_scores = defaultdict(float)
    for source, genres in sources.items():
        weight = GENRE_WEIGHTS.get(source, 0)
        for genre in genres:
            norm = normalize_genre(genre)
            genre_scores[norm] += weight
    if "live" in title.lower() or "live" in album.lower():
        genre_scores["live"] += 0.5
    if any(word in title.lower() or word in album.lower() for word in ["christmas", "xmas"]):
        genre_scores["christmas"] += 0.5
    sorted_genres = sorted(genre_scores.items(), key=lambda x: x[1], reverse=True)
    filtered = [g for g, _ in sorted_genres]
    filtered = clean_conflicting_genres(filtered)
    filtered = list(dict.fromkeys(filtered))
    metal_subgenres = [g for g in filtered if "metal" in g.lower() and g.lower() != "heavy metal"]
    if metal_subgenres:
        filtered = [g for g in filtered if g.lower() != "heavy metal"]
    if not filtered:
        filtered = [g for g, _ in sorted_genres]
    online_top = [g.capitalize() for g in filtered[:3] if g is not None]
    nav_cleaned: list[str] = [
        normalize_genre(str(g)).capitalize() 
        for g in nav_genres 
        if g is not None
    ]
    return online_top, nav_cleaned

def set_track_rating_for_all(track_id, stars):
    print("ðŸ”„ Refreshing smart playlists for all artists from DB cache (no track rescans)...")
    # Pull distinct artists that have cached tracks
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT artist FROM tracks")
    artists = [row[0] for row in cursor.fetchall()]
    conn.close()
    if not artists:
        print("âš ï¸ No cached tracks in DB. Skipping playlist refresh.")
        return
    for name in artists:
        conn = get_db_connection()
        cursor = conn.cursor()
        is_pg = bool(_is_postgres_connection(conn))
        placeholder = "%s" if is_pg else "?"
        cursor.execute(f"SELECT id, artist, album, title, stars FROM tracks WHERE artist = {placeholder}", (name,))
        rows = cursor.fetchall()
        conn.close()
        if not rows:
            print(f"âš ï¸ No cached tracks found for '{name}', skipping.")
            continue
        tracks = [{"id": r[0], "artist": r[1], "album": r[2], "title": r[3], "stars": int(r[4]) if r[4] else 0}
                  for r in rows]
        create_or_update_playlist_for_artist(name, tracks)
        print(f"âœ… Playlist refreshed for '{name}' ({len(tracks)} tracks)")

# Note: build_artist_index() imported from popularity_helpers (line 60)
# Note: scan_library_to_db() imported from helpers.scan_helpers (line 779 in run_full_scan_pipeline)

# --- Main Rating Logic ---
    """
    Scan the entire Navidrome library (artists -> albums -> tracks) and persist
    a lightweight representation of each track into the local DB.

    Behavior:
      - Uses NavidromeClient API helpers: build_artist_index(), fetch_artist_albums(), fetch_album_tracks()
      - For each track, writes a minimal `track_data` record via `save_to_db()`
      - Uses INSERT OR REPLACE semantics (so re-running is safe and refreshes `last_scanned`)
    """
    
    def _safe_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    print("ðŸ”Ž Scanning Navidrome library into local DB...")
    artist_map_local = build_artist_index(verbose=verbose) or {}
    if not artist_map_local:
        print("âš ï¸ No artists available from Navidrome; aborting library scan.")
        return


    # Cache existing track IDs to avoid re-writing cached rows unless force=True
    existing_track_ids: set[str] = set()
    try:
        # Try app's get_db first (PostgreSQL-aware)
        try:
            from app import get_db as app_get_db, _is_postgres_connection as app_is_postgres_connection
            conn = app_get_db()
            is_pg = bool(app_is_postgres_connection(conn))
        except Exception:
            conn = get_db_connection()
            is_pg = False
        
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
    
    print(f"ðŸ“Š Starting scan of {total_artists} artists...")
    
    for name, info in artist_map_local.items():
        artist_count += 1
        artist_id = info.get("id")
        if not artist_id:
            print(f"âš ï¸ [{artist_count}/{total_artists}] Skipping '{name}' (no artist ID)")
            continue
        
        print(f"ðŸŽ¨ [{artist_count}/{total_artists}] Processing artist: {name}")
        logging.debug(f"Processing artist {artist_count}/{total_artists}: {name} (ID: {artist_id})")

        # Prefetch cached tracks for this artist to enable per-artist skip decisions
        existing_album_tracks: dict[str, set[str]] = {}
        try:
            # Try app's get_db first (PostgreSQL-aware)
            try:
                from app import get_db as app_get_db, _is_postgres_connection as app_is_postgres_connection
                conn = app_get_db()
                is_pg = bool(app_is_postgres_connection(conn))
            except Exception:
                conn = get_db_connection()
                is_pg = False
            
            cursor = conn.cursor()
            placeholder = "%s" if is_pg else "?"
            cursor.execute(f"SELECT album, id FROM tracks WHERE artist = {placeholder}", (name,))
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
                print(f"   ðŸ“€ Found {len(albums)} albums")
                logging.debug(f"Found {len(albums)} albums for artist '{name}'")
        except Exception as e:
            print(f"   âŒ Failed to fetch albums: {e}")
            logging.error(f"Failed to fetch albums for '{name}': {e}")
            albums = []
        
        album_count = 0
        for alb in albums:
            album_count += 1
            album_name = alb.get("name") or ""
            album_id = alb.get("id")
            if not album_id:
                continue
            
            print(f"   ðŸ“€ [{album_count}/{len(albums)}] Album: {album_name}")
            logging.debug(f"Scanning album {album_count}/{len(albums)}: {album_name}")
            
            try:
                album_data = fetch_album_tracks(album_id)
                tracks = album_data.get("tracks", [])
                api_album_artist = album_data.get("artist", "")
                if tracks:
                    print(f"      ðŸŽµ Found {len(tracks)} tracks")
                    logging.info(f"Found {len(tracks)} tracks in album '{album_name}'")
            except Exception as e:
                print(f"      âŒ Failed to fetch tracks: {e}")
                logging.error(f"Failed to fetch tracks for album '{album_name}': {e}")
                tracks = []
                api_album_artist = ""

            # Album-level skip if counts already match cached tracks (unless force=True)
            cached_ids_for_album = existing_album_tracks.get(album_name, set())
            if not force and tracks and len(cached_ids_for_album) >= len(tracks):
                total_albums_skipped += 1
                print(f"      â© Skipping album (already cached): {album_name}")
                logging.info(f"Skipping album '{album_name}' â€” cached {len(cached_ids_for_album)} tracks matches API {len(tracks)}")
                continue
            
            tracks_written = 0
            tracks_skipped = 0
            tracks_updated = 0
            
            # Get the album artist with priority order:
            # 1. api_album_artist - from getAlbum.view response (most reliable)
            # 2. alb.get("artist") - from getArtist.view response 
            # 3. name - the function parameter (artist we're importing)
            # Note: track.albumArtist field can be incorrect (e.g., containing track artist with feat.)
            album_artist_value = api_album_artist or alb.get("artist") or name
            
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
                
                # Extract track metadata including writer/lyricist information
                from api_clients.navidrome import NavidromeClient
                navi_client = NavidromeClient(base_url="", username="", password="")  # URLs/auth not needed for extraction
                extracted = navi_client.extract_track_metadata(t)
                
                # Fallback: Try to read writer info from ID3 tags if Navidrome didn't provide it
                writer_json = extracted.get("writer", "[]")
                if not writer_json or writer_json == "[]":
                    file_path = t.get("path", "")
                    if file_path and os.path.exists(file_path):
                        try:
                            from mutagen.mp3 import MP3
                            from mutagen.flac import FLAC
                            from mutagen.id3 import ID3
                            import json as json_module
                            
                            writers = []
                            # Load configured tag aliases from config
                            _writer_config = _cfg.get("tags", {}).get("writer", {})
                            writer_aliases = _writer_config.get("aliases", [
                                "TWRT", "TOLY", "TXXX:WRITER", "TXXX:LYRICIST", "TXXX:AUTHOR",
                                "WRITER", "LYRICIST", "AUTHOR", "©wrt"
                            ])
                            
                            if file_path.lower().endswith('.mp3'):
                                audio = MP3(file_path, ID3=ID3)
                                if audio.tags:
                                    # Check ID3 tags using configured aliases
                                    for alias in writer_aliases:
                                        if alias.startswith("TXXX:"):
                                            # Custom TXXX frame - check by description
                                            desc = alias.split(":", 1)[1].upper()
                                            for frame in audio.tags.values():
                                                if frame.FrameID == 'TXXX' and hasattr(frame, 'desc'):
                                                    if frame.desc and frame.desc.upper() == desc:
                                                        if hasattr(frame, 'text') and frame.text:
                                                            value = str(frame.text[0])
                                                            writers.extend([w.strip() for w in value.replace(';', ',').split(',') if w.strip()])
                                        elif alias in audio.tags:
                                            # Standard ID3 tag
                                            value = str(audio.tags[alias].text[0]) if audio.tags[alias].text else None
                                            if value:
                                                writers.extend([w.strip() for w in value.replace(';', ',').split(',') if w.strip()])
                                        elif alias.startswith("©"):
                                            # iTunes atom tag - check if present
                                            if alias in audio.tags:
                                                value = str(audio.tags[alias].text[0]) if audio.tags[alias].text else None
                                                if value:
                                                    writers.extend([w.strip() for w in value.replace(';', ',').split(',') if w.strip()])
                            elif file_path.lower().endswith('.flac'):
                                audio = FLAC(file_path)
                                # FLAC uses Vorbis comments
                                for alias in writer_aliases:
                                    if not alias.startswith(("TXXX", "©")):  # Skip ID3/iTunes-only tags
                                        key = alias.upper()
                                        if key in audio and audio[key]:
                                            for value in audio[key]:
                                                writers.extend([w.strip() for w in value.replace(';', ',').split(',') if w.strip()])
                            
                            # Remove duplicates while preserving order
                            seen = set()
                            unique_writers = []
                            for w in writers:
                                if w.lower() not in seen:
                                    unique_writers.append(w)
                                    seen.add(w.lower())
                            
                            if unique_writers:
                                writer_json = json_module.dumps(unique_writers)
                                logging.debug(f"[WRITER] Extracted from ID3 tags for {t.get('title')}: {unique_writers}")
                        except Exception as e:
                            logging.debug(f"[WRITER] Could not read ID3 tags from {file_path}: {e}")
                
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
                    "genres": json.dumps([]),  # Serialize as JSON string
                    "navidrome_genres": "\\".join([g.get("name", "").strip() for g in t.get("genres", []) if g.get("name", "").strip()]) if t.get("genres") else ("\\".join([g.strip() for g in t.get("genre", "").replace("•", "\\").replace(";", "\\").replace(",", "\\").split("\\") if g.strip()]) if t.get("genre") else ""),  # Extract from genres array if available, else fall back to genre field
                    "spotify_genres": json.dumps([]),  # Serialize as JSON string
                    "lastfm_tags": json.dumps([]),  # Serialize as JSON string
                    "discogs_genres": json.dumps([]),  # Serialize as JSON string
                    "audiodb_genres": json.dumps([]),  # Serialize as JSON string
                    "musicbrainz_genres": json.dumps([]),  # Serialize as JSON string
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
                    "single_sources": json.dumps([]),  # Serialize as JSON string
                    "stars": 0,
                    "mbid": t.get("mbid", "") or "",
                    "suggested_mbid": "",
                    "suggested_mbid_confidence": 0.0,
                    "stars": int(t.get("userRating", 0) or 0),
                    # Enhanced metadata from Navidrome for better matching
                    # Normalize track/disc numbers from Navidrome (trackNumber/track, discNumber/disc)
                    "duration": t.get("duration"),  # Track duration in seconds
                    "track_number": _safe_int(t.get("trackNumber") or t.get("track")),
                    "disc_number": _safe_int(t.get("discNumber") or t.get("disc") or 1) or 1,
                    "year": t.get("year"),  # Release year
                    "writer": writer_json,  # JSON array of lyricists/writers from Navidrome or ID3 tags
                    "album_artist": album_artist_value,  # Album artist from album object
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
                print(f"      âœ… Saved {tracks_written} new tracks to DB")
                logging.info(f"Saved {tracks_written} new tracks from album '{album_name}'")
            if tracks_updated > 0:
                print(f"      ðŸ”„ Updated {tracks_updated} tracks with new metadata")
                logging.info(f"Updated {tracks_updated} tracks with metadata from album '{album_name}'")
            if tracks_skipped > 0:
                total_skipped += tracks_skipped
                print(f"      â© Skipped {tracks_skipped} cached tracks")
                logging.info(f"Skipped {tracks_skipped} cached tracks for album '{album_name}'")
        
        if album_count > 0:
            print(f"   âœ… Completed {album_count} albums for '{name}'")
            
    print(f"âœ… Library scan complete. Tracks written/updated: {total_written}; skipped cached: {total_skipped}")
def update_artist_stats(artist_id, artist_name):
    album_count = len(fetch_artist_albums(artist_id))
    track_count = sum(len(fetch_album_tracks(a['id']).get("tracks", [])) for a in fetch_artist_albums(artist_id))
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO artist_stats (artist_id, artist_name, album_count, track_count, last_updated)
        VALUES (?, ?, ?, ?, ?)
    """, (artist_id, artist_name, album_count, track_count, datetime.now().strftime("%Y-%m-%dT%H:%M:%S")))
    conn.commit()
    conn.close()

# Note: load_artist_map() imported from popularity_helpers (line 60)

def adjust_genres(genres, artist_is_metal=False):
    """
    Adjust genres based on artist context:
    - If artist is metal-dominant, convert rock sub-genres to metal equivalents.
    - Always deduplicate and remove generic 'metal' if sub-genres exist.
    """
    adjusted = []
    for g in genres:
        g_lower = g.lower()
        if artist_is_metal:
            if g_lower in ["prog rock", "progressive rock"]:
                adjusted.append("Progressive metal")
            elif g_lower == "folk rock":
                adjusted.append("Folk metal")
            elif g_lower == "goth rock":
                adjusted.append("Gothic metal")
            else:
                adjusted.append(g)
        else:
            adjusted.append(g)

    # Remove generic 'metal' if specific sub-genres exist
    metal_subgenres = [x for x in adjusted if "metal" in x.lower() and x.lower() != "metal"]
    if metal_subgenres:
        adjusted = [x for x in adjusted if x.lower() not in ["metal", "heavy metal"]]

    return list(dict.fromkeys(adjusted))  # Deduplicate


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

# Note: rate_artist from deprecated sptnr.py is no longer used.
# All rating logic is now in popularity.py as part of the popularity_scan() function.



def run_full_scan_pipeline(verbose=False, force=False):
    """
    Execute the full scan pipeline when full_scan is enabled in config.yaml.
    
    This runs two scans in sequence:
    1. Navidrome import - imports metadata from Navidrome
    2. Popularity detection - detects popularity and singles
    
    All progress is logged to unified_scan.log and progress bars are updated.
    
    Args:
        verbose: Enable verbose output
        force: Force re-scan of all data
    """
    from popularity import popularity_scan
    
    log_unified("=" * 80)
    log_unified("🔄 FULL SCAN PIPELINE STARTED")
    log_unified("=" * 80)
    log_unified(f"Start time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log_unified(f"Verbose: {verbose}, Force: {force}")
    log_unified("")
    
    try:
        # Step 1: Navidrome Import
        log_unified("📥 STEP 1/3: Navidrome Import")
        log_unified("-" * 80)
        log_unified("Importing metadata from Navidrome...")
        scan_library_to_db(verbose=verbose, force=force)
        log_unified("✅ Navidrome import complete")
        log_unified("")
        
        # Step 2: Popularity Detection (includes singles detection)
        log_unified("⭐ STEP 2/3: Popularity Detection")
        log_unified("-" * 80)
        log_unified("Detecting track popularity and singles...")
        popularity_scan(verbose=verbose, force=force, skip_header=True)
        log_unified("✅ Popularity detection complete")
        log_unified("")
        
        # Pipeline complete
        log_unified("=" * 80)
        log_unified("✅ FULL SCAN PIPELINE COMPLETE")
        log_unified("=" * 80)
        log_unified(f"End time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
    except KeyboardInterrupt:
        log_unified("⚠️ Full scan pipeline interrupted by user")
        raise
    except Exception as e:
        log_unified(f"❌ Full scan pipeline failed: {e}")
        logging.error(f"Full scan pipeline error: {e}", exc_info=True)
        raise


# âœ… Main scan function that can be called from app.py
def run_scan(scan_type='full', verbose=False, force=False, dry_run=False):
    """
    Execute a scan of the music library.
    
    Args:
        scan_type: 'full' or 'perpetual' (default: 'full')
        verbose: Enable verbose output
        force: Force re-scan of all tracks
        dry_run: Preview only, don't apply ratings
    """
    global config
    
    
    # Create scan lock file to indicate scanning is in progress
    scan_lock_path = "/config/.scan_lock"
    try:
        with open(scan_lock_path, 'w') as f:
            f.write(str(os.getpid()))
    except Exception as e:
        logging.warning(f"Could not create scan lock file: {e}")
    # Log scan start to unified log
    if log_unified:
        log_unified(f"🟢 SPTNR scan started: type={scan_type}, time={datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # ✅ Reload config on each run
    config = {}  # Default empty config
    if load_config:
        config = load_config() or {}
    else:
        logging.warning("load_config not available")
    
    # Get configuration options
    full_scan = scan_type == 'full'
    perpetual = scan_type == 'perpetual'
    force = force or (config.get("features", {}).get("force", False) if config else False)
    dry_run = dry_run or (config.get("features", {}).get("dry_run", False) if config else False)
    verbose = verbose or (config.get("features", {}).get("verbose", False) if config else False)
    artist_list = config.get("features", {}).get("artist", []) if config else []
    
    # If verbose enabled, route debug logs to console as well
    if verbose:
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG)
        # add a console handler if none exists
        if not any(isinstance(h, logging.StreamHandler) for h in root_logger.handlers):
            ch = logging.StreamHandler(sys.stdout)
            ch.setLevel(logging.DEBUG)
            ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
            root_logger.addHandler(ch)

    # Load artist stats from DB
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT artist_id, artist_name, album_count, track_count, last_updated
        FROM artist_stats
    """)
    artist_stats = cursor.fetchall()
    conn.close()

    artist_map = {
        row[1]: {
            "id": row[0],
            "album_count": row[2],
            "track_count": row[3],
            "last_updated": row[4],
        }
        for row in artist_stats
    }

    # If DB is empty, fallback to Navidrome API
    if not artist_map:
        print("âš ï¸ No artist stats found in DB. Building index from Navidrome...")
        artist_map = build_artist_index()

    # Auto-populate track cache when empty
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM tracks")
        has_tracks = (cursor.fetchone()[0] or 0) > 0
        conn.close()
    except Exception:
        has_tracks = False

    if not has_tracks:
        print("âš ï¸ No cached tracks found in DB. Running full library scan to populate cache...")
        try:
            scan_library_to_db(verbose=verbose)
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT artist_id, artist_name, album_count, track_count, last_updated FROM artist_stats")
            artist_stats = cursor.fetchall()
            conn.close()
            artist_map = {row[1]: {"id": row[0], "album_count": row[2], "track_count": row[3], "last_updated": row[4]} for row in artist_stats}
        except Exception as e:
            logging.warning(f"Library scan failed at startup: {e}")
    else:
        if config.get("features", {}).get("scan_on_start", False):
            print("â„¹ï¸ scan_on_start enabled â€” checking Navidrome for new/updated tracks...")
            try:
                scan_library_to_db(verbose=verbose)
            except Exception as e:
                logging.warning(f"scan_on_start failed: {e}")

    # Determine execution mode
    if artist_list:
        print("â„¹ï¸ Running artist-specific rating based on config.yaml...")
        for name in artist_list:
            artist_info = artist_map.get(name)
            if not artist_info:
                print(f"âš ï¸ No data found for '{name}', skipping.")
                continue

            if dry_run:
                print(f"ðŸ‘€ Dry run: would scan '{name}' (ID {artist_info['id']})")
                continue

            if force:
                print(f"âš ï¸ Force enabled: clearing cached data for artist '{name}'...")
                conn = get_db_connection()
                cursor = conn.cursor()
                is_pg = bool(_is_postgres_connection(conn))
                placeholder = "%s" if is_pg else "?"
                cursor.execute(f"DELETE FROM tracks WHERE artist = {placeholder}", (name,))
                cursor.execute(f"DELETE FROM artist_stats WHERE artist_name = {placeholder}", (name,))
                conn.commit()
                conn.close()
                print(f"âœ… Cache cleared for artist '{name}'")


            album_count = len(fetch_artist_albums(artist_info['id']))
            track_count = sum(len(fetch_album_tracks(a['id']).get("tracks", [])) for a in fetch_artist_albums(artist_info['id']))
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO artist_stats (artist_id, artist_name, album_count, track_count, last_updated)
                VALUES (?, ?, ?, ?, ?)
            """, (artist_info['id'], name, album_count, track_count, datetime.now().strftime("%Y-%m-%dT%H:%M:%S")))
            conn.commit()
            conn.close()

    # If force is enabled for full scan mode, clear entire cache before scanning
    if force and full_scan:
        print("âš ï¸ Force enabled: clearing entire cached library...")
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM tracks")
        cursor.execute("DELETE FROM artist_stats")
        conn.commit()
        conn.close()
        print("âœ… Entire cache cleared. Starting fresh...")
        print("â„¹ï¸ Rebuilding artist index from Navidrome after force clear...")
        build_artist_index()

    # Always run full library rating when requested
    if full_scan:
        print("â„¹ï¸ Running full library batch rating based on DB...")
        
        try:
            url = f"{NAV_BASE_URL}/rest/getArtists.view"
            params = {"u": USERNAME, "p": PASSWORD, "v": "1.16.1", "c": "sptnr", "f": "json"}
            res = session.get(url, params=params)
            res.raise_for_status()
            index = res.json().get("subsonic-response", {}).get("artists", {}).get("index", [])
            navidrome_artist_count = sum(len(group.get("artist", [])) for group in index)
            
            navidrome_album_count = 0
            navidrome_track_count = 0
            for group in index:
                for artist in group.get("artist", []):
                    artist_id = artist.get("id")
                    if artist_id:
                        albums = fetch_artist_albums(artist_id)
                        navidrome_album_count += len(albums)
                        for album in albums:
                            tracks = fetch_album_tracks(album.get("id")).get("tracks", [])
                            navidrome_track_count += len(tracks)
            
            print(f"ðŸ“Š Navidrome: {navidrome_artist_count} artists, {navidrome_album_count} albums, {navidrome_track_count} tracks")
        except Exception as e:
            print(f"âš ï¸ Failed to get counts from Navidrome: {e}")
            navidrome_track_count = 0
        
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(DISTINCT artist) FROM tracks")
        db_artist_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(DISTINCT album) FROM tracks")
        db_album_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM tracks")
        db_track_count = cursor.fetchone()[0]
        conn.close()
        
        print(f"ðŸ’¾ Database: {db_artist_count} artists, {db_album_count} albums, {db_track_count} tracks")
        
        if navidrome_track_count != db_track_count or db_track_count == 0:
            # Check if automatic Navidrome sync is enabled
            # When perpetual is false, skip automatic sync (user must manually trigger)
            auto_sync_enabled = config.get("features", {}).get("perpetual", True)
            
            if auto_sync_enabled:
                print("🔄 Track counts don't match. Running full library scan to sync database...")
                scan_library_to_db(verbose=verbose, force=force)
            else:
                print("⚠️ Track counts don't match, but automatic sync is disabled (perpetual=false)")
                print(f"   Navidrome: {navidrome_track_count} tracks, Database: {db_track_count} tracks")
                print("   To sync manually, set perpetual=true or run navidrome_import.py directly")
        else:
            print("âœ… Database is in sync with Navidrome. Refreshing artist index...")
            build_artist_index(verbose=verbose)

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT artist_id, artist_name, album_count, track_count, last_updated
            FROM artist_stats
        """)
        artist_stats = cursor.fetchall()
        conn.close()

        artist_map = {
            row[1]: {
                "id": row[0],
                "album_count": row[2],
                "track_count": row[3],
                "last_updated": row[4],
            }
            for row in artist_stats
        }

        if not artist_map:
            print("âŒ No artists found after rebuild. Aborting batch rating.")
        else:
            for name, artist_info in artist_map.items():
                needs_update = True if force else (
                    not artist_info['last_updated'] or
                    (datetime.now() - parse_datetime_flexible(artist_info['last_updated'])).days > 7
                )

                if not needs_update:
                    print(f"â© Skipping '{name}' (last updated {artist_info['last_updated']})")
                    continue

                if dry_run:
                    print(f"ðŸ‘€ Dry run: would scan '{name}' (ID {artist_info['id']})")
                    continue


                album_count = len(fetch_artist_albums(artist_info['id']))
                track_count = sum(len(fetch_album_tracks(a['id']).get("tracks", [])) for a in fetch_artist_albums(artist_info['id']))
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT OR REPLACE INTO artist_stats (artist_id, artist_name, album_count, track_count, last_updated)
                    VALUES (?, ?, ?, ?, ?)
                """, (artist_info['id'], name, album_count, track_count, datetime.now().strftime("%Y-%m-%dT%H:%M:%S")))
                conn.commit()
                conn.close()
                time.sleep(1.5)

    # Perpetual mode with self-healing index
    if perpetual:
        print("â„¹ï¸ Running perpetual mode based on DB (optimized for stale artists)...")
        while True:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT artist_id, artist_name FROM artist_stats
                WHERE last_updated IS NULL OR last_updated < DATE('now','-7 days')
            """)
            rows = cursor.fetchall()
            conn.close()

            if not rows:
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM artist_stats")
                total_artists = cursor.fetchone()[0]
                conn.close()

                if total_artists == 0:
                    print("âš ï¸ No artists found in DB; rebuilding index from Navidrome...")
                    build_artist_index()
                    conn = get_db_connection()
                    cursor = conn.cursor()
                    cursor.execute("""
                        SELECT artist_id, artist_name FROM artist_stats
                        WHERE last_updated IS NULL OR last_updated < DATE('now','-7 days')
                    """)
                    rows = cursor.fetchall()
                    conn.close()

            if not rows:
                print("âœ… No artists need updating. Sleeping for 12 hours...")
                time.sleep(12 * 60 * 60)
                continue

            print(f"ðŸ”„ Starting scheduled scan for {len(rows)} stale artists...")
            for artist_id, artist_name in rows:
                print(f"ðŸŽ¨ Processing artist: {artist_name} (ID: {artist_id})")

                update_artist_stats(artist_id, artist_name)
                time.sleep(1.5)

            print("ðŸ•’ Scan complete. Sleeping for 12 hours...")
            time.sleep(12 * 60 * 60)
    
    # Remove scan lock file when scan completes (or if perpetual mode exits)
    try:
        if os.path.exists(scan_lock_path):
            os.remove(scan_lock_path)
    except Exception as e:
        logging.warning(f"Could not remove scan lock file: {e}")
    # Log scan completion to unified log
    if log_unified:
        log_unified(f"✅ SPTNR scan complete: type={scan_type}, time={datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


# --- CLI Handling ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ðŸŽ§ SPTNR â€“ Navidrome Rating CLI with API Integration")
    parser.add_argument("--artist", type=str, nargs="+", help="Rate one or more artists by name")
    parser.add_argument("--full-scan", action="store_true", help="Rate the entire library")
    parser.add_argument("--refresh", action="store_true", help="Rebuild artist index cache")
    parser.add_argument("--pipeoutput", type=str, nargs="?", const="", help="Print cached artist index")
    parser.add_argument("--perpetual", action="store_true", help="Run perpetual 12-hour scan loop")
    parser.add_argument("--dry-run", action="store_true", help="Preview artist list only (no rating)")
    parser.add_argument("--sync", action="store_true", help="Push ratings to Navidrome after calculation")
    parser.add_argument("--force", action="store_true", help="Force re-scan of all tracks")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose debug output")
    parser.add_argument("--refresh-playlists", action="store_true",
                        help="Recreate smart playlists for all artists without rescanning tracks")

    args = parser.parse_args()

    # âœ… Update config.yaml with CLI overrides if provided
    def update_config_with_cli(args, config, config_path=CONFIG_PATH):
        updated = False
        if args.dry_run:
            config["features"]["dry_run"] = True; updated = True
        if args.sync:
            config["features"]["sync"] = True; updated = True
        if args.force:
            config["features"]["force"] = True; updated = True
        if args.verbose:
            config["features"]["verbose"] = True; updated = True
        if args.perpetual:
            config["features"]["perpetual"] = True; updated = True
        if args.full_scan:
            config["features"]["full_scan"] = True; updated = True
        if args.artist:
            config["features"]["artist"] = args.artist; updated = True

        if updated:
            try:
                with open(config_path, "w") as f:
                    yaml.safe_dump(config, f)
                print(f"âœ… Config updated with CLI overrides in {config_path}")
            except Exception as e:
                print(f"âŒ Failed to update config.yaml: {e}")

    update_config_with_cli(args, config)

    # âœ… Merge config values for runtime
    dry_run  = config["features"]["dry_run"]
    sync     = config["features"]["sync"]
    force    = config["features"]["force"]
    verbose  = config["features"]["verbose"]
    perpetual = config["features"]["perpetual"]
    full_scan = config["features"].get("full_scan", False)
    artist_list = config["features"]["artist"]
    # Legacy feature flags (deprecated - use api_integrations.enabled instead)
    use_google  = config["features"].get("use_google", GOOGLE_ENABLED)
    use_youtube = config["features"].get("use_youtube", YOUTUBE_ENABLED)
    use_audiodb = config["features"].get("use_audiodb", AUDIODB_ENABLED)
    refresh_index_on_start     = config["features"].get("refresh_artist_index_on_start", False)

    # If verbose enabled, route debug logs to console as well
    if verbose:
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG)
        # add a console handler if none exists
        if not any(isinstance(h, logging.StreamHandler) for h in root_logger.handlers):
            ch = logging.StreamHandler(sys.stdout)
            ch.setLevel(logging.DEBUG)
            ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
            root_logger.addHandler(ch)

    # --- Early startup triggers from YAML flags ---
    if refresh_index_on_start:
        print("ðŸ“š Building artist index from Navidrome (startup)â€¦")
        build_artist_index()

    # âœ… Rebuild artist index if requested by CLI
    if args.refresh:
        build_artist_index()

    # âœ… Pipe output if requested (print cached artist index and exit)
    if args.pipeoutput is not None:
        artist_map = load_artist_map()
        filtered = {
            name: info for name, info in artist_map.items()
            if not args.pipeoutput or args.pipeoutput.lower() in name.lower()
        }
        print(f"\nðŸ“ Cached Artist Index ({len(filtered)} matches):")
        for name, info in filtered.items():
            print(f"ðŸŽ¨ {name} â†’ ID: {info['id']} "
                  f"(Albums: {info['album_count']}, Tracks: {info['track_count']}, "
                  f"Last Updated: {info['last_updated']})")
        sys.exit(0)

    # âœ… Refresh smart playlists from DB cache when requested via CLI and exit
    if args.refresh_playlists:
        refresh_all_playlists_from_db()
        sys.exit(0)

    # ✅ Determine which scan type to run
    scan_type = None
    use_full_pipeline = False
    
    # Check if full_scan is enabled (either CLI or config)
    if args.full_scan or config["features"].get("full_scan", False):
        use_full_pipeline = True
    elif args.perpetual:
        scan_type = 'perpetual'
    
    # ✅ Execute the appropriate scan
    if use_full_pipeline:
        # Run the full scan pipeline (Navidrome → Popularity)
        print("🔄 Starting full scan pipeline (Navidrome → Popularity)...")
        run_full_scan_pipeline(
            verbose=args.verbose or config["features"].get("verbose", False),
            force=args.force or config["features"].get("force", False)
        )
    elif scan_type:
        # Run the standard scan (perpetual mode)
        run_scan(
            scan_type=scan_type, 
            verbose=args.verbose or config["features"].get("verbose", False),
            force=args.force or config["features"].get("force", False),
            dry_run=args.dry_run or config["features"].get("dry_run", False)
        )
    else:
        print("⚠️ No CLI arguments and no enabled features in config.yaml. Exiting...")
        sys.exit(0)



# scan_popularity() has been moved to popularity.py
# This module now calls popularity.scan_popularity() for popularity updates


def enrich_genres_aggressively(artist_name: str, verbose: bool = False):
    """
    Aggressively collect genres from all available sources for an artist.
    Called during rate_artist() to cache comprehensive genre data.
    """
    genres_collected = set()
    
    try:
        # Get from Discogs
        try:
            discogs_genres = get_discogs_genres(artist_name, "")
            if discogs_genres:
                genres_collected.update([g.lower() for g in discogs_genres])
                if verbose:
                    logging.info(f"Discogs genres for {artist_name}: {discogs_genres}")
        except Exception as e:
            logging.debug(f"Discogs genre lookup failed for {artist_name}: {e}")
        
        # Get from AudioDB
        try:
            audiodb_genres = get_audiodb_genres(artist_name)
            if audiodb_genres:
                genres_collected.update([g.lower() for g in audiodb_genres])
                if verbose:
                    logging.info(f"AudioDB genres for {artist_name}: {audiodb_genres}")
        except Exception as e:
            logging.debug(f"AudioDB genre lookup failed for {artist_name}: {e}")
        
        # Get from MusicBrainz
        try:
            mb_genres = get_musicbrainz_genres(artist_name, "")
            if mb_genres:
                genres_collected.update([g.lower() for g in mb_genres])
                if verbose:
                    logging.info(f"MusicBrainz genres for {artist_name}: {mb_genres}")
        except Exception as e:
            logging.debug(f"MusicBrainz genre lookup failed for {artist_name}: {e}")
        
        # Store in cache for later use
        if genres_collected:
            try:
                conn = get_db_connection()
                cursor = conn.cursor()
                
                # Update all tracks for this artist with collected genres
                genres_str = ", ".join(sorted(genres_collected))
                cursor.execute("""
                    UPDATE tracks SET genres = ?
                    WHERE artist = ? AND (genres IS NULL OR genres = '')
                """, (genres_str, artist_name))
                conn.commit()
                conn.close()
                
                if verbose:
                    logging.info(f"Updated {cursor.rowcount} tracks for {artist_name} with {len(genres_collected)} genres")
            except Exception as e:
                logging.debug(f"Failed to update genres for {artist_name}: {e}")
    
    except Exception as e:
        logging.debug(f"Genre enrichment failed for {artist_name}: {e}")
    
    return genres_collected

