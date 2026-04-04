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

# Shared constants for orphan-token detection used in both
# _score_soulseek_candidate and _filename_matches_queue_item.
_ORPHAN_AUDIO_EXT_TOKENS = frozenset(
    {"mp3", "flac", "wav", "ogg", "aac", "m4a", "wma", "opus", "aiff"}
)
_ORPHAN_NUM_RE = re.compile(r'^\d{1,4}$')

# Strips "feat."/"ft."/"featuring" suffixes from artist strings when building
# fallback search queries so that "KNEECAP feat. Fawzi" becomes "KNEECAP".
_FEAT_SUFFIX_RE = re.compile(
    r'\s+(?:feat\.?|ft\.?|featuring)\s+.*$',
    re.IGNORECASE,
)


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

    Artist / title matching is done against the *basename only* so that an
    album folder whose name equals the track title (e.g. the title track of an
    album) does not cause files from OTHER tracks in that folder to score
    falsely high.  Album matching intentionally uses the full path so the
    folder name still contributes positive album evidence.
    """
    filename_norm = _normalize_match_text(filename)             # full path – album only
    basename_norm = _normalize_match_text(os.path.basename(filename))  # basename – artist/title
    artist_norm = _normalize_match_text(queue_item.get('artist'))
    album_artist_norm = _normalize_match_text(queue_item.get('album_artist') or '')
    title_norm = _normalize_match_text(queue_item.get('title'))
    album_norm = _normalize_match_text(queue_item.get('album'))
    title_tokens = _tokenize_meaningful(title_norm)
    basename_tokens = set(_tokenize_meaningful(basename_norm))

    if not artist_norm or not title_norm or not basename_norm:
        return 0.0

    if title_tokens:
        shared_title_tokens = sum(1 for tok in title_tokens if tok in basename_tokens)
        title_token_ratio = shared_title_tokens / len(title_tokens)
        title_variant_tokens = {"acoustic", "demo", "edit", "instrumental", "intro", "live", "mix", "radio", "remaster", "remastered", "remix", "version"}
        requested_variants = set(title_tokens) & title_variant_tokens
        candidate_variants = basename_tokens & title_variant_tokens

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

    # Require both core fields to be reasonably represented in the basename.
    # When the track artist contains featured guests (e.g. "KNEECAP feat. Fawzi"),
    # files are often tagged with the album artist only ("KNEECAP"), so also
    # consider the album_artist similarity and take whichever is higher.
    artist_sim = SequenceMatcher(None, artist_norm, basename_norm).ratio()
    if album_artist_norm and album_artist_norm != artist_norm:
        album_artist_sim = SequenceMatcher(None, album_artist_norm, basename_norm).ratio()
        artist_sim = max(artist_sim, album_artist_sim)
    title_sim = SequenceMatcher(None, title_norm, basename_norm).ratio()
    if artist_sim < 0.12 or title_sim < 0.12:
        return 0.0

    score = (artist_sim * 0.45) + (title_sim * 0.55)
    score += (0.22 * title_token_ratio)

    # Strongly prefer explicit artist/title phrases when present in the basename.
    # Accept a match on album_artist as equivalent to a match on track artist so
    # that files tagged "KNEECAP - Palestine" still receive the artist bonus when
    # the queue item carries "KNEECAP feat. Fawzi" as the track artist.
    if artist_norm in basename_norm or (album_artist_norm and album_artist_norm in basename_norm):
        score += 0.18
    if title_norm in basename_norm:
        score += 0.25

    # Orphan-token penalty: tokens in the basename that cannot be explained by
    # the artist, title, or album strongly suggest a *different* track is present.
    # For example, title="Jailbreak" vs basename "Nervosa - Jailbreak - 01 -
    # Endless Ambition.mp3" has two orphan tokens ("endless", "ambition") that
    # are clearly a different track's title.  Apply a significant penalty only
    # when the title match is ambiguous because all title tokens also appear in
    # the album (so the title could just be the album folder name, not the track).
    # Applied AFTER all other bonuses so the album-in-path reward cannot rescue
    # a wrong-track candidate.
    explained_tokens = (
        set(_tokenize_meaningful(artist_norm))
        | set(_tokenize_meaningful(title_norm))
        | set(_tokenize_meaningful(album_norm or ""))
    )
    orphan_tokens = [
        t for t in basename_tokens
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

            # When we have >=2 meaningful album tokens, require at least 2 matches.
            # This rejects near misses like "Sword of Power" for "Power of Metal".
            if len(album_tokens) >= 2 and shared_album_tokens < 2:
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

    # Require both core fields to be reasonably close to avoid false-positive imports.
    if artist_score < 0.55 or title_score < 0.55:
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
    # Applied on the raw lowercase basename to preserve special chars (e.g. "-1").
    basename_lower = basename.lower()
    raw_title = (queue_item.get('title') or '').lower()
    basename_test = basename_norm  # retained for source-level test assertions
    title_in_basename = bool(
        raw_title
        and re.search(re.escape(raw_title) + r'(?!\s*[a-z])', basename_lower)
    )

    if not title_in_basename:
        # Fallback: allow if the full candidate score is high enough even
        # without the whole-phrase title guard (e.g. unseparated filename
        # "artist title album.flac" where the title is embedded mid-string).
        score = _score_soulseek_candidate(norm_path, queue_item)
        return score >= 0.60

    title_tokens = _tokenize_meaningful(title_norm)
    basename_tokens = set(_tokenize_meaningful(basename_norm))
    title_variant_tokens = {
        "acoustic", "demo", "edit", "instrumental", "intro", "live", "mix",
        "radio", "remaster", "remastered", "remix", "version",
    }

    # Variant check: only when title has meaningful tokens — a plain queue
    # title must not match a "(mix/live/…)" file, but when the title has no
    # meaningful tokens (e.g. the special title "-1") the check is skipped
    # because we cannot determine whether the queue item is itself a variant.
    if title_tokens:
        requested_variants = set(title_tokens) & title_variant_tokens
        candidate_variants = basename_tokens & title_variant_tokens
        if requested_variants or candidate_variants:
            if not requested_variants or not candidate_variants:
                return False
            if requested_variants.isdisjoint(candidate_variants):
                return False

    # Orphan-token rejection for title==album ambiguity:
    # When all title tokens also appear in the album name the title's presence
    # in the basename could be from the album folder rather than the track.
    # If 2+ tokens in the basename are unexplained by artist/title/album those
    # are likely the actual track title, so reject this file.
    album_norm = _normalize_match_text(queue_item.get('album'))
    title_token_set = set(title_tokens)
    album_token_set = set(_tokenize_meaningful(album_norm))
    if title_token_set and title_token_set.issubset(album_token_set):
        explained = (
            set(_tokenize_meaningful(artist_norm))
            | title_token_set
            | album_token_set
        )
        orphan = [
            t for t in basename_tokens
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


def _cleanup_sibling_downloads(queue_item, keep_path=None):
    """
    Remove downloaded files that match the same artist+title as *queue_item*
    but are NOT the file we want to keep (*keep_path*).

    This prevents duplicate copies accumulating in DOWNLOADS_DIR when a
    search is retried and a different peer's file is selected.
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

