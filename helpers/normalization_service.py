"""
Canonical normalization for titles, artists, albums, and filenames.

✅ SINGLE SOURCE OF TRUTH
✅ No matching or business logic
✅ Used by queue, matching, enrichment, metadata
"""

from __future__ import annotations

import re
import unicodedata

import os
from typing import Tuple, Optional


from helpers.config_helpers import (
    get_queue_matching_config_v2,
)


# =============================================================================
# ✅ CORE NORMALIZATION
# =============================================================================

def clean_title(
    value: str,
    *,
    remove_brackets: bool = True,
    remove_single_release: bool = True,
    remove_remaster: bool = True,
) -> str:
    """
    Shared title cleanup pipeline.

    Used by title normalization functions to avoid
    duplicated cleanup logic.
    """

    if not value:
        return ""

    if remove_brackets:
        value = strip_parentheses(
            value,
            full=True,
        )

    if remove_single_release:
        value = strip_single_release_suffix(
            value,
        )

    if remove_remaster:
        value = strip_remaster_suffix(
            value,
        )

    return value.strip()


def normalize_string(value: str) -> str:
    """
    Canonical normalization:
    - lowercase
    - remove accents
    - remove punctuation
    - collapse whitespace
    """
    if not value:
        return ""

    value = value.lower().strip()

    value = unicodedata.normalize("NFD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))

    value = re.sub(r"[^\w\s]", " ", value)

    return re.sub(r"\s+", " ", value).strip()


def normalize_filename(value: str) -> str:
    """
    Normalize filenames for matching:
    - remove extension
    - normalize text
    """
    if not value:
        return ""

    value = re.sub(r"\.[a-z0-9]{2,5}$", "", value, flags=re.IGNORECASE)
    return normalize_string(value)


# =============================================================================
# ✅ GENERIC CLEANING HELPERS (CONSOLIDATED)
# =============================================================================

def strip_parentheses(value: str, full: bool = False) -> str:
    """
    Remove parentheses.

    full=True  → remove all bracket sections
    full=False → remove trailing only
    """
    if not value:
        return ""

    if full:
        return re.sub(r"\s*\([^)]*\)", "", value).strip()

    return re.sub(r"\s*\([^)]*\)\s*$", "", value).strip()


def strip_brackets(value: str) -> str:
    """
    Remove both () and [] — useful for filenames.
    """
    return re.sub(r"(\(.*?\)|\[.*?\])", "", value or "").strip()


# Canonical feat/ft/featuring suffix pattern — single source of truth used
# by all modules that need to strip featured-artist suffixes from artist names.
# Handles both plain "feat. Guest" and bracket notation "[feat. Guest]".
FEAT_SUFFIX_RE = re.compile(
    r"""
    \s+
    (?:\[|\()?\s*
    (?:feat\.?|ft\.?|featuring|with|w/|&|and)
    \s+
    [^\]\)\[]*
    (?:\]|\)|$)
    """,
    re.IGNORECASE | re.VERBOSE,
)

def strip_featured_artist(
    value: str,
) -> str:
    if not value:
        return ""

    return FEAT_SUFFIX_RE.sub(
        "",
        value,
    ).strip()


# =============================================================================
# ✅ SUFFIX STRIPPING
# =============================================================================

SINGLE_RELEASE_SUFFIX_RE = re.compile(
    r"\s*\(\s*(?:radio\s+(?:edit|mix|version)|single\s+(?:version|edit|mix)|album\s+version)\s*\)\s*$",
    re.IGNORECASE,
)

REMASTER_SUFFIX_RE = re.compile(
    r"\s*(?:-|\(|\[)?\s*(?:\d{4}\s*)?remaster(?:ed)?(?:\s*\d{4})?\s*(?:\)|\])?\s*$",
    re.IGNORECASE,
)


def strip_single_release_suffix(value: str) -> str:
    return SINGLE_RELEASE_SUFFIX_RE.sub("", value or "").strip()


def strip_remaster_suffix(value: str) -> str:
    return REMASTER_SUFFIX_RE.sub("", value or "").strip()


# =============================================================================
# ✅ VERSION / VARIANT EXTRACTION
# =============================================================================


def get_version_keywords() -> set[str]:
    """
    Variant tokens used during title parsing.
    """

    return {
        str(token).lower()
        for token in get_queue_matching_config_v2()[
            "title_variant_tokens"
        ]
    } | {"unplugged"}


ROMAN_NUMERAL_PATTERN = r'\s+(I{1,3}|IV|V|VI{0,3}|IX|X{1,3})\s*$'
PUNCTUATION_SUFFIX_PATTERN = re.compile(r'([!+?]+)\s*$')


def extract_version_info(
    title: str,
) -> tuple[str, set[str]]:
    """
    Extract base title + version tags.

    Does NOT normalize.
    """

    if not title:
        return "", set()

    title_lower = title.lower()

    found_versions = {
        keyword
        for keyword in get_version_keywords()
        if re.search(
            rf"\b{re.escape(keyword)}\b",
            title_lower,
        )
    }

    suffix_match = PUNCTUATION_SUFFIX_PATTERN.search(title)
    preserved_suffix = (
        suffix_match.group(1)
        if suffix_match
        else ""
    )

    base_title = clean_title(
        title,
        remove_brackets=True,
        remove_single_release=False,
        remove_remaster=False,
    )

    base_title = re.sub(
        r"\s*-\s*(?:edit|mix|version|live|remix).*",
        "",
        base_title,
        flags=re.IGNORECASE,
    ).strip()

    roman_match = re.search(
        ROMAN_NUMERAL_PATTERN,
        base_title,
        re.IGNORECASE,
    )

    if roman_match:
        base_title = (
            base_title[:roman_match.start()]
            .strip()
        )

        base_title += (
            f" {roman_match.group(1).lower()}"
        )

    base_title += preserved_suffix

    return (
        base_title.strip(),
        found_versions,
    )


# Edition / version annotations that mark a track as a DIFFERENT EDITION of
# the song ("(Epic Edition)", "(Deluxe Edition)", ...).  A track carrying one
# must only be treated as the same release as a title carrying the SAME
# annotation — it must never be matched against the plain (un-annotated)
# title.  Cover annotations ("(PSY Cover)", "(Nirvana Cover)") and live/remix
# markers are NOT editions and never gate matching: MusicBrainz omits cover
# annotations from release-group titles, so a cover track matches the cover's
# own plain single, and remastered variants are intentionally treated as the
# same song as the original.
_EDITION_ANNOTATION_KEYWORDS = frozenset({
    "anniversary", "collector", "deluxe", "edition", "epic", "expanded",
    "extended", "limited", "reissue", "special", "tour", "ultimate",
})

_EDITION_ANNOTATION_RE = re.compile(r"[\(\[]([^\)\]]+)[\)\]]\s*$", re.IGNORECASE)


def extract_edition_annotation(title: str) -> str | None:
    """Return the normalized trailing edition annotation, or None.

    Only a bracketed suffix whose content contains an edition marker
    ("Epic Edition", "Deluxe Edition", ...) counts.  Cover annotations
    ("(PSY Cover)") and live/remix markers are not editions and return None.
    """
    if not title:
        return None
    m = _EDITION_ANNOTATION_RE.search(title)
    if not m:
        return None
    inner = m.group(1).strip().lower()
    if not any(kw in inner for kw in _EDITION_ANNOTATION_KEYWORDS):
        return None
    return re.sub(r"[^a-z0-9]+", " ", inner).strip()


def edition_annotations_compatible(title_a: str, title_b: str) -> bool:
    """True when the edition annotations on two titles are compatible.

    An edition-annotated track (e.g. "Valhalla (Epic Edition)") is only the
    same release as a title carrying the SAME edition annotation — it must
    never be matched against the plain "Valhalla".  When neither title carries
    an edition annotation the titles are compatible; when exactly one carries
    an annotation they are not.
    """
    ann_a = extract_edition_annotation(title_a)
    ann_b = extract_edition_annotation(title_b)
    if ann_a is None and ann_b is None:
        return True
    if ann_a is None or ann_b is None:
        return False
    return ann_a == ann_b

def is_compilation_artist(
    artist: str | None,
) -> bool:
    """
    Determine whether an artist string represents
    a compilation/various-artists release.
    """

    if not artist:
        return False

    cfg = get_queue_matching_config_v2()

    if not cfg["detect_compilations"]:
        return False

    compilation_artists = {
        normalize_string(a)
        for a in cfg["compilation_artists"]
    }

    return (
        normalize_string(artist)
        in compilation_artists
    )

# =============================================================================
# ✅ PRIMARY NORMALIZATION PIPELINES
# =============================================================================

def normalize_title_for_lookup(
    title: str,
) -> str:
    """
    Canonical matching normalization.
    """

    return normalize_string(
        clean_title(
            title,
            remove_brackets=True,
            remove_single_release=True,
            remove_remaster=True,
        )
    )


def normalize_title_for_lucene_query(
    title: str,
) -> str:
    """
    Punctuation-free title for MusicBrainz Lucene phrase queries.

    Lucene phrase queries are matched against the index's token stream, which
    is built without punctuation ("What's The Deal?" indexes as
    "whats the deal"). Removing punctuation without inserting spaces keeps the
    query tokens aligned with the index; the canonical
    :func:`normalize_title_for_lookup` replaces punctuation with spaces and is
    therefore unsuitable for phrase queries.

    Only parenthetical cover annotations ("(PSY Cover)", "(Cover)") are
    stripped — MusicBrainz titles omit those, so a track titled "Gangnam
    Style (PSY Cover)" must query for "gangnam style", not the unmatched
    phrase "gangnam style psy cover". Other annotations such as "(Epic
    Edition)" are preserved because MusicBrainz release groups carry them in
    their real titles (e.g. the "Das Elfte Gebot (Epic Edition)" single).
    """

    if not title:
        return ""

    value = re.sub(r"\s*\([^)]*\bcover\b[^)]*\)", "", title, flags=re.IGNORECASE)
    value = unicodedata.normalize("NFD", value.lower())
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = re.sub(r"[^\w\s]", "", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_title_for_lastfm(
    title: str,
) -> str:
    """
    Lighter normalization for Last.fm.
    """

    if not title:
        return ""

    value = clean_title(
        title,
        remove_brackets=True,
        remove_single_release=False,
        remove_remaster=True,
    )

    value = (
        value.replace("“", "")
             .replace("”", "")
             .replace("«", "")
             .replace("»", "")
             .replace("–", "-")
             .replace("—", "-")
             .replace("…", "...")
    )

    return normalize_string(value)


# =============================================================================
# ✅ CANONICAL ENTRY POINTS (NEW + IMPORTANT)
# =============================================================================



def normalize_artist(value: str) -> str:
    value = strip_featured_artist(value)
    return normalize_string(value)


def clean_artist_name_for_storage(value: str) -> str:
    """Conservative canonicalization for artist/album_artist DB fields.

    Unlike ``normalize_artist`` (which lowercases for comparison), this
    preserves readable casing while collapsing multi-valued artist fields
    (e.g. "Artist A • Artist B / Artist C") and normalising all-caps or
    all-lower variants to title case.

    Args:
        value: Raw artist name string.

    Returns:
        Cleaned artist name suitable for DB storage.
    """
    if not value:
        return ""

    cleaned = " ".join(str(value).strip().split())
    if not cleaned:
        return ""

    # Collapse multi-valued artist fields that can appear from malformed
    # metadata.  Keep exactly one canonical artist token.
    parts = [
        p.strip()
        for p in re.split(r"\s*[•·]+\s*|\s+[/|;]+\s+", cleaned)
        if p.strip()
    ]
    if len(parts) > 1:
        buckets: dict[str, int] = {}
        for part in parts:
            key = normalize_string(part)
            buckets[key] = buckets.get(key, 0) + 1
        most_common_key = max(buckets, key=lambda k: buckets[k])
        for part in parts:
            if normalize_string(part) == most_common_key:
                cleaned = part
                break

    # Normalise all-caps / all-lower names to title case
    # (e.g. EELS → Eels, radiohead → Radiohead).
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9' .-]*", cleaned):
        letters = re.sub(r"[^A-Za-z]+", "", cleaned)
        if letters and (letters.islower() or letters.isupper()):
            cleaned = cleaned.title()

    return " ".join(cleaned.split())


def normalize_album(value: str) -> str:
    return normalize_string(
        clean_title(
            value,
            remove_brackets=True,
            remove_single_release=False,
            remove_remaster=True,
        )
    )

# alias
normalize = normalize_title_for_lookup


# =============================================================================
# ✅ LIGHT HEURISTICS (SAFE)
# =============================================================================

def detect_cover_and_normalize_title(title: str) -> tuple[bool, str]:
    if not title:
        return False, ""

    normalized = normalize_title_for_lookup(title)
    is_cover = "cover" in title.lower()

    return is_cover, normalized


def normalise_result(result: Any) -> tuple[dict[str, Any], int]:
    """Normalise different service return shapes into (dict, int).

    Used by the queue orchestrator to handle various processor return types.

    Supported shapes:
    - ``(dict, int)`` — payload + HTTP status
    - ``dict`` — success/error payload
    - ``bool`` — treated as success/failure
    - ``None`` — treated as success with no result
    """
    if isinstance(result, tuple) and len(result) == 2:
        payload, status = result
        if isinstance(payload, dict) and isinstance(status, int):
            payload.setdefault("success", status < 400)
            return payload, status

    if isinstance(result, dict):
        status = 200 if result.get("success", True) else 500
        result.setdefault("success", status < 400)
        return result, status

    if isinstance(result, bool):
        return {"success": result}, 200 if result else 500

    if result is None:
        return {"success": True, "result": None}, 200

    return {"success": True, "result": result}, 200


def is_remastered_only_variant(title: str) -> bool:
    if not title:
        return False

    t = title.lower()
    return "remaster" in t or "remastered" in t


def normalise_year_tag(raw_year: str | int | None) -> str:
    """Extract a clean 4-digit year from a potentially longer date string.

    MusicBrainz returns full ISO dates (e.g. ``2007-11-23``).  Writing the
    full date to tag fields causes Navidrome to treat tracks from the same
    album as separate releases when some tracks carry the full date and
    others only the year.  This normalises to a consistent year-only value.

    Args:
        raw_year: Year value (string, int, or None).

    Returns:
        4-digit year string, or empty string if the input is empty/invalid.
    """
    if not raw_year:
        return ""
    m = re.search(r"((?:19|20)\d{2})", str(raw_year))
    return m.group(1) if m else str(raw_year).strip()


# =============================================================================
# ✅ TITLE CLEANUP
# =============================================================================

_COVER_ATTRIBUTION_RE = re.compile(
    r"\s*[\(\[][^\)\]]*cover[^\)\]]*[\)\]]\s*$",
    re.IGNORECASE,
)


def strip_cover_attribution(title: str) -> str:
    """Strip cover attributions from the end of a track title.

    Removes trailing parenthesised or bracketed text containing the word
    "cover" (case-insensitive), e.g. ``"(Foo Fighters Cover)"``,
    ``"[Beatles Cover]"``, ``"(Cover Version)"``.

    Args:
        title: Track title to clean.

    Returns:
        Cleaned title with cover attribution removed.
    """
    if not title:
        return ""
    result = _COVER_ATTRIBUTION_RE.sub("", title).strip()
    return result if result else title


# =============================================================================
# ✅ COVER-DETECTION HELPERS (migrated from legacy cover_detector.py)
# =============================================================================

def canonical_track_title(value: str) -> str:
    """Normalize track titles so album/version variants still match canonical recordings."""
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\s+-\s+.*$", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def normalize_name(value: str) -> str:
    """Normalize person/group names for robust matching."""
    if not value:
        return ""
    normalized = value.lower().strip()
    normalized = normalized.replace("'", "'")
    normalized = re.sub(r"\b(the|and)\b", " ", normalized)
    normalized = re.sub(r"[^a-z0-9\s]", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def names_match(left: str, right: str) -> bool:
    """Match names with token overlap to handle middle names and variants."""
    left_norm = normalize_name(left)
    right_norm = normalize_name(right)
    if not left_norm or not right_norm:
        return False
    if left_norm == right_norm:
        return True
    left_tokens = {t for t in left_norm.split() if len(t) > 1}
    right_tokens = {t for t in right_norm.split() if len(t) > 1}
    if not left_tokens or not right_tokens:
        return False
    if left_tokens <= right_tokens or right_tokens <= left_tokens:
        return True
    intersection = left_tokens & right_tokens
    return len(intersection) >= max(2, min(len(left_tokens), len(right_tokens)))


def normalize_writer_credits(writers: list[str]) -> list[str]:
    """Split combined writer credits and dedupe names."""
    normalized: list[str] = []
    for writer in writers or []:
        text = str(writer or "").strip()
        if not text:
            continue
        parts = re.split(r"\s*[;/,&]|\s+and\s+", text, flags=re.IGNORECASE)
        for part in parts:
            name = re.sub(r"^\(+|\)+$", "", part.strip())
            if name and name not in normalized:
                normalized.append(name)
    return normalized


# =============================================================================
# ✅ DISCOGS CLEANUP
# =============================================================================

DISCOGS_ARTIST_ID_RE = re.compile(
    r"\[a\d+\]"
)

DISCOGS_ORPHANED_AKA_RE = re.compile(
    r"\baka\s*(?=\s*\(|,|\.|$)",
    re.IGNORECASE,
)

DISCOGS_LEADING_AKA_RE = re.compile(
    r"^\s*aka\s+",
    re.IGNORECASE,
)


def clean_discogs_biography(text: str) -> str:
    """
    Clean Discogs biography text.

    Discogs often embeds internal artist references such as:

        [a123456]

    and combinations such as:

        [a111] aka [a222]

    which should not be shown to users.

    Examples:

        "[a111] aka [a222]"
        -> ""

        "[a111] aka John Smith"
        -> "John Smith"

        "John Doe [a123456]"
        -> "John Doe"
    """

    if not text:
        return ""

    cleaned = DISCOGS_ARTIST_ID_RE.sub(
        "",
        text,
    )

    cleaned = DISCOGS_ORPHANED_AKA_RE.sub(
        "",
        cleaned,
    )

    cleaned = DISCOGS_LEADING_AKA_RE.sub(
        "",
        cleaned,
    )

    cleaned = re.sub(
        r"\s+",
        " ",
        cleaned,
    )

    return cleaned.strip()


def normalize_core_title(
    value: str,
) -> str:
    """
    Strict title normalization for matching.
    """

    return normalize_string(
        clean_title(
            value,
            remove_brackets=True,
            remove_single_release=False,
            remove_remaster=False,
        )
    )


def normalize_core_filename(
    value: str,
) -> str:
    value = re.sub(
        r"\.[a-z0-9]{2,5}$",
        "",
        value or "",
        flags=re.IGNORECASE,
    )

    value = strip_brackets(value)

    return normalize_string(value)

# ============================================================
# ✅ TRACK PREFIX CLEANUP
# ============================================================

def strip_track_number_prefix(title: str) -> str:
    """
    Remove leading track numbers and trailing Soulseek IDs.
    """

    if not title:
        return title

    # Remove leading track/disc prefix (e.g. "01 -", "1-05 -", "0108.")
    cleaned = re.sub(
        r'^\d+(?:\s*-\s*\d+)?\s*[-\.]\s*',
        '',
        title
    ).strip()

    # Remove trailing Soulseek UID (>=12 digits)
    cleaned = re.sub(r'_\d{12,}$', '', cleaned).strip()

    return cleaned if cleaned else title


# ============================================================
# ✅ TEXT NORMALIZATION FOR MATCHING
# ============================================================

def normalize_match_text(value: str) -> str:
    if not value:
        return ""

    normalized = value.lower().strip()
    normalized = strip_track_number_prefix(normalized)

    replacements = {
        "&": "and",
        "’": "'",
        "`": "'",
        "-": " ",
        "_": " ",
        "/": " ",
        "(": " ",
        ")": " ",
        "[": " ",
        "]": " ",
    }

    for src, dst in replacements.items():
        normalized = normalized.replace(src, dst)

    return " ".join(normalized.split())


# ============================================================
# ✅ TRACK/DISC EXTRACTION (FROM TAGS)
# ============================================================

def extract_track_disc(
    value: str,
    *,
    is_filename: bool = False,
) -> tuple[Optional[int], Optional[int]]:
    """
    Extract (track_number, disc_number).

    Supported formats:

        05       -> (5, None)
        05/12    -> (5, None)
        1-05     -> (5, 1)
        1/05     -> (5, 1)
        1.05     -> (5, 1)

    If is_filename=True the filename is converted
    to a stem before parsing.
    """

    if not value:
        return None, None

    text = value

    if is_filename:
        text = os.path.splitext(
            os.path.basename(value)
        )[0]

    text = str(text).strip()

    # disc-track format
    match = re.match(
        r"^\s*(\d{1,2})\s*[-/\.]\s*(\d{1,3})",
        text,
    )

    if match:
        try:
            return (
                int(match.group(2)),
                int(match.group(1)),
            )
        except ValueError:
            return None, None

    # track-only format
    match = re.match(
        r"^\s*(\d{1,3})",
        text,
    )

    if match:
        try:
            return (
                int(match.group(1)),
                None,
            )
        except ValueError:
            return None, None

    return None, None





def _coerce_position_to_int(value, default):
    """Convert MusicBrainz position strings (e.g. 'A1', '1/12') into an integer."""
    raw = str(value or '').strip()
    if not raw:
        return default
    if raw.isdigit():
        return int(raw)
    match = re.search(r"\d+", raw)
    if match:
        return int(match.group(0))
    return default



def sanitize_path(value: str) -> str:
    """Sanitize any string to be safe for OS file systems."""
    if not value: return "Unknown"
    # Unified replacement for path-invalid characters
    clean = re.sub(r'[<>:"|?*\\]', '_', value)
    return clean.strip().strip(".")

def normalize_album_artist(value: str) -> str:
    """Canonical VA/Various Artists handling."""
    key = " ".join(str(value or "").lower().split())
    if any(key == v or key.startswith(f"{v} ") for v in ["various", "various artists", "va", "v/a"]):
        return "Various Artists"
    return value.strip()


def safe_int(value, default: int = 0) -> int:
    """Safely convert a value to int, returning *default* on failure.

    Handles None, empty strings, and non-convertible types gracefully.
    """
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_str(value, default: str = "") -> str:
    """Safely convert a value to string, returning *default* on failure.

    Handles None and other edge cases gracefully.
    """
    if value is None:
        return default
    return str(value)


def queue_duration_seconds(value: Any) -> Optional[float]:
    """Normalize a download_queue ``duration`` value to seconds.

    Queue rows historically store duration in inconsistent units:
    - Rows written via ``add_release_tracks_to_queue`` persist the raw
      MusicBrainz millisecond ``length`` (e.g. ``233000`` for 3:53).
    - Rows written via the download matching service (and rows whose missing
      duration is backfilled by the completion service) store plain seconds
      (e.g. ``233``).

    Matchers therefore accept either unit: a value of 3000 or above is treated
    as milliseconds and converted, smaller values are already seconds. Returns
    None for missing/non-numeric/zero values so callers can skip the check.

    Args:
        value: Stored queue duration (seconds or milliseconds).

    Returns:
        Duration in seconds as a float, or None when unusable.
    """
    if value is None or value == "":
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value / 1000 if value >= 3000 else value


def is_valid_version(track_title: str, allow_live_remix: bool = False) -> bool:
    """Validate track version against blacklist and whitelist.

    Filters out undesirable versions (live, remix, edit, etc.) from search
    results unless the album context permits them (e.g. a live album).

    Args:
        track_title: Track title to validate.
        allow_live_remix: If True, allow "live" and "remix" variants.

    Returns:
        True if the track version is acceptable.
    """
    title = track_title.lower()
    blacklist = {"live", "remix", "mix", "edit", "rework", "bootleg"}
    whitelist = {"remaster"}
    if allow_live_remix:
        blacklist = blacklist - {"live", "remix"}
    if any(b in title for b in blacklist) and not any(w in title for w in whitelist):
        return False
    return True