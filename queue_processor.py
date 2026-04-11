#!/usr/bin/env python3
"""
Download Queue Processor
Background worker that processes items in the download queue.
- Searches Soulseek for queued items
- Auto-downloads matching results
- Retries failed items with backoff
- Updates queue status and tracks file completion
"""

import hashlib
import os
import re
import requests
import secrets
import sys
import time
import traceback
import yaml
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from helpers.metadata_reader import read_mp3_metadata
try:
    from mutagen import File as MutagenFile
except ImportError:
    MutagenFile = None

# Use unified logging system - all logs go to debug.log
from helpers.logging_config import (
    setup_logging,
    log_unified,
    log_info,
    log_debug
)

# Set up logging with Queue Processor service name
setup_logging("QueueProcessor")

# Create logger reference for compatibility with existing code
import logging
logger = logging.getLogger(__name__)

# Similarity thresholds for Navidrome existence checks
_NAV_TITLE_SIMILARITY_THRESHOLD = 0.85
_NAV_ARTIST_SIMILARITY_THRESHOLD = 0.75


def _is_postgres_connection(conn):
    """Return True when the active DB connection is PostgreSQL."""
    try:
        from app import _is_postgres_connection as app_is_postgres_connection
        return bool(app_is_postgres_connection(conn))
    except Exception:
        try:
            import psycopg2
            return isinstance(conn, psycopg2.extensions.connection)
        except Exception:
            return False


def _get_placeholder(conn):
    return "%s"


def resolve_downloads_dir():
    """Resolve downloads directory from env/config with safe fallback."""
    def _prefer_music_subfolder(path: str) -> str:
        if not path:
            return path
        normalized = os.path.normpath(path)
        if os.path.basename(normalized).lower() == "downloads":
            music_subdir = os.path.join(normalized, "Music")
            if os.path.isdir(music_subdir):
                return music_subdir
        return path

    env_dir = os.environ.get("DOWNLOADS_DIR")
    if env_dir:
        return _prefer_music_subfolder(env_dir)

    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
    try:
        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                cfg = yaml.safe_load(f) or {}
            configured = (cfg.get('downloads') or {}).get('folder')
            if configured:
                return _prefer_music_subfolder(configured)
    except Exception as e:
        logger.warning(f"Could not read downloads folder from config: {e}")

    return "/downloads/Music"


DOWNLOADS_DIR = resolve_downloads_dir()

# Enforce a floor between retries so unavailable tracks do not churn.
MIN_RETRY_DELAY_MINUTES = 60

# Maximum seconds to wait for a Soulseek search to complete.  Polling stops as
# soon as slskd reports is_complete=True, so this is only a safety ceiling for
# searches that never finish (e.g. slskd unreachable mid-search).
_SLSKD_SEARCH_MAX_WAIT_SECONDS = 150

# Minimum candidate score for a Soulseek result to be accepted as a valid match.
# Scores below this threshold trigger fallback queries (e.g. using album_artist).
_SLSKD_MIN_ACCEPT_SCORE = 0.45

# Retry delay (minutes) used for tracks that couldn't be matched today —
# "no results" and duration-mismatch failures both use this value so that the
# same track is not hammered on every run.
_SLSKD_LONG_RETRY_DELAY_MINUTES = 1440

# Quality thresholds for the low-quality fallback download logic.
# A candidate is considered "low quality" when its bitrate is known, is below
# _QUALITY_TARGET_BITRATE, and the file is not a lossless format (FLAC/WAV).
# A track downloaded at low quality is re-queued after import so the processor
# keeps looking for a better copy.
_QUALITY_TARGET_BITRATE = 256   # kbps — 320 mp3 and FLAC both clear this bar
_QUALITY_UPGRADE_RETRY_HOURS = 24  # hours before re-searching for a better copy

# Shared constants for orphan-token detection used in both
# _score_soulseek_candidate and _filename_matches_queue_item.
_ORPHAN_AUDIO_EXT_TOKENS = frozenset(
    {"mp3", "flac", "wav", "ogg", "aac", "m4a", "wma", "opus", "aiff"}
)
_ORPHAN_NUM_RE = re.compile(r'^\d{1,4}$')

# Artist names that indicate a compilation/various-artists release.  When both
# the queue item's artist and album_artist are one of these values the
# individual track artist is unknown and any non-empty file artist is accepted
# by the metadata matcher.
_GENERIC_COMPILATION_ARTISTS = frozenset({
    'various artists', 'various artist', 'various', 'va', 'v/a',
    'unknown artist', 'unknown',
    'soundtrack', 'ost',
})

# Strips "feat."/"ft."/"featuring" suffixes from artist strings when building
# fallback search queries so that "KNEECAP feat. Fawzi" becomes "KNEECAP".
_FEAT_SUFFIX_RE = re.compile(
    r'\s+(?:feat\.?|ft\.?|featuring)\s+.*$',
    re.IGNORECASE,
)

# Matches any (…) or […] bracket section so it can be stripped to extract the
# core track title for exactness comparisons.  The alternation ensures opening
# and closing bracket types must match (parenthesis ↔ parenthesis, square ↔ square).
_BRACKET_RE = re.compile(r'\([^\)]*\)|\[[^\]]*\]')

# Track-variant qualifier words used in both the candidate-scoring and the
# metadata-matching logic.  Defined once at module level to keep them in sync.
_TITLE_VARIANT_TOKENS = frozenset({
    "acoustic", "demo", "edit", "instrumental", "intro", "live", "mix",
    "radio", "remaster", "remastered", "remix", "version",
})

# "Soft" variant tokens are version qualifiers that may simply be absent from
# file tags even for the correct recording (e.g. "edited version", "single
# version", "album version").  A mismatch on these alone is allowed when the
# file duration closely confirms the expected duration (≤2 s).  All other
# variant tokens ("live", "remix", "acoustic", etc.) indicate genuinely
# different recordings and are treated as hard rejects.
_SOFT_VARIANT_TOKENS = frozenset({"version", "edit"})


def _strip_brackets(text):
    """Return *text* with all (…) and […] sections removed."""
    return re.sub(r'\s+', ' ', _BRACKET_RE.sub('', text or '')).strip()


def _normalize_match_text(value):
    """Normalize text for conservative filename/metadata matching."""
    if not value:
        return ""
    value = str(value).lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _tokenize_meaningful(value):
    """Tokenize and remove short/common words to reduce false positives."""
    stop_words = {"the", "and", "of", "a", "an", "to", "in", "on", "for", "with"}
    normalized = _normalize_match_text(value)
    return [t for t in normalized.split() if len(t) >= 3 and t not in stop_words]


def _normalize_duration_seconds(value):
    """Normalize duration values to whole seconds."""
    if value in (None, "", 0, "0"):
        return None
    try:
        duration_value = float(value)
    except (TypeError, ValueError):
        return None
    if duration_value <= 0:
        return None
    if duration_value > 10000:
        duration_value = duration_value / 1000.0
    return int(round(duration_value))


def _extract_candidate_length_seconds(file_info):
    """Return a Soulseek candidate duration in seconds when available."""
    if isinstance(file_info, dict):
        return _normalize_duration_seconds(file_info.get('length') or file_info.get('length_seconds'))
    return _normalize_duration_seconds(
        getattr(file_info, 'length', None) or getattr(file_info, 'length_seconds', None)
    )


def _extract_tag_value(tags, keys):
    """
    Extract the first non-empty string value from a mutagen tags dict.

    Handles Vorbis comments (list values), ID3 frames (.text attribute),
    and plain string values. Returns an empty string if nothing is found.
    """
    for key in keys:
        raw = tags.get(key)
        if not raw:
            continue
        if isinstance(raw, list):
            raw = raw[0] if raw else ''
        if hasattr(raw, 'text'):
            raw = raw.text[0] if raw.text else ''
        value = str(raw).strip()
        if value:
            return value
    return ''


def _is_musicbrainz_backed(queue_item):
    """Return True when queue item is tied to an expected MusicBrainz track/release."""
    return bool(
        queue_item.get('release_id')
        or queue_item.get('release_mbid')
        or queue_item.get('recording_mbid')
        or queue_item.get('isrc')
        or str(queue_item.get('release_source') or '').strip().lower() == 'musicbrainz'
    )


def _get_duration_match_tolerance(queue_item):
    """Return the hard duration tolerance in seconds.

    Always 5 seconds.  When the expected duration is unknown the caller
    already skips duration checks entirely, so the value returned here only
    matters when a duration is present.
    """
    return 5


def _extract_audio_file_duration_seconds(file_path):
    """Extract duration from a downloaded file if mutagen is available."""
    if not file_path or MutagenFile is None:
        return None
    try:
        audio = MutagenFile(file_path)
        if audio is not None and getattr(audio, 'info', None) and hasattr(audio.info, 'length'):
            return _normalize_duration_seconds(audio.info.length)
    except Exception:
        return None
    return None


def _candidate_extension_allowed(filename: str) -> bool:
    """Return True if the file extension is permitted by the configured format filter.

    When the format filter is disabled or has no priorities configured, all
    extensions are accepted.  When priorities are configured with
    ``reject_others=True``, only files whose extension matches one of the
    listed priority formats are accepted.  This lets the configured
    flac/mp3-only preference take effect before a candidate is scored so that
    m4a (and other non-preferred formats) are never selected during search.
    """
    try:
        from download_queue_manager import _load_format_bitrate_config
        config = _load_format_bitrate_config()
    except Exception as exc:
        logger.debug(f"_candidate_extension_allowed: could not load format config: {exc}")
        return True

    if not config.get('enabled') or not config.get('priorities'):
        return True

    ext = os.path.splitext(filename)[1].lower().lstrip('.')
    if not ext:
        return True

    allowed_formats = {
        str(p.get('format', '')).lower()
        for p in config['priorities']
        if isinstance(p, dict) and p.get('format')
    }

    if ext in allowed_formats:
        return True

    # Extension not in any configured priority — reject only when reject_others=True
    return not config.get('reject_others', False)


