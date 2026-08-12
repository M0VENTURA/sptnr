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

try:  # C-speed fuzzy matching — see _detect_musicbrainz title fallback
    from rapidfuzz import fuzz as _fuzz  # type: ignore[import-untyped]
    _HAVE_RAPIDFUZZ = True
except ImportError:  # pragma: no cover — stdlib fallback keeps matching working
    from difflib import SequenceMatcher as _difflib_matcher
    _HAVE_RAPIDFUZZ = False

from services.popularity.popularity_zscore import composite_listener_z

from helpers.normalization_service import (
    strip_single_release_suffix,
    normalize_title_for_lookup,
    normalize_title_for_lucene_query,
    strip_remaster_suffix,
    is_remastered_only_variant,
    strip_featured_artist,
    strip_featured_guest_suffix,
    edition_annotations_compatible,
)

from api_clients.musicbrainz_http import escape_lucene_special_chars

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

# Alternate/version markers that strip a track from single detection.
# Matched as WHOLE WORDS (word-boundary, optional plural) so ordinary titles
# never trip them — "edit" must not flag "(Epic Edition)".  "(Epic Edition)"
# versions are deliberately NOT stripped: they run detection with their full
# title so MusicBrainz release groups carrying the annotation still match
# (e.g. "Das Elfte Gebot (Epic Edition)" is a single on MB).  Same for cover
# annotations ("(PSY Cover)") — MB titles omit them, so the plain-title
# lookup already matches the cover's own single.  Remastered variants are
# intentionally NOT listed — same song as the original, remain
# single-eligible.
IGNORE_SINGLE_KEYWORDS = frozenset({
    "intro", "outro", "jam",
    "live", "unplugged",
    "remix", "edit", "mix",
    "acoustic", "orchestral",
    "demo", "instrumental", "karaoke",
})

_IGNORE_KEYWORD_RE = re.compile(
    r"\b(?:" + "|".join(
        re.escape(kw) + r"(?:es|s)?" for kw in sorted(IGNORE_SINGLE_KEYWORDS, key=len, reverse=True)
    ) + r")\b",
    re.IGNORECASE,
)

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


def has_single_or_radio_edit_marker(title: str) -> bool:
    """Return True when title contains canonical single/radio edit markers."""
    return bool(re.search(r'\b(?:radio\s+(?:edit|mix|version)|single\s+(?:version|edit|mix))\b',
                          title or "", re.IGNORECASE))


# ── Stage 0: Album-type helpers ───────────────────────────────────────────

