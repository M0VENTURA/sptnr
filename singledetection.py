#!/usr/bin/env python3
"""
Single Detection Scanner - Detects which tracks are singles vs album tracks.
Uses Discogs, Last.fm, MusicBrainz and other sources to determine if a track is a single.
"""

import os
import sqlite3
import logging
from datetime import datetime
import sys
import re
import time
import threading
import difflib
from concurrent.futures import ThreadPoolExecutor

# Setup logging
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/config/singledetection.log"),
        logging.StreamHandler()
    ]
)

DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")

# Import from start.py
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from start import (
    config,
    discogs_client,
    musicbrainz_client,
    DISCOGS_TOKEN,
    CONTEXT_GATE,
    CONTEXT_FALLBACK_STUDIO,
    _DEF_USER_AGENT,
    strip_parentheses,
    create_retry_session,
)

def get_db_connection():
    """Get database connection with WAL mode"""
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


# ============ DISCOGS SESSION & THROTTLING ============

_discogs_session = None
_discogs_lock = threading.Lock()

def _get_discogs_session():
    """Return a shared requests.Session with sensible retries and backoff."""
    global _discogs_session
    with _discogs_lock:
        if _discogs_session is None:
            _discogs_session = create_retry_session(user_agent=_DEF_USER_AGENT, retries=5, backoff=1.2)
        return _discogs_session

# --- Simple RPM throttle (authenticated limit is generous, but be safe) ---
_last_call_ts = 0.0
_min_interval_sec = float(config.get("features", {}).get("discogs_min_interval_sec", 0.35))
# 0.35s ~ 171 req/min theoretical max; adjust to 1.0s if you still see 429s
_discogs_throttle_lock = threading.Lock()

def _throttle_discogs():
    """Sleep briefly between Discogs calls to avoid 429s (thread-safe)."""
    global _last_call_ts
    now = time.time()
    with _discogs_throttle_lock:
        wait = _min_interval_sec - (now - _last_call_ts)
        if wait > 0:
            time.sleep(wait)
        _last_call_ts = time.time()

def _respect_retry_after(resp):
    """If Discogs returns Retry-After, sleep that amount."""
    ra = resp.headers.get("Retry-After")
    if ra:
        try:
            delay = float(ra)
            time.sleep(min(delay, 10.0))  # cap at 10s per call
        except Exception:
            pass


# ============ DISCOGS HELPERS ============

def _discogs_search(session, headers, q, kind: str = "release", per_page: int = 10, timeout: int = 10) -> list:
    """Perform a Discogs database search with throttle and Retry-After handling."""
    try:
        _throttle_discogs()
        resp = session.get(
            "https://api.discogs.com/database/search",
            headers=headers,
            params={"q": q, "type": kind, "per_page": per_page},
            timeout=timeout,
        )
        if resp.status_code == 429:
            _respect_retry_after(resp)
        resp.raise_for_status()
        return resp.json().get("results", []) or []
    except Exception as e:
        logging.debug(f"Discogs {kind} search failed for '{q}': {e}")
        return []


def _canon(s: str) -> str:
    """Canonicalize string: lowercase, strip, collapse whitespace."""
    return " ".join((s or "").lower().split())


def _strip_video_noise(s: str) -> str:
    """
    Remove common boilerplate to improve title matching:
      - 'official music video', 'official video', 'music video', 'hd', '4k', 'uhd', 'remastered'
      - bracketed content [..], (..), {..}
      - normalize 'feat.' / 'ft.' to 'feat '
    Returns a canonicalized string via _canon.
    """
    s = (s or "").lower()
    noise_phrases = [
        "official music video", "official video", "music video",
        "hd", "4k", "uhd", "remastered", "lyrics", "lyric video",
        "audio", "visualizer"
    ]
    for p in noise_phrases:
        s = s.replace(p, " ")
    # Drop bracketed content
    s = re.sub(r"\[.*?\]|\(.*?\)|\{.*?\}", " ", s)
    # Normalize common abbreviations
    s = s.replace("feat.", "feat ").replace("ft.", "feat ")
    return _canon(s)