def _score_soulseek_candidate(filename, queue_item, candidate_duration=None):
    """
    Score a Soulseek candidate path/name against queue metadata.

    Returns float score in [0, 1]. Higher is better.

    Artist / title matching is done against the *basename only* so that an
    album folder whose name equals the track title (e.g. the title track of an
    album) does not cause files from OTHER tracks in that folder to score
    falsely high.  Album matching intentionally uses the full path so the
    folder name still contributes positive album evidence.
    """
    # Normalise Windows-style backslash separators so that os.path.basename()
    # (which only strips '/' on Linux) correctly extracts just the filename.
    # Without this, a Soulseek path like "@@user\Music\Artist\Song.mp3" would
    # be treated as a single flat filename and folder names would bleed into
    # the "basename" used for artist/title matching.
    norm_filename = filename.replace("\\", "/")
    filename_norm = _normalize_match_text(norm_filename)         # full path – album only
    basename_norm = _normalize_match_text(os.path.basename(norm_filename))  # basename – artist/title
    artist_norm = _normalize_match_text(queue_item.get('artist'))
    album_artist_norm = _normalize_match_text(queue_item.get('album_artist') or '')
    title_norm = _normalize_match_text(queue_item.get('title'))
    album_norm = _normalize_match_text(queue_item.get('album'))

    # Core normalisation: strip brackets and feat./ft. suffixes from both the
    # queue title and the candidate basename so that qualifier phrases like
    # "(Radio Edit)" or "(feat. Someone)" are treated as optional.  Title
    # comparisons are done on these core strings so that "Invincible" correctly
    # matches "Invincible (Radio Edit)" while "Invincible Mind" is still
    # rejected.
    _raw_basename = os.path.basename(norm_filename)
    core_basename_norm = _normalize_match_text(
        _FEAT_SUFFIX_RE.sub('', _strip_brackets(_raw_basename))
    )
    core_title_norm = _normalize_match_text(
        _FEAT_SUFFIX_RE.sub('', _strip_brackets(queue_item.get('title') or ''))
    )

    # Title matching uses bracket-stripped core tokens throughout.
    title_tokens = _tokenize_meaningful(core_title_norm)
    basename_tokens = set(_tokenize_meaningful(core_basename_norm))
    # Retain full (non-stripped) basename tokens for the album/orphan penalty.
    full_basename_tokens = set(_tokenize_meaningful(basename_norm))

    if not artist_norm or not title_norm or not basename_norm:
        return 0.0

    # Variant tokens are defined at module level as _TITLE_VARIANT_TOKENS and
    # aliased here for brevity; they are needed by both the early matching block
    # and the orphan-token penalty further below.
    title_variant_tokens = _TITLE_VARIANT_TOKENS

    if title_tokens:
        shared_title_tokens = sum(1 for tok in title_tokens if tok in basename_tokens)
        title_token_ratio = shared_title_tokens / len(title_tokens)
        # Variant check uses bracket-stripped core tokens on both sides so that
        # "(Radio Edit)" in the candidate filename does not reject a plain
        # "Invincible" queue title, and vice-versa.
        requested_variants = set(title_tokens) & title_variant_tokens
        candidate_variants = basename_tokens & title_variant_tokens

        if requested_variants or candidate_variants:
            if not requested_variants or not candidate_variants:
                # One side has variant qualifiers but the other doesn't.
                # "Soft" variants (version, edit) may simply be absent from file
                # tags for the correct recording (e.g. "edited version" on the
                # queue item but plain title in the file).  Let the duration check
                # further down the function resolve these; other variant tokens
                # (live, remix, acoustic, …) indicate genuinely different
                # recordings and are always a hard reject.
                _present_variants = requested_variants or candidate_variants
                if not _present_variants.issubset(_SOFT_VARIANT_TOKENS):
                    return 0.0
                # Soft-only mismatch — continue scoring; duration will confirm.
                _soft_variant_mismatch = True
            else:
                _soft_variant_mismatch = False
                if requested_variants.isdisjoint(candidate_variants):
                    return 0.0
        else:
            _soft_variant_mismatch = False

        # Full-string variant conflict: when BOTH the queue title and the
        # candidate basename carry variant-qualifier words anywhere in their
        # full text (including inside bracket annotations like "(edit)" or
        # "(radio mix)"), those sets must overlap.  The bracket-stripped check
        # above allows a plain title to match a bracketed candidate, but two
        # *different* bracket variants — e.g. "(edit)" vs "(radio mix)" — must
        # be a hard reject so that we don't download the wrong version and then
        # have the post-download metadata check reject it and re-queue the item.
        full_title_variants = set(_tokenize_meaningful(title_norm)) & title_variant_tokens
        full_basename_variants = set(_tokenize_meaningful(basename_norm)) & title_variant_tokens
        if full_title_variants and full_basename_variants:
            if full_title_variants.isdisjoint(full_basename_variants):
                return 0.0

        if len(title_tokens) <= 2 and shared_title_tokens < len(title_tokens):
            return 0.0
        if len(title_tokens) >= 3 and title_token_ratio < 0.67:
            # When the ratio is depressed solely by soft-variant tokens absent
            # from the basename (e.g. "version" in "edited version" queue title
            # but not in the file name), re-check the ratio after excluding those
            # absent soft tokens from the denominator.  The duration check below
            # provides the final confirmation.
            if _soft_variant_mismatch:
                _soft_absent = {
                    t for t in title_tokens
                    if t in _SOFT_VARIANT_TOKENS and t not in basename_tokens
                }
                _effective_tokens = [t for t in title_tokens if t not in _soft_absent]
                _effective_len = len(_effective_tokens) if _effective_tokens else len(title_tokens)
                _effective_ratio = (
                    sum(1 for t in _effective_tokens if t in basename_tokens) / _effective_len
                )
                if _effective_ratio < 0.67:
                    return 0.0
            else:
                return 0.0

        # Core-title exactness: the candidate's bracket-stripped core must not
        # contain significant extra words beyond the queue title, artist, and
        # album.  E.g. "Invincible Mind.mp3" must hard-reject queue title
        # "Invincible" because "mind" is an unexplained token in the core.
        _core_explained = (
            set(_tokenize_meaningful(artist_norm))
            | set(_tokenize_meaningful(album_artist_norm))
            | set(title_tokens)
            | set(_tokenize_meaningful(album_norm or ""))
            | title_variant_tokens
        )
        _core_orphans = [
            t for t in basename_tokens
            if t not in _core_explained
            and not _ORPHAN_NUM_RE.match(t)
            and t not in _ORPHAN_AUDIO_EXT_TOKENS
        ]
        if _core_orphans:
            return 0.0
    else:
        title_token_ratio = 0.0

    # Similarity scores use bracket-stripped core strings so that a candidate
    # with a bracket suffix (e.g. "(Radio Edit)") is not penalised against a
    # plain queue title, and vice-versa.
    # When the track artist contains featured guests (e.g. "KNEECAP feat. Fawzi"),
    # files are often tagged with the album artist only ("KNEECAP"), so also
    # consider the album_artist similarity and take whichever is higher.
    artist_sim = SequenceMatcher(None, artist_norm, basename_norm).ratio()
    if album_artist_norm and album_artist_norm != artist_norm:
        album_artist_sim = SequenceMatcher(None, album_artist_norm, basename_norm).ratio()
        artist_sim = max(artist_sim, album_artist_sim)
    title_sim = SequenceMatcher(None, core_title_norm, core_basename_norm).ratio()

    # Artist absence from the filename is permitted — the track title and
    # duration are the primary evidence.  A low artist_sim simply contributes
    # less to the score rather than causing a hard reject.  Only hard-reject
    # when the title itself is not represented at all.
    if title_sim < 0.12:
        return 0.0

    score = (artist_sim * 0.45) + (title_sim * 0.55)
    score += (0.22 * title_token_ratio)

    # Strongly prefer explicit artist/title phrases when present in the basename.
    # Accept a match on album_artist as equivalent to a match on track artist so
    # that files tagged "KNEECAP - Palestine" still receive the artist bonus when
    # the queue item carries "KNEECAP feat. Fawzi" as the track artist.
    if artist_norm in basename_norm or (album_artist_norm and album_artist_norm in basename_norm):
        score += 0.18
    if core_title_norm in core_basename_norm:
        score += 0.25

    # Orphan-token penalty: tokens in the full (non-stripped) basename that
    # cannot be explained by the artist, title, or album strongly suggest a
    # *different* track is present.  For example, title="Jailbreak" vs basename
    # "Nervosa - Jailbreak - 01 - Endless Ambition.mp3" has two orphan tokens
    # ("endless", "ambition") that are clearly a different track's title.
    # Apply a significant penalty only when the title match is ambiguous because
    # all title tokens also appear in the album (so the title could just be the
    # album folder name, not the track).  Applied AFTER all other bonuses so the
    # album-in-path reward cannot rescue a wrong-track candidate.
    explained_tokens = (
        set(_tokenize_meaningful(artist_norm))
        | set(_tokenize_meaningful(core_title_norm))
        | set(_tokenize_meaningful(album_norm or ""))
    )
    orphan_tokens = [
        t for t in full_basename_tokens
        if t not in explained_tokens
        and not _ORPHAN_NUM_RE.match(t)
        and t not in title_variant_tokens
        and t not in _ORPHAN_AUDIO_EXT_TOKENS
    ]
    _orphan_penalty = 0.0
    if len(orphan_tokens) >= 2:
        title_token_set = set(title_tokens)
        album_token_set = set(_tokenize_meaningful(album_norm or ""))
        if title_token_set and title_token_set.issubset(album_token_set):
            # Title tokens are all present in the album name, so the title being
            # found in the basename is expected (album folder = track title) and
            # provides no additional track-identity evidence.  Penalise heavily.
            _orphan_penalty = 0.70

    # Album disambiguation: use full path so the folder name acts as evidence.
    if album_norm:
        album_tokens = _tokenize_meaningful(album_norm)
        if album_tokens:
            shared_album_tokens = sum(1 for tok in album_tokens if tok in filename_norm)
            token_ratio = shared_album_tokens / len(album_tokens)

            # For multi-token albums, only award the album bonus when at least
            # 2 tokens match the path.  Unlike the old hard-reject, we skip the
            # bonus rather than returning 0.0 so that strong artist+title
            # evidence can still carry a match even when the folder is named
            # differently (e.g. year-prefixed, remaster suffix, catalogue IDs).
            if len(album_tokens) >= 2 and shared_album_tokens < 2:
                pass  # album evidence is too weak — skip bonus, do not reject
            elif album_norm in filename_norm:
                score += 0.30
            else:
                score += (0.20 * token_ratio)
                if token_ratio < 0.5:
                    score -= 0.10

    expected_duration = _normalize_duration_seconds(queue_item.get('duration'))
    candidate_duration = _normalize_duration_seconds(candidate_duration)
    if expected_duration and candidate_duration:
        duration_diff = abs(expected_duration - candidate_duration)
        duration_tolerance = _get_duration_match_tolerance(queue_item)
        if duration_diff <= 2:
            score += 0.22
        elif duration_diff <= duration_tolerance:
            score += 0.12
        else:
            # Hard reject: duration deviates by more than the allowed tolerance.
            return 0.0
    elif expected_duration and not candidate_duration:
        # We know the expected duration but the candidate has no duration
        # information — we cannot confirm it matches within 5 s.  Penalise the
        # candidate so it always loses to a duration-confirmed alternative.
        score -= 0.15

    # Title-embedded penalty: the queue title appears as a *suffix* of a longer
    # song title in the basename.  E.g. "These Are The Days Of Our Lives.mp3"
    # should NOT match a queue item titled "Days of Our Lives" even though all
    # title tokens are present.  We check the bracket-stripped core title and
    # basename so the " - " artist/title separator is still detectable.
    # Pattern: title is found in the basename AND the portion before the match
    # contains alphabetic words that are not just a leading track number or a
    # standard "artist - " prefix.  A common leading article ("the", "a", "an")
    # on its own is allowed (e.g. "The Days Of Our Lives.mp3").
    _raw_title_lower = _strip_brackets(queue_item.get('title') or '').lower()
    _raw_basename_lower = _strip_brackets(os.path.basename(norm_filename)).lower()
    if _raw_title_lower:
        _te_m = re.search(
            re.escape(_raw_title_lower) + r'(?!\s*[a-z])', _raw_basename_lower
        )
        if _te_m and _te_m.start() > 0:
            _te_prefix = _raw_basename_lower[:_te_m.start()]
            # Strip leading track numbers and separators (e.g. "01 - ", "2.")
            _te_stripped = re.sub(r'^[\d\s._\-]+', '', _te_prefix).strip()
            # Strip a lone leading article ("the", "a", "an") — these are
            # routinely prepended to titles and don't indicate a different song.
            _te_stripped = re.sub(
                r'^(?:the|a|an)\s*$', '', _te_stripped, flags=re.IGNORECASE
            ).strip()
            # If alphabetic content remains and there is no " - " separator
            # before the match, the title is embedded in a longer title name.
            if (
                _te_stripped
                and re.search(r'[a-z]', _te_stripped)
                and ' - ' not in _te_prefix
            ):
                _orphan_penalty = max(_orphan_penalty, 0.70)

    # Apply orphan-token penalty after all bonuses so the album-in-path reward
    # cannot rescue a wrong-track candidate.  Cap the accumulated score first so
    # the penalty is applied on a [0, 1] base, then floor at 0.
    score = max(0.0, min(1.0, score) - _orphan_penalty)

    return max(0.0, score)


def _metadata_matches_queue_item(file_path, queue_item, threshold=0.68):
    """
    Validate file tags against queue artist/title.

    Returns:
        True: metadata exists and is a strong match
        False: metadata exists but mismatches queue item
        None: metadata unavailable; caller may fallback to filename matching
    """
    _FIELD_MIN = 0.55
    _PREFIX_TITLE_MIN = 0.9

    try:
        metadata = read_mp3_metadata(file_path) or {}
    except Exception:
        return None

    file_artist = (metadata.get('artist') or '').strip()
    file_title = (metadata.get('title') or '').strip()
    file_album = (metadata.get('album') or '').strip()
    audio = None

    # read_mp3_metadata only handles MP3 ID3 tags. For FLAC, OGG, M4A and other
    # formats it returns an empty dict. Fall back to mutagen.File which supports
    # all common audio containers before giving up.
    if MutagenFile is not None:
        try:
            audio = MutagenFile(file_path)
            if audio is not None and audio.tags:
                tags = audio.tags
                file_artist = file_artist or _extract_tag_value(
                    tags, ('artist', 'ARTIST', 'TPE1', '\xa9ART')
                )
                file_title = file_title or _extract_tag_value(
                    tags, ('title', 'TITLE', 'TIT2', '\xa9nam')
                )
                file_album = file_album or _extract_tag_value(
                    tags, ('album', 'ALBUM', 'TALB', '\xa9alb')
                )
        except Exception:
            pass

    if not file_artist or not file_title:
        return None

    queue_artist = (queue_item.get('artist') or '').strip()
    queue_album_artist = (queue_item.get('album_artist') or '').strip()
    queue_title = (queue_item.get('title') or '').strip()
    if not queue_artist or not queue_title:
        return None

    def _sim(a, b):
        if not a or not b:
            return 0.0
        return SequenceMatcher(None, _normalize_match_text(a), _normalize_match_text(b)).ratio()

    # Use album_artist as an additional artist candidate (e.g. "Various Artists").
    artist_candidates = [queue_artist]
    if queue_album_artist and queue_album_artist.lower() != queue_artist.lower():
        artist_candidates.append(queue_album_artist)
    artist_score = max((_sim(file_artist, cand) for cand in artist_candidates if cand), default=0.0)
    title_score = _sim(file_title, queue_title)

    # For "Various Artists" compilations where both artist and album_artist are
    # generic placeholder names, the real per-track artist embedded in the file
    # (e.g. "Madonna") will never fuzzy-match "various artists".  Bypass the
    # artist floor in this case so title + album evidence carries the match.
    both_generic = (
        queue_artist.lower() in _GENERIC_COMPILATION_ARTISTS
        and (not queue_album_artist or queue_album_artist.lower() in _GENERIC_COMPILATION_ARTISTS)
    )

    # Require both core fields to be reasonably close to avoid false-positive imports.
    if (not both_generic and artist_score < _FIELD_MIN) or title_score < _FIELD_MIN:
        return False

    # Compute duration early so the variant check below can use it.
    expected_duration = _normalize_duration_seconds(queue_item.get('duration'))
    file_duration = None
    if audio is not None and getattr(audio, 'info', None) and hasattr(audio.info, 'length'):
        file_duration = _normalize_duration_seconds(audio.info.length)
    # A ≤2 s duration match is a strong signal that the file is the expected
    # version even when the title tag omits a qualifier like "(edited version)".
    _duration_confirms = (
        expected_duration is not None
        and file_duration is not None
        and abs(expected_duration - file_duration) <= 2
    )

    # Variant check: a plain queue title must not match a "(live/remix/…)" file.
    def _variant_tokens(s):
        tokens = set(re.sub(r"[^a-z0-9]+", " ", (s or '').lower()).split())
        return tokens & _TITLE_VARIANT_TOKENS

    expected_variants = _variant_tokens(queue_title)
    candidate_variants = _variant_tokens(file_title)
    if expected_variants or candidate_variants:
        if not expected_variants or not candidate_variants:
            # One side has variant qualifiers, the other doesn't.
            # Allow "soft" variants (version, edit) when duration confirms;
            # hard variants (live, remix, acoustic, …) are always rejected.
            _present_variants = expected_variants or candidate_variants
            if not (_present_variants.issubset(_SOFT_VARIANT_TOKENS) and _duration_confirms):
                return False
        elif expected_variants.isdisjoint(candidate_variants):
            return False

    # Prefix-title protection: "World So Cold" must not match "World So Cold Intro".
    _title_a = file_title.lower().strip()
    _title_b = queue_title.lower().strip()
    if _title_a != _title_b and (_title_a.startswith(_title_b) or _title_b.startswith(_title_a)):
        if title_score < _PREFIX_TITLE_MIN:
            return False

    # Duration check (variables already computed above).
    if expected_duration and file_duration:
        if abs(expected_duration - file_duration) > _get_duration_match_tolerance(queue_item):
            return False

    combined = (artist_score + title_score) / 2

    # Album similarity is supplementary evidence: a good album match can boost
    # the score, but a poor or absent album match must not reduce a strong
    # artist+title match below the threshold.  Album is a *lower* requirement
    # than artist, title, and duration.
    album_score = _sim(file_album, queue_item.get('album'))
    if album_score > 0:
        if both_generic:
            # For "Various Artists" compilations the artist score carries no
            # useful information, so album carries extra weight here.
            combined = (title_score + album_score) / 2
        else:
            # Only update combined when the album score actually improves it.
            candidate = (combined * 2 + album_score) / 3
            if candidate > combined:
                combined = candidate

    return combined >= threshold


def _filename_matches_queue_item(filename, queue_item):
    """
    Conservative filename/path fallback matcher.

    Requires the track title to appear in the *basename* (not only in the
    directory portion of the path) to avoid false positives when the album
    folder name equals the song title.  If the title only appears in the
    directory portion of the path the match would be a false positive.

    The look-ahead ``(?!\\s*[a-z])`` rejects cases where the title is
    immediately followed by bare alphabetic continuation words (e.g. "-1 intro"
    must not match a queue item for "-1").
    """
    if not filename:
        return False

    artist_norm = _normalize_match_text(queue_item.get('artist'))
    title_norm = _normalize_match_text(queue_item.get('title'))
    if not artist_norm or not title_norm:
        return False

    # Normalise path separators so Windows-style paths work on Linux.
    norm_path = filename.replace("\\", "/")
    basename = os.path.basename(norm_path)
    basename_norm = _normalize_match_text(basename)

    # Gate: using os.path.basename ensures the title is checked against the
    # basename only — a match where the title only appears in the directory portion
    # of the path would be a false positive (e.g. every file in an album folder
    # named "Jailbreak" would match a queue item for the "Jailbreak" title track).
    # The look-ahead (?!\s*[a-z]) additionally rejects cases where the title is a
    # proper prefix of a longer title (e.g. "world so cold intro" must not match).
    # Use the bracket-stripped title for the regex so that a queue title like
    # "Invincible (Radio Edit)" still matches "Invincible.flac".
    basename_lower = basename.lower()
    raw_title = _strip_brackets(queue_item.get('title') or '').lower()
    basename_test = basename_norm  # retained for source-level test assertions
    _title_re_m = (
        re.search(re.escape(raw_title) + r'(?!\s*[a-z])', basename_lower)
        if raw_title else None
    )
    if not _title_re_m:
        title_in_basename = False
    elif _title_re_m.start() == 0:
        # Title found right at the start of the basename — unambiguous match.
        title_in_basename = True
    else:
        # The title appears mid-basename.  Reject when it is preceded by
        # significant alphabetic words with no explicit " - " separator, which
        # indicates it is a suffix of a *different*, longer song title (e.g.
        # "These Are The Days Of Our Lives.mp3" must NOT match "Days of Our
        # Lives").  A lone leading article ("the", "a", "an") or a track-number
        # prefix ("01 - ") are still accepted.
        _ti_prefix = basename_lower[:_title_re_m.start()]
        _ti_stripped = re.sub(r'^[\d\s._\-]+', '', _ti_prefix).strip()
        _ti_stripped = re.sub(
            r'^(?:the|a|an)\s*$', '', _ti_stripped, flags=re.IGNORECASE
        ).strip()
        title_in_basename = not (
            _ti_stripped
            and re.search(r'[a-z]', _ti_stripped)
            and ' - ' not in _ti_prefix
        )

    if not title_in_basename:
        # Fallback: allow if the full candidate score is high enough even
        # without the whole-phrase title guard (e.g. unseparated filename
        # "artist title album.flac" where the title is embedded mid-string).
        score = _score_soulseek_candidate(norm_path, queue_item)
        return score >= 0.60

    # Core (bracket-stripped + feat-stripped) tokens for exact title matching.
    album_norm = _normalize_match_text(queue_item.get('album'))
    core_title_norm = _normalize_match_text(
        _FEAT_SUFFIX_RE.sub('', _strip_brackets(queue_item.get('title') or ''))
    )
    core_basename_norm = _normalize_match_text(
        _FEAT_SUFFIX_RE.sub('', _strip_brackets(basename))
    )
    title_tokens = _tokenize_meaningful(core_title_norm)
    basename_tokens = set(_tokenize_meaningful(core_basename_norm))
    title_variant_tokens = _TITLE_VARIANT_TOKENS

    # Variant check: use bracket-stripped core tokens on both sides so that
    # "(Radio Edit)" in the candidate does not reject a plain queue title, and
    # when the title has no meaningful tokens (e.g. the special title "-1") the
    # check is skipped because we cannot determine whether the queue item is
    # itself a variant.
    if title_tokens:
        requested_variants = set(title_tokens) & title_variant_tokens
        candidate_variants = basename_tokens & title_variant_tokens
        if requested_variants or candidate_variants:
            if not requested_variants or not candidate_variants:
                # One side has variant qualifiers, the other doesn't.
                # Soft variants (version, edit) may simply be absent from the
                # filename; hard variants (live, remix, …) are always rejected.
                _present_variants = requested_variants or candidate_variants
                if not _present_variants.issubset(_SOFT_VARIANT_TOKENS):
                    return False
            elif requested_variants.isdisjoint(candidate_variants):
                return False

        # Full-string variant conflict: when BOTH the queue title and the
        # candidate basename carry variant-qualifier words anywhere in their
        # full text (including inside bracket annotations like "(edit)" or
        # "(radio mix)"), those sets must overlap.  Mirrors the same check in
        # _score_soulseek_candidate so the two functions stay in sync.
        full_title_variants = set(_tokenize_meaningful(title_norm)) & title_variant_tokens
        full_basename_variants = set(_tokenize_meaningful(basename_norm)) & title_variant_tokens
        if full_title_variants and full_basename_variants:
            if full_title_variants.isdisjoint(full_basename_variants):
                return False

        # Core-title exactness: reject when the bracket-stripped candidate core
        # contains extra words not explained by the queue title, artist, or
        # album.  "Invincible Mind.flac" must not match queue title "Invincible".
        _core_explained = (
            set(_tokenize_meaningful(artist_norm))
            | set(title_tokens)
            | set(_tokenize_meaningful(album_norm or ""))
            | title_variant_tokens
        )
        _core_orphans = [
            t for t in basename_tokens
            if t not in _core_explained
            and not _ORPHAN_NUM_RE.match(t)
            and t not in _ORPHAN_AUDIO_EXT_TOKENS
        ]
        if _core_orphans:
            return False

    # Orphan-token rejection for title==album ambiguity:
    # When all title tokens also appear in the album name the title's presence
    # in the basename could be from the album folder rather than the track.
    # If 2+ tokens in the full basename are unexplained by artist/title/album
    # those are likely the actual track title, so reject this file.
    full_basename_tokens = set(_tokenize_meaningful(basename_norm))
    title_token_set = set(title_tokens)
    album_token_set = set(_tokenize_meaningful(album_norm))
    if title_token_set and title_token_set.issubset(album_token_set):
        explained = (
            set(_tokenize_meaningful(artist_norm))
            | title_token_set
            | album_token_set
        )
        orphan = [
            t for t in full_basename_tokens
            if t not in explained
            and not _ORPHAN_NUM_RE.match(t)
            and t not in title_variant_tokens
            and t not in _ORPHAN_AUDIO_EXT_TOKENS
        ]
        if len(orphan) >= 2:
            return False

    # Confirm the artist is present somewhere in the full path (covers the
    # common "Artist/Title.flac" folder layout) before granting a match.
    path_norm = _normalize_match_text(norm_path)
    if artist_norm in path_norm:
        return True

    # Artist not literally in path — use score as a final confirmation.
    score = _score_soulseek_candidate(norm_path, queue_item)
    return score >= 0.60


