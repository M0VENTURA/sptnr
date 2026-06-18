#!/usr/bin/env python3
"""Library sync / diff worker.

Implements single-flight library syncing with Navidrome via the Subsonic/OpenSubsonic
REST API.  Coalesces concurrent requests, gates on Navidrome scan status, and delegates
to the existing ``scan_artist_to_db`` flow with aggressive early-exit gates.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Dict, Set, Tuple

import time
start_time = time.time()

from helpers.logging_config import log_debug, log_unified, log_error
from api_clients.navidrome import NavidromeClient
from helpers.db_utils import get_db_connection, bulk_upsert_navidrome_tracks

# ---------------------------------------------------------------------------
# Internal worker state (single-flight)
# ---------------------------------------------------------------------------
_library_sync_lock = threading.Lock()
_library_sync_state: dict = {"running": False, "pending": False}
_last_processed_scan_marker: int | None = None

def _sync_artist_with_diff(artist_name: str, artist_id: str) -> dict:
    """Run ``scan_artist_to_db`` with diff-mode gates enabled.

    Returns a guaranteed schema dict with keys:
        - skipped_mtime      (bool)
        - skipped_album_diff (bool)
        - changed            (bool)
        - changed_albums     (int)
        - tracks             (list)
    """
    # Leave here ONLY if you have an unresolvable circular import
    from helpers.scan_helpers import scan_artist_to_db 

    log_debug(
        "[LIBRARY_SYNC] Diff-scan artist='%s' id=%s", artist_name, artist_id
    )

    result = scan_artist_to_db(
        artist_name,
        artist_id,
        verbose=False,
        force=False,
        diff_mode=True,
    )

    # Explicitly handle the 'Full Scan' state
    if result is None:
        return {
            "skipped_mtime": False,
            "skipped_album_diff": False,
            "changed": True,
            "changed_albums": 0,  
            "tracks": []  # Ensures our bulk updater doesn't crash
        }

    # Ensure a consistent schema is ALWAYS passed back to the caller
    return {
        "skipped_mtime": result.get("skipped_mtime", False),
        "skipped_album_diff": result.get("skipped_album_diff", False),
        "changed": result.get("changed", False),
        "changed_albums": result.get("changed_albums", 0),
        "tracks": result.get("tracks", [])
    }

def request_library_sync() -> dict:
    """Request a library diff/sync.

    If a sync is already running the request is coalesced (pending flag set).
    When the running sync finishes it will run exactly ONE follow-up sync if
    pending is true.  This never blocks the caller.
    """
    global _library_sync_state
    with _library_sync_lock:
        if _library_sync_state["running"]:
            if not _library_sync_state["pending"]:
                logging.debug("[LIBRARY_SYNC] Sync already running; coalescing request")
                _library_sync_state["pending"] = True
            else:
                logging.debug(
                    "[LIBRARY_SYNC] Sync already running with pending follow-up; "
                    "ignoring duplicate request"
                )
            return {"coalesced": True, "running": True}
        _library_sync_state["running"] = True
        _library_sync_state["pending"] = False

    logging.debug("[LIBRARY_SYNC] Library sync requested; starting background worker")
    thread = threading.Thread(
        target=_run_library_sync, daemon=True, name="library-sync-worker"
    )
    thread.start()
    return {"started": True}


def _run_library_sync() -> None:
    """Worker wrapper that handles the pending follow-up logic."""
    global _library_sync_state
    try:
        logging.debug("[LIBRARY_SYNC] Library sync worker started")
        _perform_library_sync()
        logging.debug("[LIBRARY_SYNC] Library sync worker finished")
    except Exception as exc:
        logging.error("[LIBRARY_SYNC] Unexpected error during sync: %s", exc, exc_info=True)
    finally:
        with _library_sync_lock:
            _library_sync_state["running"] = False
            pending = _library_sync_state["pending"]
            _library_sync_state["pending"] = False

        if pending:
            logging.debug("[LIBRARY_SYNC] Pending follow-up sync detected; scheduling one more")
            request_library_sync()

        
def _get_navidrome_config() -> dict | None:
    """Return the first usable Navidrome user config dict."""
    try:
        from helpers.config_loader import load_config

        cfg = load_config() or {}
        nav_users = cfg.get("navidrome_users", []) or []
        if not nav_users:
            nav_cfg = cfg.get("navidrome", {}) or {}
            if nav_cfg.get("base_url"):
                nav_users = [nav_cfg]
        if nav_users:
            return {
                "base_url": nav_users[0].get("base_url", ""),
                "user": nav_users[0].get("user", nav_users[0].get("username", "")),
                "pass": nav_users[0].get("pass", nav_users[0].get("password", "")),
            }
    except Exception as exc:
        logging.debug("[LIBRARY_SYNC] Could not load Navidrome config: %s", exc)
    return None


def _perform_library_sync() -> None:
    """
    Core sync logic updated for high-speed bulk PostgreSQL operations.
    """
    global _last_processed_scan_marker

    cfg = _get_navidrome_config()
    if not cfg:
        log_debug("[LIBRARY_SYNC] No Navidrome config available; skipping sync")
        return

    client = NavidromeClient(
        base_url=cfg["base_url"], username=cfg["user"], password=cfg["pass"]
    )

    # 1. & 2. Scan-status and Marker check
    scan_status = client.get_scan_status()

    if not scan_status.get("success"):
        log_debug("[LIBRARY_SYNC] Scan status request failed — skipping sync")
        return

    if scan_status.get("scanning"):
        log_debug("[LIBRARY_SYNC] Navidrome is still scanning — skipping sync")
        return

    marker = scan_status.get("count")
    if marker is not None and marker == _last_processed_scan_marker:
        return

    candidate_artists = _get_candidate_artists(client)
    if not candidate_artists:
        return

    # 3. Optimized Bulk Sync Loop
    log_unified(f"[LIBRARY_SYNC] 🚀 Starting bulk diff-sync for {len(candidate_artists)} artists...")
    
    BATCH_SIZE = 1000
    all_tracks_to_upsert = []
    seen_track_ids = set()
    
    for artist_name, artist_id in candidate_artists.items():
        if not artist_id:
            continue
        try:
            result = _sync_artist_with_diff(artist_name, artist_id)
            
            if isinstance(result, dict) and 'tracks' in result:
                for track in result['tracks']:
                    track_id = track.get("id")

                    if track_id and track_id in seen_track_ids:
                        continue

                    if track_id:
                        seen_track_ids.add(track_id)

                    all_tracks_to_upsert.append(track)
            
            # Commit every 5000 tracks
            if len(all_tracks_to_upsert) >= BATCH_SIZE:
                _run_bulk_commit(all_tracks_to_upsert)
                all_tracks_to_upsert.clear()
                
        except Exception as exc:
            log_error(f"[LIBRARY_SYNC] Artist sync failed for '{artist_name}': {exc}")

    # Final bulk commit
    if all_tracks_to_upsert:
        log_unified(f"[LIBRARY_SYNC] 💾 Final commit of {len(all_tracks_to_upsert)} tracks...")
    _run_bulk_commit(all_tracks_to_upsert)

    _last_processed_scan_marker = marker
    duration = time.time() - start_time
    log_unified(f"[LIBRARY_SYNC] ✅ Bulk sync complete in {duration:.2f}s") 

def _run_bulk_commit(tracks):
    """Utility to perform bulk upsert into PostgreSQL."""
    from helpers.db_utils import get_db_connection, bulk_upsert_navidrome_tracks
    conn = get_db_connection()
    try:
        log_unified(f"[LIBRARY_SYNC] 💾 Committing batch of {len(tracks)} tracks to database...")
        bulk_upsert_navidrome_tracks(conn, tracks)
    except Exception as e:
        log_error(f"[LIBRARY_SYNC] Bulk commit failed: {e}")
        conn.rollback()
    finally:
        conn.close()


def _get_candidate_artists(client: NavidromeClient) -> Dict[str, str]:
    """Return a mapping of album-artist name → Navidrome artist ID.

    Prefers the existing local DB artist list (cheap) and resolves IDs via
    ``build_artist_index``.  Falls back to the full Navidrome index only when
    the DB has no artists yet.
    """
    conn = None
    db_artists: dict[str, str | None] = {}
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT DISTINCT COALESCE(NULLIF(album_artist, ''), artist) AS aa
            FROM tracks
            WHERE album_artist IS NOT NULL AND TRIM(album_artist) <> ''
               OR artist IS NOT NULL AND TRIM(artist) <> ''
            """
        )
        for row in cursor.fetchall():
            name = row[0] if row else None
            if name:
                db_artists[name] = None
    except Exception as exc:
        logging.debug("[LIBRARY_SYNC] Could not query DB artists: %s", exc)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    # If we have DB artists, try to resolve their Navidrome IDs.
    if db_artists:
        try:
            index = client.build_artist_index()
            
            # 🚀 NEW: Build an O(1) case-insensitive lookup map ONCE before the loop
            case_insensitive_index = {
                idx_name.lower(): idx_info.get("id") 
                for idx_name, idx_info in index.items() 
                if idx_info.get("id")
            }
            
            for name in list(db_artists.keys()):
                info = index.get(name)
                if info and info.get("id"):
                    db_artists[name] = info["id"]
                else:
                    # 🚀 NEW: Case-insensitive fallback is now a direct dictionary lookup!
                    fallback_id = case_insensitive_index.get(name.lower())
                    if fallback_id:
                        db_artists[name] = fallback_id
                        
            resolved = {k: v for k, v in db_artists.items() if v}
            if resolved:
                logging.debug(
                    "[LIBRARY_SYNC] Using %d DB artists as candidates", len(resolved)
                )
                return resolved
        except Exception as exc:
            logging.debug("[LIBRARY_SYNC] Failed to resolve DB artist IDs: %s", exc)

    # Fallback: full Navidrome artist index (fresh library / empty DB).
    try:
        index = client.build_artist_index()
        fallback = {
            name: info["id"]
            for name, info in index.items()
            if info.get("id")
        }
        logging.debug(
            "[LIBRARY_SYNC] Fallback to Navidrome index: %d artists", len(fallback)
        )
        return fallback
    except Exception as exc:
        logging.debug("[LIBRARY_SYNC] Failed to fetch artist index: %s", exc)
        return {}



