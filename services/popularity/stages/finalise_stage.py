"""Popularity scan finalisation stage.

Migrated from the legacy ``popularity.py`` monolithic scan loop.

Handles:
- Star rating assignment (1–5★) using album/artist z-scores + z-score bands
- Navidrome rating sync via Subsonic API
- Essential playlist creation (NSP files)
- Summary logging
"""

from __future__ import annotations

import json
import logging
import math
import os
from statistics import mean, median, stdev
from typing import Any

from sqlalchemy import text
from db.engine import db_session
from db.utils import get_db_connection, row_get
from services.popularity.popularity_math import calculate_track_zscore
from services.popularity.standout_service import STANDOUT_CONFIG

from helpers.logging_config import log_unified

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Star rating thresholds
# ---------------------------------------------------------------------------

STAR_5_ALBUM_Z = STANDOUT_CONFIG.get("star_5", {}).get("album_z", 1.0)
STAR_5_ARTIST_Z = STANDOUT_CONFIG.get("star_5", {}).get("artist_z", 1.2)
# 1-4★ intra-album z-score bands (spec rule 4).  After 5★ singles/standouts
# are assigned, the REST of the album is ranked purely by its position in the
# album's own popularity distribution:
#   - Z >= +0.5              → 4★ (album standout / fan favourite)
#   - -0.5 <= Z < +0.5       → 3★ (standard album track)
#   - -1.2 <= Z < -0.5       → 2★ (deep cut / minor track)
#   - Z <  -1.2              → 1★ (filler / outlier)
# These are data-driven rather than a fixed 20/30/30/20 rank split, so a
# tightly-clustered album keeps its middle band instead of manufacturing fake
# standout/filler ratings.
STAR_4_ALBUM_Z = STANDOUT_CONFIG.get("star_4", {}).get("album_z", 0.5)
STAR_3_ALBUM_Z = STANDOUT_CONFIG.get("star_3", {}).get("album_z", -0.5)
STAR_2_ALBUM_Z = STANDOUT_CONFIG.get("star_2", {}).get("album_z", -1.2)


# ---------------------------------------------------------------------------
# Standout detection helpers
# ---------------------------------------------------------------------------

def _compute_album_z(score: float, scores: list[float]) -> float:
    if len(scores) < 3 or not any(scores):
        return 0.0
    mu = mean(scores)
    sigma = stdev(scores) if len(scores) > 1 else 1.0
    return calculate_track_zscore(score, mu, sigma)


def _compute_artist_z(score: float, artist_scores: list[float]) -> float:
    if len(artist_scores) < 5 or not any(artist_scores):
        return 0.0
    mu = mean(artist_scores)
    sigma = stdev(artist_scores) if len(artist_scores) > 1 else 1.0
    return calculate_track_zscore(score, mu, sigma)


def _listener_z(count: float, counts: list[float]) -> float:
    """Log-scaled z-score of a track's listener count within its album.

    Listener counts are heavily right-skewed (one hit dominates), so the
    z-score is computed over ``log1p``-transformed values. This is the "raw"
    standout signal from Last.fm/ListenBrainz that the blended popularity
    score compresses — a value >= 1.0 means the track is a clear listener
    outlier relative to its own album.

    Zero-variance distributions (an album whose positive counts are all the
    same value, e.g. a tracklist fallback that resolved every track to the
    same count) carry no outlier signal — return 0.0 instead of dividing by
    zero, which would crash the whole album's star-rating pass.
    """
    logs = [math.log1p(float(c)) for c in counts if float(c) > 0]
    if len(logs) < 3 or not any(logs):
        return 0.0
    mu = mean(logs)
    sigma = stdev(logs) if len(logs) > 1 else 1.0
    if not sigma or sigma <= 0:
        return 0.0
    return (math.log1p(float(count)) - mu) / sigma


def _composite_listener_z(
    track: dict[str, Any],
    album_lf_listeners: list[float] | None,
    album_lb_listens: list[float] | None,
) -> float:
    """Album-local composite z from a track's raw LF/LB counts (log-scaled).

    Blends ``_listener_z`` over the track's Last.fm listeners and ListenBrainz
    listens using the configured LF/LB weights (normalised to LF + LB = 1).
    When only one provider has usable data the composite collapses to it; no
    album distribution → 0.0 (verification skipped, the flag is honoured as-is).
    """
    if album_lf_listeners is None or album_lb_listens is None:
        return 0.0
    lf_z = _listener_z(float(track.get("lastfm_listeners") or 0), album_lf_listeners)
    lb_z = _listener_z(float(track.get("listenbrainz_listens") or 0), album_lb_listens)
    if not lf_z and not lb_z:
        return 0.0
    if not lb_z:
        return lf_z
    if not lf_z:
        return lb_z
    try:
        from services.popularity.popularity_config import resolve_weights
        w_lf, w_lb, _ = resolve_weights()
    except Exception:
        w_lf, w_lb = 0.6, 0.4
    total = w_lf + w_lb
    if total <= 0:
        return lf_z
    return (w_lf * lf_z + w_lb * lb_z) / total