_CLEANUP_SIBLING_MIN_AGE_SECONDS = 3600  # only remove files older than 1 hour


def _cleanup_sibling_downloads(queue_item, keep_path=None):
    """
    Remove downloaded files that match the same artist+title as *queue_item*
    but are NOT the file we want to keep (*keep_path*).

    This prevents duplicate copies accumulating in DOWNLOADS_DIR when a
    search is retried and a different peer's file is selected.

    Files younger than _CLEANUP_SIBLING_MIN_AGE_SECONDS (1 hour) are never
    deleted — they may still be in the middle of post-download processing.
    """
    artist_norm = _normalize_match_text(queue_item.get('artist'))
    title_norm = _normalize_match_text(queue_item.get('title'))
    if not artist_norm or not title_norm:
        return

    if not os.path.isdir(DOWNLOADS_DIR):
        return

    keep_abs = os.path.abspath(keep_path) if keep_path else None

    try:
        for root, _dirs, files in os.walk(DOWNLOADS_DIR):
            for fname in files:
                fpath = os.path.join(root, fname)
                if keep_abs and os.path.abspath(fpath) == keep_abs:
                    continue
                rel_path = os.path.relpath(fpath, DOWNLOADS_DIR)
                if _filename_matches_queue_item(rel_path, queue_item):
                    try:
                        age_seconds = time.time() - os.path.getmtime(fpath)
                        if age_seconds < _CLEANUP_SIBLING_MIN_AGE_SECONDS:
                            logger.debug(
                                f"[CLEANUP] Skipping recent file ({age_seconds:.0f}s old, "
                                f"min={_CLEANUP_SIBLING_MIN_AGE_SECONDS}s): {fpath}"
                            )
                            continue
                        os.remove(fpath)
                        logger.info(f"[CLEANUP] Removed duplicate download: {fpath}")
                        # Remove now-empty parent directories up to DOWNLOADS_DIR
                        try:
                            parent = os.path.dirname(fpath)
                            while parent != DOWNLOADS_DIR and os.path.isdir(parent) and not os.listdir(parent):
                                os.rmdir(parent)
                                parent = os.path.dirname(parent)
                        except OSError:
                            pass
                    except OSError as e:
                        logger.warning(f"[CLEANUP] Could not remove {fpath}: {e}")
    except Exception as e:
        logger.warning(f"[CLEANUP] Error during sibling cleanup: {e}")


def _delete_mismatched_download(file_path, item_id, reason):
    """Delete a downloaded file that does not match its queue item.

    Removes the file from disk, cleans up any now-empty parent directories
    up to DOWNLOADS_DIR, and logs the action.  Returns True on success.
    """
    if not file_path:
        return False
    try:
        if not os.path.isfile(file_path):
            logger.debug(f"Queue {item_id}: mismatched file already gone: {file_path}")
            return False
        os.remove(file_path)
        logger.info(
            f"Queue {item_id}: [MISMATCH-DELETE] deleted mismatched file "
            f"({reason}): {file_path}"
        )
        # Remove now-empty parent directories up to DOWNLOADS_DIR.
        try:
            parent = os.path.dirname(file_path)
            while parent != DOWNLOADS_DIR and os.path.isdir(parent) and not os.listdir(parent):
                os.rmdir(parent)
                parent = os.path.dirname(parent)
        except OSError:
            pass
        return True
    except OSError as exc:
        logger.warning(f"Queue {item_id}: could not delete mismatched file {file_path}: {exc}")
        return False


def _file_matches_queue_item(file_path, queue_item, relative_name=None):
    """Match a file to queue metadata, preferring tags and duration over filename alone."""
    metadata_state = _metadata_matches_queue_item(file_path, queue_item)
    if metadata_state is False:
        return False, 'metadata'

    candidate_name = relative_name or os.path.basename(file_path)
    if metadata_state is True:
        return True, 'metadata'

    if _filename_matches_queue_item(candidate_name, queue_item):
        return True, 'filename'

    return False, 'filename'

def get_db():
    """Get database connection — PostgreSQL only. Fails fast if unavailable."""
    from app import get_db as app_get_db
    return app_get_db()

def get_slskd_client():
    """Get configured SlskdClient instance"""
    try:
        import yaml
        
        # Prefer explicit CONFIG_PATH, then try common defaults.
        config_path = os.environ.get("CONFIG_PATH", "").strip()
        if not config_path:
            config_path = "/config/config.yml"
            if not os.path.exists(config_path):
                config_path = "/config/config.yaml"
        
        if not os.path.exists(config_path):
            logger.error(f"Config file not found (tried config.yml and config.yaml)")
            return None
        
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f) or {}
        
        slskd_config = config.get("slskd", {})
        
        if not slskd_config.get("enabled"):
            logger.warning("Soulseek (slskd) is not enabled in config")
            return None
        
        from api_clients.slskd import SlskdClient
        
        web_url = slskd_config.get("web_url", "http://localhost:5030")
        api_key = slskd_config.get("api_key", "")
        
        return SlskdClient(web_url, api_key, enabled=True)
        
    except Exception as e:
        logger.error(f"Error getting SlskdClient: {e}")
        return None

def cleanup_stuck_searching_items():
    """Detect and mark as failed any items stuck in 'searching' for too long"""
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        placeholder = _get_placeholder(conn)
        
        # Items stuck in 'searching' for more than 300 seconds are likely hung.
        # _SLSKD_SEARCH_MAX_WAIT_SECONDS (line 104) is 150 s, so legitimate
        # searches can stay in 'searching' for up to ~2.5 min.  Use 300 s
        # (5 min, i.e. 2× the max search wait) to give ample margin before
        # declaring an item truly stuck (e.g. after a crash left the status
        # unreset).
        stuck_threshold = (datetime.now() - timedelta(seconds=2 * _SLSKD_SEARCH_MAX_WAIT_SECONDS)).isoformat()
        
        cursor.execute("""
            SELECT id, artist, title, updated_at FROM download_queue
            WHERE status = 'searching'
            AND updated_at < {placeholder}
        """.format(placeholder=placeholder), (stuck_threshold,))
        
        stuck_items = cursor.fetchall()
        
        if stuck_items:
            logger.warning(f"Found {len(stuck_items)} items stuck in 'searching' status, marking for retry...")
            
            for item in stuck_items:
                item_id = item['id']
                logger.warning(
                    f"Queue {item_id}: Detected stuck search ({item['artist']} - {item['title']}, "
                    f"updated at {item['updated_at']}), marking for retry..."
                )
                mark_failed(
                    item_id,
                    "Stuck in searching state (likely slskd unresponsive)",
                    schedule_retry=True,
                    retry_delay_minutes=15
                )
        
        return len(stuck_items)
        
    except Exception as e:
        logger.error(f"Error cleaning up stuck searching items: {e}")
        return 0
    finally:
        if conn:
            conn.close()


def cleanup_stuck_moving_items():
    """Detect and recover any items stuck in 'moving' for too long.

    If a process crashes while a queue item is in the atomic 'moving' state,
    the item stays stuck forever — invisible to deduplication and never
    retried.  This function resets such items back to 'completed' so the
    auto-move logic can attempt the move again on the next pass.

    Items are considered stuck when they have been in 'moving' status for
    more than 10 minutes.
    """
    from download_queue_manager import _release_move_claim
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        placeholder = _get_placeholder(conn)

        stuck_threshold = (datetime.now() - timedelta(minutes=10)).isoformat()

        cursor.execute(
            """
            SELECT id, artist, title, file_path, updated_at FROM download_queue
            WHERE status = 'moving'
            AND updated_at < {placeholder}
            """.format(placeholder=placeholder),
            (stuck_threshold,),
        )
        stuck_items = cursor.fetchall()

        if stuck_items:
            logger.warning(
                f"Found {len(stuck_items)} item(s) stuck in 'moving' status, restoring to 'completed'..."
            )
            for item in stuck_items:
                item_id = item['id'] if isinstance(item, dict) else item[0]
                item_file_path = (item.get('file_path') if isinstance(item, dict) else (item[3] if len(item) > 3 else None))
                logger.warning(
                    f"Queue {item_id}: Detected stuck 'moving' state "
                    f"({(item.get('artist') if isinstance(item, dict) else item[1])} - "
                    f"{(item.get('title') if isinstance(item, dict) else item[2])}), "
                    f"restoring to {'completed' if item_file_path else 'failed'} for retry"
                )
                try:
                    if item_file_path:
                        _release_move_claim(item_id, restore_status='completed', file_path=item_file_path)
                    else:
                        # No file_path — cannot restore to 'completed' (guardrail in update_queue_item
                        # would block it).  Mark as 'failed' instead so the item exits the stuck
                        # 'moving' state and gets rescheduled for a fresh download.
                        _release_move_claim(item_id, restore_status='failed')
                except Exception as release_err:
                    logger.error(
                        f"Queue {item_id}: Could not release stuck 'moving' claim: {release_err}"
                    )

        return len(stuck_items)

    except Exception as e:
        logger.error(f"Error cleaning up stuck moving items: {e}")
        return 0
    finally:
        if conn:
            conn.close()

def get_queued_items(limit=None):
    """Get items ready to process (queued or scheduled for retry)"""
    conn = None
    try:
        # First, clean up any items stuck in 'searching' or 'moving' state
        cleanup_stuck_searching_items()
        cleanup_stuck_moving_items()
        
        conn = get_db()
        cursor = conn.cursor()
        placeholder = _get_placeholder(conn)
        
        now = datetime.now().isoformat()

        # Legacy queued qBittorrent rows should still be dispatched through
        # Soulseek. qBittorrent downloads are handled manually outside the queue
        # worker, so normalize those rows before selection.
        try:
            cursor.execute(
                """
                UPDATE download_queue
                SET source = 'soulseek', updated_at = CURRENT_TIMESTAMP
                                WHERE TRIM(LOWER(COALESCE(status, ''))) = 'queued'
                  AND (next_retry_at IS NULL OR next_retry_at <= {placeholder})
                                    AND TRIM(LOWER(COALESCE(source, 'soulseek'))) = 'qbittorrent'
                """.format(placeholder=placeholder),
                (now,),
            )
            promoted_count = int(cursor.rowcount or 0)
            if promoted_count > 0:
                conn.commit()
                logger.info(
                    "Queue selector promoted %s queued qBittorrent item(s) to Soulseek dispatch",
                    promoted_count,
                )
            else:
                conn.rollback()
        except Exception as promote_err:
            try:
                conn.rollback()
            except Exception:
                pass
            logger.debug(f"Could not promote queued qBittorrent rows to Soulseek: {promote_err}")

        # Surface items intentionally excluded from slskd processing so
        # operators can distinguish "not dispatched" from API failures.
        try:
            cursor.execute(
                """
                SELECT COALESCE(LOWER(source), 'soulseek') AS source_key, COUNT(*) AS item_count
                FROM download_queue
                                WHERE TRIM(LOWER(COALESCE(status, ''))) = 'queued'
                  AND (next_retry_at IS NULL OR next_retry_at <= {placeholder})
                                    AND TRIM(LOWER(COALESCE(source, 'soulseek'))) IN ('local', 'discovered')
                                GROUP BY COALESCE(LOWER(source), 'soulseek')
                ORDER BY source_key
                """.format(placeholder=placeholder),
                (now,),
            )
            excluded_rows = cursor.fetchall() or []
            if excluded_rows:
                summary_parts = []
                total_excluded = 0
                for row in excluded_rows:
                    source_key = row.get('source_key') if hasattr(row, 'get') else row[0]
                    item_count = row.get('item_count') if hasattr(row, 'get') else row[1]
                    item_count = int(item_count or 0)
                    total_excluded += item_count
                    summary_parts.append(f"{source_key}:{item_count}")
                logger.info(
                    "Queue selector skipped %s queued item(s) due to non-slskd source (%s)",
                    total_excluded,
                    ", ".join(summary_parts),
                )
        except Exception as source_diag_err:
            logger.debug(f"Could not compute queue source skip diagnostics: {source_diag_err}")
        
        # Get queued items and items scheduled for retry
        base_query = """
            SELECT * FROM download_queue
            WHERE TRIM(LOWER(COALESCE(status, ''))) = 'queued'
            AND (next_retry_at IS NULL OR next_retry_at <= {placeholder})
            AND TRIM(LOWER(COALESCE(source, 'soulseek'))) NOT IN ('local', 'discovered')
            ORDER BY priority ASC, retry_count ASC, next_retry_at ASC, created_at ASC
        """.format(placeholder=placeholder)

        if isinstance(limit, int) and limit > 0:
            cursor.execute(base_query + f"\nLIMIT {placeholder}", (now, limit))
        else:
            cursor.execute(base_query, (now,))
        
        items = [dict(row) for row in cursor.fetchall()]
        
        return items
        
    except Exception as e:
        logger.error(f"Error getting queued items: {e}")
        return []
    finally:
        if conn:
            conn.close()

def update_queue_status(queue_id, status, **kwargs):
    """Update queue item status"""
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        placeholder = _get_placeholder(conn)
        
        updates = [f"status = {placeholder}"]
        params = [status]
        
        # Add any additional fields to update
        for key, value in kwargs.items():
            if key in ['found_filename', 'file_path', 'failure_reason', 'retry_count', 
                       'last_failure_time', 'source_id']:
                updates.append(f"{key} = {placeholder}")
                params.append(value)
        
        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.append(queue_id)
        
        query = f"UPDATE download_queue SET {', '.join(updates)} WHERE id = {placeholder}"
        cursor.execute(query, params)
        conn.commit()
        
        logger.info(f"Updated queue {queue_id} to status: {status}")
        return True
        
    except Exception as e:
        logger.error(f"Error updating queue status: {e}")
        return False
    finally:
        if conn:
            conn.close()