def _release_context_compatible_discogs(rel_json: dict, require_live: bool, forbid_live: bool) -> bool:
    """Decide if a Discogs release is compatible with album context (live/unplugged)."""
    title_l = (rel_json.get("title") or "").lower()
    notes_l = (rel_json.get("notes") or "").lower()
    formats = rel_json.get("formats") or []
    tags = {d.lower() for f in formats for d in (f.get("descriptions") or [])}

    has_live_signal = (
        ("live" in tags) or ("unplugged" in title_l) or ("mtv unplugged" in title_l) or
        ("recorded live" in notes_l) or ("unplugged" in notes_l)
    )

    if require_live and not has_live_signal:
        return False
    if forbid_live and has_live_signal:
        return False
    return True


def _banned_flavor(vt_raw: str, vd_raw: str, *, allow_live: bool = False) -> bool:
    """
    Reject 'live' and 'remix' unless allow_live=True.
    Radio edits are allowed (without 'remix').
    """
    t = (vt_raw or "").lower()
    d = (vd_raw or "").lower()

    # Live only banned when album context doesn't allow it
    if (not allow_live) and ("live" in t or "live" in d):
        return True

    # 'remix' anywhere is banned; radio edits allowed if no 'remix'
    if "remix" in t or "remix" in d:
        return True

    return False


# ============ ALBUM CONTEXT & SINGLE DETECTION ============

def infer_album_context(album_title: str, release_types: list[str] | None = None) -> dict:
    """
    Infer album context flags (live/unplugged) from album title and optional release_types.
    - release_types can be Discogs-like list: ["Album", "Live"]
    """
    t = (album_title or "").lower()
    types_norm = [(rt or "").lower() for rt in (release_types or [])]
    is_unplugged = ("unplugged" in t) or ("mtv unplugged" in t)
    is_live = is_unplugged or ("live" in t) or ("live" in types_norm)
    return {
        "is_live": is_live,
        "is_unplugged": is_unplugged,
        "title": album_title,
        "raw_types": types_norm,
    }


def _has_official_on_release_top(data: dict, nav_title: str, *, allow_live: bool, min_ratio: float = 0.50) -> bool:
    """
    Compatibility helper: Check if data contains an official release matching nav_title.
    Delegate to discogs_official_video_signal if Discogs token available.
    Returns True if official match found, False otherwise.
    """
    if not data:
        return False
    
    # If Discogs token available, use the full video signal detection
    if DISCOGS_TOKEN:
        result = discogs_official_video_signal(
            title=nav_title,
            artist=data.get("artist", ""),
            discogs_token=DISCOGS_TOKEN,
            allow_lyric_as_official=True,
            album_context={"is_live": allow_live} if allow_live else None,
            permissive_fallback=True
        )
        return result.get("match", False)
    
    return False


# --- Discogs "Official Video / Official Lyric Video" signal (with cache) ---
_DISCOGS_VID_CACHE: dict[tuple[str, str, str], dict] = {}

def _has_official(vt_raw: str, vd_raw: str, allow_lyric: bool = True) -> bool:
    """Require 'official' in title/description; optionally accept 'lyric' as official."""
    t = (vt_raw or "").lower()
    d = (vd_raw or "").lower()
    if ("official" in t) or ("official" in d):
        return True
    return allow_lyric and (("lyric" in t) or ("lyric" in d))