def _resolve_navidrome_artist_id(cursor, artist: str) -> str | None:
    """Return the real Navidrome artist id for ``artist``, or None.

    ``artist_stats.artist_id`` is the PRIMARY KEY — writing the artist NAME
    into it (as an earlier version of this stage did) pollutes the table so
    ``lookup_artist_id`` returns the name and the Navidrome import then calls
    ``getArtist?id=<name>``, which returns no albums and silently skips the
    import.  Prefer an existing real id (a name-keyed row is never a real id),
    then fall back to the tracks table's stored Navidrome id.
    """
    try:
        cursor.execute(
            "SELECT artist_id FROM artist_stats "
            "WHERE LOWER(artist_name) = LOWER(%s) "
            "  AND LOWER(artist_id) <> LOWER(%s) "
            "LIMIT 1",
            (artist, artist),
        )
        row = cursor.fetchone()
        found = row_get(row, "artist_id") if row else None
        if found and str(found).strip() and str(found).casefold() != str(artist).casefold():
            return str(found).strip()
    except Exception as exc:
        logger.debug("[finalise_stage] artist_stats id lookup failed for %s: %s", artist, exc)
    try:
        cursor.execute(
            "SELECT artist_id FROM tracks "
            "WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s "
            "  AND artist_id IS NOT NULL AND artist_id <> '' "
            "  AND LOWER(artist_id) <> LOWER(%s) "
            "LIMIT 1",
            (artist, artist),
        )
        row = cursor.fetchone()
        found = row_get(row, "artist_id") if row else None
        if found and str(found).strip() and str(found).casefold() != str(artist).casefold():
            return str(found).strip()
    except Exception as exc:
        logger.debug("[finalise_stage] tracks id lookup failed for %s: %s", artist, exc)
    return None


# ---------------------------------------------------------------------------
# Star rating assignment
# ---------------------------------------------------------------------------

def _has_z_standout_source(track: dict[str, Any]) -> bool:
    """Return True when the track's single_sources carry ``popularity_z_standout``.

    ``popularity_z_standout`` is the catalog-size-aware popularity standout
    signal recorded during single detection (a popularity confirmation, not a
    medium source).  The 5★ standout condition requires it (or the artist-wide
    top-10% ``popularity_marked`` flag) as the "popularityzstandout" proof.
    """
    try:
        raw = track.get("single_sources") or ""
        if isinstance(raw, str):
            sources = json.loads(raw) if raw.strip() else []
        else:
            sources = raw
        return any(
            isinstance(s, dict) and str(s.get("source") or "") == "popularity_z_standout"
            for s in (sources or [])
        )
    except Exception:
        return False


def _album_z_band_star(score: float, album_scores: list[float]) -> int:
    """Album-relative 1-4★ rating from the album's z-score bands.

    Spec rule 4: the album's tracks are ranked by popularity and sliced into
    z-score bands — top of the album (Z >= +0.5) → 4★, the standard middle
    (-0.5 <= Z < +0.5) → 3★, the lower band (-1.2 <= Z < -0.5) → 2★, and
    bottom outliers (Z < -1.2) → 1★.  Purely album-relative: artist context
    is never consulted, so every album has a meaningful internal ranking and
    no track reaches 5★ from popularity alone.

    Unlike a fixed rank percentile split, the z-score bands respect the album's
    ACTUAL popularity spread: an album whose tracks are all similarly popular
    keeps them in the 3★ band instead of forcing artificial standouts/fillers,
    while a spread-out album separates cleanly into 4★/2★/1★ tiers.

    Albums too small for a meaningful z-score (< 3 valid scores) fall back to
    the 3★ middle band (their album z is 0.0).
    """
    if score <= 0:
        return 1
    valid = [float(s) for s in (album_scores or []) if float(s or 0) > 0]
    if len(valid) < 3:
        return 3
    album_z = _compute_album_z(score, valid)
    if album_z >= STAR_4_ALBUM_Z:
        return 4
    if album_z >= STAR_3_ALBUM_Z:
        return 3
    if album_z >= STAR_2_ALBUM_Z:
        return 2
    return 1