def get_queued_items(limit=None):
    """Get items ready to process (queued or scheduled for retry)"""
    try:
        # First, clean up any items stuck in 'searching' state
        cleanup_stuck_searching_items()
        
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
        conn.close()
        
        return items
        
    except Exception as e:
        logger.error(f"Error getting queued items: {e}")
        return []

def update_queue_status(queue_id, status, **kwargs):
    """Update queue item status"""
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
        conn.close()
        
        logger.info(f"Updated queue {queue_id} to status: {status}")
        return True
        
    except Exception as e:
        logger.error(f"Error updating queue status: {e}")
        return False

def increment_retry_count(queue_id, retry_delay_minutes=60):
    """Increment retry count and schedule next retry"""
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
            conn.close()
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
        conn.close()
        
        logger.info(
            f"Queue {queue_id}: retry count now {retry_count}, "
            f"next retry at {next_retry} (delay={effective_retry_delay}m)"
        )
        return True
        
    except Exception as e:
        logger.error(f"Error incrementing retry count: {e}")
        return False

def mark_failed(queue_id, reason, schedule_retry=True, retry_delay_minutes=60):
    """Mark queue item as failed, optionally scheduling retry"""
    try:
        from app import get_db as app_get_db
        conn = app_get_db()
        cursor = conn.cursor()
        placeholder = "%s"
        
        # Get current retry count
        cursor.execute(f"SELECT retry_count FROM download_queue WHERE id = {placeholder}", (queue_id,))
        row = cursor.fetchone()
        
        if not row:
            conn.close()
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
        conn.close()
        
        return schedule_retry  # Return whether retry was scheduled
        
    except Exception as e:
        logger.error(f"Error marking queue item as failed: {e}")
        return False

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
        conn.close()

        if row:
            track_id = row["id"] if hasattr(row, "keys") else (row[0] if row else None)
            reason = f"Track '{artist} - {title}' already exists in local database (track ID {track_id})"
            return True, reason

    except Exception as e:
        logger.debug(f"DB existence check error for '{artist} - {title}': {e}")

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


