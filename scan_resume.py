#!/usr/bin/env python3
"""
Scan Resume Module
==================

Handles auto-resume functionality for interrupted scans.

Features:
1. Detect interrupted scans using progress file and database state
2. Resume from last scanned artist/album
3. Clean up completed scan state
"""

import os
import json
import logging
import tempfile
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
from pathlib import Path
from helpers.db_utils import get_db_connection

# Import centralized logging with fallback
try:
    from helpers.logging_config import log_debug, log_info, log_unified
except ImportError:
    # Fallback if logging_config not available
    def log_debug(msg, **kwargs):
        logging.debug(msg)
    def log_info(msg, **kwargs):
        logging.info(msg)
    def log_unified(msg, **kwargs):
        logging.info(msg)

logger = logging.getLogger(__name__)

# Progress file paths
NAVIDROME_PROGRESS_FILE = os.environ.get("NAVIDROME_PROGRESS_FILE", "/database/navidrome_scan_progress.json")
POPULARITY_PROGRESS_FILE = os.environ.get("POPULARITY_PROGRESS_FILE", "/database/popularity_scan_progress.json")

# Maps each normalized scan type to the scan_history.scan_type values it produces.
# The combined scan drives navidrome + essentia-mood + popularity + singles sub-scans,
# so we check ALL of those when deciding whether combined was recently active.
_HISTORY_TYPES_FOR_SCAN: Dict[str, List[str]] = {
    "combined_scan":      ["navidrome", "essentia-mood", "essentia_mood", "popularity", "singles"],
    "navidrome_scan":     ["navidrome"],
    "popularity_scan":    ["popularity"],
    "singles_scan":       ["singles"],
    "essentia_mood_scan": ["essentia-mood", "essentia_mood"],
    "mood_scan":          ["mood"],
}


def _get_scan_history_last_active(normalized_scan_type: str) -> "Tuple[Optional[datetime], bool]":
    """Query scan_history for the most recent album-level activity for a scan type.

    Returns a tuple of:
      last_active  – datetime (UTC-naive) of the most recent scan_history entry, or
                     None if the table is empty / unavailable for this scan type.
      had_duplicates – True when the same (artist, album, scan_type) row appears
                     more than once within a 10-second window around the most recent
                     entry, which is the fingerprint of multiple concurrent scan
                     instances (see advisory-lock bug).
    """
    history_types = _HISTORY_TYPES_FOR_SCAN.get(normalized_scan_type, [])
    if not history_types:
        return None, False

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        placeholders = ",".join(["%s"] * len(history_types))

        # Most recent completed album scan for any of the constituent sub-scan types.
        cursor.execute(
            f"SELECT MAX(scan_timestamp) AS latest FROM scan_history "
            f"WHERE scan_type IN ({placeholders}) AND status = 'completed'",
            history_types,
        )
        row = cursor.fetchone()
        latest_raw = row["latest"] if row and isinstance(row, dict) else (row[0] if row else None)
        if latest_raw is None:
            return None, False

        # Normalise to a naive UTC datetime for consistent age arithmetic.
        if hasattr(latest_raw, "tzinfo") and latest_raw.tzinfo is not None:
            from datetime import timezone as _tz
            latest = latest_raw.astimezone(_tz.utc).replace(tzinfo=None)
        else:
            latest = latest_raw

        # Duplicate-instance detection: count distinct (artist, album, scan_type)
        # triplets that appear MORE than once within ±10 seconds of the most recent
        # entry.  This directly fingerprints the multi-worker restart bug.
        cursor.execute(
            f"""
            SELECT COUNT(*) AS dup_groups
            FROM (
                SELECT artist, album, scan_type
                FROM scan_history
                WHERE scan_type IN ({placeholders})
                  AND scan_timestamp >= %s - INTERVAL '10 seconds'
                  AND scan_timestamp <= %s + INTERVAL '10 seconds'
                GROUP BY artist, album, scan_type
                HAVING COUNT(*) > 1
            ) AS duplicates
            """,
            history_types + [latest_raw, latest_raw],
        )
        dup_row = cursor.fetchone()
        dup_count = dup_row["dup_groups"] if dup_row and isinstance(dup_row, dict) else (dup_row[0] if dup_row else 0)
        had_duplicates = int(dup_count or 0) > 0

        return latest, had_duplicates
    except Exception as exc:
        log_debug(f"[RESUME] scan_history query failed for {normalized_scan_type}: {exc}")
        return None, False
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _normalize_scan_type(scan_type: str) -> str:
    normalized = str(scan_type or "navidrome").strip().lower().replace("-", "_")
    alias_map = {
        "navidrome": "navidrome_scan",
        "popularity": "popularity_scan",
        "singles": "singles_scan",
        "combined": "combined_scan",
        "mood": "mood_scan",
        "essentia_mood": "essentia_mood_scan",
    }
    return alias_map.get(normalized, normalized)


