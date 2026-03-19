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
import signal
import subprocess
import sys
import time
import traceback
import yaml
from datetime import datetime, timedelta
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

# ── Graceful shutdown flag ────────────────────────────────────────────────────
# Set by the SIGTERM handler so the main loop exits cleanly after the current
# iteration, allowing in-flight DB transactions to commit/close before the
# process terminates.  Without this, Docker's graceful stop sends SIGTERM which
# Python ignores by default → then SIGKILL → Postgres sees "unexpected EOF on
# client connection while in transaction".
_shutdown_requested = False


def _handle_sigterm(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True
    logger.info("Queue processor received SIGTERM — will stop after current iteration")


signal.signal(signal.SIGTERM, _handle_sigterm)

DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")

# Similarity thresholds for Navidrome existence checks
_NAV_TITLE_SIMILARITY_THRESHOLD = 0.85
_NAV_ARTIST_SIMILARITY_THRESHOLD = 0.75
# Similarity / tolerance thresholds for confirmed in-collection matching
_ALBUM_SIMILARITY_THRESHOLD = 0.85
_CONFIRMED_MATCH_DURATION_TOLERANCE_SECONDS = 10
# Minimum title similarity for prefix-like title pairs (e.g. "World So Cold"
# vs "World So Cold Intro").  See _metadata_matches_queue_item for details.
_PREFIX_TITLE_MIN = 0.9
_TITLE_VARIANT_TOKENS = {
    "acoustic", "demo", "edit", "instrumental", "intro", "live",
    "mix", "radio", "remaster", "remastered", "remix", "version",
}

# Minimum similarity score below which a file's artist/title tags are considered
# a hard mismatch against the queue item.  Scores this low mean it's a completely
# different song and filename matching should not be attempted as a fallback.
_HARD_MISMATCH_FLOOR = 0.35

# Maximum time (in minutes) a download is allowed to stay in each active slskd
# transfer state before the queue processor cancels it and retries.
# Keys match the slskd state strings used by SlskdClient constants.
_SLSKD_ACTIVE_STATE_TIMEOUT_MINUTES = {
    "Queued, Remotely": 120,   # Remote peer queued it but never started sending
    "Requested": 30,            # No response from remote peer after 30 min
    "Initializing": 30,         # Transfer started to initialise but never progressed
    "InProgress": 240,          # Active download that has stalled for 4 hours
    "Queued": 120,
    "In Progress": 240,
    "Downloading": 240,
}


def _is_postgres_connection(conn):
    """Return True when the active DB connection is (or wraps) a psycopg2 connection."""
    try:
        import psycopg2
        underlying = getattr(conn, "_conn", conn)
        return isinstance(underlying, psycopg2.extensions.connection)
    except Exception:
        return False


def _get_placeholder(conn):
    return "%s"


def resolve_downloads_dir():
    """Resolve downloads directory from config/env with safe fallback.
    Config file takes priority over environment variable."""
    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
    try:
        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                cfg = yaml.safe_load(f) or {}
            configured = (cfg.get('downloads') or {}).get('folder')
            if configured and configured.strip():
                return os.path.normpath(configured.strip())
    except Exception as e:
        logger.warning(f"Could not read downloads folder from config: {e}")

    env_dir = os.environ.get("DOWNLOADS_DIR")
    if env_dir and env_dir.strip():
        return os.path.normpath(env_dir.strip())

    return "/downloads/Music"


DOWNLOADS_DIR = resolve_downloads_dir()

# Soulseek queue downloads are intentionally restricted to these formats.
_SLSKD_ALLOWED_EXTENSIONS = ('.mp3', '.flac')

_GENERIC_ARTIST_NAMES = {
    'various artists',
    'various',
    'va',
    'unknown artist',
    'unknown',
    'soundtrack',
    'ost',
}

_DEFAULT_DOWNLOAD_QUALITY_FILTER = {
    'enabled': False,
    'reject_others': True,
    'bitrate_tolerance': 5,
    'priorities': [
        {'format': 'mp3', 'bitrate_kbps': 320},
        {'format': 'flac', 'bitrate_kbps': None},
    ],
}

# Cache album-search response snapshots briefly so sibling tracks in the same
# queued release can reuse results instead of re-querying Soulseek each time.
_ALBUM_SEARCH_CACHE_TTL_SECONDS = 600
_album_search_cache = {}


def _strip_track_number_prefix(title):
    """
    Remove a leading track-number prefix and/or a trailing Soulseek unique-ID
    suffix from a title string.  Mirrors the helper in download_queue_manager so
    that Soulseek candidate filenames are normalised consistently before scoring.

    Leading prefix examples:
        "05 - CINEMA"          →  "CINEMA"
        "1-15 - Worms ..."     →  "Worms ..."  (disc-track prefix)
        "16. Artist - Title"   →  "Artist - Title"

    Trailing Soulseek UID suffix examples:
        "Song_639091010921933965"  →  "Song"   (12+ digit UID stripped)
        "Song_123"                 →  unchanged (≤ 11 digits kept)
    """
    cleaned = re.sub(r'^\d+(?:\s*-\s*\d+)?\s*[-\.]\s*', '', title).strip()
    cleaned = re.sub(r'_\d{12,}$', '', cleaned).strip()
    return cleaned if cleaned else title


def _normalize_match_text(value):
    """Normalize text for conservative filename/metadata matching.

    Leading track-number prefixes (e.g. ``07 ``, ``1-15 - ``) and trailing
    Soulseek unique-ID suffixes (e.g. ``_639091010921933965``) are stripped
    before the remaining non-alphanumeric characters are replaced with spaces.
    This improves candidate scoring for Soulseek filenames that embed a track
    number or a collision-avoidance suffix.
    """
    if not value:
        return ""
    value = str(value).lower()
    value = _strip_track_number_prefix(value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _tokenize_meaningful(value):
    """Tokenize and remove short/common words to reduce false positives."""
    stop_words = {"the", "and", "of", "a", "an", "to", "in", "on", "for", "with"}
    normalized = _normalize_match_text(value)
    return [t for t in normalized.split() if len(t) >= 3 and t not in stop_words]


def _extract_title_variant_tokens(value):
    """Return known version/variant tokens from a title-like string."""
    tokens = set(_normalize_match_text(value).split())
    return tokens & _TITLE_VARIANT_TOKENS


def _title_variants_are_compatible(expected_title, candidate_title):
    """Require variant labels (mix/live/edit/etc.) to agree between titles."""
    expected_variants = _extract_title_variant_tokens(expected_title)
    candidate_variants = _extract_title_variant_tokens(candidate_title)

    if expected_variants or candidate_variants:
        if not expected_variants or not candidate_variants:
            return False
        if expected_variants.isdisjoint(candidate_variants):
            return False
    return True


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
    """Use stricter duration tolerance for MusicBrainz-backed queue items."""
    return 10 if _is_musicbrainz_backed(queue_item) else 15


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


def _score_soulseek_candidate(filename, queue_item, candidate_duration=None):
    """
    Score a Soulseek candidate path/name against queue metadata.

    Returns float score in [0, 1]. Higher is better.
    """
    filename_norm = _normalize_match_text(filename)
    # Compute the basename early so it can be used for title-token matching.
    # Title tokens must appear in the *filename* (not merely in a parent folder)
    # to count as evidence.  Using the full path here causes false positives when
    # an album folder is named after a track — e.g. searching for the song
    # "Alisha Rules the World" must NOT match
    # "Alisha's Attic/Alisha Rules The World/01-02 Intense.flac" just because the
    # album folder shares the title.
    # Normalize path separators first: Soulseek delivers Windows-style backslash
    # paths even when running on Linux, so os.path.basename would otherwise
    # return the whole path unchanged.
    basename_norm = _normalize_match_text(os.path.basename(filename.replace('\\', '/')))
    artist_norm = _normalize_match_text(queue_item.get('artist'))
    title_norm = _normalize_match_text(queue_item.get('title'))
    album_norm = _normalize_match_text(queue_item.get('album'))
    title_tokens = _tokenize_meaningful(title_norm)
    # Use basename tokens (not full-path tokens) so that a folder whose name
    # coincidentally matches the requested title does not inflate the ratio.
    filename_tokens = set(_tokenize_meaningful(basename_norm))

    if not artist_norm or not title_norm or not filename_norm:
        return 0.0

    if title_tokens:
        shared_title_tokens = sum(1 for tok in title_tokens if tok in filename_tokens)
        title_token_ratio = shared_title_tokens / len(title_tokens)
        requested_variants = set(title_tokens) & _TITLE_VARIANT_TOKENS
        candidate_variants = filename_tokens & _TITLE_VARIANT_TOKENS

        if requested_variants or candidate_variants:
            if not requested_variants or not candidate_variants:
                return 0.0
            if requested_variants.isdisjoint(candidate_variants):
                return 0.0

        if len(title_tokens) <= 2 and shared_title_tokens < len(title_tokens):
            return 0.0
        if len(title_tokens) >= 3 and title_token_ratio < 0.67:
            return 0.0
    else:
        title_token_ratio = 0.0

    # Require both core fields to be reasonably represented in filename/path.
    artist_sim = SequenceMatcher(None, artist_norm, filename_norm).ratio()
    title_sim = SequenceMatcher(None, title_norm, filename_norm).ratio()
    if artist_sim < 0.12 or title_sim < 0.12:
        return 0.0

    score = (artist_sim * 0.45) + (title_sim * 0.55)
    score += (0.22 * title_token_ratio)

    # Strongly prefer explicit artist/title phrases when present.
    if artist_norm in filename_norm:
        score += 0.18
    # Award the title-in-path bonus only when the title appears in the actual
    # filename component, not merely in a parent directory.  When the album
    # folder is named after a track (e.g. folder "This Is The Sound" and the
    # file being "02. Skindred - You Got This.flac"), title_norm matches the
    # path but NOT the filename — so we should not reward it as a strong signal.
    # (basename_norm was computed near the top of this function.)
    if title_norm in filename_norm and title_norm in basename_norm:
        score += 0.25
    elif title_norm in basename_norm:
        score += 0.20

    # Album disambiguation: prevent "Power"-style partial collisions.
    if album_norm:
        album_tokens = _tokenize_meaningful(album_norm)
        if album_tokens:
            shared_album_tokens = sum(1 for tok in album_tokens if tok in filename_norm)
            token_ratio = shared_album_tokens / len(album_tokens)

            # When >=2 album tokens appear in the filename but fewer than 2
            # match, reject the candidate — this blocks near-misses like
            # "Sword of Power" matching a file tagged "Power of Metal".
            # When *no* album tokens appear in the filename at all, the album
            # information is simply absent (common for Soulseek filenames that
            # only contain the track number and title), so we skip the
            # disambiguation rather than returning 0.0.
            if len(album_tokens) >= 2 and 0 < shared_album_tokens < 2:
                return 0.0

            # Reward strong album evidence and penalize weak/partial album alignment.
            if album_norm in filename_norm:
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
        if duration_diff <= 4:
            score += 0.22
        elif duration_diff <= 8:
            score += 0.12
        elif duration_diff <= duration_tolerance:
            score += 0.05 if _is_musicbrainz_backed(queue_item) else 0.0
        elif duration_diff > duration_tolerance:
            return 0.0
        else:
            score -= 0.05

    return max(0.0, min(1.0, score))


def _extract_response_files(response):
    """Return iterable of file entries from a Soulseek response row."""
    if hasattr(response, 'files') and response.files:
        return response.files
    if isinstance(response, dict):
        files = response.get('files')
        if isinstance(files, list):
            return files
    return []


def _candidate_filename(file_info):
    if isinstance(file_info, dict):
        return file_info.get('filename', '') or ''
    return getattr(file_info, 'filename', '') or ''


def _candidate_size(file_info):
    if isinstance(file_info, dict):
        return file_info.get('size', 0) or 0
    return getattr(file_info, 'size', 0) or 0


def _is_allowed_download_extension(filename):
    return str(filename or '').lower().endswith(_SLSKD_ALLOWED_EXTENSIONS)


def _load_download_quality_filter():
    """Read downloads.quality_filter from config with safe defaults."""
    cfg = dict(_DEFAULT_DOWNLOAD_QUALITY_FILTER)
    try:
        config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
        if not os.path.exists(config_path):
            config_path = "/config/config.yml"
        if not os.path.exists(config_path):
            return cfg

        with open(config_path, 'r', encoding='utf-8') as f:
            loaded = yaml.safe_load(f) or {}

        qf = (loaded.get('downloads') or {}).get('quality_filter') or {}
        cfg['enabled'] = bool(qf.get('enabled', cfg['enabled']))
        cfg['reject_others'] = bool(qf.get('reject_others', cfg['reject_others']))
        try:
            cfg['bitrate_tolerance'] = int(qf.get('bitrate_tolerance', cfg['bitrate_tolerance']))
        except Exception:
            cfg['bitrate_tolerance'] = _DEFAULT_DOWNLOAD_QUALITY_FILTER['bitrate_tolerance']

        priorities = qf.get('priorities')
        if isinstance(priorities, list) and priorities:
            cfg['priorities'] = priorities

    except Exception as e:
        logger.debug(f"[QUALITY-FILTER] Could not read quality filter config: {e}")

    return cfg


def _extract_candidate_bitrate_kbps(file_info):
    """Extract bitrate for a Soulseek candidate when available."""
    bitrate_raw = None
    if isinstance(file_info, dict):
        bitrate_raw = (
            file_info.get('bitrate_kbps')
            or file_info.get('bitrateKbps')
            or file_info.get('bitrate')
            or file_info.get('bitRate')
        )
    else:
        bitrate_raw = (
            getattr(file_info, 'bitrate_kbps', None)
            or getattr(file_info, 'bitrateKbps', None)
            or getattr(file_info, 'bitrate', None)
            or getattr(file_info, 'bitRate', None)
        )

    try:
        if bitrate_raw is None:
            return None
        bitrate = float(bitrate_raw)
        if bitrate <= 0:
            return None
        # Some APIs report bps instead of kbps.
        if bitrate > 10000:
            bitrate = bitrate / 1000.0
        return int(round(bitrate))
    except Exception:
        return None


def _candidate_matches_quality_filter(file_info, filename, quality_filter):
    """Return True when candidate file passes configured quality rules."""
    if not quality_filter.get('enabled'):
        return True

    priorities = quality_filter.get('priorities') or []
    if not priorities:
        return True

    ext = os.path.splitext(str(filename or '').lower())[1].lstrip('.')
    if not ext:
        return not quality_filter.get('reject_others', True)

    bitrate_kbps = _extract_candidate_bitrate_kbps(file_info)
    tolerance = int(quality_filter.get('bitrate_tolerance', 5) or 0)

    format_rules = [p for p in priorities if str(p.get('format') or '').lower() == ext]
    if not format_rules:
        return not quality_filter.get('reject_others', True)

    for rule in format_rules:
        target_bitrate = rule.get('bitrate_kbps')
        if target_bitrate in (None, ''):
            return True
        try:
            target = int(target_bitrate)
        except Exception:
            continue

        if bitrate_kbps is None:
            continue

        if abs(bitrate_kbps - target) <= tolerance:
            return True

    return not quality_filter.get('reject_others', True)


def _pick_best_candidate_from_responses(responses, queue_item):
    """Select the best allowed Soulseek candidate for a queue item."""
    best_result = None
    best_score = 0.0
    quality_filter = _load_download_quality_filter()

    for resp_idx, resp in enumerate(responses or []):
        resp_files = _extract_response_files(resp)
        if not resp_files:
            logger.debug(
                f"Queue {queue_item.get('id', 'unknown')}: Response {resp_idx} has no files"
            )
            continue

        resp_username = getattr(resp, 'username', None)
        if not resp_username and isinstance(resp, dict):
            resp_username = resp.get('username')
        resp_username = resp_username or 'unknown'

        for file_info in resp_files:
            filename = _candidate_filename(file_info)
            if not _is_allowed_download_extension(filename):
                continue
            if not _candidate_matches_quality_filter(file_info, filename, quality_filter):
                logger.debug(
                    f"Queue {queue_item.get('id', 'unknown')}: skipped by quality filter: {filename}"
                )
                continue

            size = _candidate_size(file_info)
            candidate_length = _extract_candidate_length_seconds(file_info)
            candidate_score = _score_soulseek_candidate(filename, queue_item, candidate_length)
            if candidate_score > best_score:
                best_score = candidate_score
                best_result = {
                    "username": resp_username,
                    "filename": filename,
                    "size": size,
                    "length": candidate_length,
                    "score": candidate_score,
                }

    return best_result, best_score


def _poll_search_responses(client, search_id, max_poll_attempts=45):
    """Poll Soulseek search and return all gathered responses."""
    gathered = []
    for _ in range(max_poll_attempts):
        time.sleep(1)
        try:
            responses, _state, is_complete = client.get_search_results(search_id)
            if responses:
                gathered = responses
            if is_complete:
                break
        except Exception as e:
            logger.debug(f"Soulseek poll error for search_id={search_id}: {e}")
    return gathered


def _is_generic_artist_name(value):
    artist = str(value or '').strip().lower()
    return artist in _GENERIC_ARTIST_NAMES


def _get_effective_track_search_query(queue_item):
    """Build a Soulseek track query that avoids generic artist tokens."""
    artist = str(queue_item.get('artist') or '').strip()
    title = str(queue_item.get('title') or '').strip()

    if not title:
        return str(queue_item.get('search_query') or '').strip()

    if _is_generic_artist_name(artist):
        # Some discovered files or compilation rows can store titles like
        # "Track Artist - Song Title" with artist left as "Various Artists".
        # Split that when available and search by the extracted track artist.
        if ' - ' in title:
            left, right = [part.strip() for part in title.split(' - ', 1)]
            if left and right and not _is_generic_artist_name(left):
                return f"{left} - {right}"
        # No reliable per-track artist available; title-only search is safer
        # than querying Soulseek as "Various Artists - <title>".
        return title

    return f"{artist} - {title}"


def _get_album_search_query(queue_item):
    album = (queue_item.get('album') or '').strip()
    if not album or album.lower() in ('unknown', 'unknown album'):
        return None

    album_artist = (
        queue_item.get('album_artist')
        or queue_item.get('artist')
        or ''
    ).strip()
    if _is_generic_artist_name(album_artist):
        # Compilation-level artists (e.g. Various Artists) produce very broad
        # album searches and often pollute candidate results.
        return None
    if album_artist:
        return f"{album_artist} - {album}"
    return album


def _get_album_queue_titles(queue_item):
    """Return normalized title set for currently queued tracks in this album/import group."""
    import_group = (queue_item.get('import_group') or '').strip()
    album = (queue_item.get('album') or '').strip()
    album_artist = (
        queue_item.get('album_artist')
        or queue_item.get('artist')
        or ''
    ).strip()

    if not import_group and (not album or not album_artist):
        return set()

    titles = set()
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        placeholder = _get_placeholder(conn)

        if import_group:
            cursor.execute(
                f"""
                SELECT title FROM download_queue
                WHERE import_group = {placeholder}
                  AND status IN ('queued', 'searching', 'downloading')
                """,
                (import_group,),
            )
        else:
            cursor.execute(
                f"""
                SELECT title FROM download_queue
                WHERE LOWER(COALESCE(album, '')) = LOWER({placeholder})
                  AND LOWER(COALESCE(album_artist, artist, '')) = LOWER({placeholder})
                  AND status IN ('queued', 'searching', 'downloading')
                """,
                (album, album_artist),
            )

        rows = cursor.fetchall() or []
        for row in rows:
            title = row.get('title') if hasattr(row, 'get') else (row[0] if row else None)
            title_norm = _normalize_match_text(title)
            if title_norm:
                titles.add(title_norm)
    except Exception as e:
        logger.debug(f"Could not fetch queued album titles for album-first search: {e}")
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass

    return titles


def _metadata_matches_queue_item(file_path, queue_item, threshold=0.68):
    """
    Validate file tags against queue artist/title.

    Returns:
        True: metadata exists and is a strong match
        False: metadata exists but mismatches queue item
        None: metadata unavailable; caller may fallback to filename matching
    """
    try:
        metadata = read_mp3_metadata(file_path) or {}
    except Exception:
        return None

    file_artist = (metadata.get('artist') or '').strip()
    file_title = (metadata.get('title') or '').strip()
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
        except Exception:
            pass

    if not file_artist or not file_title:
        return None

    queue_artist = (queue_item.get('artist') or '').strip()
    queue_title = (queue_item.get('title') or '').strip()
    if not queue_artist or not queue_title:
        return None

    artist_score = SequenceMatcher(
        None,
        _normalize_match_text(file_artist),
        _normalize_match_text(queue_artist),
    ).ratio()
    title_score = SequenceMatcher(
        None,
        _normalize_match_text(file_title),
        _normalize_match_text(queue_title),
    ).ratio()

    # Hard mismatch: scores so low that this is clearly a different file.  Block
    # all further matching (including the filename fallback in the caller) to avoid
    # importing completely wrong tracks.
    if artist_score < _HARD_MISMATCH_FLOOR or title_score < _HARD_MISMATCH_FLOOR:
        return False

    # Soft mismatch: scores are too low for a confident match but not definitively
    # wrong.  This often happens when the downloaded file has extra information in
    # its tags that the queue item does not — e.g. the file is tagged
    # "Creep (Acoustic)" while the queue item has title "Creep", or the artist
    # field carries featured-artist annotations.  Return None to allow the caller
    # to fall back to filename matching rather than rejecting the file outright.
    if artist_score < 0.55 or title_score < 0.55:
        return None

    # Reject explicit variant mismatches like "Song" vs "Song (Dusk Mix)".
    if not _title_variants_are_compatible(queue_title, file_title):
        return False

    # Protect against "prefix" false-positives: when one title is merely a
    # leading substring of the other (e.g. "World So Cold" vs "World So Cold
    # Intro"), the similarity score is deceptively high (~0.81) even though
    # these are distinct tracks.  Require a near-exact title match in that
    # case to avoid incorrectly marking a queue item as matched/completed.
    _title_a = _normalize_match_text(file_title)
    _title_b = _normalize_match_text(queue_title)
    if _title_a != _title_b and (_title_a.startswith(_title_b) or _title_b.startswith(_title_a)):
        if title_score < _PREFIX_TITLE_MIN:
            return False

    expected_duration = _normalize_duration_seconds(queue_item.get('duration'))
    file_duration = None
    if audio is not None and getattr(audio, 'info', None) and hasattr(audio.info, 'length'):
        file_duration = _normalize_duration_seconds(audio.info.length)
    if expected_duration and file_duration:
        if abs(expected_duration - file_duration) > _get_duration_match_tolerance(queue_item):
            return False

    combined = (artist_score + title_score) / 2
    return combined >= threshold


def _filename_matches_queue_item(filename, queue_item):
    """
    Conservative filename/path fallback when file metadata is unavailable.

    Returns True if the filename strongly suggests it belongs to the queue item,
    using artist+title substring checks with a sequence-similarity safety net.
    """
    try:
        filename_test = filename.lower().replace('\\', '/')
        # Extract just the basename (without the directory path) for title matching.
        # This prevents false positives when the album folder name equals the track
        # title: e.g. queue item "This Is The Sound" must NOT match
        # "This Is The Sound/02. Skindred - You Got This.flac" because the title
        # only appears in the directory component, not in the actual filename.
        basename_test = os.path.basename(filename_test)
        artist = (queue_item.get('artist') or '').lower().strip()
        title = (queue_item.get('title') or '').lower().strip()

        if not artist or not title:
            return False

        if not _title_variants_are_compatible(title, basename_test):
            return False

        artist_in_path = artist in filename_test
        # Require the title to appear as a complete phrase in the basename — it
        # must not be immediately followed by more alphabetic words that would
        # make it a different (longer) title.  For example, a queue item titled
        # "-1" must NOT match a file named "-1 intro.flac", but it SHOULD match
        # "-1.flac" or "-1 (acoustic).flac" (parenthetical suffix, not a word).
        # Both basename_test and title are already lowercased above, so [a-z]
        # correctly covers all letter characters.
        title_in_basename = bool(re.search(re.escape(title) + r'(?!\s*[a-z])', basename_test))
        if artist_in_path and title_in_basename:
            return True

        # Guard: if the title only appears in the directory portion of the path
        # (not in the basename at all), skip the fallback — the folder match is
        # an album-name coincidence, not evidence the file contains this track.
        if title not in basename_test:
            return False

        # Similarity fallback.  The title appears in the basename as a substring
        # but not as a complete phrase (the whole-phrase guard above was False).
        # Use a stricter threshold so that prefix-titled variants like
        # "World So Cold Intro" do not match "World So Cold", while still
        # allowing high-confidence hits such as "radiohead creep pablo honey.flac"
        # matching the queue item for Radiohead – Creep.
        album = (queue_item.get('album') or '').lower().strip()
        combined_target = f"{artist} {title} {album}".strip()
        score = SequenceMatcher(None, combined_target, filename_test).ratio()
        threshold = 0.60 if title_in_basename else 0.85
        if score >= threshold and (artist_in_path or title_in_basename):
            return True

        return False

    except Exception as e:
        logger.error(f"Error in filename matching for {filename}: {e}")
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
    """Get database connection and fail fast unless PostgreSQL is active."""
    from helpers.db_utils import get_db_connection
    conn = get_db_connection()
    if not _is_postgres_connection(conn):
        try:
            conn.close()
        except Exception:
            pass
        raise RuntimeError("queue_processor requires PostgreSQL. SQLite fallback is disabled.")
    return conn

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


def _load_qbittorrent_config():
    """Load qBittorrent settings from config.yaml with safe defaults."""
    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
    if not os.path.exists(config_path):
        config_path = "/config/config.yml"

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("qbittorrent", {}) or {}
    except Exception as e:
        logger.error(f"Could not load qBittorrent config: {e}")
        return {}


def _fallback_queue_item_to_soulseek(queue_id, reason, retry_delay_minutes=5):
    """Switch a queue item to Soulseek and requeue it for a fallback attempt."""
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        placeholder = _get_placeholder(conn)
        next_retry = (datetime.now() + timedelta(minutes=retry_delay_minutes)).isoformat()
        cursor.execute(
            f"""
            UPDATE download_queue
            SET source = 'soulseek',
                status = 'queued',
                failure_reason = {placeholder},
                next_retry_at = {placeholder},
                updated_at = CURRENT_TIMESTAMP
            WHERE id = {placeholder}
            """,
            (reason, next_retry, queue_id),
        )
        conn.commit()
        logger.warning(f"Queue {queue_id}: switched to Soulseek fallback ({reason})")
        return True
    except Exception as e:
        logger.error(f"Queue {queue_id}: could not switch to Soulseek fallback: {e}")
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def search_and_download_qbittorrent(queue_id, queue_item):
    """Search qBittorrent for queue item and enqueue top torrent; fallback to Soulseek when needed."""
    try:
        qbit_cfg = _load_qbittorrent_config()
        if not qbit_cfg.get("enabled"):
            _fallback_queue_item_to_soulseek(queue_id, "qBittorrent disabled")
            return False

        web_url = (qbit_cfg.get("web_url") or "http://localhost:8080").rstrip("/")
        username = qbit_cfg.get("username") or ""
        password = qbit_cfg.get("password") or ""
        search_query = queue_item.get("search_query") or f"{queue_item.get('artist', '')} - {queue_item.get('title', '')}"

        update_queue_status(queue_id, "searching")

        with requests.Session() as session:
            if username and password:
                try:
                    session.post(
                        f"{web_url}/api/v2/auth/login",
                        data={"username": username, "password": password},
                        timeout=8,
                    )
                except Exception as login_err:
                    logger.debug(f"Queue {queue_id}: qBittorrent login warning: {login_err}")

            start_resp = session.post(
                f"{web_url}/api/v2/search/start",
                data={"pattern": search_query, "plugins": "all", "category": "Music"},
                timeout=12,
            )
            if start_resp.status_code not in (200, 201):
                _fallback_queue_item_to_soulseek(queue_id, f"qBittorrent search start failed: {start_resp.status_code}")
                return False

            search_id = (start_resp.json() or {}).get("id")
            if not search_id:
                _fallback_queue_item_to_soulseek(queue_id, "qBittorrent returned no search id")
                return False

            best_result = None
            for _ in range(40):
                time.sleep(0.5)
                status_resp = session.get(f"{web_url}/api/v2/search/status", params={"id": search_id}, timeout=8)
                if status_resp.status_code != 200:
                    continue

                results_resp = session.get(
                    f"{web_url}/api/v2/search/results",
                    params={"id": search_id, "limit": 200},
                    timeout=8,
                )
                if results_resp.status_code == 200:
                    results = (results_resp.json() or {}).get("results", [])
                    if results:
                        best_result = max(results, key=lambda r: (r.get("nb_seeders", 0), r.get("size", 0)))

                status_rows = status_resp.json() or []
                if status_rows and status_rows[0].get("status") == "Stopped":
                    break

            try:
                session.post(f"{web_url}/api/v2/search/stop", data={"id": search_id}, timeout=5)
            except Exception:
                pass

            if not best_result:
                _fallback_queue_item_to_soulseek(queue_id, "No qBittorrent results found", retry_delay_minutes=1)
                return False

            magnet = best_result.get("magnet_uri") or best_result.get("magnet")
            torrent_url = best_result.get("torrent_url") or best_result.get("link")
            if not (magnet or torrent_url):
                _fallback_queue_item_to_soulseek(queue_id, "qBittorrent result missing magnet/url", retry_delay_minutes=1)
                return False

            add_resp = session.post(
                f"{web_url}/api/v2/torrents/add",
                data={
                    "urls": magnet or torrent_url,
                    "category": "Music",
                    "tags": "Music",
                },
                timeout=12,
            )
            if add_resp.status_code in (200, 403):
                update_queue_status(queue_id, "downloading", found_filename=best_result.get("fileName") or best_result.get("name") or "")
                logger.info(f"Queue {queue_id}: qBittorrent download queued successfully")
                return True

            _fallback_queue_item_to_soulseek(queue_id, f"qBittorrent add failed: {add_resp.status_code}", retry_delay_minutes=1)
            return False

    except Exception as e:
        logger.error(f"Queue {queue_id}: qBittorrent error: {e}")
        _fallback_queue_item_to_soulseek(queue_id, f"qBittorrent error: {e}", retry_delay_minutes=1)
        return False

def cleanup_stuck_searching_items():
    """Detect and mark as failed any items stuck in 'searching' for too long"""
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        placeholder = _get_placeholder(conn)
        
        # Items stuck in 'searching' for more than 90 seconds are likely hung
        stuck_threshold = (datetime.now() - timedelta(seconds=90)).isoformat()
        
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
        
        conn.close()
        return len(stuck_items)
    except Exception as e:
        logger.error(f"Error cleaning up stuck searching items: {e}")
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

def get_queued_items(limit=10):
    """Get items ready to process (queued or scheduled for retry)"""
    conn = None
    try:
        # First, clean up any items stuck in 'searching' state
        cleanup_stuck_searching_items()
        conn = get_db()
        cursor = conn.cursor()
        placeholder = _get_placeholder(conn)
        
        now = datetime.now().isoformat()
        
        # Get queued items and items scheduled for retry
        cursor.execute("""
            SELECT * FROM download_queue 
            WHERE status = 'queued'
            AND (next_retry_at IS NULL OR next_retry_at <= {placeholder})
            ORDER BY priority ASC, retry_count ASC, next_retry_at ASC, created_at ASC
            LIMIT {placeholder}
        """.format(placeholder=placeholder), (now, limit))
        
        items = [dict(row) for row in cursor.fetchall()]
        return items
    except Exception as e:
        logger.error(f"Error getting queued items: {e}")
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def claim_queued_items(limit=10):
    """Claim queued rows for this worker with backend-safe SQL."""
    conn = None
    try:
        cleanup_stuck_searching_items()

        conn = get_db()
        cursor = conn.cursor()
        placeholder = _get_placeholder(conn)
        now = datetime.now().isoformat()

        cursor.execute(
            """
            WITH candidates AS (
                SELECT id
                FROM download_queue
                WHERE status = 'queued'
                  AND (next_retry_at IS NULL OR next_retry_at <= %s)
                ORDER BY priority ASC, retry_count ASC, next_retry_at ASC NULLS FIRST, created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT %s
            )
            UPDATE download_queue dq
            SET status = 'searching',
                updated_at = CURRENT_TIMESTAMP
            FROM candidates c
            WHERE dq.id = c.id
            RETURNING dq.*
            """,
            (now, int(limit)),
        )
        rows = cursor.fetchall() or []
        items = [dict(row) for row in rows]

        conn.commit()
        return items

    except Exception as e:
        logger.error(f"Error claiming queued items: {e}")
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def promote_stale_queried_items(min_age_seconds=120, limit=200):
    """
    Promote stale 'queried' items to 'queued' so they are processed automatically.

    Historically queried items required manual approval via UI. In practice this
    can leave tracks stuck indefinitely. We auto-promote only after a short age
    threshold so freshly inserted rows are not immediately flipped in the same
    write burst.

    Returns:
        int: number of rows promoted
    """
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        placeholder = _get_placeholder(conn)

        stale_before = (datetime.now() - timedelta(seconds=max(0, int(min_age_seconds)))).isoformat()

        cursor.execute(
            """
            SELECT id
            FROM download_queue
            WHERE status = 'queried'
              AND updated_at < {placeholder}
            ORDER BY updated_at ASC
            LIMIT {placeholder}
            """.format(placeholder=placeholder),
            (stale_before, int(limit)),
        )
        rows = cursor.fetchall() or []
        if not rows:
            return 0

        row_ids = [r['id'] if hasattr(r, 'keys') else r[0] for r in rows]
        if not row_ids:
            return 0

        cursor.execute(
            """
            UPDATE download_queue
            SET status = 'queued',
                updated_at = CURRENT_TIMESTAMP,
                failure_reason = COALESCE(failure_reason, 'Auto-promoted from queried by queue processor')
            WHERE id = ANY(%s)
            """,
            (row_ids,),
        )
        promoted = cursor.rowcount or 0

        conn.commit()

        if promoted > 0:
            logger.info(f"Auto-promoted {promoted} stale queried item(s) to queued")

        return int(promoted)

    except Exception as e:
        logger.error(f"Error promoting queried items: {e}")
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

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
                       'last_failure_time', 'source_id', 'source', 'matched_file_path',
                       'in_collection', 'collection_track_id']:
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
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

def increment_retry_count(queue_id, retry_delay_minutes=30):
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
        
        next_retry = datetime.now() + timedelta(minutes=retry_delay_minutes)
        
        cursor.execute(f"""
            UPDATE download_queue 
            SET retry_count = {placeholder}, next_retry_at = {placeholder}, updated_at = CURRENT_TIMESTAMP
            WHERE id = {placeholder}
        """, (retry_count, next_retry.isoformat(), queue_id))
        
        conn.commit()
        logger.info(f"Queue {queue_id}: retry count now {retry_count}, next retry at {next_retry}")
        return True
    except Exception as e:
        logger.error(f"Error incrementing retry count: {e}")
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

def mark_failed(queue_id, reason, schedule_retry=True, retry_delay_minutes=30):
    """Mark queue item as failed, optionally scheduling retry"""
    conn = None
    try:
        conn = get_db()
        
        cursor = conn.cursor()
        placeholder = "%s"
        
        # Get current retry_count and max_retries to enforce bounded retry behavior
        cursor.execute(f"SELECT retry_count, max_retries FROM download_queue WHERE id = {placeholder}", (queue_id,))
        row = cursor.fetchone()
        
        if not row:
            return False
        
        retry_count = (row['retry_count'] or 0) + 1
        max_retries = row.get('max_retries') if hasattr(row, 'keys') else (row[1] if len(row) > 1 else None)
        
        if schedule_retry and (not max_retries or retry_count < max_retries):
            next_retry = datetime.now() + timedelta(minutes=retry_delay_minutes)
            new_status = 'queued'
            logger.warning(f"Queue {queue_id}: Failed ({reason}), scheduling retry #{retry_count} at {next_retry}")
        else:
            next_retry = None
            new_status = 'failed'
            if schedule_retry and max_retries:
                logger.error(f"Queue {queue_id}: Failed permanently ({reason}) after max retries ({retry_count}/{max_retries})")
            else:
                logger.error(f"Queue {queue_id}: Failed permanently ({reason}) - retry not requested")
        
        cursor.execute(f"""
            UPDATE download_queue 
            SET status = {placeholder}, retry_count = {placeholder}, failure_reason = {placeholder}, last_failure_time = CURRENT_TIMESTAMP,
                next_retry_at = {placeholder}, updated_at = CURRENT_TIMESTAMP
            WHERE id = {placeholder}
        """, (new_status, retry_count, reason, next_retry.isoformat() if next_retry else None, queue_id))
        
        conn.commit()

        return new_status == 'queued'  # Return whether retry was scheduled

    except Exception as e:
        logger.error(f"Error marking queue item as failed: {e}")
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

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
    """Fire a Navidrome library scan so newly moved files are indexed without waiting
    for the next manually-scheduled import.

    Safe to call repeatedly — Navidrome coalesces concurrent scan requests and the
    call is fire-and-forget (we do not wait for it to complete).  Returns True when
    the scan was accepted, False when Navidrome is not configured or the request
    failed.
    """
    base_url, username, password = _get_navidrome_config()
    if not base_url:
        return False
    try:
        auth = _build_subsonic_auth_params(username, password)
        resp = requests.get(
            f"{base_url}/rest/startScan",
            params=auth,
            timeout=10,
        )
        resp.raise_for_status()
        status = resp.json().get("subsonic-response", {}).get("status", "")
        if status == "ok":
            logger.info("[NAVIDROME] Triggered Navidrome library scan for newly added file")
            return True
        logger.debug(f"[NAVIDROME] startScan returned status={status!r}")
        return False
    except Exception as e:
        logger.debug(f"[NAVIDROME] Could not trigger scan: {e}")
        return False


def check_track_exists_in_db(queue_item):
    """
    Check if a track matching the queue item already exists in the local tracks database.

    Returns:
        tuple: (exists: bool, reason: str, matched_track: dict|None)
    """
    artist = queue_item.get("artist", "")
    title = queue_item.get("title", "")
    album = queue_item.get("album")

    if not artist or not title:
        return False, "", None

    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        placeholder = _get_placeholder(conn)

        if album:
            cursor.execute(
                f"""
                SELECT id, title, artist, album, duration, file_path FROM tracks
                WHERE LOWER(artist) = LOWER({placeholder})
                  AND LOWER(title) = LOWER({placeholder})
                  AND LOWER(album) = LOWER({placeholder})
                  AND (file_path IS NULL OR file_path NOT LIKE '__queued_for_download__%')
                LIMIT 1
                """,
                (artist, title, album),
            )
        else:
            cursor.execute(
                f"""
                SELECT id, title, artist, album, duration, file_path FROM tracks
                WHERE LOWER(artist) = LOWER({placeholder})
                  AND LOWER(title) = LOWER({placeholder})
                  AND (file_path IS NULL OR file_path NOT LIKE '__queued_for_download__%')
                LIMIT 1
                """,
                (artist, title),
            )

        row = cursor.fetchone()
        if row:
            matched = dict(row) if hasattr(row, "keys") else {
                "id": row[0], "title": row[1], "artist": row[2], "album": row[3],
                "duration": row[4], "file_path": row[5]
            }
            db_file_path = matched.get("file_path") or ""
            # If the tracks row has a file_path, verify it still exists on disk.
            # Stale entries for deleted files must not block re-downloading.
            if db_file_path and not db_file_path.startswith("__queued_for_download__"):
                if not os.path.isfile(db_file_path):
                    logger.debug(
                        f"DB existence check: '{artist} - {title}' found in tracks table "
                        f"but file no longer on disk ({db_file_path}); skipping"
                    )
                    return False, "", None
            track_id = matched.get("id")
            reason = f"Track '{artist} - {title}' already exists in local database (track ID {track_id})"
            return True, reason, matched

    except Exception as e:
        logger.debug(f"DB existence check error for '{artist} - {title}': {e}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    return False, "", None


def check_track_exists_in_navidrome(queue_item):
    """
    Check if a track matching the queue item already exists in Navidrome via Subsonic search3 API.

    Returns:
        tuple: (exists: bool, reason: str, matched_song: dict|None)
    """
    artist = queue_item.get("artist", "")
    title = queue_item.get("title", "")

    if not artist or not title:
        return False, "", None

    base_url, username, password = _get_navidrome_config()
    if not base_url:
        logger.debug("Navidrome not configured — skipping Navidrome existence check")
        return False, "", None

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
            return False, "", None

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
                return True, reason, dict(song)

    except Exception as e:
        logger.debug(f"Navidrome existence check error for '{artist} - {title}': {e}")

    return False, "", None


def _is_confirmed_collection_match(queue_item, matched_data):
    """
    Returns True when the matched song/track data is a confirmed full match for the queue
    item based on all available criteria: title, artist, album name, and song duration.

    A confirmed match means the track in /music is definitively the same as the queued
    item, allowing safe auto-cleanup of the queue entry and any /downloads file.
    """
    if not matched_data:
        return False

    def _sim(a, b):
        if not a or not b:
            return 0.0
        return SequenceMatcher(None, str(a).lower().strip(), str(b).lower().strip()).ratio()

    # Title must match strongly
    if _sim(queue_item.get("title", ""), matched_data.get("title", "")) < _NAV_TITLE_SIMILARITY_THRESHOLD:
        return False

    # Artist must match strongly
    if _sim(queue_item.get("artist", ""), matched_data.get("artist", "")) < _NAV_ARTIST_SIMILARITY_THRESHOLD:
        return False

    # Album name must match when both sides have it
    q_album = (queue_item.get("album") or "").strip()
    m_album = (matched_data.get("album") or "").strip()
    if q_album and m_album:
        if _sim(q_album, m_album) < _ALBUM_SIMILARITY_THRESHOLD:
            return False
    elif q_album or m_album:
        # One side has album info and the other doesn't — cannot confirm
        return False

    # Duration must match within tolerance when both sides have it
    q_dur = _normalize_duration_seconds(queue_item.get("duration"))
    m_dur = _normalize_duration_seconds(matched_data.get("duration"))
    if q_dur and m_dur:
        if abs(q_dur - m_dur) > _CONFIRMED_MATCH_DURATION_TOLERANCE_SECONDS:
            return False
    elif q_dur or m_dur:
        # One side has duration and the other doesn't — cannot confirm
        return False

    return True


def _delete_confirmed_collection_item(queue_id, queue_item):
    """
    Auto-clean a confirmed in-collection queue item:
      1. Mark the queue row as 'deleted' in the database.
      2. Delete any associated file from /downloads.

    This is called when a track in /music fully matches the queue item on all
    four criteria (title, artist, album, duration), so keeping the entry would
    only produce spurious 'already in queue' messages.
    """
    artist = queue_item.get("artist", "")
    title = queue_item.get("title", "")

    # 1. Mark as deleted in the database
    try:
        conn = get_db()
        cursor = conn.cursor()
        placeholder = _get_placeholder(conn)
        cursor.execute(
            f"UPDATE download_queue SET status = 'deleted', updated_at = CURRENT_TIMESTAMP WHERE id = {placeholder}",
            (queue_id,),
        )
        conn.commit()
        conn.close()
        logger.info(
            f"Queue {queue_id}: ✅ Auto-deleted confirmed in-collection entry for '{artist} - {title}'"
        )
    except Exception as e:
        logger.error(f"Queue {queue_id}: Failed to mark as deleted: {e}")

    # 2. Delete any associated file from /downloads
    file_path = queue_item.get("file_path") or ""
    if file_path:
        abs_file = os.path.abspath(file_path)
        abs_downloads = os.path.abspath(DOWNLOADS_DIR)
        try:
            within_downloads = Path(abs_file).resolve().is_relative_to(Path(abs_downloads))
        except (AttributeError, ValueError):
            # Fallback for Python < 3.9 where is_relative_to is unavailable
            within_downloads = os.path.commonpath([abs_downloads, abs_file]) == abs_downloads
        if within_downloads and os.path.isfile(abs_file):
            try:
                os.remove(abs_file)
                logger.info(
                    f"Queue {queue_id}: 🗑️  Deleted /downloads file for confirmed in-collection track: {abs_file}"
                )
            except Exception as e:
                logger.warning(f"Queue {queue_id}: Could not delete /downloads file '{abs_file}': {e}")
    else:
        # Also try to find the file by found_filename in /downloads
        found_fn = queue_item.get("found_filename") or ""
        if found_fn:
            candidate_path = os.path.join(DOWNLOADS_DIR, os.path.basename(found_fn))
            abs_candidate = os.path.abspath(candidate_path)
            abs_downloads = os.path.abspath(DOWNLOADS_DIR)
            try:
                within_downloads = Path(abs_candidate).resolve().is_relative_to(Path(abs_downloads))
            except (AttributeError, ValueError):
                within_downloads = os.path.commonpath([abs_downloads, abs_candidate]) == abs_downloads
            if within_downloads and os.path.isfile(abs_candidate):
                try:
                    os.remove(abs_candidate)
                    logger.info(
                        f"Queue {queue_id}: 🗑️  Deleted /downloads file for confirmed in-collection track: {abs_candidate}"
                    )
                except Exception as e:
                    logger.warning(
                        f"Queue {queue_id}: Could not delete /downloads file '{abs_candidate}': {e}"
                    )


def _cleanup_sibling_downloads(queue_item, keep_path):
    """
    Delete any audio files in DOWNLOADS_DIR that match the same artist+title as
    *queue_item* but are NOT the file at *keep_path*.

    This removes stale copies that accumulated during previous failed download
    attempts (e.g. when a queue item retried several times and left orphaned files).

    Only files whose names contain both the artist and title strings are removed to
    avoid accidental deletion of unrelated files.
    """
    artist = (queue_item.get("artist") or "").lower().strip()
    title = (queue_item.get("title") or "").lower().strip()
    if not artist or not title:
        return

    abs_downloads = os.path.abspath(DOWNLOADS_DIR)
    keep_abs = os.path.abspath(keep_path) if keep_path else None

    if not os.path.isdir(abs_downloads):
        return

    removed = 0
    try:
        for root, _, files in os.walk(abs_downloads):
            for fname in files:
                if not fname.lower().endswith(('.mp3', '.flac', '.m4a', '.ogg', '.wav')):
                    continue
                full = os.path.abspath(os.path.join(root, fname))
                if keep_abs and full == keep_abs:
                    continue
                fname_lower = fname.lower()
                if artist in fname_lower and title in fname_lower:
                    try:
                        os.remove(full)
                        removed += 1
                        logger.info(
                            f"[DEDUP] Removed sibling download for "
                            f"'{queue_item.get('artist')} - {queue_item.get('title')}': {full}"
                        )
                    except Exception as rm_err:
                        logger.warning(f"[DEDUP] Could not remove sibling file '{full}': {rm_err}")
    except Exception as e:
        logger.error(f"[DEDUP] Error during sibling download cleanup: {e}")

    if removed:
        logger.info(
            f"[DEDUP] Cleaned {removed} sibling download(s) for "
            f"'{queue_item.get('artist')} - {queue_item.get('title')}'"
        )


def _safe_delete_download_candidate(file_path, reason, queue_id=None):
    """Delete a candidate file only when it is within DOWNLOADS_DIR."""
    if not file_path:
        return False

    try:
        abs_file = os.path.abspath(file_path)
        abs_downloads = os.path.abspath(DOWNLOADS_DIR)
        within_downloads = os.path.commonpath([abs_downloads, abs_file]) == abs_downloads
    except Exception:
        return False

    if not within_downloads or not os.path.isfile(abs_file):
        return False

    try:
        os.remove(abs_file)
        logger.warning(
            f"Queue {queue_id or 'unknown'}: deleted unmatched Soulseek file {abs_file} "
            f"(reason={reason})"
        )
        return True
    except Exception as delete_err:
        logger.warning(
            f"Queue {queue_id or 'unknown'}: could not delete unmatched Soulseek file "
            f"{abs_file}: {delete_err}"
        )
        return False


def _find_location_match_in_music(queue_item):
    """Return /music path when queue source path maps to an existing library file."""
    source_path = (queue_item.get('file_path') or '').strip()
    if not source_path:
        return None

    try:
        abs_source = os.path.abspath(source_path)
        abs_downloads = os.path.abspath(DOWNLOADS_DIR)
        rel = os.path.relpath(abs_source, abs_downloads)
    except Exception:
        return None

    if not rel or rel.startswith('..'):
        return None

    music_root = os.path.abspath(os.environ.get('MUSIC_ROOT', '/music'))
    candidate = os.path.abspath(os.path.join(music_root, rel))
    if os.path.exists(candidate):
        return candidate
    return None


def search_and_download(queue_id, queue_item, client):
    """Search Soulseek for queue item and download top result"""
    try:
        search_query = _get_effective_track_search_query(queue_item)
        if not search_query:
            search_query = str(queue_item.get('search_query') or '').strip()

        # Location-first collection check: only trust concrete path matches.
        location_match = _find_location_match_in_music(queue_item)
        if location_match:
            logger.info(f"Queue {queue_id}: ⏭️  Skipping download — location match found in /music: {location_match}")
            update_queue_status(
                queue_id,
                'in_collection',
                in_collection=1,
                matched_file_path=location_match,
                failure_reason=f"Location match in /music: {location_match}"
            )
            return False

        # By default we avoid metadata-only DB/Navidrome matching because it can
        # produce ambiguous "Matched" rows with no usable file path details.
        location_only_matching = os.environ.get('SPTNR_LOCATION_MATCH_ONLY', '1').strip() != '0'
        if not location_only_matching:
            db_exists, db_reason, db_matched = check_track_exists_in_db(queue_item)
            if db_exists:
                logger.info(f"Queue {queue_id}: ⏭️  Skipping download — {db_reason}")
                if _is_confirmed_collection_match(queue_item, db_matched):
                    logger.info(
                        f"Queue {queue_id}: 🎯 Confirmed full match in local DB "
                        f"(title+artist+album+duration) — auto-cleaning queue and /downloads"
                    )
                    _delete_confirmed_collection_item(queue_id, queue_item)
                else:
                    update_queue_status(queue_id, 'in_collection', failure_reason=db_reason)
                return False

            nav_exists, nav_reason, nav_matched = check_track_exists_in_navidrome(queue_item)
            if nav_exists:
                logger.info(f"Queue {queue_id}: ⏭️  Skipping download — {nav_reason}")
                if _is_confirmed_collection_match(queue_item, nav_matched):
                    logger.info(
                        f"Queue {queue_id}: 🎯 Confirmed full match in Navidrome "
                        f"(title+artist+album+duration) — auto-cleaning queue and /downloads"
                    )
                    _delete_confirmed_collection_item(queue_id, queue_item)
                else:
                    update_queue_status(queue_id, 'in_collection', failure_reason=nav_reason)
                return False
        else:
            logger.debug(f"Queue {queue_id}: location-only matching enabled; skipping metadata DB/Navidrome checks")

        logger.info(f"Queue {queue_id}: Searching for '{search_query}'...")
        update_queue_status(queue_id, 'searching')

        best_result = None
        best_score = 0.0
        poll_start_time = datetime.now()

        # Pass 1: album-level search first (when album metadata exists), then
        # choose a file that matches this queued track. This reduces repeated
        # per-track queries for album imports.
        album_query = _get_album_search_query(queue_item)
        album_titles = _get_album_queue_titles(queue_item)
        if album_query and album_titles:
            cache_key = f"album::{_normalize_match_text(album_query)}"
            cached = _album_search_cache.get(cache_key)
            album_responses = None

            if cached and (time.time() - cached.get('timestamp', 0)) <= _ALBUM_SEARCH_CACHE_TTL_SECONDS:
                album_responses = cached.get('responses') or []
            else:
                try:
                    logger.info(f"Queue {queue_id}: Album-first search '{album_query}'")
                    album_search_id = client.start_search(album_query)
                    if album_search_id:
                        album_responses = _poll_search_responses(client, album_search_id, max_poll_attempts=30)
                        _album_search_cache[cache_key] = {
                            'timestamp': time.time(),
                            'responses': album_responses or [],
                        }
                except Exception as album_search_err:
                    logger.debug(f"Queue {queue_id}: album-first search failed: {album_search_err}")

            if album_responses:
                best_album_result, best_album_score = _pick_best_candidate_from_responses(album_responses, queue_item)
                if best_album_result and best_album_score >= 0.45:
                    best_result = best_album_result
                    best_score = best_album_score
                    logger.info(
                        f"Queue {queue_id}: Album-first match selected "
                        f"(score={best_album_score:.2f})"
                    )

        # Pass 2: fallback to per-track search when album pass was inconclusive.
        if not best_result:
            search_id = client.start_search(search_query)
            if not search_id:
                logger.warning(f"Queue {queue_id}: Failed to start Soulseek search")
                mark_failed(queue_id, "Failed to start Soulseek search", schedule_retry=True)
                return False

            # Increased timeout to 45 seconds to handle slow Soulseek peer responses
            responses = _poll_search_responses(client, search_id, max_poll_attempts=45)
            best_result, best_score = _pick_best_candidate_from_responses(responses, queue_item)
        
        if not best_result:
            elapsed = (datetime.now() - poll_start_time).total_seconds()
            logger.warning(f"Queue {queue_id}: ✗ No results found after {elapsed:.0f}s of polling")
            mark_failed(queue_id, f"No results found for '{search_query}'", schedule_retry=True, retry_delay_minutes=60)
            return False

        if best_score < 0.45:
            elapsed = (datetime.now() - poll_start_time).total_seconds()
            logger.warning(
                f"Queue {queue_id}: ✗ Results found but no safe match for '{search_query}' "
                f"(best_score={best_score:.2f}, elapsed={elapsed:.0f}s)"
            )
            mark_failed(
                queue_id,
                f"No safe Soulseek match for '{search_query}' (best_score={best_score:.2f})",
                schedule_retry=True,
                retry_delay_minutes=60,
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

        # Prepare optional helpers once so they are always defined for the full
        # function scope, including exception paths.
        dq_update_queue_item = None
        move_single_track_to_music_dir = None
        verify_downloaded_file_metadata = None
        verify_file_in_music = None
        mark_queue_item_moved = None

        try:
            from download_queue_manager import (
                move_single_track_to_music_dir,
                update_queue_item as dq_update_queue_item,
                verify_downloaded_file_metadata,
            )
            from download_file_verification import verify_file_in_music, mark_queue_item_moved
        except Exception as helper_import_err:
            logger.debug(f"Could not import auto-move helpers; using fallback queue updates only: {helper_import_err}")

        def _safe_update_queue_item(queue_id, **kwargs):
            """Prefer download_queue_manager.update_queue_item; fallback to local status update."""
            if dq_update_queue_item:
                return dq_update_queue_item(queue_id, **kwargs)

            # Fallback path only supports status/file_path updates.
            status = kwargs.get('status')
            file_path = kwargs.get('file_path')
            if status is not None:
                return update_queue_status(queue_id, status, file_path=file_path)
            return None

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
                return (datetime.now() - updated_dt.replace(tzinfo=None)).total_seconds() >= (stale_minutes * 60)
            except Exception:
                return False

        def _state_normalize(value):
            return str(value or "").strip().lower()

        slskd_client = None
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
        fs_files: list[str] = []
        if os.path.isdir(DOWNLOADS_DIR):
            try:
                for root, _, root_files in os.walk(DOWNLOADS_DIR):
                    for f in root_files:
                        if f.lower().endswith(('.mp3', '.flac')):
                            fs_files.append(os.path.relpath(os.path.join(root, f), DOWNLOADS_DIR))
                if fs_files:
                    logger.debug(f"Filesystem walk: {len(fs_files)} audio files in {DOWNLOADS_DIR}")
            except Exception as e:
                logger.error(f"Error scanning downloads folder: {e}")
        else:
            logger.warning(f"Downloads directory does not exist: {DOWNLOADS_DIR}")

        # ------------------------------------------------------------------
        # Fetch all items currently in 'downloading' status
        # ------------------------------------------------------------------
        cursor.execute("""
            SELECT * FROM download_queue
            WHERE status = 'downloading'
        """)
        downloading = [dict(row) for row in cursor.fetchall()]
        if downloading:
            logger.debug(f"Checking {len(downloading)} items in 'downloading' status")

        # Build an active queue snapshot once so we can determine whether a
        # rejected Soulseek file is unmatched against the entire queue, not
        # just the current row being processed.
        cursor.execute("""
            SELECT * FROM download_queue
            WHERE status IN ('queued', 'searching', 'downloading', 'matched', 'unmatched', 'completed')
        """)
        active_queue_items = [dict(row) for row in cursor.fetchall()]

        def _matches_any_queue_item(file_path, relative_name=None, exclude_queue_id=None):
            if not file_path or not os.path.isfile(file_path):
                return False

            for candidate_item in active_queue_items:
                candidate_id = candidate_item.get('id')
                if exclude_queue_id is not None and candidate_id == exclude_queue_id:
                    continue
                try:
                    is_match, _ = _file_matches_queue_item(file_path, candidate_item, relative_name)
                except Exception:
                    continue
                if is_match:
                    return True
            return False

        newly_completed = []
        for item in downloading:
            match_found = None
            match_meta_state = None
            item_source = (item.get("source") or "soulseek").strip().lower()

            found_fn = item.get("found_filename") or ""
            item_id = item["id"]

            # 1. Exact match via slskd localFilePath (most reliable)
            if found_fn:
                found_norm = _normalize_transfer_key(found_fn)
                # Distinguish full-path vs basename-only hits so we can apply
                # different trust levels below.
                abs_path_full = slskd_completed.get(found_norm)
                abs_path = abs_path_full or slskd_completed.get(os.path.basename(found_norm))
            else:
                abs_path_full = None
                abs_path = None

            if abs_path:
                candidate_rel = os.path.relpath(abs_path, DOWNLOADS_DIR)
                # Guard: if slskd's download root differs from DOWNLOADS_DIR the
                # relative path would escape with "..".  Skip and let the
                # filesystem walk handle it.
                if candidate_rel.startswith('..'):
                    logger.warning(
                        f"Queue {item_id}: slskd localFilePath is outside DOWNLOADS_DIR, "
                        f"skipping: {abs_path}"
                    )
                elif abs_path is abs_path_full and abs_path_full:
                    # Full remote-path match: abs_path came directly from the
                    # full-path lookup, meaning the found_filename stored for this
                    # queue item matches the filename in slskd's completed-transfer
                    # record exactly.  slskd is telling us this specific file was
                    # downloaded for this queue item — trust it unconditionally.
                    # Metadata/filename matching can incorrectly reject valid files
                    # when the downloaded track lacks embedded tags or has a minimal
                    # filename (e.g. just a track number like "01.mp3").
                    match_found = candidate_rel
                    match_meta_state = 'slskd_localpath'
                    logger.debug(
                        f"Queue {item_id}: matched via slskd localFilePath (full path): {abs_path}"
                    )
                else:
                    # Basename-only match: weaker signal — multiple downloads can
                    # share the same basename, so verify with metadata/filename to
                    # avoid false positives.
                    is_match, match_source = _file_matches_queue_item(abs_path, item, candidate_rel)
                    if is_match:
                        match_found = candidate_rel
                        match_meta_state = match_source
                        logger.debug(
                            f"Queue {item_id}: matched via slskd localFilePath (basename): {abs_path}"
                        )
                    else:
                        logger.info(
                            f"Queue {item_id}: rejecting slskd-completed file due to queue mismatch: {candidate_rel}"
                        )
                        if item_source == 'soulseek':
                            if not _matches_any_queue_item(abs_path, candidate_rel, exclude_queue_id=item_id):
                                _safe_delete_download_candidate(
                                    abs_path,
                                    reason="soulseek download unmatched against queue",
                                    queue_id=item_id,
                                )

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
                            if item_source == 'soulseek':
                                if not _matches_any_queue_item(file_path, rel_file, exclude_queue_id=item_id):
                                    _safe_delete_download_candidate(
                                        file_path,
                                        reason="soulseek filename mismatch against queue",
                                        queue_id=item_id,
                                    )
                            continue
                        match_found = rel_file
                        match_meta_state = match_source
                        break

            # 3. Fuzzy match against filesystem files
            if match_found is None:
                for filename in fs_files:
                    file_path = os.path.join(DOWNLOADS_DIR, filename)
                    is_match, match_source = _file_matches_queue_item(file_path, item, filename)
                    if is_match:
                        match_found = filename
                        match_meta_state = match_source
                        logger.debug(f"Queue {item_id}: fuzzy match found: {filename}")
                        break

            # 4. No file match found. Reconcile against live slskd transfers so
            # stale 'downloading' rows do not remain stuck forever.
            if match_found is None:
                if item_source == 'soulseek' and slskd_status_available:
                    found_fn = item.get("found_filename") or ""
                    transfer = _get_transfer_entry(found_fn)

                    if transfer:
                        transfer_state = transfer.get("state", "")
                        transfer_state_norm = _state_normalize(transfer_state)
                        failed_states_norm = {
                            _state_normalize(s)
                            for s in getattr(slskd_client, "FAILED_STATES", set())
                        }
                        is_failed_state = (
                            transfer_state_norm in failed_states_norm
                            or "failed" in transfer_state_norm
                            or "error" in transfer_state_norm
                            or "rejected" in transfer_state_norm
                            or "cancel" in transfer_state_norm
                            or "timeout" in transfer_state_norm
                        )
                        if is_failed_state:
                            logger.warning(
                                f"Queue {item_id}: slskd reports terminal failed state {transfer_state!r}, scheduling retry"
                            )
                            mark_failed(
                                item_id,
                                f"slskd transfer failed: {transfer_state}",
                                schedule_retry=True,
                                retry_delay_minutes=10,
                            )
                        elif (
                            transfer_state == getattr(slskd_client, "STATE_SUCCEEDED", None)
                            or "succeed" in transfer_state_norm
                            or transfer_state_norm in {"completed", "complete", "succeeded"}
                        ):
                            # slskd reports success but no local file was found — the file
                            # likely disappeared before matching completed.  Re-queue so it
                            # can be downloaded again.
                            logger.warning(
                                f"Queue {item_id}: slskd reports succeeded but no file found, scheduling retry"
                            )
                            mark_failed(
                                item_id,
                                "slskd transfer succeeded but local file not found",
                                schedule_retry=True,
                                retry_delay_minutes=10,
                            )
                        else:
                            # Active or unrecognised transfer state. Apply a per-state
                            # timeout so that downloads stuck indefinitely — e.g. the
                            # remote peer queued the file but never started sending it —
                            # are eventually cancelled and retried from a different source.
                            timeout_minutes = (
                                _SLSKD_ACTIVE_STATE_TIMEOUT_MINUTES.get(transfer_state)
                                or _SLSKD_ACTIVE_STATE_TIMEOUT_MINUTES.get(" ".join(str(transfer_state).split()))
                            )
                            if timeout_minutes and _is_stale_queue_item(item, stale_minutes=timeout_minutes):
                                logger.warning(
                                    f"Queue {item_id}: Download stuck in '{transfer_state}' state for "
                                    f">{timeout_minutes}min, cancelling and retrying"
                                )
                                transfer_id = transfer.get("id", "")
                                transfer_username = transfer.get("username", "")
                                if transfer_id and transfer_username:
                                    slskd_client.cancel_download(transfer_username, transfer_id, remove=True)
                                mark_failed(
                                    item_id,
                                    f"slskd download timed out in '{transfer_state}' state",
                                    schedule_retry=True,
                                    retry_delay_minutes=10,
                                )
                            else:
                                # Download may still be in progress or state is
                                # unrecognised — skip and re-evaluate next cycle.
                                continue
                        # Always advance to the next item after handling a transfer
                        # match — file matching above already failed so no further
                        # processing is needed in this iteration.
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

                elif item_source == 'soulseek' and _is_stale_queue_item(item, stale_minutes=10):
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
                elif item_source == 'qbittorrent' and _is_stale_queue_item(item, stale_minutes=20):
                    # qBittorrent items that do not produce local files in a timely
                    # way are switched to Soulseek for a deterministic fallback path.
                    _fallback_queue_item_to_soulseek(
                        item_id,
                        "qBittorrent download stale with no local file",
                        retry_delay_minutes=1,
                    )
                    continue

            if match_found:
                file_path = os.path.join(DOWNLOADS_DIR, match_found)
                if match_meta_state == 'metadata':
                    logger.info(
                        f"Queue {item_id}: matched file '{match_found}' by metadata — marking as completed"
                    )
                else:
                    logger.info(
                        f"Queue {item_id}: matched file '{match_found}' by filename/path — marking as completed"
                    )
                update_queue_status(item_id, 'completed', file_path=file_path, found_filename=match_found)

                # Only auto-move items that were explicitly added via the MusicBrainz
                # search UI.  Fuzzy-matched downloads without MusicBrainz backing must
                # wait for the user to approve them in the Downloads UI.
                if not _is_musicbrainz_backed(item):
                    logger.info(
                        f"[AUTO_MOVE] Queue {item_id}: not from MusicBrainz search — "
                        f"leaving as completed for manual approval"
                    )
                else:
                    # Atomically claim this item for moving before starting any file
                    # operations.  If the UI Move button (or another processor cycle)
                    # already claimed it, we skip rather than double-moving the file.
                    try:
                        from download_queue_manager import _try_claim_for_move, _release_move_claim
                        _claim_fn = _try_claim_for_move
                        _release_fn = _release_move_claim
                    except Exception:
                        _claim_fn = None
                        _release_fn = None

                    if _claim_fn and not _claim_fn(item_id, 'completed'):
                        logger.info(
                            f"[AUTO_MOVE] Queue {item_id}: already claimed by another "
                            f"process for moving — skipping auto-move"
                        )
                    else:
                        # Immediately move the file to /music
                        try:
                            if not move_single_track_to_music_dir or not verify_downloaded_file_metadata:
                                if _claim_fn:
                                    _release_fn(item_id, restore_status='completed', file_path=file_path)
                                raise RuntimeError("Auto-move helpers unavailable")

                            # ── Step 1: Extract duration from file and persist it ──────────
                            # We do this before verification so the queue item's duration
                            # is populated for the metadata check below.
                            if not item.get('duration') and MutagenFile is not None:
                                try:
                                    audio = MutagenFile(file_path)
                                    if audio is not None and audio.info and hasattr(audio.info, 'length'):
                                        file_duration = _normalize_duration_seconds(audio.info.length)
                                        if file_duration:
                                            _safe_update_queue_item(item_id, duration=file_duration)
                                            # Refresh item dict so the verification step sees the
                                            # newly-stored duration.
                                            item = dict(item)
                                            item['duration'] = file_duration
                                            logger.debug(
                                                f"Queue {item_id}: updated duration from file to {file_duration}s"
                                            )
                                except Exception as dur_err:
                                    logger.debug(f"Queue {item_id}: could not extract duration from file: {dur_err}")

                            # ── Step 2: Verify file metadata matches the queue item ────────
                            # For filename-only matches, a tag mismatch blocks the metadata
                            # clear (Step 3) — the existing tags are preserved and merged
                            # rather than replaced.  For all other match types it remains a
                            # warning so the file is not stranded in /downloads.
                            meta_mismatch_for_filename_match = False
                            try:
                                meta_check = verify_downloaded_file_metadata(file_path, item)
                                if not meta_check['ok']:
                                    if match_meta_state == 'filename':
                                        logger.warning(
                                            f"[AUTO_MOVE] Queue {item_id}: filename-only match but "
                                            f"existing tags conflict with queue item — "
                                            f"{meta_check['reason']} "
                                            f"(detail={meta_check['detail']}) — "
                                            f"will merge stored metadata without clearing existing tags"
                                        )
                                        meta_mismatch_for_filename_match = True
                                    else:
                                        logger.warning(
                                            f"[AUTO_MOVE] Queue {item_id}: metadata verification "
                                            f"WARNING — {meta_check['reason']} "
                                            f"(detail={meta_check['detail']}) — proceeding with move"
                                        )
                                else:
                                    logger.debug(
                                        f"[AUTO_MOVE] Queue {item_id}: metadata OK "
                                        f"(detail={meta_check['detail']})"
                                    )
                            except Exception as verify_err:
                                logger.debug(f"[AUTO_MOVE] Queue {item_id}: metadata check skipped: {verify_err}")

                            # ── Step 3: Write stored MusicBrainz metadata to file tags ────
                            # Apply the metadata captured at queue-creation time (track
                            # number, artist, album, year, etc.) to the file before it is
                            # moved.  move_single_track_to_music_dir will further enrich
                            # the tags (cover art, per-track artist from MB release) on top
                            # of this baseline, but we guarantee the stored metadata is
                            # written even if the live MB fetch below fails.
                            # For filename-only matches whose tags contradicted the queue
                            # item, merge rather than clear to avoid overwriting the
                            # original tag data with potentially wrong information.
                            should_clear_tags = not meta_mismatch_for_filename_match
                            try:
                                from post_download_processor import update_file_metadata_with_albumart
                                stored_metadata = {
                                    'title': item.get('title'),
                                    'artist': item.get('artist'),
                                    'album_artist': item.get('album_artist') or item.get('artist'),
                                    'album': item.get('album'),
                                    'year': item.get('year'),
                                    'track_number': item.get('track_number'),
                                    'disc_number': item.get('disc_number'),
                                }
                                update_file_metadata_with_albumart(
                                    file_path, stored_metadata, clear_existing_tags=should_clear_tags
                                )
                                logger.info(
                                    f"[AUTO_MOVE] Queue {item_id}: applied stored MusicBrainz metadata to file "
                                    f"(clear_existing_tags={should_clear_tags})"
                                )
                            except Exception as meta_err:
                                logger.warning(
                                    f"[AUTO_MOVE] Queue {item_id}: could not apply stored metadata before move: {meta_err}"
                                )

                            # ── Step 4: Move file to /music, enrich tags via live MB API ──
                            item_for_move = dict(item)
                            item_for_move['file_path'] = file_path
                            move_result = move_single_track_to_music_dir(item_for_move)
                            if move_result['success']:
                                target_path = move_result['target_path']

                                # ── Step 5: Verify the file arrived at the destination ────
                                verify_result = verify_file_in_music(item_id, target_path)
                                if verify_result['success']:
                                    mark_queue_item_moved(item_id, target_path)
                                    _safe_update_queue_item(
                                        item_id,
                                        status='imported',
                                        file_path=target_path,
                                        copied_individually=1,
                                        copied_individually_at=datetime.now().isoformat()
                                    )
                                    logger.info(f"[AUTO_MOVE] Queue {item_id}: verified and imported to {target_path}")

                                    # ── Step 6: Remove siblings and trigger Navidrome ─────
                                    # _cleanup_sibling_downloads removes other downloads of
                                    # the same track that accumulated across retries.
                                    _cleanup_sibling_downloads(item, keep_path=None)
                                    _trigger_navidrome_scan()
                                else:
                                    logger.warning(
                                        f"[AUTO_MOVE] Queue {item_id}: file verification failed after move to {target_path}, updating path"
                                    )
                                    _safe_update_queue_item(item_id, status='completed', file_path=target_path)
                            else:
                                logger.warning(
                                    f"[AUTO_MOVE] Queue {item_id}: move to music dir failed, releasing claim"
                                )
                                if _claim_fn:
                                    _release_fn(item_id, restore_status='completed', file_path=file_path)
                        except Exception as e:
                            logger.error(f"[AUTO_MOVE] Queue {item_id}: error during auto-move: {e}")
                            if _claim_fn:
                                _release_fn(item_id, restore_status='completed', file_path=file_path)
                            else:
                                _safe_update_queue_item(item_id, status='completed', file_path=file_path)

        conn.close()
    except Exception as e:
        logger.error(f"Error in check_completed_downloads: {e}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def process_matched_items(limit=5):
    """
    Automatically move queue items in 'matched' status to the music directory.

    'matched' items have a confirmed file-to-MusicBrainz mapping (set either by
    the user via the Downloads UI or by the auto-discovery workflow) but have not
    yet been moved to /music.  Without this function they remain stuck in
    'matched' status indefinitely unless the user manually presses the Move
    button.

    Returns:
        int: number of items successfully moved and marked as 'imported'.
    """
    try:
        conn = get_db()
        cursor = conn.cursor()
        placeholder = _get_placeholder(conn)

        cursor.execute(
            f"""
            SELECT *
            FROM download_queue
            WHERE status = 'matched'
              AND (
                TRIM(COALESCE(matched_file_path, '')) != ''
                OR TRIM(COALESCE(file_path, '')) != ''
              )
            ORDER BY updated_at ASC
            LIMIT {placeholder}
            """,
            (limit,),
        )
        rows = cursor.fetchall() or []
        items = [dict(row) for row in rows]
        conn.close()

    except Exception as e:
        logger.error(f"[MATCHED_MOVE] Error fetching matched items: {e}")
        return 0

    if not items:
        return 0

    try:
        from download_queue_manager import _try_claim_for_move, _release_move_claim
        _claim_fn = _try_claim_for_move
        _release_fn = _release_move_claim
    except Exception:
        _claim_fn = None
        _release_fn = None

    processed = 0
    for item in items:
        item_id = item.get('id')
        if not item_id:
            continue

        # Atomically claim the item so the UI Move button and this loop cannot
        # both move the same file simultaneously.
        if _claim_fn and not _claim_fn(item_id, 'matched'):
            logger.debug(
                f"[MATCHED_MOVE] Queue {item_id}: already claimed by another process — skipping"
            )
            continue

        try:
            from download_monitor_enhancements import move_to_music_collection
            result = move_to_music_collection(item_id)

            if 'error' in result:
                logger.warning(
                    f"[MATCHED_MOVE] Queue {item_id}: move failed — {result['error']}"
                )
                if _release_fn:
                    _release_fn(item_id, restore_status='matched')
            else:
                # move_to_music_collection sets status='completed' internally;
                # promote immediately to 'imported' to be consistent with the
                # auto-move flow in check_completed_downloads().
                try:
                    from download_queue_manager import update_queue_item as dq_update
                    dq_update(
                        item_id,
                        status='imported',
                        copied_individually=1,
                        copied_individually_at=datetime.now().isoformat(),
                    )
                except Exception:
                    update_queue_status(item_id, 'imported')

                logger.info(
                    f"[MATCHED_MOVE] Queue {item_id}: moved to music and marked as imported: "
                    f"{result.get('path')}"
                )
                _trigger_navidrome_scan()
                processed += 1

        except Exception as move_err:
            logger.error(f"[MATCHED_MOVE] Queue {item_id}: error during move: {move_err}")
            if _release_fn:
                _release_fn(item_id, restore_status='matched')

    return processed


def process_queue(client):
    """Process queued download items"""
    try:
        promote_stale_queried_items(min_age_seconds=120, limit=200)

        items = claim_queued_items(limit=10)
        processed = 0
        for item in items:
            try:
                source = (item.get('source') or 'soulseek').strip().lower()
                if source == 'qbittorrent':
                    if search_and_download_qbittorrent(item['id'], item):
                        processed += 1
                else:
                    if not client:
                        logger.error("SlskdClient not available, skipping Soulseek queue item")
                        break
                    if search_and_download(item['id'], item, client):
                        processed += 1
            except Exception as e:
                logger.error(f"Error processing queue {item['id']}: {e}")
                mark_failed(item['id'], f"Processing error: {str(e)}", schedule_retry=True)

        # Always check for completed downloads, even if no new items were processed
        # This ensures downloads that complete between processing cycles are detected
        check_completed_downloads()

        # Process matched items (files confirmed by user or auto-discovery but not
        # yet moved to /music).  Without this, 'matched' items remain stuck forever
        # unless the user manually clicks Move in the Downloads UI.
        try:
            matched_processed = process_matched_items(limit=5)
            if matched_processed > 0:
                logger.info(f"[MATCHED_MOVE] Processed {matched_processed} matched item(s)")
        except Exception as e:
            logger.error(f"[MATCHED_MOVE] Error processing matched items: {e}")

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
    interval_seconds = 600

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

    if interval_seconds < 120:
        interval_seconds = 120

    return enabled, interval_seconds


def maybe_auto_discover_files(now_ts, last_run_ts):
    """Run background auto-discovery on interval and return updated last-run timestamp."""
    enabled, interval_seconds = _load_auto_discovery_settings()
    if not enabled:
        return last_run_ts

    # Avoid a heavy discovery run immediately on process startup unless explicitly requested.
    run_on_start = str(os.environ.get("DOWNLOADS_AUTO_DISCOVER_RUN_ON_START", "0")).strip().lower() in {"1", "true", "yes", "on"}
    if last_run_ts is None and not run_on_start:
        logger.info("[AUTO-DISCOVER] Startup run skipped; first run in %ss", interval_seconds)
        return now_ts

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


def maybe_check_musicbrainz_files(now_ts, last_run_ts, interval_seconds=30):
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


def maybe_finalize_musicbrainz_releases(now_ts, last_run_ts, interval_seconds=60):
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


def maybe_check_missing_moved_files(now_ts, last_run_ts, interval_seconds=300):
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

def maybe_clear_slskd_completed_downloads(now_ts, last_run_ts, interval_seconds=1800):
    """
    Periodically remove all completed and failed download entries from slskd's
    transfer list.

    Stale entries — especially failed ones — accumulate over time and can
    interfere with re-downloading the same file from the same peer (slskd may
    refuse or deduplicate the new request against an existing failed entry).
    Clearing them every 30 minutes (default) keeps slskd's queue clean and
    ensures retried downloads start with a fresh slate.

    The first run is intentionally skipped on process startup (when last_run_ts
    is None) so that check_completed_downloads() has a chance to process any
    transfers that completed before the processor started, before those entries
    are removed from slskd's transfer list.

    Args:
        now_ts: Current time.time() value.
        last_run_ts: Timestamp of the last cleanup run (None = never run).
        interval_seconds: Minimum seconds between runs (default 1800 = 30 min).

    Returns:
        Updated last-run timestamp.
    """
    if last_run_ts is None:
        # Skip the very first run so that check_completed_downloads() can read
        # any already-completed transfers from slskd before they are cleared.
        logger.debug(
            "[SLSKD_CLEANUP] Startup run skipped; first cleanup in %s seconds", interval_seconds
        )
        return now_ts

    if (now_ts - last_run_ts) < interval_seconds:
        return last_run_ts

    try:
        slskd_client = get_slskd_client()
        if slskd_client:
            cleared = slskd_client.clear_completed_downloads()
            if cleared:
                logger.info("[SLSKD_CLEANUP] Cleared completed/failed download entries from slskd")
            else:
                logger.debug("[SLSKD_CLEANUP] clear_completed_downloads returned False (no entries or API unavailable)")
    except Exception as e:
        logger.error(f"[SLSKD_CLEANUP] Error clearing slskd completed downloads: {e}")

    return now_ts


def maybe_trigger_navidrome_scan_for_new_imports(now_ts, last_run_ts, interval_seconds=300):
    """Periodically check whether new tracks were recently imported and, when so,
    trigger a Navidrome library scan so they appear in Navidrome without requiring
    a manual full-import.

    The check is intentionally lightweight — it queries only the count of queue
    items that transitioned to 'imported' within the last *interval_seconds* window
    and only calls Navidrome's ``startScan`` when at least one such item is found.

    This complements the immediate trigger fired in ``check_completed_downloads()``
    and acts as a safety net for cases where that call was missed (e.g. the service
    restarted between move and scan).

    Args:
        now_ts: Current time.time() value.
        last_run_ts: Timestamp of the last check (None = never run).
        interval_seconds: Minimum seconds between checks (default 300 = 5 min).

    Returns:
        Updated last-run timestamp.
    """
    if last_run_ts is not None and (now_ts - last_run_ts) < interval_seconds:
        return last_run_ts

    try:
        conn = get_db()
        cursor = conn.cursor()
        placeholder = _get_placeholder(conn)
        # Look for items imported within twice the check interval to avoid missing
        # items that fell just outside the previous window.
        lookback_seconds = interval_seconds * 2
        cutoff = (datetime.now() - timedelta(seconds=lookback_seconds)).isoformat()
        cursor.execute(
            f"""
            SELECT COUNT(*) FROM download_queue
            WHERE status = 'imported'
              AND updated_at >= {placeholder}
            """,
            (cutoff,),
        )
        row = cursor.fetchone()
        conn.close()
        count = row[0] if row else 0
        if count > 0:
            logger.debug(f"[NAVIDROME] {count} item(s) recently imported — triggering Navidrome scan")
            _trigger_navidrome_scan()
        else:
            logger.debug("[NAVIDROME] No recent imports; skipping Navidrome scan trigger")
    except Exception as e:
        logger.debug(f"[NAVIDROME] Error checking for recent imports: {e}")

    return now_ts


def run_processor(interval=30):
    """Run queue processor loop"""
    logger.info("=== Queue Processor Started ===")
    logger.info(f"Processing interval: {interval}s")

    # Self-heal queue schema even when process startup bypasses entrypoint.sh.
    try:
        migration_script = os.path.join(os.path.dirname(__file__), "migrations", "startup_queue_columns_fast.py")
        if os.path.exists(migration_script):
            proc = subprocess.run(
                [sys.executable, migration_script],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if proc.stdout:
                logger.info(f"[STARTUP_MIGRATION] {proc.stdout.strip()}")
            if proc.stderr:
                logger.debug(f"[STARTUP_MIGRATION] stderr: {proc.stderr.strip()}")
            if proc.returncode != 0:
                raise RuntimeError(
                    f"startup_queue_columns_fast.py failed with exit code {proc.returncode}"
                )
        else:
            logger.debug("[STARTUP_MIGRATION] startup_queue_columns_fast.py not found, skipping")
    except Exception as migration_err:
        logger.warning(f"[STARTUP_MIGRATION] Could not run startup queue migration: {migration_err}")
    
    client = get_slskd_client()
    if not client:
        logger.warning("Soulseek (slskd) is not configured or not enabled — queue processor will run without Soulseek support")
    
    loop_count = 0
    last_auto_discover_ts = None
    last_mb_check_ts = None
    last_mb_finalize_ts = None
    last_verify_ts = None
    last_slskd_cleanup_ts = None
    last_navidrome_scan_ts = None

    try:
        while not _shutdown_requested:
            try:
                loop_count += 1
                logger.debug(f"--- Loop {loop_count} ---")

                now_ts = time.time()
                last_auto_discover_ts = maybe_auto_discover_files(now_ts, last_auto_discover_ts)
                last_mb_check_ts = maybe_check_musicbrainz_files(now_ts, last_mb_check_ts)
                last_mb_finalize_ts = maybe_finalize_musicbrainz_releases(now_ts, last_mb_finalize_ts)
                last_verify_ts = maybe_check_missing_moved_files(now_ts, last_verify_ts)
                last_slskd_cleanup_ts = maybe_clear_slskd_completed_downloads(now_ts, last_slskd_cleanup_ts)
                last_navidrome_scan_ts = maybe_trigger_navidrome_scan_for_new_imports(now_ts, last_navidrome_scan_ts)

                processed = process_queue(client)

                if processed > 0:
                    logger.info(f"Processed {processed} queue items")

                if _shutdown_requested:
                    break

                # Interruptible sleep: wake early if SIGTERM was received
                for _ in range(interval):
                    if _shutdown_requested:
                        break
                    time.sleep(1)

            except KeyboardInterrupt:
                logger.info("Queue processor stopped by user")
                break
            except Exception as e:
                logger.error(f"Error in processor loop: {e}")
                logger.error(traceback.format_exc())
                if not _shutdown_requested:
                    time.sleep(interval)

    except KeyboardInterrupt:
        logger.info("Queue processor interrupted")
    finally:
        logger.info("=== Queue Processor Stopped ===")

if __name__ == "__main__":
    # Default interval is 30 seconds
    interval = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    run_processor(interval)
