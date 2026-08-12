"""Popularity scan finalisation stage.

Migrated from the legacy ``popularity.py`` monolithic scan loop.

Handles:
- Star rating assignment (1–5★) using album/artist z-scores + z-score bands
- Navidrome rating sync via Subsonic API
- Essential Collection .m3u creation (deduplicated 4★/5★ artist best-of)
- Summary logging
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from datetime import datetime
from statistics import mean, median, stdev
from typing import Any

from sqlalchemy import text
from db.engine import db_session
from db.utils import get_db_connection, row_get
from services.popularity.popularity_math import (
    age_skew_multiplier,
    apply_album_relative_popularity,
    calculate_robust_zscore,
    effective_album_ratio,
    fmt_count as _fmt_count,
)
from services.popularity.popularity_zscore import composite_listener_z
from services.catalog.album_classification_service import is_live_or_alternate_track_title

from helpers.config_helpers import get_standout_config
from helpers.logging_config import log_unified

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Live star-rating / era-scaling config
# ---------------------------------------------------------------------------
# Every star/era threshold below is read from ``single_detection`` config on
# EACH call (``get_standout_config`` reads the cached config source) so a
# config.html edit applies to the next scan without a process restart — the
# legacy module-level constants froze them at Python startup.

_DEFAULT_ERA_RULES: dict[str, dict[str, float | int]] = {
    "peak": {"catalog_top_pct": 0.20, "album_top_n": 3, "max_5star_slots": 4},
    "solid": {"catalog_top_pct": 0.15, "album_top_n": 3, "max_5star_slots": 3},
    "minor": {"catalog_top_pct": 0.10, "album_top_n": 3, "max_5star_slots": 2},
}


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value) if value is not None else float(default)
    except (TypeError, ValueError):
        return float(default)


def _live_star_thresholds() -> dict[str, float]:
    """Star-tier z boundaries + epsilon buffer, read live from config."""
    cfg = get_standout_config() or {}

    def _tier(key: str, field: str, default: float) -> float:
        block = cfg.get(key) or {}
        return _safe_float(block.get(field), default)

    return {
        "star5_album_z": _tier("star_5", "album_z", 1.0),
        "star5_artist_z": _tier("star_5", "artist_z", 1.2),
        "star4_album_z": _tier("star_4", "album_z", 0.5),
        "star3_album_z": _tier("star_3", "album_z", -0.5),
        "star2_album_z": _tier("star_2", "album_z", -1.2),
        "epsilon": _safe_float(cfg.get("star_epsilon_score_points"), 0.5),
        "listener_5star_z": _safe_float(cfg.get("listener_5star_z_threshold"), 1.0),
    }


def _live_album_scaling() -> tuple[dict[str, dict[str, float | int]], float, float]:
    """Era rules + era-boundary ratios, read live from ``album_scaling``.

    Returns ``(rules, peak_era_min_ratio, solid_era_min_ratio)`` with keys
    ``{era: {catalog_top_pct, album_top_n, max_5star_slots}}``.
    """
    cfg = get_standout_config() or {}
    scaling = cfg.get("album_scaling") or {}
    if not isinstance(scaling, dict):
        scaling = {}
    rules: dict[str, dict[str, float | int]] = {}
    for era, defaults in _DEFAULT_ERA_RULES.items():
        rules[era] = {
            "catalog_top_pct": _safe_float(
                scaling.get(f"{era}_catalog_top_pct"), float(defaults["catalog_top_pct"])
            ),
            "album_top_n": int(_safe_float(
                scaling.get(f"{era}_album_top_n"), float(defaults["album_top_n"])
            )),
            "max_5star_slots": int(_safe_float(
                scaling.get(f"{era}_max_5star_slots"), float(defaults["max_5star_slots"])
            )),
        }
    peak_min = _safe_float(scaling.get("peak_era_min_ratio"), 0.75)
    solid_min = _safe_float(scaling.get("solid_era_min_ratio"), 0.40)
    return rules, peak_min, solid_min


# ---------------------------------------------------------------------------
# Star rating thresholds (documentation of the live-read defaults)
# ---------------------------------------------------------------------------
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

# Epsilon-delta closeness buffer, in SCORE POINTS on the 0-100 scale: a track
# within this of a tier boundary shares the HIGHER tier.  Hard cutoffs split
# virtually identical tracks across the boundary (e.g. STRANGER at 54.1 → 5★
# while A VIEW FROM ABOVE at 53.9 → 4★) — a single-scrobble difference decides
# the rating.  The buffer is converted to z-units per-album via the album's
# robust spread (``_star_epsilon_z``), so it means the same "0.5-point gap"
# on every album regardless of how tight the distribution is, and — with the
# robust spread floored at 8.0 — never widens a band far enough to overlap
# the neighbouring tier, preserving a distinct gap to the tier below.
# Tunable via config ``single_detection.star_epsilon_score_points``.

# 3-step album scaling model (era-qualified 5★ singles):
# Songs no longer auto-earn 5★ just for being a confirmed high-confidence
# single.  Each album is classified by where it sits on the artist's career
# curve (R_eff, from the discography benchmark M_peak + the age-skew
# multiplier A_skew), and singles must clear that era's bar:
#
#   Era (R_eff)        Catalog top-%   Album top-N    Max 5★ slots
#   peak   (>= 0.75)   top 20%         top 3          4
#   solid  (0.40-0.74) top 15%         top 2          2
#   minor  (< 0.40)    top 10%         #1 only        1
#
# A ``single=high`` track that misses the bar drops to the 4★ Single Floor
# (never below 4★).  Tunable via config.yaml ``single_detection.album_scaling``.


# ---------------------------------------------------------------------------
# Standout detection helpers
# ---------------------------------------------------------------------------

def _compute_album_z(score: float, scores: list[float]) -> tuple[float, float]:
    """Robust album z (median + scaled-MAD) — and the spread used.

    Matches ``popularity_math`` exactly (``calculate_robust_zscore``): the
    SAME robust z the album-relative popularity re-map is built on, so a
    track's z-band standing and its re-mapped popularity score can never
    disagree about its position in the album.  Returns ``(z, spread)``;
    ``(0.0, 0.0)`` when there are fewer than 3 valid scores.
    """
    return calculate_robust_zscore(score, scores, min_count=3)


def _compute_artist_z(score: float, artist_scores: list[float]) -> tuple[float, float]:
    """Robust artist-catalogue z (median + scaled-MAD) — and the spread used.

    Same mathematics as ``_compute_album_z``; an artist needs at least 5
    valid scores before its catalogue is a meaningful reference.
    """
    return calculate_robust_zscore(score, artist_scores, min_count=5)


def _star_epsilon_z(spread: float, epsilon: float | None = None) -> float:
    """Convert the score-point epsilon buffer into z-units for a spread.

    Defined in score points (the domain the user sees: 54.1 vs 53.9) because
    a fixed z-epsilon would be lax on wide-spread albums and useless on tight
    ones.  With the robust spread floored at 8.0 the epsilon is at most
    0.5/8.0 = 0.0625 z, so the widened band can never reach the next tier
    boundary (bands sit >= 0.5 z apart).  ``epsilon`` defaults to the LIVE
    ``star_epsilon_score_points`` config value.
    """
    if not spread or spread <= 0:
        return 0.0
    if epsilon is None:
        epsilon = _live_star_thresholds()["epsilon"]
    return epsilon / spread


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


def _album_z_band_star(
    score: float,
    album_scores: list[float],
    reference_scores: list[float] | None = None,
) -> int:
    """Album-relative 1-4★ rating from the z-score bands.

    Spec rule 4: the album's tracks are ranked by popularity and sliced into
    z-score bands — top of the album (Z >= +0.5) → 4★, the standard middle
    (-0.5 <= Z < +0.5) → 3★, the lower band (-1.2 <= Z < -0.5) → 2★, and
    bottom outliers (Z < -1.2) → 1★.  Purely album-relative: artist context
    is never consulted, so every album has a meaningful internal ranking and
    no track reaches 5★ from popularity alone.

    ``reference_scores`` overrides the reference distribution (used for
    compilation / Best-Of albums, where the curated tracklist inflates the
    album median — the bands are evaluated against the ARTIST's catalogue
    instead).

    Unlike a fixed rank percentile split, the z-score bands respect the album's
    ACTUAL popularity spread: an album whose tracks are all similarly popular
    keeps them in the 3★ band instead of forcing artificial standouts/fillers,
    while a spread-out album separates cleanly into 4★/2★/1★ tiers.

    Albums too small for a meaningful z-score (< 3 valid scores) fall back to
    the 3★ middle band (their album z is 0.0).  Thresholds are read LIVE from
    config on every call.
    """
    if score <= 0:
        return 1
    valid = [float(s) for s in (reference_scores if reference_scores is not None else album_scores or []) if float(s or 0) > 0]
    if len(valid) < 3:
        return 3
    album_z, spread = _compute_album_z(score, valid)
    th = _live_star_thresholds()
    # Epsilon-delta closeness buffer: a track within ``epsilon`` z of a band
    # boundary shares the HIGHER tier, so a near-boundary track is not
    # punished for a single-scrobble difference while a distinct gap to the
    # next tier down is preserved (bands are >= 0.5 z apart and the epsilon
    # is at most ~0.06 z).
    epsilon = _star_epsilon_z(spread, th["epsilon"])
    if album_z >= th["star4_album_z"] - epsilon:
        return 4
    if album_z >= th["star3_album_z"] - epsilon:
        return 3
    if album_z >= th["star2_album_z"] - epsilon:
        return 2
    return 1


# ---------------------------------------------------------------------------
# 3-step scaling model helpers
# ---------------------------------------------------------------------------

def _percentile_cutoff(scores: list[float], top_pct: float) -> float | None:
    """Score threshold separating the artist catalogue's top ``top_pct``.

    Mirrors ``_artist_top_marked_cutoffs`` semantics (sorted desc, cutoff at
    the ``ceil(len * pct)``-th score).  ``None`` when there is no data.
    """
    valid = sorted((float(s) for s in (scores or []) if float(s or 0) > 0), reverse=True)
    if not valid:
        return None
    n = max(1, math.ceil(len(valid) * top_pct))
    return valid[min(n - 1, len(valid) - 1)]


def _album_rank(score: float, album_scores: list[float]) -> int:
    """1-based rank of ``score`` within the album (ties share the top rank)."""
    if score <= 0:
        return len([s for s in (album_scores or []) if float(s or 0) > 0]) + 1
    higher = sum(1 for s in (album_scores or []) if float(s or 0) > score)
    return higher + 1


def _album_era_for_ratio(reff: float) -> str:
    """Classify an album by its effective ratio: peak / solid / minor.

    Era boundaries (``peak_era_min_ratio`` / ``solid_era_min_ratio``) are
    read LIVE from ``single_detection.album_scaling`` on every call.
    """
    _rules, peak_min, solid_min = _live_album_scaling()
    if reff >= peak_min:
        return "peak"
    if reff >= solid_min:
        return "solid"
    return "minor"


def _build_album_model(
    artist: str,
    album_results: list[dict[str, Any]],
    artist_scores: list[float],
    cursor,
) -> dict[str, Any]:
    """3-step scaling model context for ONE album.

    Step 1 — M_peak: the highest album-median across the artist's catalogue
        (DB ``final_score`` per album, re-anchored onto the album-relative
        scale so stored raw-scale albums are comparable; the current album's
        fresh scan scores are merged in).
    Step 2 — A_skew: age-skew multiplier from the album's release year vs
        the peak album's year (fresh releases / legacy albums boosted).
    Step 3 — R_eff = min(1.0, album_median * A_skew / M_peak).
    Step 4 — era rules: catalog top-% cutoff, album top-N and the 5★ slot cap.

    Returns ``{}`` when no benchmark can be derived (empty catalogue / no
    scores) — the caller then keeps the legacy single-→-5★ behaviour.
    """
    try:
        current_album = str(album_results[0].get("album") or "")
        scanned_titles = {
            str(r.get("title") or "").strip().lower() for r in album_results
        }

        cursor.execute(
            "SELECT title, album, final_score, year FROM tracks "
            "WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s AND final_score > 0",
            (artist,),
        )
        rows = cursor.fetchall()

        by_album: dict[str, list[float]] = {}
        album_years: dict[str, int] = {}
        for row in rows:
            _title = str(row_get(row, "title") or "").strip().lower()
            if _title in scanned_titles:
                continue
            _album = str(row_get(row, "album") or "")
            _score = float(row_get(row, "final_score") or 0)
            if _score > 0:
                by_album.setdefault(_album, []).append(_score)
            _year = int(row_get(row, "year") or 0)
            if _year > 0:
                album_years.setdefault(_album, _year)

        # Fresh scan scores for the current album (not yet stored) join its
        # group; years fall back to the scan's track fields.
        for r in album_results:
            _score = float(r.get("popularity_score") or 0)
            if _score > 0 and not bool(r.get("exclude_from_stats")):
                by_album.setdefault(current_album, []).append(_score)
            _year = int(r.get("year") or r.get("release_year") or 0)
            if _year > 0:
                album_years.setdefault(current_album, _year)

        # Re-anchor each album against its own distribution so the medians
        # sit on the same (album-relative) scale.
        medians: dict[str, float] = {}
        for _album, _scores in by_album.items():
            if not _scores:
                continue
            _reanchored = [
                apply_album_relative_popularity(s, _scores) for s in _scores
            ]
            medians[_album] = median(_reanchored)

        if not medians:
            return {}
        m_peak = max(medians.values())
        if m_peak <= 0:
            return {}
        current_median = medians.get(current_album) or m_peak

        peak_album = max(medians, key=medians.get)
        peak_year = album_years.get(peak_album, 0)
        album_year = album_years.get(current_album, 0)

        a_skew = age_skew_multiplier(album_year, datetime.now().year, peak_year)
        reff = effective_album_ratio(current_median * a_skew, m_peak)
        era = _album_era_for_ratio(reff)
        _rules, _, _ = _live_album_scaling()
        rules = _rules.get(era, _rules["peak"])

        return {
            "has_benchmark": True,
            "m_peak": m_peak,
            "album_median": current_median,
            "a_skew": a_skew,
            "reff": reff,
            "era": era,
            "catalog_cutoff": _percentile_cutoff(
                artist_scores, float(rules["catalog_top_pct"])
            ),
            "album_top_n": int(rules["album_top_n"]),
            "max_5star_slots": int(rules["max_5star_slots"]),
        }
    except Exception as exc:
        logger.debug("[finalise_stage] Album benchmark failed for %s: %s", artist, exc)
        return {}


def _assign_stars(
    track: dict[str, Any],
    album_scores: list[float],
    artist_scores: list[float],
    album_lf_listeners: list[float] | None = None,
    album_lb_listens: list[float] | None = None,
    popularity_only: bool = False,
    album_model: dict[str, Any] | None = None,
    is_compilation: bool = False,
) -> int:
    """Assign 1–5 star rating to a single track (album-relative spec).

    Pipeline (spec rules 4-5):

    1. The album-relative base rating is 1-4★, assigned purely from the
       album's popularity z-score bands (``_album_z_band_star``).  For
       compilation / Best-Of albums the bands use the ARTIST's catalogue
       distribution instead — a curated hits tracklist inflates the local
       median, which drives real 4★ singles at the bottom of the tracklist
       down to 1★.
    2. 5★ is reserved for:
       - high-confidence singles that clear the ORGANIC popularity floor
         (score >= 45.0 or >= 1000 Last.fm listeners — a Discogs-tagged
         single with ~300 listeners must not leapfrog genuinely popular
         album tracks), or
       - genuine triple-standouts: album z AND artist z above the standout
         thresholds AND a popularity standout (top-10% ``popularity_marked``
         or the ``popularity_z_standout`` detection signal).
       Popularity alone never grants 5★.
    3. When the 3-step album scaling model (``album_model``) is available,
       singles and marked tracks must ALSO clear their album's era bar
       (R_eff tier): the era's artist catalog top-% cutoff OR the album's
       top-N tracks.  A high single that misses the bar drops to the 4★
       Single Floor — it never falls below 4★ (and never above 3★ when the
       organic floor is not met).

    ``popularity_only`` (a scan that rated popularity without single
    detection) ignores single status so a stale stored flag can't inflate the
    rating — only genuine standouts reach 5★.  Live tracks cap at 4★ (legacy
    parity).  A manual user override (``single_confidence == 'user'``) is
    always preserved.  All thresholds are read LIVE from config on every call.
    """
    score = float(track.get("popularity_score") or 0)
    single_confidence = str(track.get("single_confidence") or "low")
    # A "(Live)"/"(Acoustic)" title-suffixed track on a studio album is a live
    # recording even when the album itself is studio — cap it at 4★ so a bonus
    # live cut can never outrank the album's real singles.  A track flagged by
    # single detection as an ``alternate_or_live_version`` carries exactly this
    # marker, so the cap fires for the same titles detection skips.
    is_live = (
        bool(track.get("is_live"))
        or bool(track.get("album_context_live"))
        or is_live_or_alternate_track_title(track.get("title"))
    )

    # User override — a manually-set rating is preserved by every scan type.
    if single_confidence == "user":
        return 5

    th = _live_star_thresholds()

    # Organic popularity floor: single-driven elevation (5★ / 4★ Single
    # Floor / era album-top-N) requires the track to have a real organic
    # audience — a metadata-tagged single with almost no listeners must not
    # jump ahead of popular album tracks.  Marked / standout paths are
    # popularity-driven by definition and are not gated.
    try:
        from services.popularity.popularity_config import get_single_organic_floor
        _org_score, _org_listeners = get_single_organic_floor()
    except Exception:
        _org_score, _org_listeners = 45.0, 1000.0
    organic = score >= _org_score or int(track.get("lastfm_listeners") or 0) >= _org_listeners

    # Compilation / Best-Of albums evaluate the star bands against the
    # ARTIST's catalogue distribution (the curated tracklist inflates the
    # album median).  True Various-Artists albums keep the album reference —
    # the "album artist" has no catalogue to compare against.
    ref_scores = artist_scores if is_compilation else album_scores

    album_z, album_spread = _compute_album_z(score, ref_scores)
    artist_z, artist_spread = _compute_artist_z(score, artist_scores)
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
        # Shared ``composite_listener_z`` — identical math to the old local
        # helper, plus the log-sigma noise floor.  When no album distribution
        # is available the verification returns 0.0 and the flag is honoured
        # as-is (passing ``None`` distributions here deliberately skips the
        # shared helper's DB fallback — the caller has already decided the
        # distribution is unavailable).
        _verify_z = 0.0
        if album_lf_listeners is not None and album_lb_listens is not None:
            _verify_z = composite_listener_z(
                float(track.get("lastfm_listeners") or 0),
                float(track.get("listenbrainz_listens") or 0),
                artist=track.get("artist"),
                album=track.get("album"),
                album_lf_listeners=album_lf_listeners,
                album_lb_listens=album_lb_listens,
            )
        if _verify_z and _verify_z < th["listener_5star_z"]:
            z_standout_source = False
    is_standout = (
        album_z >= th["star5_album_z"] - _star_epsilon_z(album_spread, th["epsilon"])
        and artist_z >= th["star5_artist_z"] - _star_epsilon_z(artist_spread, th["epsilon"])
        and (popularity_marked or z_standout_source)
    )
    # Top-% popularity marking alone grants 5★ (spec rule 2): a track in the
    # artist's top 10% is "popular" regardless of single status, so it never
    # needs a single-detection source.  The medium→high bump (rule 3) already
    # upgraded widened top-20% medium singles to HIGH confidence, which the
    # ``single_confidence == 'high'`` branch awards below — gated on the
    # organic floor so metadata-only singles can't leapfrog real popularity.
    if not is_live and (
        popularity_marked
        or (not popularity_only and single_confidence == "high" and organic)
        or is_standout
    ):
        # Triple-standout proof (album z + artist z + popularity source)
        # always earns 5★ — it is not a "single" award and is not era-capped.
        if is_standout:
            return 5

        # ── 3-step album scaling model (step 4: era rules) ──
        # When the artist's discography benchmark is available (and the track
        # actually carries a popularity score), singles and marked tracks must
        # clear the album's era bar instead of auto-earning 5★:
        #   qualify = score >= era catalog top-% cutoff
        #             OR (single=high AND album rank <= era top-N)
        # A high single that misses the bar falls to the 4★ Single Floor.
        # Marked tracks are top-10% catalogue tracks — they clear the
        # strictest (minor-era top 10%) bar and still earn 5★.
        if album_model and album_model.get("has_benchmark") and score > 0:
            era = str(album_model.get("era") or "")
            _rules, _, _ = _live_album_scaling()
            rules = _rules.get(era)
            catalog_cutoff = album_model.get("catalog_cutoff")
            qualifies_catalog = catalog_cutoff is not None and score >= float(catalog_cutoff)
            qualifies_album = (
                not popularity_only
                and organic
                and rules is not None
                and _album_rank(score, album_scores) <= int(rules["album_top_n"])
            )
            if qualifies_catalog or qualifies_album:
                track["_era_5star"] = True
                return 5
            # 4★ Single Floor (spec safety net): a high-confidence single
            # that fails the 5★ criteria never drops below 4★ — unless the
            # organic floor is unmet, in which case it must not exceed 3★.
            if not popularity_only and single_confidence == "high":
                band = _album_z_band_star(score, ref_scores)
                return max(band, 4) if organic else min(band, 3)
            # Marked-only tracks below the era bar (medium-bumped singles
            # ranked 10-20% on a minor-era album) fall through to the band.
            return _album_z_band_star(score, ref_scores)

        return 5

    # ── 1-4★: album-relative z-score base ──
    return _album_z_band_star(score, ref_scores)


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
# Essential Collection .m3u helpers
# ---------------------------------------------------------------------------

# Compilation/sampler buckets that never get an essential collection.
_ESSENTIAL_EXCLUDED_ARTISTS = frozenset({
    "various artists", "various", "va", "v/a",
    "soundtrack", "soundtracks", "unknown artist", "unknown",
})

# Minimum unique 4★/5★ tracks before an essential collection is created.
_ESSENTIAL_MIN_TRACKS = 12

# Parenthetical/bracket noise stripped from titles for dedup grouping:
# "(2018 Remaster)", "[Deluxe Edition]", "(Live)".
_ESSENTIAL_TITLE_NOISE_RE = re.compile(r"\([^)]*\)|\[[^\]]*\]")


def _sanitize_name(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c in ("-", "_", " ")).strip()


def _essential_playlist_name(artist: str) -> str:
    """Resolve the essential collection name from the config template.

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


