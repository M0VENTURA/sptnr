"""Single detection service (Enhanced 8-Stage Algorithm).

Single detection is metadata/enrichment classification, not popularity math.
Implements the comprehensive 8-stage detection algorithm:

1. Pre-filter & validation
2. Z-score threshold gate (artist + album)
3. Discogs confirmation
4. MusicBrainz confirmation + compilation check
5. Radio edit / single marker detection
6. Title normalization & duration matching
7. Last.fm album track count check
8. Final hybrid confidence decision

Popularity scans call this service; the result is a classification signal.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from statistics import median as stat_median
from typing import Any

from helpers.normalization_service import (
    strip_single_release_suffix,
    normalize_title_for_lookup,
    strip_remaster_suffix,
    is_remastered_only_variant,
)

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

IGNORE_SINGLE_KEYWORDS = frozenset({
    "intro", "outro", "jam",
    "live", "unplugged",
    "remix", "edit", "mix",
    "acoustic", "orchestral",
    "demo", "instrumental", "karaoke",
})

# Suffixes that can be stripped during title normalisation (radio edit etc.)
_STRIPPABLE_SUFFIXES = [
    "radio edit", "single edit", "edit",
    "single version", "radio version", "radio mix",
]
_SEPARATORS = [" - ", " (", " ["]

# Compilation / special-edition keywords
_COMPILATION_KEYWORDS = [
    "greatest hits", "best of", "the very best", "anthology",
    "singles", "collection", "ultimate", "gold", "platinum",
]
_SPECIAL_EDITION_KEYWORDS = [
    "deluxe", "expanded", "reissue", "anniversary", "bonus",
    "special edition", "extended edition", "tour edition",
    "limited edition", "collector's edition", "remastered",
]

# Method-failure threshold for method fallback
_METHOD_FAIL_THRESHOLD = 3


# ── Stage 0: Title normalisation helpers ──────────────────────────────────

def normalize_title_strict(title: str) -> str:
    """Normalise title: strip variant suffixes, lowercase, collapse whitespace.

    Preserves trailing ``!``, ``+``, ``?``, and Roman numerals.
    """
    t = strip_release_variant_suffix(title or "")
    # Preserve trailing punctuation
    preserved = ""
    m = re.search(r'([!+?]+)\s*$', t)
    if m:
        preserved = m.group(1)
        t = t[:m.start()]
    # Preserve Roman numeral suffix
    roman = ""
    m = re.search(r'\s+(I{1,3}|IV|V|VI{0,3}|IX|X{1,3}|XI{0,3}|XIV|XV|XVI{0,3}|XIX|XX)\s*$', t, re.IGNORECASE)
    if m:
        roman = " " + m.group(1).lower()
        t = t[:m.start()]
    # Strip brackets, dashes, punctuation
    t = re.sub(r'\s*[\(\[].*?[\)\]]', '', t)
    t = re.sub(r'[^\w\s]', '', t)
    t = t.lower().strip()
    t = re.sub(r'^(?:a|an|the)\s+', '', t)
    t = re.sub(r'\s+', ' ', t)
    if roman:
        t += roman
    if preserved:
        t += preserved
    return t


def strip_release_variant_suffix(title: str) -> str:
    """Strip known release variant suffixes (Radio Edit, Single Version, etc.)."""
    if not title:
        return title
    for sep in _SEPARATORS:
        for suffix in _STRIPPABLE_SUFFIXES:
            m = re.search(re.escape(sep) + r'\s*' + re.escape(suffix) + r'\s*$', title, re.IGNORECASE)
            if m:
                return title[:m.start()].rstrip()
    return title


def is_non_canonical_version_strict(title: str) -> bool:
    """Return True for titles with non-canonical version markers.

    Allows ``(radio edit)`` and ``(single)`` as canonical single versions.
    Remastered markers are stripped before checking.
    """
    t = strip_remaster_suffix(title or "").lower()
    for pat in [r'\(radio\s+edit\s*\)', r'\(single\s*\)']:
        t = re.sub(pat, '', t)
    markers = [r'\bremix\b', r'\bacoustic\b', r'\blive\b', r'\bunplugged\b',
               r'\borchestral\b', r'\bsymphonic\b', r'\bdemo\b', r'\binstrumental\b',
               r'\bedit\b', r'\bextended\b', r'\bversion\b', r'\balt\b', r'\balternate\b']
    return any(re.search(p, t) for p in markers)


def duration_matches_strict(d1: float | None, d2: float | None) -> bool:
    """Duration must match within ±2 seconds (Stage 6)."""
    if d1 is None or d2 is None:
        return True
    return abs(d1 - d2) <= 2.0


def has_single_or_radio_edit_marker(title: str) -> bool:
    """Return True when title contains canonical single/radio edit markers."""
    return bool(re.search(r'\b(?:radio\s+(?:edit|mix|version)|single\s+(?:version|edit|mix))\b',
                          title or "", re.IGNORECASE))


# ── Stage 0: Album-type helpers ───────────────────────────────────────────

def is_compilation_album(album_type: str | None, album_title: str) -> bool:
    if album_type and "compilation" in album_type.lower():
        return True
    t = (album_title or "").lower()
    return any(kw in t for kw in _COMPILATION_KEYWORDS)


def is_special_edition_album(album_title: str) -> bool:
    t = (album_title or "").lower()
    if any(kw in t for kw in _SPECIAL_EDITION_KEYWORDS):
        return True
    if ":" in t:
        parts = t.split(":", 1)
        if len(parts) > 1 and "edition" in parts[1]:
            return True
    return False


# ── Stage 1: Pre-filter ───────────────────────────────────────────────────

def should_skip_single_detection(title: str, album_type: str | None = None) -> bool:
    """Return True for obvious non-single alternate/live/demo versions.

    Remastered variants are intentionally NOT skipped — they are the same
    song as the original and remain eligible for single detection.
    """
    t = (title or "").lower()
    at = (album_type or "").lower()
    if any(kw in t for kw in IGNORE_SINGLE_KEYWORDS):
        return True
    if any(kw in at for kw in ["live", "compilation"]):
        return True
    return False


# ── Stage 2: Z-score calculation (median + MAD) ───────────────────────────

def calculate_z_score_strict(popularity: float, pop_median: float, pop_mad_scaled: float) -> float:
    """Robust z-score using median + scaled MAD."""
    if pop_mad_scaled == 0:
        return 0.0
    return (popularity - pop_median) / pop_mad_scaled


def get_dynamic_z_threshold(track_count: int, release_year: int | None = None, is_compilation: bool = False) -> float:
    """Calculate dynamic z-score threshold based on catalog size and release date."""
    if track_count < 5:
        threshold = 1.5
    elif track_count < 10:
        threshold = 1.8
    elif track_count < 50:
        threshold = 2.0
    elif track_count < 200:
        threshold = 1.9
    else:
        threshold = 1.8
    if release_year and release_year < 2000:
        reduction = min(0.3, (2000 - release_year) * 0.02)
        threshold = max(1.2, threshold - reduction)
    if is_compilation:
        threshold = min(threshold + 0.2, 2.5)
    return threshold


# ── Stage 3-4: Source confidence ──────────────────────────────────────────

def check_high_confidence_dynamic(
    discogs: bool = False, musicbrainz: bool = False,
    discogs_video: bool = False, lastfm: bool = False,
    radio_edit: bool = False, compilation: bool = False,
    date_match: bool = False,
) -> bool:
    """Return True when HIGH confidence is achieved (1 high source or 2 medium)."""
    high = sum([discogs, musicbrainz])  # treated as high-confidence sources
    medium = sum([discogs_video, lastfm, radio_edit, compilation, date_match])
    if high >= 1 or medium >= 2:
        return True
    return False


def _source_confidence_levels() -> dict[str, str]:
    """Load per-source confidence levels from the ``features`` config.

    Mirrors the legacy ``source_*_confidence`` knobs. Defaults match current
    behaviour: Discogs + MusicBrainz are high-confidence sources; the rest
    are medium. ``low`` excludes a source from the confidence decision.
    """
    feats: dict = {}
    try:
        from helpers.config_helpers import get_config
        cfg = get_config()
        feats = cfg.get("features", {}) or {}
    except Exception:
        feats = {}
    defaults = {
        "discogs": "high",
        "musicbrainz": "high",
        "discogs_video": "medium",
        "musicbrainz_compilation": "medium",
        "lastfm": "medium",
        "radio_edit": "medium",
    }
    keys = {
        "discogs": "source_discogs_confidence",
        "musicbrainz": "source_musicbrainz_confidence",
        "discogs_video": "source_discogs_video_confidence",
        "musicbrainz_compilation": "source_musicbrainz_compilation_confidence",
        "lastfm": "source_lastfm_confidence",
        "radio_edit": "source_radio_edit_confidence",
    }
    result: dict[str, str] = {}
    for src, default in defaults.items():
        val = str(feats.get(keys[src], default) or default).lower()
        result[src] = val if val in ("high", "medium", "low") else default
    return result


# ── Stage 5-7: Source detection methods ───────────────────────────────────

# Per-artist ListenBrainz top-10% context cache (keyed by artist MBID) so the
# single-detection evidence below never issues more than one LB API call per
# artist per scan run.
_lb_artist_context_cache: dict[str, dict] = {}


def _get_lb_artist_context_cached(artist_mbid: str) -> dict:
    """Return the cached ListenBrainz artist top-10% context."""
    if not artist_mbid:
        return {"threshold": 0, "total": 0}
    if artist_mbid not in _lb_artist_context_cache:
        try:
            from services.enrichment.single_detection_context_service import get_artist_listenbrainz_context
            _lb_artist_context_cache[artist_mbid] = get_artist_listenbrainz_context(artist_mbid)
        except Exception:
            _lb_artist_context_cache[artist_mbid] = {"threshold": 0, "total": 0}
    return _lb_artist_context_cache[artist_mbid]


def _detect_musicbrainz(title: str, artist: str, artist_mbid: str | None,
                        album_track_count: int | None, mb_client=None,
                        mb_cached_singles: set | None = None) -> dict[str, Any]:
    # Fast path: the title is already known to be a MusicBrainz single from
    # the artist's cached missing_releases (avoids one MB API call per track).
    if mb_cached_singles:
        normalized = (title or "").lower().strip()
        if normalized in {str(t).lower().strip() for t in mb_cached_singles}:
            return {"source": "musicbrainz", "matched": True, "confidence": 0.9,
                    "metadata": {}, "cached": True}
    try:
        if mb_client is None:
            from api_clients.musicbrainz_http import MusicBrainzHttpClient
            mb_client = MusicBrainzHttpClient()

        matched = False
        # Preferred path: scope the search to the artist's own singles/EPs
        # release-groups when the artist MBID is known. Far more reliable than
        # fuzzy recording title matching, and immune to recording-split issues
        # (album version vs single version being separate MB recordings).
        if artist_mbid and hasattr(mb_client, "search_release_groups"):
            try:
                target = normalize_title_for_lookup(strip_single_release_suffix(title) or title)
                candidates: list[dict] = []
                for _pt in ("single", "ep"):
                    try:
                        candidates += mb_client.search_release_groups(
                            f"arid:{artist_mbid} AND primarytype:{_pt}", limit=100
                        ) or []
                    except Exception:
                        continue
                for group in candidates:
                    rg_title = str(group.get("title") or "")
                    if normalize_title_for_lookup(strip_single_release_suffix(rg_title) or rg_title) == target:
                        matched = True
                        break
            except Exception as exc:
                logger.debug("Artist-scoped MB single lookup failed for %s / %s: %s", artist, title, exc)

        # Fallback: per-recording fuzzy match via the service.
        if not matched:
            # Use the client's is_single method if available, otherwise use the service.
            if hasattr(mb_client, "is_single"):
                matched = bool(mb_client.is_single(title, artist, artist_mbid=artist_mbid,
                                                    album_track_count=album_track_count))
            else:
                from services.enrichment.musicbrainz_service import MusicBrainzService
                svc = MusicBrainzService(enabled=True)
                matched = bool(svc.is_single(title, artist, album_track_count=album_track_count))
        release_date = None
        if matched:
            try:
                if hasattr(mb_client, "get_single_release_date"):
                    release_date = mb_client.get_single_release_date(title, artist, artist_mbid=artist_mbid)
            except Exception:
                pass
        return {"source": "musicbrainz", "matched": matched, "confidence": 0.9 if matched else 0.0,
                "metadata": {"release_date": release_date} if release_date else {}}
    except Exception as exc:
        logger.debug("MusicBrainz single detection failed for %s / %s: %s", artist, title, exc)
        return {"source": "musicbrainz", "matched": False, "confidence": 0.0, "metadata": {}}


def _detect_discogs(title: str, artist: str, album: str | None,
                    discogs_token: str | None, duration: float | None = None,
                    is_special_edition: bool = False) -> dict[str, Any]:
    token = discogs_token or os.environ.get("DISCOGS_TOKEN", "")
    if not token:
        try:
            from helpers.config_helpers import get_config
            cfg = get_config()
            token = (cfg.get("api_integrations", {}).get("discogs", {}) or {}).get("token", "") or ""
        except Exception:
            token = ""
    if not token or token.lower() in ("your_discogs_token", "your_token", "placeholder"):
        return {"source": "discogs", "matched": False, "confidence": 0.0, "metadata": {}}
    try:
        from services.enrichment.discogs_service import DiscogsService
        svc = DiscogsService(token=token)
        ctx = {"album": album, "is_special_edition": is_special_edition} if album else {"is_special_edition": is_special_edition}
        matched = bool(svc.is_single(title, artist, album_context=ctx))
        year = None
        if matched and hasattr(svc, "get_single_release_year"):
            year = svc.get_single_release_year(title, artist)
        return {"source": "discogs", "matched": matched, "confidence": 0.8 if matched else 0.0,
                "metadata": {"release_year": year} if year else {}}
    except Exception as exc:
        logger.debug("Discogs single detection failed for %s / %s: %s", artist, title, exc)
        return {"source": "discogs", "matched": False, "confidence": 0.0, "metadata": {}}


def _detect_lastfm(artist: str, album: str, lastfm_client=None) -> bool:
    """Medium-confidence check: Last.fm album track count 1-3 suggests a single."""
    if not lastfm_client:
        return False
    try:
        count = lastfm_client.get_album_track_count(artist, album)
        if 1 <= count <= 3:
            return True
        if 4 <= count <= 6:
            try:
                return bool(lastfm_client.has_title_track(artist, album))
            except Exception:
                return True  # borderline EP — treat as single
    except Exception:
        pass
    return False


# ── Stage 8: Final decision ────────────────────────────────────────────────

def determine_final_status(
    discogs: bool = False, musicbrainz: bool = False,
    album_z: float = 0.0, artist_z: float = 0.0,
    discogs_video: bool = False, lastfm: bool = False,
    mb_video: bool = False, mb_compilation: bool = False,
    radio_edit: bool = False, popularity: float = 0.0,
    album_mean: float = 0.0, has_metadata: bool = False,
    is_remastered_only: bool = False, date_match: bool = False,
    is_title_track: bool = False,
    zscore_high: float = 1.0, zscore_medium: float = 0.6,
    high_sources: int | None = None, medium_sources: int | None = None,
) -> str:
    """Final single status based on source detection and z-score analysis.

    ``zscore_high`` / ``zscore_medium`` are the configurable confidence
    boundaries (``single_detection.zscore_high_threshold`` /
    ``zscore_medium_threshold``). ``high_sources`` / ``medium_sources``
    optionally override the source-confidence counts (used to honour the
    per-source ``source_*_confidence`` config knobs). Returns ``'high'``,
    ``'medium'``, or ``'none'``.
    """
    max_z = max(album_z, artist_z)
    if high_sources is not None and medium_sources is not None:
        high = high_sources
        medium = medium_sources
    else:
        high = sum([discogs, musicbrainz])
        medium = sum([discogs_video, lastfm, mb_video, mb_compilation, radio_edit, date_match])

    # Z-score above the high boundary
    if max_z >= max(0.0, zscore_high):
        if high >= 1 or medium >= 2:
            return 'high'
        return 'none'

    # Z-score between the medium and high boundaries
    if max_z > max(0.0, zscore_medium):
        if high >= 1:
            return 'high'
        if medium >= 2:
            return 'medium'
        return 'none'

    # Z-score <= 0: remastered bypass, title-track boost, or metadata evidence
    if is_remastered_only or is_title_track or high >= 1 or medium >= 2:
        if high >= 1:
            return 'high'
        if medium >= 1:
            return 'medium'
        return 'none'

    return 'none'


# ── Main entry point ──────────────────────────────────────────────────────

def detect_single_for_track(
    title: str,
    artist: str,
    album_track_count: int = 1,
    spotify_results_cache: dict | None = None,
    verbose: bool = False,
    discogs_token: str | None = None,
    track_id: str | None = None,
    album: str | None = None,
    isrc: str | None = None,
    duration: float | None = None,
    popularity: float | None = None,
    album_type: str | None = None,
    use_advanced_detection: bool = True,
    zscore_threshold: float = 1.0,
    album_is_underperforming: bool = False,
    artist_median_popularity: float = 0.0,
    lastfm_client=None,
    track_repo: Any = None,
    persist_result: bool = True,
    mb_cached_singles: set | None = None,
    artist_mbid: str | None = None,
    mb_client=None,
    listenbrainz_listens: int | None = None,
) -> dict[str, Any]:
    """Detect whether a track is a single using the 8-stage algorithm.

    Returns ``{is_single, confidence, sources, reasons}``.
    """
    title = title or ""
    artist = artist or ""
    lookup_title = strip_single_release_suffix(title)
    logger.debug("[SINGLE_DETECTION] Checking: %s - %s", artist, title)

    if not title or not artist:
        return {"is_single": False, "confidence": "low", "confidence_score": 0.0, "sources": [], "reasons": ["missing_title_or_artist"]}

    if should_skip_single_detection(title, album_type=album_type):
        return {"is_single": False, "confidence": "low", "confidence_score": 0.0, "sources": [], "reasons": ["alternate_or_live_version"]}

    is_compilation = is_compilation_album(album_type, album or "")
    is_special = is_special_edition_album(album or "")
    is_remastered = is_remastered_only_variant(title)

    # ── Calculate z-scores (median + MAD) ──
    album_z = 0.0
    artist_z = 0.0
    if popularity is not None and popularity > 0:
        try:
            # Artist-level stats
            from services.popularity.popularity_stats_service import calculate_artist_stats, calculate_album_stats

            _, _, artist_vals = calculate_artist_stats(None, artist)
            if artist_vals:
                art_med = stat_median(artist_vals)
                art_mad = stat_median([abs(v - art_med) for v in artist_vals]) if artist_vals else 0
                art_spread = max(art_mad * 1.4826, 10.0)
                artist_z = (popularity - art_med) / art_spread if art_spread > 0 else 0

            if album:
                _, _, album_vals = calculate_album_stats(None, artist, album)
                if album_vals:
                    alb_med = stat_median(album_vals)
                    alb_mad = stat_median([abs(v - alb_med) for v in album_vals]) if album_vals else 0
                    alb_spread = max(alb_mad * 1.4826, 10.0)
                    album_z = (popularity - alb_med) / alb_spread if alb_spread > 0 else 0
                    if is_compilation:
                        album_z = artist_z  # use artist-wide for compilations
        except Exception as exc:
            logger.debug("Z-score calculation failed: %s", exc)

    # Z-score gate (SOFT): a track scoring far below the artist median is no
    # longer rejected before source lookups — its popularity data may simply be
    # weak (missing Last.fm/LB counts, split variants). Sources still run; the
    # low z-score only caps the final confidence below 'high' unless two or
    # more high-confidence sources independently confirm the single.
    z_low = artist_z < -1.0 and not is_compilation and not is_remastered

    # ── Gather source confirmations ──
    discogs_confirmed = False
    musicbrainz_confirmed = False
    discogs_video_confirmed = False
    lastfm_confirmed = False
    radio_edit_found = False
    mb_compilation_confirmed = False
    single_release_date_match = False

    reasons: list[str] = []
    sources: list[dict] = []
    if z_low:
        reasons.append("z_score_low")

    # ── Dynamic z-score standout signal ──────────────────────────────────
    # Legacy behaviour: when a track's z-score exceeds the catalog-size-aware
    # dynamic threshold, the z-score alone is treated as strong single
    # evidence (the old engine used it to short-circuit source lookups).
    # Here it is added as an additive high-confidence source instead, so the
    # normal source lookups still run and report their results.
    z_standout = False
    try:
        from services.popularity.popularity_stats_service import calculate_artist_stats
        _, _, artist_vals = calculate_artist_stats(None, artist)
        artist_track_count = len(artist_vals or [])
        if artist_track_count >= 3 and max(album_z, artist_z) > 0:
            dyn_threshold = get_dynamic_z_threshold(
                artist_track_count,
                None,
                is_compilation,
            )
            if max(album_z, artist_z) >= dyn_threshold:
                z_standout = True
                reasons.append("z_score_standout")
    except Exception as exc:
        logger.debug("Dynamic z-score standout check failed for %s / %s: %s", artist, title, exc)

    # Discogs
    if use_advanced_detection:
        dr = _detect_discogs(lookup_title, artist, album, discogs_token, duration=duration,
                             is_special_edition=is_special)
        sources.append(dr)
        if dr["matched"]:
            discogs_confirmed = True
            reasons.append("discogs_matched")

    # MusicBrainz
    mr = _detect_musicbrainz(lookup_title, artist, artist_mbid, album_track_count, mb_client=mb_client,
                             mb_cached_singles=mb_cached_singles)
    sources.append(mr)
    if mr["matched"]:
        musicbrainz_confirmed = True
        reasons.append("musicbrainz_matched")

    # ── ListenBrainz top-10% evidence ───────────────────────────────────
    # When the artist's ListenBrainz top-10% listen threshold is available and
    # the track's own listen count meets it, that is strong community evidence
    # the track is a standout single. Cached per artist to avoid N+1 API calls.
    lb_top10 = False
    if listenbrainz_listens is not None and listenbrainz_listens > 0 and artist_mbid:
        _lb_ctx = _get_lb_artist_context_cached(artist_mbid)
        _lb_threshold = int(_lb_ctx.get("threshold") or 0)
        if _lb_threshold > 0 and int(listenbrainz_listens) >= _lb_threshold:
            lb_top10 = True
            reasons.append("lb_top10")

    # Check high confidence early-stop
    if check_high_confidence_dynamic(discogs_confirmed, musicbrainz_confirmed):
        pass  # continue to collect sources for reporting

    # Radio edit marker
    if has_single_or_radio_edit_marker(title):
        radio_edit_found = True
        reasons.append("radio_edit_marker")

    # Last.fm
    if lastfm_client:
        lastfm_confirmed = _detect_lastfm(artist, album or "", lastfm_client)
        if lastfm_confirmed:
            reasons.append("lastfm_confirmed")

    # Single release date proximity
    if discogs_confirmed or musicbrainz_confirmed:
        release_date = mr.get("metadata", {}).get("release_date") or dr.get("metadata", {}).get("release_year")
        if release_date and album:
            from services.popularity.popularity_stats_service import calculate_album_stats
            _, _, vals = calculate_album_stats(None, artist, album)
            # not used directly; just signal
            single_release_date_match = True
            reasons.append("release_date_match")

    # ── ISRC-based MusicBrainz release lookup ──
    # If the track has an ISRC, look it up on MusicBrainz to see if the
    # associated release is a single or EP.
    isrc_single_confirmed = False
    if isrc and not musicbrainz_confirmed:
        try:
            if mb_client is None:
                from api_clients.musicbrainz_http import MusicBrainzHttpClient
                mb_client = MusicBrainzHttpClient()
            recordings = mb_client.lookup_by_isrc(isrc, inc="releases")
            for recording in recordings:
                for release in recording.get("releases", []):
                    rg = release.get("release-group") or {}
                    pt = (rg.get("primary-type") or rg.get("primary_type") or "").lower()
                    if pt in ("single", "ep"):
                        isrc_single_confirmed = True
                        reasons.append("isrc_single")
                        break
                if isrc_single_confirmed:
                    break
        except Exception as exc:
            logger.debug("ISRC single lookup failed for %s: %s", isrc, exc)

    # ── Duration-based signal (weak supporting evidence) ──
    # Only used when other signals are absent. A typical single/radio-edit
    # duration under 4:30 is a weak corroborating signal.
    duration_support = False
    if duration and duration > 0 and duration < 270:  # < 4:30 minutes
        duration_support = True

    # ── Compilation check via MusicBrainz ──
    if musicbrainz_confirmed and mb_client and hasattr(mb_client, "appears_on_various_artists"):
        try:
            if mb_client.appears_on_various_artists(lookup_title, artist):
                mb_compilation_confirmed = True
                reasons.append("mb_compilation")
        except Exception:
            pass

    # ── Final decision ──
    max_z = max(album_z, artist_z)
    has_meta = discogs_confirmed or musicbrainz_confirmed
    is_title = normalize_title_strict(title) == normalize_title_strict(album or "")

    # ISRC confirmation counts as medium confidence
    if isrc_single_confirmed:
        musicbrainz_confirmed = True

    # Configurable z-score confidence boundaries (single_detection section).
    try:
        from services.popularity.popularity_config import get_zscore_thresholds
        _zth = get_zscore_thresholds()
        zscore_high = float(_zth.get("high", 1.0) or 1.0)
        zscore_medium = float(_zth.get("medium", 0.6) or 0.6)
    except Exception:
        zscore_high, zscore_medium = 1.0, 0.6

    # Per-source confidence levels (features.source_*_confidence) decide which
    # sources count as high / medium evidence. Defaults preserve the legacy
    # behaviour (Discogs + MusicBrainz = high, others = medium).
    _levels = _source_confidence_levels()
    high_sources = 0
    medium_sources = 0
    for _src, _confirmed in (
        ("discogs", discogs_confirmed),
        ("musicbrainz", musicbrainz_confirmed),
    ):
        if _confirmed and _levels.get(_src, "high") != "low":
            if _levels.get(_src, "high") == "high":
                high_sources += 1
            else:
                medium_sources += 1
    if discogs_video_confirmed and _levels.get("discogs_video", "medium") != "low":
        medium_sources += 1
    if lastfm_confirmed and _levels.get("lastfm", "medium") != "low":
        medium_sources += 1
    if mb_compilation_confirmed and _levels.get("musicbrainz_compilation", "medium") != "low":
        medium_sources += 1
    if (radio_edit_found or duration_support) and _levels.get("radio_edit", "medium") != "low":
        medium_sources += 1
    if single_release_date_match:
        medium_sources += 1
    if isrc_single_confirmed:
        medium_sources += 1
    if lb_top10:
        medium_sources += 1
    # A catalog-size-aware z-score standout counts as a high-confidence source
    # (legacy "z-score alone is strong evidence" behaviour).
    if z_standout:
        high_sources += 1

    final = determine_final_status(
        discogs=discogs_confirmed, musicbrainz=musicbrainz_confirmed,
        album_z=album_z, artist_z=artist_z,
        discogs_video=discogs_video_confirmed, lastfm=lastfm_confirmed,
        mb_compilation=mb_compilation_confirmed,
        radio_edit=radio_edit_found or duration_support,
        popularity=popularity or 0,
        album_mean=0, has_metadata=has_meta or isrc_single_confirmed,
        is_remastered_only=is_remastered,
        date_match=single_release_date_match,
        is_title_track=is_title,
        zscore_high=zscore_high,
        zscore_medium=zscore_medium,
        high_sources=high_sources,
        medium_sources=medium_sources,
    )

    # Soft z-gate cap: low-scoring tracks need two+ high-confidence sources to
    # reach 'high'; a single high source lands at 'medium' instead.
    if z_low and final == "high" and high_sources < 2:
        final = "medium"

    # `confidence` is a STRING LABEL ('high'/'medium'/'low') — every consumer
    # (star-rating stage, templates, edit modal) compares against these labels.
    # `confidence_score` carries the numeric 0.0-1.0 equivalent for any numeric
    # consumers (e.g. the legacy single_confidence_score column).
    label_map = {"high": "high", "medium": "medium", "none": "low"}
    score_map = {"high": 1.0, "medium": 0.67, "none": 0.0}
    is_single = final in ("high", "medium")

    result = {
        "is_single": is_single,
        "confidence": label_map.get(final, "low"),
        "confidence_score": score_map.get(final, 0.0),
        "sources": sources,
        "reasons": reasons or ["no_source_match"],
        "single_status": final,
    }

    if persist_result and track_repo and track_id:
        try:
            track_repo.update_track_single_status(track_id, is_single, result["confidence"])
        except Exception as exc:
            logger.debug("Persistence failed for %s: %s", track_id, exc)

    return result