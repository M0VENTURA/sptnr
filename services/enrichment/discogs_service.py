"""Discogs enrichment service."""

from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher
from typing import Any, TypedDict, List, Dict, Optional

try:  # Optional C-speed fuzzy matching — see _discogs_title_similarity
    from rapidfuzz import fuzz as _rapidfuzz_fuzz
except ImportError:  # pragma: no cover — stdlib fallback keeps matching working
    _rapidfuzz_fuzz = None

from api_clients.discogs_http import DiscogsHttpClient
from helpers.normalization_service import (
    normalize_title_for_lookup,
    strip_parentheses,
    strip_featured_artist,
    clean_discogs_biography,
    edition_annotations_compatible,
)

logger = logging.getLogger(__name__)

# --- CONSTANTS ---
# Titles scoring below this normalized Similarity Ratio never match (loose
# 1-word collisions like "Halo" vs "Hallow" sit just above 0.8 raw, so the
# gate alone is not enough — see the short-title penalty below).
MIN_DISCOGS_SIMILARITY = 0.75
# Max confidence for a verified Discogs single/EP match (exact title).
DISCOGS_BASE_WEIGHT = 0.85
# A confidence this low is treated as "full" (near-exact verified match).
DISCOGS_FULL_CONFIDENCE = 0.85


def calculate_discogs_confidence(title: str, similarity_ratio: float,
                                 artist_verified: bool) -> dict[str, Any]:
    """Dynamic Discogs match confidence.

    Formula: ``base_weight(0.85) × title_similarity_ratio × penalties``.

    Penalties:
    - Artist-ID sanity check: an UNVERIFIED match (free-text DB search
      fallback, where the release may belong to a different artist) halves
      the score — verified only when the release came from the artist's own
      Discogs page.
    - Short/generic title penalty: one- or two-word titles ("Halo",
      "Tomorrow", "Nothing") routinely hit 0.80+ ratio against unrelated
      releases; they need near-exact precision (ratio >= 0.95), otherwise
      the score is cut to 60%.

    Returns ``{matched, confidence, metadata}`` — ``matched`` when the final
    confidence is >= 0.40, ``metadata.similarity_ratio`` for logs/UI.
    """
    sim = float(similarity_ratio or 0.0)
    if sim < MIN_DISCOGS_SIMILARITY:
        return {"matched": False, "confidence": 0.0,
                "metadata": {"similarity_ratio": round(sim, 2)}}

    confidence = DISCOGS_BASE_WEIGHT * sim

    if not artist_verified:
        confidence *= 0.50  # cannot confirm the release belongs to this artist

    if len(str(title or "").split()) <= 2 and sim < 0.95:
        confidence *= 0.60  # short/generic titles must be near-exact

    final = round(max(0.0, min(1.0, confidence)), 2)
    return {
        "matched": final >= 0.40,
        "confidence": final,
        "metadata": {"similarity_ratio": round(sim, 2)},
    }


# --- DISCOGS TITLE COMPARISON ---
# Discogs release/track titles routinely carry structural noise that is not
# part of the song title and dilutes raw SequenceMatcher ratios:
#   "They're All Around Us - Single"
#   "They're All Around Us (Single)"
#   "They're All Around Us [Explicit]"
#   "They're All Around Us / New Way Boy"   (split single B-side)
DISCOGS_NOISE_SUFFIX_RE = re.compile(
    r"\s*[-–—]\s*(?:the\s+)?"
    r"(?:single|ep|promo|radio\s+edit|edit|explicit|clean|remaster(?:ed)?|mono|stereo|album\s+version)"
    r"\s*$",
    re.IGNORECASE,
)


def _clean_title_for_comparison(title: str) -> str:
    """Normalized comparison key for a Discogs title.

    Canonical normalization (``normalize_title_for_lookup``) PLUS stripping
    of structural release noise so it cannot dilute similarity scoring:
    - bracketed sections: ``[Explicit]``, ``(Single)``, ``(Remastered)``
    - trailing dash suffixes: ``- Single``, ``- EP``, ``- Radio Edit``, ...

    Edition annotations ("(Epic Edition)") are guarded BEFORE scoring by
    ``edition_annotations_compatible`` (the same annotation is required on
    both sides), so stripping all brackets here is safe.
    """
    if not title:
        return ""
    value = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", title)
    value = DISCOGS_NOISE_SUFFIX_RE.sub("", value)
    return normalize_title_for_lookup(value)


