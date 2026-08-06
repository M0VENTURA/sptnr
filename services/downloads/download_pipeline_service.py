"""Download pipeline service.

Orchestrates the end-to-end download processing pipeline:
1. Fetch ready-to-process queue items.
2. Resolve MusicBrainz release metadata.
3. Build structured search queries for slskd.
4. Score and rank results to pick the best match.
5. Execute downloads via slskd.
6. Update queue status and library records.

Coordinates between ``downloads/slskd_service``, ``enrichment/musicbrainz_service``,
and ``db/repositories``.
"""

from __future__ import annotations

import logging
import os
import re
import time

from difflib import SequenceMatcher
from typing import Any, Dict

from services.downloads.slskd_service import SlskdService
from services.enrichment.musicbrainz_service import (
    fetch_musicbrainz_release_metadata,
    fetch_release_metadata,
    resolve_release_id,
)
from db.repositories.queue import (
    update_queue_item,
    mark_failed,
    mark_processing,
    get_ready_for_processing,
)
from db.repositories.library import upsert_musicbrainz_release
from services.infrastructure.filesystem_service import (
    create_monitoring_folder,
)

from services.queue.queue_processing_service import add_release_tracks_to_queue
from helpers.logging_config import log_unified

logger = logging.getLogger(__name__)

# =============================================================================
# PEER FAILURE MEMORY
# =============================================================================

# Peers that recently rejected/failed a download request. Failed queue items
# are retried on a schedule; without remembering which peer+file failed, the
# retry re-searches and picks the exact same peer that just rejected the
# download, failing again in a loop ("the transfers keep failing").
# ``(username, filename) -> blocked-until`` (unix time).
_BLOCKED_PEER_TTL_SECONDS = int(
    os.environ.get("SLSKD_BLOCKED_PEER_TTL_SECONDS", "7200")
)
_blocked_peers: dict[tuple[str, str], float] = {}


def _block_peer(username: str | None, filename: str | None) -> None:
    """Remember that *username* rejected/failed this file for *ttl* seconds."""
    if not username or not filename:
        return
    _blocked_peers[(str(username), str(filename))] = time.time() + _BLOCKED_PEER_TTL_SECONDS


def _is_peer_blocked(username: str | None, filename: str | None) -> bool:
    if not username or not filename:
        return False
    key = (str(username), str(filename))
    blocked_until = _blocked_peers.get(key)
    if blocked_until is None:
        return False
    if blocked_until < time.time():
        _blocked_peers.pop(key, None)
        return False
    return True