def _assign_stars(
    track: dict[str, Any],
    album_scores: list[float],
    artist_scores: list[float],
    album_lf_listeners: list[float] | None = None,
    album_lb_listens: list[float] | None = None,
    popularity_only: bool = False,
) -> int:
    """Assign 1–5 star rating to a single track (album-relative spec).

    Pipeline (spec rules 4-5):

    1. The album-relative base rating is 1-4★, assigned purely from the
       album's popularity z-score bands (``_album_z_band_star``).
    2. 5★ is reserved for:
       - high-confidence singles (``single_confidence == 'high'``), or
       - genuine triple-standouts: album z AND artist z above the standout
         thresholds AND a popularity standout (top-10% ``popularity_marked``
         or the ``popularity_z_standout`` detection signal).
       Popularity alone never grants 5★.

    ``popularity_only`` (a scan that rated popularity without single
    detection) ignores single status so a stale stored flag can't inflate the
    rating — only genuine standouts reach 5★.  Live tracks cap at 4★ (legacy
    parity).  A manual user override (``single_confidence == 'user'``) is
    always preserved.
    """
    score = float(track.get("popularity_score") or 0)
    is_single = bool(track.get("is_single"))
    single_confidence = str(track.get("single_confidence") or "low")
    is_live = bool(track.get("is_live")) or bool(track.get("album_context_live"))

    # User override — a manually-set rating is preserved by every scan type.
    if single_confidence == "user":
        return 5

    album_z = _compute_album_z(score, album_scores)
    artist_z = _compute_artist_z(score, artist_scores)
    popularity_marked = bool(track.get("popularity_marked"))

    # ── 5★: singles + standouts only ──
    # Scan synchronization for the ``popularity_z_standout`` proof: the flag
    # was recorded during single detection, so it must be re-verified against
    # the album's raw listener distribution here.  A standalone album vs the
    # rest of an artist's catalogue used to inflate every track's artist_z and
    # mark whole albums as ``z_standout`` (e.g. 36 Crazyfists - Bitterness the
    # Star).  Re-verification requires the track's composite LF/LB listener z
    # to actually clear the ``listener_5star_z_threshold`` before the flag is
    # honoured; tracks without a usable album distribution keep the flag
    # (verification returns 0.0 and is skipped, not demoted).
    z_standout_source = _has_z_standout_source(track)
    if z_standout_source:
        _verify_z = _composite_listener_z(track, album_lf_listeners, album_lb_listens)
        if _verify_z:
            _listener_threshold = float(STANDOUT_CONFIG.get("listener_5star_z_threshold", 1.0) or 1.0)
            if _verify_z < _listener_threshold:
                z_standout_source = False
    is_standout = (
        album_z >= STAR_5_ALBUM_Z
        and artist_z >= STAR_5_ARTIST_Z
        and (popularity_marked or z_standout_source)
    )
    if not is_live and (
        (not popularity_only and single_confidence == "high")
        or is_standout
    ):
        return 5

    # ── 1-4★: album-relative z-score base ──
    return _album_z_band_star(score, album_scores)


# ---------------------------------------------------------------------------
# Navidrome sync
# ---------------------------------------------------------------------------

def _load_navidrome_users() -> list[dict]:
    """Load Navidrome credentials from config."""
    users: list[dict] = []
    try:
        from helpers.config_helpers import get_config
        cfg = get_config()
        nav_users = cfg.get("navidrome_users", [])
        for u in nav_users:
            base_url = (u.get("base_url") or "").strip().rstrip("/")
            user = (u.get("user") or "").strip()
            pw = (u.get("pass") or "").strip()
            if base_url and user and pw:
                users.append({"base_url": base_url, "user": user, "pass": pw})

        if not users:
            nav = cfg.get("navidrome", {})
            base_url = (nav.get("base_url") or "").strip().rstrip("/")
            user = (nav.get("user") or "").strip()
            pw = (nav.get("pass") or "").strip()
            if base_url and user and pw:
                users.append({"base_url": base_url, "user": user, "pass": pw})
    except Exception:
        for key in ("NAV_BASE_URL", "NAV_USER", "NAV_PASS"):
            if not all(os.environ.get(k) for k in ("NAV_BASE_URL", "NAV_USER", "NAV_PASS")):
                break
        else:
            users.append({
                "base_url": os.environ["NAV_BASE_URL"].strip("/"),
                "user": os.environ["NAV_USER"],
                "pass": os.environ["NAV_PASS"],
            })
    return users


