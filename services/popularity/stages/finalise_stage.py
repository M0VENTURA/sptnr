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
from db.utils import row_get
from services.popularity.popularity_math import (
    age_skew_multiplier,
    apply_album_relative_popularity,
    calculate_robust_zscore,
    effective_album_ratio,
    fmt_count as _fmt_count,
)
from services.popularity.popularity_zscore import composite_listener_z
from services.catalog.album_classification_service import (
    is_instrumental_track_title,
    is_live_or_alternate_track_title,
)

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
        "star4_artist_z": _tier("star_4", "artist_z", 1.0),
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


def _resolve_navidrome_artist_id(artist: str) -> str | None:
    """Return the real Navidrome artist id for ``artist``, or None.

    ``artist_stats.artist_id`` is the PRIMARY KEY — writing the artist NAME
    into it (as an earlier version of this stage did) pollutes the table so
    ``lookup_artist_id`` returns the name and the Navidrome import then calls
    ``getArtist?id=<name>``, which returns no albums and silently skips the
    import.  Prefer an existing real id (a name-keyed row is never a real id),
    then fall back to the tracks table's stored Navidrome id.
    """
    from sqlalchemy import text as _text
    from db.engine import db_session as _db_session

    try:
        with _db_session() as session:
            row = session.execute(
                _text(
                    "SELECT artist_id FROM artist_stats "
                    "WHERE LOWER(artist_name) = LOWER(:artist) "
                    "  AND LOWER(artist_id) <> LOWER(:artist) "
                    "LIMIT 1"
                ),
                {"artist": artist},
            ).fetchone()
            found = row_get(row, "artist_id") if row else None
            if found and str(found).strip() and str(found).casefold() != str(artist).casefold():
                return str(found).strip()
    except Exception as exc:
        logger.debug("[finalise_stage] artist_stats id lookup failed for %s: %s", artist, exc)
    try:
        with _db_session() as session:
            row = session.execute(
                _text(
                    "SELECT artist_id FROM tracks "
                    "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist "
                    "  AND artist_id IS NOT NULL AND artist_id <> '' "
                    "  AND LOWER(artist_id) <> LOWER(:artist) "
                    "LIMIT 1"
                ),
                {"artist": artist},
            ).fetchone()
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

    Instrumental tracks can never qualify: a legacy row that predates the
    single-detection instrumental gate may still carry the flag, so the
    verdict is re-checked here (same exclusion the scan path applies).
    """
    if is_instrumental_track_title(str(track.get("title") or "")):
        return False
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
    artist_scores: list[float] | None = None,
) -> int:
    """Album-relative 1-4★ rating from the z-score bands.

    Spec rule 4: the album's tracks are ranked by popularity and sliced into
    z-score bands — top of the album (Z >= +0.5) → 4★, the standard middle
    (-0.5 <= Z < +0.5) → 3★, the lower band (-1.2 <= Z < -0.5) → 2★, and
    bottom outliers (Z < -1.2) → 1★.

    When ``artist_scores`` is provided, the 4★ band ALSO requires the track
    to clear the artist-catalogue z minimum (``star_4.artist_z``, default
    1.0) — 4★ reflects absolute catalogue prominence, not just "best track
    on this specific record".  A track that tops a low-prominence album but
    sits below the artist-catalogue z gate falls to 3★.  Without artist
    context (compilation tracks, tiny catalogues) the pure album band stands
    (legacy parity).

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
    # 4★ artist-z hard minimum: the track must also be a catalogue standout
    # (``star_4.artist_z``) when a REAL artist catalogue exists.  The gate
    # only fires when the artist reference is a distinct, wider catalogue
    # (more valid scores than the album reference) — when the artist reference
    # IS the album (single-album artist / tiny catalogue) the pure album band
    # stands, so 4★ never double-gates against the same distribution.  Only
    # the 4★ band is gated — 3★/2★/1★ stay purely album-relative.
    artist_eligible_4star = True
    if artist_scores:
        valid_artist = [float(s) for s in artist_scores if float(s or 0) > 0]
        if len(valid_artist) >= 5 and len(valid_artist) > len(valid):
            artist_z, artist_spread = _compute_artist_z(score, valid_artist)
            artist_eligible_4star = artist_z >= th["star4_artist_z"] - _star_epsilon_z(artist_spread, th["epsilon"])
    if album_z >= th["star4_album_z"] - epsilon and artist_eligible_4star:
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
) -> dict[str, Any]:
    """3-step scaling model context for ONE album.

    Step 1 — M_peak: the highest album-median across the artist's catalogue.
        Prefers a RAW-LISTENER prominence benchmark (per-album median of the
        log-scaled LF/LB blend) because the album-relative ``final_score``
        values erase cross-album magnitude — every album's median re-anchors
        to ~50, so R_eff computed from them collapses every album onto
        ``era=peak`` regardless of raw listener volume.  Falls back to the
        re-anchored score medians when no listener data is stored (legacy
        rows / test fixtures).
    Step 2 — A_skew: age-skew multiplier from the album's release year vs
        the peak album's year (fresh releases / legacy albums boosted).
    Step 3 — R_eff = min(1.0, album_median * A_skew / M_peak).
    Step 4 — era rules: catalog top-% cutoff, album top-N and the 5★ slot cap.

    Returns ``{}`` when no benchmark can be derived (empty catalogue / no
    scores) — the caller then keeps the legacy single-→-5★ behaviour.
    """
    try:
        from sqlalchemy import text as _text
        from db.engine import db_session as _db_session
        from services.popularity.popularity_math import (
            album_prominence_score,
            album_prominence_median,
        )
        current_album = str(album_results[0].get("album") or "")
        scanned_titles = {
            str(r.get("title") or "").strip().lower() for r in album_results
        }

        with _db_session() as session:
            rows = session.execute(
                _text(
                    "SELECT title, album, final_score, year, "
                    "COALESCE(lastfm_listeners, 0) AS lastfm_listeners, "
                    "COALESCE(listenbrainz_listens, 0) AS listenbrainz_listens "
                    "FROM tracks "
                    "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND final_score > 0"
                ),
                {"artist": artist},
            ).fetchall() or []

        by_album: dict[str, list[float]] = {}
        album_years: dict[str, int] = {}
        prominence_by_album: dict[str, list[float]] = {}
        for row in rows:
            _title = str(row_get(row, "title") or "").strip().lower()
            if _title in scanned_titles:
                continue
            _album = str(row_get(row, "album") or "")
            _score = float(row_get(row, "final_score") or 0)
            if _score > 0:
                by_album.setdefault(_album, []).append(_score)
            _lf = int(row_get(row, "lastfm_listeners") or 0)
            _lb = int(row_get(row, "listenbrainz_listens") or 0)
            if _lf > 0 or _lb > 0:
                prominence_by_album.setdefault(_album, []).append(
                    album_prominence_score(_lf, _lb)
                )
            _year = int(row_get(row, "year") or 0)
            if _year > 0:
                album_years.setdefault(_album, _year)

        # Fresh scan scores for the current album (not yet stored) join its
        # group; years fall back to the scan's track fields.  The fresh
        # results also carry the raw listener counts, so the current album's
        # prominence (if any) joins the prominence benchmark too.
        for r in album_results:
            _score = float(r.get("popularity_score") or 0)
            if _score > 0 and not bool(r.get("exclude_from_stats")):
                by_album.setdefault(current_album, []).append(_score)
            _lf = int(r.get("lastfm_listeners") or 0)
            _lb = int(r.get("listenbrainz_listens") or 0)
            if _lf > 0 or _lb > 0:
                prominence_by_album.setdefault(current_album, []).append(
                    album_prominence_score(_lf, _lb)
                )
            _year = int(r.get("year") or r.get("release_year") or 0)
            if _year > 0:
                album_years.setdefault(current_album, _year)

        # ── Prominence benchmark (preferred) ─────────────────────────────
        # Raw-listener prominence preserves CROSS-ALBUM magnitude: Mer de
        # Noms (median ~700k listeners) scores ~86 while Eat the Elephant
        # (median ~80k) scores ~70.  The log scale compresses a 10x listener
        # gap to ~16 points, so the raw ratio (0.82) would still clear the
        # peak-era 0.75 boundary — the ratio is therefore power-amplified
        # (R_eff = ratio^k) so a 10x gap (0.82^2 = 0.67) lands in ``solid``
        # and a ~20x gap (0.68^2 = 0.46) approaches ``minor``.  Requires
        # >= 2 albums with listener data so the catalogue peak is meaningful.
        prominence_medians: dict[str, float] = {
            _album: album_prominence_median(_scores)
            for _album, _scores in prominence_by_album.items()
            if album_prominence_median(_scores) > 0
        }
        use_prominence = len(prominence_medians) >= 2
        prominence_power = 2.0
        if use_prominence:
            m_peak = max(prominence_medians.values())
            current_median = prominence_medians.get(current_album) or m_peak
            peak_album = max(prominence_medians, key=prominence_medians.get)
        else:
            # ── Score-based fallback ──────────────────────────────────────
            # Re-anchor each album against its own distribution so the
            # medians sit on the same (album-relative) scale.  This flattens
            # cross-album magnitude (every median → ~50), so it is only used
            # when no listener data exists to benchmark against.
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
        if use_prominence:
            # Power-amplified prominence ratio: R_eff = min(1.0, (median/M_peak)^k).
            # The log-scale prominence compresses a 10x listener gap to ~0.82
            # raw ratio, which would still clear the peak-era 0.75 boundary;
            # squaring (k=2) makes a 10x gap land in ``solid`` and a ~20x gap
            # approach ``minor`` while a genuine peak stays at 1.0.
            raw_ratio = min(1.0, max(0.0, float(current_median) / float(m_peak)))
            reff = min(1.0, max(0.0, raw_ratio ** float(prominence_power)))
        else:
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
            "benchmark_source": "prominence" if use_prominence else "scores",
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
    artist_listen_distribution: list[float] | None = None,
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
    # Instrumental versions can never be 5★ via the popularity marking path
    # (the runner clears the flag for them, but a legacy/standalone caller
    # may still carry it) — the era caps should bounce them to 4★.
    if is_instrumental_track_title(str(track.get("title") or "")):
        popularity_marked = False

    # ── 5★: singles + standouts only ──
    # GLOBAL-FIRST 5★ POOL: the scan runner's artist-section pre-pass locked
    # the catalog's top tracks (by RAW cross-album score, order-independent)
    # into a protected 5★ pool.  A locked track is the artist's biggest hit —
    # it keeps 5★ regardless of its album's per-album era gate or the 5★ slot
    # cap (which otherwise demote late-processed strong tracks: Battle Beast's
    # Eden, the catalog #1 by raw score, was demoted to 4★ by its album's
    # era/slot gating while lower-scored tracks on earlier albums kept 5★).
    # The lock still respects the organic floor (a locked track with no real
    # audience is not a hit) and the live cap.
    if track.get("_global_5star_locked") and not popularity_only:
        if not is_live and organic:
            track["_global_5star_locked"] = True
            return 5

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

    # ── Hard absolute floors (failsafe vs within-album normalization) ────
    # A track in the artist's absolute top-N% by RAW Last.fm listeners is
    # undeniable, regardless of how tight its own album's distribution is
    # (the "consistent album" paradox: every L.D. 50 track is popular, so
    # Dig's album_z looks ordinary).  Config:
    #   artist_top_percentile_force_5_star: 0.03  (top 3% → 5★)
    #   artist_top_percentile_force_4_star: 0.10  (top 10% → at least 4★)
    # These bypass album_z gating but NEVER the live cap or user override.
    # Instrumental versions are ALSO excluded: a massive instrumental must
    # not force its way to 5★ through raw listens any more than through the
    # standout / marking paths — the era caps should bounce it to 4★.
    _force_stars: int | None = None
    if (
        not is_live
        and not is_instrumental_track_title(str(track.get("title") or ""))
        and artist_listen_distribution
        and not popularity_only
    ):
        try:
            from services.popularity.popularity_config import get_artist_force_star_percentiles
            _force5_pct, _force4_pct = get_artist_force_star_percentiles()
            _track_listens = float(track.get("lastfm_listeners") or 0)
            if _track_listens > 0 and (_force5_pct > 0 or _force4_pct > 0):
                _valid = [float(v) for v in artist_listen_distribution if float(v or 0) > 0]
                if len(_valid) >= 5:
                    _valid.sort(reverse=True)
                    _rank = sum(1 for v in _valid if v > _track_listens) + 1
                    _pct = _rank / len(_valid)
                    if _force5_pct > 0 and _pct <= _force5_pct:
                        _force_stars = 5
                    elif _force4_pct > 0 and _pct <= _force4_pct:
                        _force_stars = 4
        except Exception:
            _force_stars = None
    if _force_stars is not None and not is_live:
        track["_force_floor"] = _force_stars
        return _force_stars

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
                band = _album_z_band_star(score, ref_scores, artist_scores=artist_scores)
                return max(band, 4) if organic else min(band, 3)
            # Marked-only tracks below the era bar (medium-bumped singles
            # ranked 10-20% on a minor-era album) fall through to the band.
            return _album_z_band_star(score, ref_scores, artist_scores=artist_scores)

        return 5

    # ── 1-4★: album-relative z-score base ──
    return _album_z_band_star(score, ref_scores, artist_scores=artist_scores)