def _filter_blocked_peers(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop search results from peers that recently failed this download."""
    if not _blocked_peers:
        return results
    return [
        r for r in results
        if not _is_peer_blocked(r.get("username"), r.get("filename"))
    ]


# =============================================================================
# TEXT NORMALISATION HELPERS
# =============================================================================

def _normalise(text: str) -> str:
    """Lower-case, strip, and collapse whitespace for comparison."""
    return re.sub(r"\s+", " ", text.lower()).strip()


def _similarity(a: str, b: str) -> float:
    """Return a 0–1 similarity score between two strings."""
    return SequenceMatcher(None, _normalise(a), _normalise(b)).ratio()


def _parse_filename_parts(filename: str) -> dict[str, str | None]:
    """Try to extract artist, album, title, bitrate, and format from a Soulseek filename.

    Common patterns found on Soulseek:
        Artist - Title.mp3
        Artist - Album - 01 - Title.flac
        Artist\Album\01 Title.mp3
        Various - Artist - Title (Year).mp3
    """
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]  # strip path, keep filename
    name = name.rsplit(".", 1)[0] if "." in name else name  # strip extension

    result: dict[str, str | None] = {
        "artist": None,
        "album": None,
        "title": None,
        "has_track_number": False,
        "format": None,
    }

    # Detect format from original filename
    ext_match = re.search(r"\.(flac|mp3|wav|aac|ogg|wma|m4a|opus)$", filename.lower())
    if ext_match:
        result["format"] = ext_match.group(1)

    # Pattern: "Artist - Album - 01 - Title.flac" or "Artist - 01 - Title.mp3"
    parts = re.split(r"\s*-\s*", name)
    parts = [p.strip() for p in parts if p.strip()]

    if len(parts) >= 3:
        # Check if second-to-last part is a track number
        track_match = re.match(r"^(\d{1,3})(?:\s|$)", parts[-2])
        if track_match:
            result["artist"] = parts[0]
            result["album"] = parts[1] if len(parts) >= 4 else None
            result["title"] = parts[-1]
            result["has_track_number"] = True
        else:
            result["artist"] = parts[0]
            result["title"] = parts[-1]
    elif len(parts) == 2:
        result["artist"] = parts[0]
        result["title"] = parts[1]

    # If nothing worked, try "TrackNumber Title" pattern with no dash
    if not result["artist"]:
        fallback = re.match(r"^(\d{1,3})\s+(.+)", name)
        if fallback:
            result["title"] = fallback.group(2).strip()
            result["has_track_number"] = True

    return result


# Regex for characters slskd's tokenizer handles poorly
_SLSKD_PROBLEMATIC_PUNCT_RE = re.compile(r"[\u2018\u2019\u201A\u201B\u2039\u203A'\u201C\u201D\u201E\u201F\u2033\u2036]")

# =============================================================================
# QUERY BUILDER
# =============================================================================

def _sanitize_slskd_query(query: str) -> str:
    """Strip characters that Soulseek's search tokenizer mishandles.

    Removes apostrophes and curly/typographic quote characters while keeping
    hyphens, slashes, parentheses, and other legitimate characters.
    """
    if not query:
        return query
    cleaned = _SLSKD_PROBLEMATIC_PUNCT_RE.sub("", query)
    return " ".join(cleaned.split())


def build_search_query(item: dict) -> str:
    """Build the primary structured search query for slskd.

    Matches the legacy queue processor: track-level ``Artist - Title`` search
    (using the stored ``search_query`` when present) rather than an album-level
    ``Artist - Album`` search.  Album-level searching misses tracks that peers
    share as individual files, which is why the manual ``Artist - Title`` scan
    finds matches the automatic lookup does not.

    - Prefers the stored ``search_query`` (``Artist - Title``) when available.
    - Falls back to ``Artist - Title`` built from the item fields.
    - Strips problematic punctuation for slskd compatibility.
    - For generic artists (Various Artists, etc.) uses title as the query.
    """
    from helpers.config_helpers import _GENERIC_COMPILATION_ARTISTS, _FEAT_SUFFIX_RE

    artist = (item.get("artist") or "").strip()
    title = (item.get("title") or "").strip()
    stored = (item.get("search_query") or "").strip()

    # If artist is a generic compilation name, use title as the search query
    # instead of "Various Artists - Song" which returns poor results on slskd.
    if artist.lower() in _GENERIC_COMPILATION_ARTISTS:
        query = title or stored
    else:
        # Strip "feat." suffixes for a cleaner query
        clean_artist = _FEAT_SUFFIX_RE.sub("", artist).strip()

        if stored:
            query = stored
        elif clean_artist:
            query = f"{clean_artist} - {title}"
        else:
            query = title

    return _sanitize_slskd_query(query)


def _build_fallback_search_queries(item: dict, primary_query: str) -> list[str]:
    """Build alternative queries mirroring the legacy ``_build_fallback_search_queries``.

    Soulseek requires every query token to appear in a shared filename/path,
    so the stored ``Artist - Title`` query can return zero results when peers
    tag files differently (album artist instead of track artist, feat.-suffixed
    artists, bracketed title annotations, multi-word artists).  These fallbacks
    are tried in order until one yields a qualifying match:
    - bracket-stripped title variants
    - album-artist + title
    - feat.-stripped artist + title
    - first meaningful word of artist + title
    - title only (broadest, last resort)
    """
    from helpers.config_helpers import _FEAT_SUFFIX_RE
    from helpers.normalization_service import strip_brackets

    artist = (item.get("artist") or "").strip()
    album_artist = (item.get("album_artist") or "").strip()
    title = (item.get("title") or "").strip()

    if not title:
        return []

    fallbacks: list[str] = []
    seen: set[str] = {primary_query}

    def _add(query: str) -> None:
        query = _sanitize_slskd_query(query)
        if query and query not in seen:
            seen.add(query)
            fallbacks.append(query)

    # Bracket-stripped title variants (e.g. "(Radio Edit)" annotations absent
    # from shared filenames).
    core_title = strip_brackets(title).strip()
    if core_title and core_title.lower() != title.lower():
        if artist:
            _add(f"{artist} - {core_title}")
        if album_artist and album_artist.lower() != artist.lower():
            _add(f"{album_artist} - {core_title}")
        _add(core_title)

    # Album artist + title (peers often tag the album artist only).
    if album_artist and album_artist.lower() != artist.lower():
        _add(f"{album_artist} - {title}")

    # Feat.-stripped artist + title (e.g. "KNEECAP feat. Fawzi" -> "KNEECAP").
    feat_stripped = _FEAT_SUFFIX_RE.sub("", artist).strip()
    if feat_stripped and feat_stripped.lower() != artist.lower():
        _add(f"{feat_stripped} - {title}")

    # First meaningful word of artist + title for multi-word artists
    # ("The Pretty Reckless - Heaven Knows" -> "Pretty - Heaven Knows").
    _ARTICLE_WORDS = {"the", "a", "an"}
    effective_artist = feat_stripped or artist
    first_word = ""
    for word in effective_artist.split():
        if word.lower() not in _ARTICLE_WORDS:
            first_word = word
            break
    if not first_word and effective_artist.split():
        first_word = effective_artist.split()[0]
    if first_word and first_word.lower() != effective_artist.lower():
        _add(f"{first_word} - {title}")

    # Title only, as a last resort.
    _add(title)

    return fallbacks


# =============================================================================
# RESULT SCORING
# =============================================================================

def _score_result(
    result: dict[str, Any],
    expected_artist: str,
    expected_title: str,
    expected_album: str | None = None,
    expected_duration: int | None = None,
) -> float:
    """Score a single slskd search result (0–100).

    Scoring criteria:
    - Filename contains expected artist (+30)
    - Filename contains expected title  (+25)
    - Filename contains expected album  (+20, if relevant)
    - Duration within 10% of expected   (+15, if available)
    - High bitrate bonus                (+10 for lossless, +5 for 320)
    - Penalty for format mismatch       (-10)
    """
    score = 0.0
    filename = str(result.get("filename", ""))
    parts = _parse_filename_parts(filename)

    # Artist match
    art_score = _similarity(str(parts.get("artist") or ""), expected_artist)
    if art_score > 0.7:
        score += 30 * min(1.0, art_score)
    elif _normalise(expected_artist) in _normalise(filename):
        score += 20  # partial match

    # Title match
    title_score = _similarity(str(parts.get("title") or ""), expected_title)
    if title_score > 0.7:
        score += 25 * min(1.0, title_score)
    elif _normalise(expected_title) in _normalise(filename):
        score += 15  # partial match

    # Album match (bonus if searching for an album)
    if expected_album:
        album_score = _similarity(str(parts.get("album") or ""), expected_album)
        if album_score > 0.6:
            score += 20 * min(1.0, album_score)
        elif _normalise(expected_album) in _normalise(filename):
            score += 10  # partial match

    # Duration match (requires MusicBrainz metadata)
    if expected_duration and result.get("length_seconds"):
        dur_ratio = min(expected_duration, result["length_seconds"]) / max(expected_duration, result["length_seconds"])
        if dur_ratio >= 0.9:
            score += 15 * dur_ratio

    # Quality bonus (prefer API-provided extension over parsed filename)
    bitrate = int(result.get("bitrate", 0) or 0)
    ext = (result.get("extension") or parts.get("format") or "").lower()

    is_lossless = bool(result.get("is_lossless")) or ext in ("flac", "wav", "aiff", "alac")
    if is_lossless:
        score += 10
    elif bitrate >= 320:
        score += 5
    elif bitrate > 0 and bitrate < 192:
        score -= 10  # low-bitrate penalty

    # Higher bit-depth bonus (24-bit > 16-bit)
    bit_depth = result.get("bit_depth")
    if bit_depth and is_lossless:
        if int(bit_depth) >= 24:
            score += 3

    # Free upload slot bonus
    if result.get("has_free_upload_slot"):
        score += 3

    # Shorter queue = faster download start
    qlen = result.get("queue_length")
    if qlen is not None:
        try:
            qlen = int(qlen)
            if qlen == 0:
                score += 5
            elif qlen <= 5:
                score += 3
            elif qlen > 50:
                score -= 2
        except (TypeError, ValueError):
            pass

    # Upload speed bonus (above average = more reliable)
    uspeed = result.get("upload_speed")
    if uspeed is not None:
        try:
            if int(uspeed) > 1_000_000:  # > 1 MB/s
                score += 3
        except (TypeError, ValueError):
            pass

    return round(score, 1)


def _select_best_result(
    results: list[dict[str, Any]],
    expected_artist: str,
    expected_title: str,
    expected_album: str | None = None,
    expected_duration: int | None = None,
    min_score: float = 30.0,
) -> dict[str, Any] | None:
    """Score all results and return the best match above the threshold."""
    scored: list[tuple[float, dict]] = []

    for r in results:
        s = _score_result(r, expected_artist, expected_title, expected_album, expected_duration)
        scored.append((s, r))

    scored.sort(key=lambda pair: -pair[0])  # descending by score

    if scored and scored[0][0] >= min_score:
        best_score, best = scored[0]
        logger.debug(
            "Best result score=%.1f for '%s' — pick from %d candidates",
            best_score, best.get("filename", "")[:80], len(scored),
        )
        return best

    logger.debug(
        "No result met min_score=%.1f for %s - %s (top score=%.1f)",
        min_score, expected_artist, expected_title, scored[0][0] if scored else 0,
    )
    return None


# =============================================================================
# PROCESS SINGLE ITEM
# =============================================================================

def process_queue_item(item: dict, slskd: SlskdService) -> dict:
    queue_id = item.get("id")
    logger.debug("[DOWNLOAD_PIPELINE] Processing queue item: %s", queue_id)

    if not queue_id:
        logger.error("[PIPELINE] Missing queue_id in item: %s", item)
        return {
            "success": False,
            "error": "missing_queue_id"
        }

    expected_artist = (item.get("artist") or "").strip()
    expected_title = (item.get("title") or "").strip()
    expected_album = (item.get("album") or "").strip() or None
    expected_duration = item.get("duration")
    if expected_duration:
        try:
            expected_duration = int(expected_duration)
        except (TypeError, ValueError):
            expected_duration = None

    query = build_search_query(item)
    fallback_queries = _build_fallback_search_queries(item, query)
    started_at = time.time()

    try:
        # ✅ mark processing
        mark_processing(queue_id)

        # ✅ search (allow lower bitrates since scoring handles quality).
        # Legacy parity: try the primary query first, then fallback queries
        # (bracket-stripped, album-artist, feat.-stripped, first-word,
        # title-only) until one yields a qualifying match.  The manual scan
        # matches on the same broad query variants, so without these the
        # automatic lookup fails even when results exist on the network.
        from helpers.config_helpers import _SLSKD_FALLBACK_SEARCH_MAX_WAIT_SECONDS

        best = None
        all_results: list[dict[str, Any]] = []
        searched_queries: list[str] = []

        for idx, q in enumerate([query] + fallback_queries):
            searched_queries.append(q)
            wait_seconds = None if idx == 0 else _SLSKD_FALLBACK_SEARCH_MAX_WAIT_SECONDS
            results = slskd.search_and_filter(q, min_bitrate=192, wait_seconds=wait_seconds)
            # Skip peers that recently rejected/failed this file so a retry picks
            # a different peer instead of failing on the same one repeatedly.
            results = _filter_blocked_peers(results)
            all_results.extend(results)

            best = _select_best_result(
                results,
                expected_artist=expected_artist,
                expected_title=expected_title,
                expected_album=expected_album,
                expected_duration=expected_duration,
            )
            if best:
                break

        elapsed = round(time.time() - started_at, 1)

        if not all_results:
            _log_search_event(
                search_type="automatic",
                query=query,
                queue_id=queue_id,
                item=item,
                result_count=0,
                duration_seconds=elapsed,
                notes="no_results",
            )
            log_unified(f"[QUEUE] {expected_artist} - {expected_title} → failed: no_results ({elapsed:.0f}s)")
            mark_failed(queue_id, "no_results")
            return {"success": False, "status": "no_results"}

        if not best:
            logger.info(
                "[PIPELINE] No qualifying result for queue %s (%s - %s)",
                queue_id, expected_artist, expected_title,
            )
            _log_search_event(
                search_type="automatic",
                query=query,
                queue_id=queue_id,
                item=item,
                result_count=len(all_results),
                duration_seconds=elapsed,
                notes=f"no_qualifying_result: candidates={len(all_results)}, queries={len(searched_queries)}",
                selected_result=best,
                results=all_results,
            )
            log_unified(f"[QUEUE] {expected_artist} - {expected_title} → failed: no_qualifying_result ({len(all_results)} candidates)")
            mark_failed(queue_id, "no_qualifying_result")
            return {"success": False, "status": "no_qualifying_result"}

        # ✅ Re-check the queue item still exists before requesting a download.
        # The Soulseek search can take up to 150s and the user may have removed
        # the item (or the album) from the queue in the meantime (legacy parity).
        if not _queue_item_exists(queue_id):
            logger.info(
                "[PIPELINE] Queue %s was removed while searching — skipping download request",
                queue_id,
            )
            return {"success": False, "status": "item_removed"}

        # ✅ Skip peers with no free upload slots (legacy parity).
        if not best.get("has_free_upload_slot", True):
            logger.warning(
                "[PIPELINE] Best match peer %s has 0 free slots — skipping download",
                best.get("username"),
            )
            _block_peer(best.get("username"), best.get("filename"))
            _log_search_event(
                search_type="automatic",
                query=query,
                queue_id=queue_id,
                item=item,
                result_count=len(results),
                duration_seconds=elapsed,
                notes="peer_no_free_slots",
                selected_result=best,
                results=results,
            )
            log_unified(f"[QUEUE] {expected_artist} - {expected_title} → failed: peer has no free upload slots")
            mark_failed(queue_id, "peer_no_free_slots")
            return {"success": False, "status": "peer_no_free_slots"}

        # ✅ update searching → downloading
        update_queue_item(queue_id, status="downloading")

        # ✅ download
        success = slskd.download_file(
            best["username"],
            best["filename"],
            size=int(best.get("size_mb", 0) * 1024 * 1024)
        )

        _log_search_event(
            search_type="automatic",
            query=query,
            queue_id=queue_id,
            item=item,
            result_count=len(results),
            duration_seconds=elapsed,
            notes=("queued" if success else "download_failed"),
            selected_result=best,
            results=results,
        )

        if not success:
            # Remember this peer so the retry does not immediately pick the
            # same peer that just rejected the download.
            _block_peer(best.get("username"), best.get("filename"))
            log_unified(f"[QUEUE] {expected_artist} - {expected_title} → failed: download_failed ({best.get('username')})")
            mark_failed(queue_id, "download_failed")
            return {"success": False, "status": "download_failed"}

        # ✅ success → downloading (final completion is confirmed by the
        # downloads watcher when the file actually lands on disk).
        update_queue_item(
            queue_id,
            found_filename=best["filename"],
            status="downloading",
        )
        log_unified(
            f"[QUEUE] {expected_artist} - {expected_title} → downloading from {best.get('username')} "
            f"({best.get('filename') or ''})"
        )

        return {
            "success": True,
            "status": "downloading",
            "query": query,
            "match": best
        }

    except Exception as e:
        logger.error("[PIPELINE] Error processing %s: %s", queue_id, e, exc_info=True)
        log_unified(f"[QUEUE] {expected_artist} - {expected_title} → failed: {e}")
        mark_failed(queue_id, str(e))
        return {"success": False, "error": str(e)}


def _log_search_event(
    *,
    search_type: str,
    query: str,
    queue_id: int | None,
    item: dict,
    result_count: int,
    duration_seconds: float | None = None,
    notes: str | None = None,
    selected_result: dict | None = None,
    results: list | None = None,
) -> None:
    """Write a Soulseek search event to ``slskd_search_logs`` (legacy parity).

    The old ``queue_processor`` logged every automatic search (success,
    no-results, no-safe-match). Restoring that here so the diagnostics UI
    and ``log_service`` show what the worker is doing.
    """
    try:
        from db.repositories.search_logs import log_slskd_search
        log_slskd_search(
            search_type=search_type,
            query=query,
            queue_id=queue_id,
            artist=(item.get("artist") or "").strip(),
            title=(item.get("title") or "").strip(),
            album=(item.get("album") or "").strip(),
            result_count=result_count,
            duration_seconds=duration_seconds,
            notes=notes,
            selected_result=selected_result,
            results=results,
        )
    except Exception as exc:
        logger.debug("[PIPELINE] Could not log slskd search event: %s", exc)


def _queue_item_exists(queue_id: int | None) -> bool:
    """Return True when the queue item still exists (legacy re-check)."""
    if queue_id is None:
        return False
    try:
        from sqlalchemy import text
        from db.engine import db_session
        with db_session() as session:
            result = session.execute(
                text("SELECT 1 FROM download_queue WHERE id = :qid"),
                {"qid": int(queue_id)},
            )
            return result.fetchone() is not None
    except Exception as exc:
        logger.debug("[PIPELINE] Item existence check failed for %s: %s", queue_id, exc)
        return True  # err on the side of not blocking a valid download


# =============================================================================
# BULK PROCESSOR
# =============================================================================

def run_pipeline(slskd: SlskdService, limit: int = 10) -> Dict[str, Any]:
    queue = get_ready_for_processing(limit)

    results = {
        "processed": 0,
        "success": 0,
        "failed": 0,
        "details": []
    }

    for item in queue:
        result = process_queue_item(item, slskd)

        results["processed"] += 1
        results["details"].append(result)

        if result.get("success"):
            results["success"] += 1
        else:
            results["failed"] += 1

    return results


# =============================================================================
# SYNC TRANSFERS
# =============================================================================

def sync_transfers(slskd: SlskdService) -> Dict[str, Any]:
    transfers = slskd.get_active_downloads()

    updated = 0

    for t in transfers:
        try:
            queue_id = t.get("queue_id")

            if not queue_id:
                continue

            update_queue_item(
                queue_id,
                status="downloading",
                progress=t.get("progress"),
                speed=t.get("speed"),
            )

            updated += 1

        except Exception as e:
            logger.debug(f"[SYNC] failed to update transfer: {e}")

    return {
        "success": True,
        "updated": updated,
        "total": len(transfers)
    }


# =============================================================================
# TRANSFER & VERIFY
# =============================================================================

def transfer_and_verify_download(
    source_path: str,
    dest_path: str,
    queue_id: int | None = None,
    *,
    convert_flac_to_mp3: bool = False,
    mp3_bitrate: int = 320,
) -> dict[str, Any]:
    """Move/convert a downloaded file into /music, then verify it landed safely.

    Handles FLAC->MP3 conversion when enabled.  After the move, checks that
    the file exists, is readable, and has non-zero size.
    """
    import os
    import subprocess

    from services.downloads.download_verification_service import (
        mark_queue_item_moved,
        verify_file_in_music,
    )
    from services.infrastructure.filesystem_service import transfer_download_to_music

    final_dest = dest_path
    source_ext = os.path.splitext(source_path)[1].lower()

    if convert_flac_to_mp3 and source_ext == ".flac":
        final_dest = os.path.splitext(dest_path)[0] + ".mp3"
        os.makedirs(os.path.dirname(final_dest), exist_ok=True)
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", source_path,
                 "-c:a", "libmp3lame", "-b:a", f"{mp3_bitrate}k",
                 "-id3v2_version", "3", final_dest],
                capture_output=True, timeout=300, check=True,
            )
            logger.info("Converted FLAC->MP3: %s -> %s", source_path, final_dest)
            try:
                os.remove(source_path)
            except Exception:
                pass
        except Exception as exc:
            logger.error("FLAC->MP3 conversion failed for %s: %s", source_path, exc)
            return {"success": False, "error": f"Conversion failed: {exc}"}
    else:
        tr = transfer_download_to_music(source_path, final_dest)
        if not tr.get("success"):
            return tr
        final_dest = tr.get("target_path", final_dest)

    if queue_id:
        mark_queue_item_moved(queue_id, final_dest)

    return verify_file_in_music(queue_id or 0, final_dest)


def start_release_download(release_id, release_title, artist, method='slskd'):

    try:
        logger.info(f"[START_DOWNLOAD] {release_id}")

        # The MB search UI hands over a release-group MBID (the search endpoint
        # returns release-groups). /ws/2/release/{id} 404s for a release-group
        # id, which would fail the fetch below and collapse the whole album
        # into a single "album as one track" queue entry. Resolve it to a
        # concrete release MBID first (legacy parity — old_system's
        # _fetch_release_payload did the same browse fallback).
        resolved_release_id = resolve_release_id(release_id)

        # Primary fetch via the raw WS/2 endpoint (rate-limited, sleeps 1s).
        mb_data = fetch_musicbrainz_release_metadata(resolved_release_id)

        # Fallback: the service-based fetch (MusicBrainzHttpClient) uses the
        # same WS/2 inc set and can succeed when the raw-httpx call was
        # rate-limited or hiccupped. Without this, a single transient fetch
        # failure degrades the whole album into a single un-downloadable
        # "album as one track" queue entry.
        if not mb_data:
            mb_data = fetch_release_metadata(resolved_release_id)

        if not mb_data:
            return {"success": False, "error": "MusicBrainz fetch failed"}

        release_year = mb_data.get("release_year")
        tracks = mb_data.get("tracks", [])
        total_tracks = len(tracks)

        release_album_artist = mb_data.get("artist") or artist

        monitoring_folder = create_monitoring_folder(
            artist, release_title, release_year
        )

        mb_release_db_id = upsert_musicbrainz_release(
            resolved_release_id,
            release_title,
            artist,
            release_year,
            total_tracks,
            monitoring_folder,
            method,
            album_artist=release_album_artist,
            release_source='musicbrainz',
        )

        queue_source = 'soulseek' if method.lower() == 'slskd' else 'qbittorrent'

        queue_ids = add_release_tracks_to_queue(
            resolved_release_id,
            tracks,
            artist,
            release_title,
            album_artist=release_album_artist,
            queue_source=queue_source,
        )

        return {
            "success": True,
            "mb_release_db_id": mb_release_db_id,
            "queue_items_created": len(queue_ids),
        }

    except Exception as e:
        logger.error("[START_DOWNLOAD] Failed: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}