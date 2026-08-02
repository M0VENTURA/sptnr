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
import re
import time

from difflib import SequenceMatcher
from typing import Any, Dict

from services.downloads.slskd_service import SlskdService
from services.enrichment.musicbrainz_service import (
    fetch_musicbrainz_release_metadata,
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

logger = logging.getLogger(__name__)


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
    """Build a structured search query for slskd.

    Produces targeted queries that reduce noise:
    - For album downloads: ``Artist - Album Year``
    - For track downloads: ``Artist - Title``
    - Strips problematic punctuation for slskd compatibility.
    - For generic artists (Various Artists, etc.) uses title as the query.
    """
    from helpers.config_helpers import _GENERIC_COMPILATION_ARTISTS, _FEAT_SUFFIX_RE

    artist = (item.get("artist") or "").strip()
    title = (item.get("title") or "").strip()
    album = (item.get("album") or "").strip()
    year = (item.get("year") or item.get("release_year") or "")

    # If artist is a generic compilation name, use title as the search query
    # instead of "Various Artists - Song" which returns poor results on slskd.
    if artist.lower() in _GENERIC_COMPILATION_ARTISTS:
        query = title
    else:
        # Strip "feat." suffixes for a cleaner query
        clean_artist = _FEAT_SUFFIX_RE.sub("", artist).strip()

        if album:
            query = f"{clean_artist} - {album}"
            if year:
                query += f" {year}"
        else:
            query = f"{clean_artist} - {title}"

    return _sanitize_slskd_query(query)


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
    started_at = time.time()

    try:
        # ✅ mark processing
        mark_processing(queue_id)

        # ✅ search (allow lower bitrates since scoring handles quality)
        results = slskd.search_and_filter(query, min_bitrate=192)

        elapsed = round(time.time() - started_at, 1)

        if not results:
            _log_search_event(
                search_type="automatic",
                query=query,
                queue_id=queue_id,
                item=item,
                result_count=0,
                duration_seconds=elapsed,
                notes="no_results",
            )
            mark_failed(queue_id, "no_results")
            return {"success": False, "status": "no_results"}

        # ✅ score and pick best match
        best = _select_best_result(
            results,
            expected_artist=expected_artist,
            expected_title=expected_title,
            expected_album=expected_album,
            expected_duration=expected_duration,
        )

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
                result_count=len(results),
                duration_seconds=elapsed,
                notes=f"no_qualifying_result: candidates={len(results)}",
                selected_result=best,
                results=results,
            )
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
            mark_failed(queue_id, "download_failed")
            return {"success": False, "status": "download_failed"}

        # ✅ success → downloading (final completion is confirmed by the
        # downloads watcher when the file actually lands on disk).
        update_queue_item(
            queue_id,
            found_filename=best["filename"],
            status="downloading",
        )

        return {
            "success": True,
            "status": "downloading",
            "query": query,
            "match": best
        }

    except Exception as e:
        logger.error("[PIPELINE] Error processing %s: %s", queue_id, e, exc_info=True)
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

        mb_data = fetch_musicbrainz_release_metadata(release_id)

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
            release_id,
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
            release_id,
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