"""
DOWNLOAD MATCHING SERVICE

Handles:
- Folder matching wrappers for queue/release matching
- MusicBrainz release match batch application
- MusicBrainz metadata prefetch after match assignment
- Optional release track expansion into queue rows
- Filename-to-queue-item matching for cleanup logic
- Folder/release scoring
- Auto-match suggestion logic

Architecture rules:
✅ Routes call this service
✅ This service may orchestrate DB updates and enrichment lookups
✅ MusicBrainz lookup stays in services.enrichment.musicbrainz_service
✅ Queue item creation is delegated to download_processing_service.add_to_queue
✅ Cleanup matching can call filename_matches_queue_item()
"""

from __future__ import annotations

import logging
import re
import threading

from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple


from db.utils import get_db_connection


from db.repositories.queue import (
    get_queue_match_targets,
    get_album_queue_tracks,
)
from db.repositories.tracks import (
    find_library_track,
)

from helpers.normalization_service import (
    normalize_match_text,
    extract_track_disc,
)

from services.enrichment.musicbrainz_service import (
    MusicBrainzService,
    fetch_musicbrainz_release_metadata,
    fetch_release_metadata,
)
from api_clients.musicbrainz_http import MUSICBRAINZ_UUID_RE

logger = logging.getLogger(__name__)


def _is_valid_mbid(value: str | None) -> bool:
    if not value:
        return False
    return bool(MUSICBRAINZ_UUID_RE.match(str(value).strip()))


def select_best_musicbrainz_candidate(candidates: list[dict[str, Any]] | None) -> tuple[dict[str, Any] | None, str | None]:
    """Return the first valid MusicBrainz candidate from a candidate list."""
    if not candidates:
        return None, None

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        source = (candidate.get("source") or "").strip().lower()
        candidate_id = candidate.get("id")
        if source == "musicbrainz" and _is_valid_mbid(candidate_id):
            return candidate, str(candidate_id).strip()

    return None, None


def match_folder_group_with_musicbrainz(
    folder_path: str,
    artist: str,
    album: str,
    allow_discogs_fallback: bool = False,
) -> dict[str, Any]:
    """Resolve MusicBrainz release candidates for a folder/artist/album match."""
    if not artist or not album:
        return {"success": True, "candidates": []}

    try:
        service = MusicBrainzService(enabled=True)
        groups = service.search_releasegroup_matches(artist, album, limit=5)
        candidates = []
        for group in groups:
            candidates.append(
                {
                    "id": group.get("id"),
                    "source": "musicbrainz",
                    "title": group.get("title"),
                    "artist": artist,
                    "date": None,
                    "match_score": group.get("match_score"),
                }
            )
        return {"success": True, "candidates": candidates}
    except Exception as exc:
        logger.debug("MusicBrainz release-group lookup failed: %s", exc)
        return {"success": False, "error": str(exc), "candidates": []}


def match_folder(folder_path: str, data: Dict[str, Any]) -> Any:
    return match_folder_group_with_musicbrainz(folder_path, data.get("artist", "") or "", data.get("album", "") or "", allow_discogs_fallback=False)


def auto_match_folder(folder_path: str, data: Dict[str, Any]) -> Any:
    return match_folder(folder_path, data)


def search_and_update_musicbrainz(queue_id: int, artist: str, title: str, album: str) -> dict[str, Any]:
    """Search MusicBrainz for a release match and update the queue item metadata."""
    if not album:
        return {"success": False, "error": "No album provided"}

    try:
        match_result = match_folder_group_with_musicbrainz("", artist, album, allow_discogs_fallback=False)
        releases = match_result.get("candidates", []) if isinstance(match_result, dict) else []
        candidate, release_mbid = select_best_musicbrainz_candidate(releases)

        if not candidate or not release_mbid:
            return {"success": False, "error": "No MusicBrainz match found"}

        release_year = None
        if candidate.get("date"):
            release_year = str(candidate.get("date"))[:4]

        release_artist = candidate.get("artist") or artist
        cover_art_url = candidate.get("cover_art_url") or candidate.get("cover_art") or ""

        if not cover_art_url:
            try:
                from api_clients.coverartarchive import get_release_image_from_caa

                cover_art_url = get_release_image_from_caa(release_mbid) or ""
            except Exception as exc:
                logger.debug("Cover Art Archive lookup failed for queue %s: %s", queue_id, exc)

        from db.repositories.queue import update_queue_item

        update_queue_item(
            queue_id,
            release_mbid=release_mbid,
            release_id=release_mbid,
            release_source="musicbrainz",
            release_year=release_year,
            album_artist=release_artist,
            cover_art_url=cover_art_url or None,
            status="matched",
        )

        return {
            "success": True,
            "queue_id": queue_id,
            "release_mbid": release_mbid,
            "release_year": release_year,
            "cover_art_url": cover_art_url or None,
        }
    except Exception as exc:
        logger.error("MusicBrainz auto-search failed for queue_id=%s: %s", queue_id, exc, exc_info=True)
        return {"success": False, "error": str(exc)}