def increment_retry_count(queue_id, retry_delay_minutes=60):
    """Increment retry count and schedule next retry"""
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        placeholder = _get_placeholder(conn)
        
        # Get current retry count
        cursor.execute(f"""
            SELECT retry_count FROM download_queue WHERE id = {placeholder}
        """, (queue_id,))
        
        row = cursor.fetchone()
        if not row:
            return False
        
        retry_count = (row['retry_count'] or 0) + 1
        
        effective_retry_delay = max(int(retry_delay_minutes or 0), MIN_RETRY_DELAY_MINUTES)
        next_retry = datetime.now() + timedelta(minutes=effective_retry_delay)
        
        cursor.execute(f"""
            UPDATE download_queue 
            SET retry_count = {placeholder}, next_retry_at = {placeholder}, updated_at = CURRENT_TIMESTAMP
            WHERE id = {placeholder}
        """, (retry_count, next_retry.isoformat(), queue_id))
        
        conn.commit()
        
        logger.info(
            f"Queue {queue_id}: retry count now {retry_count}, "
            f"next retry at {next_retry} (delay={effective_retry_delay}m)"
        )
        return True
        
    except Exception as e:
        logger.error(f"Error incrementing retry count: {e}")
        return False
    finally:
        if conn:
            conn.close()

def mark_failed(queue_id, reason, schedule_retry=True, retry_delay_minutes=60):
    """Mark queue item as failed, optionally scheduling retry"""
    conn = None
    try:
        from app import get_db as app_get_db
        conn = app_get_db()
        cursor = conn.cursor()
        placeholder = "%s"
        
        # Get current retry count
        cursor.execute(f"SELECT retry_count FROM download_queue WHERE id = {placeholder}", (queue_id,))
        row = cursor.fetchone()
        
        if not row:
            return False
        
        retry_count = (row['retry_count'] or 0) + 1
        
        # Always schedule retry if requested - no max retry limit for Soulseek searches
        if schedule_retry:
            effective_retry_delay = max(int(retry_delay_minutes or 0), MIN_RETRY_DELAY_MINUTES)
            next_retry = datetime.now() + timedelta(minutes=effective_retry_delay)
            new_status = 'queued'
            logger.warning(
                f"Queue {queue_id}: Failed ({reason}), scheduling retry #{retry_count} at {next_retry} "
                f"(delay={effective_retry_delay}m)"
            )
        else:
            next_retry = None
            new_status = 'failed'
            logger.error(f"Queue {queue_id}: Failed permanently ({reason}) - retry not requested")
        
        cursor.execute(f"""
            UPDATE download_queue 
            SET status = {placeholder}, retry_count = {placeholder}, failure_reason = {placeholder}, last_failure_time = CURRENT_TIMESTAMP,
                next_retry_at = {placeholder}, updated_at = CURRENT_TIMESTAMP
            WHERE id = {placeholder}
        """, (new_status, retry_count, reason, next_retry.isoformat() if next_retry else None, queue_id))
        
        conn.commit()
        
        return schedule_retry  # Return whether retry was scheduled
        
    except Exception as e:
        logger.error(f"Error marking queue item as failed: {e}")
        return False
    finally:
        if conn:
            conn.close()

def _get_navidrome_config():
    """Load Navidrome credentials from config file, supporting both navidrome_users list and legacy navidrome block."""
    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
    if not os.path.exists(config_path):
        config_path = "/config/config.yml"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        # Prefer navidrome_users list (multi-user config)
        nav_users = cfg.get("navidrome_users") or []
        if isinstance(nav_users, list) and nav_users:
            first = nav_users[0]
            base_url = first.get("base_url", "").rstrip("/")
            username = first.get("user", "")
            password = first.get("pass", "")
            if base_url and username and password:
                return base_url, username, password
        # Fall back to legacy single navidrome block
        nav = cfg.get("navidrome") or {}
        base_url = nav.get("base_url", "").rstrip("/")
        username = nav.get("user", "") or nav.get("username", "")
        password = nav.get("pass", "") or nav.get("password", "")
        if base_url and username and password:
            return base_url, username, password
    except Exception as e:
        logger.debug(f"Could not read Navidrome config: {e}")
    return None, None, None


def _build_subsonic_auth_params(username, password):
    """Build Subsonic API auth params using token-based authentication."""
    salt = secrets.token_hex(8)
    token = hashlib.md5((password + salt).encode()).hexdigest()
    return {
        "u": username,
        "t": token,
        "s": salt,
        "v": "1.16.1",
        "c": "sptnr",
        "f": "json",
    }


def _trigger_navidrome_scan():
    """Fire a Navidrome library startScan via the Subsonic API.

    Called after a track is successfully auto-moved to /music so Navidrome
    immediately picks up the new file rather than waiting for its next
    scheduled full scan.

    Returns True if the scan was triggered successfully, False otherwise.
    """
    base_url, username, password = _get_navidrome_config()
    if not base_url:
        logger.debug("[NAVIDROME-SCAN] Navidrome not configured — skipping scan trigger")
        return False
    try:
        params = _build_subsonic_auth_params(username, password)
        url = f"{base_url}/rest/startScan"
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        if result.get("subsonic-response", {}).get("status") == "ok":
            logger.info("[NAVIDROME-SCAN] ✅ Navidrome library scan triggered")
            return True
        error = result.get("subsonic-response", {}).get("error", {}) or {}
        err_code = error.get("code") if isinstance(error, dict) else None
        err_msg = error.get("message") if isinstance(error, dict) else None
        logger.warning(
            f"[NAVIDROME-SCAN] Scan request returned non-ok status: code={err_code} message={err_msg}"
        )
        return False
    except Exception as e:
        logger.warning(f"[NAVIDROME-SCAN] Could not trigger Navidrome scan: {e}")
        return False


def check_track_exists_in_db(queue_item):
    """
    Check if a track matching the queue item already exists in the local tracks database.

    Returns:
        tuple: (exists: bool, reason: str)
    """
    artist = queue_item.get("artist", "")
    title = queue_item.get("title", "")
    album = queue_item.get("album")

    if not artist or not title:
        return False, ""

    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        placeholder = _get_placeholder(conn)

        # Only match tracks that have a real on-disk path.  Rows with
        # file_path IS NULL are incomplete imports, and rows matching
        # '__queued_for_download__%' are queue placeholders — both must be
        # excluded so pending downloads are not falsely detected as existing.
        if album:
            cursor.execute(
                f"""
                SELECT id FROM tracks
                WHERE LOWER(artist) = LOWER({placeholder})
                  AND LOWER(title) = LOWER({placeholder})
                  AND LOWER(album) = LOWER({placeholder})
                  AND file_path IS NOT NULL
                  AND file_path NOT LIKE '__queued_for_download__%'
                LIMIT 1
                """,
                (artist, title, album),
            )
        else:
            cursor.execute(
                f"""
                SELECT id FROM tracks
                WHERE LOWER(artist) = LOWER({placeholder})
                  AND LOWER(title) = LOWER({placeholder})
                  AND file_path IS NOT NULL
                  AND file_path NOT LIKE '__queued_for_download__%'
                LIMIT 1
                """,
                (artist, title),
            )

        row = cursor.fetchone()

        if row:
            track_id = row["id"] if hasattr(row, "keys") else (row[0] if row else None)
            reason = f"Track '{artist} - {title}' already exists in local database (track ID {track_id})"
            return True, reason

    except Exception as e:
        logger.debug(f"DB existence check error for '{artist} - {title}': {e}")
    finally:
        if conn:
            conn.close()

    return False, ""


def check_track_exists_in_navidrome(queue_item):
    """
    Check if a track matching the queue item already exists in Navidrome via Subsonic search3 API.

    Returns:
        tuple: (exists: bool, reason: str)
    """
    artist = queue_item.get("artist", "")
    title = queue_item.get("title", "")

    if not artist or not title:
        return False, ""

    base_url, username, password = _get_navidrome_config()
    if not base_url:
        logger.debug("Navidrome not configured — skipping Navidrome existence check")
        return False, ""

    try:
        auth_params = _build_subsonic_auth_params(username, password)
        search_params = dict(auth_params)
        search_params["query"] = f"{artist} {title}"
        search_params["songCount"] = 10
        search_params["albumCount"] = 0
        search_params["artistCount"] = 0

        response = requests.get(
            f"{base_url}/rest/search3.view",
            params=search_params,
            timeout=10,
        )
        response.raise_for_status()

        data = response.json()
        if data.get("subsonic-response", {}).get("status") != "ok":
            logger.debug(f"Navidrome search3 returned non-ok status for '{artist} - {title}'")
            return False, ""

        songs = data.get("subsonic-response", {}).get("searchResult3", {}).get("song", [])
        if not isinstance(songs, list):
            songs = [songs] if songs else []

        def _sim(a, b):
            if not a or not b:
                return 0.0
            return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()

        for song in songs:
            title_sim = _sim(song.get("title", ""), title)
            artist_sim = _sim(song.get("artist", ""), artist)
            if title_sim >= _NAV_TITLE_SIMILARITY_THRESHOLD and artist_sim >= _NAV_ARTIST_SIMILARITY_THRESHOLD:
                reason = (
                    f"Track '{artist} - {title}' already exists in Navidrome "
                    f"(matched: '{song.get('artist')} - {song.get('title')}', "
                    f"id={song.get('id')})"
                )
                return True, reason

    except Exception as e:
        logger.debug(f"Navidrome existence check error for '{artist} - {title}': {e}")

    return False, ""


def _build_bracketsanitized_query(queue_item):
    """Build a plain-text Soulseek search query with bracketed words stripped from the title.

    Soulseek's ``artist=X, title=Y`` structured query syntax returns few or no
    results in practice (slskd matches against raw filenames, not embedded tags).
    Plain-text keyword searches work much more reliably.  Bracket annotations
    such as "(Batman Forever Soundtrack)" or "(Radio Edit)" narrow the result
    set unnecessarily; stripping them lets Soulseek surface all files for the
    core title, and the candidate scorer then filters for the best match.

    Returns a ``"{artist} - {core_title}"`` string, or ``None`` when artist or
    title are unavailable, or when the stripped title is identical to the stored
    ``search_query`` (so we don't waste a duplicate search slot).
    """
    # Use track artist (consistent with the stored search_query).  If missing,
    # fall back to album_artist so the query is still useful.  Album-artist
    # fallbacks are handled separately by _build_fallback_search_queries.
    artist = (queue_item.get('artist') or queue_item.get('album_artist') or '').strip()
    title = (queue_item.get('title') or '').strip()
    if not artist or not title:
        return None

    stripped_title = _strip_brackets(title).strip()
    if not stripped_title:
        return None

    try:
        from download_queue_manager import _sanitize_search_query_for_slskd
    except ImportError:
        def _sanitize_search_query_for_slskd(q):  # type: ignore[misc]
            return " ".join(q.split())

    query = _sanitize_search_query_for_slskd(f"{artist} - {stripped_title}")

    # Skip when identical to the stored search_query – no point running the
    # same search twice.
    stored = (queue_item.get('search_query') or '').strip()
    if query == stored:
        return None

    return query or None


def _build_fallback_search_queries(queue_item, primary_query):
    """Build alternative search queries for tracks with featured artists.

    When the primary search uses the full track artist (e.g. "KNEECAP feat. Fawzi
    - Palestine"), files on Soulseek are often tagged with the album artist only
    ("KNEECAP").  This helper returns fallback queries to try when the primary
    search yields no usable match.

    Returns a list of ``(query, min_score)`` tuples.  ``min_score`` is the
    per-query acceptance threshold to use in place of ``_SLSKD_MIN_ACCEPT_SCORE``.
    Narrower queries (which contain the artist) use the normal threshold; broader
    queries (album-augmented title, bare title) require a higher score to
    compensate for their lower specificity.

    Query order:
    0. ``sanitized primary_query`` – apostrophes and quote characters removed
       from the stored query.  Handles legacy queue items where the
       ``search_query`` column was populated before sanitization was applied
       (e.g. via the ``fix_queue_search_queries`` utility) or any title that
       contains a straight apostrophe such as "Where's the Love".
    0b. ``depunctuated primary_query`` – all non-word punctuation stripped from
        the stored query.  Broader than (0): handles titles containing commas,
        exclamation marks, periods, etc. that cause Soulseek's tokenizer to
        return zero results.
    1. ``album_artist - title`` when album_artist differs from the track artist.
    2. ``feat.-stripped track_artist - title`` when the track artist contains a
       "feat." / "ft." / "featuring" clause.
    3. ``first_meaningful_word_of_artist - title`` for multi-word artists where
       Soulseek's all-tokens-required matching causes zero results (e.g.
       "The Pretty Reckless - Heaven Knows" → "Pretty - Heaven Knows").
       Common articles ("The", "A", "An") are skipped so they don't become
       the search token.  Soulseek requires every token in the query to appear
       in the filename, so a shorter artist prefix broadens the match while
       the candidate-scoring step still enforces artist similarity.
    3b. ``title album`` – title combined with the album name when known.
        Soulseek requires ALL query tokens to be present somewhere in the file
        path, so adding the album name forces results to come from the correct
        folder while keeping the query artist-agnostic.  This is more precise
        than a bare title search and is tried before it.  A moderately elevated
        score threshold (0.55) is applied since no artist token is present.
    4. ``title`` only, as a last resort for cases where the artist token(s) are
       entirely absent from shared filenames.  No artist anchor means this query
       is the broadest of all; a stricter score threshold (0.60) is enforced to
       reduce the risk of accepting a file that merely contains the title words
       somewhere in its path (e.g. a folder coincidentally named after the song).

    Already-tried queries (i.e. ``primary_query``) and plain duplicates are
    excluded from the returned list.
    """
    artist = str(queue_item.get('artist') or '').strip()
    album_artist = str(queue_item.get('album_artist') or '').strip()
    title = str(queue_item.get('title') or '').strip()
    album = str(queue_item.get('album') or '').strip()

    if not title:
        return []

    try:
        from download_queue_manager import (
            _sanitize_search_query_for_slskd,
            _strip_query_punctuation_for_slskd,
        )
    except ImportError:
        logger.warning(
            "_build_fallback_search_queries: could not import sanitization helpers "
            "from download_queue_manager; using plain whitespace normalisation as fallback"
        )
        def _sanitize_search_query_for_slskd(q):  # type: ignore[misc]
            return " ".join(q.split())
        def _strip_query_punctuation_for_slskd(q):  # type: ignore[misc]
            return " ".join(q.split())

    # Each entry is (query_string, min_accept_score).  None means use the global
    # _SLSKD_MIN_ACCEPT_SCORE default.
    fallbacks: list[tuple[str, float | None]] = []
    seen_queries: set[str] = set()

    def _add(q: str, min_score: float | None = None) -> None:
        """Append (q, min_score) to fallbacks if q is new and non-empty.

        min_score overrides _SLSKD_MIN_ACCEPT_SCORE for this query only.
        None means use the global default.
        """
        if q and q != primary_query and q not in seen_queries:
            seen_queries.add(q)
            fallbacks.append((q, min_score))

    # Fallback 0: sanitized primary – strips apostrophes/quotes from the stored
    # query.  This is the most targeted fix when the stored search_query still
    # contains a straight apostrophe (e.g. "Where's the Love") because it was
    # added before sanitization was applied or via a utility script.
    _add(_sanitize_search_query_for_slskd(primary_query))

    # Fallback 0b: fully depunctuated primary – strips ALL punctuation from the
    # stored query, not just quote characters.  Handles titles containing commas,
    # exclamation marks, periods, etc. ("Hello! World" → "Hello World").
    _add(_strip_query_punctuation_for_slskd(primary_query))

    # Fallback 0c: bracket-stripped artist + title.  Removes parenthesised /
    # square-bracketed annotations from the title (e.g. "(Batman Forever
    # Soundtrack)", "(Radio Edit)") so that Soulseek can match plain filenames
    # that don't include the annotation.  Results are still filtered by the
    # candidate scorer.  Tried for both the track artist and the album artist.
    core_title = _strip_brackets(title).strip()
    if core_title and core_title.lower() != title.lower():
        if artist:
            _add(_sanitize_search_query_for_slskd(f"{artist} - {core_title}"))
        if album_artist and album_artist.lower() != artist.lower():
            _add(_sanitize_search_query_for_slskd(f"{album_artist} - {core_title}"))
        # Also try just the core title without artist, with a stricter threshold.
        # No artist token means Soulseek may return files whose path merely
        # contains the title words; 0.60 (vs the default ~0.45) reduces the
        # risk of accepting a false-positive match.
        _add(_sanitize_search_query_for_slskd(core_title), min_score=0.60)

    # Fallback 1: album artist (e.g. "KNEECAP - Palestine")
    if album_artist and album_artist.lower() != artist.lower():
        _add(_sanitize_search_query_for_slskd(f"{album_artist} - {title}"))

    # Fallback 2: feat.-stripped track artist (e.g. "KNEECAP - Palestine")
    feat_stripped = _FEAT_SUFFIX_RE.sub("", artist).strip()
    if feat_stripped and feat_stripped.lower() != artist.lower():
        _add(_sanitize_search_query_for_slskd(f"{feat_stripped} - {title}"))

    # Fallback 3: first meaningful word of artist + title for multi-word artists.
    # Soulseek requires ALL query tokens to be present in a filename, so a
    # long artist name like "Pretty Reckless" can produce zero results when
    # "reckless" is absent from most shared filenames.  Using only the first
    # meaningful word (e.g. "Pretty") broadens the token set while the scorer
    # still validates the full artist name against the candidate filename.
    # If the first word is a common article ("The", "A", "An") it is skipped
    # so that "The Pretty Reckless" uses "Pretty" rather than "The".
    _ARTICLE_WORDS = {"the", "a", "an"}
    effective_artist = feat_stripped if feat_stripped else artist
    artist_words = effective_artist.split()
    # Find the first non-article word
    first_word = ""
    for _w in artist_words:
        if _w.lower() not in _ARTICLE_WORDS:
            first_word = _w
            break
    if not first_word and artist_words:
        first_word = artist_words[0]  # fallback: use first word even if it's an article
    if first_word and first_word.lower() != effective_artist.lower():
        _add(_sanitize_search_query_for_slskd(f"{first_word} - {title}"))

    # Fallback 3b: title + album.  Combining both forces Soulseek to return only
    # files whose path contains every album word, so results come from the right
    # folder even without an artist token.  Much more precise than a bare title
    # search when the album name is distinctive (e.g. "Days of Our Lives Innuendo"
    # only matches files in a folder named "Innuendo", not files from an artist
    # folder whose name happens to contain the same words as the title).
    # A minimum score of 0.55 is required because artist evidence is absent.
    if album:
        _add(_sanitize_search_query_for_slskd(f"{title} {album}"), min_score=0.55)

    # Fallback 4: title only.  Used when even a partial artist token blocks
    # results (e.g. the artist name is not present in any shared filename at
    # all).  No artist anchor means Soulseek may return files from folders whose
    # name simply contains the title words; a stricter score threshold (0.60)
    # is enforced to reduce the risk of false positives.
    _add(_sanitize_search_query_for_slskd(title), min_score=0.60)

    return fallbacks