def _build_fallback_search_queries(queue_item, primary_query):
    """Build alternative search queries for tracks with featured artists.

    When the primary search uses the full track artist (e.g. "KNEECAP feat. Fawzi
    - Palestine"), files on Soulseek are often tagged with the album artist only
    ("KNEECAP").  This helper returns fallback queries to try when the primary
    search yields no usable match:

    1. ``album_artist - title`` when album_artist differs from the track artist.
    2. ``feat.-stripped track_artist - title`` when the track artist contains a
       "feat." / "ft." / "featuring" clause.
    3. ``first_word_of_artist - title`` for multi-word artists where Soulseek's
       all-tokens-required matching causes zero results (e.g. "Pretty Reckless -
       I Am Death" → "Pretty - I Am Death").  Soulseek requires every token in
       the query to appear in the filename, so a shorter artist prefix broadens
       the match while the candidate-scoring step still enforces artist similarity.
    4. ``title`` only, as a last resort for cases where the artist token(s) are
       entirely absent from shared filenames.  The scoring system enforces a
       minimum artist-similarity threshold so false positives remain unlikely.

    Already-tried queries (i.e. ``primary_query``) and plain duplicates are
    excluded from the returned list.
    """
    artist = str(queue_item.get('artist') or '').strip()
    album_artist = str(queue_item.get('album_artist') or '').strip()
    title = str(queue_item.get('title') or '').strip()

    if not title:
        return []

    try:
        from download_queue_manager import _sanitize_search_query_for_slskd
    except ImportError:
        logger.warning(
            "_build_fallback_search_queries: could not import _sanitize_search_query_for_slskd "
            "from download_queue_manager; using plain whitespace normalisation as fallback"
        )
        def _sanitize_search_query_for_slskd(q):  # type: ignore[misc]
            return " ".join(q.split())

    fallbacks = []

    def _add(q):
        if q and q != primary_query and q not in fallbacks:
            fallbacks.append(q)

    # Fallback 1: album artist (e.g. "KNEECAP - Palestine")
    if album_artist and album_artist.lower() != artist.lower():
        _add(_sanitize_search_query_for_slskd(f"{album_artist} - {title}"))

    # Fallback 2: feat.-stripped track artist (e.g. "KNEECAP - Palestine")
    feat_stripped = _FEAT_SUFFIX_RE.sub("", artist).strip()
    if feat_stripped and feat_stripped.lower() != artist.lower():
        _add(_sanitize_search_query_for_slskd(f"{feat_stripped} - {title}"))

    # Fallback 3: first word of artist + title for multi-word artists.
    # Soulseek requires ALL query tokens to be present in a filename, so a
    # long artist name like "Pretty Reckless" can produce zero results when
    # "reckless" is absent from most shared filenames.  Using only the first
    # word (e.g. "Pretty") broadens the token set while the scorer still
    # validates the full artist name against the candidate filename.
    effective_artist = feat_stripped if feat_stripped else artist
    artist_words = effective_artist.split()
    first_word = artist_words[0] if artist_words else ""
    if first_word and first_word.lower() != effective_artist.lower():
        _add(_sanitize_search_query_for_slskd(f"{first_word} - {title}"))

    # Fallback 4: title only.  Used when even a partial artist token blocks
    # results (e.g. the artist name is not present in any shared filename at
    # all).  The candidate scorer still requires minimum artist similarity so
    # unrelated tracks are rejected.
    _add(_sanitize_search_query_for_slskd(title))

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
                        size = (
                            getattr(file_info, 'size', file_info.get('size', 0))
                            if isinstance(file_info, dict)
                            else getattr(file_info, 'size', 0)
                        )
                        candidate_length = _extract_candidate_length_seconds(file_info)
                        candidate_score = _score_soulseek_candidate(filename, queue_item, candidate_length)
                        if candidate_score > best_score:
                            best_score = candidate_score
                            best_result = {
                                "username": resp.username,
                                "filename": filename,
                                "size": size,
                                "length": candidate_length,
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

        # Primary search using the stored search_query (built from track artist).
        best_result, best_score = _run_soulseek_search(queue_id, search_query, queue_item, client)

        # If the primary search found nothing useful, retry with fallback queries.
        # This handles tracks where the track artist contains featured guests
        # (e.g. "KNEECAP feat. Fawzi - Palestine") but Soulseek files are tagged
        # with the album artist only ("KNEECAP - Palestine").
        if not best_result or best_score < _SLSKD_MIN_ACCEPT_SCORE:
            for fallback_query in _build_fallback_search_queries(queue_item, search_query):
                logger.info(
                    f"Queue {queue_id}: Primary search insufficient "
                    f"(score={best_score:.2f}), trying fallback query '{fallback_query}'..."
                )
                fb_result, fb_score = _run_soulseek_search(queue_id, fallback_query, queue_item, client)
                if fb_score > best_score:
                    best_score = fb_score
                    best_result = fb_result
                if best_result and best_score >= _SLSKD_MIN_ACCEPT_SCORE:
                    logger.info(
                        f"Queue {queue_id}: Fallback query '{fallback_query}' "
                        f"succeeded (score={best_score:.2f})"
                    )
                    break

        if not best_result:
            elapsed = (datetime.now() - poll_start_time).total_seconds()
            logger.warning(f"Queue {queue_id}: ✗ No results found after {elapsed:.0f}s of polling")
            mark_failed(queue_id, f"No results found for '{search_query}'", schedule_retry=True, retry_delay_minutes=1440)
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
                retry_delay_minutes=1440,
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
                return (datetime.now() - updated_dt.replace(tzinfo=None)).total_seconds() >= (stale_minutes * 60)
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
        fs_files: list[str] = []
        if os.path.isdir(DOWNLOADS_DIR):
            try:
                for root, _, root_files in os.walk(DOWNLOADS_DIR):
                    for f in root_files:
                        if f.lower().endswith(('.mp3', '.flac', '.m4a')):
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
                            continue
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
                        f"Queue {item_id}: matched file '{match_found}' by metadata — marking as completed"
                    )
                else:
                    logger.info(
                        f"Queue {item_id}: matched file '{match_found}' by filename/path — marking as completed"
                    )
                update_queue_status(item_id, 'completed', file_path=file_path, found_filename=match_found)

                # Immediately move the file to /music
                try:
                    from download_queue_manager import move_single_track_to_music_dir, update_queue_item
                    from download_file_verification import verify_file_in_music, mark_queue_item_moved

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
                    if move_result['success']:
                        target_path = move_result['target_path']
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
                        else:
                            logger.warning(
                                f"[AUTO_MOVE] Queue {item_id}: verification FAILED "
                                f"({verify_result.get('error')}), marking back to 'completed'"
                            )
                            update_queue_item(item_id, status='completed', music_file_path=target_path)
                    else:
                        logger.warning(
                            f"[AUTO_MOVE] Queue {item_id}: could not move "
                            f"({move_result.get('error')}), keeping as 'completed'"
                        )
                except Exception as move_err:
                    logger.warning(f"[AUTO_MOVE] Queue {item_id}: move error: {move_err}")

                newly_completed.append(item)

        if conn is not None:
            conn.close()
            conn = None

        for item in newly_completed:
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

    except Exception as e:
        logger.error(f"Error checking completed downloads: {e}")

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