def discogs_official_video_signal(
    title: str,
    artist: str,
    *,
    discogs_token: str,
    timeout: int = 10,
    per_page: int = 10,
    min_ratio: float = 0.55,
    allow_lyric_as_official: bool = True,
    album_context: dict | None = None,
    permissive_fallback: bool = False,
) -> dict:
    """
    Detect an 'official' (or 'lyric' if allowed) video for a track on Discogs,
    honoring album context (live/unplugged), with:
      - Candidate shortlist (title similarity + 'Single' OR 'Album' hint),
      - Parallel inspections (bounded executor),
      - Early bailouts and caching.
    Returns (on success):
      {"match": True, "uri": <video_url>, "release_id": <id>, "ratio": <float>, "why": "discogs_official_video"}
    """
    # ---- Basic token check ---------------------------------------------------
    if not discogs_token:
        return {"match": False, "uri": None, "release_id": None, "ratio": None, "why": "no_token"}
    # ---- Context gate --------------------------------------------------------
    allow_live_ctx = bool(album_context and (album_context.get("is_live") or album_context.get("is_unplugged")))
    context_key = "live" if allow_live_ctx else "studio"
    cache_key = (_canon(artist), _canon(title), context_key)
    # Fast cache path
    cached = _DISCOGS_VID_CACHE.get(cache_key)
    if cached is not None:
        return cached
    # ---- Setup ---------------------------------------------------------------
    session = _get_discogs_session()
    headers = {"Authorization": f"Discogs token={discogs_token}", "User-Agent": _DEF_USER_AGENT}
    nav_title_raw = strip_parentheses(title)
    nav_title_clean = _strip_video_noise(nav_title_raw)
    nav_title = _canon(nav_title_raw)  # canonical for shortlist similarity
    # Context rules
    require_live = allow_live_ctx and CONTEXT_GATE
    forbid_live  = (not allow_live_ctx) and CONTEXT_GATE
    allow_live_for_video = allow_live_ctx  # allow 'live' in video title only if album context is live

    # ---- Release inspection helper -------------------------------------------
    def _inspect_release(rel_id: int, *, require_live: bool, forbid_live: bool, allow_live_for_video: bool) -> dict | None:
        """
        Pull the release, apply context compatibility, then scan videos:
          - Require 'official' (or 'lyric' if allowed),
          - Ban 'remix' always; ban 'live' unless album context allows it,
          - Title/description similarity >= min_ratio against cleaned nav title.
        """
        try:
            _throttle_discogs()
            r = session.get(f"https://api.discogs.com/releases/{rel_id}", headers=headers, timeout=timeout)
            if r.status_code == 429:
                _respect_retry_after(r)
            r.raise_for_status()
            data = r.json()
        except Exception:
            return None
        if not _release_context_compatible_discogs(data, require_live, forbid_live):
            return None
        best = None
        for v in (data.get("videos") or []):
            vt_raw = v.get("title", "") or ""
            vd_raw = v.get("description", "") or ""

            # official/lyric requirement and similarity check
            t_l = vt_raw.lower(); d_l = vd_raw.lower()
            if ("official" not in t_l and "official" not in d_l):
                if not allow_lyric_as_official or ("lyric" not in t_l and "lyric" not in d_l):
                    continue
            if _banned_flavor(vt_raw, vd_raw, allow_live=allow_live_for_video):
                continue
            vt_clean = _strip_video_noise(vt_raw)
            vd_clean = _strip_video_noise(vd_raw)
            ratio = max(
                difflib.SequenceMatcher(None, vt_clean, nav_title_clean).ratio(),
                difflib.SequenceMatcher(None, vd_clean, nav_title_clean).ratio(),
            )
            if ratio >= min_ratio:
                current = {
                    "match": True,
                    "uri": v.get("uri"),
                    "release_id": rel_id,
                    "ratio": round(ratio, 3),
                    "why": "discogs_official_video",
                }
                if best is None or current["ratio"] > best["ratio"]:
                    best = current

        return best
    # ---- Release search & shortlist --------------------------------
    results = _discogs_search(session, headers, f"{artist} {title}", kind="release", per_page=per_page, timeout=timeout)
    if not results:
        res = {"match": False, "uri": None, "release_id": None, "ratio": None, "why": "no_video_match"}
        _DISCOGS_VID_CACHE[cache_key] = res
        return res
    cands: list[tuple[int, bool, float]] = []
    for r in results[:15]:  # inspect a few more results; still bounded
        rid = r.get("id")
        if not rid:
            continue
        rel_title = _canon(r.get("title", ""))
        title_ratio = difflib.SequenceMatcher(None, rel_title, nav_title).ratio()
        # 'format' hint list may include entries like ['CD', 'Album'] or ['VHS', 'Promo']
        formats_hint = r.get("format", []) or []
        fmt_norm = [(fmt or "").lower() for fmt in formats_hint]
        prefer_single  = any("single" in f for f in fmt_norm)
        is_album_like  = any("album" in f for f in fmt_norm)
        # Keep candidate if:
        #  - it's an obvious SINGLE, OR
        #  - release title is reasonably similar to the track title, OR
        #  - it's an ALBUM (many official videos live on the album's Discogs page)
        keep = prefer_single or (title_ratio >= 0.65) or is_album_like
        if keep:
            cands.append((rid, prefer_single, title_ratio))
    # Prefer singles; otherwise title similarity. Cap to 8 to keep requests sane.
    cands = sorted(cands, key=lambda x: (not x[1], -x[2]))[:8]
    # ---- Parallel inspections of shortlist ----------------------------------
    best = None
    if cands:
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [
                pool.submit(
                    _inspect_release,
                    rid,
                    require_live=require_live,
                    forbid_live=forbid_live,
                    allow_live_for_video=allow_live_for_video,
                )
                for rid, _, _ in cands
            ]
            for f in futures:
                hit = f.result()
                if hit and (best is None or hit["ratio"] > best["ratio"]):
                    best = hit
                    # Cancel remaining inspections to save time
                    for other in futures:
                        if other is not f:
                            try:
                                other.cancel()
                            except:
                                pass
                    break
    if best:
        _DISCOGS_VID_CACHE[cache_key] = best
        return best
    # ---- Optional permissive fallback (studio allowed if album is live) -----
    if allow_live_ctx and (permissive_fallback or CONTEXT_FALLBACK_STUDIO):
        relaxed_best = None
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    _inspect_release,
                    rid,
                    require_live=False,
                    forbid_live=False,
                    allow_live_for_video=False,
                )
                for rid, _, _ in cands
            ]
            for f in futures:
                hit = f.result()
                if hit and (relaxed_best is None or hit["ratio"] > relaxed_best["ratio"]):
                    relaxed_best = hit
                    for other in futures:
                        if other is not f:
                            try:
                                other.cancel()
                            except:
                                pass
                    break
        if relaxed_best:
            _DISCOGS_VID_CACHE[cache_key] = relaxed_best
            return relaxed_best
    # ---- No match -----------------------------------------------------------
    res = {"match": False, "uri": None, "release_id": None, "ratio": None, "why": "no_video_match"}
    _DISCOGS_VID_CACHE[cache_key] = res
    return res


