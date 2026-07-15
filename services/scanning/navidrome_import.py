"""Navidrome artist import service.

Core service for importing artist/album/track data from Navidrome into
the Popularr database. Coordinates between Navidrome API calls, metadata
processing, and database persistence.

Key Functions:
    - scan_artist_to_db(): Full artist import pipeline (API fetch, metadata
      extraction, DB upsert).
    - fetch_artist_albums(): Get all albums for an artist from Navidrome.
    - fetch_album_tracks(): Get all tracks for an album from Navidrome.

Architecture:
    Uses ``db.repositories.tracks.upsert_track_payload`` for PostgreSQL-safe
    writes. Compatible with both old-style direct calls and new repository
    path. Cleanup hooks run after import to maintain data integrity.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

from api_clients.navidrome import NavidromeClient
from db.repositories.tracks import upsert_track_payload
from sqlalchemy import text
from db.engine import db_session
from helpers.logging_config import log_unified
from helpers.text_utils import _clean_artist_name_for_storage
from services.scanning.cleanup import (
    cleanup_empty_artist_dirs,
    cleanup_stale_album_tracks_if_needed,
    cleanup_stale_artist_tracks_if_needed,
    normalize_existing_artist_rows_safe,
    sanitize_artist_rows_safe,
)
from services.scanning.filters import should_skip_album, should_skip_cached_album
from services.scanning.metadata_extractor import extract_track_metadata
from services.scanning.payload_builder import build_track_payload

VA_ALBUM_ARTIST_VARIANTS = frozenset({
    "various artists", "various", "v/a", "va", "compilation", "original soundtrack",
})


def _get_fallback_client() -> NavidromeClient | None:
    """Return a properly configured Navidrome client from the scan service.

    Returns None when Navidrome is not configured so callers can degrade
    gracefully instead of crashing or producing malformed requests.
    """
    try:
        from services.scanning.navidrome_scan_service import get_nav_client
        return get_nav_client()
    except RuntimeError:
        return None
    except Exception:
        return None


def fetch_artist_albums(artist_id: str, client: NavidromeClient | None = None) -> list[dict[str, Any]]:
    """Fetch artist albums using an explicit client, legacy start.py helper, or fallback client."""
    if client is not None:
        return client.fetch_artist_albums(artist_id) or []

    try:
        from start import fetch_artist_albums as existing_fetch_artist_albums
        return existing_fetch_artist_albums(artist_id) or []
    except Exception:
        fb = _get_fallback_client()
        if fb:
            return fb.fetch_artist_albums(artist_id) or []
        return []


def fetch_album_tracks(album_id: str, client: NavidromeClient | None = None) -> dict[str, Any]:
    """Fetch album tracks using an explicit client, legacy start.py helper, or fallback client."""
    if client is not None:
        return client.fetch_album_tracks(album_id) or {}

    try:
        from start import fetch_album_tracks as existing_fetch_album_tracks
        return existing_fetch_album_tracks(album_id) or {}
    except Exception:
        fb = _get_fallback_client()
        if fb:
            return fb.fetch_album_tracks(album_id) or {}
        return {}


def save_to_db(track_payload: dict[str, Any]) -> None:
    """Persist a track payload through the PostgreSQL-safe tracks repository.

    This replaces the previous fallback to ``popularity_helpers.save_to_db`` so
    the import path stays PostgreSQL-only and avoids old SQLite-era behaviour.
    """
    upsert_track_payload(track_payload)


def detect_live_album(album_name: str) -> dict[str, bool]:
    """Return lightweight live/unplugged album context flags.
    
    Delegates to the canonical implementation in services.catalog.album_classification_service.
    """
    from services.catalog.album_classification_service import (
        is_live_or_alternate_album,
        detect_live_album_type,
    )
    is_live = is_live_or_alternate_album(album_name)
    album_type = detect_live_album_type(album_name)
    return {"is_live": is_live, "is_unplugged": album_type == "acoustic"}


def artist_album_name_diff(
    artist_name: str,
    artist_id: str,
    client: NavidromeClient | None = None,
) -> tuple[bool, set[str]]:
    """Compare Navidrome album names/counts to current DB album names/counts."""
    try:
        nav_albums = fetch_artist_albums(artist_id, client=client)
    except Exception as exc:
        logging.debug("[NAVIDROME_SCAN] Could not fetch albums for '%s': %s", artist_name, exc)
        return False, set()

    nav_names: set[str] = set()
    nav_counts: dict[str, int] = {}
    for album in nav_albums:
        name = album.get("name") or ""
        if name:
            nav_names.add(name)
            nav_counts[name] = int(album.get("songCount", 0) or 0)

    db_names: set[str] = set()
    db_counts: dict[str, int] = {}

    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT album, COUNT(*) as track_count
                    FROM tracks
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
                      AND album IS NOT NULL AND TRIM(album) <> ''
                    GROUP BY album
                """),
                {"artist": artist_name},
            )
            for row in result.fetchall() or []:
                album_name = str(row[0])
                count = int(row[1] or 0)
                if album_name:
                    db_names.add(album_name)
                    db_counts[album_name] = count
    except Exception as exc:
        logging.debug("[NAVIDROME_SCAN] Could not query DB albums for '%s': %s", artist_name, exc)
        return False, set()

    changed = nav_names.symmetric_difference(db_names)
    for album in nav_names & db_names:
        if nav_counts.get(album, 0) != db_counts.get(album, 0):
            changed.add(album)

    return (not changed), changed