def _run_soulseek_search(queue_id, query, queue_item, client):
    """Submit a single Soulseek search and collect the best-scoring candidate.

    Polls until slskd marks the search as complete (is_complete=True) or
    _SLSKD_SEARCH_MAX_WAIT_SECONDS is reached, whichever comes first.  Stopping
    as soon as is_complete=True means searches that run the full slskd timeout
    (state='Completed, TimedOut') still have their results evaluated rather than
    being abandoned mid-poll.

    Returns ``(best_result, best_score)`` where *best_result* is a dict with
    keys ``username``, ``filename``, ``size``, ``length``, and ``score``, or
    ``(None, 0.0)`` if no candidates were found or the search could not be
    started.
    """
    search_id = client.start_search(query)
    if not search_id:
        logger.warning(f"Queue {queue_id}: Failed to start search for '{query}'")
        return None, 0.0

    logger.info(
        "Queue %s: slskd search submitted (search_id=%s) for query '%s'",
        queue_id, search_id, query,
    )

    best_result = None
    best_score = 0.0
    poll_deadline = time.monotonic() + _SLSKD_SEARCH_MAX_WAIT_SECONDS
    poll_attempt = 0

    while time.monotonic() < poll_deadline:
        time.sleep(1)
        poll_attempt += 1
        try:
            responses, state, is_complete = client.get_search_results(search_id)
            elapsed = int(time.monotonic() - (poll_deadline - _SLSKD_SEARCH_MAX_WAIT_SECONDS))
            logger.debug(
                f"Queue {queue_id}: Poll {poll_attempt} (+{elapsed}s) - "
                f"Got {len(responses)} responses, state={state}"
            )

            if responses:
                for resp_idx, resp in enumerate(responses):
                    if not (hasattr(resp, 'files') and resp.files and len(resp.files) > 0):
                        logger.debug(
                            f"Queue {queue_id}: Response {resp_idx} from "
                            f"{getattr(resp, 'username', 'unknown')} has no files or empty files list"
                        )
                        continue

                    logger.debug(
                        f"Queue {queue_id}: Response {resp_idx} from {resp.username} "
                        f"has {len(resp.files)} files"
                    )
                    for file_info in resp.files:
                        filename = (
                            getattr(file_info, 'filename', file_info.get('filename', ''))
                            if isinstance(file_info, dict)
                            else getattr(file_info, 'filename', '')
                        )
                        # Skip candidates whose format is excluded by the configured
                        # quality filter (e.g. reject m4a when only flac/mp3 are
                        # listed as priorities with reject_others=True).
                        if not _candidate_extension_allowed(filename):
                            logger.debug(
                                f"Queue {queue_id}: Skipping {os.path.basename(filename)} "
                                f"— format not in configured priorities"
                            )
                            continue
                        size = (
                            getattr(file_info, 'size', file_info.get('size', 0))
                            if isinstance(file_info, dict)
                            else getattr(file_info, 'size', 0)
                        )
                        candidate_bitrate = (
                            file_info.get('bitRate', 0) or file_info.get('bitrate', 0)
                            if isinstance(file_info, dict)
                            else int(getattr(file_info, 'bitrate', 0) or 0)
                        )
                        candidate_length = _extract_candidate_length_seconds(file_info)
                        # Pre-filter by duration before running the full scorer.
                        # When both the expected and candidate durations are known,
                        # discard any file whose length falls outside the ±5s window
                        # so we never waste scorer CPU on clearly wrong tracks.
                        _expected_dur = _normalize_duration_seconds(queue_item.get('duration'))
                        if _expected_dur and candidate_length:
                            _dur_tol = _get_duration_match_tolerance(queue_item)
                            if abs(_expected_dur - candidate_length) > _dur_tol:
                                logger.debug(
                                    f"Queue {queue_id}: Skipping {os.path.basename(filename)} "
                                    f"— length {candidate_length:.0f}s outside "
                                    f"±{_dur_tol:.0f}s of expected {_expected_dur:.0f}s"
                                )
                                continue
                        candidate_score = _score_soulseek_candidate(filename, queue_item, candidate_length)
                        if candidate_score > best_score:
                            best_score = candidate_score
                            best_result = {
                                "username": resp.username,
                                "filename": filename,
                                "size": size,
                                "length": candidate_length,
                                "bitrate": candidate_bitrate,
                                "score": candidate_score,
                            }

                # Exit early once we have a high-confidence match.
                if best_result and best_score >= 0.72:
                    logger.info(
                        f"Queue {queue_id}: ✓ Found high-confidence match after {poll_attempt}s "
                        f"(score={best_score:.2f})"
                    )
                    break

            # Stop as soon as slskd says the search is finished (including
            # 'Completed, TimedOut').  All results are already in *responses*
            # at this point, so further polling is pointless.
            if is_complete:
                logger.info(
                    f"Queue {queue_id}: Search complete (state={state}) after {poll_attempt}s, "
                    f"stopping poll"
                )
                break

        except Exception as e:
            logger.warning(f"Queue {queue_id}: Error polling results (attempt {poll_attempt}): {e}")
            logger.debug(traceback.format_exc())

    return best_result, best_score


def search_and_download(queue_id, queue_item, client):
    """Search Soulseek for queue item and download top result"""
    try:
        search_query = queue_item['search_query']

        # Pre-download existence checks: skip download if the track already exists
        # in the local database or in Navidrome (catches items indexed there but not
        # yet scanned into the local DB).
        db_exists, db_reason = check_track_exists_in_db(queue_item)
        if db_exists:
            logger.info(f"Queue {queue_id}: ⏭️  Skipping download — {db_reason}")
            update_queue_status(queue_id, 'in_collection', failure_reason=db_reason)
            return False

        nav_exists, nav_reason = check_track_exists_in_navidrome(queue_item)
        if nav_exists:
            logger.info(f"Queue {queue_id}: ⏭️  Skipping download — {nav_reason}")
            update_queue_status(queue_id, 'in_collection', failure_reason=nav_reason)
            return False

        logger.info(f"Queue {queue_id}: Searching for '{search_query}'...")
        update_queue_status(queue_id, 'searching')

        poll_start_time = datetime.now()

        # Try a bracket-stripped plain-text query first.  Searching with the
        # core title (brackets removed) produces more results than the stored
        # search_query when the title contains annotations like
        # "(Batman Forever Soundtrack)" that are absent from shared filenames.
        # Raw Soulseek results are then filtered by _score_soulseek_candidate
        # which validates artist, title, and duration against the queue item.
        best_result = None
        best_score = 0.0
        stripped_query = _build_bracketsanitized_query(queue_item)
        if stripped_query:
            logger.info(
                f"Queue {queue_id}: Trying bracket-stripped query '{stripped_query}'..."
            )
            best_result, best_score = _run_soulseek_search(
                queue_id, stripped_query, queue_item, client
            )
            if best_result and best_score >= _SLSKD_MIN_ACCEPT_SCORE:
                logger.info(
                    f"Queue {queue_id}: Bracket-stripped query succeeded (score={best_score:.2f})"
                )

        # Primary plain-text search using the stored search_query when the
        # bracket-stripped query returned nothing useful.
        if not best_result or best_score < _SLSKD_MIN_ACCEPT_SCORE:
            plain_result, plain_score = _run_soulseek_search(
                queue_id, search_query, queue_item, client
            )
            if plain_score > best_score:
                best_result = plain_result
                best_score = plain_score

        # If the primary search found nothing useful, retry with fallback queries.
        # This handles tracks where the track artist contains featured guests
        # (e.g. "KNEECAP feat. Fawzi - Palestine") but Soulseek files are tagged
        # with the album artist only ("KNEECAP - Palestine").
        # _build_fallback_search_queries returns (query, min_score) tuples;
        # min_score overrides _SLSKD_MIN_ACCEPT_SCORE for broader queries.
        if not best_result or best_score < _SLSKD_MIN_ACCEPT_SCORE:
            for fallback_query, fb_min_score in _build_fallback_search_queries(queue_item, search_query):
                effective_min = fb_min_score if fb_min_score is not None else _SLSKD_MIN_ACCEPT_SCORE
                logger.info(
                    f"Queue {queue_id}: Primary search insufficient "
                    f"(score={best_score:.2f}), trying fallback query '{fallback_query}'"
                    f" (min_score={effective_min:.2f})..."
                )
                fb_result, fb_score = _run_soulseek_search(queue_id, fallback_query, queue_item, client)
                if fb_score > best_score:
                    best_score = fb_score
                    best_result = fb_result
                if best_result and best_score >= effective_min:
                    logger.info(
                        f"Queue {queue_id}: Fallback query '{fallback_query}' "
                        f"succeeded (score={best_score:.2f})"
                    )
                    break

        if not best_result:
            elapsed = (datetime.now() - poll_start_time).total_seconds()
            logger.warning(f"Queue {queue_id}: ✗ No results found after {elapsed:.0f}s of polling")
            mark_failed(queue_id, f"No results found for '{search_query}'", schedule_retry=True, retry_delay_minutes=_SLSKD_LONG_RETRY_DELAY_MINUTES)
            return False

        if best_score < _SLSKD_MIN_ACCEPT_SCORE:
            elapsed = (datetime.now() - poll_start_time).total_seconds()
            logger.warning(
                f"Queue {queue_id}: ✗ Results found but no safe match for '{search_query}' "
                f"(best_score={best_score:.2f}, elapsed={elapsed:.0f}s)"
            )
            mark_failed(
                queue_id,
                f"No safe Soulseek match for '{search_query}' (best_score={best_score:.2f})",
                schedule_retry=True,
                retry_delay_minutes=_SLSKD_LONG_RETRY_DELAY_MINUTES,
            )
            return False

        # Download the result
        logger.info(
            f"Queue {queue_id}: Downloading '{best_result['filename']}' from "
            f"{best_result['username']} (score={best_score:.2f})..."
        )
        update_queue_status(queue_id, 'downloading', found_filename=best_result['filename'])

        success = client.download_file(best_result['username'], best_result['filename'], best_result['size'])

        if success:
            logger.info(f"Queue {queue_id}: Download queued successfully in slskd")
            logger.info(f"Queue {queue_id}: File will appear in {DOWNLOADS_DIR} when download completes")
            # Remove any earlier duplicate downloads for the same track so we
            # don't accumulate stale files from previous retry attempts.
            try:
                _cleanup_sibling_downloads(queue_item=queue_item, keep_path=None)
            except Exception as cleanup_err:
                logger.debug(f"Queue {queue_id}: Sibling cleanup skipped: {cleanup_err}")
            # Status already set to 'downloading' above
            return True
        else:
            logger.error(f"Queue {queue_id}: Failed to queue download in slskd")
            mark_failed(queue_id, "Failed to queue Soulseek download", schedule_retry=True, retry_delay_minutes=15)
            return False

    except Exception as e:
        logger.error(f"Queue {queue_id}: Error in search_and_download: {e}")
        logger.debug(traceback.format_exc())
        mark_failed(queue_id, f"Search error: {str(e)}", schedule_retry=True)
        return False