# Cache for single detection
_DISCOGS_SINGLE_CACHE: dict[tuple[str, str, str], bool] = {}

def is_discogs_single(
    title: str,
    artist: str,
    *,
    album_context: dict | None = None,
    timeout: int = 10
) -> bool:
    """Check if track is a single via Discogs (wrapper using DiscogsClient)."""
    return discogs_client.is_single(title, artist, album_context, timeout)

def is_lastfm_single(title: str, artist: str) -> bool:
    """Placeholder for Last.fm single detection."""
    return False

def is_musicbrainz_single(title: str, artist: str) -> bool:
    """Check if track is a single via MusicBrainz (wrapper using MusicBrainzClient)."""
    return musicbrainz_client.is_single(title, artist)


def secondary_single_lookup(track: dict, artist_name: str, album_ctx: dict | None, *, singles_set: set | None = None, required_strong_sources: int = 2) -> dict:
    """Perform a lightweight secondary check for single evidence.

    Returns a dict: {"sources": [...], "confidence": "low|medium|high"}.
    This aggregates Discogs single/video, MusicBrainz, Last.fm, and Spotify prefetch signals.
    """
    sources = set()
    title = track.get("title", "")
    try:
        # Discogs single
        try:
            if DISCOGS_TOKEN and is_discogs_single(title, artist=artist_name, album_context=album_ctx):
                sources.add("discogs")
        except Exception:
            pass

        # Discogs official video
        try:
            if DISCOGS_TOKEN:
                dv = discogs_official_video_signal(title, artist_name, discogs_token=DISCOGS_TOKEN, album_context=album_ctx, permissive_fallback=CONTEXT_FALLBACK_STUDIO)
                if dv.get("match"):
                    sources.add("discogs_video")
        except Exception:
            pass

        # MusicBrainz
        try:
            if is_musicbrainz_single(title, artist_name):
                sources.add("musicbrainz")
        except Exception:
            pass

        # Last.fm (configurable)
        try:
            if config.get("features", {}).get("use_lastfm_single", True) and is_lastfm_single(title, artist_name):
                sources.add("lastfm")
        except Exception:
            pass

        # Spotify prefetch
        try:
            spid = track.get("spotify_id")
            if singles_set and spid and spid in singles_set:
                sources.add("spotify")
        except Exception:
            pass

    except Exception:
        return {"sources": [], "confidence": "low"}

    strong_sources = {"discogs", "discogs_video", "musicbrainz"}
    strong_count = len(sources & strong_sources)
    if strong_count >= required_strong_sources:
        confidence = "high"
    elif len(sources) >= 2:
        confidence = "medium"
    else:
        confidence = "low"

    return {"sources": sorted(sources), "confidence": confidence}