# =============================================================================
# Optional MusicBrainz cache compatibility
# =============================================================================

try:
    from db.repositories.musicbrainz_cache import (
        get_cached_release_metadata,
        cache_release_metadata,
    )
except ImportError:
    get_cached_release_metadata = None
    cache_release_metadata = None


# =============================================================================
# Optional Discogs track fetcher
# =============================================================================

try:
    from services.queue.queue_matching_service import _fetch_discogs_tracks
except ImportError:
    fetch_discogs_tracks = None


# =============================================================================
# Small DB/helper utilities
# =============================================================================

def _row_get(row, key: str, index: int, default=None):
    """
    Safely read either dict-like DB rows or tuple/list rows.
    """

    if row is None:
        return default

    if hasattr(row, "get"):
        return row.get(key, default)

    try:
        return row[index]
    except Exception:
        return default


def _extract_release_year(raw_year) -> int | None:
    """
    Extract a 4-digit release year from MusicBrainz metadata.
    """

    if raw_year in (None, ""):
        return None

    match = re.search(r"(19|20)\d{2}", str(raw_year))

    if not match:
        return None

    try:
        return int(match.group(0))
    except Exception:
        return None


# =============================================================================
# Route/API wrappers
# =============================================================================


# =============================================================================
# MBID batch match application
# =============================================================================

def _prefetch_mbid_metadata_batch(
    mbid: str,
    queue_ids: List[int],
) -> None:
    """
    Fetch MusicBrainz release metadata and enrich already matched queue rows.

    Replaces old route-local dependency:
        from folder_matching_enhancements import get_musicbrainz_release_metadata

    Behaviour:
    - fetch release artist/title/year
    - bulk update release-level fields on queue rows
    - if queue rows already have recording_mbid, update track artist/title
      from release metadata where possible
    """

    if not mbid or not queue_ids:
        return

    try:
        release_meta = fetch_release_metadata(mbid) or {}

        if not release_meta:
            logger.debug(
                "[QUEUE_MATCH_BATCH] No release metadata found for MBID %s",
                mbid,
            )
            return

        release_artist = (release_meta.get("artist") or "").strip() or None
        release_title = (release_meta.get("release_title") or "").strip() or None
        release_year = _extract_release_year(release_meta.get("release_year"))

        tracks = release_meta.get("tracks") or []

        tracks_by_rec_mbid = {
            (track.get("recording_mbid") or track.get("id")): track
            for track in tracks
            if (track.get("recording_mbid") or track.get("id"))
        }

        release_updates: Dict[str, Any] = {}

        if release_artist:
            release_updates["album_artist"] = release_artist

        if release_title:
            release_updates["album"] = release_title

        if release_year is not None:
            release_updates["release_year"] = release_year

        conn = get_db_connection()
        cursor = conn.cursor()
        placeholder = "%s"

        try:
            # -----------------------------------------------------------------
            # Bulk update release-level fields
            # -----------------------------------------------------------------
            if release_updates:
                ids_placeholders = ", ".join([placeholder] * len(queue_ids))

                set_clause = ", ".join(
                    f"{column} = {placeholder}"
                    for column in release_updates.keys()
                )

                params = list(release_updates.values()) + list(queue_ids)

                cursor.execute(
                    f"""
                    UPDATE download_queue
                    SET {set_clause},
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id IN ({ids_placeholders})
                    """,
                    params,
                )

            # -----------------------------------------------------------------
            # Per-item track-level updates where recording_mbid is known
            # -----------------------------------------------------------------
            if tracks_by_rec_mbid:
                ids_placeholders = ", ".join([placeholder] * len(queue_ids))

                cursor.execute(
                    f"""
                    SELECT id, recording_mbid
                    FROM download_queue
                    WHERE id IN ({ids_placeholders})
                    """,
                    list(queue_ids),
                )

                rows = cursor.fetchall()

                for row in rows:
                    item_id = _row_get(row, "id", 0)
                    recording_mbid = (
                        _row_get(row, "recording_mbid", 1, "") or ""
                    ).strip()

                    if not item_id or not recording_mbid:
                        continue

                    track = tracks_by_rec_mbid.get(recording_mbid)

                    if not track:
                        continue

                    track_updates: Dict[str, Any] = {}

                    track_artist = (track.get("artist") or "").strip()
                    track_title = (
                        track.get("title")
                        or track.get("recording_title")
                        or ""
                    ).strip()

                    if track_artist:
                        track_updates["artist"] = track_artist

                    if track_title:
                        track_updates["title"] = track_title

                    if not track_updates:
                        continue

                    set_clause = ", ".join(
                        f"{column} = {placeholder}"
                        for column in track_updates.keys()
                    )

                    params = list(track_updates.values()) + [item_id]

                    cursor.execute(
                        f"""
                        UPDATE download_queue
                        SET {set_clause},
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = {placeholder}
                        """,
                        params,
                    )

            conn.commit()

            logger.info(
                "[QUEUE_MATCH_BATCH] Prefetched MB metadata for MBID %s: %s item(s), release fields=%s",
                mbid,
                len(queue_ids),
                sorted(release_updates.keys()),
            )

        except Exception:
            conn.rollback()
            raise

        finally:
            conn.close()

    except Exception as exc:
        logger.warning(
            "[QUEUE_MATCH_BATCH] Metadata prefetch failed for MBID %s: %s",
            mbid,
            exc,
        )