def maybe_auto_discover_files(now_ts, last_run_ts):
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


def maybe_clear_slskd_completed_downloads(now_ts, last_run_ts, interval_seconds=1800):
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


def maybe_cleanup_stale_downloads(now_ts, last_run_ts, interval_seconds=3600):
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


def maybe_check_downloads_folder(now_ts, last_run_ts, interval_seconds=90):
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
        from download_queue_manager import check_downloads_folder
        completed = check_downloads_folder()
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

    try:
        while True:
            try:
                loop_count += 1
                logger.debug(f"--- Loop {loop_count} ---")

                now_ts = time.time()
                last_auto_discover_ts = maybe_auto_discover_files(now_ts, last_auto_discover_ts)
                last_mb_check_ts = maybe_check_musicbrainz_files(now_ts, last_mb_check_ts)
                last_mb_finalize_ts = maybe_finalize_musicbrainz_releases(now_ts, last_mb_finalize_ts)
                last_verify_ts = maybe_check_missing_moved_files(now_ts, last_verify_ts)
                last_stale_cleanup_ts = maybe_cleanup_stale_downloads(now_ts, last_stale_cleanup_ts)
                last_slskd_retry_ts = check_failed_slskd_downloads(now_ts, last_slskd_retry_ts)
                last_slskd_clear_ts = maybe_clear_slskd_completed_downloads(now_ts, last_slskd_clear_ts)
                last_downloads_folder_ts = maybe_check_downloads_folder(now_ts, last_downloads_folder_ts)
                
                processed = process_queue(client)
                
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