def is_compilation_album(album_type: str | None, album_title: str) -> bool:
    if album_type and ("compilation" in album_type.lower() or "soundtrack" in album_type.lower()):
        return True
    t = (album_title or "").lower()
    if "various artists" in t:
        return True
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

    Only genuine version markers strip a track (whole-word keywords from
    ``_IGNORE_KEYWORD_RE``: live, remix, edit, acoustic, ...).  "(Epic
    Edition)" and cover annotations like "(PSY Cover)" do NOT strip — those
    versions run detection with their full title so MusicBrainz release
    groups that carry the annotation (e.g. "Das Elfte Gebot (Epic Edition)"
    is a single) still match.  Remastered variants are intentionally NOT
    skipped either — they are the same song as the original and remain
    eligible for single detection.

    Compilation / Various-Artists albums are deliberately NOT skipped by
    album type: every track on a compilation has a different artist, so ALL
    tracks are checked as singles (the scan pipeline already bypasses the
    top-50% popularity gate for them).  Live albums still skip entirely.
    """
    t = (title or "").lower()
    at = (album_type or "").lower()
    if _IGNORE_KEYWORD_RE.search(t):
        return True
    if "live" in at:
        return True
    return False


# ── Stage 2: Z-score calculation (median + MAD) ───────────────────────────

def calculate_z_score_strict(popularity: float, pop_median: float, pop_mad_scaled: float) -> float:
    """Robust z-score using median + scaled MAD."""
    if pop_mad_scaled == 0:
        return 0.0
    return (popularity - pop_median) / pop_mad_scaled


def _parse_release_year(value) -> int | None:
    """Best-effort 4-digit year from a ``YYYY-MM-DD`` / ``YYYY`` release value."""
    if not value:
        return None
    try:
        match = re.search(r"(?<!\d)(\d{4})(?!\d)", str(value).strip())
        return int(match.group(1)) if match else None
    except Exception:
        return None


def _album_release_year(artist: str, album: str | None) -> int | None:
    """Earliest stored release year for an album (tracks table).

    The album's own release metadata — the reference the single-before-album
    check compares the matched single release date against.  ``None`` when the
    album has no stored year (no DB access / still scanning).
    """
    if not album:
        return None
    try:
        from sqlalchemy import text as _text
        from db.engine import db_session
        with db_session() as session:
            row = session.execute(
                _text(
                    "SELECT MIN(release_year) AS y FROM tracks "
                    "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist "
                    "AND album = :album AND release_year IS NOT NULL"
                ),
                {"artist": artist, "album": album},
            ).first()
            year = row._mapping.get("y") if row else None
        return int(year) if year else None
    except Exception:
        return None


# A commercial single is typically issued BEFORE its parent album — the
# matched Discogs/MB release year leads the album's stored release year.
# The release-date signal only fires when that lead is unambiguous (at least
# this many years), so a same-year "album track with a single release match"
# never injects a phantom corroboration.
SINGLE_RELEASE_LEAD_YEARS = 1


def get_dynamic_z_threshold(track_count: int, release_year: int | None = None, is_compilation: bool = False) -> float:
    """Calculate dynamic z-score threshold based on catalog size and release date.

    Thresholds sit around 1.6-1.8: a track one and a half to two scaled-MADs
    above the artist median is a genuine popularity outlier.  Larger catalogs
    use a *lower* threshold because their z-distributions compress (more
    tracks cluster the top-of-catalog scores closer together), so the same
    absolute z-score is a stronger signal there.
    """
    if track_count < 5:
        threshold = 1.5
    elif track_count < 10:
        threshold = 1.7
    elif track_count < 50:
        threshold = 1.8
    elif track_count < 200:
        threshold = 1.7
    else:
        threshold = 1.6
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

    Mirrors the legacy ``source_*_confidence`` knobs. Defaults match legacy:
    Discogs is a high-confidence source; MusicBrainz and the rest are medium.
    ``low`` excludes a source from the confidence decision.
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
        "musicbrainz": "medium",
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


def _is_promo_only_group(group: dict[str, Any]) -> bool:
    """True when EVERY release of a release-group is promotional.

    MusicBrainz editors tag promo-only releases with status "Promotion"
    (e.g. "+44 - Cliff Diving").  A promo-only single is genuine
    confirmation the track was issued as a single, but it is promotional
    evidence — weaker than a commercial single (mirrors the Discogs
    promo downgrade).
    """
    releases = group.get("releases") or []
    statuses = [
        str(r.get("status") or "").strip().lower()
        for r in releases
        if isinstance(r, dict) and r.get("status")
    ]
    return bool(statuses) and all(s == "promotion" for s in statuses)


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
        promo_only = False
        # Query-sanitised title: strip the trailing featured-guest credit so
        # "Uncontrolled (feat. Charlie Rolfe of As Everything Unfolds)" is
        # queried as "Uncontrolled" — MusicBrainz release-group titles and
        # Discogs release titles rarely carry the guest credit, so the raw
        # title scored ~0.0 similarity and dropped real singles to low.
        clean_title = strip_featured_guest_suffix(
            strip_single_release_suffix(title) or title
        )
        # Preferred path: scope the search to the artist's own singles/EPs
        # release-groups when the artist MBID is known. Far more reliable than
        # fuzzy recording title matching, and immune to recording-split issues
        # (album version vs single version being separate MB recordings).
        if artist_mbid and hasattr(mb_client, "search_release_groups"):
            try:
                from difflib import SequenceMatcher as _SM
                target = normalize_title_for_lookup(clean_title)
                lucene_title = normalize_title_for_lucene_query(clean_title)
                candidates: list[dict] = []
                for _pt in ("single", "ep"):
                    # Title-scoped query first — an artist with 25+ singles
                    # can otherwise hide the target beyond the client's page
                    # cap (search_release_groups caps at 25).
                    try:
                        _found = mb_client.search_release_groups(
                            f'arid:{artist_mbid} AND primarytype:{_pt} '
                            f'AND releasegroup:"{escape_lucene_special_chars(lucene_title)}"',
                            limit=25,
                        ) or []
                    except Exception:
                        _found = []
                    if not _found:
                        # Tokenisation drift (punctuation, apostrophes) — fall
                        # back to the artist-scoped list and match by title
                        # similarity.
                        try:
                            _found = mb_client.search_release_groups(
                                f"arid:{artist_mbid} AND primarytype:{_pt}", limit=100
                            ) or []
                        except Exception:
                            continue
                    candidates += _found
                for group in candidates:
                    rg_title = str(group.get("title") or "")
                    # An edition-annotated track ("Valhalla (Epic Edition)")
                    # must only match a release group carrying the SAME
                    # edition annotation — never the plain "Valhalla" single.
                    # Brackets are stripped by normalize_title_for_lookup on
                    # both sides, so without this gate the epic-edition track
                    # collides with the non-edition single's normalized title.
                    if not edition_annotations_compatible(title, rg_title):
                        continue
                    norm_rg = normalize_title_for_lookup(strip_single_release_suffix(rg_title) or rg_title)
                    # Exact normalized equality first, then fuzzy fallback for
                    # residual punctuation/case drift between sources.
                    # RapidFuzz token_set_ratio (C-speed, order-insensitive)
                    # with a difflib fallback for the non-rapidfuzz installs.
                    if norm_rg == target or (
                        (_fuzz.token_set_ratio(norm_rg, target) / 100.0) >= 0.85
                        if _HAVE_RAPIDFUZZ
                        else _SM(None, norm_rg, target).ratio() >= 0.85
                    ):
                        matched = True
                        promo_only = _is_promo_only_group(group)
                        break
            except Exception as exc:
                logger.debug("Artist-scoped MB single lookup failed for %s / %s: %s", artist, title, exc)

        # Fallback: per-recording fuzzy match via the service.
        if not matched:
            # Use the client's is_single method if available, otherwise use the service.
            if hasattr(mb_client, "is_single"):
                matched = bool(mb_client.is_single(clean_title, artist, artist_mbid=artist_mbid,
                                                    album_track_count=album_track_count))
            else:
                from services.enrichment.musicbrainz_service import MusicBrainzService
                svc = MusicBrainzService(enabled=True)
                matched = bool(svc.is_single(clean_title, artist, album_track_count=album_track_count))
        release_date = None
        if matched:
            try:
                if hasattr(mb_client, "get_single_release_date"):
                    release_date = mb_client.get_single_release_date(title, artist, artist_mbid=artist_mbid)
            except Exception:
                pass
        metadata: dict[str, Any] = {}
        if release_date:
            metadata["release_date"] = release_date
        if matched and promo_only:
            metadata["is_promo"] = True
        return {"source": "musicbrainz", "matched": matched, "confidence": 0.9 if matched else 0.0,
                "metadata": metadata}
    except Exception as exc:
        logger.debug("MusicBrainz single detection failed for %s / %s: %s", artist, title, exc)
        return {"source": "musicbrainz", "matched": False, "confidence": 0.0,
                "metadata": {}, "error": True}


def _detect_discogs(title: str, artist: str, album: str | None,
                    discogs_token: str | None, duration: float | None = None,
                    is_special_edition: bool = False,
                    cached_single_titles: set | None = None,
                    cached_promo_titles: set | None = None) -> dict[str, Any]:
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
    # Fast path: the title is already known to be a Discogs single from the
    # artist's cached release list (avoids one Discogs API call per track).
    # Exact list membership = perfect title similarity, verified artist — so
    # this is always HIGH confidence (a cached promo is downgraded to a
    # medium source downstream via its ``is_promo`` flag, never by score).
    if cached_single_titles:
        normalized = (title or "").lower().strip()
        if normalized in {str(t).lower().strip() for t in cached_single_titles}:
            is_promo = bool(cached_promo_titles) and normalized in {
                str(t).lower().strip() for t in cached_promo_titles
            }
            return {"source": "discogs", "matched": True,
                    "confidence": 0.85,
                    "metadata": {"is_promo": is_promo, "similarity_ratio": 1.0},
                    "cached": True}
    try:
        from services.enrichment.discogs_service import (
            _get_service as _get_discogs_service,
            calculate_discogs_confidence,
        )
        # Use the shared per-token service so the per-artist release list and
        # single verdicts are cached across tracks — a fresh DiscogsService per
        # track would re-fetch the artist's releases for EVERY track (N+1).
        svc = _get_discogs_service(token)
        ctx = {"album": album, "is_special_edition": is_special_edition} if album else {"is_special_edition": is_special_edition}
        if hasattr(svc, "get_single_status"):
            st = svc.get_single_status(title, artist, album_context=ctx)
            matched_raw = bool(st.get("is_single"))
            is_promo = bool(st.get("is_promo"))
            year = st.get("release_year")
            similarity = float(st.get("similarity") or 0.0)
            artist_verified = bool(st.get("artist_verified", False))
        else:
            matched_raw = bool(svc.is_single(title, artist, album_context=ctx))
            is_promo = False
            year = None
            similarity = 1.0
            artist_verified = True
            if matched_raw and hasattr(svc, "get_single_release_year"):
                year = svc.get_single_release_year(title, artist)
        # Dynamic confidence: base weight (0.85) × title similarity ratio ×
        # penalties (unverified artist → half; short/generic title → 0.6
        # unless near-exact). The scan already strips single-release suffixes
        # (``lookup_title``) and both sides normalize brackets/punctuation,
        # so "New Way Out (Radio Edit)" vs "New Way Out" scores ~1.0, not 0.70.
        calc = calculate_discogs_confidence(title, similarity, artist_verified)
        matched = bool(matched_raw and calc["matched"])
        metadata: dict[str, Any] = {"is_promo": is_promo}
        metadata.update(calc.get("metadata") or {})
        if year:
            metadata["release_year"] = year
        return {"source": "discogs", "matched": matched,
                "confidence": calc["confidence"] if matched else 0.0,
                "metadata": metadata}
    except Exception as exc:
        logger.debug("Discogs single detection failed for %s / %s: %s", artist, title, exc)
        return {"source": "discogs", "matched": False, "confidence": 0.0,
                "metadata": {}, "error": True}


def _detect_discogs_video(title: str, artist: str,
                          discogs_token: str | None = None) -> dict[str, Any]:
    """Medium-confidence check: official/promo music video on Discogs.

    Legacy parity — the old scanner confirmed singles via the track's
    official video on Discogs (``has_official_video``). Video presence is
    corroborating evidence, never primary: it counts as a medium source.
    """
    token = discogs_token or os.environ.get("DISCOGS_TOKEN", "")
    if not token:
        try:
            from helpers.config_helpers import get_config
            cfg = get_config()
            token = (cfg.get("api_integrations", {}).get("discogs", {}) or {}).get("token", "") or ""
        except Exception:
            token = ""
    if not token or token.lower() in ("your_discogs_token", "your_token", "placeholder"):
        return {"source": "discogs_video", "matched": False, "confidence": 0.0, "metadata": {}}
    try:
        from services.enrichment.discogs_service import _get_service
        svc = _get_service(token)
        matched = bool(svc.has_official_video(title, artist)) if hasattr(svc, "has_official_video") else False
        return {"source": "discogs_video", "matched": matched,
                "confidence": 0.5 if matched else 0.0, "metadata": {}}
    except Exception as exc:
        logger.debug("Discogs video detection failed for %s / %s: %s", artist, title, exc)
        return {"source": "discogs_video", "matched": False, "confidence": 0.0, "metadata": {}}


def _detect_lastfm(artist: str, album: str, title: str, lastfm_client=None) -> bool:
    """Medium-confidence check: Last.fm release evidence for a single.

    Legacy parity: (1) the track itself exists as a single/EP release on
    Last.fm (album payload named after the track with < 6 tracks), or
    (2) the track's album has 1-3 tracks (single), or 4-6 tracks with a
    title track (EP).

    Fallback: Last.fm often stores the single under a suffixed release name
    ("Knightclub - Single") while ``album.getInfo`` with the track title
    resolves to the full LP (a 20-track "Knightclub") — ``album.search``
    surfaces those Single/EP rows directly.
    """
    if not lastfm_client:
        return False
    try:
        if lastfm_client.check_track_as_single(artist, title):
            return True
    except Exception:
        pass
    # Album-track-count evidence: 1-3 tracks = single, 4-6 = EP (title-track).
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
        count = 0
    # Search-based fallback — only when the cheap album-count evidence did
    # not settle it: Last.fm often stores the single under a suffixed release
    # name ("Knightclub - Single") while ``album.getInfo`` with the track
    # title resolves to the full LP (a 20-track "Knightclub").  ``album.search``
    # surfaces those Single/EP rows directly.
    if count == 0 or count >= 7:
        try:
            if hasattr(lastfm_client, "search_album"):
                target = normalize_title_for_lookup(strip_featured_guest_suffix(title) or title)
                # Only "- Single"/"- EP" suffixed rows are single evidence —
                # a bare album named after the track is a scrobble-derived
                # entry (users tagging files with the track name as album),
                # not a release marker.
                single_marker = re.compile(
                    r"\s*[-–—]?\s*(?:single|ep)\s*$", flags=re.IGNORECASE
                )
                for alb in lastfm_client.search_album(title, artist=artist, limit=30) or []:
                    alb_name = str(alb.get("name") or "").strip()
                    if not alb_name or not single_marker.search(alb_name):
                        continue
                    base = single_marker.sub("", alb_name).strip()
                    if normalize_title_for_lookup(base) == target:
                        return True
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
    is_title_track: bool = False, is_compilation: bool = False,
    zscore_high: float = 1.0, zscore_medium: float = 0.6,
    high_sources: int | None = None, medium_sources: int | None = None,
    discogs_promo: bool = False, musicbrainz_promo: bool = False,
    z_standout: bool = False,
) -> str:
    """Final single status based on source detection and z-score analysis.

    ``zscore_high`` / ``zscore_medium`` are the configurable confidence
    boundaries (``single_detection.zscore_high_threshold`` /
    ``zscore_medium_threshold``). ``high_sources`` / ``medium_sources``
    optionally override the source-confidence counts (used to honour the
    per-source ``source_*_confidence`` config knobs). ``discogs_promo`` /
    ``musicbrainz_promo`` mark a confirmation that came from a promo-only
    release — promotional evidence caps the verdict at 'medium' unless an
    independent high-confidence source also confirms. ``z_standout`` marks a
    catalog-size-aware popularity standout (dynamic z threshold) — it is NOT
    single evidence on its own (a popular album track is not a single), but
    it bolsters a track that already carries medium-confidence evidence to
    'high' when the track's z-score hits the standout range. ``is_compilation``
    disables popularity entirely: every track on a compilation has a different
    artist, so the z-score bands and ``z_standout`` are ignored and the verdict
    is decided by the metadata sources alone. Returns ``'high'``,
    ``'medium'``, or ``'none'``.

    Popularity z is evaluated against the ALBUM's own tracklist (the local
    baseline): ``album_z`` is used when present, falling back to ``artist_z``
    only when no album-level baseline exists.  Using ``max(album_z, artist_z)``
    let an album that is itself a standout vs the rest of an artist's catalogue
    promote EVERY one of its tracks (inflated artist_z) — e.g. 36 Crazyfists -
    "Bury Me Where I Fall" reached 'high' with album_z 0.19 purely on
    artist_z 2.78.
    """
    max_z = album_z if album_z else artist_z
    # Compilation / Various-Artists albums: every track has a different
    # artist, so album/artist-relative popularity (z-score) is meaningless
    # for single detection.  Zero ``max_z`` so the z-score bands and the
    # z_standout bump never factor — the verdict is decided purely by the
    # metadata sources (Discogs / MusicBrainz / ISRC / radio-edit / Last.fm).
    if is_compilation:
        max_z = 0.0
    if high_sources is not None and medium_sources is not None:
        high = high_sources
        medium = medium_sources
    else:
        high = sum([discogs, musicbrainz])
        medium = sum([discogs_video, lastfm, mb_video, mb_compilation, radio_edit, date_match])

    # A promo-only Discogs match is genuine confirmation the track was issued
    # as a (promotional) single, but never high-confidence on its own — legacy
    # parity: promo releases resolved to medium. With no independent high
    # source the verdict is capped at 'medium' regardless of z-score band.
    if discogs_promo and high == 0:
        return 'medium'
    # Same for MusicBrainz promo-only release groups (status "Promotion",
    # e.g. "+44 - Cliff Diving"): promotional evidence caps at 'medium' unless
    # an independent high-confidence source also confirms.
    if musicbrainz_promo and high == 0:
        return 'medium'

    # Z-score above the high boundary. 'high' needs real external
    # confirmation (Discogs/MusicBrainz/ISRC); two corroborating weak signals
    # (z-standout + duration/radio-edit marker, etc.) also reach 'high' — the
    # legacy rule was ``high >= 1 OR medium >= 2``. A single weak signal with
    # no other evidence still returns 'none' (that produced false positives
    # like Tehran / Crossroads, z ≈ 1.2, zero source matches).
    if max_z >= max(0.0, zscore_high):
        if high >= 1 or medium >= 2:
            verdict = 'high'
        # A catalog-size-aware popularity standout is NOT single evidence on
        # its own — a popular album track is not a single (z≈1.2
        # Tehran/Crossroads false positives were the cautionary tale). It
        # only BOLSTERS a track that already carries medium-confidence
        # evidence: when a genuinely dominant outlier (z-score in the
        # standout range, e.g. District 9 at ~1.8 with a Last.fm
        # confirmation) is also backed by a medium source, the medium
        # evidence is promoted to 'high'. With no medium evidence the
        # standout alone still returns 'none'.
        elif z_standout and medium >= 1:
            verdict = 'high'
        else:
            verdict = 'none'

    # Z-score between the medium and high boundaries
    elif max_z > max(0.0, zscore_medium):
        if high >= 1:
            verdict = 'high'
        # A medium-band z-score is medium-confidence single evidence when at
        # least two weak signals corroborate it (legacy parity: the legacy
        # engine required ``medium >= 2`` in this band). A single weak signal
        # (radio edit, ISRC, …) must not flag every mid-album track as a
        # single. Popularity alone is NEVER single evidence — the old
        # metadata-poor fallback here flagged every mid-album track with a
        # z-score in the 0.6-1.0 band as a single (Human Era / Ph4/NT0mA /
        # Buried in Code on Unleash the Archers - Phantoma all got 'medium'
        # with zero sources).
        elif medium >= 2:
            verdict = 'medium'
        else:
            verdict = 'none'

    # Z-score <= 0: remastered bypass, title-track boost, or metadata evidence.
    # Title-track boost: a track sharing the album name is a classic single —
    # but ONLY with real confirmation (a MusicBrainz/Discogs/ISRC single
    # match). Weak signals (duration under 4:30, music video, popularity
    # standout, album-size heuristics) must never flag a title track without
    # checking the recording's actual release type — most tracks are under
    # 4:30, so duration alone would flag nearly every album's title track as
    # a single.
    else:
        if is_remastered_only or high >= 1 or medium >= 2:
            if high >= 1:
                verdict = 'high'
            elif medium >= 1 and not is_title_track:
                verdict = 'medium'
            else:
                verdict = 'none'
        # Title-track boost (legacy parity): a title track corroborated by
        # ANY weak source (radio-edit marker, ISRC, Last.fm track count, ...)
        # reaches 'medium' even when its popularity sits at or below the
        # album median. The single version of a title track routinely has a
        # smaller stream share than the album version, so a low z-score
        # reflects that split rather than the absence of single status. With
        # no weak evidence at all it still requires real metadata
        # confirmation above.
        elif is_title_track and medium >= 1:
            verdict = 'medium'
        else:
            verdict = 'none'

    # Authoritative floor: Discogs/MusicBrainz/ISRC confirming the track as a
    # single means the track IS a single — the z-score bands above only
    # refine high vs medium and must never demote a confirmed single to
    # 'none' (MB-only singles with low z-scores were vanishing entirely,
    # e.g. Unleash the Archers / +44 scans where only the z-standout tracks
    # surfaced). Weak signals (radio-edit marker, LB top-10, ...) without a
    # real metadata confirmation are still gated above.
    if verdict == 'none' and (discogs or musicbrainz) and (high >= 1 or medium >= 1):
        return 'high' if high >= 1 else 'medium'
    return verdict


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
    discogs_cached_singles: set | None = None,
    discogs_cached_promos: set | None = None,
    artist_mbid: str | None = None,
    mb_client=None,
    listenbrainz_listens: int | None = None,
    lastfm_listeners: int | None = None,
    album_lf_listeners: list[float] | None = None,
    album_lb_listens: list[float] | None = None,
    is_va_compilation: bool | None = None,
) -> dict[str, Any]:
    """Detect whether a track is a single using the 8-stage algorithm.

    When the scan pipeline has already classified the album with the
    VA-vs-single-artist split (``is_va_compilation`` provided, True or False),
    that verdict REPLACES the title/type heuristic: only TRUE Various-Artists
    compilations bypass the popularity z-gates, while single-artist
    compilations (Greatest Hits tagged "compilation") keep the normal gates.
    When ``None`` (standalone callers / tests), the legacy
    ``is_compilation_album`` heuristic applies unchanged.

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

    # The scan pipeline's VA classification is authoritative when available:
    # a single-artist compilation ("Queen - Greatest Hits", type
    # "compilation") is treated like a standard studio album here — z-score
    # gates and the popularity-standout signal run normally.  Only TRUE
    # Various-Artists compilations bypass them (every track has a different
    # artist, so artist/album-relative popularity is meaningless).  Without
    # the flag (standalone calls) the legacy title/type heuristic applies.
    if is_va_compilation is not None:
        is_compilation = is_va_compilation
    else:
        is_compilation = is_compilation_album(album_type, album or "")
    is_special = is_special_edition_album(album or "")
    is_remastered = is_remastered_only_variant(title)

    # ── Calculate z-scores (median + MAD) ──
    # The popularity-stats lookups group by ``album_artist`` in the DB, so a
    # featured-artist track ("Stray Kids feat. Tablo") stores its popularity
    # under the album artist ("Stray Kids").  Resolve the artist that actually
    # has stats (raw name first, then the canonical feature-stripped name) so
    # z-scores and the popularity-standout signal still compute for collabs.
    #
    # Compilation / Various-Artists albums SKIP this entirely: every track has
    # a different artist, so album/artist-relative popularity (z-score) is
    # meaningless for single detection — the verdict is purely source-based
    # (Discogs / MusicBrainz / ISRC / radio-edit / Last.fm ...).  Zeroed
    # z-scores also keep z_standout and the popularity marking from firing.
    _stats_artist = artist
    artist_vals: list[float] = []
    album_vals: list[float] = []
    album_z = 0.0
    artist_z = 0.0
    if popularity is not None and popularity > 0 and not is_compilation:
        try:
            # Artist-level stats
            from services.popularity.popularity_stats_service import calculate_artist_stats, calculate_album_stats

            _, _, _raw_vals = calculate_artist_stats(None, _stats_artist)
            if not _raw_vals:
                _canon = strip_featured_artist(_stats_artist)
                if _canon and _canon != _stats_artist:
                    _, _, _canon_vals = calculate_artist_stats(None, _canon)
                    if _canon_vals:
                        _stats_artist = _canon
                        _raw_vals = _canon_vals
            artist_vals = _raw_vals

            if artist_vals:
                art_med = stat_median(artist_vals)
                art_mad = stat_median([abs(v - art_med) for v in artist_vals]) if artist_vals else 0
                # Adaptive spread floor (same rule as popularity_math): a
                # uniform high-scoring catalogue must not amplify tiny score
                # gaps into large z-swings.
                art_spread = max(art_mad * 1.4826, 10.0, 0.10 * art_med)
                artist_z = (popularity - art_med) / art_spread if art_spread > 0 else 0

            if album:
                _, _, album_vals = calculate_album_stats(None, _stats_artist, album)
                if album_vals:
                    alb_med = stat_median(album_vals)
                    alb_mad = stat_median([abs(v - alb_med) for v in album_vals]) if album_vals else 0
                    alb_spread = max(alb_mad * 1.4826, 10.0, 0.10 * alb_med)
                    album_z = (popularity - alb_med) / alb_spread if alb_spread > 0 else 0
        except Exception as exc:
            logger.debug("Z-score calculation failed: %s", exc)

    # ── Composite album-local z-score (raw listener counts) ────────────────
    # The popularity-score z above (``album_z`` / ``artist_z``) is computed
    # against stored blended/decay-adjusted ``final_score`` values, which
    # compress the standout signal (and can lag mid-scan).  The composite
    # blends the track's RAW Last.fm listeners and ListenBrainz listens,
    # each log-scaled against ITS OWN ALBUM's tracklist, so it directly
    # answers "is this track a standout within its album?"  Used as the
    # popularity-z for the single verdict and the ``z_standout`` gate.
    z_composite = 0.0
    if popularity is not None and popularity > 0 and not is_compilation:
        z_composite = composite_listener_z(
            lastfm_listeners,
            listenbrainz_listens,
            _stats_artist,
            album,
            album_lf_listeners=album_lf_listeners,
            album_lb_listens=album_lb_listens,
        )

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
    # dynamic threshold, the z-score alone was treated as strong single
    # evidence (the old engine used it to short-circuit source lookups).
    # Here the normal source lookups still run and report their results, and
    # the standout acts as a popularity confirmation — it is NOT a medium
    # source on its own, but in the high z-band it bolsters a track that
    # already has medium-confidence evidence to 'high' (see
    # determine_final_status).
    z_standout = False
    try:
        # artist_vals was fetched above for the z-scores; reuse it here so a
        # track costs one artist-stats query, not two.  On the FIRST scan of an
        # artist the artist-stats table is empty (no stored scores yet), so the
        # catalogue-size proxy falls back to the album's own track count —
        # otherwise a genuinely dominant album track (District 9, album-z ~1.8)
        # would never qualify as a standout on the pass that first scores it.
        artist_track_count = max(len(artist_vals or []), len(album_vals or []))
        # ALBUM-LOCAL baseline only: the composite listener z (or the album
        # popularity z when no raw listener data exists) decides the standout —
        # never the artist-wide z.  A standout album vs the rest of an artist's
        # catalogue inflates every track's artist_z (36 Crazyfists: 7/12 tracks
        # on Bitterness the Star had artist_z >= 2.6 but album z <= 0.9), so
        # max(album_z, artist_z) promoted whole albums.
        standout_z = z_composite or album_z
        if artist_track_count >= 3 and standout_z > 0:
            dyn_threshold = get_dynamic_z_threshold(
                artist_track_count,
                None,
                is_compilation,
            )
            if standout_z >= dyn_threshold:
                z_standout = True
                reasons.append("z_score_standout")
    except Exception as exc:
        logger.debug("Dynamic z-score standout check failed for %s / %s: %s", artist, title, exc)

    # Discogs
    dr: dict[str, Any] = {"source": "discogs", "matched": False, "confidence": 0.0, "metadata": {}}
    if use_advanced_detection:
        dr = _detect_discogs(lookup_title, artist, album, discogs_token, duration=duration,
                             is_special_edition=is_special,
                             cached_single_titles=discogs_cached_singles,
                             cached_promo_titles=discogs_cached_promos)
        sources.append(dr)
        if dr["matched"]:
            discogs_confirmed = True
            reasons.append("discogs_matched")
    # A promo-only Discogs release is weaker evidence than a commercial single
    # (legacy parity: promos resolved to medium confidence). It still confirms
    # the track was issued as a single, so it stays a confirmed source — it is
    # simply counted as a medium source below and capped at 'medium'.
    discogs_promo = bool((dr.get("metadata") or {}).get("is_promo"))

    # MusicBrainz
    mr = _detect_musicbrainz(lookup_title, artist, artist_mbid, album_track_count, mb_client=mb_client,
                             mb_cached_singles=mb_cached_singles)
    sources.append(mr)
    if mr["matched"]:
        musicbrainz_confirmed = True
        reasons.append("musicbrainz_matched")
    elif mr.get("error"):
        reasons.append("mb_unavailable")
    # Promo-only release groups (status "Promotion") are weaker evidence than
    # a commercial single — downgraded to a MEDIUM source below.
    musicbrainz_promo = bool((mr.get("metadata") or {}).get("is_promo"))

    # Surface source-API failures so a flaky scan's zero-single verdict is
    # distinguishable from a genuine miss (the track-stage log shows the
    # reasons list).
    if dr.get("error"):
        reasons.append("discogs_unavailable")

    # Discogs official-video evidence (MEDIUM confidence, legacy parity): a
    # track with an official/promo music video on Discogs was issued as a
    # single. Runs only when neither Discogs nor MusicBrainz confirmed — it
    # corroborates weak evidence, it is not a primary source.
    if use_advanced_detection and not discogs_confirmed and not musicbrainz_confirmed:
        dv = _detect_discogs_video(lookup_title, artist, discogs_token)
        if dv.get("matched"):
            discogs_video_confirmed = True
            reasons.append("discogs_video")

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
        lastfm_confirmed = _detect_lastfm(artist, album or "", lookup_title, lastfm_client)
        if lastfm_confirmed:
            reasons.append("lastfm_confirmed")

    # Single release date proximity (TRUE check): a commercial single is
    # typically issued before its parent album, so the matched release year
    # LEADING the album's stored release year by at least
    # ``SINGLE_RELEASE_LEAD_YEARS`` is genuine corroborating evidence.
    # Previously this fired UNCONDITIONALLY whenever Discogs/MusicBrainz
    # confirmed the track — injecting a phantom +1 ``medium_sources`` that
    # distorted corroboration counts (the signal was derived from the very
    # match it claimed to back).
    if discogs_confirmed or musicbrainz_confirmed:
        single_release_year = _parse_release_year(
            mr.get("metadata", {}).get("release_date")
            or dr.get("metadata", {}).get("release_year")
        )
        album_release_year = _album_release_year(artist, album)
        if (
            single_release_year
            and album_release_year
            and (album_release_year - single_release_year) >= SINGLE_RELEASE_LEAD_YEARS
        ):
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

    # ── Compilation check via MusicBrainz ──
    if musicbrainz_confirmed and mb_client and hasattr(mb_client, "appears_on_various_artists"):
        try:
            if mb_client.appears_on_various_artists(lookup_title, artist):
                mb_compilation_confirmed = True
                reasons.append("mb_compilation")
        except Exception:
            pass

    # ── Append corroborating evidence to the sources list (UI visibility) ──
    # The track page's detection-source table reads `single_sources`; only
    # Discogs/MusicBrainz entries were stored there, so tracks confirmed via
    # ISRC, ListenBrainz top-10%, z-standout etc. showed high confidence with
    # an all-"Not Detected" source table.
    for _src_flag, _src_name in (
        (isrc_single_confirmed, "isrc"),
        (lb_top10, "listenbrainz_top10"),
        (z_standout, "popularity_z_standout"),
        (radio_edit_found, "radio_edit"),
        (mb_compilation_confirmed, "musicbrainz_compilation"),
        (lastfm_confirmed, "lastfm"),
        (discogs_video_confirmed, "discogs_video"),
        (single_release_date_match, "release_date_match"),
    ):
        if _src_flag:
            sources.append({"source": _src_name, "matched": True, "confidence": 0.5})

    # ── Final decision ──
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
    # behaviour (Discogs + MusicBrainz = high, others = medium). A promo-only
    # Discogs match is downgraded to a MEDIUM source — promotional evidence,
    # not commercial-single confirmation (legacy parity).
    #
    # Discogs at sub-full confidence (fuzzy match) is ALSO a MEDIUM source: a
    # lone fuzzy Discogs match must not grant 'high' on its own (that produced
    # false-positive 5★ singles from a single partial match).  Only a
    # near-exact, artist-verified title match counts as high — the dynamic
    # confidence formula scores those at 0.85 (base 0.85 × ratio ≈ 1.0), so
    # the full-confidence gate is 0.85, not 1.0.
    _discogs_full_confidence = discogs_confirmed and float(dr.get("confidence") or 0) >= 0.85
    _levels = _source_confidence_levels()
    high_sources = 0
    medium_sources = 0
    for _src, _confirmed in (
        ("discogs", discogs_confirmed),
        ("musicbrainz", musicbrainz_confirmed),
    ):
        if not _confirmed or _levels.get(_src, "high") == "low":
            continue
        _level = _levels.get(_src, "high")
        if _src == "discogs" and not _discogs_full_confidence and _level == "high":
            _level = "medium"
        if _src == "discogs" and discogs_promo and _level == "high":
            _level = "medium"
        if _src == "musicbrainz" and musicbrainz_promo and _level == "high":
            _level = "medium"
        if _level == "high":
            high_sources += 1
        else:
            medium_sources += 1
    if discogs_video_confirmed and _levels.get("discogs_video", "medium") != "low":
        medium_sources += 1
    if lastfm_confirmed and _levels.get("lastfm", "medium") != "low":
        medium_sources += 1
    if mb_compilation_confirmed and _levels.get("musicbrainz_compilation", "medium") != "low":
        medium_sources += 1
    if radio_edit_found and _levels.get("radio_edit", "medium") != "low":
        medium_sources += 1
    if single_release_date_match:
        medium_sources += 1
    if isrc_single_confirmed:
        medium_sources += 1
    if lb_top10:
        medium_sources += 1
    # A catalog-size-aware z-score standout is popularity evidence, NOT a
    # medium source — a popular album track is not a single, so it must not
    # stack into ``medium >= 2`` on its own. It only BOLSTERS existing
    # medium-confidence evidence to 'high' when the z-score is in the
    # standout range (handled inside determine_final_status), leaving tracks
    # with no real sources (Tehran/Crossroads, z≈1.2) unflagged.
    #
    # Independent medium corroboration for a sub-0.85 Discogs match: the
    # release-date signal is now genuinely INDEPENDENT evidence (the album's
    # STORED release year vs the matched release date — the album year comes
    # from the tracks table, not from the Discogs match itself), so it
    # legitimately counts toward the "second medium confidence method" that
    # promotes Discogs to high.
    _discogs_med_slot = 1 if (
        discogs_confirmed
        and _levels.get("discogs", "high") != "low"
        and not _discogs_full_confidence
    ) else 0
    _corroborating_medium = medium_sources - _discogs_med_slot

    final = determine_final_status(
        discogs=discogs_confirmed, musicbrainz=musicbrainz_confirmed,
        # The single verdict uses the ALBUM-LOCAL z only: the raw-listener
        # composite when available, else the album popularity z.  artist_z is
        # zeroed here so an inflated artist-wide z (a standout album) can never
        # promote an album-local non-standout to 'high'.
        album_z=z_composite or album_z, artist_z=0.0,
        discogs_video=discogs_video_confirmed, lastfm=lastfm_confirmed,
        mb_compilation=mb_compilation_confirmed,
        radio_edit=radio_edit_found,
        popularity=popularity or 0,
        album_mean=0, has_metadata=has_meta or isrc_single_confirmed,
        is_remastered_only=is_remastered,
        date_match=single_release_date_match,
        is_title_track=is_title,
        is_compilation=is_compilation,
        zscore_high=zscore_high,
        zscore_medium=zscore_medium,
        high_sources=high_sources,
        medium_sources=medium_sources,
        discogs_promo=discogs_promo,
        musicbrainz_promo=musicbrainz_promo,
        z_standout=z_standout,
    )

    # A sub-0.85 Discogs match (fuzzy / unverified) is MEDIUM, so it needs an
    # independent medium source or the popularity standout (``z_standout``)
    # to reach 'high'.  Without corroboration a lone Discogs match stays
    # 'medium' — that is the false-positive fix.
    if (
        discogs_confirmed
        and not _discogs_full_confidence
        and final in ("none", "medium")
        and (_corroborating_medium >= 1 or z_standout)
    ):
        final = "high"

    # Soft z-gate cap: low-scoring tracks need two+ high-confidence sources to
    # reach 'high'; a single high source with NO independent corroboration
    # lands at 'medium' instead.  Medium sources are real metadata
    # confirmations (MusicBrainz, Last.fm, ISRC, ...), so a high source backed
    # by any of them keeps the verdict 'high' regardless of z-score — a
    # confirmed single (e.g. Discogs + MusicBrainz + Last.fm) was demoted to
    # 'medium'/4★ purely because its score sat below the artist median.
    if z_low and final == "high" and high_sources < 2 and medium_sources == 0:
        final = "medium"

    # `confidence` is a STRING LABEL ('high'/'medium'/'low') — every consumer
    # (star-rating stage, templates, edit modal) compares against these labels.
    # `confidence_score` carries the numeric 0.0-1.0 equivalent for any numeric
    # consumers (e.g. the legacy single_confidence_score column).
    label_map = {"high": "high", "medium": "medium", "none": "low"}
    score_map = {"high": 1.0, "medium": 0.67, "none": 0.0}
    is_single = final in ("high", "medium")

    logger.debug(
        "[SINGLE_DETECTION] %s - %s: z=(album=%.2f, artist=%.2f) "
        "discogs=%s mb=%s lastfm=%s radio=%s isrc=%s lb_top10=%s z_standout=%s "
        "high_sources=%d medium_sources=%d → final=%s",
        artist, title, album_z, artist_z,
        discogs_confirmed, musicbrainz_confirmed, lastfm_confirmed,
        radio_edit_found, isrc_single_confirmed, lb_top10, z_standout,
        high_sources, medium_sources, final,
    )

    result = {
        "is_single": is_single,
        "confidence": label_map.get(final, "low"),
        "confidence_score": score_map.get(final, 0.0),
        "sources": sources,
        "reasons": reasons or ["no_source_match"],
        "single_status": final,
        # Diagnostic breadcrumb so the track-stage log line can show WHY a
        # verdict was reached (z-scores, source counts, title-track state).
        "decision": {
            "album_z": round(album_z, 2),
            "artist_z": round(artist_z, 2),
            "z_composite": round(z_composite, 2),
            "z_low": bool(z_low),
            "high_sources": high_sources,
            "medium_sources": medium_sources,
            "is_title_track": is_title,
            "z_standout": bool(z_standout),
            "source_levels": {
                k: _levels.get(k) for k in (
                    "discogs", "musicbrainz", "discogs_video", "lastfm"
                )
            },
        },
    }

    if persist_result and track_repo and track_id:
        try:
            track_repo.update_track_single_status(track_id, is_single, result["confidence"])
        except Exception as exc:
            logger.debug("Persistence failed for %s: %s", track_id, exc)

    return result