def _scan_type_variants(scan_type: str) -> List[str]:
    """Return normalized scan_type aliases used in stored progress/history rows."""
    normalized = _normalize_scan_type(scan_type)
    variants = {normalized}
    alias_map = {
        "navidrome_scan": "navidrome",
        "popularity_scan": "popularity",
        "singles_scan": "singles",
        "combined_scan": "combined",
        "mood_scan": "mood",
        "essentia_mood_scan": "essentia_mood",
    }
    short = alias_map.get(normalized)
    if short:
        variants.add(short)
    return sorted(variants)


def _resolve_progress_file(scan_type: str) -> str:
    normalized = _normalize_scan_type(scan_type)
    if normalized in {"navidrome", "navidrome_scan"}:
        return os.environ.get("NAVIDROME_PROGRESS_FILE", "/database/navidrome_scan_progress.json")
    if normalized in {"popularity", "popularity_scan"}:
        return os.environ.get("POPULARITY_PROGRESS_FILE", "/database/popularity_scan_progress.json")
    if normalized in {"singles", "singles_scan"}:
        return os.environ.get("SINGLES_PROGRESS_FILE", "/database/singles_scan_progress.json")
    if normalized in {"combined", "combined_scan"}:
        return os.environ.get("COMBINED_PROGRESS_FILE", "/database/combined_scan_progress.json")
    if normalized in {"mood", "mood_scan"}:
        return os.environ.get("MOOD_PROGRESS_FILE", "/database/mood_scan_progress.json")
    if normalized in {"essentia_mood", "essentia_mood_scan"}:
        return os.environ.get("ESSENTIA_MOOD_PROGRESS_FILE", "/database/essentia_mood_scan_progress.json")
    return os.environ.get("PROGRESS_FILE", "/database/scan_progress.json")


def _resolve_marker_file(scan_type: str) -> str:
    progress_file = _resolve_progress_file(scan_type)
    marker_name = f"{_normalize_scan_type(scan_type)}.json"
    return os.path.join(os.path.dirname(progress_file), "scan_resume_markers", marker_name)


def _load_scan_marker(scan_type: str) -> Optional[Dict]:
    marker_file = _resolve_marker_file(scan_type)
    if not os.path.exists(marker_file):
        return None

    try:
        with open(marker_file, "r", encoding="utf-8") as f:
            marker = json.load(f)
        return marker if isinstance(marker, dict) else None
    except Exception as e:
        log_debug(f"Failed to load scan marker {marker_file}: {e}")
        return None