# ---------------------------------------------------------------------------
# Navidrome sync
# ---------------------------------------------------------------------------

def _sync_rating_to_navidrome(track_id: str, stars: int, clients: list[Any] | None = None) -> bool:
    """Push a single track rating to Navidrome.

    Delegates to ``services.navidrome.rating_sync_service`` — the single
    implementation using the Navidrome client (token auth).  The old raw-HTTP
    copy sent plaintext ``u``/``p`` credentials with a hardcoded API version;
    it was removed so the two paths can never drift.  ``sync_ratings_to_all_users``
    (default off) controls whether every configured user is updated; when off
    only the primary user is.

    ``clients`` lets the album-level caller reuse ONE set of Navidrome clients
    across all of the album's tracks (built via ``_build_rating_sync_clients``)
    instead of reconstructing a client — and reloading config — per track.
    When omitted, the clients are built per call (fallback for direct callers).
    """
    try:
        from services.navidrome.rating_sync_service import (
            sync_track_rating_to_navidrome,
            sync_track_rating_with_clients,
        )
        if clients is None:
            return sync_track_rating_to_navidrome(track_id, stars)
        return sync_track_rating_with_clients(clients, track_id, stars)
    except Exception:
        return False


def _build_rating_sync_clients() -> list[Any]:
    """Build the Navidrome rating-sync clients once per album.

    ``_sync_rating_to_navidrome`` used to reload the configured user list and
    construct a fresh ``NavidromeClient`` per track per user.  Building them
    once per album removes that per-track config load / client churn while
    keeping the exact same user list and multi-user behaviour.
    """
    try:
        from services.navidrome.rating_sync_service import get_rating_sync_clients
        return get_rating_sync_clients()
    except Exception:
        return []


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

# Featured-artist markers in track artist fields ("Powerwolf feat. Alissa").
_ESSENTIAL_FEAT_RE = re.compile(r"\s+(?:feat\.?|featuring|ft\.?)\s+", re.IGNORECASE)
# Collab separators inside a feat. guest list ("A & B", "A, B", "A x B").
_ESSENTIAL_FEAT_SPLIT_RE = re.compile(
    r"\s*(?:&|,|/|\+|\bx\b|\bvs\.?\b)\s*", re.IGNORECASE
)


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


def _essential_strip_guest_credit(artist: str) -> str:
    """Primary artist of a feat.-credited field, guest side only.

    Strips ONLY explicit feat./ft./featuring credits ("Apocalyptica feat.
    Ville Valo & Lauri Ylönen" → "Apocalyptica").  Deliberately does NOT
    strip "&" / "with" / "and" — a genuine two-artist collaboration
    ("Metallica & San Francisco Symphony") is its own project and must not
    be folded onto either partner.  Mirrors ``_ESSENTIAL_FEAT_RE`` so the
    primary side and the guest side (``_track_has_featured_artist``) use the
    same credit markers.
    """
    match = _ESSENTIAL_FEAT_RE.search(str(artist or ""))
    if not match:
        return str(artist or "").strip()
    return str(artist or "")[: match.start()].strip()


def _refresh_all_essential_collections() -> int:
    """(Re)generate essential collections for every qualifying artist.

    Runs when a scan produced no per-track results (e.g. a singles-only scan
    where every album was already assessed and skipped): essential collections
    are DB-driven, so they can be refreshed from stored ratings alone without
    any per-track results.  Returns the number of artists processed.
    """
    from sqlalchemy import text as _text
    from db.engine import db_session as _db_session

    artists: list[str] = []
    try:
        with _db_session() as session:
            result = session.execute(_text("""
                SELECT DISTINCT TRIM(COALESCE(NULLIF(album_artist, ''), artist)) AS artist
                FROM tracks
                WHERE COALESCE(stars, star_rating) >= 4
                  AND TRIM(COALESCE(NULLIF(album_artist, ''), artist)) <> ''
            """))
            artists = [str(r[0]) for r in result.fetchall() or [] if r and r[0]]
    except Exception as exc:
        logger.debug("[finalise_stage] Essential collection artist scan failed: %s", exc)
        return 0

    # Collapse feat.-credited artist names onto their primary artist
    # ("Apocalyptica feat. Ville Valo & Lauri Ylönen" → "Apocalyptica") so the
    # DB-driven refresh keys on the same artist identity as the scan path and
    # never spawns a separate tiny collection for the guest suffix.
    seen: set[str] = set()
    unique_artists: list[str] = []
    for artist in artists:
        primary = _essential_strip_guest_credit(artist) or artist
        key = primary.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique_artists.append(primary)

    refreshed = 0
    for artist in unique_artists:
        if _is_excluded_essential_artist(artist):
            continue
        try:
            _create_essential_m3u(artist)
            refreshed += 1
        except Exception as exc:
            logger.debug("[finalise_stage] Essential collection refresh failed for %s: %s", artist, exc)
    return refreshed


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


# Rolling "New Music" playlist: only written once this many 4★/5★ tracks
# qualify, and capped at this many (newer additions push older out).
_NEW_MUSIC_MIN_TRACKS = 100
_NEW_MUSIC_MAX_TRACKS = 100


def _new_music_playlist_enabled() -> bool:
    """Whether the library-wide ``New Music.m3u`` generation is on.

    Config key ``playlists.new_music_playlist_enabled`` (default true): a
    rolling playlist of the most recently ADDED (to Navidrome) 4★/5★ tracks.
    """
    try:
        from helpers.config_helpers import get_config
        cfg = (get_config() or {}).get("playlists") or {}
        return bool(cfg.get("new_music_playlist_enabled", True))
    except Exception:
        return True