def _expand_release_tracks(
    mbid: str,
    artist_name: str,
    album_name: str,
) -> None:
    """
    Expand a MusicBrainz release into queue rows.

    Replaces old route-local dependency:
        from download_queue_manager import add_to_queue

    Import is intentionally inside the function to avoid circular imports.
    """

    if not mbid:
        return

    try:
        from services.downloads.download_processing_service import add_to_queue

        release_data = fetch_release_metadata(mbid) or {}
        release_tracks = release_data.get("tracks") or []

        inferred_year_raw = release_data.get("release_year")
        inferred_year = (
            str(inferred_year_raw).strip()
            if inferred_year_raw not in (None, "")
            else None
        )

        added_tracks = 0

        for track in release_tracks:
            track_title = (track.get("title") or "").strip()

            if not track_title:
                continue

            track_artist = (
                track.get("artist")
                or artist_name
                or ""
            ).strip() or artist_name

            track_duration = track.get("duration")

            if track_duration:
                try:
                    track_duration = int(track_duration) // 1000
                except Exception:
                    track_duration = None

            result = add_to_queue(
                artist=track_artist,
                title=track_title,
                album=album_name,
                source="soulseek",
                album_artist=artist_name,
                release_mbid=mbid,
                release_id=mbid,
                release_source="musicbrainz",
                track_number=track.get("track_number"),
                disc_number=track.get("disc_number"),
                recording_mbid=track.get("recording_mbid") or track.get("id"),
                year=inferred_year,
                duration=track_duration,
            )

            if result.get("success"):
                added_tracks += 1

        logger.info(
            "[QUEUE_MATCH_BATCH] Expanded MBID %s: added %s track(s)",
            mbid,
            added_tracks,
        )

    except Exception as exc:
        logger.warning(
            "[QUEUE_MATCH_BATCH] Release expansion failed for MBID %s: %s",
            mbid,
            exc,
        )



# =============================================================================
# Release metadata / track lookups
# =============================================================================

def get_musicbrainz_release_metadata(
    release_id: str,
) -> Optional[Dict[str, Any]]:
    """
    Fetch release metadata from cache first, then MusicBrainz live service.

    Removes old legacy dependency:
        from post_download_processor import fetch_musicbrainz_release_metadata
    """

    if not release_id:
        return None

    cached_metadata = None

    if get_cached_release_metadata:
        try:
            cached_metadata = get_cached_release_metadata(release_id)

            if cached_metadata and cached_metadata.get("tracks"):
                return cached_metadata

        except Exception as cache_err:
            logger.debug(
                "Could not read cached MusicBrainz metadata for %s: %s",
                release_id,
                cache_err,
            )

    try:
        live_metadata = fetch_musicbrainz_release_metadata(release_id)

        if live_metadata and live_metadata.get("tracks") and cache_release_metadata:
            try:
                cache_release_metadata(release_id, live_metadata)
            except Exception as cache_write_err:
                logger.debug(
                    "Could not cache MusicBrainz metadata for %s: %s",
                    release_id,
                    cache_write_err,
                )

        return live_metadata or cached_metadata

    except Exception as live_err:
        logger.error(
            "Error fetching MusicBrainz release metadata for %s: %s",
            release_id,
            live_err,
        )
        return cached_metadata