def _essential_playlists_dir() -> str:
    """Watch directory where essential collection .m3u files are written."""
    music_folder = (
        os.environ.get("MUSIC_FOLDER")
        or os.environ.get("MUSIC_ROOT")
        or "/music"
    )
    return os.path.join(music_folder, "Playlists")


def _normalise_essential_title(title: str) -> str:
    """Normalized title used to group duplicate songs across releases.

    Strips parenthetical/bracket noise (``(2018 Remaster)``,
    ``[Deluxe Edition]``, ``(Live)``) and collapses whitespace, so the same
    track on a studio album, a Greatest Hits collection and a live release
    maps to one group.
    """
    t = str(title or "").strip().lower()
    t = _ESSENTIAL_TITLE_NOISE_RE.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def _is_excluded_essential_artist(artist: str) -> bool:
    """Return True for compilation/sampler buckets that never get a collection."""
    return str(artist or "").strip().casefold() in _ESSENTIAL_EXCLUDED_ARTISTS


def _cleanup_stale_essential_files(playlists_dir: str, artist: str, playlist_name: str) -> None:
    """Remove old NSP essential files so the .m3u is the single source.

    Earlier scans wrote ``{artist} (Essential Playlist).nsp`` and
    ``{playlist_name}.nsp`` — both are deleted whenever the .m3u is written
    or removed, so a file name change never leaves stale duplicates behind.
    """
    for name in (f"{artist} (Essential Playlist)", playlist_name):
        stale = os.path.join(playlists_dir, f"{_sanitize_name(name)}.nsp")
        try:
            if os.path.exists(stale):
                os.remove(stale)
                logger.info("[finalise_stage] Removed stale NSP essential file: %s", stale)
        except Exception:
            pass


