"""Artist/title matching helpers for popularity provider lookups."""

from __future__ import annotations

import re
import unicodedata

from helpers.normalization_service import FEAT_SUFFIX_RE, strip_cover_attribution

ARTIST_JOIN_RE = re.compile(
    r"""
    \s+
    (?:&|and|x|×|\+)
    \s+
    """,
    re.IGNORECASE | re.VERBOSE,
)


def clean_artist_spacing(value: str) -> str:
    """Clean whitespace while preserving casing for provider lookups."""
    return re.sub(r"\s+", " ", (value or "").strip())


def build_artist_variants(artist: str) -> list[str]:
    """Generate artist name variants (main artist, featured artists, combinations)."""
    variants: set[str] = set()
    value = clean_artist_spacing(artist)
    if not value:
        return []
    variants.add(value)

    for pattern in [r"\s+feat\.\s+", r"\s+ft\.\s+", r"\s+featuring\s+"]:
        if re.search(pattern, value, flags=re.I):
            parts = re.split(pattern, value, maxsplit=1, flags=re.I)
            main, featured = parts[0].strip(), parts[1].strip() if len(parts) > 1 else ""
            if main:
                variants.add(main)
            if featured:
                variants.add(featured)
            if main and featured:
                variants.add(f"{main} & {featured}")

    return sorted(v for v in variants if v)


def get_primary_artist_preserve_case(artist: str) -> str:
    """Return likely primary artist while preserving original casing."""
    if not artist:
        return ""
    return clean_artist_spacing(FEAT_SUFFIX_RE.split(artist, maxsplit=1)[0])


def get_artist_lookup_candidates(artist: str, album_artist: str | None = None) -> list[str]:
    """Build provider lookup candidates in preferred order."""
    candidates: list[str] = []
    seen: set[str] = set()

    def add(value: str | None):
        value = clean_artist_spacing(value or "")
        key = value.casefold()
        if value and key not in seen:
            candidates.append(value)
            seen.add(key)

    add(artist)
    add(get_primary_artist_preserve_case(artist))
    add(album_artist)
    add(get_primary_artist_preserve_case(album_artist or ""))
    # Collab / multi-artist credits ("BABYMETAL & Electric Callboy",
    # "A x B", "A and B"): each sub-artist may index the same track under
    # its own catalogue, so query each part individually too.
    if ARTIST_JOIN_RE.search(artist or ""):
        for part in ARTIST_JOIN_RE.split(artist or ""):
            add(part.strip())
    if album_artist and ARTIST_JOIN_RE.search(album_artist or ""):
        for part in ARTIST_JOIN_RE.split(album_artist or ""):
            add(part.strip())
    return candidates


def make_artist_match_key(artist: str) -> str:
    """Internal-only canonical artist key for matching/cache grouping."""
    artist = get_primary_artist_preserve_case(artist)
    artist = unicodedata.normalize("NFKC", artist)
    artist = artist.casefold()
    return re.sub(r"\s+", " ", artist).strip()


def make_track_match_key(artist: str, title: str) -> str:
    """Canonical key for combining variants of the same song."""
    artist_key = make_artist_match_key(artist)
    title_key = unicodedata.normalize("NFKC", title or "").casefold()
    title_key = re.sub(r"\s+", " ", title_key).strip()
    return f"{artist_key}::{title_key}"


def normalize_for_aggregation(title: str) -> str:
    """Aggressively normalise title for local provider-count aggregation."""
    value = str(title or "").lower()
    # Strip trailing cover attributions ("(PSY Cover)", "[Foo Cover]") so a
    # cover track correlates with its canonical Last.fm row — the popular
    # "Gangnam Style" single vs the low-listen "Gangnam Style (PSY Cover)"
    # album row are the same song and must collapse to one key.
    value = strip_cover_attribution(value)
    value = re.sub(r"\s*[\(\[].*?(feat\.|featuring|ft\.|remaster|remastered|radio edit|single version|album version).*?[\)\]]", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*-\s*(remaster(?:ed)?|radio edit|single version|album version).*$", "", value, flags=re.IGNORECASE)
    # Unparenthesised "feat. Guest" / "featuring Guest" suffixes — the album
    # version of a song is frequently stored without brackets, and correlating
    # it with the bracket-carrying single requires both forms to collapse to
    # the same key (legacy parity with old_system/popularity_helpers.py).
    value = re.sub(r"\s+(?:feat\.?|ft\.?|featuring)\s+.*$", "", value, flags=re.IGNORECASE)
    # Drop dashes that are NOT space-delimited ("Ph4/NT0‐mA" → "Ph4/NT0mA",
    # unicode/ASCII variants) — some sources omit the separator entirely, so
    # both variants must collapse to the same key.  Space-delimited dashes
    # ("Foo - Bar") stay separators.
    value = re.sub(r"(?<=\S)[\u2010\u2011\u2012\u2013\u2014\u2015\u2212-](?=\S)", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def choose_best_provider_counts(results: list[dict]) -> dict:
    """Pick strongest provider result across artist/title variants."""
    if not results:
        return {}
    def score(item: dict):
        return (
            int(item.get("listeners", 0) or 0),
            int(item.get("playcount", item.get("track_play", 0)) or 0),
            int(item.get("listen_count", item.get("total_listen_count", 0)) or 0),
        )
    return sorted(results, key=score, reverse=True)[0]