def get_release_tracks(
    release_id: str,
    source: str = "musicbrainz",
) -> List[Dict[str, Any]]:
    """
    Fetch full track listing for a MusicBrainz or Discogs release.
    """

    if not release_id:
        return []

    try:
        if source == "musicbrainz":
            metadata = get_musicbrainz_release_metadata(release_id)
            return metadata.get("tracks", []) if metadata else []

        if source == "discogs":
            if not fetch_discogs_tracks:
                logger.warning("Discogs track fetcher is not available")
                return []

            return fetch_discogs_tracks(release_id)

        logger.error("Unknown release source: %s", source, exc_info=True)
        return []

    except Exception as exc:
        logger.error(
            "Error fetching %s release tracks for %s: %s",
            source,
            release_id,
            exc,
        )
        return []


def get_musicbrainz_release_tracks(
    release_id: str,
    source: str = "musicbrainz",
) -> List[Dict[str, Any]]:
    """
    Backwards-compatible wrapper for legacy callers.
    """

    if not release_id:
        return []

    return get_release_tracks(
        release_id=release_id,
        source=source,
    )




def _find_matching_queue_item(
    track: dict[str, Any],
    queue_items: list[dict[str, Any]],
    used_queue_ids: set[int],
) -> dict[str, Any] | None:
    """
    Match a release track against queue items.

    Priority:
        1. Track number
        2. Title fallback
    """

    track_num, _ = extract_track_disc(
        str(track.get("track_number") or "")
    )

    title_norm = normalize_match_text(
        track.get("title") or ""
    )

    if track_num is not None:
        for candidate in queue_items:
            if candidate.get("id") in used_queue_ids:
                continue

            candidate_num, _ = extract_track_disc(
                str(candidate.get("track_number") or "")
            )

            if candidate_num == track_num:
                return candidate

    for candidate in queue_items:
        if candidate.get("id") in used_queue_ids:
            continue

        candidate_title_norm = normalize_match_text(
            candidate.get("title") or ""
        )

        if (
            candidate_title_norm
            and candidate_title_norm == title_norm
        ):
            return candidate

    return None

def get_release_tracks_with_status(
    artist: str,
    album: str,
    release_id: str,
    current_folder_files: list[str] | None = None,
) -> dict[str, Any]:
    """
    Return release tracks with queue/library status.

    Status values:
        downloading
        in_folder
        other_folder
        missing
    """

    release_tracks = get_release_tracks(
        release_id=release_id,
        source="musicbrainz",
    )

    if not release_tracks:
        return {
            "success": False,
            "tracks": [],
            "summary": {},
        }

    queue_items = get_album_queue_tracks(
        artist=artist,
        album=album,
    )

    current_folder_files = [
        f.lower()
        for f in (current_folder_files or [])
    ]

    used_queue_ids: set[int] = set()

    tracks_with_status: list[dict[str, Any]] = []

    for track in release_tracks:
        status = "missing"
        status_details: dict[str, Any] = {}

        queue_item = _find_matching_queue_item(
            track,
            queue_items,
            used_queue_ids,
        )

        # ---------------------------------------------------------
        # Queue match
        # ---------------------------------------------------------
        if queue_item:
            used_queue_ids.add(
                int(queue_item["id"])
            )

            status = "downloading"

            status_details = {
                "queue_id": queue_item.get("id"),
                "queue_status": queue_item.get("status"),
                "queue_title": queue_item.get("title"),
                "queue_track_number": queue_item.get(
                    "track_number"
                ),
            }

        # ---------------------------------------------------------
        # Current folder match
        # ---------------------------------------------------------
        elif current_folder_files:
            track_title_norm = normalize_match_text(
                track.get("title") or ""
            )

            for filename in current_folder_files:
                filename_norm = normalize_match_text(
                    filename
                )

                if (
                    track_title_norm
                    and (
                        track_title_norm in filename_norm
                        or filename_norm in track_title_norm
                    )
                ):
                    status = "in_folder"

                    status_details = {
                        "filename": filename,
                    }

                    break

        # ---------------------------------------------------------
        # Existing library match
        # ---------------------------------------------------------
        if status == "missing":
            library_track = find_library_track(
                artist=artist,
                title=track.get("title") or "",
                album=album,
            )

            if library_track:
                status = "other_folder"

                status_details = {
                    "track_id": library_track.get("id"),
                    "file_path": library_track.get(
                        "file_path"
                    ),
                    "album": library_track.get(
                        "album"
                    ),
                }

        tracks_with_status.append(
            {
                **track,
                "status": status,
                "status_details": status_details,
            }
        )

    statuses = [
        track["status"]
        for track in tracks_with_status
    ]

    return {
        "success": True,
        "summary": {
            "total": len(tracks_with_status),
            "downloading": statuses.count(
                "downloading"
            ),
            "in_folder": statuses.count(
                "in_folder"
            ),
            "other_folder": statuses.count(
                "other_folder"
            ),
            "missing": statuses.count(
                "missing"
            ),
        },
        "tracks": tracks_with_status,
    }