def _sync_rating_to_navidrome(track_id: str, stars: int) -> bool:
    """Push a single track rating to Navidrome via the Subsonic API.

    Old-system parity: ``features.sync_ratings_to_all_users`` (default off)
    controls whether every configured Navidrome user is updated; when off,
    only the primary (first) user is.
    """
    users = _load_navidrome_users()
    if not users:
        return False
    try:
        from services.navidrome.rating_sync_service import is_sync_ratings_to_all_users_enabled
        sync_all = is_sync_ratings_to_all_users_enabled()
    except Exception:
        sync_all = False  # old-system default: primary user only
    if not sync_all:
        users = users[:1]

    from api_clients import session
    any_success = False
    for creds in users:
        params = {
            "u": creds["user"],
            "p": creds["pass"],
            "v": "1.16.1",
            "c": "popularr",
            "f": "json",
            "id": track_id,
            "rating": stars,
        }
        try:
            resp = session.get(f"{creds['base_url']}/rest/setRating.view", params=params, timeout=10)
            resp.raise_for_status()
            result = resp.json()
            if result.get("subsonic-response", {}).get("status") == "ok":
                any_success = True
        except Exception as exc:
            logger.debug("[finalise_stage] Navidrome sync failed for track %s: %s", track_id, exc)
    return any_success


# ---------------------------------------------------------------------------
# NSP playlist helpers
# ---------------------------------------------------------------------------

def _sanitize_name(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c in ("-", "_", " ")).strip()


def _essential_playlist_name(artist: str) -> str:
    """Resolve the essential playlist name from the config template.

    ``playlists.essential_name_template`` supports the ``{artist}``
    placeholder; default ``"{artist} - Essential Collection"``.
    """
    try:
        from helpers.config_helpers import get_config
        template = str(
            (get_config().get("playlists") or {}).get("essential_name_template")
            or "{artist} - Essential Collection"
        )
    except Exception:
        template = "{artist} - Essential Collection"
    name = str(template).replace("{artist}", artist).strip()
    return name or f"{artist} - Essential Collection"