def check_completed_downloads():
    """Check for completed downloads and match them to queue items.

    Primary:  Query slskd's transfers API for entries in state 'Completed,
              Succeeded' — each carries a localFilePath that gives the exact
              on-disk location without a filesystem walk.
    Fallback: Walk DOWNLOADS_DIR for audio files when slskd is unavailable or
              returns no localFilePath.
    """
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()

        # ------------------------------------------------------------------
        # Build a lookup of slskd-completed files: filename → localFilePath
        # ------------------------------------------------------------------
        slskd_completed: dict[str, str] = {}
        slskd_active: dict[str, dict] = {}
        slskd_status_available = False

        def _normalize_transfer_key(value):
            if not value:
                return ""
            return str(value).replace('\\', '/').strip().lower()

        def _get_transfer_entry(found_filename):
            if not found_filename:
                return None
            key = _normalize_transfer_key(found_filename)
            if not key:
                return None
            basename = os.path.basename(key)
            return slskd_active.get(key) or slskd_active.get(basename)

        def _is_stale_queue_item(item, stale_minutes=10):
            updated_at = item.get('updated_at')
            if not updated_at:
                return False
            try:
                updated_text = str(updated_at).replace('Z', '+00:00')
                updated_dt = datetime.fromisoformat(updated_text)
                # Normalise to a naive UTC datetime regardless of whether the DB
                # returned a timezone-aware or naive value.  Using utcnow() on
                # both sides avoids the silent offset loss that occurs when
                # .replace(tzinfo=None) is called on an aware datetime.
                if updated_dt.tzinfo is not None:
                    updated_dt = updated_dt.astimezone(timezone.utc).replace(tzinfo=None)
                return (datetime.utcnow() - updated_dt).total_seconds() >= (stale_minutes * 60)
            except Exception:
                return False

        try:
            slskd_client = get_slskd_client()
            if slskd_client:
                for transfer in slskd_client.get_completed_transfers():
                    local = transfer.get("localFilePath", "")
                    remote = transfer.get("filename", "")
                    if local and os.path.isfile(local):
                        remote_norm = _normalize_transfer_key(remote)
                        if remote_norm:
                            slskd_completed[remote_norm] = local
                            slskd_completed[os.path.basename(remote_norm)] = local
                        slskd_completed[os.path.basename(local).lower()] = local
                logger.debug(f"slskd API: {len(slskd_completed)} completed transfer paths")

                # Fetch active transfers with an explicit status check so we can
                # distinguish a true empty queue from an API failure.
                try:
                    active_list = slskd_client.get_active_downloads()
                    for transfer in active_list:
                        filename = transfer.get("filename", "")
                        norm = _normalize_transfer_key(filename)
                        if norm:
                            slskd_active[norm] = transfer
                            slskd_active[os.path.basename(norm)] = transfer
                    slskd_status_available = True
                    logger.debug(f"slskd API: {len(active_list)} active transfer entries")
                except Exception as status_err:
                    logger.warning(
                        f"Could not fetch active slskd transfers for reconciliation: {status_err}"
                    )
        except Exception as slskd_err:
            logger.debug(f"Could not query slskd completed transfers: {slskd_err}")

        # ------------------------------------------------------------------
        # Filesystem walk (fallback / supplement)
        # ------------------------------------------------------------------
        # Determine which audio extensions to accept.  When the format filter
        # is enabled with reject_others=True only include extensions that match
        # a configured priority so that files already downloaded in a disallowed
        # format (e.g. m4a when only flac/mp3 are configured) are not
        # inadvertently matched to queue items.
        _all_audio_exts = ('.mp3', '.flac', '.m4a', '.ogg', '.wav', '.aac')
        try:
            from download_queue_manager import _load_format_bitrate_config as _dqm_load_fmt_cfg
            _fmt_cfg = _dqm_load_fmt_cfg()
        except Exception as exc:
            logger.debug(f"check_completed_downloads: could not load format config: {exc}")
            _fmt_cfg = {'enabled': False, 'priorities': [], 'reject_others': False}

        if _fmt_cfg.get('enabled') and _fmt_cfg.get('priorities') and _fmt_cfg.get('reject_others'):
            _allowed_exts = tuple(
                '.' + str(p.get('format', '')).lower()
                for p in _fmt_cfg['priorities']
                if isinstance(p, dict) and p.get('format')
            )
            # If no valid format strings were found in priorities fall back to
            # the full set so the walk still finds something (misconfiguration
            # should not silently suppress all completion checks).
            if not _allowed_exts:
                _allowed_exts = _all_audio_exts
        else:
            _allowed_exts = _all_audio_exts

        fs_files: list[str] = []
        if os.path.isdir(DOWNLOADS_DIR):
            try:
                for root, _, root_files in os.walk(DOWNLOADS_DIR):
                    for f in root_files:
                        if f.lower().endswith(_allowed_exts):
                            fs_files.append(os.path.relpath(os.path.join(root, f), DOWNLOADS_DIR))
                if fs_files:
                    logger.debug(f"Filesystem walk: {len(fs_files)} audio files in {DOWNLOADS_DIR}")
            except Exception as e:
                logger.error(f"Error scanning downloads folder: {e}")
        else:
            logger.warning(f"Downloads directory does not exist: {DOWNLOADS_DIR}")

        # ------------------------------------------------------------------
        # Build the set of files already claimed by non-downloading items
        # so the fuzzy scan never re-assigns a file that another queue item
        # already owns (e.g. a different track from the same album folder).
        # ------------------------------------------------------------------
        claimed_files: set[str] = set()
        try:
            cursor.execute("""
                SELECT found_filename FROM download_queue
                WHERE found_filename IS NOT NULL
                  AND found_filename <> ''
                  AND status NOT IN ('downloading', 'queued', 'failed', 'searching')
            """)
            # NOTE: statuses excluded above are those where found_filename may be
            # set but ownership has not yet been confirmed (still hunting/in-flight
            # or needs a retry).  All other statuses — including 'moving',
            # 'completed', and 'imported' — represent confirmed ownership and are
            # intentionally included in claimed_files so fuzzy matching cannot
            # reassign those files to a different queue item.
            for row in cursor.fetchall():
                fn = (row.get('found_filename') if isinstance(row, dict) else row[0]) or ''
                if fn:
                    claimed_files.add(fn.replace('\\', '/').strip())
                    claimed_files.add(os.path.basename(fn.replace('\\', '/').strip()))
        except Exception as ce:
            logger.debug(f"Could not pre-load claimed files: {ce}")

        # ------------------------------------------------------------------
        # Fetch all items currently in 'downloading' status
        # ------------------------------------------------------------------
        cursor.execute("""
            SELECT * FROM download_queue
            WHERE status = 'downloading'
        """)
        downloading = [dict(row) for row in cursor.fetchall()]
        conn.close()
        conn = None

        if downloading:
            logger.debug(f"Checking {len(downloading)} items in 'downloading' status")

        newly_completed = []
        scan_needed = False

        # Import move/verify helpers once, outside the per-item loop.
        try:
            from download_queue_manager import (
                _try_claim_for_move,
                _release_move_claim,
                move_single_track_to_music_dir,
                update_queue_item,
            )
            from download_file_verification import verify_file_in_music, mark_queue_item_moved
            _move_helpers_available = True
        except ImportError as _imp_err:
            logger.error(f"[AUTO_MOVE] Could not import move helpers: {_imp_err}")
            _move_helpers_available = False

        for item in downloading:
            match_found = None
            match_meta_state = None

            found_fn = item.get("found_filename") or ""
            item_id = item["id"]

            # 1. Exact match via slskd localFilePath (most reliable)
            if found_fn:
                found_norm = _normalize_transfer_key(found_fn)
                abs_path = slskd_completed.get(found_norm) or slskd_completed.get(os.path.basename(found_norm))
            else:
                abs_path = None

            if abs_path:
                candidate_rel = os.path.relpath(abs_path, DOWNLOADS_DIR)
                is_match, match_source = _file_matches_queue_item(abs_path, item, candidate_rel)
                if is_match:
                    match_found = candidate_rel
                    match_meta_state = match_source
                    logger.debug(f"Queue {item_id}: matched via slskd localFilePath: {abs_path}")
                else:
                    logger.info(
                        f"Queue {item_id}: rejecting slskd-completed file due to queue mismatch: {candidate_rel}"
                    )
                    # The file was downloaded specifically for this queue item but its
                    # content (metadata / duration) does not match what we expected.
                    # Delete it so the retry downloads a different source rather than
                    # looping indefinitely on the same mismatched file.
                    _delete_mismatched_download(
                        abs_path, item_id,
                        f"metadata mismatch for slskd-completed file ({match_source})"
                    )
                    mark_failed(
                        item_id,
                        f"Downloaded file did not match queue item ({match_source} mismatch); "
                        f"deleted and rescheduled",
                        schedule_retry=True,
                        retry_delay_minutes=MIN_RETRY_DELAY_MINUTES,
                    )
                    continue

            # 2. Exact filename match against filesystem files
            if match_found is None and found_fn:
                for rel_file in fs_files:
                    rel_norm = rel_file.replace('\\', '/')
                    found_norm = found_fn.replace('\\', '/')
                    if rel_norm == found_norm or os.path.basename(rel_norm) == os.path.basename(found_norm):
                        file_path = os.path.join(DOWNLOADS_DIR, rel_file)
                        is_match, match_source = _file_matches_queue_item(file_path, item, rel_file)
                        if not is_match:
                            logger.info(
                                f"Queue {item_id}: rejecting exact filename match due to queue mismatch: {rel_file}"
                            )
                            # Delete the mismatched file so the retry can fetch a
                            # better source instead of re-matching the same file.
                            _delete_mismatched_download(
                                file_path, item_id,
                                f"metadata mismatch for exact-filename match ({match_source})"
                            )
                            mark_failed(
                                item_id,
                                f"Downloaded file did not match queue item ({match_source} mismatch); "
                                f"deleted and rescheduled",
                                schedule_retry=True,
                                retry_delay_minutes=MIN_RETRY_DELAY_MINUTES,
                            )
                            break
                        match_found = rel_file
                        match_meta_state = match_source
                        break

            # 3. Fuzzy match against filesystem files
            if match_found is None:
                for filename in fs_files:
                    # Skip files already owned by another queue item.
                    fn_key = filename.replace('\\', '/').strip()
                    if fn_key in claimed_files or os.path.basename(fn_key) in claimed_files:
                        continue
                    file_path = os.path.join(DOWNLOADS_DIR, filename)
                    is_match, match_source = _file_matches_queue_item(file_path, item, filename)
                    if is_match:
                        match_found = filename
                        match_meta_state = match_source
                        # Register so later queue items in this cycle can't also claim it.
                        claimed_files.add(fn_key)
                        claimed_files.add(os.path.basename(fn_key))
                        logger.debug(f"Queue {item_id}: fuzzy match found: {filename}")
                        break

            # 4. No file match found. Reconcile against live slskd transfers so
            # stale 'downloading' rows do not remain stuck forever.
            if match_found is None:
                if slskd_status_available:
                    found_fn = item.get("found_filename") or ""
                    transfer = _get_transfer_entry(found_fn)

                    if transfer:
                        transfer_state = transfer.get("state", "")
                        if transfer_state in getattr(slskd_client, "FAILED_STATES", set()):
                            logger.warning(
                                f"Queue {item_id}: slskd reports terminal failed state {transfer_state!r}, scheduling retry"
                            )
                            mark_failed(
                                item_id,
                                f"slskd transfer failed: {transfer_state}",
                                schedule_retry=True,
                                retry_delay_minutes=10,
                            )
                        elif transfer_state == getattr(slskd_client, "STATE_SUCCEEDED", None):
                            # slskd reports success but no local file was found — the file
                            # was deleted before the queue processor could match it.
                            # Remove the stale "Completed, Succeeded" entry from slskd so
                            # that the next retry actually queues a fresh download instead
                            # of slskd seeing the old completed entry and skipping it.
                            logger.warning(
                                f"Queue {item_id}: slskd reports succeeded but no file found; "
                                f"removing stale transfer and scheduling retry"
                            )
                            try:
                                _stale_id = str(transfer.get("id") or "")
                                _stale_user = str(transfer.get("username") or "")
                                if _stale_id and _stale_user:
                                    slskd_client.cancel_download(
                                        _stale_user, _stale_id, remove=True
                                    )
                                    logger.debug(
                                        f"Queue {item_id}: removed stale completed transfer "
                                        f"{_stale_id} (user={_stale_user}) from slskd"
                                    )
                            except Exception as _cancel_err:
                                logger.debug(
                                    f"Queue {item_id}: could not remove stale transfer: {_cancel_err}"
                                )
                            mark_failed(
                                item_id,
                                "slskd transfer succeeded but local file not found",
                                schedule_retry=True,
                                retry_delay_minutes=10,
                            )
                        # Active/unknown transfer states are left untouched —
                        # the download may still be in progress.  Skip to the
                        # next item and let it be re-evaluated next cycle.
                        continue

                    # Transfer no longer exists in slskd. If the item has been
                    # stale for a while and no file is present, queue it for retry.
                    if _is_stale_queue_item(item, stale_minutes=10):
                        logger.warning(
                            f"Queue {item_id}: missing from slskd transfers and stale in downloading state; scheduling retry"
                        )
                        mark_failed(
                            item_id,
                            "Transfer missing from slskd API while marked downloading",
                            schedule_retry=True,
                            retry_delay_minutes=10,
                        )

                elif _is_stale_queue_item(item, stale_minutes=10):
                    # slskd API was unavailable but the item has been stuck in
                    # 'downloading' for too long with no file present.  Re-queue
                    # so it can be retried once slskd becomes reachable again.
                    logger.warning(
                        f"Queue {item_id}: no file found and slskd unavailable; item stale in downloading state, scheduling retry"
                    )
                    mark_failed(
                        item_id,
                        "No file found and slskd unavailable while marked downloading",
                        schedule_retry=True,
                        retry_delay_minutes=15,
                    )
                    continue

            if match_found:
                file_path = os.path.join(DOWNLOADS_DIR, match_found)
                if match_meta_state == 'metadata':
                    logger.info(
                        f"Queue {item_id}: matched file '{match_found}' by metadata — claiming for move"
                    )
                else:
                    logger.info(
                        f"Queue {item_id}: matched file '{match_found}' by filename/path — claiming for move"
                    )

                # For filename-only matches, the file's embedded metadata was either
                # absent or incomplete when _metadata_matches_queue_item ran.  Do a
                # secondary check: if the file now has readable artist+title tags and
                # those tags clearly contradict the queue item, reject and delete the
                # file rather than importing wrong content.
                if match_meta_state != 'metadata':
                    _sec_meta = _metadata_matches_queue_item(file_path, item)
                    if _sec_meta is False:
                        logger.warning(
                            f"Queue {item_id}: ✗ secondary metadata check FAILED on filename-only match "
                            f"'{match_found}' — file tags do not match queue item; "
                            f"deleting and rescheduling"
                        )
                        _delete_mismatched_download(
                            file_path, item_id,
                            "secondary metadata check failed on filename-only match"
                        )
                        mark_failed(
                            item_id,
                            "File tags do not match queue item (secondary check after filename match); "
                            "deleted and rescheduled",
                            schedule_retry=True,
                            retry_delay_minutes=MIN_RETRY_DELAY_MINUTES,
                        )
                        continue

                # Pre-copy duration validation: confirm the actual file duration
                # matches the queue item's expected duration before moving to the
                # collection.  This catches cases where the search selected the
                # right filename but the audio content is a wrong version (e.g. a
                # 9:35 medley downloaded for a 3:40 remastered LP track).
                # The check is only applied when the queue item carries an
                # expected duration; items without one are left through because we
                # have no reliable reference to compare against.
                _expected_dur = _normalize_duration_seconds(item.get('duration'))
                if _expected_dur:
                    _actual_dur = _extract_audio_file_duration_seconds(file_path)
                    if _actual_dur:
                        _dur_diff = abs(_expected_dur - _actual_dur)
                        _dur_tolerance = _get_duration_match_tolerance(item)
                        if _dur_diff > _dur_tolerance:
                            logger.warning(
                                f"Queue {item_id}: ✗ pre-copy duration check FAILED — "
                                f"expected {_expected_dur}s, file is {_actual_dur}s "
                                f"(diff={_dur_diff}s, tolerance={_dur_tolerance}s); "
                                f"deleting '{match_found}' and scheduling retry"
                            )
                            _delete_mismatched_download(
                                file_path, item_id,
                                f"duration mismatch: expected {_expected_dur}s, got {_actual_dur}s"
                            )
                            mark_failed(
                                item_id,
                                f"Pre-copy duration mismatch: expected {_expected_dur}s, "
                                f"got {_actual_dur}s (diff={_dur_diff}s); deleted and rescheduled",
                                schedule_retry=True,
                                retry_delay_minutes=_SLSKD_LONG_RETRY_DELAY_MINUTES,
                            )
                            continue
                        logger.debug(
                            f"Queue {item_id}: pre-copy duration OK "
                            f"(expected {_expected_dur}s, file {_actual_dur}s, diff={_dur_diff}s)"
                        )
                    else:
                        logger.debug(
                            f"Queue {item_id}: pre-copy duration check skipped — "
                            f"could not read duration from '{match_found}'"
                        )

                if not _move_helpers_available:
                    logger.error(f"Queue {item_id}: move helpers unavailable, skipping auto-move")
                    update_queue_status(item_id, 'completed', file_path=file_path, found_filename=match_found)
                    newly_completed.append(item)
                    continue

                # Atomically transition from 'downloading' → 'moving' so that a
                # concurrent caller (e.g. UI button) cannot also attempt to move
                # this file.  If the claim fails the other caller already owns
                # it; skip this item to avoid a double-move.
                claimed = _try_claim_for_move(item_id, 'downloading')
                if not claimed:
                    logger.info(
                        f"Queue {item_id}: move already claimed by another process — skipping"
                    )
                    continue

                # Also persist found_filename now that we've claimed the item.
                update_queue_item(item_id, found_filename=match_found, file_path=file_path)

                # Immediately move the file to /music
                try:
                    # Extract duration from the downloaded file and persist it when the
                    # queue item has no duration yet (e.g. it was added without MusicBrainz
                    # metadata). MutagenFile may be None when mutagen is not installed.
                    if not item.get('duration') and MutagenFile is not None:
                        try:
                            audio = MutagenFile(file_path)
                            if audio is not None and audio.info and hasattr(audio.info, 'length'):
                                file_duration = _normalize_duration_seconds(audio.info.length)
                                if file_duration:
                                    update_queue_item(item_id, duration=file_duration)
                                    logger.debug(
                                        f"Queue {item_id}: updated duration from file to {file_duration}s"
                                    )
                        except Exception as dur_err:
                            logger.debug(f"Queue {item_id}: could not extract duration from file: {dur_err}")

                    item_for_move = dict(item)
                    item_for_move['file_path'] = file_path
                    move_result = move_single_track_to_music_dir(item_for_move)
                    if move_result.get('success'):
                        target_path = move_result.get('target_path')
                        verify_result = verify_file_in_music(item_id, target_path)
                        if verify_result['success']:
                            mark_queue_item_moved(item_id, target_path)
                            update_queue_item(
                                item_id,
                                status='imported',
                                music_file_path=target_path,
                                copied_individually=1,
                                copied_individually_at=datetime.now().isoformat()
                            )
                            logger.info(f"[AUTO_MOVE] Queue {item_id}: verified and imported to {target_path}")
                            scan_needed = True
                            newly_completed.append(item)
                        else:
                            # Verification failed: the file could not be confirmed at the
                            # target path.  Release the move claim so the next processor
                            # cycle retries the transfer rather than leaving the item in a
                            # permanently-inconsistent 'imported' state pointing at a file
                            # that may not exist.
                            logger.error(
                                f"[AUTO_MOVE] Queue {item_id}: verification FAILED "
                                f"({verify_result.get('error')}), releasing claim for retry"
                            )
                            _release_move_claim(item_id, restore_status='completed', file_path=file_path)
                    else:
                        logger.warning(
                            f"[AUTO_MOVE] Queue {item_id}: could not move "
                            f"({move_result.get('error')}), releasing claim back to 'completed'"
                        )
                        _release_move_claim(item_id, restore_status='completed', file_path=file_path)
                except Exception as move_err:
                    logger.warning(f"[AUTO_MOVE] Queue {item_id}: move error: {move_err}")
                    try:
                        _release_move_claim(item_id, restore_status='completed', file_path=file_path)
                    except Exception:
                        pass

        for item in newly_completed:
            # Only attempt the album-completion check when there is enough context
            # to query meaningfully; without a release_id or album name the call
            # would do a full-table scan and return nothing useful.
            if not (item.get('release_id') or item.get('album')):
                continue
            try:
                from download_queue_manager import auto_move_completed_album
                result = auto_move_completed_album(
                    release_id=item.get('release_id'),
                    artist=item.get('artist'),
                    album=item.get('album')
                )
                if result.get('album_complete'):
                    logger.info(
                        f"[AUTO_MOVE] Album complete after download: "
                        f"{item.get('artist')} – {item.get('album')} | "
                        f"moved={result['moved']}, already_copied={result['already_copied']}"
                    )
            except Exception as auto_err:
                logger.warning(f"[AUTO_MOVE] Error triggering auto-move for queue {item['id']}: {auto_err}")

        if scan_needed:
            if not _trigger_navidrome_scan():
                logger.warning("[NAVIDROME-SCAN] Imports occurred but scan trigger failed — safety-net will retry")

    except Exception as e:
        logger.error(f"Error checking completed downloads: {e}")
    finally:
        if conn:
            conn.close()

