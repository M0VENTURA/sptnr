"""Multi-stage Wikipedia → MusicBrainz matching pipeline for upcoming releases.

1. ``sanitize_wiki_entry()`` — fixes Wikipedia scraping artifacts before any
   lookup: concatenated collaborations (``AkinmusireandMary`` →
   ``Akinmusire and Mary``), parenthetical notes (``(deluxe edition)``,
   ``(EP)``, ``(album)``), citation markers (``[12]``) and smart quotes.

2. Two-pass search — Pass 1 uses a local artist MBID (``arid:`` query) when
   the artist is in the library, Pass 2 falls back to a global Lucene query
   with an OR'd ``releasegroup``/``release`` term.

3. Confidence scoring (rapidfuzz): artist ``token_sort_ratio`` (45%) + album
   ``token_set_ratio`` (45%) + release-date proximity (10%, same year or
   within ±30 days).  Thresholds: ≥0.85 auto-match, 0.65–0.85 candidate
   (flag for one-click manual confirmation), <0.65 unmatched.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from api_clients.musicbrainz_http import (
    MusicBrainzHttpClient,
    escape_lucene_special_chars,
)

logger = logging.getLogger(__name__)

try:
    from rapidfuzz import fuzz as _fuzz  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    _fuzz = None

# ---------------------------------------------------------------------------
# Thresholds / weights
# ---------------------------------------------------------------------------

AUTO_MATCH_THRESHOLD = 0.85
CANDIDATE_THRESHOLD = 0.65

_ARTIST_WEIGHT = 0.45
_ALBUM_WEIGHT = 0.45
_DATE_BONUS = 0.10

# ---------------------------------------------------------------------------
# Text sanitization
# ---------------------------------------------------------------------------

# Glued conjunction: lowercase letter + conjunction + uppercase letter
# (e.g. "AkinmusireandMary" → "Akinmusire and Mary").  Requires a lowercase
# char before so proper names containing "and" ("Brandy", "Understand") and
# genres like "R&B" are never split.
_CONCAT_RE = re.compile(r"([a-z])([Aa]nd|[Ff]eat(?:uring)?|[Ww]ith|&)([A-Z])")

# Editorial notes appended to album titles that carry no matching value.
_PAREN_NOTES_RE = re.compile(
    r"\s*\((?:album|ep|deluxe(?:\s+edition)?|self-titled|"
    r"remaster(?:ed)?|expanded(?:\s+edition)?)\)",
    re.IGNORECASE,
)

_CITATION_RE = re.compile(r"\s*\[\d+\]\s*")

_QUOTE_MAP = str.maketrans(
    {
        "\u2019": "'",  # ’ right single quote
        "\u2018": "'",  # ‘ left single quote
        "\u201d": '"',  # ” right double quote
        "\u201c": '"',  # “ left double quote
        "\u2013": "-",  # – en dash
        "\u2014": "-",  # — em dash
        "\u00a0": " ",  # non-breaking space
    }
)


def _sanitize_text(text: str, fix_concat: bool) -> str:
    if not text:
        return ""
    text = text.translate(_QUOTE_MAP)
    text = _CITATION_RE.sub(" ", text)
    text = _PAREN_NOTES_RE.sub("", text)
    if fix_concat:
        text = _CONCAT_RE.sub(r"\1 \2 \3", text)
    return re.sub(r"\s+", " ", text).strip()


def sanitize_wiki_entry(artist: str, album: str) -> tuple[str, str]:
    """Clean Wikipedia scraping artifacts from an artist/album pair."""
    return _sanitize_text(artist, fix_concat=True), _sanitize_text(album, fix_concat=True)


# ---------------------------------------------------------------------------
# Similarity helpers
# ---------------------------------------------------------------------------

def _difflib_ratio(a: str, b: str) -> float:
    from difflib import SequenceMatcher
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _similarity_artist(a: str, b: str) -> float:
    """token_sort_ratio — tolerates word-order swaps (feat. ordering)."""
    if _fuzz is None:
        return _difflib_ratio(a, b)
    return _fuzz.token_sort_ratio(a, b) / 100.0


def _similarity_album(a: str, b: str) -> float:
    """token_set_ratio — tolerates extra tokens (edition words, articles)."""
    if _fuzz is None:
        return _difflib_ratio(a, b)
    return _fuzz.token_set_ratio(a, b) / 100.0


def _date_bonus(wiki_date: str | None, mb_date: str | None) -> float:
    """Full bonus when the dates agree by year or are within ±30 days."""
    if not wiki_date or not mb_date:
        return 0.0
    wiki = str(wiki_date)[:10].strip()
    mb = str(mb_date)[:10].strip()

    # Year-only dates ("2026") — compare the leading year.
    if wiki[:4].isdigit() and mb[:4].isdigit() and wiki[:4] == mb[:4]:
        return _DATE_BONUS

    try:
        wd = datetime.strptime(wiki, "%Y-%m-%d")
        md = datetime.strptime(mb, "%Y-%m-%d")
        if abs((md - wd).days) <= 30:
            return _DATE_BONUS
    except ValueError:
        pass
    return 0.0


def _candidate_artist_name(cand: dict[str, Any]) -> str:
    credit = cand.get("artist-credit") or []
    if credit and isinstance(credit, list):
        parts: list[str] = []
        for ac in credit:
            if isinstance(ac, dict):
                name = ac.get("name") or (ac.get("artist") or {}).get("name") or ""
                join_phrase = ac.get("joinphrase", "")
                if name:
                    parts.append(name)
                if join_phrase:
                    parts.append(join_phrase)
            elif isinstance(ac, str):
                parts.append(ac)
        if parts:
            return "".join(parts)
    return str(cand.get("artist") or "Unknown Artist")


def score_candidate(
    clean_artist: str,
    clean_album: str,
    wiki_date: str | None,
    cand: dict[str, Any],
) -> dict[str, Any]:
    """Score one MusicBrainz release-group candidate (0.0 – 1.0)."""
    mb_artist = _candidate_artist_name(cand)
    mb_album = str(cand.get("title") or "")

    artist_sim = _similarity_artist(clean_artist.lower(), mb_artist.lower())
    album_sim = _similarity_album(clean_album.lower(), mb_album.lower())
    total = artist_sim * _ARTIST_WEIGHT + album_sim * _ALBUM_WEIGHT
    total += _date_bonus(wiki_date, str(cand.get("first-release-date") or ""))

    return {
        "mbid": str(cand.get("id") or ""),
        "title": mb_album,
        "artist": mb_artist,
        "score": round(total, 3),
        "first_release_date": str(cand.get("first-release-date") or "")[:10],
        "primary_type": str(cand.get("primary-type") or cand.get("category") or ""),
    }


# ---------------------------------------------------------------------------
# Two-pass search
# ---------------------------------------------------------------------------

_mb_client: MusicBrainzHttpClient | None = None


def _get_mb_client() -> MusicBrainzHttpClient:
    global _mb_client
    if _mb_client is None:
        _mb_client = MusicBrainzHttpClient(enabled=True)
    return _mb_client


def _artist_mbid_from_library(artist: str) -> str | None:
    """Best-effort local artist MBID (tracks + artists tables)."""
    try:
        from sqlalchemy import text
        from db.engine import db_session

        with db_session() as session:
            row = session.execute(
                text("""
                    SELECT MAX(COALESCE(NULLIF(musicbrainz_artist_id, ''),
                                        NULLIF(musicbrainz_artistid, ''))) AS mbid
                    FROM tracks
                    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
                """),
                {"artist": artist},
            ).fetchone()
            mbid = str(row[0]) if row and row[0] else ""
            if mbid.strip():
                return mbid.strip()
            row2 = session.execute(
                text("SELECT id FROM artists WHERE LOWER(name) = LOWER(:artist)"),
                {"artist": artist},
            ).fetchone()
            mbid2 = str(row2[0]) if row2 and row2[0] else ""
            if re.fullmatch(r"[0-9a-f-]{36}", mbid2):
                return mbid2
    except Exception as exc:
        logger.debug("[MATCH] Local artist MBID lookup failed: %s", exc)
    return None


def _search_release_groups(query: str, limit: int) -> list[dict[str, Any]]:
    try:
        return _get_mb_client().search_release_groups(query, limit=limit)
    except Exception as exc:
        logger.warning("[MATCH] MusicBrainz search failed (%s): %s", query[:120], exc)
        return []


def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the highest-scoring entry per MBID (passes can overlap)."""
    best: dict[str, dict[str, Any]] = {}
    for cand in candidates:
        key = cand.get("mbid") or cand.get("title") or ""
        if not key:
            continue
        if key not in best or cand["score"] > best[key]["score"]:
            best[key] = cand
    return sorted(best.values(), key=lambda c: c["score"], reverse=True)