INVERTED_RETRY_MIN_SIMILARITY = 0.50


def _discogs_title_similarity(local_title: str, candidate_title: str) -> float:
    """Token-aware similarity between a local track title and a Discogs candidate.

    Plain SequenceMatcher ratios penalize extra structural words (" - Single")
    and word order, so scoring escalates:
    1. exact normalized equality -> 1.0
    2. token containment with >= 70% coverage -> 0.95
    3. split-title ("A / B") primary-side containment -> 0.95
    4. otherwise the best of the plain and word-order-sorted ratios
    """
    local_key = _clean_title_for_comparison(local_title)
    candidate_key = _clean_title_for_comparison(candidate_title)
    if not local_key or not candidate_key:
        return 0.0
    if local_key == candidate_key:
        return 1.0

    shorter, longer = (
        (local_key, candidate_key)
        if len(local_key) <= len(candidate_key)
        else (candidate_key, local_key)
    )
    if shorter in longer and len(shorter) / len(longer) >= 0.70:
        return 0.95

    # Split singles: "They're All Around Us / New Way Boy" — the primary side
    # (before the first " / ") of either title may be the whole other title.
    for raw in (local_title, candidate_title):
        primary = re.split(r"\s*/\s*", raw.strip(), maxsplit=1)[0] if raw else ""
        primary_key = _clean_title_for_comparison(primary)
        if not primary_key:
            continue
        if primary_key == local_key or primary_key == candidate_key:
            return 0.95
        for other_key in (local_key, candidate_key):
            if primary_key in other_key and len(primary_key) / len(other_key) >= 0.70:
                return 0.95

    # RapidFuzz (C-speed) when installed: token_set_ratio handles subsets and
    # word order ("they re all around us" vs "they re all around us new way
    # boy"); partial_ratio handles suffix differences ("A" vs "A B").  The
    # stdlib fallback is the equivalent max of plain and word-order-sorted
    # SequenceMatcher ratios.
    if _rapidfuzz_fuzz is not None:
        return max(
            _rapidfuzz_fuzz.token_set_ratio(local_key, candidate_key),
            _rapidfuzz_fuzz.partial_ratio(local_key, candidate_key),
        ) / 100.0

    def _sorted(value: str) -> str:
        return " ".join(sorted(value.split()))

    return max(
        SequenceMatcher(None, local_key, candidate_key).ratio(),
        SequenceMatcher(None, _sorted(local_key), _sorted(candidate_key)).ratio(),
    )


# --- TYPES ---
class DiscogsTrack(TypedDict):
    number: str
    title: str
    artist: str
    duration: int | None
    isrc: str

class DiscogsArtistProfile(TypedDict):
    profile: str
    real_name: str | None
    urls: List[str]
    images: List[Dict[str, Any]]

# --- HELPERS ---
def _parse_discogs_duration(duration_str: str) -> int | None:
    if not duration_str: return None
    try:
        parts = duration_str.split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
    except Exception: pass
    return None