def load_scan_progress(scan_type: str = "navidrome") -> Optional[Dict]:
    """
    Load scan progress from file.
    
    Args:
        scan_type: Type of scan ('navidrome', 'popularity', or 'combined')
        
    Returns:
        Progress dict or None if not found
    """
    progress_file = _resolve_progress_file(scan_type)
    progress: Optional[Dict] = None

    if os.path.exists(progress_file):
        try:
            with open(progress_file, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                progress = loaded
                log_debug(f"Loaded progress from {progress_file}: {progress}")
        except Exception as e:
            log_debug(f"Failed to load progress file {progress_file}: {e}")

    marker = _load_scan_marker(scan_type)
    if not progress and not marker:
        log_debug(f"No progress state found for {scan_type}")
        return None

    merged = dict(progress or {})
    if marker:
        # Marker is authoritative for checkpoint metadata.
        for key in ("current_artist", "status", "stop_requested", "is_running", "last_updated"):
            if marker.get(key) is not None:
                merged[key] = marker.get(key)

    if "scan_type" not in merged:
        merged["scan_type"] = _normalize_scan_type(scan_type)
    if "progress_path" not in merged:
        merged["progress_path"] = progress_file

    return merged


def _atomic_json_write(path: str, data: dict, indent: int = None) -> None:
    """Write *data* as JSON to *path* atomically via a temp file + os.replace()."""
    _dir = os.path.dirname(path) or "."
    os.makedirs(_dir, exist_ok=True)
    _fd, _tmp = tempfile.mkstemp(dir=_dir, suffix=".tmp")
    try:
        with os.fdopen(_fd, "w", encoding="utf-8") as _f:
            json.dump(data, _f, indent=indent)
        os.replace(_tmp, path)
    except Exception:
        try:
            os.unlink(_tmp)
        except OSError:
            pass
        raise


def save_scan_progress(scan_type: str, progress_data: Dict) -> bool:
    """
    Save scan progress to file.
    
    Args:
        scan_type: Type of scan ('navidrome', 'popularity', or 'combined')
        progress_data: Progress data to save
        
    Returns:
        True if successful, False otherwise
    """
    progress_file = _resolve_progress_file(scan_type)
    
    try:
        _atomic_json_write(progress_file, progress_data, indent=2)
        log_debug(f"Saved progress to {progress_file}: {progress_data.get('percent_complete', 0)}%")
        return True
    except Exception as e:
        log_debug(f"Failed to save progress file {progress_file}: {e}")
        return False


def clear_scan_progress(scan_type: str = "navidrome") -> bool:
    """
    Clear scan progress file (on completion).
    
    Args:
        scan_type: Type of scan ('navidrome', 'popularity', or 'combined')
        
    Returns:
        True if successful, False otherwise
    """
    progress_file = _resolve_progress_file(scan_type)
    
    try:
        if os.path.exists(progress_file):
            # Update to mark as not running before deleting
            try:
                with open(progress_file, 'r') as f:
                    progress = json.load(f)
                progress['is_running'] = False
                progress['percent_complete'] = 100
                _atomic_json_write(progress_file, progress, indent=2)
            except Exception as e:
                log_debug(f"Failed to update progress file before clearing {progress_file}: {e}")
            
            os.remove(progress_file)
            log_info(f"Cleared progress file: {progress_file}")
            return True
    except Exception as e:
        log_debug(f"Failed to clear progress file {progress_file}: {e}")
        return False


def detect_interrupted_scan(scan_type: str = "navidrome") -> Optional[Dict]:
    """
    Detect if a scan was genuinely interrupted mid-run and can be auto-resumed.

    Checks (in order):
    1. Progress file / marker exists with ``is_running=True``.
    2. A ``current_artist`` checkpoint is present.
    3. The scan was not already complete or in a fresh-start state.
    4. The most recent album work (from scan_history) or progress-file timestamp
       is within the last 30 minutes — indicating the scan was actively running
       when the app stopped, not just idle between perpetual cycles.

    When duplicate concurrent instances are detected in the prior run (same album
    logged more than once within 10 seconds) a warning is emitted so operators
    can correlate it with the advisory-lock / restart behaviour.

    Args:
        scan_type: Logical scan type ('navidrome', 'popularity', 'combined', …)

    Returns:
        Progress dict if a genuine interrupted scan is detected, None otherwise.
    """
    progress = load_scan_progress(scan_type)

    if not progress:
        return None

    current_artist = progress.get("current_artist")
    if not current_artist:
        log_debug("No checkpoint artist in progress/marker state")
        return None

    # Only auto-resume scans that were genuinely running when the app died.
    is_running = bool(progress.get("is_running", False))
    if not is_running:
        log_debug(f"Scan is_running=False; skipping auto-resume (status={progress.get('status')!r})")
        return None

    status = str(progress.get("status") or "").strip().lower()

    if status in {"complete", "completed", "success"}:
        log_debug("Scan is already complete; no resume needed")
        return None

    # A scan in 'starting' state has not yet processed any artists; the resume
    # marker may carry a stale current_artist from the previous run.
    if status == "starting":
        log_debug("Progress file shows scan in 'starting' state; skipping auto-resume")
        return None

    # -----------------------------------------------------------------------
    # Age gate: use scan_history timestamps (actual album-level work) as the
    # authoritative source for "when did this scan last do something".
    # This is more reliable than progress.last_updated because the progress file
    # is written during idle/cycle-pause periods as well as active scanning.
    # Fall back to the progress file timestamp if the DB is unavailable.
    # -----------------------------------------------------------------------
    normalized = _normalize_scan_type(scan_type)
    last_active, had_duplicates = _get_scan_history_last_active(normalized)

    if had_duplicates:
        log_info(
            f"[RESUME] Prior {scan_type} run had duplicate concurrent instances "
            "(same album logged multiple times within 10 s).  This is caused by "
            "multiple workers starting the scan simultaneously."
        )

    if last_active is None:
        # DB unavailable — fall back to progress file timestamp.
        try:
            raw = progress.get("last_updated", "")
            if raw:
                last_active = datetime.fromisoformat(raw)
        except Exception as _ts_err:
            log_debug(f"[RESUME] Could not parse progress last_updated: {_ts_err}")

    if last_active is None:
        log_debug("[RESUME] Could not determine last active time; skipping auto-resume")
        return None

    age = datetime.utcnow() - last_active
    if age > timedelta(minutes=30):
        log_debug(f"[RESUME] Last scan activity was {age} ago (> 30 min); skipping auto-resume")
        return None

    log_info(
        f"Detected interrupted {scan_type} scan at {progress.get('percent_complete', 0)}% "
        f"(last active {int(age.total_seconds() // 60)} min ago, artist: {current_artist!r})"
    )

    return progress


def get_artists_to_scan(all_artists: List[str], resume_from: Optional[str] = None) -> List[str]:
    """
    Get list of artists to scan, optionally resuming from a specific artist.
    
    Args:
        all_artists: Complete list of artists
        resume_from: Artist name to resume from (exclusive - this artist was already scanned)
        
    Returns:
        List of artists to scan
    """
    if not resume_from:
        return all_artists
    
    # Find the index of the resume artist
    try:
        resume_index = all_artists.index(resume_from)
        # Start from the next artist (resume_from was already scanned)
        artists_to_scan = all_artists[resume_index + 1:]
        log_info(f"Resuming scan from artist index {resume_index + 1}/{len(all_artists)}")
        log_info(f"Skipping {resume_index + 1} already scanned artists")
        return artists_to_scan
    except ValueError:
        log_debug(f"Resume artist '{resume_from}' not found in artist list, starting from beginning")
        return all_artists


def get_last_scanned_artist_from_db(
    db_path: str = "/database/sptnr.db",
    scan_type: str = "navidrome",
) -> Optional[str]:
    """
    Get the last scanned artist that NEEDS TO BE RESUMED (not completed).
    
    Priority:
    1. Check scan_history for incomplete scans for the requested scan type
    2. Fall back to most recently scanned artist
    
    Args:
        db_path: Path to database
        
    Returns:
        Artist name or None
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # First, try to find an artist with an incomplete scan in scan_history,
        # scoped to the active scan type. Without this filter, an interrupted
        # unrelated scan can force Essentia/Navidrome to resume from the wrong artist.
        scan_type_values = _scan_type_variants(scan_type)
        placeholders = ", ".join(["%s"] * len(scan_type_values))
        cursor.execute(
            f"""
            SELECT artist
            FROM scan_history
            WHERE status != 'completed'
              AND LOWER(COALESCE(scan_type, '')) IN ({placeholders})
            ORDER BY scan_timestamp DESC
            LIMIT 1
            """,
            tuple(v.lower() for v in scan_type_values),
        )
        
        row = cursor.fetchone()
        if row:
            artist = row['artist'] if isinstance(row, dict) else row[0]
            log_debug(f"Found incomplete {scan_type} scan for artist: {artist}")
            conn.close()
            return artist
        
        # Fall back to the most recently scanned track's artist
        # (for cases where scan_history doesn't have data)
        cursor.execute("""
            SELECT artist, MAX(last_scanned) as latest
            FROM tracks
            WHERE last_scanned IS NOT NULL
            GROUP BY artist
            ORDER BY latest DESC
            LIMIT 1
        """)
        
        row = cursor.fetchone()
        conn.close()
        
        if row:
            artist = row['artist'] if isinstance(row, dict) else row[0]
            latest = row['latest'] if isinstance(row, dict) else row[1]
            log_debug(f"Last scanned artist from DB (fallback): {artist} at {latest}")
            return artist
        
    except Exception as e:
        log_debug(f"Failed to get last scanned artist from DB ({scan_type}): {e}")
    
    return None


def should_resume_scan(scan_type: str = "navidrome") -> Tuple[bool, Optional[str]]:
    """
    Check if scan should be resumed.
    
    Args:
        scan_type: Type of scan ('navidrome', 'popularity', or 'combined')
    
    Returns:
        Tuple of (should_resume: bool, resume_from_artist: Optional[str])
    """
    progress = detect_interrupted_scan(scan_type)
    
    if not progress:
        return False, None
    
    current_artist = progress.get('current_artist')
    
    if not current_artist:
        log_debug("No current artist in progress file")
        return False, None
    
    log_unified(f"Auto-Resume: Resuming {scan_type} scan from {current_artist}")
    log_info(f"Resuming {scan_type} scan from artist: {current_artist}")
    
    return True, current_artist


def get_last_scanned_artist(scan_type: str = "navidrome", db_path: str = "/database/sptnr.db") -> Optional[str]:
    """
    Get the last scanned artist that needs to be RESUMED (incomplete scans only).
    
    Strategy:
    1. First check progress file for interrupted scan (current_artist in progress)
    2. Then check scan_history for incomplete scans (artist with status != 'completed')
    3. Fall back to most recently scanned artist from database
    
    Args:
        scan_type: Type of scan ('navidrome', 'popularity', or 'combined')
        db_path: Path to database
        
    Returns:
        Artist name or None
    """
    # First check for a genuinely interrupted scan state.
    interrupted = detect_interrupted_scan(scan_type)
    if interrupted and interrupted.get('current_artist'):
        artist = interrupted.get('current_artist')
        log_info(f"Last scanned artist from interrupted progress: {artist}")
        return artist
    
    # Fall back to database query - check for incomplete scans
    artist = get_last_scanned_artist_from_db(db_path, scan_type=scan_type)
    if artist:
        log_info(f"Last scanned artist from database: {artist}")
        return artist
    
    log_debug(f"No last scanned artist found for {scan_type}")
    return None


def mark_scan_completed(scan_type: str = "navidrome") -> bool:
    """
    Mark scan as completed and clear progress.
    
    Args:
        scan_type: Type of scan ('navidrome', 'popularity', or 'combined')
        
    Returns:
        True if successful
    """
    log_info(f"Marking {scan_type} scan as completed")
    return clear_scan_progress(scan_type)