def match_to_musicbrainz(
    artist: str,
    album: str,
    release_date: str | None = None,
) -> dict[str, Any]:
    """Two-pass Wikipedia → MusicBrainz matching with confidence scoring.

    Returns:
        {
            "status": "matched" | "candidate" | "unmatched",
            "score": float,
            "mbid": str | None,
            "title": str | None,
            "artist": str | None,
            "query": str,             # last Lucene query used
            "candidates": [scored],   # ranked, best first (capped)
        }
    """
    clean_artist, clean_album = sanitize_wiki_entry(artist, album)
    candidates: list[dict[str, Any]] = []

    # ---- Pass 1: local artist MBID → arid: query (high precision) ----
    local_mbid = _artist_mbid_from_library(clean_artist)
    if local_mbid:
        q1 = (
            f'arid:{local_mbid} AND '
            f'releasegroup:"{escape_lucene_special_chars(clean_album)}"'
        )
        for cand in _search_release_groups(q1, limit=10):
            scored = score_candidate(clean_artist, clean_album, release_date, cand)
            if scored["mbid"]:
                candidates.append(scored)
        if candidates:
            candidates = _dedupe_candidates(candidates)
            if candidates[0]["score"] >= AUTO_MATCH_THRESHOLD:
                best = candidates[0]
                return {
                    "status": "matched",
                    "score": best["score"],
                    "mbid": best["mbid"],
                    "title": best["title"],
                    "artist": best["artist"],
                    "query": q1,
                    "candidates": candidates[:5],
                }

    # ---- Pass 2: global Lucene fallback ----
    q2 = (
        f'artist:"{escape_lucene_special_chars(clean_artist)}" AND '
        f'(releasegroup:"{escape_lucene_special_chars(clean_album)}" OR '
        f'release:"{escape_lucene_special_chars(clean_album)}")'
    )
    for cand in _search_release_groups(q2, limit=20):
        scored = score_candidate(clean_artist, clean_album, release_date, cand)
        if scored["mbid"]:
            candidates.append(scored)

    candidates = _dedupe_candidates(candidates)
    if not candidates:
        return {
            "status": "unmatched",
            "score": 0.0,
            "mbid": None,
            "title": None,
            "artist": None,
            "query": q2,
            "candidates": [],
        }

    best = candidates[0]
    if best["score"] >= AUTO_MATCH_THRESHOLD:
        status = "matched"
    elif best["score"] >= CANDIDATE_THRESHOLD:
        status = "candidate"
    else:
        status = "unmatched"

    return {
        "status": status,
        "score": best["score"],
        "mbid": best["mbid"],
        "title": best["title"],
        "artist": best["artist"],
        "query": q2,
        "candidates": candidates[:5],
    }