def matches_queue_item(filename, queue_item, file_path=None):
    """Conservative filename/path fallback matcher when metadata is unavailable."""
    try:
        candidate_duration = _extract_audio_file_duration_seconds(file_path) if file_path else None
        score = _score_soulseek_candidate(filename, queue_item, candidate_duration=candidate_duration)
        return score >= 0.60
        
    except Exception as e:
        logger.error(f"Error matching filename: {e}")
        return False

def process_queue(client):
    """Process all currently eligible queued items for this cycle."""
    try:
        items = get_queued_items()
        
        if not items:
            logger.debug("No queued items to process")
        else:
            logger.info(f"Processing {len(items)} queue items...")
        
        processed = 0
        for item in items:
            if not client:
                logger.error("SlskdClient not available, skipping")
                break
            
            try:
                if search_and_download(item['id'], item, client):
                    processed += 1
            except Exception as e:
                logger.error(f"Error processing queue {item['id']}: {e}")
                mark_failed(item['id'], f"Processing error: {str(e)}", schedule_retry=True)
        
        # Always check for completed downloads, even if no new items were processed
        # This ensures downloads that complete between processing cycles are detected
        check_completed_downloads()
        
        # Process completed downloads with MusicBrainz/Discogs metadata
        try:
            from post_download_processor import process_pending_completed_items
            post_stats = process_pending_completed_items(limit=5)
            if post_stats.get('processed', 0) > 0:
                logger.info(f"Post-download processing: {post_stats['processed']} items organized")
        except Exception as e:
            logger.error(f"Error in post-download processing: {e}")
        
        return processed
        
    except Exception as e:
        logger.error(f"Error in process_queue: {e}")
        return 0


def _load_auto_discovery_settings():
    """Load persistent auto-discovery settings from config/env with safe defaults."""
    enabled = True
    interval_seconds = 60

    # Optional env overrides for quick control.
    env_enabled = os.environ.get("DOWNLOADS_AUTO_DISCOVER_ENABLED")
    env_interval = os.environ.get("DOWNLOADS_AUTO_DISCOVER_INTERVAL_SECONDS")

    if env_enabled is not None:
        enabled = str(env_enabled).strip().lower() in {"1", "true", "yes", "on"}

    if env_interval:
        try:
            interval_seconds = int(env_interval)
        except ValueError:
            logger.warning("Invalid DOWNLOADS_AUTO_DISCOVER_INTERVAL_SECONDS='%s'", env_interval)

    # Config file settings override defaults when present.
    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
    try:
        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                cfg = yaml.safe_load(f) or {}

            features = cfg.get('features') or {}
            discovery_cfg = features.get('downloads_auto_discover') or {}

            if 'enabled' in discovery_cfg:
                enabled = bool(discovery_cfg.get('enabled'))
            if 'interval_seconds' in discovery_cfg:
                interval_seconds = int(discovery_cfg.get('interval_seconds') or interval_seconds)
    except Exception as e:
        logger.warning(f"Could not read auto-discovery settings: {e}")

    if interval_seconds < 15:
        interval_seconds = 15

    return enabled, interval_seconds


def auto_discover_files(now_ts, last_run_ts):
    """Run background auto-discovery on interval and return updated last-run timestamp."""
    enabled, interval_seconds = _load_auto_discovery_settings()
    if not enabled:
        return last_run_ts

    if last_run_ts is not None and (now_ts - last_run_ts) < interval_seconds:
        return last_run_ts

    try:
        from download_queue_manager import auto_discover_and_queue_files

        stats = auto_discover_and_queue_files()
        queued = int(stats.get('queued', 0) or 0)
        scanned = int(stats.get('scanned', 0) or 0)
        if queued > 0:
            logger.info(
                "[AUTO-DISCOVER] Added %s new files to queue (scanned=%s)",
                queued,
                scanned,
            )
        else:
            logger.debug("[AUTO-DISCOVER] No new files found (scanned=%s)", scanned)
    except Exception as e:
        logger.error(f"[AUTO-DISCOVER] Error during background discovery: {e}")

    return now_ts


def check_musicbrainz_files(now_ts, last_run_ts, interval_seconds=30):
    """
    Run MusicBrainz file matching on interval and return updated last-run timestamp.
    Checks for new files matching active releases every 30 seconds.
    """
    if last_run_ts is not None and (now_ts - last_run_ts) < interval_seconds:
        return last_run_ts

    try:
        from musicbrainz_file_matcher import get_matcher
        
        matcher = get_matcher()
        result = matcher.monitor_and_match()
        matched = result.get("matched", 0)
        
        if matched > 0:
            logger.info(f"[MB_FILE_MATCHER] Matched {matched} files to releases")
        else:
            logger.debug("[MB_FILE_MATCHER] No new matches found")
            
    except Exception as e:
        logger.error(f"[MB_FILE_MATCHER] Error during file matching: {e}")

    return now_ts


def finalize_musicbrainz_releases(now_ts, last_run_ts, interval_seconds=60):
    """
    Run MusicBrainz release finalization on interval and return updated last-run timestamp.
    Finalizes releases when all tracks are discovered (every 60 seconds).
    """
    if last_run_ts is not None and (now_ts - last_run_ts) < interval_seconds:
        return last_run_ts

    try:
        from musicbrainz_finalizer import get_finalizer
        
        finalizer = get_finalizer()
        result = finalizer.check_and_finalize_releases()
        finalized = result.get("finalized", 0)
        
        if finalized > 0:
            logger.info(f"[MB_FINALIZER] Finalized {finalized} releases")
        else:
            logger.debug("[MB_FINALIZER] No releases ready for finalization")
            
    except Exception as e:
        logger.error(f"[MB_FINALIZER] Error during release finalization: {e}")

    return now_ts


def check_missing_moved_files(now_ts, last_run_ts, interval_seconds=300):
    """
    Periodically check for files that were moved to /music but have since disappeared.
    Requeues them for retry. Runs every 5 minutes by default.
    
    Args:
        now_ts: Current timestamp
        last_run_ts: Timestamp of last run
        interval_seconds: Interval between checks (default 300 seconds = 5 minutes)
    
    Returns:
        Updated last-run timestamp
    """
    if last_run_ts is not None and (now_ts - last_run_ts) < interval_seconds:
        return last_run_ts

    try:
        from download_file_verification import check_missing_moved_files
        
        result = check_missing_moved_files(minutes_old=30)
        checked = result.get('checked', 0)
        found_missing = result.get('found_missing', 0)
        requeued = result.get('requeued', 0)
        
        if found_missing > 0:
            logger.warning(
                f"[FILE_VERIFY] File verification: checked {checked}, "
                f"found {found_missing} missing, requeued {requeued}"
            )
        else:
            logger.debug(f"[FILE_VERIFY] File verification: checked {checked}, all present")
            
    except Exception as e:
        logger.error(f"[FILE_VERIFY] Error during file verification check: {e}")

    return now_ts


def check_failed_slskd_downloads(now_ts, last_run_ts, interval_seconds=300):
    """
    Periodically query slskd for failed/stalled transfers, cancel them via the
    slskd API (remove=True), and mark the matching queue items for retry.
    Delegates to download_queue_manager.check_and_remove_failed_downloads().
    Runs every 5 minutes by default.

    Args:
        now_ts: Current timestamp
        last_run_ts: Timestamp of last run
        interval_seconds: Interval between checks (default 300 seconds = 5 minutes)

    Returns:
        Updated last-run timestamp
    """
    if last_run_ts is not None and (now_ts - last_run_ts) < interval_seconds:
        return last_run_ts

    try:
        from download_queue_manager import check_and_remove_failed_downloads
        stats = check_and_remove_failed_downloads()
        failed = stats.get('failed_detected', 0)
        retried = stats.get('retry_scheduled', 0)
        if failed > 0:
            logger.info(
                f"[SLSKD-RETRY] Detected {failed} failed transfer(s), scheduled {retried} for retry"
            )
        else:
            logger.debug("[SLSKD-RETRY] No failed transfers detected")
    except Exception as e:
        logger.error(f"[SLSKD-RETRY] Error checking failed slskd downloads: {e}")

    return now_ts


def clear_slskd_completed_downloads(now_ts, last_run_ts, interval_seconds=1800):
    """
    Periodically clear all terminal-state (completed/cancelled/errored) entries
    from slskd's transfer list using DELETE /transfers/downloads/all/completed.
    Prevents the slskd UI from accumulating stale completed entries.
    Runs every 30 minutes by default.

    Args:
        now_ts: Current timestamp
        last_run_ts: Timestamp of last run
        interval_seconds: Interval between clears (default 1800 seconds = 30 minutes)

    Returns:
        Updated last-run timestamp
    """
    if last_run_ts is not None and (now_ts - last_run_ts) < interval_seconds:
        return last_run_ts

    try:
        client = get_slskd_client()
        if client and client.enabled:
            cleared = client.clear_completed_downloads()
            if cleared:
                logger.info("[SLSKD-CLEAR] Cleared completed download entries from slskd")
            else:
                logger.debug("[SLSKD-CLEAR] clear_completed_downloads returned False (may be empty or unavailable)")
        else:
            logger.debug("[SLSKD-CLEAR] slskd client not available, skipping completed-transfer clear")
    except Exception as e:
        logger.error(f"[SLSKD-CLEAR] Error clearing slskd completed downloads: {e}")

    return now_ts


def cleanup_stale_downloads(now_ts, last_run_ts, interval_seconds=3600):
    """
    Periodically delete files in the downloads folder that are outside the 'torrents'
    subfolder and older than 24 hours.  Runs every hour by default as a safety net
    independent of the auto-discovery setting.

    Args:
        now_ts: Current timestamp
        last_run_ts: Timestamp of last run
        interval_seconds: Interval between checks (default 3600 seconds = 1 hour)

    Returns:
        Updated last-run timestamp
    """
    if last_run_ts is not None and (now_ts - last_run_ts) < interval_seconds:
        return last_run_ts

    try:
        from download_queue_manager import get_downloads_dir, cleanup_stale_non_torrent_downloads

        downloads_dir = get_downloads_dir()
        deleted = cleanup_stale_non_torrent_downloads(downloads_dir)
        if deleted > 0:
            logger.info(f"[STALE-CLEANUP] Deleted {deleted} stale non-torrent file(s)")
        else:
            logger.debug("[STALE-CLEANUP] No stale non-torrent files found")
    except Exception as e:
        logger.error(f"[STALE-CLEANUP] Error during stale downloads cleanup: {e}")

    return now_ts


def check_downloads_folder(now_ts, last_run_ts, interval_seconds=90):
    """Periodically call check_downloads_folder() from the background processor.

    This ensures that files already present in the downloads directory (e.g.
    manually placed files or files downloaded via a path not tracked by slskd)
    are automatically matched to queue items in 'queued'/'searching'/'downloading'
    status without requiring the user to open the queue status page.

    check_downloads_folder() has its own internal rate-limit cache
    (_DOWNLOADS_CHECK_MIN_INTERVAL_SECONDS = 60) so calling it more frequently
    than interval_seconds is harmless — it will return cached data.
    """
    if last_run_ts is not None and (now_ts - last_run_ts) < interval_seconds:
        return last_run_ts

    try:
        from download_queue_manager import check_downloads_folder as _dqm_check_downloads_folder
        completed = _dqm_check_downloads_folder()
        if completed:
            logger.info(
                "[DOWNLOADS-FOLDER] Matched %d file(s) to queue items",
                len(completed),
            )
        else:
            logger.debug("[DOWNLOADS-FOLDER] No new matches found")
    except Exception as e:
        logger.error(f"[DOWNLOADS-FOLDER] Error during downloads folder check: {e}")

    return now_ts


def trigger_navidrome_scan_for_new_imports(now_ts, last_run_ts, interval_seconds=300):
    """Periodic safety-net: trigger a Navidrome scan when new imports are detected.

    Runs every *interval_seconds* (default 5 min).  It looks for download_queue
    rows that were moved to the music directory since the previous check.  When
    any are found, _trigger_navidrome_scan() is called so Navidrome indexes the
    new files even if the per-item trigger in check_completed_downloads() was
    skipped (e.g. the file was imported via another path).

    Args:
        now_ts: Current timestamp (time.time())
        last_run_ts: Timestamp returned by the previous call, or None
        interval_seconds: Minimum seconds between checks (default 300)

    Returns:
        Updated last-run timestamp
    """
    if last_run_ts is not None and (now_ts - last_run_ts) < interval_seconds:
        return last_run_ts

    try:
        conn = get_db()
        cursor = conn.cursor()
        placeholder = _get_placeholder(conn)

        if last_run_ts is not None:
            cutoff = datetime.fromtimestamp(last_run_ts)
        else:
            cutoff = datetime.now() - timedelta(seconds=interval_seconds)

        cursor.execute(
            f"""
            SELECT COUNT(*) FROM download_queue
            WHERE status = 'imported'
              AND copied_individually_at >= {placeholder}
            """,
            (cutoff.isoformat(),),
        )
        row = cursor.fetchone()
        count = int(row[0] if row else 0)
        conn.close()

        if count > 0:
            logger.info(
                f"[NAVIDROME-SCAN] {count} import(s) detected since last check "
                f"— triggering safety-net scan"
            )
            _trigger_navidrome_scan()
        else:
            logger.debug("[NAVIDROME-SCAN] No new imports since last check, scan not needed")
    except Exception as e:
        logger.error(f"[NAVIDROME-SCAN] Error in trigger_navidrome_scan_for_new_imports: {e}")

    return now_ts