def single_detection_scan(verbose: bool = False):
    """Detect which tracks are singles"""
    logging.info("=" * 60)
    logging.info("Single Detection Scanner Started")
    logging.info("=" * 60)
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get all tracks
        cursor.execute("""
            SELECT id, artist, title, album
            FROM tracks
            ORDER BY artist, title
        """)
        
        tracks = cursor.fetchall()
        logging.info(f"Found {len(tracks)} tracks to scan for single detection")
        
        scanned_count = 0
        
        for track in tracks:
            track_id = track["id"]
            artist = track["artist"]
            title = track["title"]
            album = track["album"]
            
            if verbose:
                logging.info(f"Checking: {artist} - {title}")
            
            is_single = False
            single_source = None
            confidence = "low"
            
            # Try Discogs first
            try:
                if is_discogs_single(title, artist, album_context=infer_album_context(album)):
                    is_single = True
                    single_source = "discogs"
                    confidence = "high"
                    if verbose:
                        logging.debug(f"  -> Single (Discogs)")
            except Exception as e:
                if verbose:
                    logging.debug(f"Discogs check failed: {e}")
            
            # Try Last.fm if not already marked as single
            if not is_single:
                try:
                    if is_lastfm_single(title, artist):
                        is_single = True
                        single_source = "lastfm"
                        confidence = "medium"
                        if verbose:
                            logging.debug(f"  -> Single (Last.fm)")
                except Exception as e:
                    if verbose:
                        logging.debug(f"Last.fm check failed: {e}")
            
            # Try MusicBrainz if not already marked as single
            if not is_single:
                try:
                    if is_musicbrainz_single(title, artist):
                        is_single = True
                        single_source = "musicbrainz"
                        confidence = "medium"
                        if verbose:
                            logging.debug(f"  -> Single (MusicBrainz)")
                except Exception as e:
                    if verbose:
                        logging.debug(f"MusicBrainz check failed: {e}")
            
            # Try secondary lookup for additional validation
            if is_single or not is_single:
                try:
                    secondary = secondary_single_lookup(
                        {"title": title, "artist": artist},
                        artist,
                        infer_album_context(album) if album else None
                    )
                    if secondary.get("is_single"):
                        is_single = True
                        single_source = secondary.get("source", "secondary")
                        confidence = "high"
                except Exception as e:
                    if verbose:
                        logging.debug(f"Secondary lookup failed: {e}")
            
            # Update database with retry logic
            for retry in range(3):
                try:
                    cursor.execute(
                        """UPDATE tracks 
                           SET is_single = ?, single_source = ?, single_confidence = ?
                           WHERE id = ?""",
                        (1 if is_single else 0, single_source or "none", confidence, track_id)
                    )
                    break
                except sqlite3.OperationalError as e:
                    if "locked" in str(e) and retry < 2:
                        logging.debug(f"Database locked during update, retrying ({retry + 1}/3)...")
                        time.sleep(0.5 * (retry + 1))
                        continue
                    raise
            scanned_count += 1
        
        conn.commit()
        conn.close()
        
        logging.info(f"✅ Single detection scan completed: {scanned_count} tracks scanned")
        
    except Exception as e:
        logging.error(f"❌ Single detection scan failed: {str(e)}")
        raise
    
    finally:
        logging.info("=" * 60)