def _create_nsp_playlist(artist: str, stars_data: list[dict]) -> None:
    """Create or update the artist's essential playlist NSP file."""
    total = len(stars_data)
    five_star = [t for t in stars_data if t.get("stars") == 5]
    music_folder = os.environ.get("MUSIC_ROOT", "/music")
    playlists_dir = os.path.join(music_folder, "Playlists")
    playlist_name = _essential_playlist_name(artist)
    safe_name = _sanitize_name(playlist_name)
    file_path = os.path.join(playlists_dir, f"{safe_name}.nsp")

    # Remove the legacy-named file so a name change doesn't leave duplicates.
    legacy_path = os.path.join(playlists_dir, f"{_sanitize_name(f'{artist} (Essential Playlist)')}.nsp")
    if legacy_path != file_path and os.path.exists(legacy_path):
        try:
            os.remove(legacy_path)
        except Exception:
            pass

    # Case A: 10+ five-star tracks → pure 5-star essentials
    if len(five_star) >= 10:
        playlist = {
            "name": playlist_name,
            "comment": "Auto-generated by Popularr",
            "all": [{"is": {"artist": artist}}, {"is": {"rating": 5}}],
            "sort": "random",
        }
    # Case B: 100+ total tracks → top 10% by rating
    elif total >= 100:
        limit = max(1, math.ceil(total * 0.10))
        playlist = {
            "name": playlist_name,
            "comment": "Auto-generated by Popularr",
            "all": [{"is": {"artist": artist}}],
            "sort": "-rating,random",
            "limit": limit,
        }
    else:
        # Delete existing playlist
        if os.path.exists(file_path):
            os.remove(file_path)
        return

    os.makedirs(playlists_dir, exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(playlist, f, indent=2)
    logger.info("[finalise_stage] NSP playlist created: %s", file_path)


# ---------------------------------------------------------------------------
# Per-album star rating posting
# ---------------------------------------------------------------------------

def compute_artist_scores(
    artist: str,
    scan_scores: list[float],
    conn,
    cursor,
    scanned_titles: set[str] | None = None,
) -> list[float]:
    """Artist-wide score distribution = scan results so far + existing DB scores.

    Mirrors the merge used at the end of the scan so per-album star ratings
    posted during the scan use the same artist context the final pass would.

    ``scanned_titles`` (lowercased titles from THIS scan) are excluded from
    the DB merge — those tracks were persisted during the scan, so including
    them would double-count every scanned track (raw scan score + stored
    final_score) and drift the artist z-scores between scans.

    Stored ``final_score`` values mix two scales — albums scanned before the
    album-relative re-map keep their RAW combined score (hits at 85-95) while
    freshly-scanned albums persist album-relative values centred at ~50 — so
    each stored album is re-anchored onto the album-relative scale before
    merging.  Without this, raw-scale outliers dominate the artist
    distribution: they inflate the top-10% ``popularity_marked`` cutoff and
    skew artist z-scores, pushing genuinely top-10% album-relative tracks below
    the cut.
    """
    scanned_titles = scanned_titles or set()
    artist_scores = [float(s) for s in scan_scores if float(s or 0) > 0]
    db_rows: list[tuple[str, str]] = []
    try:
        cursor.execute(
            "SELECT title, album, final_score FROM tracks "
            "WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s AND final_score > 0",
            (artist,),
        )
        db_rows = [
            (str(row.get("album") or ""), float(row.get("final_score") or 0))
            for row in cursor.fetchall()
            if row.get("final_score")
            and str(row.get("title") or "").strip().lower() not in scanned_titles
        ]
    except Exception as exc:
        logger.debug("[finalise_stage] Artist DB score fetch failed for %s: %s", artist, exc)
    try:
        from services.popularity.popularity_math import reanchor_scores_to_album_relative
        db_scores = list(reanchor_scores_to_album_relative(db_rows))
    except Exception as exc:
        logger.debug("[finalise_stage] Artist DB score re-anchor failed for %s: %s", artist, exc)
        db_scores = [float(s) for _alb, s in db_rows]
    return list(artist_scores) + db_scores


def post_album_star_ratings(
    *,
    album_results: list[dict[str, Any]],
    artist: str,
    artist_scores: list[float],
    options: dict[str, Any],
    conn=None,
    cursor=None,
) -> dict[str, int]:
    """Assign, persist, log and sync star ratings for ONE album.

    Mirrors the legacy scanner, which posted the per-album star-rating summary
    right after each album completed.  During a full artist/library scan the
    ratings are written to the DB and surfaced in the unified log as each
    album finishes, instead of all being batched for the end of the scan.

    ``conn``/``cursor`` are optional — when omitted a connection is opened and
    closed for this album (safe to call standalone from the scan loop).
    """
    if not album_results:
        return {"star_ratings": 0, "navidrome_synced": 0}

    owns_conn = conn is None or cursor is None
    if owns_conn:
        conn = get_db_connection()
        cursor = conn.cursor()

    total_star_ratings = 0
    navidrome_synced = 0

    try:
        album = str(album_results[0].get("album") or "Unknown")

        album_scores = [
            float(r.get("popularity_score") or 0)
            for r in album_results
            if float(r.get("popularity_score") or 0) > 0
        ]
        # Raw listener counts per album track — used to detect listener
        # standouts (log-scaled z) for confirmed singles.
        album_lf_listeners = [float(r.get("lastfm_listeners") or 0) for r in album_results]
        album_lb_listens = [float(r.get("listenbrainz_listens") or 0) for r in album_results]

        try:
            # Merge DB scores ONLY for tracks not in the current scan.
            # Otherwise the distribution double-counts every scanned track
            # (raw popularity_score + stored final_score, which is
            # decay-adjusted) and the z-scores drift between scans.
            scanned_titles = {str(r.get("title") or "").strip().lower() for r in album_results}
            cursor.execute(
                "SELECT title, final_score FROM tracks "
                "WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s AND album = %s AND final_score > 0",
                (artist, album),
            )
            db_album_scores = [
                float(row.get("final_score") or 0)
                for row in cursor.fetchall()
                if row.get("final_score")
                and str(row.get("title") or "").strip().lower() not in scanned_titles
            ]
            album_scores = list(album_scores) + list(db_album_scores)
        except Exception as exc:
            logger.debug("[finalise_stage] Album DB score merge failed for %s - %s: %s", artist, album, exc)

        for track in album_results:
            # Assign star rating — one track's edge case (e.g. a degenerate
            # distribution) must never abort the whole album's rating pass and
            # silently leave every track unrated.  Surface it, skip it, and let
            # the rest of the album proceed.
            try:
                stars = _assign_stars(
                    track,
                    album_scores,
                    artist_scores,
                    album_lf_listeners,
                    album_lb_listens,
                    popularity_only=bool(options.get("popularity_only")),
                )
            except Exception as exc:
                logger.warning(
                    "[finalise_stage] Star assignment failed for %s - %s: %s",
                    artist, track.get("title"), exc,
                )
                continue
            track["stars"] = stars
            total_star_ratings += 1
            logger.debug(
                "[finalise_stage] %s - %s → %d★ (score=%.1f, album_z=%.2f, artist_z=%.2f, single=%s/%s)",
                artist, track.get("title"), stars,
                float(track.get("popularity_score") or 0),
                _compute_album_z(float(track.get("popularity_score") or 0), album_scores),
                _compute_artist_z(float(track.get("popularity_score") or 0), artist_scores),
                track.get("is_single"), track.get("single_confidence"),
            )

            # Persist to database
            track_id = str(track.get("track_id") or "")
            if track_id:
                try:
                    cursor.execute(
                        "UPDATE tracks SET stars = %s WHERE id = %s",
                        (stars, track_id)
                    )
                except Exception as exc:
                    logger.debug("[finalise_stage] DB update failed for %s: %s", track_id, exc)

        conn.commit()

        # ── Per-album progress (dashboard unified log) ──────────
        # Mirrors the legacy scanner: emit a human-readable per-album
        # star-rating summary so operators can follow progress in the
        # dashboard log while the scan is running.
        try:
            star_counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
            for t in album_results:
                s = int(t.get("stars") or 0)
                if 1 <= s <= 5:
                    star_counts[s] += 1
            log_unified(
                f"Star Ratings - Album '{album}' by {artist}: "
                f"5★: {star_counts[5]}, 4★: {star_counts[4]}, 3★: {star_counts[3]}, "
                f"2★: {star_counts[2]}, 1★: {star_counts[1]}"
            )
            # A popularity-only pass did NOT run single detection, so the
            # stale single flags in the DB must not drive the per-album output
            # (tracks are grouped by star tier instead).
            _pop_only = bool(options.get("popularity_only"))
            singles_detected = [] if _pop_only else [t for t in album_results if t.get("is_single")]
            if singles_detected:
                log_unified(
                    f"Singles Detection - Detected {len(singles_detected)} single(s) in '{album}'"
                )

            # ── Detailed per-track final output ───────────────────
            # Mirrors the legacy scanner's album summary: every track
            # is listed with its star rating, grouped into detected
            # singles, popular tracks, and the rest of the album.
            if album_results:
                detected_singles: list[tuple[str, int, str, float, str]] = []
                popular_songs: list[tuple[str, int, str, float, str]] = []
                rest_of_album: list[tuple[str, int, str, float, str]] = []
                for t in album_results:
                    t_stars = int(t.get("stars") or 0)
                    t_single = bool(t.get("is_single"))
                    t_conf = str(t.get("single_confidence") or "low").lower()
                    t_title = str(t.get("title") or "Unknown")
                    t_artist = str(t.get("artist") or artist)
                    t_score = float(t.get("popularity_score") or t.get("final_score") or 0)
                    album_z = _compute_album_z(t_score, album_scores)
                    artist_z = _compute_artist_z(t_score, artist_scores)

                    reasons: list[str] = []
                    try:
                        sources = t.get("single_sources") or ""
                        if isinstance(sources, str):
                            parsed = json.loads(sources) if sources.strip() else []
                        else:
                            parsed = sources
                        if isinstance(parsed, list):
                            reasons.append(", ".join(str(s) for s in parsed[:3]))
                    except Exception:
                        pass
                    if t_single and t_conf == "high" and album_z:
                        reasons.append(f"album-z-score: {album_z:.2f}")
                    elif t_stars == 5 and album_z:
                        reasons.append(f"album-z-score: {album_z:.2f}")
                    elif album_z:
                        reasons.append(f"album-z-score: {album_z:.2f}")
                    # Surface the raw listener counts alongside the
                    # scoring so ratings are easy to sanity-check
                    # against the source data (Last.fm / ListenBrainz).
                    reasons.append(
                        f"lf={int(t.get('lastfm_listeners') or 0):,} "
                        f"lb={int(t.get('listenbrainz_listens') or 0):,}"
                    )
                    # Show each provider's score contribution so the
                    # rating can be traced back to the sources.
                    reasons.append(
                        f"LF-score={float(t.get('lastfm_score') or 0):.1f} "
                        f"LB-score={float(t.get('listenbrainz_score') or 0):.1f} "
                        f"score={t_score:.1f}"
                    )
                    reason_str = f" ({'; '.join(r for r in reasons if r)})" if reasons else ""

                    # Both high- and medium-confidence singles belong in the
                    # Detected Singles list — a medium single (e.g. MB-only
                    # confirmation with a low z-score) is still a single and
                    # must not vanish into "Rest of Album".  A popularity-only
                    # pass has no fresh single data, so it groups by star tier
                    # (5★ standouts under "Top Rated Tracks") instead.
                    if not _pop_only and t_single and t_conf in ("high", "medium"):
                        detected_singles.append((t_title, t_stars, t_artist, t_score, reason_str))
                    elif t_stars == 5:
                        popular_songs.append((t_title, t_stars, t_artist, t_score, reason_str))
                    else:
                        rest_of_album.append((t_title, t_stars, t_artist, t_score, reason_str))

                def _log_track_group(lines: list[tuple[str, int, str, float, str]]) -> None:
                    # List in star-rating order (descending), using the
                    # track's popularity score as the tie-breaker.
                    for t_title, t_stars, t_artist, t_score, reason in sorted(
                        lines, key=lambda item: (-item[1], -item[3])
                    ):
                        star_str = "★" * max(0, min(t_stars, 5)) + "☆" * max(0, 5 - min(t_stars, 5))
                        log_unified(
                            f"Single Detection Scan - {star_str:<5} {t_artist} - {t_title}{reason}"
                        )

                if detected_singles:
                    log_unified(f"Single Detection Scan - ===== {album} - Detected Singles =====")
                    _log_track_group(detected_singles)
                if popular_songs:
                    if _pop_only:
                        _pop_header = "Top Rated Tracks"
                    else:
                        _pop_header = "Popular Songs (Not Detected as Single)"
                    log_unified(f"Single Detection Scan - ===== {album} - {_pop_header} =====")
                    _log_track_group(popular_songs)
                if rest_of_album:
                    if detected_singles or popular_songs:
                        log_unified(f"Single Detection Scan - ===== {album} - Rest of Album =====")
                    else:
                        log_unified(f"Single Detection Scan - ===== {album} - All Tracks =====")
                    _log_track_group(rest_of_album)
        except Exception as log_exc:
            logger.debug("[finalise_stage] Album progress log failed: %s", log_exc)

        # Sync to Navidrome — every rated track (old-system parity:
        # 1★/2★ ratings and downgrades must propagate too).
        if options.get("sync_navidrome", True):
            _attempted = 0
            _synced = 0
            for track in album_results:
                stars = track.get("stars", 0)
                if stars < 1:
                    continue  # unrated
                track_id = str(track.get("track_id") or "")
                if track_id:
                    _attempted += 1
                    if _sync_rating_to_navidrome(track_id, stars):
                        navidrome_synced += 1
                        _synced += 1
            _failed = _attempted - _synced
            if _failed > 0:
                logger.warning(
                    "[finalise_stage] %d/%d Navidrome rating syncs failed for %s — check credentials / Subsonic API",
                    _failed, _attempted, artist,
                )

    except Exception as exc:
        logger.error("[finalise_stage] Album finalisation failed for %s - %s: %s", artist, album, exc)
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        if owns_conn:
            conn.close()

    return {"star_ratings": total_star_ratings, "navidrome_synced": navidrome_synced}


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

def finalise_scan(*, results: list[dict[str, Any]], options: dict[str, Any]) -> None:
    """Finalise the scan: assign star ratings, sync to Navidrome, create playlists, log summary.

    ``results`` is a list of per-track result dicts produced by ``track_stage``.
    Each dict should contain at minimum:
        track_id, artist, album, title, popularity_score,
        lastfm_listeners, listenbrainz_listens,
        is_single, single_confidence, is_live, album_context_live
    """
    track_count = len(results) if results else 0
    logger.info("[FINALISE_STAGE] Finalising scan — %s tracks processed", track_count)
    if not results:
        return

    # Group results by artist for per-artist stats.  The grouping key is the
    # ALBUM artist (``album_artist``, falling back to the track artist): the
    # DB stores popularity under the album artist, so a featured-artist track
    # ("Feuerschwanz feat. Fabienne Erni") must join its album-mates instead of
    # splitting the album into 1-track fragments with a degenerate (MAD=0.0)
    # distribution.
    from collections import defaultdict
    by_artist: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        artist = str(r.get("album_artist") or r.get("artist") or r.get("canonical_artist") or "Unknown")
        by_artist[artist].append(r)

    conn = get_db_connection()
    cursor = conn.cursor()
    total_star_ratings = 0
    navidrome_synced = 0

    # When the scan runner posted star ratings after each album (legacy
    # behaviour), finalise skips those albums — it still does the remaining
    # artist-level work (stats, essential playlist, summary).  Re-assigning
    # already-posted albums would duplicate the per-album output/log lines.
    per_album_posted = bool(options.get("_per_album_posted"))
    posted_keys: set[tuple[str, str]] = set(options.get("_per_album_posted_keys") or set())

    try:
        for artist, artist_results in by_artist.items():
            if artist.lower() in ("various artists", "various", "compilation", "soundtrack"):
                continue

            # Collect artist-wide scores, merging in existing DB scores so
            # mature/frozen tracks (skipped by the runner) still anchor the
            # album/artist distributions.  Tracks scored during THIS scan are
            # excluded from the DB merge so the distribution never
            # double-counts them (raw scan score + stored final_score).
            scanned_titles = {
                str(r.get("title") or "").strip().lower()
                for r in artist_results
            }
            artist_scores = compute_artist_scores(
                artist,
                [float(r.get("popularity_score") or 0) for r in artist_results],
                conn,
                cursor,
                scanned_titles=scanned_titles,
            )

            # ── Persist artist_stats (artist-context popularity data) ────
            # The mean-popularity adjustment reads median_popularity / MAD
            # from this table — without a write here the adjustment silently
            # no-ops and the artist page has no catalogue statistics.
            try:
                _valid_scores = [float(s) for s in artist_scores if float(s or 0) > 0]
                if _valid_scores:
                    _med = median(_valid_scores)
                    _mads = [abs(s - _med) for s in _valid_scores]
                    _mad = median(_mads) if _mads else 0.0
                    _album_count = len({str(r.get("album") or "") for r in artist_results if r.get("album")})
                    # artist_id is the PRIMARY KEY and must be the real
                    # Navidrome id — writing the artist name here made the
                    # artist import resolve "id = name" and skip (getArtist
                    # with a name returns no albums).  Resolve an existing
                    # real id when possible.
                    _artist_id = _resolve_navidrome_artist_id(cursor, artist) or artist
                    from datetime import datetime as _dt
                    cursor.execute(
                        """
                        INSERT INTO artist_stats
                            (artist_id, artist_name, album_count, track_count, last_updated,
                             mean_popularity, median_popularity, popularity_stddev, popularity_mad)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (artist_id) DO UPDATE SET
                            artist_name = EXCLUDED.artist_name,
                            album_count = EXCLUDED.album_count,
                            track_count = EXCLUDED.track_count,
                            last_updated = EXCLUDED.last_updated,
                            mean_popularity = EXCLUDED.mean_popularity,
                            median_popularity = EXCLUDED.median_popularity,
                            popularity_stddev = EXCLUDED.popularity_stddev,
                            popularity_mad = EXCLUDED.popularity_mad
                        """,
                        (
                            _artist_id, artist, _album_count, len(_valid_scores), _dt.now().isoformat(),
                            mean(_valid_scores), _med,
                            stdev(_valid_scores) if len(_valid_scores) > 1 else 0.0,
                            _mad,
                        ),
                    )
                    logger.info(
                        "[FINALISE_STAGE] artist_stats updated for '%s' (id=%s, tracks=%d, median=%.1f, MAD=%.1f)",
                        artist, _artist_id, len(_valid_scores), _med, _mad,
                    )
            except Exception as exc:
                logger.debug("[finalise_stage] artist_stats persist failed for %s: %s", artist, exc)

            # Group by album for album-level z-scores
            by_album: dict[str, list[dict]] = defaultdict(list)
            for r in artist_results:
                album = str(r.get("album") or "Unknown")
                by_album[album].append(r)

            for album, album_results in by_album.items():
                if (artist, album) in posted_keys:
                    # The runner already assigned, persisted, logged and synced
                    # star ratings for this album during the scan loop.
                    continue
                _posted = post_album_star_ratings(
                    album_results=album_results,
                    artist=artist,
                    artist_scores=artist_scores,
                    options=options,
                    conn=conn,
                    cursor=cursor,
                )
                total_star_ratings += _posted.get("star_ratings", 0)
                navidrome_synced += _posted.get("navidrome_synced", 0)

            # Create essential playlist
            if options.get("create_playlists", True):
                _create_nsp_playlist(artist, artist_results)

    except Exception as exc:
        logger.error("[finalise_stage] Finalisation failed: %s", exc)
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()

    # Log summary
    if per_album_posted:
        # Star ratings were assigned/persisted during the scan loop — count
        # them from the results for the summary instead of 0.
        total_star_ratings = sum(1 for r in results if (r.get("stars") or 0) > 0)
    logger.info("[FINALISE_STAGE] Star ratings assigned: %d", total_star_ratings)
    logger.info("[FINALISE_STAGE] Navidrome syncs: %d", navidrome_synced)

    star_counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for r in results:
        s = r.get("stars", 0) or 0
        if 1 <= s <= 5:
            star_counts[s] += 1
    logger.info(
        "[FINALISE_STAGE] Star distribution — 5★: %d, 4★: %d, 3★: %d, 2★: %d, 1★: %d",
        star_counts[5], star_counts[4], star_counts[3], star_counts[2], star_counts[1],
    )

    return None