def _create_new_music_playlist() -> int:
    """Build/refresh the library-wide ``New Music.m3u`` playlist.

    Rolling "recently added" playlist: the 4★/5★ tracks most recently added
    to Navidrome (``tracks.updated_at`` — set once at import, never bumped by
    scans), newest first, deduplicated by normalized title (the newest copy
    wins).  The playlist is only written once at least
    ``_NEW_MUSIC_MIN_TRACKS`` qualify and is capped at
    ``_NEW_MUSIC_MAX_TRACKS``, so every scan a newer addition pushes the
    oldest entry out.  Below the threshold any existing file is removed.

    Returns the number of tracks written (0 when skipped/removed).
    """
    from sqlalchemy import text as _text
    from db.engine import db_session as _db_session

    playlists_dir = _essential_playlists_dir()
    file_path = os.path.join(playlists_dir, f"{_sanitize_name('New Music')}.m3u")

    rows: list[Any] = []
    try:
        with _db_session() as session:
            result = session.execute(
                _text("""
                    SELECT id, title, file_path, duration,
                           COALESCE(NULLIF(album_artist, ''), artist) AS artist,
                           COALESCE(stars, star_rating) AS stars,
                           updated_at
                    FROM tracks
                    WHERE COALESCE(stars, star_rating) >= 4
                      AND file_path IS NOT NULL AND TRIM(file_path) <> ''
                    ORDER BY updated_at DESC NULLS LAST
                    LIMIT :max_rows
                """),
                # Headroom so the dedupe below can't starve the cap.
                {"max_rows": _NEW_MUSIC_MAX_TRACKS * 5},
            )
            rows = [dict(r._mapping) for r in result.fetchall() or []]
    except Exception as exc:
        logger.debug("[finalise_stage] New Music fetch failed: %s", exc)
        return 0

    # Dedupe by (artist, normalized title) — rows are newest-first, so the
    # first copy per key is the newest one and wins.
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (
            str(row.get("artist") or "").strip().casefold(),
            _normalise_essential_title(str(row.get("title") or "")),
        )
        if not key[1]:
            continue
        grouped.setdefault(key, []).append(row)
    winners = [group[0] for group in grouped.values()]

    if len(winners) < _NEW_MUSIC_MIN_TRACKS:
        # Not enough qualifying tracks yet — remove any stale playlist.
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.info(
                    "[finalise_stage] Removed New Music playlist (only %d qualifying tracks)",
                    len(winners),
                )
        except Exception as exc:
            logger.debug("[finalise_stage] New Music removal failed: %s", exc)
        return 0

    winners = winners[:_NEW_MUSIC_MAX_TRACKS]
    os.makedirs(playlists_dir, exist_ok=True)
    lines = ["#EXTM3U"]
    for row in winners:
        try:
            duration = int(float(row.get("duration") or 0) or 0)
        except (TypeError, ValueError):
            duration = 0
        lines.append(f"#EXTINF:{duration},{row.get('artist')} - {row.get('title')}")
        lines.append(str(row.get("file_path") or row.get("title") or ""))
    try:
        with open(file_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        logger.info("[finalise_stage] New Music playlist written (%d tracks)", len(winners))
        log_unified(f"📄 Playlist: Generated 'New Music.m3u' ({len(winners)} tracks)")
        # Sync the Navidrome playlist IN PLACE (update, never recreate) so
        # scans do not leave duplicate entries — the rolling cap pushes older
        # tracks out and the newest stay, ordered by recency.
        try:
            _song_ids = [
                str(r.get("id") or "") for r in winners
                if str(r.get("id") or "").strip()
            ]
            if _song_ids:
                _sync_playlist_to_navidrome("New Music", _song_ids)
        except Exception:
            pass
        return len(winners)
    except Exception as exc:
        logger.warning("[finalise_stage] New Music playlist write failed: %s", exc)
        return 0


def _essential_include_featured_enabled() -> bool:
    """Whether featured-appearance tracks join other artists' essentials.

    Config key ``playlists.essential_include_featured`` (default true): a
    4★/5★ "Powerwolf feat. Unleash The Archers" track also lands on Unleash
    The Archers' Essential Collection.
    """
    try:
        from helpers.config_helpers import get_config
        cfg = (get_config() or {}).get("playlists") or {}
        return bool(cfg.get("essential_include_featured", True))
    except Exception:
        return True


def _track_has_featured_artist(artist_field: str, target_artist: str) -> bool:
    """Whether a feat.-credited track's guest list includes ``target_artist``.

    ``artist_field`` is the track artist ("Powerwolf feat. Unleash The
    Archers"); the guest side after the marker is split on collab separators
    and compared case-/whitespace-insensitively against the playlist owner.
    Returns False when the field carries no feat marker or no guest matches.
    """
    if not target_artist:
        return False
    match = _ESSENTIAL_FEAT_RE.search(artist_field or "")
    if not match:
        return False
    target_key = re.sub(r"\s+", " ", target_artist).strip().casefold()
    guests = _ESSENTIAL_FEAT_SPLIT_RE.split(artist_field[match.end():])
    return any(
        re.sub(r"\s+", " ", g).strip().casefold() == target_key
        for g in guests
        if g and g.strip()
    )


def _fetch_essential_featured_rows() -> list[dict[str, Any]]:
    """All 4★/5★ feat.-credited tracks (pool for featured-artist essentials).

    The feat-track pool is artist-INDEPENDENT — the per-artist filter (does
    this artist appear in the guest list?) runs in ``_track_has_featured_artist``.
    Fetching it once per scan and reusing it for every artist's essential
    collection avoids the O(artists × library) re-query the per-artist path
    used to issue (the ``artist ILIKE '%feat%'`` predicate has no usable
    index and full-scans the tracks table each time).
    """
    from sqlalchemy import text as _text
    from db.engine import db_session as _db_session

    try:
        with _db_session() as session:
            result = session.execute(
                _text("""
                    SELECT id, title, file_path, duration,
                           COALESCE(stars, star_rating) AS stars,
                           COALESCE(is_live, 0) AS is_live,
                           COALESCE(is_compilation, 0) AS is_compilation,
                           COALESCE(popularity, final_score, 0) AS popularity_score,
                           year, release_year, artist
                    FROM tracks
                    WHERE COALESCE(stars, star_rating) >= 4
                      AND (
                          artist ILIKE '% feat %' OR artist ILIKE '% feat.%'
                          OR artist ILIKE '%feat.%' OR artist ILIKE '%featuring%'
                          OR artist ILIKE '% ft %' OR artist ILIKE '% ft.%'
                      )
                """),
            )
            return [dict(r._mapping) for r in (result.fetchall() or [])]
    except Exception as exc:
        logger.debug("[finalise_stage] Featured-track fetch failed: %s", exc)
        return []


def _create_essential_m3u(artist: str, featured_rows: list | None = None) -> None:
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

    When ``playlists.essential_include_featured`` is enabled, 4★/5★ tracks
    where the artist is a FEATURED guest ("Powerwolf feat. Unleash The
    Archers") join the featured artist's collection too — the track then
    appears on both bands' essential playlists.

    ``featured_rows`` is an optional pre-fetched feat-track pool (see
    ``_fetch_essential_featured_rows``); callers that build many collections
    in one scan pass it so the library-wide feat query runs once, not once per
    artist.
    """
    from sqlalchemy import text as _text
    from db.engine import db_session as _db_session

    if _is_excluded_essential_artist(artist):
        return

    # A feat.-credited artist section ("Apocalyptica feat. Ville Valo & Lauri
    # Ylönen") is the SAME artist as its primary ("Apocalyptica") for essential-
    # collection purposes: the feat. track belongs on the primary artist's
    # collection, and no separate tiny collection is spawned for the guest
    # suffix.  Normalise up-front so the playlist name, file name and the
    # ``done``-set dedup all key on the primary artist.  Only feat./ft./
    # featuring credits are stripped — genuine "&"/"with" collaborations are
    # their own projects and keep their own artist identity.
    artist = _essential_strip_guest_credit(artist) or artist
    if _is_excluded_essential_artist(artist):
        return

    playlists_dir = _essential_playlists_dir()
    playlist_name = _essential_playlist_name(artist)
    file_path = os.path.join(playlists_dir, f"{_sanitize_name(playlist_name)}.m3u")

    rows: list[Any] = []
    try:
        with _db_session() as session:
            result = session.execute(
                _text("""
                    SELECT id, title, file_path, duration,
                           COALESCE(stars, star_rating) AS stars,
                           COALESCE(is_live, 0) AS is_live,
                           COALESCE(is_compilation, 0) AS is_compilation,
                           COALESCE(popularity, final_score, 0) AS popularity_score,
                           year, release_year, artist, album_artist
                    FROM tracks
                    WHERE COALESCE(stars, star_rating) >= 4
                      AND (
                          LOWER(TRIM(COALESCE(NULLIF(album_artist, ''), artist))) = LOWER(TRIM(:artist))
                          OR LOWER(TRIM(COALESCE(NULLIF(album_artist, ''), artist))) LIKE LOWER(TRIM(:artist)) || ' %'
                      )
                """),
                {"artist": artist},
            )
            rows = [dict(r._mapping) for r in result.fetchall() or []]
    except Exception as exc:
        logger.debug("[finalise_stage] Essential collection fetch failed for %s: %s", artist, exc)

    # Keep only rows that genuinely belong to this artist.  The SQL scopes rows
    # to ``artist`` exactly OR a ``artist %`` prefix (a cheap pre-filter for
    # feat.-credited rows like "Apocalyptica feat. Ville Valo & Lauri Ylönen").
    # Exact matches are kept as-is; prefixed rows are adopted ONLY when they
    # carry a feat./ft./featuring credit whose primary artist is this artist —
    # a different artist whose name merely starts with the same prefix
    # ("Apocalyptica Tribute Band") or a genuine "&" collaboration is never
    # folded in.  Rows with no artist field (test/legacy fixtures) are kept.
    _artist_lower = str(artist or "").strip().casefold()
    _kept: list[dict[str, Any]] = []
    for row in rows:
        stored = str(row.get("album_artist") or row.get("artist") or "").strip()
        if not stored:
            _kept.append(row)
            continue
        stored_key = stored.casefold()
        if stored_key == _artist_lower:
            _kept.append(row)
            continue
        if (
            _ESSENTIAL_FEAT_RE.search(stored)
            and _essential_strip_guest_credit(stored).casefold() == _artist_lower
        ):
            _kept.append(row)
    rows = _kept

    # Featured appearances: a "Powerwolf feat. Unleash The Archers" 4★/5★
    # track is stored under Powerwolf's album_artist, but the FEATURED band's
    # essential collection should carry it too.  Adopt the feat.-credited
    # rows whose guest list matches THIS artist (dedup by normalized title
    # below handles any overlap with the artist's own query).
    if _essential_include_featured_enabled():
        try:
            _feat_rows = (
                featured_rows
                if featured_rows is not None
                else _fetch_essential_featured_rows()
            )
            for row in _feat_rows:
                if _track_has_featured_artist(row.get("artist") or "", artist):
                    rows.append(row)
        except Exception as exc:
            logger.debug(
                "[finalise_stage] Featured-track fetch failed for %s: %s", artist, exc
            )

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
            # Sync the Navidrome playlist IN PLACE (update, never recreate) so
            # scans do not leave duplicate entries — old tracks that dropped
            # below 4★/5★ are removed, new tracks added, ordered by the same
            # popularity sort above.  Local track ids == Navidrome song ids.
            try:
                _song_ids = [
                    str(r.get("id") or "") for r in winners
                    if str(r.get("id") or "").strip()
                ]
                if _song_ids:
                    _sync_playlist_to_navidrome(playlist_name, _song_ids)
            except Exception:
                pass
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
    else:
        logger.info(
            "[finalise_stage] Essential collection for '%s' not created: only %d unique 4★/5★ tracks (min %d) — no existing file to remove",
            artist, len(winners), _ESSENTIAL_MIN_TRACKS,
        )


# ---------------------------------------------------------------------------
# Genre top-tracks playlists (library-wide, generated once per scan)
# ---------------------------------------------------------------------------


def _genre_playlists_enabled() -> bool:
    """Whether library-wide ``{Genre} - Top Tracks`` playlists are generated.

    Config key ``playlists.genre_playlists_enabled`` (default true).
    """
    try:
        from helpers.config_helpers import get_config
        cfg = (get_config() or {}).get("playlists") or {}
        return bool(cfg.get("genre_playlists_enabled", True))
    except Exception:
        return True


def _genre_playlists_delete_enabled() -> bool:
    """Whether under-threshold genre playlists are removed from disk.

    Config key ``playlists.genre_playlists_delete_enabled`` (default true).
    """
    try:
        from helpers.config_helpers import get_config
        cfg = (get_config() or {}).get("playlists") or {}
        return bool(cfg.get("genre_playlists_delete_enabled", True))
    except Exception:
        return True


def _genre_playlists_active() -> bool:
    """Whether any genre-playlist task (create or delete) is enabled."""
    return _genre_playlists_enabled() or _genre_playlists_delete_enabled()


def _genre_playlists_state_file() -> str:
    """Path to a small JSON state file tracking genre playlist filenames.

    Lets the stale-file cleanup forget files that were removed while the
    delete toggle was off (or under a previous name template) without ever
    touching unrelated playlists in the watch directory.
    """
    try:
        from helpers.config_helpers import get_state_directory
        return os.path.join(get_state_directory(), "genre_playlists.json")
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".popularr_genre_playlists.json")


def _load_genre_playlist_state() -> set[str]:
    """Return the set of genre playlist filenames written on previous scans."""
    try:
        with open(_genre_playlists_state_file(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return set(str(n) for n in (data or []) if n)
    except Exception:
        return set()


def _save_genre_playlist_state(names: set[str]) -> None:
    """Persist the current set of genre playlist filenames for next scan."""
    try:
        path = _genre_playlists_state_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(sorted(names), handle)
    except Exception:
        pass


def _genre_playlist_name(genre: str) -> str:
    """Resolve the genre playlist name from the config template.

    ``playlists.genre_playlists_name_template`` supports the ``{genre}``
    placeholder; default ``"{genre} - Top Tracks"``.
    """
    try:
        from helpers.config_helpers import get_config
        template = str(
            (get_config().get("playlists") or {}).get("genre_playlists_name_template")
            or "{genre} - Top Tracks"
        )
    except Exception:
        template = "{genre} - Top Tracks"
    return str(template).replace("{genre}", genre).strip() or f"{genre} - Top Tracks"


def _display_genre(genre: str) -> str:
    """'progressive metal' -> 'Progressive Metal' (avoiding '3Rd'-style caps)."""
    return " ".join(
        w.capitalize() if not (w and w[0].isdigit()) else w
        for w in str(genre or "").split()
    )


def _navidrome_clients() -> list[Any]:
    """Resolve Navidrome clients from config (all user shapes + env).

    Uses ``get_navidrome_users_normalized`` so ``user``/``pass``,
    ``username``/``password``, the legacy ``navidrome`` block and env vars all
    resolve — the raw-config loops used in older playlist code silently no-op
    when the credential keys differ, which is why playlist deletions "didn't
    work".
    """
    clients: list[Any] = []
    try:
        from helpers.config_helpers import get_navidrome_users_normalized
        from api_clients.navidrome import NavidromeClient
        for user in get_navidrome_users_normalized():
            base_url = (user.get("base_url") or "").strip()
            username = (user.get("user") or "").strip()
            password = (user.get("pass") or "").strip()
            if base_url and username and password:
                clients.append(NavidromeClient(base_url, username, password))
    except Exception as exc:
        logger.debug("[finalise_stage] Navidrome client resolution failed: %s", exc)
    return clients


def _delete_genre_playlist_from_navidrome(playlist_name: str) -> None:
    """Best-effort: remove a genre playlist from Navidrome by name.

    Deleting the ``.m3u`` from the watch folder only removes the file —
    Navidrome keeps the imported playlist in its DB, so an under-threshold
    genre playlist would linger in the UI.  Mirrors the legacy NSP-overwrite
    flow (``find_playlist_by_name`` + ``deletePlaylist``).  Warnings are
    logged when the playlist is not found so a silent delete failure is
    diagnosable; the file deletion remains the source of truth.
    """
    if not playlist_name:
        return
    try:
        for client in _navidrome_clients():
            playlist = client.find_playlist_by_name(playlist_name)
            if playlist and playlist.get("id") and client.delete_playlist(str(playlist["id"])):
                logger.info("[finalise_stage] Deleted Navidrome playlist '%s'", playlist_name)
                return
        logger.warning(
            "[finalise_stage] Navidrome playlist '%s' not found for deletion (file already removed)",
            playlist_name,
        )
    except Exception as exc:
        logger.warning(
            "[finalise_stage] Navidrome playlist delete failed for %s: %s",
            playlist_name, exc,
        )


def _sweep_orphaned_genre_playlists_from_navidrome() -> None:
    """Self-healing sweep: remove Navidrome genre playlists whose ``.m3u``
    file is no longer on disk.

    The file cleanup only deletes a Navidrome playlist for the exact file it
    removed in the same run — if that API call failed (or the file was
    removed out-of-band earlier), the imported playlist lingers forever.  This
    sweep walks Navidrome's playlists, matches names to the genre template
    (the configured suffix, e.g. " - Top Tracks") and deletes any whose
    ``.m3u`` is missing from the watch directory.  Gated by
    ``playlists.genre_playlists_delete_enabled``; best-effort, never raises.
    """
    try:
        if not _genre_playlists_delete_enabled():
            return
        suffix = _sanitize_name(_genre_playlist_name("GENRE")).replace("GENRE", "", 1).strip() or ""
        if not suffix:
            return
        playlists_dir = _essential_playlists_dir()
        for client in _navidrome_clients():
            for playlist in client.fetch_all_playlists() or []:
                name = str(playlist.get("name") or "")
                if suffix not in name:
                    continue
                file_path = os.path.join(playlists_dir, f"{_sanitize_name(name)}.m3u")
                if os.path.exists(file_path):
                    continue
                playlist_id = str(playlist.get("id") or "")
                if playlist_id and client.delete_playlist(playlist_id):
                    logger.info(
                        "[finalise_stage] Swept orphaned Navidrome playlist '%s' (file removed)",
                        name,
                    )
                else:
                    logger.warning(
                        "[finalise_stage] Could not delete orphaned Navidrome playlist '%s'",
                        name,
                    )
    except Exception as exc:
        logger.warning("[finalise_stage] Genre playlist Navidrome sweep failed: %s", exc)


def _sync_playlist_to_navidrome(playlist_name: str, song_ids: list[str]) -> dict:
    """Create/update (IN PLACE) a generated playlist in Navidrome by name.

    Wraps ``playlist_navidrome_service.sync_playlist_by_name`` for every
    configured Navidrome user: an existing regular playlist with the same
    name is UPDATED (old songs below the threshold removed, new songs added,
    order per ``song_ids``), duplicate same-name playlists are deleted, and a
    missing playlist is created.  Smart playlists (.nsp) with the same name
    are left untouched.

    This is the fix for the duplicate playlists in Navidrome: the generated
    playlists are written as ``.m3u`` files into the watch folder and
    Navidrome imported each rewritten file as a NEW playlist.  Syncing via
    ``updatePlaylist`` keeps the playlist identity intact so scans never
    recreate it.

    ``song_ids`` are the local track ids (== Navidrome song ids — the library
    is imported from Navidrome).  Best-effort, never raises.
    """
    try:
        from services.playlists.playlist_navidrome_service import sync_playlist_by_name
        synced = {"updated": 0, "created": 0, "deduped": 0}
        for client in _navidrome_clients():
            r = sync_playlist_by_name(client, playlist_name, list(song_ids))
            if r.get("updated"):
                synced["updated"] += 1
            if r.get("created"):
                synced["created"] += 1
            synced["deduped"] += int(r.get("deduped") or 0)
        if any(synced.values()):
            logger.info(
                "[finalise_stage] Navidrome playlist sync '%s': %s",
                playlist_name, synced,
            )
        return synced
    except Exception as exc:
        logger.debug("[finalise_stage] Navidrome playlist sync failed for %s: %s", playlist_name, exc)
        return {}


def _genre_playlist_track_genres(
    row: Any,
    *,
    max_genres: int = 3,
    json_sources: dict[str, str] | None = None,
    delimited_sources: dict[str, str] | None = None,
) -> list[str]:
    """Resolve a track row's TOP weighted genres for genre-playlist pools.

    Shared by the library-wide genre playlist build and the per-album refresh
    so both use the exact same aggregation weights/synonyms.
    """
    from services.enrichment.genre_aggregation_service import aggregate_genres
    from services.enrichment.genre_tag_aggregator import parse_json_tags, parse_delimited_tags

    _json_sources = json_sources or {
        "lastfm_tags": "lastfm",
        "listenbrainz_genres": "listenbrainz",
        "discogs_genres": "discogs",
        "musicbrainz_genres": "musicbrainz",
        "spotify_genres": "spotify",
    }
    _delimited_sources = delimited_sources or {
        "essentia_genres": "essentia",
        "manual_genres": "manual",
        "navidrome_genres": "navidrome",
    }

    source_map: dict[str, list[str]] = {}
    for column, source in _json_sources.items():
        raw = row.get(column)
        if raw:
            names = [t.get("name") for t in (parse_json_tags(raw) or []) if t.get("name")]
            if names:
                source_map[source] = names
    for column, source in _delimited_sources.items():
        raw = row.get(column)
        if raw:
            names = [t.get("name") for t in (parse_delimited_tags(raw) or []) if t.get("name")]
            if names:
                source_map[source] = names
    if not source_map:
        return []
    return aggregate_genres(source_map, max_genres=max_genres)


def refresh_genre_playlists_for_album(artist: str, album: str) -> int:
    """Refresh genre top-tracks playlists for genres touched by ONE album.

    Called at the end of an album scan (per-album star posting) so a genre's
    playlist reflects tracks that just changed, instead of waiting for the
    whole library scan to finish.  Only the affected genres' pools are
    rebuilt (``only_genres``), so unrelated genre playlists are never touched
    and the stale-file sweep is skipped (the scan-end full pass still handles
    deletion).  Best-effort; never raises.
    """
    if not _genre_playlists_active():
        return 0
    try:
        from sqlalchemy import text as _text
        from db.engine import db_session as _db_session

        try:
            from helpers.config_helpers import get_config
            _cfg = (get_config() or {}).get("playlists") or {}
        except Exception:
            _cfg = {}
        min_stars = max(1, int(_cfg.get("genre_playlists_min_stars", 4) or 4))

        rows: list[Any] = []
        with _db_session() as session:
            result = session.execute(
                _text("""
                    SELECT id, title, file_path, duration, artist, album_artist,
                           COALESCE(stars, star_rating) AS stars,
                           COALESCE(popularity, final_score, 0) AS popularity_score,
                           COALESCE(is_live, 0) AS is_live,
                           COALESCE(is_compilation, 0) AS is_compilation,
                           lastfm_tags, listenbrainz_genres, discogs_genres,
                           musicbrainz_genres, spotify_genres,
                           essentia_genres, manual_genres, navidrome_genres
                    FROM tracks
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
                      AND album = :album
                      AND COALESCE(stars, star_rating) >= :min_stars
                """),
                {"artist": artist, "album": album, "min_stars": min_stars},
            )
            rows = [dict(r._mapping) for r in result.fetchall() or []]
        if not rows:
            return 0

        max_genres = max(1, int(_cfg.get("genre_playlists_max_genres", 3) or 3))
        affected: set[str] = set()
        for row in rows:
            for genre in _genre_playlist_track_genres(row, max_genres=max_genres):
                affected.add(genre)
        if not affected:
            return 0
        return _create_genre_top_track_playlists(only_genres=affected)
    except Exception as exc:
        logger.debug(
            "[finalise_stage] Per-album genre playlist refresh failed for %s - %s: %s",
            artist, album, exc,
        )
        return 0


def _create_genre_top_track_playlists(
    prune_only: bool = False,
    only_genres: set[str] | None = None,
) -> int:
    """Build ``{Genre} - Top Tracks.m3u`` playlists for the whole library.

    Library-wide (unlike the per-artist essential collections): every track
    at or above ``playlists.genre_playlists_min_stars`` (default 4★) is
    assigned its TOP weighted genres (``aggregate_genres`` with the same
    config weights/synonyms the genre UI uses, capped at
    ``playlists.genre_playlists_max_genres``, default 3).  Each genre pool is
    deduplicated by (track artist, normalized title) — the best version wins
    (studio over live, main release over compilation, then stars, then
    popularity) — and the playlist is sorted by ``final_score`` DESC (most
    popular first).  Every qualifying track is included: there is no top-N
    cap.

    A playlist is created or refreshed only when the genre pool holds MORE
    than ``playlists.genre_playlists_create_threshold`` (default 100) unique
    4★/5★ tracks, and an existing playlist is deleted once the pool drops
    BELOW ``playlists.genre_playlists_delete_threshold`` (default 80) unique
    tracks.  The gap between the two thresholds acts as hysteresis: a genre
    hovering between 80 and 100 tracks keeps whatever playlist already exists
    without churning it every scan.  Creation is gated by
    ``playlists.genre_playlists_enabled`` and deletion by
    ``playlists.genre_playlists_delete_enabled``.  Returns the number of
    playlists written.

    ``prune_only=True`` runs only the deletion check (used at app startup
    and scan start): pools are counted so ``keep_names`` is accurate, but no
    files are written — stale playlists below the delete threshold are still
    removed.

    ``only_genres`` (a set of genre names) restricts the pass to just those
    genres' playlists: only the listed pools are (re)written and the stale-file
    sweep is skipped, so unrelated genre playlists are never touched.  Used for
    the per-album refresh at the end of an album scan where tracks of that
    genre changed.
    """
    from collections import defaultdict

    from sqlalchemy import text as _text
    from db.engine import db_session as _db_session

    try:
        from helpers.config_helpers import get_config
        cfg = (get_config() or {}).get("playlists") or {}
    except Exception:
        cfg = {}
    min_stars = max(1, int(cfg.get("genre_playlists_min_stars", 4) or 4))
    max_genres = max(1, int(cfg.get("genre_playlists_max_genres", 3) or 3))
    create_threshold = max(1, int(cfg.get("genre_playlists_create_threshold", 100) or 100))
    delete_threshold = max(1, int(cfg.get("genre_playlists_delete_threshold", 80) or 80))
    create_enabled = _genre_playlists_enabled()
    delete_enabled = _genre_playlists_delete_enabled()

    rows: list[Any] = []
    try:
        with _db_session() as session:
            result = session.execute(
                _text("""
                    SELECT id, title, file_path, duration, artist, album_artist,
                           COALESCE(stars, star_rating) AS stars,
                           COALESCE(popularity, final_score, 0) AS popularity_score,
                           COALESCE(is_live, 0) AS is_live,
                           COALESCE(is_compilation, 0) AS is_compilation,
                           lastfm_tags, listenbrainz_genres, discogs_genres,
                           musicbrainz_genres, spotify_genres,
                           essentia_genres, manual_genres, navidrome_genres
                    FROM tracks
                    WHERE COALESCE(stars, star_rating) >= :min_stars
                """),
                {"min_stars": min_stars},
            )
            rows = [dict(r._mapping) for r in result.fetchall() or []]
    except Exception as exc:
        logger.debug("[finalise_stage] Genre playlist fetch failed: %s", exc)
        return 0

    from services.enrichment.genre_aggregation_service import aggregate_genres
    from services.enrichment.genre_tag_aggregator import parse_json_tags, parse_delimited_tags

    # Column -> weighted-source key (must match config GENRE_WEIGHTS keys).
    _json_sources = {
        "lastfm_tags": "lastfm",
        "listenbrainz_genres": "listenbrainz",
        "discogs_genres": "discogs",
        "musicbrainz_genres": "musicbrainz",
        "spotify_genres": "spotify",
    }
    _delimited_sources = {
        "essentia_genres": "essentia",
        "manual_genres": "manual",
        "navidrome_genres": "navidrome",
    }

    def _track_genres(row: Any) -> list[str]:
        return _genre_playlist_track_genres(
            row,
            max_genres=max_genres,
            json_sources=_json_sources,
            delimited_sources=_delimited_sources,
        )

    pools: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        for genre in _track_genres(row):
            pools[genre].append({
                "id": str(row.get("id") or ""),
                "title": str(row.get("title") or "Unknown"),
                "file_path": str(row.get("file_path") or ""),
                "duration": row.get("duration"),
                "stars": int(row.get("stars") or 0),
                "score": float(row.get("popularity_score") or 0),
                "artist": str(row.get("artist") or row.get("album_artist") or ""),
                "is_live": int(row.get("is_live") or 0),
                "is_compilation": int(row.get("is_compilation") or 0),
            })

    def _tiebreak(item: dict[str, Any]) -> tuple:
        # Winner-selection order for dedup (best version of a song wins):
        # studio over live, main release over compilation, then stars, then
        # popularity.
        return (
            item["is_live"],
            item["is_compilation"],
            -item["stars"],
            -item["score"],
            item["title"].casefold(),
        )

    def _popularity_order(item: dict[str, Any]) -> tuple:
        # Final playlist ordering: most popular first (final_score DESC),
        # then stars DESC, then title.  The playlist pools only contain
        # 4★/5★ tracks, so popularity is the meaningful primary key.
        return (
            -item["score"],
            -item["stars"],
            item["title"].casefold(),
        )

    playlists_dir = _essential_playlists_dir()
    os.makedirs(playlists_dir, exist_ok=True)
    written = 0
    keep_names: set[str] = set()
    for genre, tracks in pools.items():
        if only_genres is not None and genre not in only_genres:
            continue

        # Dedup: (track artist, normalized title) — the same song on the
        # artist's own release and a compilation (different album_artist)
        # counts once; winner by tiebreak.
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for t in tracks:
            key = (
                re.sub(r"\s+", " ", t["artist"]).strip().casefold(),
                _normalise_essential_title(t["title"]),
            )
            grouped[key].append(t)
        winners = [min(group, key=_tiebreak) for group in grouped.values()]
        qualifying_count = len(winners)

        playlist_name = _genre_playlist_name(_display_genre(genre))
        display_genre = _display_genre(genre)
        file_name = f"{_sanitize_name(playlist_name)}.m3u"
        file_path = os.path.join(playlists_dir, file_name)

        # Hysteresis: a genre still at/above the delete threshold keeps
        # whatever playlist already exists, even if it no longer clears the
        # (higher) create threshold.  Genres below the delete threshold have
        # their stale file removed further down.
        if qualifying_count >= delete_threshold:
            keep_names.add(file_name)

        if prune_only:
            continue

        if not create_enabled:
            continue
        if qualifying_count <= create_threshold:
            continue

        winners.sort(key=_popularity_order)
        lines = ["#EXTM3U"]
        for t in winners:
            try:
                duration = int(float(t.get("duration") or 0) or 0)
            except (TypeError, ValueError):
                duration = 0
            lines.append(f"#EXTINF:{duration},{display_genre} - {t['title']}")
            lines.append(t["file_path"] or t["title"])
        try:
            with open(file_path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
            keep_names.add(file_name)
            written += 1
            log_unified(
                f"📄 Playlist: Generated '{playlist_name}.m3u' ({len(winners)} tracks)"
            )
            # Sync the Navidrome playlist IN PLACE (update, never recreate) so
            # scans do not leave duplicate entries — old tracks that dropped
            # below the star threshold are removed, new tracks added, ordered
            # by the same popularity sort above.  Local track ids == Navidrome
            # song ids.
            try:
                _song_ids = [
                    str(t.get("id") or "") for t in winners
                    if str(t.get("id") or "").strip()
                ]
                if _song_ids:
                    _sync_playlist_to_navidrome(playlist_name, _song_ids)
            except Exception:
                pass
        except Exception as exc:
            logger.warning("[finalise_stage] Genre playlist write failed for %s: %s", genre, exc)

    # Stale-file cleanup: remove genre playlists that were written on an
    # earlier scan (tracked in the state file) but whose genre has dropped
    # below the delete threshold or whose name template changed.  Files that
    # never came from this generator are never touched.  Gated by the delete
    # toggle; the state file is refreshed either way so template changes are
    # cleaned up on later scans even while deletion is disabled.
    # A scoped ``only_genres`` refresh skips the sweep entirely so unrelated
    # genre playlists are never touched — the full pass at scan end handles
    # stale-file removal.
    previous_names = _load_genre_playlist_state()
    suffix = _sanitize_name(_genre_playlist_name("GENRE")) \
        .replace("GENRE", "", 1).strip() or ""

    candidates: set[str] = set(previous_names)
    try:
        for name in os.listdir(playlists_dir):
            if name.endswith(".m3u") and (not suffix or suffix in name):
                candidates.add(name)
    except Exception:
        pass

    removed: set[str] = set()
    if delete_enabled and only_genres is None:
        for name in candidates:
            if name in keep_names:
                continue
            try:
                os.remove(os.path.join(playlists_dir, name))
                removed.add(name)
                logger.info("[finalise_stage] Removed stale genre playlist: %s", name)
            except FileNotFoundError:
                removed.add(name)
            except Exception:
                pass
            # The file is gone — drop the imported Navidrome playlist too so
            # under-threshold genres don't linger in the UI.
            _delete_genre_playlist_from_navidrome(os.path.splitext(name)[0])

    # Self-healing Navidrome sweep: a previous delete attempt may have failed
    # or the file may have been removed out-of-band — drop any Navidrome
    # genre playlist whose .m3u is no longer on disk.
    try:
        _sweep_orphaned_genre_playlists_from_navidrome()
    except Exception:
        pass

    _save_genre_playlist_state((candidates | keep_names) - removed)

    return written


def prune_genre_playlists_for_deletion() -> None:
    """Delete genre playlists whose qualifying pool dropped below the delete
    threshold — without creating or refreshing anything.

    Runs at app startup and at the start of a fresh scan (in addition to the
    scan-end finalise check) so stale files are cleaned even when no full
    finalise pass completes.  Gated by
    ``playlists.genre_playlists_delete_enabled``; best-effort.
    """
    try:
        if not _genre_playlists_delete_enabled():
            return
        _create_genre_top_track_playlists(prune_only=True)
    except Exception as exc:
        logger.debug("[finalise_stage] Genre playlist prune failed: %s", exc)


# ---------------------------------------------------------------------------
# Per-album star rating posting
# ---------------------------------------------------------------------------

def compute_artist_scores(
    artist: str,
    scan_scores: list[float],
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
        from sqlalchemy import text as _text
        from db.engine import db_session as _db_session
        with _db_session() as session:
            result = session.execute(
                _text(
                    "SELECT title, album, final_score FROM tracks "
                    "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND final_score > 0"
                ),
                {"artist": artist},
            )
            db_rows = [
                (str(row_get(row, "album") or ""), float(row_get(row, "final_score") or 0))
                for row in result.fetchall() or []
                if row_get(row, "final_score")
                and str(row_get(row, "title") or "").strip().lower() not in scanned_titles
                and not is_bonus_track_title(str(row_get(row, "title") or ""))
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
) -> dict[str, int]:
    """Assign, persist, log and sync star ratings for ONE album.

    Mirrors the legacy scanner, which posted the per-album star-rating summary
    right after each album completed.  During a full artist/library scan the
    ratings are written to the DB and surfaced in the unified log as each
    album finishes, instead of all being batched for the end of the scan.

    All persistence runs on its own SQLAlchemy sessions (safe to call
    standalone from the scan loop).
    """
    if not album_results:
        return {"star_ratings": 0, "navidrome_synced": 0}

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
            from sqlalchemy import text as _text
            from db.engine import db_session as _db_session
            scanned_titles = {str(r.get("title") or "").strip().lower() for r in album_results}
            with _db_session() as session:
                rows = session.execute(
                    _text("SELECT title, final_score FROM tracks "
                          "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND album = :album AND final_score > 0"),
                    {"artist": artist, "album": album},
                ).fetchall() or []
            db_album_scores = [
                float(row_get(row, "final_score") or 0)
                for row in rows
                if row_get(row, "final_score")
                and str(row_get(row, "title") or "").strip().lower() not in scanned_titles
                and not is_bonus_track_title(str(row_get(row, "title") or ""))
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
            album_model = _build_album_model(artist, album_results, artist_scores)
            if album_model.get("has_benchmark"):
                logger.info(
                    "[finalise_stage] %s - %s → era=%s (M_peak=%.1f, album median=%.1f, A_skew=%.2f, R_eff=%.2f, benchmark=%s)",
                    artist, album, album_model.get("era"),
                    float(album_model.get("m_peak") or 0),
                    float(album_model.get("album_median") or 0),
                    float(album_model.get("a_skew") or 1.0),
                    float(album_model.get("reff") or 0),
                    album_model.get("benchmark_source", "scores"),
                )
        except Exception as exc:
            logger.debug("[finalise_stage] Album model build failed for %s - %s: %s", artist, album, exc)

        # Surface the exact weights + era rules this album was rated with.
        _log_scan_weights(artist, album, album_model)

        # ── Batch-load current rating + file path for the album's tracks ──
        # Used to (a) skip rewriting the audio file and re-syncing Navidrome
        # when the star rating is unchanged — the dominant scan disk-write
        # source (mutagen rewrites the ENTIRE audio file on every rating
        # save, so a full library scan rewrites every rated file even when
        # nothing changed) — and (b) replace the per-track file-path DB
        # lookup with one query for the whole album.
        from sqlalchemy import text as _text
        from db.engine import db_session as _db_session
        _stored_stars: dict[str, int] = {}
        _stored_paths: dict[str, str] = {}
        try:
            _album_ids = [
                str(t.get("track_id") or "").strip()
                for t in album_results
                if str(t.get("track_id") or "").strip()
            ]
            if _album_ids:
                _ph = ", ".join(f":id{i}" for i in range(len(_album_ids)))
                with _db_session() as _sess:
                    _rows = _sess.execute(
                        _text(
                            "SELECT id, stars, file_path FROM tracks "
                            f"WHERE CAST(id AS TEXT) IN ({_ph})"
                        ),
                        {f"id{i}": _tid for i, _tid in enumerate(_album_ids)},
                    ).fetchall() or []
                for _r in _rows:
                    _m = _r._mapping
                    _stored_stars[str(_m.get("id"))] = int(_m.get("stars") or 0)
                    _p = str(_m.get("file_path") or "").strip()
                    if _p:
                        _stored_paths[str(_m.get("id"))] = _p
        except Exception as exc:
            logger.debug(
                "[finalise_stage] Album rating/path batch load failed for %s - %s: %s",
                artist, album, exc,
            )

        # When unchanged ratings are skipped (default), tracks whose assigned
        # star rating matches the stored value do NOT get a full audio-file
        # rewrite or a redundant Navidrome setRating call.  Set
        # ``tagging.skip_unchanged_ratings: false`` to always rewrite/sync
        # (e.g. to repair stale tags after an external tagger touched files).
        try:
            from helpers.config_helpers import get_tagging_config
            _skip_unchanged = bool(get_tagging_config().get("skip_unchanged_ratings", True))
        except Exception:
            _skip_unchanged = True

        # ── Artist raw-listen distribution (hard absolute floors) ────────
        # The artist's Last.fm listeners across ALL albums — this album's
        # fresh results plus stored rows for the rest of the catalogue.  Used
        # by ``_assign_stars`` for the force-5★/4★ failsafe: a track in the
        # artist's absolute top-N% by raw listens bypasses album_z gating.
        _artist_listen_distribution: list[float] = [
            float(r.get("lastfm_listeners") or 0)
            for r in album_results
            if float(r.get("lastfm_listeners") or 0) > 0
        ]
        try:
            _artist_titles = {
                str(r.get("title") or "").strip().lower() for r in album_results
            }
            with _db_session() as _sess:
                _artist_rows = _sess.execute(
                    _text(
                        "SELECT title, lastfm_listeners FROM tracks "
                        "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist "
                        "AND COALESCE(lastfm_listeners, 0) > 0"
                    ),
                    {"artist": artist},
                ).fetchall() or []
            for _r in _artist_rows:
                _m = _r._mapping
                _t = str(_m.get("title") or "").strip().lower()
                _lf = float(_m.get("lastfm_listeners") or 0)
                if _lf > 0 and _t not in _artist_titles:
                    _artist_listen_distribution.append(_lf)
        except Exception as exc:
            logger.debug(
                "[finalise_stage] Artist listen distribution failed for %s: %s", artist, exc,
            )

        # Persist to database — one session for the whole album's rating
        # writes so the per-album assignment commits atomically.
        with _db_session() as session:
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
                        artist_listen_distribution=_artist_listen_distribution,
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
                _final_score = float(track.get("final_score") or _track_score or 0)
                _album_z = _compute_album_z(_track_score, album_scores)[0]
                _artist_z = _compute_artist_z(_track_score, artist_scores)[0]
                # Matched single-detection source names (Discogs, MusicBrainz,
                # Video, Last.fm, ...) from the track's ``single_sources``.
                _src_names: list[str] = []
                try:
                    _src_raw = track.get("single_sources") or ""
                    if isinstance(_src_raw, str):
                        _src_parsed = json.loads(_src_raw) if _src_raw.strip() else []
                    else:
                        _src_parsed = _src_raw
                    _src_names = [
                        str(s.get("source") or "").replace("_", " ")
                        for s in (_src_parsed or [])
                        if isinstance(s, dict) and bool(s.get("matched"))
                    ]
                except Exception:
                    _src_names = []
                _src_part = f", matched=[{', '.join(_src_names)}]" if _src_names else ""
                # Per-track rating line at INFO so operators can verify the
                # scoring logic end-to-end (score → z-scores → star band).
                log_unified(
                    f"[TRACK_RESULT] {artist} - {track.get('title')} → {stars}★ "
                    f"(final_score={_final_score:.1f}, album_z={_album_z:.2f}, artist_z={_artist_z:.2f}, "
                    f"single={track.get('is_single')}/{track.get('single_confidence')}"
                    + (
                        f", era={album_model.get('era')}/R={float(album_model.get('reff') or 0):.2f}"
                        if album_model.get("has_benchmark") else ""
                    )
                    + f"{_src_part})"
                )
                logger.debug(
                    "[finalise_stage] %s - %s → %d★ (final_score=%.1f, album_z=%.2f, artist_z=%.2f, single=%s/%s%s%s)",
                    artist, track.get("title"), stars,
                    _final_score,
                    _album_z,
                    _artist_z,
                    track.get("is_single"), track.get("single_confidence"),
                    f", era={album_model.get('era')}/R={float(album_model.get('reff') or 0):.2f}"
                    if album_model.get("has_benchmark") else "",
                    _src_part,
                )

                track_id = str(track.get("track_id") or "")
                if track_id:
                    try:
                        session.execute(
                            _text("UPDATE tracks SET stars = :stars WHERE id = :tid"),
                            {"stars": stars, "tid": track_id},
                        )
                    except Exception as exc:
                        logger.debug("[finalise_stage] DB update failed for %s: %s", track_id, exc)

                    # Mirror the rating into the audio file tags (POPM/RATING) when
                    # the tagging policy permits — the master toggle and the
                    # ratings_only mode are honoured by ``write_rating_to_file``.
                    # Silently skipped when tag writes are disabled.  A rating
                    # that matches the stored value skips the write entirely
                    # (``tagging.skip_unchanged_ratings``) — mutagen rewrites the
                    # whole audio file on every save, which is the single biggest
                    # disk-write source during a library scan.
                    if stars >= 1:
                        _rating_changed = stars != _stored_stars.get(track_id, 0)
                        if _skip_unchanged and not _rating_changed:
                            logger.debug(
                                "[finalise_stage] Rating unchanged for %s (%d★) — skipping file rewrite",
                                track_id, stars,
                            )
                        else:
                            try:
                                from services.metadata.tag_file_service import (
                                    _resolve_music_file_path,
                                    write_rating_to_file,
                                )
                                _path = _stored_paths.get(track_id) or ""
                                _abs = _resolve_music_file_path(_path) if _path else None
                                if _abs:
                                    write_rating_to_file(_abs, stars)
                            except Exception as _tag_err:
                                logger.debug("[finalise_stage] Rating tag write failed for %s: %s", track_id, _tag_err)

            # ── Era 5★ slot cap (scaling model step 4) ────────────────────
            # The era's slot budget limits how many 5★ singles one album can
            # carry via the catalog/album-rank path; surplus (weakest by album
            # z-score) are demoted to the 4★ Single Floor.  GLOBAL-FIRST 5★
            # pool locks are exempt: a catalog-locked track is the artist's
            # biggest hit and must never be demoted by its album's slot cap
            # (Battle Beast: Eden was the catalog #1 but a late-processed
            # album's slot gating demoted it while earlier albums kept 5★).
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
                # Locked tracks never consume a slot cap position — they are
                # removed from the pool BEFORE the weakest-by-album-z demotion
                # so they can never be the surplus that gets cut.
                if len(slot_tracks) > max_slots:
                    locked_kept = [t for t in slot_tracks if t.get("_global_5star_locked")]
                    demotable = [t for t in slot_tracks if not t.get("_global_5star_locked")]
                    demotable.sort(
                        key=lambda t: _compute_album_z(
                            float(t.get("popularity_score") or 0), album_scores
                        )[0],
                        reverse=True,
                    )
                    # Re-add the locked tracks as protected leaders, then
                    # demote the surplus from the non-locked tail.
                    reordered = list(locked_kept) + list(demotable)
                    for t in reordered[max_slots:]:
                        t["stars"] = 4
                        _tid = str(t.get("track_id") or "")
                        if _tid:
                            try:
                                session.execute(
                                    _text("UPDATE tracks SET stars = 4 WHERE id = :tid"),
                                    {"tid": _tid},
                                )
                            except Exception as exc:
                                logger.debug(
                                    "[finalise_stage] 5★ cap demote failed for %s: %s", _tid, exc,
                                )
                        logger.info(
                            "[finalise_stage] %s - %s 5★ → 4★ (era slot cap %d)",
                            artist, t.get("title"), max_slots,
                        )

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

        # Per-user heart rating floor: any track hearted by a configured
        # Navidrome user never drops below the configured floor (e.g. 4★).
        # Applied AFTER the algorithm so personal taste overrides the
        # popularity-based band, and BEFORE the Navidrome sync so the raised
        # ratings propagate.
        try:
            from services.favourites_service import apply_favourite_rating_floor
            _floored = apply_favourite_rating_floor(artist, album)
            if _floored:
                log_unified(f"♥ {_floored} hearted track(s) raised to the favourite rating floor")
        except Exception as _floor_err:
            logger.debug("[finalise_stage] Favourite rating floor skipped: %s", _floor_err)

        # Sync to Navidrome — every rated track (old-system parity:
        # 1★/2★ ratings and downgrades must propagate too).  Ratings that
        # match the stored value are skipped (unless ``skip_unchanged_ratings``
        # is off) — each setRating is a Subsonic HTTP call + config reload per
        # user per track, which churns Navidrome and the filesystem during a
        # full library scan.
        if options.get("sync_navidrome", True):
            _attempted = 0
            _synced = 0
            _skipped = 0
            # Build the rating-sync clients ONCE per album — each setRating is
            # a Subsonic HTTP call, and the old per-track path ALSO reloaded
            # the user config and constructed a fresh NavidromeClient per track
            # per user.  Clients are built lazily so an album with nothing to
            # sync (all unchanged / unrated) never pays the config load.
            _sync_clients: list[Any] | None = None
            _consecutive_failures = 0
            for track in album_results:
                stars = track.get("stars", 0)
                if stars < 1:
                    continue  # unrated
                track_id = str(track.get("track_id") or "")
                if track_id:
                    if _skip_unchanged and stars == _stored_stars.get(track_id, 0):
                        _skipped += 1
                        continue
                    _attempted += 1
                    if _sync_clients is None:
                        _sync_clients = _build_rating_sync_clients()
                    if _sync_rating_to_navidrome(track_id, stars, clients=_sync_clients):
                        navidrome_synced += 1
                        _synced += 1
                        _consecutive_failures = 0
                    else:
                        _consecutive_failures += 1
                        # A Navidrome that is unreachable / timing out must not
                        # make every remaining track of the album burn a full
                        # HTTP timeout each — stop pushing after 3 consecutive
                        # misses and let the per-album summary surface it.
                        if _consecutive_failures >= 3:
                            logger.warning(
                                "[finalise_stage] Aborting Navidrome rating sync for %s after %d consecutive failures — Navidrome unreachable?",
                                str(artist or "").strip(), _consecutive_failures,
                            )
                            break
            _failed = _attempted - _synced
            # Surface successful syncs at INFO — a per-album summary line so
            # the album scan log shows Navidrome output (previously silent).
            # Unchanged ratings that were skipped are surfaced at INFO too so
            # operators can see the write/API load the scan avoided.
            if _skipped > 0:
                log_unified(
                    f"🔗 Navidrome: skipped {_skipped} unchanged rating(s) for '{artist}'"
                )
            if _synced > 0:
                log_unified(f"🔗 Navidrome: synced {_synced} rating(s) for '{artist}'")
            if _failed > 0:
                logger.warning(
                    "[finalise_stage] %d/%d Navidrome rating syncs failed for %s — check credentials / Subsonic API",
                    _failed, _attempted, str(artist or "").strip(),
                )

    except Exception as exc:
        logger.error("[finalise_stage] Album finalisation failed for %s - %s: %s", artist, album, exc)

    return {"star_ratings": total_star_ratings, "navidrome_synced": navidrome_synced}


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

def _sync_isrc_popularity() -> int:
    """Sync popularity stats across tracks sharing the same ISRC.

    A recording is a unique performance — every track in the library that
    carries the same ISRC is the same recording regardless of how its file
    tags spelled the title/artist.  After a scan completes, any duplicate
    row inherits the HIGHEST popularity score and single status found for
    that ISRC, so a badly-tagged copy can never drag the shared recording's
    scores down.  ``final_score`` and ``popularity`` are written in lockstep
    everywhere, so both columns are synced together.
    """
    from sqlalchemy import text as _text
    from db.engine import db_session as _db_session
    try:
        with _db_session() as session:
            result = session.execute(
                _text("""
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
                """)
            )
            return result.rowcount or 0
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
    # Diagnostic: surface the LIVE star config the scan will apply (organic
    # floor for single-driven elevation + the listener-standout 5★ z
    # threshold).  ``source: config`` when the keys exist in config.yaml,
    # ``defaults`` otherwise — mirrors the ⚖️ WEIGHTS marker so a mis-saved
    # config value is visible in the scan log instead of silently producing
    # unexpected star ratings.
    try:
        from services.popularity.popularity_config import get_single_organic_floor
        from helpers.config_helpers import get_config
        _floor_score, _floor_listeners = get_single_organic_floor()
        _sd_cfg = (get_config() or {}).get("single_detection") or {}
        _cfg_ok = isinstance(_sd_cfg, dict)
        _floor_source = (
            "config" if _cfg_ok and "single_organic_floor_score" in _sd_cfg
            else "defaults"
        )
        _lz = _live_star_thresholds().get("listener_5star_z", 1.0)
        _lz_source = (
            "config" if _cfg_ok and "listener_5star_z_threshold" in _sd_cfg
            else "defaults"
        )
        log_unified(
            f"🧪 STAR CONFIG: organic_floor={_floor_score:g} (listeners {_floor_listeners:g}, {_floor_source}) | "
            f"listener_5star_z={_lz:g} ({_lz_source})"
        )
    except Exception:
        pass
    if not results:
        # No per-track results were produced (all albums were skipped — e.g. a
        # singles-only scan where every album was already assessed).  The
        # library-wide playlists (essential collections, genre top-tracks,
        # New Music) are DB-driven and must still be refreshed so a skip-all
        # scan does not silently leave them stale.  Star ratings have nothing
        # to assign here, so only the DB-driven outputs run.
        try:
            if _essential_playlists_enabled(options):
                # The runner already created/refreshed every artist's essential
                # collection at the END of each artist's scan section — even in
                # a skip-all scan (its section closeout still fires per artist).
                # Only fall back to the full DB-driven refresh when the runner
                # covered nobody (e.g. a scan that loaded zero albums).
                if options.get("_essential_playlists_done"):
                    log_unified(
                        f"[FINALISE_STAGE] Essential collections refreshed during scan: "
                        f"{len(options['_essential_playlists_done'])} artist(s)"
                    )
                else:
                    _essential_refreshed = _refresh_all_essential_collections()
                    if _essential_refreshed:
                        log_unified(
                            f"[FINALISE_STAGE] Essential collections refreshed: {_essential_refreshed} artist(s)"
                        )
            if _genre_playlists_active():
                _genre_playlists_written = _create_genre_top_track_playlists()
                if _genre_playlists_written:
                    log_unified(
                        f"[FINALISE_STAGE] Genre playlists: {_genre_playlists_written} file(s) written"
                    )
            if _new_music_playlist_enabled():
                _create_new_music_playlist()
        except Exception as exc:
            logger.error("[finalise_stage] Finalisation failed: %s", exc)
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
                scanned_titles=scanned_titles,
            )

            # ── Persist artist_stats (artist-context popularity data) ────
            # The mean-popularity adjustment reads median_popularity / MAD
            # from this table — without a write here the adjustment silently
            # no-ops and the artist page has no catalogue statistics.
            try:
                from sqlalchemy import text as _text
                from db.engine import db_session as _db_session
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
                    _artist_id = _resolve_navidrome_artist_id(artist) or artist
                    from datetime import datetime as _dt
                    with _db_session() as session:
                        session.execute(
                            _text("""
                                INSERT INTO artist_stats
                                    (artist_id, artist_name, album_count, track_count, last_updated,
                                     mean_popularity, median_popularity, popularity_stddev, popularity_mad)
                                VALUES (:artist_id, :artist_name, :album_count, :track_count, :last_updated,
                                        :mean, :median, :stddev, :mad)
                                ON CONFLICT (artist_id) DO UPDATE SET
                                    artist_name = EXCLUDED.artist_name,
                                    album_count = EXCLUDED.album_count,
                                    track_count = EXCLUDED.track_count,
                                    last_updated = EXCLUDED.last_updated,
                                    mean_popularity = EXCLUDED.mean_popularity,
                                    median_popularity = EXCLUDED.median_popularity,
                                    popularity_stddev = EXCLUDED.popularity_stddev,
                                    popularity_mad = EXCLUDED.popularity_mad
                            """),
                            {
                                "artist_id": _artist_id,
                                "artist_name": artist,
                                "album_count": _album_count,
                                "track_count": len(_valid_scores),
                                "last_updated": _dt.now().isoformat(),
                                "mean": mean(_valid_scores),
                                "median": _med,
                                "stddev": stdev(_valid_scores) if len(_valid_scores) > 1 else 0.0,
                                "mad": _mad,
                            },
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
                )
                total_star_ratings += _posted.get("star_ratings", 0)
                navidrome_synced += _posted.get("navidrome_synced", 0)

            # Create/refresh the artist's Essential Collection .m3u — scanned
            # against the FULL DB track history, deduplicated by normalized
            # title, and only written when > 12 unique 4★/5★ tracks exist.
            # Gated by config (playlists.essential_playlists_enabled) or an
            # explicit pipeline override (options.create_playlists).
            if _essential_playlists_enabled(options):
                # The runner already created/refreshed every artist's
                # collection at the END of its own scan section (including
                # fully-skipped artists) — skip re-doing them here, and reuse
                # the once-per-scan feat-track pool instead of re-querying the
                # whole library per artist.
                _done_artists = set(options.get("_essential_playlists_done") or set())
                if artist.casefold() not in _done_artists:
                    _featured_rows = options.get("_essential_featured_rows")
                    if _featured_rows is not None:
                        _create_essential_m3u(artist, featured_rows=_featured_rows)
                    else:
                        _create_essential_m3u(artist)

        # ── ISRC popularity sync (recording-level inheritance) ─────────────
        # Runs once per scan, after every track is persisted: duplicate rows
        # sharing an ISRC inherit the strongest popularity/single evidence so
        # the recording is scored identically everywhere.
        try:
            _isrc_updated = _sync_isrc_popularity()
            if _isrc_updated:
                log_unified(
                    f"[FINALISE_STAGE] ISRC sync: {_isrc_updated} track(s) inherited higher popularity across shared ISRCs"
                )
        except Exception as exc:
            logger.debug("[finalise_stage] ISRC sync commit failed: %s", exc)

        # ── Genre top-tracks playlists (library-wide, once per scan) ──────
        # Every ≥4★ track's top weighted genres feed per-genre pools, sorted
        # by stars then final_score and capped at the top-N (default 500).
        # A playlist is created only when the genre clears the create
        # threshold and removed when it drops below the delete threshold.
        # Gated by config (playlists.genre_playlists_enabled /
        # playlists.genre_playlists_delete_enabled).
        try:
            if _genre_playlists_active():
                _genre_playlists_written = _create_genre_top_track_playlists()
                if _genre_playlists_written:
                    log_unified(
                        f"[FINALISE_STAGE] Genre playlists: {_genre_playlists_written} file(s) written"
                    )
        except Exception as exc:
            logger.debug("[finalise_stage] Genre playlist generation failed: %s", exc)

        # ── New Music playlist (library-wide, once per scan) ──────────────
        # Rolling "recently added" playlist: the most recently added (to
        # Navidrome) 4★/5★ tracks, newest first, created once 100 qualify and
        # capped at 100 — newer additions push older entries out each scan.
        try:
            if _new_music_playlist_enabled():
                _create_new_music_playlist()
        except Exception as exc:
            logger.debug("[finalise_stage] New Music playlist generation failed: %s", exc)

    except Exception as exc:
        logger.error("[finalise_stage] Finalisation failed: %s", exc)

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