def rate_track_single_detection(
    track: dict,
    artist_name: str,
    album_ctx: dict,
    config: dict,
    title_sim_threshold: float = 0.92,
    count_short_release_as_match: bool = False,
    use_lastfm_single: bool = True,
    verbose: bool = False
) -> dict:
    """
    Perform single detection on a track and update its fields with single status, sources, and star assignment.
    
    This is extracted from rate_artist() to keep single detection logic centralized in singledetection.py.
    
    Returns the updated track dict with:
    - is_single (bool)
    - single_sources (list)
    - single_confidence (str): 'high', 'medium', 'low'
    - stars (int): 5 for confirmed singles, 2 for single hints, 1 default
    - Audit fields: is_canonical_title, title_similarity_to_base, discogs_single_confirmed, discogs_video_found, album_context_live
    """
    from start import (
        _base_title,
        _has_subtitle_variant,
        _similar,
        is_valid_version,
        DISCOGS_ENABLED,
        DISCOGS_TOKEN,
        MUSICBRAINZ_ENABLED,
        CONTEXT_FALLBACK_STUDIO,
    )
    
    title = track.get("title", "")
    canonical_base = _base_title(title)
    sim_to_base = _similar(title, canonical_base)
    has_subtitle = _has_subtitle_variant(title)
    
    if verbose:
        logging.info(f"🎵 Checking: {title}")
    
    allow_live_remix = bool(album_ctx.get("is_live") or album_ctx.get("is_unplugged"))
    canonical = is_valid_version(title, allow_live_remix=allow_live_remix)
    
    # ✅ Store canonical title audit fields
    track['is_canonical_title'] = 1 if canonical else 0
    track['title_similarity_to_base'] = sim_to_base
    
    spotify_matched = bool(track.get("is_spotify_single"))
    tot = track.get("spotify_total_tracks")
    short_release = (tot is not None and tot > 0 and tot <= 2)
    
    # Accumulate sources for visibility
    sources = set()
    if spotify_matched:
        sources.add("spotify")
    if short_release:
        sources.add("short_release")
    
    if verbose:
        hints = []
        if spotify_matched:
            hints.append("Spotify single")
        if short_release:
            hints.append(f"short release ({tot} tracks)")
        if hints:
            logging.info(f"💡 Initial hints: {', '.join(hints)}")
    
    # --- Discogs Single (hard stop) ---
    discogs_single_hit = False
    try:
        if verbose:
            logging.info("🔍 Checking Discogs single...")
        logging.debug(f"Checking Discogs single for '{title}' by '{artist_name}'")
        if DISCOGS_ENABLED and DISCOGS_TOKEN and is_discogs_single(title, artist=artist_name, album_context=album_ctx):
            sources.add("discogs")
            discogs_single_hit = True
            track['discogs_single_confirmed'] = 1
            logging.debug(f"Discogs single detected for '{title}' (sources={sources})")
            if verbose:
                logging.info("✅ Discogs single FOUND")
        else:
            logging.debug(f"Discogs single not detected for '{title}'")
            if verbose and DISCOGS_ENABLED:
                logging.info("❌ Discogs single not found")
    except Exception as e:
        logging.exception(f"is_discogs_single failed for '{title}': {e}")
    
    if discogs_single_hit and canonical and not has_subtitle and sim_to_base >= title_sim_threshold:
        track["is_single"] = True
        track["single_sources"] = sorted(sources)
        track["single_confidence"] = "high"
        track["stars"] = 5
        logging.info(f"Single CONFIRMED (Discogs): '{title}' → 5★")
        if verbose:
            logging.info(f"⭐⭐⭐⭐⭐ CONFIRMED via Discogs single (sources: {', '.join(sorted(sources))})")
        return track
    
    # --- Discogs Official Video ---
    discogs_video_hit = False
    try:
        if DISCOGS_ENABLED and DISCOGS_TOKEN:
            if verbose:
                logging.info("🎬 Checking Discogs official video...")
            logging.debug(f"Searching Discogs for official video for '{title}' by '{artist_name}'")
            dv = discogs_official_video_signal(
                title, artist_name,
                discogs_token=DISCOGS_TOKEN,
                album_context=album_ctx,
                permissive_fallback=CONTEXT_FALLBACK_STUDIO,
            )
            logging.debug(f"Discogs video check result for '{title}': {dv}")
            if dv.get("match"):
                sources.add("discogs_video")
                discogs_video_hit = True
                track['discogs_video_found'] = 1
                if verbose:
                    logging.info("✅ Discogs official video FOUND")
            elif verbose:
                logging.info("❌ Discogs official video not found")
    except Exception as e:
        logging.exception(f"discogs_official_video_signal failed for '{title}': {e}")
    
    # Paired hard stop: Spotify + Official Video both match → 5★
    if (discogs_video_hit and spotify_matched) and canonical and not has_subtitle and sim_to_base >= title_sim_threshold:
        track["is_single"] = True
        track["single_sources"] = sorted(sources)
        track["single_confidence"] = "high"
        track["stars"] = 5
        logging.info(f"Single CONFIRMED (Spotify + Video): '{title}' → 5★")
        if verbose:
            logging.info(f"⭐⭐⭐⭐⭐ CONFIRMED via Spotify + Discogs video (sources: {', '.join(sorted(sources))})")
        return track
    
    # --- If neither Spotify nor Video match → not a single ---
    if not (discogs_video_hit or spotify_matched):
        track["is_single"] = False
        track["single_sources"] = sorted(sources)
        track["single_confidence"] = "low" if len(sources) == 0 else "medium"
        logging.debug(f"No single hint (Spotify/Video) for '{title}' → not checking further")
        if verbose:
            logging.info("⭕ No Spotify/Video hints - skipping further checks")
        return track
    
    # Add corroborative sources
    if verbose:
        logging.info("🔍 Checking additional sources (MusicBrainz, Last.fm)...")
    
    try:
        logging.debug(f"Checking MusicBrainz single for '{title}' by '{artist_name}'")
        if is_musicbrainz_single(title, artist_name):
            sources.add("musicbrainz")
            logging.debug(f"MusicBrainz reports single for '{title}'")
            if verbose:
                logging.info("✅ MusicBrainz single FOUND")
        elif verbose and MUSICBRAINZ_ENABLED:
            logging.info("❌ MusicBrainz single not found")
    except Exception as e:
        logging.exception(f"MusicBrainz single check failed for '{title}': {e}")
    
    try:
        logging.debug(f"Checking Last.fm single for '{title}' by '{artist_name}' (enabled={use_lastfm_single})")
        if use_lastfm_single and is_lastfm_single(title, artist_name):
            sources.add("lastfm")
            logging.debug(f"Last.fm reports single for '{title}'")
            if verbose:
                logging.info("✅ Last.fm single FOUND")
        elif verbose and use_lastfm_single:
            logging.info("❌ Last.fm single not found")
    except Exception as e:
        logging.exception(f"Last.fm single check failed for '{title}': {e}")
    
    # Count matches toward 5★ confirmation
    match_pool = {"spotify", "discogs_video", "musicbrainz", "lastfm"}
    if count_short_release_as_match:
        match_pool.add("short_release")
    total_matches = len(sources & match_pool)
    
    if verbose:
        logging.info(f"📊 Total sources: {', '.join(sorted(sources))} ({total_matches} matches)")
    
    if (total_matches >= 2) and canonical and not has_subtitle and sim_to_base >= title_sim_threshold:
        track["is_single"] = True
        track["single_sources"] = sorted(sources)
        track["single_confidence"] = "high"
        track["stars"] = 5
        logging.info(f"Single CONFIRMED (2+ sources): '{title}' sources={sorted(sources)} → 5★")
        if verbose:
            logging.info(f"⭐⭐⭐⭐⭐ CONFIRMED via 2+ sources: {', '.join(sorted(sources))}")
    else:
        # Got Spotify or Video hit, but only 1 source total → +1★ bump
        track["is_single"] = False
        track["single_sources"] = sorted(sources)
        track["single_confidence"] = "medium" if total_matches >= 1 else "low"
        # Apply +1★ bump if we have Spotify or Video signal
        if (spotify_matched or discogs_video_hit) and canonical and not has_subtitle:
            track["stars"] = 2  # +1 from default 1
            logging.debug(f"Low-evidence +1★ bump for '{title}' (Spotify/Video hint)")
            if verbose:
                logging.info("⭐⭐ Low-evidence bump (Spotify/Video hint)")
        elif verbose:
            logging.info("ℹ️ Not enough sources for single confirmation")
        logging.debug(f"Single NOT confirmed for '{title}' – sources={sorted(sources)} total_matches={total_matches}")
    
    return track


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Detect which tracks are singles")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output")
    
    args = parser.parse_args()
    single_detection_scan(verbose=args.verbose)