def retry_pending_completed_moves(now_ts, last_run_ts, interval_seconds=120):
    """Periodically sweep items stuck in 'completed' status and auto-move them.

    Items land in 'completed' with file_path set when they were matched to a
    downloaded file but the auto-move was deferred (e.g. the album was not yet
    fully downloaded at match time, or the move attempt was rolled back after a
    transient error).  This function re-evaluates all such items every
    *interval_seconds* and calls auto_move_completed_album() for any album that
    is now fully ready, or moves standalone tracks directly.

    Args:
        now_ts: Current timestamp (time.time())
        last_run_ts: Timestamp returned by the previous call, or None
        interval_seconds: Minimum seconds between sweeps (default 120)

    Returns:
        Updated last-run timestamp
    """
    if last_run_ts is not None and (now_ts - last_run_ts) < interval_seconds:
        return last_run_ts

    try:
        import psycopg2.extras
        from download_queue_manager import (
            _get_postgres_conn_from_app_or_fallback,
            auto_move_completed_album,
            move_single_track_to_music_dir,
            _try_claim_for_move,
            _release_move_claim,
            update_queue_item,
        )
        from download_file_verification import verify_file_in_music, mark_queue_item_moved

        conn = _get_postgres_conn_from_app_or_fallback()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Find completed items that have a file_path but have not yet been
        # moved to the music library (music_file_path is still unset).
        cursor.execute("""
            SELECT id, artist, title, album, release_mbid, release_id,
                   file_path, recording_mbid, release_source,
                   track_number, disc_number, year, album_artist
            FROM download_queue
            WHERE status = 'completed'
              AND file_path IS NOT NULL
              AND file_path != ''
              AND (music_file_path IS NULL OR music_file_path = '')
              AND NOT STARTS_WITH(file_path, %s)
            ORDER BY updated_at ASC
        """, (os.environ.get("MUSIC_ROOT", "/music").rstrip("/") + "/",))
        pending = [dict(row) for row in cursor.fetchall()]
        conn.close()
        conn = None

        if not pending:
            logger.debug("[RETRY_MOVES] No completed items pending move")
            return now_ts

        logger.info(
            f"[RETRY_MOVES] {len(pending)} completed item(s) pending move to music library"
        )

        albums_tried = set()
        moved_count = 0
        scan_needed = False

        for item in pending:
            release_mbid = (
                (item.get('release_mbid') or item.get('release_id') or '').strip()
            )

            if release_mbid:
                # Album-grouped move: auto_move_completed_album handles
                # sibling readiness check and batch move.
                if release_mbid in albums_tried:
                    continue
                albums_tried.add(release_mbid)
                try:
                    result = auto_move_completed_album(
                        release_id=release_mbid,
                        artist=item.get('artist'),
                        album=item.get('album'),
                    )
                    n_moved = result.get('moved', 0)
                    if n_moved > 0:
                        moved_count += n_moved
                        scan_needed = True
                        logger.info(
                            f"[RETRY_MOVES] Moved {n_moved} track(s): "
                            f"{item.get('artist')} – {item.get('album')}"
                        )
                    elif not result.get('album_complete'):
                        logger.debug(
                            f"[RETRY_MOVES] Album not yet fully matched — "
                            f"holding: {item.get('artist')} – {item.get('album')}"
                        )
                except Exception as alb_err:
                    logger.warning(
                        f"[RETRY_MOVES] Queue {item['id']}: album move error: {alb_err}"
                    )
            else:
                # Standalone track — attempt a direct move.
                file_path = item.get('file_path', '')
                if not file_path or not os.path.isfile(file_path):
                    logger.debug(
                        f"[RETRY_MOVES] Queue {item['id']}: file not found at {file_path!r}"
                    )
                    continue
                try:
                    claimed = _try_claim_for_move(item['id'], 'completed')
                    if not claimed:
                        logger.debug(
                            f"[RETRY_MOVES] Queue {item['id']}: already claimed, skipping"
                        )
                        continue
                    logger.info(
                        f"[RETRY_MOVES] Queue {item['id']}: claiming standalone track "
                        f"'{item.get('title')}' for move"
                    )
                    item_for_move = dict(item)
                    item_for_move['file_path'] = file_path
                    move_result = move_single_track_to_music_dir(item_for_move)
                    if move_result.get('success'):
                        target_path = move_result.get('target_path')
                        verify_result = verify_file_in_music(item['id'], target_path)
                        if verify_result.get('success') or (
                            target_path and os.path.isfile(target_path)
                        ):
                            mark_queue_item_moved(item['id'], target_path)
                            update_queue_item(
                                item['id'],
                                status='imported',
                                music_file_path=target_path,
                                copied_individually=1,
                                copied_individually_at=datetime.now().isoformat(),
                            )
                            logger.info(
                                f"[RETRY_MOVES] Queue {item['id']}: imported to {target_path}"
                            )
                            moved_count += 1
                            scan_needed = True
                        else:
                            logger.warning(
                                f"[RETRY_MOVES] Queue {item['id']}: move verification failed "
                                f"({verify_result.get('error')}), releasing claim"
                            )
                            _release_move_claim(
                                item['id'], restore_status='completed', file_path=file_path
                            )
                    else:
                        logger.warning(
                            f"[RETRY_MOVES] Queue {item['id']}: move failed "
                            f"({move_result.get('error')}), releasing claim"
                        )
                        _release_move_claim(
                            item['id'], restore_status='completed', file_path=file_path
                        )
                except Exception as mv_err:
                    logger.warning(
                        f"[RETRY_MOVES] Queue {item['id']}: error during move: {mv_err}"
                    )
                    try:
                        _release_move_claim(
                            item['id'], restore_status='completed', file_path=file_path
                        )
                    except Exception:
                        pass

        if moved_count > 0:
            logger.info(f"[RETRY_MOVES] Total moved this sweep: {moved_count} track(s)")

        if scan_needed:
            if not _trigger_navidrome_scan():
                logger.warning(
                    "[RETRY_MOVES] Imports occurred but Navidrome scan trigger failed "
                    "— safety-net will retry"
                )

    except Exception as e:
        logger.error(f"[RETRY_MOVES] Sweep error: {e}")

    return now_ts


def maybe_enrich_queue_items_from_mb(now_ts, last_run_ts, interval_seconds=600):
    """Periodically fetch missing duration / artist for queued items from MusicBrainz.

    Runs at most every *interval_seconds* (default 10 minutes).  For each
    queue item that is missing a ``duration`` or has a blank ``artist`` we
    attempt a MusicBrainz lookup:

    1. If the item has a ``recording_mbid`` we call the MB recording endpoint
       directly — this gives duration (ms) and artist credits in one round-trip.
    2. Otherwise, if the item has a ``release_mbid`` / ``release_id`` we fetch
       the full release and match the track by disc + position number, falling
       back to a title match.

    We stay polite to MB: a 1-second sleep between requests is enforced and
    the batch is capped at 20 items per run so the loop does not block the
    processor for too long.
    """
    if last_run_ts is not None and (now_ts - last_run_ts) < interval_seconds:
        return last_run_ts

    _MB_ENRICH_BATCH = 20
    _MB_REQUEST_SLEEP = 1.1   # seconds between MB API calls

    try:
        conn = get_db()
        cursor = conn.cursor()
        placeholder = _get_placeholder(conn)

        # Find active queue items that are missing duration OR artist.
        # We only bother when there is at least one MB identifier to look up.
        cursor.execute(f"""
            SELECT id, artist, title, track_number, disc_number,
                   recording_mbid, release_mbid, release_id, release_source
            FROM download_queue
            WHERE status IN ('queued', 'failed')
              AND (
                    duration IS NULL
                    OR TRIM(COALESCE(artist, '')) = ''
              )
              AND (
                    (recording_mbid IS NOT NULL AND recording_mbid <> '')
                    OR (release_mbid  IS NOT NULL AND release_mbid  <> '')
                    OR (release_id    IS NOT NULL AND release_id    <> ''
                        AND LOWER(COALESCE(release_source, '')) = 'musicbrainz')
              )
            ORDER BY id ASC
            LIMIT {placeholder}
        """, (_MB_ENRICH_BATCH,))
        rows = [dict(r) for r in cursor.fetchall()]
        conn.close()
    except Exception as db_err:
        logger.warning(f"[MB_ENRICH] Could not query queue for enrichment: {db_err}")
        return last_run_ts

    if not rows:
        return now_ts

    logger.info(f"[MB_ENRICH] Enriching {len(rows)} queue item(s) with MusicBrainz data")

    try:
        from post_download_processor import fetch_musicbrainz_release_metadata
        from api_clients.musicbrainz import _USER_AGENT as _MB_USER_AGENT
    except Exception as import_err:
        logger.warning(f"[MB_ENRICH] Could not import MB helpers: {import_err}")
        return last_run_ts

    import requests as _requests

    _mb_headers = {
        "User-Agent": _MB_USER_AGENT,
        "Accept": "application/json",
    }

    # Simple in-process cache so we only fetch each release once per run.
    _release_cache = {}

    def _fetch_recording(recording_mbid):
        """Fetch duration + artist from MB recording endpoint."""
        try:
            time.sleep(_MB_REQUEST_SLEEP)
            url = f"https://musicbrainz.org/ws/2/recording/{recording_mbid}"
            resp = _requests.get(url, headers=_mb_headers,
                                 params={"fmt": "json", "inc": "artist-credits"},
                                 timeout=10)
            resp.raise_for_status()
            data = resp.json()
            duration_ms = data.get("length")
            duration_sec = int(round(duration_ms / 1000)) if duration_ms else None
            artist = ""
            ac = data.get("artist-credit") or []
            if ac:
                parts = []
                for credit in ac:
                    if isinstance(credit, dict):
                        name = credit.get("name") or (credit.get("artist") or {}).get("name") or ""
                        join = credit.get("joinphrase") or ""
                        parts.append(name + join)
                artist = "".join(parts).strip()
            return duration_sec, artist or None
        except Exception as e:
            logger.debug(f"[MB_ENRICH] Recording lookup failed for {recording_mbid}: {e}")
            return None, None

    def _fetch_from_release(release_mbid, disc_number, track_number, title):
        """Return (duration_sec, artist) by locating the track within a release."""
        if release_mbid not in _release_cache:
            try:
                _release_cache[release_mbid] = fetch_musicbrainz_release_metadata(release_mbid)
                time.sleep(_MB_REQUEST_SLEEP)
            except Exception as e:
                logger.debug(f"[MB_ENRICH] Release fetch failed for {release_mbid}: {e}")
                _release_cache[release_mbid] = None
        mb_release = _release_cache.get(release_mbid)
        if not mb_release:
            return None, None

        disc_num = int(disc_number or 1)
        track_num_int = None
        if track_number is not None:
            try:
                track_num_int = int(track_number)
            except (TypeError, ValueError):
                pass

        norm_title = re.sub(r"\s+", " ", (title or "").lower().strip())

        best = None
        for t in mb_release.get("tracks", []):
            t_disc = int(t.get("disc_number") or 1)
            t_num = t.get("track_number")
            try:
                t_num_int = int(t_num) if t_num is not None else None
            except (TypeError, ValueError):
                t_num_int = None

            # Match by disc + track number (preferred)
            if (t_disc == disc_num and track_num_int is not None
                    and t_num_int == track_num_int):
                best = t
                break

            # Fall back: match by title on same disc
            t_norm = re.sub(r"\s+", " ", (t.get("title") or "").lower().strip())
            if t_disc == disc_num and norm_title and t_norm == norm_title:
                best = t

        if not best:
            return None, None

        dur_ms = best.get("duration")
        duration_sec = int(round(dur_ms / 1000)) if dur_ms else None
        artist = best.get("artist") or None
        return duration_sec, artist

    enriched = 0
    for row in rows:
        item_id = row["id"]
        recording_mbid = (row.get("recording_mbid") or "").strip()
        release_mbid = (row.get("release_mbid") or row.get("release_id") or "").strip()
        needs_duration = row.get("duration") is None
        needs_artist = not (row.get("artist") or "").strip()

        duration_sec = None
        artist = None

        if recording_mbid:
            duration_sec, artist = _fetch_recording(recording_mbid)
        elif release_mbid:
            duration_sec, artist = _fetch_from_release(
                release_mbid,
                row.get("disc_number"),
                row.get("track_number"),
                row.get("title"),
            )

        updates = {}
        if needs_duration and duration_sec:
            updates["duration"] = duration_sec
        if needs_artist and artist:
            updates["artist"] = artist

        if updates:
            try:
                from download_queue_manager import update_queue_item
                update_queue_item(item_id, **updates)
                enriched += 1
                logger.info(
                    f"[MB_ENRICH] Queue {item_id} ({'|'.join(f'{k}={v}' for k, v in updates.items())})"
                )
            except Exception as upd_err:
                logger.warning(f"[MB_ENRICH] Could not update queue {item_id}: {upd_err}")

    if enriched:
        logger.info(f"[MB_ENRICH] Enriched {enriched} queue item(s)")

    return now_ts


def run_processor(interval=30):
    """Run queue processor loop"""
    logger.info("=== Queue Processor Started ===")
    logger.info(f"Processing interval: {interval}s")
    
    client = get_slskd_client()
    if not client:
        logger.error("Cannot initialize SlskdClient - exiting")
        sys.exit(1)
    
    loop_count = 0
    last_auto_discover_ts = None
    last_mb_check_ts = None
    last_mb_finalize_ts = None
    last_verify_ts = None
    last_stale_cleanup_ts = None
    last_slskd_retry_ts = None
    last_slskd_clear_ts = None
    last_downloads_folder_ts = None
    last_navidrome_scan_ts = None
    last_retry_completed_ts = None
    last_cleanup_imported_ts = None
    last_mb_enrich_ts = None

    try:
        while True:
            try:
                loop_count += 1
                logger.debug(f"--- Loop {loop_count} ---")

                now_ts = time.time()
                last_auto_discover_ts = auto_discover_files(now_ts, last_auto_discover_ts)
                last_mb_check_ts = check_musicbrainz_files(now_ts, last_mb_check_ts)
                last_mb_finalize_ts = finalize_musicbrainz_releases(now_ts, last_mb_finalize_ts)
                last_verify_ts = check_missing_moved_files(now_ts, last_verify_ts)
                last_stale_cleanup_ts = cleanup_stale_downloads(now_ts, last_stale_cleanup_ts)
                last_slskd_retry_ts = check_failed_slskd_downloads(now_ts, last_slskd_retry_ts)
                last_slskd_clear_ts = clear_slskd_completed_downloads(now_ts, last_slskd_clear_ts)
                last_mb_enrich_ts = maybe_enrich_queue_items_from_mb(now_ts, last_mb_enrich_ts)

                # process_queue() (which contains check_completed_downloads()) must
                # run BEFORE check_downloads_folder() so that items in 'downloading'
                # state are claimed and auto-moved by the slskd-aware path first.
                # If check_downloads_folder() ran first it would transition those
                # items from 'downloading' → 'completed', and check_completed_downloads
                # (which only queries WHERE status='downloading') would find nothing —
                # leaving non-MusicBrainz and incomplete-album items stuck in
                # 'completed' permanently.
                processed = process_queue(client)

                last_downloads_folder_ts = check_downloads_folder(now_ts, last_downloads_folder_ts)

                # Sweep items that landed in 'completed' (file matched, move
                # deferred) and retry the move now that all siblings may be ready.
                last_retry_completed_ts = retry_pending_completed_moves(
                    now_ts, last_retry_completed_ts
                )

                # Periodically remove old imported records so the queue doesn't
                # accumulate stale entries that trigger spurious move attempts.
                if last_cleanup_imported_ts is None or (now_ts - last_cleanup_imported_ts) >= 3600:
                    try:
                        from download_queue_manager import cleanup_imported
                        cleanup_imported(days=7)
                    except Exception as _ci_err:
                        logger.warning(f"[CLEANUP-IMPORTED] Error purging old imported records: {_ci_err}")
                    last_cleanup_imported_ts = now_ts

                last_navidrome_scan_ts = trigger_navidrome_scan_for_new_imports(now_ts, last_navidrome_scan_ts)
                
                if processed > 0:
                    logger.info(f"Processed {processed} queue items")
                
                time.sleep(interval)
                
            except KeyboardInterrupt:
                logger.info("Queue processor stopped by user")
                break
            except Exception as e:
                logger.error(f"Error in processor loop: {e}")
                logger.error(traceback.format_exc())
                time.sleep(interval)
                
    except KeyboardInterrupt:
        logger.info("Queue processor interrupted")
    finally:
        logger.info("=== Queue Processor Stopped ===")

if __name__ == "__main__":
    # Default interval is 30 seconds
    interval = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    run_processor(interval)