# --- SERVICE CLASS ---
class DiscogsService:
    def __init__(self, token: str, http_client: DiscogsHttpClient | None = None, enabled: bool = True):
        self.token = token or ""
        self.enabled = enabled
        self.http = http_client or DiscogsHttpClient(token=token)
        self._single_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._video_cache: dict[tuple[str, str], bool] = {}
        self._artist_releases_cache: dict[str, list[dict[str, Any]]] = {}

    def _normalize_title(self, title: str) -> str:
        base = strip_parentheses(title)
        return normalize_title_for_lookup(base or title)

    def _get_artist_releases(self, artist: str) -> list[dict[str, Any]]:
        """Fetch (and cache per artist) the artist's own Discogs releases.

        The artist-releases endpoint lists every release for the artist with
        its format and role, which is authoritative for single detection. The
        free-text database search ranks the full-length album editions above
        the 7"/promo single, so the single routinely misses a small top-N
        window even when it is genuinely on Discogs (e.g. "+44 - When Your
        Heart Stops Beating").
        """
        key = artist.lower()
        if key not in self._artist_releases_cache:
            releases: list[dict[str, Any]] = []
            artist_id = self.get_artist_id(artist)
            if artist_id:
                # Fetch ALL pages — a single page of 100 can miss older
                # singles of catalogue-heavy artists (Discogs caps pages at
                # 100 releases each).
                releases = self.http.get_artist_releases_all(artist_id, max_pages=10) or []
                # Discogs returns the artist's singles/EPs as MASTER entries,
                # which carry NO ``format`` field — the format check in
                # ``_scan_releases`` would skip every one of them (the
                # "When Your Heart Stops Beating" single missed for this
                # reason). Resolve each main release's format so singles are
                # detectable straight from the artist page, independent of
                # database-search ranking.
                for rel in releases:
                    if (
                        rel.get("type") == "master"
                        and not rel.get("format")
                        and str(rel.get("role") or "Main").lower() == "main"
                        and rel.get("main_release")
                    ):
                        try:
                            main = self.http.get_release(rel["main_release"], timeout=8.0)
                            rel["format"] = [
                                " ".join(
                                    part for part in (
                                        str(f.get("name") or ""),
                                        " ".join(str(d) for d in (f.get("descriptions") or [])),
                                    ) if part
                                )
                                for f in (main.get("formats") or [])
                            ]
                        except Exception as exc:
                            logger.debug(
                                "[DISCOGS] Master format lookup failed for %s: %s",
                                rel.get("title"), exc,
                            )
            self._artist_releases_cache[key] = releases
        return self._artist_releases_cache[key]

    @staticmethod
    def _release_is_promo(rel: dict[str, Any]) -> bool:
        """True when a Discogs release's format marks it as a promo."""
        formats = " ".join(str(f).lower() for f in (rel.get("format") or []) if f)
        return "promo" in formats

    def _scan_releases(self, title: str, title_key: str, releases: list[dict[str, Any]],
                       artist_verified: bool = True) -> dict[str, Any] | None:
        """Find the best single/EP match in *releases* for *title_key*.

        Candidates are scored with a continuous title-similarity ratio
        (SequenceMatcher over the normalized titles), gated at
        ``MIN_DISCOGS_SIMILARITY`` — the old binary equality/containment
        test reported every hit as full confidence, which let a short
        release title ("Halo", "Tomorrow") collide with loosely-related
        editions.  The highest-scoring commercial single/EP wins; a
        promo-only match is the fallback (promotional evidence is weaker).

        ``artist_verified`` records whether *releases* came from the
        artist's OWN Discogs page (True) or a free-text DB search (False)
        so the caller's confidence formula can apply the artist-ID penalty.

        Returns a status dict, or None when nothing matches.
        """
        best_commercial: dict[str, Any] | None = None
        best_promo: dict[str, Any] | None = None
        best_commercial_score = 0.0
        best_promo_score = 0.0

        def _status(rel: dict[str, Any], formats: str, is_promo: bool, sim: float) -> dict[str, Any]:
            return {
                "is_single": True,
                "is_promo": is_promo,
                "release_year": rel.get("year") if isinstance(rel.get("year"), int) else None,
                "release_id": str(rel.get("id") or "") or None,
                "format": formats,
                "similarity": round(sim, 2),
                "artist_verified": artist_verified,
            }

        for rel in releases:
            if str(rel.get("role") or "Main").strip().lower() != "main":
                continue
            formats = " ".join(str(f).lower() for f in (rel.get("format") or []) if f)
            if "single" not in formats and "ep" not in formats:
                continue
            # An edition-annotated track ("Valhalla (Epic Edition)") must only
            # match a single/EP release carrying the SAME edition annotation —
            # never the plain "Valhalla" single (title_key strips brackets).
            if not edition_annotations_compatible(title, str(rel.get("title") or "")):
                continue
            # Normalize the RELEASE title too — ``title_key`` is punctuation-
            # stripped ("what s the deal"), so matching it against the raw
            # lowercased title ("what's the deal?") fails on apostrophes.
            rel_title = self._normalize_title(str(rel.get("title") or ""))
            if not rel_title:
                continue
            # Token-aware rapidfuzz scoring: handles Discogs release-title
            # noise ("They're All Around Us (Single)", "- EP" suffixes) that
            # plain SequenceMatcher dilutes.  Subset/order-insensitive by
            # construction, so "A / B" split singles match their primary side.
            sim = _discogs_title_similarity(title, str(rel.get("title") or ""))
            if sim < MIN_DISCOGS_SIMILARITY:
                continue
            is_promo = "promo" in formats
            status = _status(rel, formats, is_promo, sim)
            if not is_promo:
                if sim > best_commercial_score:
                    best_commercial_score = sim
                    best_commercial = status
            elif sim > best_promo_score:
                best_promo_score = sim
                best_promo = status
        return best_commercial or best_promo

    def get_single_status(self, title: str, artist: str,
                          album_context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return the Discogs single verdict with promo/release detail.

        Returns ``{is_single, is_promo, release_year, release_id, format}``.
        A promo-only release confirms the track WAS issued as a single, but a
        promo is promotional evidence — weaker than a commercial single.
        """
        if not self.enabled or not self.token or not title or not artist:
            return {"is_single": False, "is_promo": False, "release_year": None, "release_id": None, "format": ""}
        if album_context and album_context.get("is_special_edition"):
            return {"is_single": False, "is_promo": False, "release_year": None, "release_id": None, "format": ""}

        title_key = self._normalize_title(title)
        cache_key = (artist.lower(), title_key)
        if cache_key in self._single_cache:
            return self._single_cache[cache_key]

        # Primary: match against the artist's OWN release list. A single/EP
        # release with a matching title on the artist's release list is
        # authoritative confirmation, independent of search-result ranking —
        # the artist ID was resolved, so matches here are artist-verified.
        artist_releases = self._get_artist_releases(artist) or []
        status = self._scan_releases(title, title_key, artist_releases, artist_verified=True)

        if status is None:
            # Self-diagnosing miss: how many releases were scanned and what
            # formats they carried helps distinguish "not on Discogs" from
            # "window too small / artist page incomplete".
            logger.debug(
                "[DISCOGS] No single/EP match for '%s' by '%s' across %d artist release(s)",
                title, artist, len(artist_releases),
            )

        # Fallback: use search_database with specific params. A wider window
        # (25 vs 5) because the album edition outranks the single and the
        # single can sit outside a tiny top-N (e.g. "+44 - When Your Heart
        # Stops Beating").  Matches here are NOT artist-verified — the search
        # can surface another artist's release — so the caller halves their
        # confidence via the artist-ID sanity penalty.
        if status is None:
            results = self.http.search_database({"q": f"{strip_featured_artist(artist)} {title_key}", "type": "release", "per_page": 25})
            status = self._scan_releases(title, title_key, results or [], artist_verified=False)

        # ── Inverted-artist retry (feat. splits) ───────────────────────────
        # "Lord of the Lost feat. Feuerschwanz" has no Discogs artist page —
        # Discogs credits the single to "Feuerschwanz feat. Lord of the Lost".
        # When the standard match fails or is sub-threshold, retry under the
        # inverted credit before declaring "not a single".
        _inv_used = False
        if (status is None or float(status.get("similarity") or 0.0) < INVERTED_RETRY_MIN_SIMILARITY):
            from services.popularity.popularity_sources import invert_featured_artist
            inverted = invert_featured_artist(artist)
            if inverted != artist:
                _std_sim = float(status.get("similarity") or 0.0) if status else 0.0
                inv_status = self._scan_releases(
                    title, title_key, self._get_artist_releases(inverted) or [],
                    artist_verified=True,
                )
                if inv_status is None:
                    inv_results = self.http.search_database(
                        {"q": f"{inverted} {title_key}", "type": "release", "per_page": 25}
                    )
                    inv_status = self._scan_releases(title, title_key, inv_results or [], artist_verified=False)
                if inv_status and float(inv_status.get("similarity") or 0.0) > _std_sim:
                    inv_status["inverted_match_used"] = True
                    status = inv_status
                    _inv_used = True
                    _sim = inv_status.get("similarity", 0.0)
                    logger.info(
                        "[DISCOGS_MATCH] Standard match %.2f -> Inverted retry: %s -> %s (%.2f)",
                        _std_sim, inverted,
                        "MATCHED" if inv_status.get("is_single") else "partial",
                        _sim,
                    )

        if status is None:
            status = {"is_single": False, "is_promo": False, "release_year": None,
                      "release_id": None, "format": "", "similarity": 0.0,
                      "artist_verified": False}
        if _inv_used:
            status["inverted_match_used"] = True

        self._single_cache[cache_key] = status
        return status

    @staticmethod
    def _is_official_video_for_track(video: dict, track_title_lower: str) -> bool:
        """True when a Discogs video is the official/promo clip for the track.

        Ported from the legacy scanner: the video title (or description) must
        contain the word ``official`` or ``promo`` (whole word, so
        "unofficial" never counts) AND match the track title exactly after
        stripping video-suffix noise ("official video", "music video", "hd",
        "4k", ...) and any "Artist - " prefix.
        """
        video_title = (video.get("title") or "").lower()
        video_desc = (video.get("description") or "").lower()

        official_pattern = re.compile(r"\b(official|promo)\b")
        is_official_or_promo = bool(
            official_pattern.search(video_title) or official_pattern.search(video_desc)
        )

        # Canonical comparison key — punctuation/apostrophe-insensitive, so
        # the Discogs title "No, It Isnt" confirms "No, It Isn't".
        # (normalize_title_for_lookup turns the apostrophe into a space, so
        # it must be dropped first.)
        def _canonical(value: str) -> str:
            return normalize_title_for_lookup(
                value.replace("'", "").replace("’", "")
            )

        video_title_cleaned = re.sub(
            r"\s*[\(\[]?(official|music|promo)?\s*(video|music video|mv|hd|4k|lyric video)[\)\]]?\s*$",
            "", video_title, flags=re.IGNORECASE,
        ).strip()
        if " - " in video_title_cleaned:
            parts = video_title_cleaned.split(" - ", 1)
            if len(parts) == 2:
                video_title_cleaned = parts[1].strip()

        matches_title = _canonical(track_title_lower) == _canonical(video_title_cleaned)

        if not matches_title and video_desc:
            desc_cleaned = re.sub(
                r"\s*[\(\[]?(official|music|promo)?\s*(video|music video|mv|hd|4k|lyric video)[\)\]]?\s*",
                "", video_desc, flags=re.IGNORECASE,
            ).strip()
            if " - " in desc_cleaned:
                parts = desc_cleaned.split(" - ", 1)
                if len(parts) == 2:
                    desc_cleaned = parts[1].strip()
            matches_title = _canonical(track_title_lower) == _canonical(desc_cleaned)

        return is_official_or_promo and matches_title

    def has_official_video(self, title: str, artist: str) -> bool:
        """Return True when Discogs lists an official/promo video for the track.

        Legacy parity (``has_official_video``): search master releases for
        ``<artist> <title>``, inspect the first five masters' ``videos`` lists
        and require an official/promo video whose cleaned title matches the
        track. Results are cached per (artist, title) — a track appears once
        per album scan, and repeated scans reuse the verdict.
        """
        if not self.enabled or not self.token or not title or not artist:
            return False
        cache_key = (artist.lower(), self._normalize_title(title))
        if cache_key in self._video_cache:
            return self._video_cache[cache_key]
        matched = False
        try:
            results = self.http.search_database(
                {"q": f"{artist} {title}", "type": "master", "per_page": 10}
            ) or []
            for rel in results[:5]:
                master_id = rel.get("id")
                if not master_id:
                    continue
                master = self.http.get_master(master_id, timeout=8.0)
                if not master:
                    continue
                for video in (master.get("videos") or []):
                    if self._is_official_video_for_track(video, title.lower()):
                        matched = True
                        break
                if matched:
                    break
        except Exception as exc:
            logger.debug("[DISCOGS_VIDEO] Check failed for %s / %s: %s", artist, title, exc)
        self._video_cache[cache_key] = matched
        return matched

    def is_single(self, title: str, artist: str, album_context: dict[str, Any] | None = None) -> bool:
        return bool(self.get_single_status(title, artist, album_context=album_context).get("is_single"))

    def get_artist_id(self, artist: str, timeout: float = 10.0) -> str | None:
        """Resolve a Discogs artist ID via database search (type=artist).

        Returns the first result's numeric ID as a string, or ``None`` when
        the artist cannot be found. Mirrors the legacy
        ``MusicBrainzClient``/``DiscogsClient.get_artist_id`` behaviour.
        """
        if not self.enabled or not self.token or not artist:
            return None
        try:
            results = self.http.search_database(
                {"q": artist, "type": "artist", "per_page": 5},
                timeout=timeout,
            )
            if results and isinstance(results, list):
                first = results[0]
                if isinstance(first, dict) and first.get("id"):
                    return str(first["id"])
        except Exception as exc:
            logger.debug("Discogs artist ID lookup failed for '%s': %s", artist, exc)
        return None

    def get_genres(self, title: str, artist: str) -> list[str]:
        if not self.enabled or not self.token: return []
        
        # FIXED: Use search_database
        results = self.http.search_database({"q": f"{artist} {title}", "type": "release", "per_page": 5})
        
        genres = []
        for r in results:
            genres.extend(r.get("genre", []))
            genres.extend(r.get("style", []))
        return genres

    def get_artist_biography(self, artist: str) -> DiscogsArtistProfile:
        # FIXED: Use search_database
        results = self.http.search_database({"q": artist, "type": "artist", "per_page": 1})
        if not results:
            return {"profile": "", "real_name": None, "urls": [], "images": []}
        
        artist_id = results[0].get("id")
        data = self.http.get_artist(artist_id) if artist_id else {}
        return {
            "profile": clean_discogs_biography(data.get("profile", "")),
            "real_name": data.get("realname"),
            "urls": data.get("urls", []),
            "images": data.get("images", []),
        }

    def get_release_tracks(self, release_id: str) -> List[DiscogsTrack]:
        if not self.enabled or not self.token or not release_id: return []
        release = self.http.get_release(release_id)
        if not isinstance(release, dict): return []
        
        tracks = []
        for track in release.get("tracklist", []):
            tracks.append({
                "number": track.get("position", ""),
                "title": track.get("title", ""),
                "artist": track.get("artist", track.get("artists", [{}])[0].get("name", "")),
                "duration": _parse_discogs_duration(track.get("duration", "")),
                "isrc": ""
            })
        return tracks

# --- BRIDGE FUNCTIONS ---
_DEFAULT_SERVICE: DiscogsService | None = None

def _get_service(token: str) -> DiscogsService:
    global _DEFAULT_SERVICE
    if _DEFAULT_SERVICE is None or _DEFAULT_SERVICE.token != token:
        _DEFAULT_SERVICE = DiscogsService(token=token)
    return _DEFAULT_SERVICE

def is_discogs_single(title: str, artist: str, token: str = "", album_context: dict | None = None) -> bool:
    return _get_service(token).is_single(title, artist, album_context=album_context)

def get_discogs_genres(title: str, artist: str, token: str = "") -> list[str]:
    return _get_service(token).get_genres(title, artist)

def get_discogs_artist_biography(artist: str, token: str = "") -> DiscogsArtistProfile:
    return _get_service(token).get_artist_biography(artist)

def has_discogs_video(title: str, artist: str, token: str = "") -> bool:
    return _get_service(token).has_official_video(title, artist)


def lookup_discogs_album(artist: str, album: str) -> dict:
    """Search Discogs for an album and return release candidates."""
    from api_clients.discogs_http import DiscogsHttpClient
    from helpers.config_helpers import get_config
    cfg = get_config() or {}
    token = cfg.get("api_integrations", {}).get("discogs", {}).get("token", "") or ""
    if not token:
        return {"success": False, "error": "Discogs token not configured"}
    try:
        http = DiscogsHttpClient(token=token)
        results = http.search_database({"q": f"{artist} {album}", "type": "release", "per_page": 5})
        return {"success": True, "results": results}
    except Exception as exc:
        return {"success": False, "error": str(exc)}