def album_artist_from_sources(
    *,
    api_album_artist: str,
    album_artist_from_artist_view: str,
    canonical_artist_name: str,
    album_name: str,
    existing_album_artists: dict[str, str],
) -> str:
    """Resolve album_artist while preserving existing Various Artists-style values."""
    album_artist_value = (
        _clean_artist_name_for_storage(api_album_artist or album_artist_from_artist_view or canonical_artist_name)
        or canonical_artist_name
    )

    existing_album_artist = existing_album_artists.get(album_name, "")
    existing_is_va = existing_album_artist.lower().strip() in VA_ALBUM_ARTIST_VARIANTS
    new_is_va = album_artist_value.lower().strip() in VA_ALBUM_ARTIST_VARIANTS

    if existing_album_artist and existing_is_va and not new_is_va:
        return existing_album_artist

    return album_artist_value


def fetch_album_tracks_safe(
    *,
    album_id: str,
    album_name: str,
    client: NavidromeClient | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Fetch tracks for one album without failing the whole artist scan."""
    try:
        album_data = fetch_album_tracks(album_id, client=client) or {}
        return album_data.get("tracks", []) or [], album_data.get("artist", "") or ""
    except Exception as err:
        logging.debug("Failed to fetch tracks for album '%s': %s", album_name, err)
        return [], ""


def extract_and_backfill_track_metadata(
    *,
    navi_client: Any,
    track: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """Extract Navidrome metadata and writer JSON.

    Kept as a compatibility wrapper, but implemented via metadata_extractor.
    """
    get_song = getattr(navi_client, "get_song", None)
    extracted = extract_track_metadata(track, get_song=get_song)
    writer_json = extracted.get("writer", "[]") or "[]"
    return extracted, writer_json


def prefetch_artist_state(*, canonical_artist_name: str) -> dict[str, Any]:
    """Read existing DB state needed for one artist scan."""
    existing_track_ids: set[str] = set()
    existing_album_tracks: dict[str, set[str]] = {}
    existing_album_artists: dict[str, str] = {}

    with db_session() as session:
        result = session.execute(
            text("""
                SELECT id, album, album_artist
                FROM tracks
                WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
            """),
            {"artist": canonical_artist_name},
        )

        for row in result.fetchall() or []:
            track_id = row[0]
            album = row[1]
            album_artist = row[2]

            if track_id:
                existing_track_ids.add(str(track_id))
            if album and track_id:
                existing_album_tracks.setdefault(str(album), set()).add(str(track_id))
            if album and album_artist:
                existing_album_artists[str(album)] = str(album_artist)

    return {
        "existing_track_ids": existing_track_ids,
        "navidrome_track_ids": set(),
        "existing_album_tracks": existing_album_tracks,
        "existing_album_artists": existing_album_artists,
        "albums_needing_reimport": set(),
    }


def scan_artist_to_db(
    artist_name: str,
    artist_id: str,
    verbose: bool = False,
    force: bool = False,
    filter_missing: bool = False,
    processed_artists: int = 0,
    total_artists: int = 0,
    album_filter: str | None = None,
    progress_file: str | None = None,
    progress_scan_type: str | None = None,
    diff_mode: bool = False,
    client: NavidromeClient | None = None,
):
    """Scan one Navidrome artist and upsert local PostgreSQL track rows."""
    logger.info("[NAVIDROME_IMPORT] Importing artist: %s (artist_id=%s, force=%s, processed=%s)",
                 artist_name, artist_id or "none", force, processed_artists)

    if not artist_id:
        logging.warning("[NAVIDROME_SCAN] No artist_id for '%s'", artist_name)
        return None

    canonical_artist_name = _clean_artist_name_for_storage(artist_name) or artist_name
    active_client = client

    try:
        state = prefetch_artist_state(canonical_artist_name=canonical_artist_name)

        existing_track_ids = state["existing_track_ids"]
        navidrome_track_ids = state["navidrome_track_ids"]
        existing_album_tracks = state["existing_album_tracks"]
        existing_album_artists = state["existing_album_artists"]
        albums_needing_reimport = state["albums_needing_reimport"]

        normalize_existing_artist_rows_safe(
            artist_name=artist_name,
            canonical_artist_name=canonical_artist_name,
        )
        sanitize_artist_rows_safe(canonical_artist_name=canonical_artist_name)

        changed_album_names: set[str] | None = None

        if diff_mode and not force and not album_filter and not filter_missing:
            skip_artist, changed_album_names = artist_album_name_diff(
                canonical_artist_name,
                artist_id,
                client=active_client,
            )
            if skip_artist:
                return {"changed": False, "changed_albums": 0}

        albums = fetch_artist_albums(artist_id, client=active_client) or []

        # Only create fallback client for getSong metadata extraction if a real
        # client was not passed and the legacy start.py helpers were used for
        # album/track fetching.
        navi_client = active_client or _get_fallback_client()

        for album_index, album in enumerate(albums, 1):
            album_name = album.get("name") or ""

            if should_skip_album(
                album_name=album_name,
                album_filter=album_filter,
                filter_missing=filter_missing,
                albums_needing_reimport=albums_needing_reimport,
                diff_mode=diff_mode,
                changed_album_names=changed_album_names,
            ):
                continue

            album_id = album.get("id")
            if not album_id:
                continue

            logging.info("   💿 [Album %s/%s] %s", album_index, len(albums), album_name)
            log_unified(f"Navidrome Import - {artist_name} - Album {album_index}/{len(albums)}: {album_name}")

            album_context = detect_live_album(album_name)
            tracks, api_album_artist = fetch_album_tracks_safe(
                album_id=album_id,
                album_name=album_name,
                client=active_client,
            )

            for track in tracks:
                if track.get("id"):
                    navidrome_track_ids.add(track.get("id"))

            cached_ids_for_album = existing_album_tracks.get(album_name, set())
            album_needs_reimport = album_name in albums_needing_reimport

            if should_skip_cached_album(
                artist_name=artist_name,
                album_name=album_name,
                tracks=tracks,
                cached_ids_for_album=cached_ids_for_album,
                force=force,
                album_needs_reimport=album_needs_reimport,
                verbose=verbose,
            ):
                continue

            album_artist_value = album_artist_from_sources(
                api_album_artist=api_album_artist,
                album_artist_from_artist_view=album.get("artist", "") or "",
                canonical_artist_name=canonical_artist_name,
                album_name=album_name,
                existing_album_artists=existing_album_artists,
            )

            album_mbids_seen: set[str] = set()

            for track in tracks:
                if not track.get("id"):
                    continue

                extracted, writer_json = extract_and_backfill_track_metadata(
                    navi_client=navi_client,
                    track=track,
                )

                payload = build_track_payload(
                    track=track,
                    extracted=extracted,
                    album_name=album_name,
                    album_artist_value=album_artist_value,
                    album_context=album_context,
                    canonical_artist_name=canonical_artist_name,
                    writer_json=writer_json,
                    is_new_track=track.get("id") not in cached_ids_for_album,
                )

                album_mbid = str(payload.get("musicbrainz_album_mbid") or "").strip()
                if album_mbid:
                    album_mbids_seen.add(album_mbid)

                save_to_db(payload)

            if len(album_mbids_seen) > 1:
                logging.warning(
                    "[NAVIDROME_SCAN] Album MBID inconsistency for '%s - %s': %s MBIDs",
                    artist_name,
                    album_name,
                    len(album_mbids_seen),
                )

            if diff_mode:
                cleanup_stale_album_tracks_if_needed(
                    artist_name=artist_name,
                    album_name=album_name,
                    cached_ids_for_album=cached_ids_for_album,
                    navidrome_tracks=tracks,
                )

        if not filter_missing and not album_filter and not diff_mode:
            cleanup_stale_artist_tracks_if_needed(
                artist_name=artist_name,
                existing_track_ids=existing_track_ids,
                navidrome_track_ids=navidrome_track_ids,
            )
            cleanup_empty_artist_dirs(
                artist_name=artist_name,
                canonical_artist_name=canonical_artist_name,
            )

        if diff_mode and changed_album_names is not None:
            return {"changed": True, "changed_albums": len(changed_album_names)}

        return None

    except Exception:
        logging.error("scan_artist_to_db failed for %s", artist_name, exc_info=True)
        raise


def pre_import_sync_album_artists(artist_id: str | None = None) -> dict[str, Any]:
    """Compatibility placeholder for album-artist pre-sync.

    Keep your richer implementation if you have one elsewhere. This placeholder
    remains so callers do not fail while the pre-sync behaviour is migrated.
    """
    return {
        "success": True,
        "unique_album_artists": 0,
        "new_artists_created": 0,
        "existing_artists": 0,
        "sync_time_ms": 0,
        "new_artists": [],
    }