def _essential_playlists_enabled(options: dict) -> bool:
    """Whether Essential Collection .m3u generation is on.

    Pipeline callers may override via ``options["create_playlists"]``;
    otherwise the config key ``playlists.essential_playlists_enabled``
    (default true) governs.
    """
    flag = options.get("create_playlists")
    if flag is not None:
        return bool(flag)
    try:
        from helpers.config_helpers import get_config
        cfg = (get_config() or {}).get("playlists") or {}
        return bool(cfg.get("essential_playlists_enabled", True))
    except Exception:
        return True


def _create_essential_m3u(artist: str, cursor) -> None:
    """Create/refresh or delete the artist's Essential Collection .m3u.

    Evaluated against the artist's FULL track history in the DB (not just the
    freshly scanned album), scoped strictly to the ``album_artist`` tag
    (featured-guest tracks are included as long as the primary album artist
    matches).  All 4★/5★ tracks are grouped by normalized title
    (parenthetical/bracket noise stripped) and the winner per group is chosen
    deterministically:

    1. studio over live         (``is_live`` ASC)
    2. main discography         (``is_compilation`` ASC)
    3. rating                   (``stars`` DESC)
    4. popularity               (``popularity``/``final_score`` DESC)
    5. original release         (``year`` ASC)

    The ``[Artist] - Essential Collection.m3u`` file is written to the watch
    Playlists directory only when the artist has MORE than
    ``_ESSENTIAL_MIN_TRACKS`` unique 4★/5★ tracks; below that any existing
    .m3u (and stale NSP files) is deleted.
    """
    if _is_excluded_essential_artist(artist):
        return

    playlists_dir = _essential_playlists_dir()
    playlist_name = _essential_playlist_name(artist)
    file_path = os.path.join(playlists_dir, f"{_sanitize_name(playlist_name)}.m3u")

    rows: list[Any] = []
    try:
        cursor.execute(
            """
            SELECT id, title, file_path, duration,
                   COALESCE(stars, star_rating) AS stars,
                   COALESCE(is_live, 0) AS is_live,
                   COALESCE(is_compilation, 0) AS is_compilation,
                   COALESCE(popularity, final_score, 0) AS popularity_score,
                   year, release_year
            FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s
              AND COALESCE(stars, star_rating) >= 4
            """,
            (artist,),
        )
        rows = cursor.fetchall() or []
    except Exception as exc:
        logger.debug("[finalise_stage] Essential collection fetch failed for %s: %s", artist, exc)

    def _track_year(row: Any) -> int:
        raw = row.get("release_year") or row.get("year") or 0
        try:
            return int(float(raw)) if str(raw).strip() else 0
        except (TypeError, ValueError):
            return 0

    grouped: dict[str, list[Any]] = {}
    for row in rows:
        key = _normalise_essential_title(row.get("title") or "")
        if key:
            grouped.setdefault(key, []).append(row)

    winners: list[Any] = []
    for group in grouped.values():
        group.sort(
            key=lambda r: (
                int(r.get("is_live") or 0),
                int(r.get("is_compilation") or 0),
                -int(r.get("stars") or 0),
                -float(r.get("popularity_score") or 0),
                _track_year(r),
                str(r.get("title") or "").casefold(),
            ),
        )
        winners.append(group[0])

    if len(winners) > _ESSENTIAL_MIN_TRACKS:
        # Deterministic playlist order: rating, popularity, then title.
        winners.sort(
            key=lambda r: (
                -int(r.get("stars") or 0),
                -float(r.get("popularity_score") or 0),
                str(r.get("title") or "").casefold(),
            ),
        )
        os.makedirs(playlists_dir, exist_ok=True)
        _cleanup_stale_essential_files(playlists_dir, artist, playlist_name)
        lines = ["#EXTM3U"]
        for row in winners:
            title = str(row.get("title") or "Unknown")
            try:
                duration = int(float(row.get("duration") or 0) or 0)
            except (TypeError, ValueError):
                duration = 0
            lines.append(f"#EXTINF:{duration},{artist} - {title}")
            lines.append(str(row.get("file_path") or title))
        try:
            with open(file_path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
            logger.info(
                "[finalise_stage] Essential collection created: %s (%d tracks)",
                file_path, len(winners),
            )
            log_unified(f"📄 Playlist: Generated '{playlist_name}.m3u' ({len(winners)} tracks)")
            # Best-effort: push the artist's image as the Navidrome playlist
            # cover (config: navidrome.playlist_cover_art).  Runs after the
            # .m3u is on disk so Navidrome's import has something to attach to.
            try:
                from services.playlists.playlist_service import attach_playlist_cover
                attach_playlist_cover(playlist_name, artist)
            except Exception:
                pass
        except Exception as exc:
            logger.warning("[finalise_stage] Essential collection write failed for %s: %s", artist, exc)
        return

    _cleanup_stale_essential_files(playlists_dir, artist, playlist_name)
    if os.path.exists(file_path):
        try:
            os.remove(file_path)
            logger.info(
                "[finalise_stage] Removed essential collection for %s (only %d unique 4★/5★ tracks)",
                artist, len(winners),
            )
        except Exception:
            pass


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
    from services.catalog.album_classification_service import is_bonus_track_title
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
            and not is_bonus_track_title(str(row.get("title") or ""))
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


def _album_scaling_configured() -> bool:
    """True when ``single_detection.album_scaling`` exists in the loaded config."""
    try:
        cfg = get_standout_config() or {}
        return isinstance(cfg.get("album_scaling"), dict) and bool(cfg.get("album_scaling"))
    except Exception:
        return False


def _log_scan_weights(artist: str, album: str, album_model: dict[str, Any]) -> None:
    """Log the blend weights + era rules applied to one album's rating pass.

    Makes the config actually driving the scan visible: the normalised
    popularity blend (LF / LB / Age) and the era classification with the
    caps in effect (catalog top %, album top-N, max 5★ slots).
    """
    try:
        from services.popularity.popularity_config import resolve_weights
        lf, lb, age = resolve_weights()
        rules, peak_min, solid_min = _live_album_scaling()
        era = str(album_model.get("era") or "?")
        reff = float(album_model.get("reff") or 0)
        era_rules = rules.get(era) or {}
        log_unified(
            f"⚖️ WEIGHTS: {str(artist or '').strip()} — {str(album or '').strip()} "
            f"| blend: LF={lf:.2f} LB={lb:.2f} Age={age:.2f} "
            f"| era={era} (R_eff={reff:.2f}, peak≥{peak_min:.2f}, solid≥{solid_min:.2f}) "
            f"| caps: catalog_top={float(era_rules.get('catalog_top_pct') or 0) * 100:.0f}%, "
            f"album_top_n={era_rules.get('album_top_n') or '—'}, "
            f"max_5★={era_rules.get('max_5star_slots') or '—'} "
            f"| source: {'config' if _album_scaling_configured() else 'defaults'}"
        )
    except Exception as exc:
        logger.debug("[finalise_stage] Weights log failed: %s", exc)


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

        # ── Compilation / Best-Of reference offset ─────────────────────────
        # Single-artist compilations (Greatest Hits) rate tracks against the
        # ARTIST's catalogue distribution instead of the album's: a curated
        # hits tracklist sits on an artificially high local median, which
        # drives the real 4★ singles at the bottom of the tracklist down to
        # 1★.  True Various-Artists albums keep the album reference — the
        # "album artist" has no catalogue to compare against.
        try:
            from services.enrichment.single_detection_service import is_compilation_album
            _album_type = str(
                album_results[0].get("album_type")
                or album_results[0].get("detected_album_type")
                or ""
            )
            is_compilation = bool(is_compilation_album(_album_type, album))
        except Exception:
            is_compilation = False
        if is_compilation and artist.lower() in (
            "various artists", "various", "compilation", "soundtrack"
        ):
            is_compilation = False
        # Album score distribution used for z-scores and the 1-4★ bands.
        # Bonus / alternate / live tracks (``exclude_from_stats``) on a studio
        # album are dropped from the reference so a deluxe edition padded with
        # extra live cuts does not drag the album average down and crush the
        # core tracks' ratings.  A true LIVE album flags every track excluded,
        # so the drop falls back to the full set when too few tracks remain
        # (the live album is scored against itself).
        album_scores = [
            float(r.get("popularity_score") or 0)
            for r in album_results
            if float(r.get("popularity_score") or 0) > 0
        ]
        if len(album_scores) >= 3:
            _eligible = [
                float(r.get("popularity_score") or 0)
                for r in album_results
                if float(r.get("popularity_score") or 0) > 0
                and not bool(r.get("exclude_from_stats"))
            ]
            if len(_eligible) >= 3:
                album_scores = _eligible
        # Raw listener counts per album track — used to detect listener
        # standouts (log-scaled z) for confirmed singles.  Same bonus-track
        # exclusion so an extra live cut cannot anchor the listener
        # distribution.
        album_lf_listeners = [float(r.get("lastfm_listeners") or 0) for r in album_results]
        album_lb_listens = [float(r.get("listenbrainz_listens") or 0) for r in album_results]
        if len(album_lf_listeners) >= 3 and len(album_lb_listens) >= 3:
            _elf = [
                float(r.get("lastfm_listeners") or 0)
                for r in album_results
                if not bool(r.get("exclude_from_stats"))
            ]
            _elb = [
                float(r.get("listenbrainz_listens") or 0)
                for r in album_results
                if not bool(r.get("exclude_from_stats"))
            ]
            if len(_elf) >= 3 and len(_elb) >= 3:
                album_lf_listeners = _elf
                album_lb_listens = _elb

        try:
            # Merge DB scores ONLY for tracks not in the current scan.
            # Otherwise the distribution double-counts every scanned track
            # (raw popularity_score + stored final_score, which is
            # decay-adjusted) and the z-scores drift between scans.  Stored
            # bonus tracks (live/remix/demo/... titles) are excluded the same
            # way the in-scan results are — a studio album's average must not
            # be dragged down by its padded live cuts.
            from services.catalog.album_classification_service import is_bonus_track_title
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
                and not is_bonus_track_title(str(row.get("title") or ""))
            ]
            album_scores = list(album_scores) + list(db_album_scores)
        except Exception as exc:
            logger.debug("[finalise_stage] Album DB score merge failed for %s - %s: %s", artist, album, exc)

        # ── 3-step album scaling model: era context for this album ──
        # M_peak (discography benchmark) → A_skew (age skew) → R_eff → era
        # rules.  Falls back to {} (legacy single→5★ behaviour) when the
        # artist has no catalogue data to benchmark against.
        album_model: dict[str, Any] = {}
        try:
            album_model = _build_album_model(artist, album_results, artist_scores, cursor)
            if album_model.get("has_benchmark"):
                logger.info(
                    "[finalise_stage] %s - %s → era=%s (M_peak=%.1f, album median=%.1f, A_skew=%.2f, R_eff=%.2f)",
                    artist, album, album_model.get("era"),
                    float(album_model.get("m_peak") or 0),
                    float(album_model.get("album_median") or 0),
                    float(album_model.get("a_skew") or 1.0),
                    float(album_model.get("reff") or 0),
                )
        except Exception as exc:
            logger.debug("[finalise_stage] Album model build failed for %s - %s: %s", artist, album, exc)

        # Surface the exact weights + era rules this album was rated with.
        _log_scan_weights(artist, album, album_model)

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
                    album_model=album_model,
                    is_compilation=is_compilation,
                )
            except Exception as exc:
                logger.warning(
                    "[finalise_stage] Star assignment failed for %s - %s: %s",
                    artist, track.get("title"), exc,
                )
                continue
            track["stars"] = stars
            total_star_ratings += 1
            _track_score = float(track.get("popularity_score") or 0)
            _album_z = _compute_album_z(_track_score, album_scores)[0]
            _artist_z = _compute_artist_z(_track_score, artist_scores)[0]
            # Per-track rating line at INFO so operators can verify the
            # scoring logic end-to-end (score → z-scores → star band).
            log_unified(
                f"[TRACK_RESULT] {artist} - {track.get('title')} → {stars}★ "
                f"(score={_track_score:.1f}, album_z={_album_z:.2f}, artist_z={_artist_z:.2f}, "
                f"single={track.get('is_single')}/{track.get('single_confidence')}"
                + (
                    f", era={album_model.get('era')}/R={float(album_model.get('reff') or 0):.2f}"
                    if album_model.get("has_benchmark") else ""
                )
                + ")"
            )
            logger.debug(
                "[finalise_stage] %s - %s → %d★ (score=%.1f, album_z=%.2f, artist_z=%.2f, single=%s/%s%s)",
                artist, track.get("title"), stars,
                _track_score,
                _album_z,
                _artist_z,
                track.get("is_single"), track.get("single_confidence"),
                f", era={album_model.get('era')}/R={float(album_model.get('reff') or 0):.2f}"
                if album_model.get("has_benchmark") else "",
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

                # Mirror the rating into the audio file tags (POPM/RATING) when
                # the tagging policy permits — the master toggle and the
                # ratings_only mode are honoured by ``write_rating_to_file``.
                # Silently skipped when tag writes are disabled.
                if stars >= 1:
                    try:
                        from services.metadata.tag_file_service import (
                            _get_track_file_path,
                            _resolve_music_file_path,
                            write_rating_to_file,
                        )
                        _abs = _resolve_music_file_path(_get_track_file_path(track_id))
                        if _abs:
                            write_rating_to_file(_abs, stars)
                    except Exception as _tag_err:
                        logger.debug("[finalise_stage] Rating tag write failed for %s: %s", track_id, _tag_err)

        # ── Era 5★ slot cap (scaling model step 4) ────────────────────────
        # The era's slot budget limits how many 5★ singles one album can
        # carry via the catalog/album-rank path; surplus (weakest by album
        # z-score) are demoted to the 4★ Single Floor.
        if album_model.get("has_benchmark"):
            _rules, _, _ = _live_album_scaling()
            max_slots = int(
                album_model.get("max_5star_slots")
                or _rules["peak"]["max_5star_slots"]
            )
            slot_tracks = [
                t for t in album_results
                if t.get("_era_5star") and int(t.get("stars") or 0) == 5
            ]
            if len(slot_tracks) > max_slots:
                slot_tracks.sort(
                    key=lambda t: _compute_album_z(
                        float(t.get("popularity_score") or 0), album_scores
                    )[0],
                    reverse=True,
                )
                for t in slot_tracks[max_slots:]:
                    t["stars"] = 4
                    _tid = str(t.get("track_id") or "")
                    if _tid:
                        try:
                            cursor.execute(
                                "UPDATE tracks SET stars = 4 WHERE id = %s",
                                (_tid,),
                            )
                        except Exception as exc:
                            logger.debug(
                                "[finalise_stage] 5★ cap demote failed for %s: %s", _tid, exc,
                            )
                    logger.info(
                        "[finalise_stage] %s - %s 5★ → 4★ (era slot cap %d)",
                        artist, t.get("title"), max_slots,
                    )

        conn.commit()

        # ── Per-album tabular summary (dashboard unified log) ─────
        # One clean table per album: rating, track title, album z-score,
        # popularity score, Last.fm listeners and single confidence.
        try:
            star_counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
            for t in album_results:
                s = int(t.get("stars") or 0)
                if 1 <= s <= 5:
                    star_counts[s] += 1

            # A popularity-only pass did NOT run single detection, so the
            # stale single flags in the DB must not drive the per-album output.
            _pop_only = bool(options.get("popularity_only"))
            singles_detected = [] if _pop_only else [t for t in album_results if t.get("is_single")]
            if singles_detected:
                log_unified(
                    f"Singles Detection - Detected {len(singles_detected)} single(s) in '{album}'"
                )

            # Compilation / Best-Of albums evaluate the z-score against the
            # artist's catalogue — the same reference the star bands used.
            _ref_scores = artist_scores if is_compilation else album_scores

            rows: list[dict[str, Any]] = []
            for t in album_results:
                t_stars = int(t.get("stars") or 0)
                t_conf = str(t.get("single_confidence") or "low").lower()
                t_title = str(t.get("title") or "Unknown").strip()
                t_score = float(t.get("popularity_score") or t.get("final_score") or 0)
                album_z, _ = _compute_album_z(t_score, _ref_scores)

                # Confidence note: which sources actually confirmed it.
                note = ""
                try:
                    sources = t.get("single_sources") or ""
                    if isinstance(sources, str):
                        parsed = json.loads(sources) if sources.strip() else []
                    else:
                        parsed = sources
                    if isinstance(parsed, list) and parsed:
                        matched_sources = [
                            str(s.get("source") or "") for s in parsed
                            if isinstance(s, dict) and bool(s.get("matched"))
                        ]
                        if matched_sources:
                            note = " (" + ", ".join(
                                s.replace("_", " ").title() for s in matched_sources[:2]
                            ) + ")"
                except Exception:
                    pass
                if not note and t_stars == 5 and t_conf == "low":
                    note = " (Standout)"

                rows.append({
                    "stars": max(1, min(t_stars, 5)),
                    "title": t_title[:34],
                    "z": album_z,
                    "score": t_score,
                    "lf": _fmt_count(t.get("lastfm_listeners")),
                    "conf": t_conf.upper() if t_conf in ("high", "medium") else "LOW",
                    "note": note,
                })
            rows.sort(key=lambda r: (-r["stars"], -r["score"]))

            log_unified("=" * 80)
            log_unified(
                f"📊 SCAN RESULTS: {str(artist or '').strip()} — {str(album or '').strip()} "
                f"({len(album_results)} Tracks)"
            )
            log_unified("=" * 80)
            log_unified(
                f"{'RATING':<7} {'TRACK TITLE':<34} {'Z-SCORE':>7} {'SCORE':>6} "
                f"{'LF LISTENS':>10}  SINGLE CONF"
            )
            log_unified("-" * 80)
            for r in rows:
                star_str = "★" * r["stars"] + "☆" * (5 - r["stars"])
                log_unified(
                    f"{star_str:<7} {r['title']:<34} {r['z']:>+7.2f} {r['score']:>6.1f} "
                    f"{r['lf']:>10}  {r['conf']}{r['note']}"
                )
            log_unified("-" * 80)
            log_unified(
                f"⭐ Distribution: 5★: {star_counts[5]} | 4★: {star_counts[4]} | "
                f"3★: {star_counts[3]} | 2★: {star_counts[2]} | 1★: {star_counts[1]}"
            )
            log_unified("=" * 80)
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
            # Surface successful syncs at INFO — a per-album summary line so
            # the album scan log shows Navidrome output (previously silent).
            if _synced > 0:
                log_unified(f"🔗 Navidrome: synced {_synced} rating(s) for '{artist}'")
            if _failed > 0:
                logger.warning(
                    "[finalise_stage] %d/%d Navidrome rating syncs failed for %s — check credentials / Subsonic API",
                    _failed, _attempted, str(artist or "").strip(),
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

def _sync_isrc_popularity(cursor) -> int:
    """Sync popularity stats across tracks sharing the same ISRC.

    A recording is a unique performance — every track in the library that
    carries the same ISRC is the same recording regardless of how its file
    tags spelled the title/artist.  After a scan completes, any duplicate
    row inherits the HIGHEST popularity score and single status found for
    that ISRC, so a badly-tagged copy can never drag the shared recording's
    scores down.  ``final_score`` and ``popularity`` are written in lockstep
    everywhere, so both columns are synced together.
    """
    try:
        cursor.execute(
            """
            UPDATE tracks
            SET popularity = subquery.max_score,
                final_score = subquery.max_score,
                is_single = subquery.max_single_status
            FROM (
                SELECT isrc,
                       MAX(COALESCE(popularity, final_score)) AS max_score,
                       MAX(CASE WHEN is_single THEN 1 ELSE 0 END) = 1 AS max_single_status
                FROM tracks
                WHERE isrc IS NOT NULL AND isrc != ''
                GROUP BY isrc
            ) AS subquery
            WHERE tracks.isrc = subquery.isrc
              AND COALESCE(tracks.popularity, tracks.final_score) < subquery.max_score
            """
        )
        return cursor.rowcount
    except Exception as exc:
        logger.debug("[finalise_stage] ISRC popularity sync failed: %s", exc)
        return 0


def finalise_scan(*, results: list[dict[str, Any]], options: dict[str, Any]) -> None:
    """Finalise the scan: assign star ratings, sync to Navidrome, create playlists, log summary.

    ``results`` is a list of per-track result dicts produced by ``track_stage``.
    Each dict should contain at minimum:
        track_id, artist, album, title, popularity_score,
        lastfm_listeners, listenbrainz_listens,
        is_single, single_confidence, is_live, album_context_live
    """
    track_count = len(results) if results else 0
    log_unified(f"[FINALISE_STAGE] Finalising scan — {track_count} tracks processed")
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
                [
                    float(r.get("popularity_score") or 0)
                    for r in artist_results
                    if float(r.get("popularity_score") or 0) > 0
                    and not bool(r.get("exclude_from_stats"))
                ],
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

            # Create/refresh the artist's Essential Collection .m3u — scanned
            # against the FULL DB track history, deduplicated by normalized
            # title, and only written when > 12 unique 4★/5★ tracks exist.
            # Gated by config (playlists.essential_playlists_enabled) or an
            # explicit pipeline override (options.create_playlists).
            if _essential_playlists_enabled(options):
                _create_essential_m3u(artist, cursor)

        # ── ISRC popularity sync (recording-level inheritance) ─────────────
        # Runs once per scan, after every track is persisted: duplicate rows
        # sharing an ISRC inherit the strongest popularity/single evidence so
        # the recording is scored identically everywhere.
        try:
            _isrc_updated = _sync_isrc_popularity(cursor)
            conn.commit()
            if _isrc_updated:
                log_unified(
                    f"[FINALISE_STAGE] ISRC sync: {_isrc_updated} track(s) inherited higher popularity across shared ISRCs"
                )
        except Exception as exc:
            logger.debug("[finalise_stage] ISRC sync commit failed: %s", exc)

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
    log_unified(f"[FINALISE_STAGE] Star ratings assigned: {total_star_ratings}")
    log_unified(f"[FINALISE_STAGE] Navidrome syncs: {navidrome_synced}")

    star_counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for r in results:
        s = r.get("stars", 0) or 0
        if 1 <= s <= 5:
            star_counts[s] += 1
    log_unified(
        f"[FINALISE_STAGE] Star distribution — 5★: {star_counts[5]}, 4★: {star_counts[4]}, 3★: {star_counts[3]}, 2★: {star_counts[2]}, 1★: {star_counts[1]}",
    )

    return None

