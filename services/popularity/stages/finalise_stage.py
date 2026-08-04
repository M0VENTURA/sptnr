"""Popularity scan finalisation stage.

Migrated from the legacy ``popularity.py`` monolithic scan loop.

Handles:
- Star rating assignment (1–5★) using album/artist z-scores + percentiles
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
STAR_5_ARTIST_PCT = STANDOUT_CONFIG.get("star_5", {}).get("artist_pct", 0.10)
STAR_4_ALBUM_Z = STANDOUT_CONFIG.get("star_4", {}).get("album_z", 0.8)
STAR_4_ARTIST_Z = STANDOUT_CONFIG.get("star_4", {}).get("artist_z", 1.0)
STAR_4_ARTIST_PCT = STANDOUT_CONFIG.get("star_4", {}).get("artist_pct", 0.20)
STAR_3_ALBUM_Z = STANDOUT_CONFIG.get("star_3", {}).get("album_z", 0.0)
# Tracks clearly below the album average (album z below this floor) get 1★ —
# without it, every track with a valid score defaulted to 2★ and 1★ was
# unreachable.
STAR_1_ALBUM_Z = STANDOUT_CONFIG.get("star_1", {}).get("album_z", -1.0)
POPULARITY_5STAR_Z = STANDOUT_CONFIG.get("popularity_5star_z_threshold", 2.0)
LB_UNRELIABLE_5STAR = STANDOUT_CONFIG.get("lb_unreliable_5star_threshold", 0.50)
# Log-scaled listener z-score (within the album) that qualifies a confirmed
# single as a listener standout — the raw Last.fm/ListenBrainz evidence the
# blended popularity score compresses.
LISTENER_5STAR_Z = STANDOUT_CONFIG.get("listener_5star_z_threshold", 1.0)
UNDERPERFORMING_THRESHOLD = 0.6
# Percentile-based elite paths need a statistically meaningful catalogue —
# "top 10%" of a 5-track artist is a single track and means nothing. Below
# this size, only a genuinely high artist z-score can substitute.
STAR_5_MIN_CATALOGUE = 20


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


def _is_top_artist_percentile(score: float, artist_scores: list[float], pct: float) -> bool:
    if not artist_scores or score <= 0:
        return False
    above = sum(1 for s in artist_scores if s > score)
    total = len(artist_scores)
    return total > 0 and (above / total) <= pct


def _is_lastfm_unreliable(lastfm_listeners: float, lb_listens: float) -> bool:
    return int(lastfm_listeners) <= 20 and int(lb_listens) >= 75


def _listener_z(count: float, counts: list[float]) -> float:
    """Log-scaled z-score of a track's listener count within its album.

    Listener counts are heavily right-skewed (one hit dominates), so the
    z-score is computed over ``log1p``-transformed values. This is the "raw"
    standout signal from Last.fm/ListenBrainz that the blended popularity
    score compresses — a value >= 1.0 means the track is a clear listener
    outlier relative to its own album.
    """
    logs = [math.log1p(float(c)) for c in counts if float(c) > 0]
    if len(logs) < 3 or not any(logs):
        return 0.0
    mu = mean(logs)
    sigma = stdev(logs) if len(logs) > 1 else 1.0
    return (math.log1p(float(count)) - mu) / sigma


# ---------------------------------------------------------------------------
# Star rating assignment
# ---------------------------------------------------------------------------

def _assign_stars(
    track: dict[str, Any],
    album_scores: list[float],
    artist_scores: list[float],
    album_lf_listeners: list[float] | None = None,
    album_lb_listens: list[float] | None = None,
) -> int:
    """Assign 1–5 star rating to a single track."""
    score = float(track.get("popularity_score") or 0)
    is_single = bool(track.get("is_single"))
    single_confidence = str(track.get("single_confidence") or "low")
    is_live = bool(track.get("is_live")) or bool(track.get("album_context_live"))
    lb_listens = float(track.get("listenbrainz_listens") or 0)
    lf_listeners = float(track.get("lastfm_listeners") or 0)
    lb_percentile = float(track.get("lb_percentile") or 0)

    # User override
    if single_confidence == "user":
        return 5

    album_z = _compute_album_z(score, album_scores)
    artist_z = _compute_artist_z(score, artist_scores)
    artist_catalogue_size = len(artist_scores)
    lf_z = _listener_z(lf_listeners, album_lf_listeners or [])
    lb_z = _listener_z(lb_listens, album_lb_listens or [])
    # Elite = top of the artist catalogue AND a standout within its own album.
    # Without the album check, the least-bad track of a weak album would earn
    # 5★ purely from percentile. Percentile paths also need a meaningful
    # catalogue size; for small catalogues a genuinely high artist z-score is
    # the only substitute.
    _album_distribution_valid = len(album_scores) >= 3 and any(album_scores)
    is_elite = (
        _is_top_artist_percentile(score, artist_scores, STAR_5_ARTIST_PCT)
        and (not _album_distribution_valid or album_z >= STAR_4_ALBUM_Z)
        and (artist_catalogue_size >= STAR_5_MIN_CATALOGUE or artist_z >= STAR_5_ARTIST_Z)
    )
    is_top_catalog = _is_top_artist_percentile(score, artist_scores, 0.25)

    # ── 5★ paths (old-system alignment: artist-catalogue standing required) ──
    # The legacy scanner only granted 5★ to genuine artist-wide standouts:
    # top-10% elite, or top-25% catalogue for the other 5★ paths. That gate
    # is restored here (ListenBrainz evidence is kept as an additional
    # confirmation source, but never removes the catalogue requirement).

    # 5★: elite catalogue track (top 10% artist-wide) — album-relative too.
    if is_elite and not is_live:
        return 5

    # 5★/4★: high-confidence single (old-system path 3 + LB addition).
    if is_single and single_confidence == "high":
        if is_live:
            # Live singles reach 5★ only when elite/top-catalogue.
            return 5 if is_elite else 4
        if (
            is_top_catalog
            or album_z >= STAR_5_ALBUM_Z
            or artist_z >= STAR_5_ARTIST_Z
            or max(lf_z, lb_z) >= LISTENER_5STAR_Z
        ):
            return 5
        return 4

    # 4★/5★: medium-confidence single (old-system path 4).
    if is_single and single_confidence == "medium":
        if is_live:
            return 4 if is_top_catalog else 3
        return 5 if is_top_catalog else 4

    # Non-single popularity 5★ (old-system path 5): album z ≥ 2.0 AND
    # top-25% catalogue — a huge album-local z alone is not enough.
    if album_z >= POPULARITY_5STAR_Z and not is_live:
        return 5 if is_top_catalog else 4

    # Listener standout (non-single): raw Last.fm/ListenBrainz counts make
    # the track a clear log-scaled outlier within its album — 5★ only when
    # the track is also top-25% of the artist catalogue (old-system LB gate).
    if not is_live and max(lf_z, lb_z) >= LISTENER_5STAR_Z:
        return 5 if is_top_catalog else 4

    # LB rescue (old-system path 6 + lb_z addition): Last.fm unreliable but
    # ListenBrainz percentile strong and the track stands out by album z or
    # raw LB listen z — 5★ only if top-25% catalogue, else 4★.
    if (
        not is_live
        and _is_lastfm_unreliable(lf_listeners, lb_listens)
        and lb_percentile >= LB_UNRELIABLE_5STAR
        and (album_z >= STAR_4_ALBUM_Z or lb_z >= LISTENER_5STAR_Z)
    ):
        return 5 if is_top_catalog else 4

    # ── 4★ ──
    if _is_top_artist_percentile(score, artist_scores, STAR_4_ARTIST_PCT):
        return 4

    # 4★: clearly above the album average even when the artist catalogue is
    # too large to crack the top-20% percentile — album-relative standing is
    # a first-class signal (an album standout like "Demons" at z=1.17 should
    # not sit at 3★ merely because the artist has hundreds of tracks).
    if album_z >= STAR_4_ALBUM_Z and score > 0:
        return 4

    # ── 3★ / 1★ / 2★ ──
    # 3★: above album average
    if album_z >= STAR_3_ALBUM_Z and score > 0:
        return 3

    # 1★: clearly below album average
    if album_z < STAR_1_ALBUM_Z and score > 0:
        return 1

    # 2★: has a score, around album average
    if score > 0:
        return 2

    return 1


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
    """Push a single track rating to all Navidrome users via Subsonic API."""
    users = _load_navidrome_users()
    if not users:
        return False

    from api_clients import session
    any_success = False
    for creds in users:
        params = {
            "u": creds["user"],
            "p": creds["pass"],
            "v": "1.16.1",
            "c": "sptnr",
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


def _create_nsp_playlist(artist: str, stars_data: list[dict]) -> None:
    """Create or update an '{artist} (Essential Playlist)' NSP playlist."""
    total = len(stars_data)
    five_star = [t for t in stars_data if t.get("stars") == 5]
    music_folder = os.environ.get("MUSIC_ROOT", "/music")
    playlists_dir = os.path.join(music_folder, "Playlists")
    safe_name = _sanitize_name(f"{artist} (Essential Playlist)")
    file_path = os.path.join(playlists_dir, f"{safe_name}.nsp")

    # Case A: 10+ five-star tracks → pure 5-star essentials
    if len(five_star) >= 10:
        playlist = {
            "name": f"{artist} (Essential Playlist)",
            "comment": "Auto-generated by SPTNR",
            "all": [{"is": {"artist": artist}}, {"is": {"rating": 5}}],
            "sort": "random",
        }
    # Case B: 100+ total tracks → top 10% by rating
    elif total >= 100:
        limit = max(1, math.ceil(total * 0.10))
        playlist = {
            "name": f"{artist} (Essential Playlist)",
            "comment": "Auto-generated by SPTNR",
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

    # Group results by artist for per-artist stats
    from collections import defaultdict
    by_artist: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        artist = str(r.get("artist") or r.get("canonical_artist") or "Unknown")
        by_artist[artist].append(r)

    conn = get_db_connection()
    cursor = conn.cursor()
    total_star_ratings = 0
    navidrome_synced = 0

    try:
        for artist, artist_results in by_artist.items():
            if artist.lower() in ("various artists", "various", "compilation", "soundtrack"):
                continue

            # Collect artist-wide scores
            artist_scores = [float(r.get("popularity_score") or 0) for r in artist_results if float(r.get("popularity_score") or 0) > 0]

            # Merge in existing DB scores so mature/frozen tracks (skipped by
            # the runner) still anchor the album/artist distributions.
            try:
                cursor.execute(
                    "SELECT final_score FROM tracks "
                    "WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s AND final_score > 0",
                    (artist,),
                )
                db_artist_scores = [float(row.get("final_score") or 0) for row in cursor.fetchall() if row.get("final_score")]
                artist_scores = list(artist_scores) + list(db_artist_scores)
            except Exception as exc:
                logger.debug("[finalise_stage] Artist DB score merge failed for %s: %s", artist, exc)

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
                            artist, artist, _album_count, len(_valid_scores), _dt.now().isoformat(),
                            mean(_valid_scores), _med,
                            stdev(_valid_scores) if len(_valid_scores) > 1 else 0.0,
                            _mad,
                        ),
                    )
                    logger.info(
                        "[FINALISE_STAGE] artist_stats updated for '%s' (tracks=%d, median=%.1f, MAD=%.1f)",
                        artist, len(_valid_scores), _med, _mad,
                    )
            except Exception as exc:
                logger.debug("[finalise_stage] artist_stats persist failed for %s: %s", artist, exc)

            # Group by album for album-level z-scores
            by_album: dict[str, list[dict]] = defaultdict(list)
            for r in artist_results:
                album = str(r.get("album") or "Unknown")
                by_album[album].append(r)

            for album, album_results in by_album.items():
                album_scores = [float(r.get("popularity_score") or 0) for r in album_results if float(r.get("popularity_score") or 0) > 0]
                # Raw listener counts per album track — used to detect
                # listener standouts (log-scaled z) for confirmed singles.
                album_lf_listeners = [float(r.get("lastfm_listeners") or 0) for r in album_results]
                album_lb_listens = [float(r.get("listenbrainz_listens") or 0) for r in album_results]
                try:
                    cursor.execute(
                        "SELECT final_score FROM tracks "
                        "WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s AND album = %s AND final_score > 0",
                        (artist, album),
                    )
                    db_album_scores = [float(row.get("final_score") or 0) for row in cursor.fetchall() if row.get("final_score")]
                    album_scores = list(album_scores) + list(db_album_scores)
                except Exception as exc:
                    logger.debug("[finalise_stage] Album DB score merge failed for %s - %s: %s", artist, album, exc)

                for track in album_results:
                    # Assign star rating
                    stars = _assign_stars(
                        track,
                        album_scores,
                        artist_scores,
                        album_lf_listeners,
                        album_lb_listens,
                    )
                    track["stars"] = stars
                    total_star_ratings += 1

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
                    singles_detected = [t for t in album_results if t.get("is_single")]
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

                            if t_single and t_conf == "high":
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
                            log_unified(f"Single Detection Scan - ===== {album} - Popular Songs (Not Detected as Single) =====")
                            _log_track_group(popular_songs)
                        if rest_of_album:
                            if detected_singles or popular_songs:
                                log_unified(f"Single Detection Scan - ===== {album} - Rest of Album =====")
                            else:
                                log_unified(f"Single Detection Scan - ===== {album} - All Tracks =====")
                            _log_track_group(rest_of_album)
                except Exception as log_exc:
                    logger.debug("[finalise_stage] Album progress log failed: %s", log_exc)

                # Sync to Navidrome
                if options.get("sync_navidrome", True):
                    for track in album_results:
                        stars = track.get("stars", 0)
                        if stars < 3:
                            continue  # Only sync 3★+
                        track_id = str(track.get("track_id") or "")
                        if track_id and _sync_rating_to_navidrome(track_id, stars):
                            navidrome_synced += 1

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

